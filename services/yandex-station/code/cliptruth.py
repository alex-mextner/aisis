"""Owner-verified truth for archived voice clips, and the Telegram message → clip mapping behind it.

The recognizer's own transcript is never ground truth. The owner hears each clip in Telegram
(clipfeed.py), replies with what was really said, and the reply lands here:

  clip_feed   one row per clip the feed looked at: the Telegram message id it went out as, or why
              it was skipped. A reply to a Telegram message resolves to a clip through this table.
  clip_truth  the owner's CURRENT verdict per clip. label: `correct` (the transcript is right), `corrected`
              (truth holds the real text) or `noise` (there was no speech; truth is "").
  clip_truth_history  append-only (triggers refuse UPDATE/DELETE): one row per verdict, version 1, 2, …; a re-correction
              adds a version and keeps the earlier ones. Each row snapshots what was heard at that moment (recognizer
              text, engine, the clip's front-end facts: trigger / silence / voiced ms / floor / contrast, and the ASR
              corrector's verdict when the dialog carries one) next to the truth, label, source and time.
              clip_truth is always the latest version. voice_clips.transcript is never modified.

Every verdict is mirrored into `asr_reference` (clip_id, text, source, ts), the table
bench_asr_archive.py scores candidate engines against, so each correction improves the benchmark
without a manual step. Same semantics there: text "" means "no speech".

WER is computed on read (transcript vs truth) with bench_asr_archive.wer, the one implementation the
benchmark uses, so the dashboard and the benchmark agree.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import time

import voiceclips

LABELS = ("correct", "corrected", "noise")
REFERENCE_SOURCE = "owner-tg"
MAX_TRUTH_CHARS = 500

# via: which bot posted the message ('tg' = the tg CLI's bot, 'bot' = the feed's own); caption: the posted text, kept so a verdict can be appended.
FEED_COLUMNS = (("via", "TEXT NOT NULL DEFAULT 'tg'"), ("caption", "TEXT"))

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS clip_feed(clip_id TEXT PRIMARY KEY, tg_msg_id INTEGER, state TEXT NOT NULL, why TEXT, ts REAL NOT NULL, "
    "via TEXT NOT NULL DEFAULT 'tg', caption TEXT)",
    "CREATE INDEX IF NOT EXISTS clip_feed_msg ON clip_feed(tg_msg_id)",
    "CREATE TABLE IF NOT EXISTS clip_feed_state(k TEXT PRIMARY KEY, v TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS clip_truth(clip_id TEXT PRIMARY KEY, truth TEXT NOT NULL, "
    "label TEXT NOT NULL CHECK(label IN ('correct','corrected','noise')), source TEXT NOT NULL, ts REAL NOT NULL, tg_msg_id INTEGER)",
    "CREATE TABLE IF NOT EXISTS clip_truth_history(id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id TEXT NOT NULL, version INTEGER NOT NULL, "
    "truth TEXT NOT NULL, label TEXT NOT NULL, source TEXT NOT NULL, ts REAL NOT NULL, tg_msg_id INTEGER, heard TEXT, engine TEXT, "
    "frontend TEXT, asr_correction TEXT, UNIQUE(clip_id, version))",
    "CREATE TRIGGER IF NOT EXISTS clip_truth_history_no_update BEFORE UPDATE ON clip_truth_history "
    "BEGIN SELECT RAISE(ABORT, 'clip_truth_history is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS clip_truth_history_no_delete BEFORE DELETE ON clip_truth_history "
    "BEGIN SELECT RAISE(ABORT, 'clip_truth_history is append-only'); END",
    # Same definition as bench_asr_archive.SCHEMA; whichever module connects first creates it.
    "CREATE TABLE IF NOT EXISTS asr_reference(clip_id TEXT PRIMARY KEY, text TEXT NOT NULL, source TEXT, ts REAL NOT NULL)",
)

_NOISE_WORDS = {"шум", "не речь", "нет речи", "тишина", "мусор", "пусто", "не голос", "noise", "silence", "no speech"}
_CORRECT_WORDS = {"=", "верно", "правильно", "точно", "все верно", "все правильно", "ок", "ok", "yes", "да верно"}


class TruthError(ValueError):
    """A reply that cannot be turned into a verdict (unknown clip, empty text, too long)."""


def connect(db_path: str) -> sqlite3.Connection:
    conn = voiceclips.connect(db_path)  # creates voice_clips too, so joins never hit a missing table
    for statement in SCHEMA:
        conn.execute(statement)
    have = {row[1] for row in conn.execute("PRAGMA table_info(clip_feed)")}
    for column, definition in FEED_COLUMNS:
        if column not in have:
            try:
                conn.execute("ALTER TABLE clip_feed ADD COLUMN %s %s" % (column, definition))
            except sqlite3.OperationalError:  # another process migrated first
                pass
    conn.row_factory = sqlite3.Row
    _backfill_history(conn)
    return conn


FRONTEND_COLUMNS = ("rate", "trigger", "status", "reason", "silence_ms", "voiced_ms", "floor_rms", "contrast", "asr_ms", "final_ms", "peak", "rms")


def _snapshot(conn: sqlite3.Connection, clip_id: str) -> tuple[str, str | None, str, str | None] | None:
    """(heard, engine, front-end facts as JSON, ASR corrector entry as JSON or None) of a clip as archived; None if it is gone."""
    clip = conn.execute("SELECT transcript, engine, dialog_id, %s FROM voice_clips WHERE id=?" % ",".join(FRONTEND_COLUMNS), (clip_id,)).fetchone()
    if clip is None:
        return None
    heard = (clip["transcript"] or "").strip()
    correction = None
    if clip["dialog_id"] and heard:
        try:
            row = conn.execute("SELECT actions FROM dialogs WHERE id=?", (clip["dialog_id"],)).fetchone()
            for entry in json.loads(row["actions"]) if row else []:
                if str(entry.get("kind", "")).startswith("коррекция") and _words(entry.get("text", "")) == _words(heard):
                    correction = json.dumps(entry, ensure_ascii=False)
                    break
        except (sqlite3.OperationalError, ValueError, AttributeError):  # no dialogs table yet / unparsable row
            pass
    frontend = {k: clip[k] for k in FRONTEND_COLUMNS}
    try:  # processed copies of the clip (front-end variants) and the engine that listened to each, when the archive keeps them
        frontend["variants"] = [{"variant": v["variant"], "engine": v["engine"]} for v in conn.execute(
            "SELECT variant, engine FROM voice_clip_variants WHERE clip_id=? ORDER BY variant", (clip_id,))]
    except sqlite3.OperationalError:
        pass
    return heard, clip["engine"], json.dumps(frontend), correction


def _append_history(conn: sqlite3.Connection, clip_id: str, truth: str, label: str, source: str, ts: float, tg_msg_id: int | None) -> int:
    """Add the next version of a clip's verdict with a fresh snapshot of what was heard; returns the version."""
    heard, engine, frontend, correction = _snapshot(conn, clip_id) or ("", None, "{}", None)
    version = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM clip_truth_history WHERE clip_id=?", (clip_id,)).fetchone()[0]
    conn.execute("INSERT INTO clip_truth_history(clip_id, version, truth, label, source, ts, tg_msg_id, heard, engine, frontend, asr_correction) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (clip_id, version, truth, label, source, ts, tg_msg_id, heard, engine, frontend, correction))
    return version


def _backfill_history(conn: sqlite3.Connection) -> None:
    """Verdicts stored before the history existed become version 1 (snapshot taken now: the transcript never changes)."""
    missing = conn.execute("SELECT t.* FROM clip_truth t WHERE NOT EXISTS (SELECT 1 FROM clip_truth_history h WHERE h.clip_id=t.clip_id)").fetchall()
    if missing:
        try:
            with conn:
                for row in missing:
                    _append_history(conn, row["clip_id"], row["truth"], row["label"], row["source"], row["ts"], row["tg_msg_id"])
        except (sqlite3.OperationalError, sqlite3.IntegrityError):
            pass  # another connection is backfilling the same rows; it wins


def _words(text: str) -> str:
    text = re.sub(r"[^\w\s=]", " ", (text or "").lower().replace("ё", "е"))
    return " ".join(text.split())


def parse_reply(text: str) -> tuple[str, str | None]:
    """(label, truth) from the owner's reply. truth is None for `correct` (it is the transcript) and "" for `noise`."""
    cleaned = (text or "").strip().strip("«»\"'").strip()
    if not cleaned:
        raise TruthError("пустой ответ")
    words = _words(cleaned)
    if words in _NOISE_WORDS:
        return "noise", ""
    if words in _CORRECT_WORDS:
        return "correct", None
    body = re.sub(r"^\s*[=→:]\s*", "", cleaned).strip().strip("«»\"'").strip()
    if not body:
        raise TruthError("пустой ответ")
    return "corrected", body


def clip_for_message(conn: sqlite3.Connection, tg_msg_id: int) -> str | None:
    """Clip of a message posted through the `tg` CLI (the feed bot's message ids live in another chat: via='bot')."""
    row = conn.execute("SELECT clip_id FROM clip_feed WHERE tg_msg_id=? AND via='tg' AND state='sent'", (tg_msg_id,)).fetchone()
    return row["clip_id"] if row else None


def record_feed(conn: sqlite3.Connection, clip_id: str, state: str, why: str | None, tg_msg_id: int | None, ts: float,
                via: str = "tg", caption: str | None = None) -> None:
    """One clip_feed row: posted (state sent + message id), skipped (why) or failed."""
    with conn:
        conn.execute("INSERT OR REPLACE INTO clip_feed(clip_id, tg_msg_id, state, why, ts, via, caption) VALUES(?,?,?,?,?,?,?)",
                     (clip_id, tg_msg_id, state, why, ts, via, caption))


def set_truth(db_path: str, clip_id: str, label: str, truth: str | None = None, source: str = "tg",
              tg_msg_id: int | None = None) -> dict:
    """Store the verdict for one clip as a new history version (an identical repeat adds nothing), make it the current one and
    mirror it into asr_reference. Returns the stored row with `version` and, when it replaced one, `previous`."""
    if label not in LABELS:
        raise TruthError("label must be one of %s" % ", ".join(LABELS))
    conn = connect(db_path)
    try:
        clip = conn.execute("SELECT transcript FROM voice_clips WHERE id=?", (clip_id,)).fetchone()
        if clip is None:
            raise TruthError("клип %s не найден в архиве" % clip_id)
        heard = (clip["transcript"] or "").strip()
        if label == "correct":
            truth = heard
            if not heard:
                label = "noise"  # "the transcript is right" about an empty transcript means there was no speech
        elif label == "noise":
            truth = ""
        else:
            truth = (truth or "").strip()
            if not truth:
                raise TruthError("для corrected нужен текст")
            if len(truth) > MAX_TRUTH_CHARS:
                raise TruthError("текст длиннее %d символов" % MAX_TRUTH_CHARS)
            if _words(truth) == _words(heard):
                label = "correct"  # a retyped identical transcript is a confirmation, not a correction
        now = time.time()
        current = conn.execute("SELECT truth, label, source, ts, tg_msg_id FROM clip_truth WHERE clip_id=?", (clip_id,)).fetchone()
        if current is not None and (current["truth"], current["label"]) == (truth, label):
            version = conn.execute("SELECT MAX(version) FROM clip_truth_history WHERE clip_id=?", (clip_id,)).fetchone()[0]
            return {"clip_id": clip_id, "truth": truth, "label": label, "source": current["source"], "ts": current["ts"],
                    "tg_msg_id": current["tg_msg_id"], "heard": heard, "wer": wer_of(heard, truth, label), "version": version,
                    "previous": None, "unchanged": True}
        with conn:
            version = _append_history(conn, clip_id, truth, label, source, now, tg_msg_id)
            conn.execute("INSERT OR REPLACE INTO clip_truth(clip_id, truth, label, source, ts, tg_msg_id) VALUES(?,?,?,?,?,?)",
                         (clip_id, truth, label, source, now, tg_msg_id))
            conn.execute("INSERT OR REPLACE INTO asr_reference(clip_id, text, source, ts) VALUES(?,?,?,?)",
                         (clip_id, truth, REFERENCE_SOURCE, now))
        return {"clip_id": clip_id, "truth": truth, "label": label, "source": source, "ts": now, "tg_msg_id": tg_msg_id,
                "heard": heard, "wer": wer_of(heard, truth, label), "version": version, "unchanged": False,
                "previous": None if current is None else {"truth": current["truth"], "label": current["label"]}}
    finally:
        conn.close()


def _token(word: str) -> str:
    return re.sub(r"[^\w]", "", word.lower().replace("ё", "е"))


def word_diff(heard: str | None, truth: str) -> list[dict]:
    """Word-level «было → стало»: [{"op": "same"|"del"|"ins", "text": …}] — `del` words are in what was heard only, `ins` in
    the truth only. Case and punctuation do not count as a difference."""
    old, new = (heard or "").split(), truth.split()
    parts: list[dict] = []

    def add(op: str, words: list[str]) -> None:
        if words:
            if parts and parts[-1]["op"] == op:
                parts[-1]["text"] += " " + " ".join(words)
            else:
                parts.append({"op": op, "text": " ".join(words)})

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=[_token(w) for w in old], b=[_token(w) for w in new], autojunk=False).get_opcodes():
        if tag == "equal":
            add("same", new[j1:j2])
        else:
            add("del", old[i1:i2])
            add("ins", new[j1:j2])
    return parts


def versions(db_path: str, clip_id: str) -> list[dict]:
    """The clip's whole verdict history, newest first, each with its snapshot and the word diff heard → truth."""
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT version, truth, label, source, ts, tg_msg_id, heard, engine, frontend, asr_correction "
                            "FROM clip_truth_history WHERE clip_id=? ORDER BY version DESC", (clip_id,)).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        item = dict(row)
        item["frontend"] = json.loads(item["frontend"] or "{}")
        item["asr_correction"] = json.loads(item["asr_correction"]) if item["asr_correction"] else None
        item["diff"] = word_diff(item["heard"], item["truth"])
        item["wer"] = wer_of(item["heard"], item["truth"], item["label"])
        out.append(item)
    return out


def export_rows(db_path: str, history: bool = False) -> list[dict]:
    """Training/benchmark export: one row per judged clip (the current verdict) or, with `history`, per verdict version.
    `audio` is the clip file on this host (never uploaded); `heard` is the recognizer text at correction time."""
    conn = connect(db_path)
    try:
        where = "" if history else "WHERE h.version=(SELECT MAX(version) FROM clip_truth_history WHERE clip_id=h.clip_id)"
        rows = conn.execute(
            "SELECT h.clip_id, h.version, h.truth, h.label, h.source, h.ts AS truth_ts, h.heard, h.engine, h.frontend, h.asr_correction, "
            "(SELECT COUNT(*) FROM clip_truth_history WHERE clip_id=h.clip_id) AS versions, c.path, c.ts AS clip_ts, c.dur "
            "FROM clip_truth_history h LEFT JOIN voice_clips c ON c.id=h.clip_id %s ORDER BY h.ts, h.version" % where).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        item = dict(row)
        item["audio"] = str(voiceclips.root() / item.pop("path")) if item["path"] else None
        item["frontend"] = json.loads(item["frontend"] or "{}")
        item["asr_correction"] = json.loads(item["asr_correction"]) if item["asr_correction"] else None
        out.append(item)
    return out


def wer_of(heard: str | None, truth: str, label: str) -> float | None:
    """WER of the recognizer's text against the truth; None for noise clips (there is no reference text to score)."""
    if label == "noise":
        return None
    from bench_asr_archive import wer  # deferred: the benchmark module pulls in thread-cap env setup
    return round(wer(truth, heard or ""), 4)


def annotate(db_path: str, clips: list[dict]) -> None:
    """Add `truth`, `truth_label`, `wer`, `diff` (word-level heard → truth) and `versions` to clip dicts (None when the owner has not judged the clip). Never raises."""
    ids = [c["id"] for c in clips]
    if not ids:
        return
    try:
        conn = connect(db_path)
        try:
            marks = ",".join("?" * len(ids))
            rows = {r["clip_id"]: r for r in conn.execute(
                "SELECT clip_id, truth, label FROM clip_truth WHERE clip_id IN (%s)" % marks, ids)}
            counts = dict(conn.execute("SELECT clip_id, COUNT(*) FROM clip_truth_history WHERE clip_id IN (%s) GROUP BY clip_id" % marks, ids).fetchall())
        finally:
            conn.close()
    except sqlite3.Error:
        rows, counts = {}, {}
    for clip in clips:
        row = rows.get(clip["id"])
        clip["truth"] = row["truth"] if row else None
        clip["truth_label"] = row["label"] if row else None
        clip["wer"] = wer_of(clip.get("transcript"), row["truth"], row["label"]) if row else None
        clip["diff"] = word_diff(clip.get("transcript"), row["truth"]) if row else None
        clip["versions"] = counts.get(clip["id"], 0)


def stats(db_path: str) -> dict:
    """Totals and recognizer quality over every judged clip, overall and per engine."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT t.clip_id, t.truth, t.label, c.transcript, c.engine FROM clip_truth t JOIN voice_clips c ON c.id=t.clip_id").fetchall()
        fed = conn.execute("SELECT COUNT(*) FROM clip_feed WHERE state='sent'").fetchone()[0]
    finally:
        conn.close()
    by_engine: dict[str, dict] = {}
    labels = {name: 0 for name in LABELS}
    for row in rows:
        labels[row["label"]] += 1
        slot = by_engine.setdefault(row["engine"] or "?", {"n": 0, "speech": 0, "wer_sum": 0.0, "exact": 0, "noise": 0, "text_on_noise": 0})
        slot["n"] += 1
        if row["label"] == "noise":
            slot["noise"] += 1
            slot["text_on_noise"] += 1 if (row["transcript"] or "").strip() else 0
        else:
            value = wer_of(row["transcript"], row["truth"], row["label"])
            slot["speech"] += 1
            slot["wer_sum"] += value
            slot["exact"] += 1 if value == 0 else 0
    engines = {}
    for name, s in by_engine.items():
        engines[name] = {"n": s["n"], "speech": s["speech"], "noise": s["noise"], "text_on_noise": s["text_on_noise"],
                         "wer": round(s["wer_sum"] / s["speech"], 4) if s["speech"] else None,
                         "exact": round(s["exact"] / s["speech"], 4) if s["speech"] else None}
    speech = sum(s["speech"] for s in by_engine.values())
    wer_sum = sum(s["wer_sum"] for s in by_engine.values())
    exact = sum(s["exact"] for s in by_engine.values())
    return {"judged": len(rows), "labels": labels, "fed": fed, "speech": speech,
            "wer": round(wer_sum / speech, 4) if speech else None,
            "exact": round(exact / speech, 4) if speech else None,
            "text_on_noise": sum(s["text_on_noise"] for s in by_engine.values()),
            "engines": engines}


LIST_MODES = ("judged", "pending", "all")
_MODE_WHERE = {"judged": "t.clip_id IS NOT NULL",
               "pending": "t.clip_id IS NULL AND f.state='sent' AND COALESCE(f.why,'')!='owner-skip'", "all": "1"}
_CLIP_COLS = "c.id, c.ts, c.dur, c.transcript, c.status, c.reason, c.engine, c.peak, c.rms"


def list_clips(db_path: str, mode: str = "judged", limit: int = 30, before: float | None = None) -> dict:
    """Clips newest first. `judged`: has a verdict; `pending`: sent to Telegram, no verdict yet; `all`: every archived clip."""
    if mode not in LIST_MODES:
        raise TruthError("mode must be one of %s" % ", ".join(LIST_MODES))
    limit = max(1, min(100, limit))
    conn = connect(db_path)
    try:
        args: list = []
        where = _MODE_WHERE[mode]
        if before is not None:
            where += " AND c.ts<?"
            args.append(before)
        rows = conn.execute(
            "SELECT %s, t.truth, t.label AS truth_label, t.source AS truth_source, f.tg_msg_id FROM voice_clips c "
            "LEFT JOIN clip_truth t ON t.clip_id=c.id LEFT JOIN clip_feed f ON f.clip_id=c.id AND f.state='sent' "
            "WHERE %s ORDER BY c.ts DESC LIMIT ?" % (_CLIP_COLS, where), args + [limit + 1]).fetchall()
    finally:
        conn.close()
    clips = [dict(r) for r in rows[:limit]]
    counts = {}
    if clips:
        conn = connect(db_path)
        try:
            marks = ",".join("?" * len(clips))
            counts = dict(conn.execute("SELECT clip_id, COUNT(*) FROM clip_truth_history WHERE clip_id IN (%s) GROUP BY clip_id" % marks,
                                       [c["id"] for c in clips]).fetchall())
        finally:
            conn.close()
    for clip in clips:
        clip["wer"] = wer_of(clip["transcript"], clip["truth"], clip["truth_label"]) if clip["truth_label"] else None
        clip["diff"] = word_diff(clip["transcript"], clip["truth"]) if clip["truth_label"] else None
        clip["versions"] = counts.get(clip["id"], 0)
    return {"clips": clips, "hasMore": len(rows) > limit, "nextBefore": clips[-1]["ts"] if clips and len(rows) > limit else None}
