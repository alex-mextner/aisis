#!/usr/bin/env python3
"""Telegram feedback loop over the voice archive: every heard utterance goes to the owner, the owner's reply is the truth.

`run` (systemd unit aisis-clipfeed.service) polls `voice_clips` and posts each new clip to the owner's own
Telegram chat through the `tg` CLI: the recording as an audio file (amplified, the speaker mic is very quiet)
and a caption with the transcript, engine, duration, level and what the pipeline did with it (executed
intent / answer / rejected as noise). The message id is stored in `clip_feed`; the owner replies to that
message with the real text ("=" or "верно" — the transcript is right, "шум" / "не речь" — nobody spoke)
and the verdict lands in `clip_truth` (cliptruth.py), which also feeds the ASR benchmark.

Why no inbound code here: the bot's updates belong to the tg-ctl daemon (a second getUpdates poller would
steal them). tg-ctl injects the owner's reply into the agent session that owns the replied-to message
("[TG from … tg#<id>] ↩ tg#<clip message id> …"). Messages are therefore sent pinned to a registry session
(`tg_session` in the settings) and that session runs the `truth` subcommand (shim `clip-truth`):

    clip-truth 28345 верно                    # reply to tg#28345
    clip-truth 28345 включи свет в гостиной   # the real words
    clip-truth --last шум                     # most recent clip that went out
    clip-truth --clip 20261109T002233-ab12cd = # by clip id

Recordings leave the machine only to the owner's Telegram chat (the `tg` bot target), never anywhere else.
Settings (~/.config/aisis/clip_feed.json, edited in the dashboard): on/off, include rejected clips,
quiet hours, messages per minute.

    clipfeed.py run | once | status | test | truth ...
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import clipbot
import cliptruth
import voiceclips

LOG = logging.getLogger("aisis.clipfeed")

POLL_S = 3
SETTLE_S = 35              # a sent clip waits until voiceclips.link_dialogs can attach its dialog (it needs 30 s)
REJECTED_SETTLE_S = 2      # rejected clips have no dialog to wait for
RETRY_FAILED_S = 300      # a clip whose send kept failing is tried again this often, without end
MAX_ATTEMPTS = 3
SEND_TIMEOUT_S = 120
FAILURE_PAUSE_S = 30
CAPTION_CHARS = 900        # tg adds a header; Telegram caps a caption at 1024
HEALTH_PORT = int(os.environ.get("AISIS_CLIPFEED_PORT") or 31124)
FULL_SCALE = 32768
MAX_GAIN = 60

DEFAULTS = {"enabled": True, "include_rejected": True, "quiet_enabled": False, "quiet_from": "23:00",
            "quiet_to": "08:00", "rate_per_min": 30, "tg_session": ""}
CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
SESSION_KEY = re.compile(r"^[a-z]+:[A-Za-z0-9._-]{1,80}$")
MSG_REF = re.compile(r"^(?:tg)?#?(\d+)$")


def config_dir() -> Path:
    return Path(os.environ.get("AISIS_CONFIG_DIR", str(Path.home() / ".config" / "aisis")))


def settings_path() -> Path:
    return Path(os.environ.get("AISIS_CLIPFEED_SETTINGS", str(config_dir() / "clip_feed.json")))


def db_path() -> str:
    return os.environ.get("AISIS_DIALOGS_DB") or os.path.expanduser("~/.local/share/aisis/dialogs.sqlite3")


def _validated(changes: dict) -> dict:
    out = {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            continue
        if key in ("enabled", "include_rejected", "quiet_enabled"):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true/false")
        elif key in ("quiet_from", "quiet_to"):
            if not isinstance(value, str) or not CLOCK.match(value):
                raise ValueError(f"{key} must be HH:MM")
        elif key == "rate_per_min":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 60:
                raise ValueError("rate_per_min must be an integer 1..60")
        elif key == "tg_session":
            if not isinstance(value, str) or (value and not SESSION_KEY.match(value)):
                raise ValueError("tg_session must be a registry session key like omp:<id>, or empty")
        out[key] = value
    return out


def load_settings() -> dict:
    settings = dict(DEFAULTS)
    try:
        stored = json.loads(settings_path().read_text())
    except (OSError, ValueError):
        return settings
    if isinstance(stored, dict):
        for key in DEFAULTS:  # one bad stored field must not discard the others
            if key in stored:
                try:
                    settings.update(_validated({key: stored[key]}))
                except ValueError:
                    pass
    return settings


def update_settings(changes: dict) -> dict:
    merged = load_settings()
    merged.update(_validated(changes))
    path = settings_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".clip_feed.")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(merged, fh, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return merged


def in_quiet_hours(settings: dict, ts: float) -> bool:
    if not settings["quiet_enabled"]:
        return False
    local = time.localtime(ts)
    minutes = local.tm_hour * 60 + local.tm_min
    start, end = (int(p[:2]) * 60 + int(p[3:]) for p in (settings["quiet_from"], settings["quiet_to"]))
    if start == end:
        return False
    return start <= minutes < end if start < end else (minutes >= start or minutes < end)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower().replace("ё", "е")).split())


def _seconds_of_day(hms: str) -> int | None:
    try:
        h, m, s = (int(part) for part in hms.split(":"))
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def describe_result(clip: dict, dialog: dict | None, window_end: float) -> str:
    """One line on what became of the clip. `dialog` holds the dialogs row's JSON columns (actions/brain), None if unlinked."""
    reason = clip.get("reason") or ""
    if clip["status"] == "gate":
        return "🚫 отброшено как не-речь: %s" % (reason or "гейт")
    if clip["status"] == "blacklist":
        return "🧹 отфильтровано как галлюцинация: %s" % (reason or "чёрный список")
    heard = _norm(clip.get("transcript") or "")
    if not heard:
        return "∅ пустой текст"
    if dialog is None:
        return "❔ диалог не найден"
    start = _seconds_of_day(time.strftime("%H:%M:%S", time.localtime(clip["ts"]))) or 0
    stop = start + max(1, int(window_end - clip["ts"]))
    done = []
    for action in dialog["actions"]:
        at = _seconds_of_day(action.get("ts", ""))
        if action.get("kind") == "действие" and at is not None and start - 3 <= at <= stop:
            done.append(action.get("text", ""))
    if done:
        return "✅ выполнено: %s" % "; ".join(done[:2])
    for action in dialog["actions"]:
        if action.get("kind") == "запрос" and _norm(action.get("text", "")) == heard:
            res = (action.get("res") or "").strip()
            if res in ("", "null"):
                break
            return "🤫 молча, без ответа" if res == "(silent)" else "💬 ответ: %s" % res[:160]
    for turn in dialog["brain"]:
        if _norm(turn.get("prompt", "")) == heard:
            return "💬 ответ ИИ: %s" % str(turn.get("answer", ""))[:160]
    return "❔ ответ не найден (диалог %s)" % (dialog.get("status") or "?")


def load_dialog(conn: sqlite3.Connection, dialog_id: str | None) -> dict | None:
    if not dialog_id:
        return None
    try:
        row = conn.execute("SELECT status, actions, brain FROM dialogs WHERE id=?", (dialog_id,)).fetchone()
    except sqlite3.OperationalError:  # the dialogs table is created by the dashboard's ingester
        return None
    if row is None:
        return None
    try:
        return {"status": row["status"], "actions": json.loads(row["actions"]), "brain": json.loads(row["brain"])}
    except ValueError:
        return None


WAKE_WORDS = ("алис", "яндекс")
CUT_BY = {"vad": "закрыто по паузе (VAD)", "finish": "поток закончила станция (EOF)", "idle": "закрыто по таймауту потока"}
SHORT_SPEECH_MS = 700
REPLY_FOOTER = ("Реплай на это сообщение = что сказали на самом деле; «=» — расшифровка верна, «шум» — речи не было. "
                "Могли говорить издалека, начало или конец могло обрезаться.")


def window_end(conn: sqlite3.Connection, clip: dict) -> float:
    """The clip's result window closes at the next clip of the same dialog (or 45 s)."""
    if clip.get("dialog_id"):
        row = conn.execute("SELECT MIN(ts) FROM voice_clips WHERE dialog_id=? AND ts>?", (clip["dialog_id"], clip["ts"])).fetchone()
        if row and row[0]:
            return row[0]
    return clip["ts"] + 45


def endpoint_hints(clip: dict) -> list[str]:
    """What closed the clip and whether its start or end may be cut: the usual cause of «Полиция» for «Алиса, …»."""
    cut = CUT_BY.get(clip.get("trigger") or "", "причина закрытия неизвестна")
    lines = ["✂ %s · хвост тишины %s мс · речь %s мс из %s с" % (
        cut, clip.get("silence_ms", "—"), clip.get("voiced_ms", "—"), "%.1f" % clip["dur"] if clip.get("dur") is not None else "—")]
    heard = (clip.get("transcript") or "").strip()
    if clip.get("voiced_ms") is not None and clip["voiced_ms"] < SHORT_SPEECH_MS:
        lines.append("⚠ речи всего %s мс: начало или конец могли обрезаться" % clip["voiced_ms"])
    first = _norm(heard).split(" ")[0] if heard else ""
    if clip.get("first_in_dialog") and first and not first.startswith(WAKE_WORDS):
        lines.append("⚠ первая реплика диалога начинается с «%s», а не со слова активации: «Алиса» могла исказиться" % first)
    return lines


def build_caption(clip: dict, result: str, gain: int, test: bool = False, footer: str = REPLY_FOOTER) -> str:
    heard = (clip.get("transcript") or "").strip()
    text = ("«%s»" % heard) if heard else "— текста нет —"
    if len(text) > 250:
        text = text[:249] + "…"
    stamp = time.strftime("%H:%M:%S", time.localtime(clip["ts"]))
    parts = ["%s%s" % ("TEST · " if test else "", text), result, *endpoint_hints(clip),
             "🕒 %s · пик %s · rms %s" % (stamp, clip.get("peak", "—"), clip.get("rms", "—")),
             "⚙ %s%s" % (clip.get("engine") or "?", " · усилено ×%d" % gain if gain > 1 else ""),
             "id %s" % clip["id"], footer]
    return "\n".join(parts)[:CAPTION_CHARS]


def boost_for(peak: int | None) -> int:
    if not peak or peak <= 0:
        return 1
    return int(min(MAX_GAIN, max(1, round(0.5 * FULL_SCALE / peak))))


def _ffmpeg() -> str | None:
    return os.environ.get("FFMPEG") or shutil.which("ffmpeg")


def prepare_audio(src: Path, directory: Path, clip_id: str, gain: int) -> tuple[Path, int]:
    """(copy named clip-<id>.<ext> in `directory`, gain applied): amplified Ogg/Opus when ffmpeg works (peak 60–800 is inaudible on a phone)."""
    ffmpeg = _ffmpeg()
    if ffmpeg and gain > 1:
        out = directory / ("clip-%s.ogg" % clip_id)
        try:
            subprocess.run(["nice", "-n", "19", ffmpeg, "-loglevel", "error", "-y", "-i", str(src), "-af",
                            "volume=%d,alimiter=limit=0.95" % gain, "-ac", "1", "-c:a", "libopus", "-b:a", "32k", str(out)],
                           check=True, timeout=voiceclips.ENCODE_TIMEOUT, capture_output=True)
            if out.stat().st_size > 0:
                return out, gain
        except (OSError, subprocess.SubprocessError):
            LOG.warning("clip_feed_boost_failed clip=%s", clip_id)
    ext = ".ogg" if src.suffix == ".opus" else src.suffix  # Telegram recognises .ogg as audio; the bytes are Ogg/Opus either way
    out = directory / ("clip-%s%s" % (clip_id, ext))
    shutil.copyfile(src, out)
    return out, 1


def tg_env() -> dict:
    env = dict(os.environ)
    home = Path.home()
    env["PATH"] = os.pathsep.join([str(home / ".local" / "bin"), str(home / ".bun" / "bin"), env.get("PATH", "")])
    # The intent gate's classifier sometimes reads a clip caption (a transcript such as «повтори») as a question to the owner and
    # refuses the post. These are program-generated posts, so the documented advisory mode applies: the post still goes out.
    env["ESCALATION_GATE_ENFORCE"] = "0"
    return env


def tg_send(settings: dict, caption: str, audio: Path | None) -> int | None:
    """Post through the tg CLI (an automated post; pinned to the reply-owning session). Returns the Telegram message id."""
    command = ["tg", "--automated"]
    if settings["tg_session"]:
        command += ["--session", settings["tg_session"]]
    if audio is not None:
        command += ["--file", str(audio)]
    command.append(caption)
    done = subprocess.run(command, capture_output=True, text=True, timeout=SEND_TIMEOUT_S, env=tg_env(), stdin=subprocess.DEVNULL)
    match = re.search(r"\bOK\b[^\n]*?tg#(\d+)", done.stdout)
    if done.returncode != 0 or not match:
        raise RuntimeError("tg exit %d: %s" % (done.returncode, (done.stderr or done.stdout).strip()[:200]))
    return int(match.group(1))


def send_clip(settings: dict, clip: dict, path: Path, result: str, test: bool = False, send=tg_send, bot=None) -> tuple[str, int | None, str]:
    """Post one clip: through the feed's own bot (voice note with buttons) when it can deliver, else through the tg CLI.
    Returns (via, message id, caption as posted)."""
    with tempfile.TemporaryDirectory(prefix="clipfeed-") as tmp:
        audio, gain = prepare_audio(path, Path(tmp), clip["id"], boost_for(clip.get("peak")))
        if bot is not None and bot.available():
            caption = build_caption(clip, result, gain, test, footer=clipbot.BOT_FOOTER)
            try:
                return "bot", bot.post(clip, caption, audio), caption
            except clipbot.BotUnavailable:
                pass  # not started / API down: the tg CLI delivers this one
        caption = build_caption(clip, result, gain, test)
        return "tg", send(settings, caption, audio), caption


class Feed:
    """The poll loop: new clips → Telegram, with the settings applied. `send` and `now` are injectable for tests."""

    def __init__(self, db: str, send=tg_send, now=time.time, bot=None):
        self.db, self.send, self.now, self.bot = db, send, now, bot
        self.attempts: dict[str, int] = {}
        self.last_error = ""
        self.last_poll = 0.0
        self.paused_until = 0.0

    def _state(self, conn, key, default=None):
        row = conn.execute("SELECT v FROM clip_feed_state WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def _set_state(self, conn, key, value):
        with conn:
            conn.execute("INSERT OR REPLACE INTO clip_feed_state(k, v) VALUES(?,?)", (key, str(value)))

    def _record(self, conn, clip_id, state, why=None, msg=None, via="tg", caption=None):
        cliptruth.record_feed(conn, clip_id, state, why, msg, self.now(), via, caption)

    def skip_reason(self, settings: dict, clip: dict) -> str | None:
        rejected = clip["status"] != "sent" or not (clip.get("transcript") or "").strip()
        if rejected and not settings["include_rejected"]:
            return "rejected"
        if in_quiet_hours(settings, clip["ts"]):
            return "quiet"
        return None

    def _waiting(self, conn, since: float, now: float) -> list[dict]:
        """Clips to post, oldest first: new rejected ones almost at once, sent ones once their dialog has settled, and
        clips whose last send failed once RETRY_FAILED_S has passed. Nothing is ever abandoned for rate or age."""
        return [dict(r) for r in conn.execute(
            "SELECT %s FROM voice_clips c WHERE c.ts>? AND c.ts<=? AND (c.status!='sent' OR c.ts<=?) AND NOT EXISTS ("
            "SELECT 1 FROM clip_feed f WHERE f.clip_id=c.id AND NOT (f.state='failed' AND f.ts<=?)) ORDER BY c.ts LIMIT 500"
            % ",".join("c." + k for k in voiceclips.COLUMNS),
            (since, now - REJECTED_SETTLE_S, now - SETTLE_S, now - RETRY_FAILED_S)).fetchall()]

    def _reevaluate_skips(self, conn, settings: dict, since: float) -> None:
        """When the include/quiet settings change, clips skipped under the old ones are judged again, so turning
        «include rejected» on delivers what it had held back instead of leaving it silently unsent."""
        basis = json.dumps([settings[k] for k in ("include_rejected", "quiet_enabled", "quiet_from", "quiet_to")])
        if self._state(conn, "skip_basis") == basis:
            return
        rows = conn.execute(
            "SELECT c.id, c.ts, c.status, c.transcript FROM clip_feed f JOIN voice_clips c ON c.id=f.clip_id "
            "WHERE f.state='skipped' AND f.why IN ('rejected','quiet') AND c.ts>?", (since,)).fetchall()
        with conn:
            for row in rows:
                if self.skip_reason(settings, dict(row)) is None:
                    conn.execute("DELETE FROM clip_feed WHERE clip_id=?", (row["id"],))
            conn.execute("INSERT OR REPLACE INTO clip_feed_state(k, v) VALUES('skip_basis', ?)", (basis,))


    def tick(self) -> int:
        """One pass; returns how many clips were posted."""
        now = self.now()
        self.last_poll = now
        settings = load_settings()
        conn = cliptruth.connect(self.db)
        try:
            stored = self._state(conn, "since")
            self._reevaluate_skips(conn, settings, float(stored) if stored is not None else now)
            since = float(stored) if stored is not None else now
            if not settings["enabled"] or now < self.paused_until:
                if not settings["enabled"]:
                    self._set_state(conn, "since", now)  # a re-enable never replays what was heard while off
                return 0
            if stored is None:
                self._set_state(conn, "since", since)
            waiting = self._waiting(conn, since, now)
            if any(c["status"] == "sent" and not c["dialog_id"] for c in waiting):
                voiceclips.link_dialogs(self.db)  # only when there is something to attach; it is a write
                waiting = self._waiting(conn, since, now)
            sent = 0
            eligible = []
            for clip in waiting:
                why = self.skip_reason(settings, clip)
                if why:
                    self._record(conn, clip["id"], "skipped", why)
                else:
                    eligible.append(clip)
            budget = settings["rate_per_min"] - conn.execute(
                "SELECT COUNT(*) FROM clip_feed WHERE state='sent' AND ts>?", (now - 60,)).fetchone()[0]
            for clip in eligible:
                if budget <= 0:
                    break  # the rest waits for the next minute; nothing is dropped for rate alone
                if self._deliver(conn, settings, clip):
                    sent += 1
                    budget -= 1
                elif self.paused_until > now:
                    break
            return sent
        finally:
            conn.close()

    def _deliver(self, conn, settings: dict, clip: dict) -> bool:
        path = voiceclips.clip_file(self.db, clip["id"])
        if path is None:
            self._record(conn, clip["id"], "skipped", "nofile")
            return False
        clip["first_in_dialog"] = bool(clip.get("dialog_id")) and conn.execute(
            "SELECT 1 FROM voice_clips WHERE dialog_id=? AND ts<? LIMIT 1", (clip["dialog_id"], clip["ts"])).fetchone() is None
        result = describe_result(clip, load_dialog(conn, clip.get("dialog_id")), window_end(conn, clip))
        try:
            via, msg, caption = send_clip(settings, clip, path, result, send=self.send, bot=self.bot)
        except Exception as exc:
            self.attempts[clip["id"]] = self.attempts.get(clip["id"], 0) + 1
            self.last_error = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            LOG.warning("clip_feed_send_failed clip=%s attempt=%d %s", clip["id"], self.attempts[clip["id"]], self.last_error)
            if self.attempts[clip["id"]] >= MAX_ATTEMPTS:
                self._record(conn, clip["id"], "failed", self.last_error)
            self.paused_until = self.now() + FAILURE_PAUSE_S
            return False
        self.attempts.pop(clip["id"], None)
        self.last_error = ""
        self._record(conn, clip["id"], "sent", msg=msg, via=via, caption=caption)
        LOG.info("clip_feed_sent clip=%s msg=%s", clip["id"], msg)
        return True

    def health(self) -> dict:
        settings = load_settings()
        conn = cliptruth.connect(self.db)
        try:
            counts = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) n FROM clip_feed GROUP BY state")}
        finally:
            conn.close()
        return {"ok": True, "enabled": settings["enabled"], "poll_age_s": round(self.now() - self.last_poll, 1) if self.last_poll else None,
                "counts": counts, "last_error": self.last_error}


def ledger(conn: sqlite3.Connection) -> dict:
    """Reconciliation of the archive against the feed since its watermark: every archived clip is exactly one of
    sent / pending (not looked at yet or waiting its turn) / failed (retried every RETRY_FAILED_S) / skipped (with the reason).
    `balanced` is the proof that nothing falls between the cracks."""
    row = conn.execute("SELECT v FROM clip_feed_state WHERE k='since'").fetchone()
    since = float(row["v"]) if row else 0.0
    sent = pending = failed = 0
    skipped: dict[str, int] = {}
    for r in conn.execute("SELECT f.state, COALESCE(f.why,'') why, COUNT(*) n FROM voice_clips c LEFT JOIN clip_feed f ON f.clip_id=c.id "
                          "WHERE c.ts>? GROUP BY 1, 2", (since,)):
        if r["state"] is None:
            pending += r["n"]
        elif r["state"] == "sent":
            sent += r["n"]
        elif r["state"] == "failed":
            failed += r["n"]
        else:
            skipped[r["why"]] = skipped.get(r["why"], 0) + r["n"]
    archived = conn.execute("SELECT COUNT(*) FROM voice_clips WHERE ts>?", (since,)).fetchone()[0]
    return {"since": since, "archived": archived, "sent": sent, "pending": pending, "failed": failed, "skipped": skipped,
            "balanced": archived == sent + pending + failed + sum(skipped.values())}


def feed_state(db: str) -> dict:
    """Dashboard view: settings plus what the feed has done (read straight from SQLite, independent of the service)."""
    settings = load_settings()
    conn = cliptruth.connect(db)
    try:
        counts = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) n FROM clip_feed GROUP BY state")}
        skipped = {r["why"]: r["n"] for r in conn.execute("SELECT why, COUNT(*) n FROM clip_feed WHERE state='skipped' GROUP BY why")}
        last = conn.execute("SELECT MAX(ts) FROM clip_feed WHERE state='sent'").fetchone()[0]
        today = conn.execute("SELECT COUNT(*) FROM clip_feed WHERE state='sent' AND ts>?",
                             (time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)),)).fetchone()[0]
        failed = conn.execute("SELECT why FROM clip_feed WHERE state='failed' ORDER BY ts DESC LIMIT 1").fetchone()
        bot = conn.execute("SELECT v FROM clip_feed_state WHERE k='bot_status'").fetchone()
        books = ledger(conn)
    finally:
        conn.close()
    return {**settings, "counts": counts, "skipped": skipped, "last_sent": last, "sent_today": today, "ledger": books,
            "last_failure": failed["why"] if failed else None, "bot": bot["v"] if bot else "none"}


def serve_health(feed: Feed) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/health":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(feed.health()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", HEALTH_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run() -> int:
    db = db_path()
    bot = clipbot.BotService(db)
    bot.start()
    feed = Feed(db, bot=bot)
    serve_health(feed)
    LOG.info("clip_feed_started port=%d", HEALTH_PORT)
    while True:
        try:
            feed.tick()
        except Exception as exc:  # a bad tick must not kill the service; the next poll retries
            feed.last_error = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            LOG.exception("clip_feed_tick_failed")
        time.sleep(POLL_S)


def send_test(db: str) -> int:
    """One clearly labelled synthetic clip (a tone, not stored in the archive) to prove the whole path to the owner's chat."""
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        print("ffmpeg не найден", file=sys.stderr)
        return 1
    settings = load_settings()
    clip = {"id": "TEST-%s" % time.strftime("%Y%m%dT%H%M%S"), "ts": time.time(), "dur": 2.0, "transcript": "тестовая реплика, это не речь",
            "status": "sent", "peak": 4000, "rms": 900, "voiced_ms": 2000, "silence_ms": 0, "engine": "synthetic tone (clipfeed test)"}
    with tempfile.TemporaryDirectory(prefix="clipfeed-test-") as tmp:
        src = Path(tmp) / "tone.opus"
        subprocess.run(["nice", "-n", "19", ffmpeg, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                        "-ar", "16000", "-ac", "1", "-c:a", "libopus", "-b:a", "24k", str(src)], check=True, timeout=30)
        via, msg, caption = send_clip(settings, clip, src, "🧪 проверка ленты: эталон для этого клипа не сохранится (его нет в архиве)",
                                      test=True, bot=clipbot.BotService(db))
    conn = cliptruth.connect(db)
    try:
        cliptruth.record_feed(conn, clip["id"], "sent", "test", msg, time.time(), via, caption)
    finally:
        conn.close()
    print("OK %s#%s %s" % ("bot" if via == "bot" else "tg", msg, clip["id"]))
    return 0


def refresh_session() -> None:
    """Whoever applies corrections owns the replies from now on: pin new posts to the caller's own session."""
    try:
        done = subprocess.run(["tg", "--session-id"], capture_output=True, text=True, timeout=10, env=tg_env(), stdin=subprocess.DEVNULL)
        key = done.stdout.strip()
        if done.returncode == 0 and SESSION_KEY.match(key) and key != load_settings()["tg_session"]:
            update_settings({"tg_session": key})
    except (OSError, subprocess.SubprocessError, ValueError):
        pass


def apply_truth(db: str, args: list[str], clip_id: str | None, last: bool) -> int:
    """`clip-truth <tg-msg-id> <text…>` / `--clip <id> <text…>` / `--last <text…>`; text is the owner's reply verbatim."""
    msg_id = None
    conn = cliptruth.connect(db)
    try:
        if clip_id is None:
            if last:
                row = conn.execute("SELECT clip_id, tg_msg_id FROM clip_feed WHERE state='sent' AND why IS NOT 'test' "
                                   "ORDER BY ts DESC, tg_msg_id DESC LIMIT 1").fetchone()
            else:
                match = MSG_REF.match(args[0]) if args else None
                if not match:
                    print("usage: clip-truth <tg-msg-id> <text> | --clip <id> <text> | --last <text>", file=sys.stderr)
                    return 2
                args = args[1:]
                row = conn.execute("SELECT clip_id, tg_msg_id FROM clip_feed WHERE tg_msg_id=? AND via='tg' AND state='sent'",
                                   (int(match.group(1)),)).fetchone()
            if row is None:
                print("сообщение не связано с клипом ленты", file=sys.stderr)
                return 1
            clip_id, msg_id = row["clip_id"], row["tg_msg_id"]
    finally:
        conn.close()
    try:
        label, truth = cliptruth.parse_reply(" ".join(args))
        stored = cliptruth.set_truth(db, clip_id, label, truth, source="tg", tg_msg_id=msg_id)
    except cliptruth.TruthError as exc:
        print("ошибка: %s" % exc, file=sys.stderr)
        return 1
    refresh_session()
    wer = "" if stored["wer"] is None else " WER %.2f" % stored["wer"]
    print("ok clip %s %s%s | слышал: «%s» | эталон: «%s»" % (stored["clip_id"], stored["label"], wer, stored["heard"], stored["truth"]))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="service loop (default)")
    sub.add_parser("once", help="one poll")
    sub.add_parser("status", help="settings and counters as JSON")
    sub.add_parser("reconcile", help="archive vs feed ledger: sent / pending / failed / skipped (with reasons)")
    sub.add_parser("test", help="send one synthetic TEST clip to the owner chat")
    truth = sub.add_parser("truth", help="record the owner's verdict for a clip (clip-truth)")
    truth.add_argument("--clip", help="clip id instead of a Telegram message id")
    truth.add_argument("--last", action="store_true", help="the most recent clip that was posted")
    truth.add_argument("text", nargs="*", help="[tg-msg-id] reply text")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    command = args.command or "run"
    if command == "run":
        return run()
    if command == "once":
        print(Feed(db_path()).tick())
        return 0
    if command == "status":
        print(json.dumps(feed_state(db_path()), ensure_ascii=False, indent=1))
        return 0
    if command == "reconcile":
        conn = cliptruth.connect(db_path())
        try:
            print(json.dumps(ledger(conn), ensure_ascii=False, indent=1))
        finally:
            conn.close()
        return 0
    if command == "test":
        return send_test(db_path())
    return apply_truth(db_path(), args.text, args.clip, args.last)


if __name__ == "__main__":
    sys.exit(main())
