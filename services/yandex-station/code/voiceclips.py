"""Always-on local archive of every utterance the STT gateway cut from the mic stream.

Each clip is a 16 kHz mono Opus file under <root>/YYYY-MM-DD/<id>.opus (WAV when no ffmpeg
is available) plus one `voice_clips` row in the dialogs DB with the transcript, the level
diagnostics and what became of it (sent to the recognizer, or discarded before it). The
audio never leaves this machine; files are 0600, pruned after the retention window.

Dialog linkage is by time: the gateway does not know the dialog id, so `link_dialogs`
back-fills `dialog_id` from the `dialogs` time window.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
import wave
from pathlib import Path

LOG = logging.getLogger("aisis.voiceclips")

CLIP_ID = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{6}$")
DEFAULT_RETENTION_DAYS = 180
ENCODE_TIMEOUT = 20

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS voice_clips("
    "id TEXT PRIMARY KEY, ts REAL NOT NULL, dialog_id TEXT, path TEXT NOT NULL, dur REAL, rate INTEGER, bytes INTEGER, "
    "rms INTEGER, peak INTEGER, voiced_ms INTEGER, floor_rms INTEGER, contrast REAL, "
    "engine TEXT, trigger TEXT, status TEXT NOT NULL, reason TEXT, transcript TEXT, asr_ms INTEGER, "
    "silence_ms INTEGER, final_ms INTEGER)",
    "CREATE INDEX IF NOT EXISTS voice_clips_ts ON voice_clips(ts)",
    "CREATE INDEX IF NOT EXISTS voice_clips_dialog ON voice_clips(dialog_id)",
    # Processed copies of an archived clip (e.g. the enhanced "studio" voice). A sibling table, not rows of voice_clips:
    # the clip feed, truth set and ASR bench enumerate voice_clips and must keep seeing one row per utterance.
    "CREATE TABLE IF NOT EXISTS voice_clip_variants(clip_id TEXT NOT NULL, variant TEXT NOT NULL, path TEXT NOT NULL, "
    "bytes INTEGER, ts REAL NOT NULL, engine TEXT, ms INTEGER, PRIMARY KEY(clip_id, variant))",
)
COLUMNS = ("id", "ts", "dialog_id", "path", "dur", "rate", "bytes", "rms", "peak", "voiced_ms", "floor_rms", "contrast",
           "engine", "trigger", "status", "reason", "transcript", "asr_ms", "silence_ms", "final_ms")


def root() -> Path:
    return Path(os.environ.get("AISIS_VOICE_ARCHIVE") or os.path.expanduser("~/.local/share/aisis/voice-archive"))


def retention_days() -> int:
    try:
        return max(1, int(os.environ.get("AISIS_VOICE_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)))
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    for statement in SCHEMA:
        conn.execute(statement)
    return conn


def new_clip_id(ts: float) -> str:
    return "%s-%s" % (time.strftime("%Y%m%dT%H%M%S", time.localtime(ts)), secrets.token_hex(3))


def _ffmpeg() -> str | None:
    configured = os.environ.get("FFMPEG")
    if configured and os.access(configured, os.X_OK):
        return configured
    return shutil.which("ffmpeg")


def encode(pcm: bytes, rate: int) -> tuple[bytes, str]:
    """(bytes, extension): 24 kbit/s Opus at 16 kHz; a 16 kHz WAV when ffmpeg is missing or fails."""
    binary = _ffmpeg()
    if binary:
        try:
            done = subprocess.run([binary, "-v", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
                                   "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", "-application", "voip", "-f", "ogg", "pipe:1"],
                                  input=pcm, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=ENCODE_TIMEOUT, check=True)
            if done.stdout:
                return done.stdout, "opus"
        except (OSError, subprocess.SubprocessError):
            LOG.warning("voice_clip_opus_failed falling back to wav")
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buf.getvalue(), "wav"


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def save(db_path: str, pcm: bytes, rate: int, meta: dict) -> str | None:
    """Archive one utterance and its row. Never raises; returns the clip id (None on failure).

    meta keys (all optional): ts, rms, peak, voiced_ms, floor_rms, contrast, engine, trigger, status, reason,
    transcript, asr_ms, silence_ms, final_ms."""
    try:
        ts = float(meta.get("ts") or time.time())
        clip_id = new_clip_id(ts)
        data, ext = encode(pcm, rate)
        directory = root() / time.strftime("%Y-%m-%d", time.localtime(ts))
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / (clip_id + "." + ext)
        _write_private(path, data)
        row = {"id": clip_id, "ts": ts, "dialog_id": None, "path": str(path.relative_to(root())), "dur": len(pcm) / 2 / rate,
               "rate": rate, "bytes": len(data), "status": meta.get("status") or "sent"}
        for key in COLUMNS:
            if key not in row:
                row[key] = meta.get(key)
        conn = connect(db_path)
        try:
            conn.execute("INSERT INTO voice_clips(%s) VALUES(%s)" % (",".join(COLUMNS), ",".join("?" * len(COLUMNS))),
                         [row[key] for key in COLUMNS])
            conn.commit()
        finally:
            conn.close()
        return clip_id
    except Exception as exc:  # the archive must never break a voice turn
        LOG.warning("voice_clip_save_failed class=%s", type(exc).__name__)
        return None


def clip_file(db_path: str, clip_id: str) -> Path | None:
    """Absolute path of an archived clip, or None. The id is validated; the path never leaves the archive root."""
    if not CLIP_ID.match(clip_id or ""):
        return None
    conn = connect(db_path)
    try:
        found = conn.execute("SELECT path FROM voice_clips WHERE id=?", (clip_id,)).fetchone()
    finally:
        conn.close()
    if not found:
        return None
    base = root().resolve()
    full = (base / found[0]).resolve()
    if base not in full.parents or not full.is_file():
        return None
    return full


def _rows(conn: sqlite3.Connection, where: str, args: tuple) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute("SELECT %s FROM voice_clips WHERE %s ORDER BY ts" % (",".join(COLUMNS), where), args)]


def link_dialogs(db_path: str) -> int:
    """Attach unlinked clips (older than 30 s, so the dialog row has settled) to the dialog whose window holds them."""
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE voice_clips SET dialog_id=(SELECT d.id FROM dialogs d WHERE d.kind='voice' AND d.ts_start-5<=voice_clips.ts "
            "AND voice_clips.ts<=COALESCE(d.ts_end, d.ts_start+3600)+15 ORDER BY d.ts_start DESC LIMIT 1) "
            "WHERE dialog_id IS NULL AND ts<?", (time.time() - 30,))
        conn.commit()
        return cur.rowcount
    except sqlite3.OperationalError:  # dialogs table not created yet
        return 0
    finally:
        conn.close()


def clips_for_dialog(db_path: str, dialog_id: str) -> list[dict]:
    """Clips of one dialog, oldest first (back-fills the link first)."""
    link_dialogs(db_path)
    conn = connect(db_path)
    try:
        return _rows(conn, "dialog_id=?", (dialog_id,))
    finally:
        conn.close()


def usage(db_path: str) -> dict:
    """Totals for the Services tab: clip count, bytes on disk, oldest clip, retention window."""
    conn = connect(db_path)
    try:
        count, size, oldest = conn.execute("SELECT COUNT(*), COALESCE(SUM(bytes),0), MIN(ts) FROM voice_clips").fetchone()
    finally:
        conn.close()
    return {"clips": count, "bytes": size, "oldest": oldest, "retention_days": retention_days(), "path": str(root())}


def prune(db_path: str, now: float | None = None) -> int:
    """Delete clips past the retention window (file, then row); returns how many were removed."""
    cutoff = (now or time.time()) - retention_days() * 86400
    conn = connect(db_path)
    removed = 0
    try:
        for clip_id, rel in conn.execute("SELECT id, path FROM voice_clips WHERE ts<?", (cutoff,)).fetchall():
            try:
                (root() / rel).unlink(missing_ok=True)
            except OSError:
                continue
            for (variant_rel,) in conn.execute("SELECT path FROM voice_clip_variants WHERE clip_id=?", (clip_id,)).fetchall():
                try:
                    (root() / variant_rel).unlink(missing_ok=True)
                except OSError:
                    pass
            conn.execute("DELETE FROM voice_clip_variants WHERE clip_id=?", (clip_id,))
            conn.execute("DELETE FROM voice_clips WHERE id=?", (clip_id,))
            removed += 1
        conn.commit()
    finally:
        conn.close()
    for directory in sorted(root().glob("*-*-*")) if root().is_dir() else []:
        try:
            directory.rmdir()  # only succeeds when empty
        except OSError:
            pass
    return removed


def save_variant(db_path: str, clip_id: str, variant: str, pcm: bytes, rate: int, engine: str | None = None, ms: int | None = None) -> bool:
    """Store a processed copy next to the original clip (<id>.<variant>.<ext>). Never raises; True when stored."""
    try:
        if not CLIP_ID.match(clip_id or "") or not re.fullmatch(r"[a-z0-9_]{1,24}", variant or ""):
            return False
        original = clip_file(db_path, clip_id)
        if original is None:
            return False
        data, ext = encode(pcm, rate)
        path = original.with_name("%s.%s.%s" % (clip_id, variant, ext))
        path.unlink(missing_ok=True)
        _write_private(path, data)
        conn = connect(db_path)
        try:
            conn.execute("INSERT OR REPLACE INTO voice_clip_variants(clip_id, variant, path, bytes, ts, engine, ms) VALUES(?,?,?,?,?,?,?)",
                         (clip_id, variant, str(path.relative_to(root())), len(data), time.time(), engine, ms))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:
        LOG.warning("voice_clip_variant_failed class=%s", type(exc).__name__)
        return False


def variants_for(db_path: str, clip_ids: list[str]) -> dict[str, list[str]]:
    """{clip_id: [variant, ...]} for the given clips (empty lists omitted)."""
    if not clip_ids:
        return {}
    conn = connect(db_path)
    try:
        marks = ",".join("?" * len(clip_ids))
        found: dict[str, list[str]] = {}
        for clip_id, variant in conn.execute("SELECT clip_id, variant FROM voice_clip_variants WHERE clip_id IN (%s) ORDER BY variant" % marks, clip_ids):
            found.setdefault(clip_id, []).append(variant)
        return found
    finally:
        conn.close()


def variant_file(db_path: str, clip_id: str, variant: str) -> Path | None:
    """Absolute path of a processed copy, or None (same containment rules as clip_file)."""
    if not CLIP_ID.match(clip_id or ""):
        return None
    conn = connect(db_path)
    try:
        found = conn.execute("SELECT path FROM voice_clip_variants WHERE clip_id=? AND variant=?", (clip_id, variant)).fetchone()
    finally:
        conn.close()
    if not found:
        return None
    base = root().resolve()
    full = (base / found[0]).resolve()
    return full if base in full.parents and full.is_file() else None
