"""Owner-adjustable level front-end of the STT gateway (speech_frontend.py) and its A/B counters.

Defaults come from AISIS_STT_* in runtime.env (Tuning.from_env); the dashboard stores overrides in
~/.config/aisis/stt_dsp.json and the gateway re-reads them at the start of every conversation, so a
change applies to the next utterance without a restart.

A/B: for `ab_percent` % of the utterances the gateway also asks the recognizer about the UNPROCESSED copy, in
the background (the owner's answer never waits for it), and records both transcripts here. The counters say
how often the front-end made a heard command out of nothing (`lift_only`), lost one (`raw_only`) or changed the
words (`differ`).
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

DENOISE_ENGINES = ("off",)
KEYS = ("enabled", "target_dbfs", "max_gain_db", "compressor", "highpass_hz", "denoise", "ab_percent")
BOUNDS = {"target_dbfs": (-45.0, -10.0), "max_gain_db": (0.0, 60.0), "highpass_hz": (0.0, 300.0), "ab_percent": (0.0, 100.0)}
VERDICTS = ("same", "differ", "lift_only", "raw_only", "both_empty")


def settings_path() -> Path:
    config = Path(os.environ.get("AISIS_CONFIG_DIR", str(Path.home() / ".config" / "aisis")))
    return Path(os.environ.get("AISIS_STT_DSP", str(config / "stt_dsp.json")))


def _valid(key, value) -> bool:
    if key in ("enabled", "compressor"):
        return isinstance(value, bool)
    if key == "denoise":
        return value in DENOISE_ENGINES
    low, high = BOUNDS[key]
    return isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high


def overrides() -> dict:
    """Valid stored overrides only; a missing/corrupt file or a bad value contributes nothing."""
    try:
        stored = json.loads(settings_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(stored, dict):
        return {}
    return {key: stored[key] for key in KEYS if key in stored and _valid(key, stored[key])}


def update(changes: dict) -> dict:
    """Merge validated overrides (atomic, 0600); ValueError on an unknown key or an out-of-range value."""
    merged = overrides()
    for key, value in changes.items():
        if key not in KEYS:
            raise ValueError("unknown setting %s" % key)
        if not _valid(key, value):
            raise ValueError("bad value for %s" % key)
        merged[key] = value
    path = settings_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".stt_dsp.")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(merged, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return merged


def _runtime_env() -> dict:
    """AISIS_STT_* lines of runtime.env under the process environment (the same file the gateway service loads)."""
    env = {}
    path = Path(os.environ.get("AISIS_CONFIG_DIR", str(Path.home() / ".config" / "aisis"))) / "runtime.env"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key.startswith("AISIS_STT_") and not key.startswith("#"):
            env[key.strip()] = value.strip().strip('"')
    env.update({key: value for key, value in os.environ.items() if key.startswith("AISIS_STT_")})
    return env


def defaults() -> dict:
    """Values in force without dashboard overrides (runtime.env or the built-in defaults)."""
    import speech_frontend
    env = _runtime_env()
    cfg = speech_frontend.FrontEndConfig.from_env(env)

    def number(key, fallback):
        try:
            return float(env.get(key, fallback))
        except ValueError:
            return fallback
    return {"enabled": env.get("AISIS_STT_DSP", "1") != "0", "target_dbfs": cfg.target_dbfs, "max_gain_db": cfg.max_gain_db,
            "compressor": cfg.compressor, "highpass_hz": cfg.highpass_hz, "denoise": "off", "ab_percent": number("AISIS_STT_AB_PERCENT", 10.0)}


def state(db_path) -> dict:
    """Everything the dashboard card shows: effective settings, defaults, bounds, A/B counters."""
    base = defaults()
    stored = overrides()
    return {"settings": {**base, **stored}, "defaults": base, "overridden": sorted(stored), "bounds": BOUNDS,
            "denoise_engines": list(DENOISE_ENGINES), "ab": ab_stats(db_path)}


def reset() -> None:
    settings_path().unlink(missing_ok=True)


# ---------------------------------------------------------------- A/B counters

def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS dsp_ab(ts REAL NOT NULL, gain_db REAL, in_rms REAL, verdict TEXT NOT NULL, raw_text TEXT, lifted_text TEXT)")
    return conn


def verdict(raw_text: str, lifted_text: str, usable) -> str:
    """Compare the recognizer's answers for the original and the processed copy; `usable(text)` says a text is a real answer."""
    raw_ok, lifted_ok = usable(raw_text), usable(lifted_text)
    if raw_ok and lifted_ok:
        return "same" if raw_text.strip().lower() == lifted_text.strip().lower() else "differ"
    if lifted_ok:
        return "lift_only"
    return "raw_only" if raw_ok else "both_empty"


def record_ab(db_path, gain_db, in_rms, ab_verdict, raw_text, lifted_text) -> None:
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute("INSERT INTO dsp_ab(ts, gain_db, in_rms, verdict, raw_text, lifted_text) VALUES(?,?,?,?,?,?)",
                         (time.time(), gain_db, in_rms, ab_verdict, raw_text, lifted_text))
    finally:
        conn.close()


def ab_stats(db_path, recent=8) -> dict:
    """{total, counts{verdict: n}, recent[{ts, verdict, raw, lifted, gain_db}]}; empty when nothing was sampled yet."""
    try:
        conn = _connect(db_path)
    except sqlite3.Error:
        return {"total": 0, "counts": {}, "recent": []}
    try:
        counts = dict(conn.execute("SELECT verdict, COUNT(*) FROM dsp_ab GROUP BY verdict"))
        rows = conn.execute("SELECT ts, verdict, raw_text, lifted_text, gain_db FROM dsp_ab ORDER BY ts DESC LIMIT ?", (recent,)).fetchall()
    finally:
        conn.close()
    return {"total": sum(counts.values()), "counts": {name: counts.get(name, 0) for name in VERDICTS},
            "recent": [{"ts": ts, "verdict": v, "raw": raw, "lifted": lifted, "gain_db": gain} for ts, v, raw, lifted, gain in rows]}
