"""Owner-adjustable end-of-speech wait of the STT gateway, shared by the gateway and the dashboard.

The gateway closes an utterance after `silence_ms` of trailing silence. The default comes from
AISIS_STT_SILENCE_MS in runtime.env (800); the dashboard stores an override in
~/.config/aisis/stt_tuning.json and the gateway re-reads it at the start of every conversation,
so a change applies to the next utterance without a restart. Too short a wait cuts a command after the
wake word; too long a wait delays every answer by the same amount.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

MIN_SILENCE_MS = 400
MAX_SILENCE_MS = 3000
DEFAULT_SILENCE_MS = 800


def settings_path() -> Path:
    config = Path(os.environ.get("AISIS_CONFIG_DIR", str(Path.home() / ".config" / "aisis")))
    return Path(os.environ.get("AISIS_STT_TUNING", str(config / "stt_tuning.json")))


def in_bounds(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and MIN_SILENCE_MS <= value <= MAX_SILENCE_MS


def override() -> int | None:
    """The dashboard's silence_ms, or None when none is stored (or the stored one is invalid)."""
    try:
        value = json.loads(settings_path().read_text()).get("silence_ms")
    except (OSError, ValueError, AttributeError):
        return None
    return value if in_bounds(value) else None


def env_default() -> int:
    """AISIS_STT_SILENCE_MS from the process environment or runtime.env (only that key is read)."""
    raw = os.environ.get("AISIS_STT_SILENCE_MS")
    if raw is None:
        runtime = Path(os.environ.get("AISIS_CONFIG_DIR", str(Path.home() / ".config" / "aisis"))) / "runtime.env"
        try:
            match = re.search(r"^AISIS_STT_SILENCE_MS=(\d+)\s*$", runtime.read_text(), re.M)
        except OSError:
            match = None
        raw = match.group(1) if match else None
    try:
        return int(raw) if raw is not None else DEFAULT_SILENCE_MS
    except ValueError:
        return DEFAULT_SILENCE_MS


def state() -> dict:
    stored = override()
    return {"silence_ms": stored if stored is not None else env_default(), "default_ms": env_default(),
            "overridden": stored is not None, "min_ms": MIN_SILENCE_MS, "max_ms": MAX_SILENCE_MS}


def update(silence_ms) -> dict:
    """Store the override; ValueError when out of bounds. Setting the default value clears nothing: it stays explicit."""
    if not in_bounds(silence_ms):
        raise ValueError("silence_ms must be an integer %d..%d" % (MIN_SILENCE_MS, MAX_SILENCE_MS))
    path = settings_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".stt_tuning.")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump({"silence_ms": silence_ms}, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return state()
