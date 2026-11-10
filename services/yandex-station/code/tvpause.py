#!/usr/bin/env python3
"""Pause the living-room TV while the speaker listens, resume it after the dialog.

Activation signal: the STT gateway (stt_wyoming.py) opens one websocket per spoken turn, right
after the wake word/spotter fired on the speaker; it calls `TvPause.activation()`.
End signal: dialogstore's docker log follower (dashboard process) sees `Dialog <id> ended` and
calls `TvPause.dialog_ended()`. Both processes share one state file under flock, so the one
that paused is not required to be the one that resumes.

Rules (all deliberate):
  * pause only a configured media_player whose state is `playing`, read live from ha-cli; a
    stale cache read, a down Home Assistant or an unknown state does nothing (ha-cli would
    queue a service call and run it minutes later, which is worse than no pause);
  * resume only what *we* paused, only if it is still `paused` (someone pressed play: leave
    it) and only if the dialog did not itself act on that TV (`Fast intent executed: ...
    [media_player.televizor]`: the owner said pause/stop/play, never undo it);
  * a new activation inside the resume grace cancels the resume (next turn/dialog owns it);
  * if no end signal ever arrives, a timer resumes after `max_hold_s` since the last activation;
  * `AISIS_TVPAUSE_DRY_RUN=1` reads states but never calls a service (events say "dry-run").

Settings: ~/.config/aisis/tv_pause.json (atomic, 0600), edited in the dashboard.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

LOG = logging.getLogger("aisis.tvpause")

HA_CLI_URL = os.environ.get("AISIS_HACLI_URL", "http://127.0.0.1:8795")
DEFAULTS = {"enabled": True, "resume": True, "entities": ["media_player.televizor"],
            "resume_delay_s": 1.5, "max_hold_s": 300}
ENTITY_RE = re.compile(r"^media_player\.[a-z0-9_]+$")
# A dialog belongs to an activation if it started at most this long after it (clock/queue jitter
# between the STT open and the container's "Dialog started"); a later start is another dialog.
START_TOLERANCE_S = 3.0
EVENT_LIMIT = 30
DIALOG_RE = re.compile(r"Dialog ([0-9a-f-]{36}) (started|ended)")
EXECUTED_RE = re.compile(r"Fast intent executed: .*\[([^\]]*)\]")


def config_dir() -> Path:
    return Path(os.environ.get("AISIS_CONFIG_DIR", str(Path.home() / ".config" / "aisis")))


def settings_path() -> Path:
    return Path(os.environ.get("AISIS_TVPAUSE_SETTINGS", str(config_dir() / "tv_pause.json")))


def state_path() -> Path:
    default = Path.home() / ".local" / "state" / "aisis" / "tv_pause_state.json"
    return Path(os.environ.get("AISIS_TVPAUSE_STATE", str(default)))


def load_settings() -> dict:
    settings = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    try:
        stored = json.loads(settings_path().read_text())
    except (OSError, ValueError):
        return settings
    if isinstance(stored, dict):
        try:
            settings.update(_validated({k: stored[k] for k in DEFAULTS if k in stored}))
        except ValueError:
            pass
    return settings


def _validated(changes: dict) -> dict:
    out = {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            continue
        if key in ("enabled", "resume"):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true/false")
        elif key == "entities":
            if not isinstance(value, list) or not value or not all(isinstance(e, str) and ENTITY_RE.match(e) for e in value):
                raise ValueError("entities must be a non-empty list of media_player.* ids")
            value = list(dict.fromkeys(value))
        elif key == "resume_delay_s":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 30:
                raise ValueError("resume_delay_s must be 0..30")
        elif key == "max_hold_s":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 10 <= value <= 3600:
                raise ValueError("max_hold_s must be 10..3600")
        out[key] = value
    return out


def update_settings(changes: dict) -> dict:
    merged = load_settings()
    merged.update(_validated(changes))
    path = settings_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tv_pause.")
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


class HaCli:
    """Read states and call media_player services through the local ha-cli proxy (no token here)."""

    def __init__(self, base=HA_CLI_URL, timeout=5.0):
        self.base, self.timeout = base.rstrip("/"), timeout

    def _open(self, req):
        return urllib.request.urlopen(req, timeout=self.timeout)

    def ha_up(self) -> bool:
        try:
            with self._open(urllib.request.Request(self.base + "/health")) as r:
                return bool(json.loads(r.read().decode()).get("ha_up"))
        except (OSError, ValueError):
            return False

    def state(self, entity: str):
        """(state, stale). state is None if unreadable."""
        try:
            with self._open(urllib.request.Request(f"{self.base}/api/states/{entity}")) as r:
                stale = r.headers.get("X-Aisis-Cache") == "stale"
                return json.loads(r.read().decode()).get("state"), stale
        except (OSError, ValueError):
            return None, True

    def call(self, service: str, entity: str) -> bool:
        """True only for an immediately executed call; a queued (202) call is reported as False."""
        data = json.dumps({"entity_id": entity}).encode()
        req = urllib.request.Request(f"{self.base}/api/services/media_player/{service}", data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with self._open(req) as r:
                return r.status == 200
        except (OSError, ValueError):
            return False


class _Locked:
    """Process-wide + cross-process lock around the state file."""

    _local = threading.RLock()

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self._local.acquire()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        self._local.release()


class TvPause:
    def __init__(self, ha=None, settings=load_settings, state_file=None, dry_run=None, clock=time.time):
        self.ha = ha or HaCli()
        self.settings = settings
        self.state_file = Path(state_file) if state_file else state_path()
        self.dry_run = os.environ.get("AISIS_TVPAUSE_DRY_RUN") == "1" if dry_run is None else dry_run
        self.clock = clock
        self.timers: dict[str, threading.Timer] = {}
        self._started: dict[str, float] = {}      # dialog id -> start time seen in this process

    # ----- state file -----
    def _load(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            data = {}
        data.setdefault("paused", {})
        data.setdefault("last_activation", 0.0)
        data.setdefault("user_touched", False)
        data.setdefault("events", [])
        return data

    def _save(self, st: dict):
        st["events"] = st["events"][-EVENT_LIMIT:]
        fd, tmp = tempfile.mkstemp(dir=self.state_file.parent, prefix=".tv_pause_state.")
        with os.fdopen(fd, "w") as fh:
            json.dump(st, fh, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_file)

    def _event(self, st: dict, kind: str, entity: str = "", detail: str = ""):
        st["events"].append({"ts": round(self.clock(), 3), "kind": kind, "entity": entity, "detail": detail})
        LOG.info("tv_pause %s %s %s", kind, entity, detail)

    def status(self) -> dict:
        with _Locked(self.state_file):
            st = self._load()
        return {"paused": st["paused"], "last_activation": st["last_activation"], "events": st["events"][-10:]}

    # ----- activation: pause -----
    def activation(self):
        cfg = self.settings()
        if not cfg["enabled"]:
            return
        now = self.clock()
        with _Locked(self.state_file):
            st = self._load()
            st["last_activation"] = now
            if not st["paused"]:
                st["user_touched"] = False
            if not self.ha.ha_up():
                self._event(st, "skip", detail="Home Assistant down")
                self._save(st)
                # A pause held from an earlier activation must follow this newer last_activation,
                # or its hold timer (keyed to the older one) expires as a no-op and never resumes.
                holding = bool(st["paused"])
            else:
                holding = self._pause_entities(cfg, st, now)
            # Armed under the state lock: an older activation must not replace the timer of a newer one
            # after releasing the lock (its timestamp guard would then expire as a no-op).
            if holding:
                self._arm("hold", cfg["max_hold_s"], self._hold_expired, now)

    def _pause_entities(self, cfg, st, now) -> bool:
        """Pause playing entities (caller holds the state lock); returns whether any pause is held."""
        for entity in cfg["entities"]:
            state, stale = self.ha.state(entity)
            if stale or state is None:
                self._event(st, "skip", entity, "state unavailable")
            elif state == "playing":
                if self.dry_run:
                    self._event(st, "dry-run", entity, "would pause")
                elif self.ha.call("media_pause", entity):
                    st["paused"][entity] = now
                    self._event(st, "pause", entity)
                else:
                    self._event(st, "error", entity, "media_pause failed")
            elif entity in st["paused"] and state != "paused":
                del st["paused"][entity]          # someone else changed it; no longer ours
                self._event(st, "forget", entity, f"state {state}")
        self._save(st)
        return bool(st["paused"])

    def _arm(self, name, delay, fn, *args):
        old = self.timers.pop(name, None)
        if old:
            old.cancel()
        timer = threading.Timer(delay, fn, args)
        timer.daemon = True
        self.timers[name] = timer
        timer.start()

    def _hold_expired(self, activation_ts):
        # a newer activation owns the hold
        self.resume("max hold elapsed", unless=lambda st: st["last_activation"] != activation_ts)

    # ----- dialog log events -----
    def observe(self, ts: float, message: str):
        """Feed one container log message (already ANSI-free, with its epoch)."""
        m = DIALOG_RE.search(message)
        if m:
            did, what = m.groups()
            if what == "started":
                self._started[did] = ts
                if len(self._started) > 64:
                    self._started.pop(next(iter(self._started)))
            else:
                self.dialog_ended(did, ts)
            return
        m = EXECUTED_RE.search(message)
        if m:
            targets = {t.strip() for t in m.group(1).split(",")}
            if targets & set(self.settings()["entities"]):
                with _Locked(self.state_file):
                    st = self._load()
                    if st["paused"]:
                        st["user_touched"] = True
                        self._event(st, "user-action", ",".join(sorted(targets)), "dialog acted on the TV; will not resume")
                        self._save(st)

    def dialog_ended(self, did: str, ended_ts: float):
        cfg = self.settings()
        started = self._started.pop(did, None)
        with _Locked(self.state_file):
            st = self._load()
            holding = bool(st["paused"])
            last = st["last_activation"]
        if not holding or not cfg["resume"]:
            return
        if started is not None and started > last + START_TOLERANCE_S:
            return                                   # a dialog opened after the activation we hold (e.g. text command)
        self._arm("resume", cfg["resume_delay_s"], self._resume_if_idle, ended_ts)

    def _resume_if_idle(self, ended_ts):
        # the next turn/dialog started during the grace
        self.resume("dialog ended", unless=lambda st: st["last_activation"] > ended_ts)

    # ----- resume -----
    def resume(self, reason: str, unless=None):
        """Resume what we paused. `unless(state)` is evaluated under the same lock as the resume,
        so an activation cannot slip in between the idle check and the release."""
        cfg = self.settings()
        with _Locked(self.state_file):
            st = self._load()
            if unless is not None and unless(st):
                return
            ours, touched = dict(st["paused"]), st["user_touched"]
            st["paused"], st["user_touched"] = {}, False
            for entity in ours:
                if not cfg["resume"]:
                    self._event(st, "skip-resume", entity, "resume disabled")
                    continue
                if touched:
                    self._event(st, "skip-resume", entity, "dialog acted on the TV")
                    continue
                state, stale = self.ha.state(entity)
                if stale or state != "paused":
                    self._event(st, "skip-resume", entity, f"state {state}{' (stale)' if stale else ''}")
                elif self.dry_run:
                    self._event(st, "dry-run", entity, f"would resume ({reason})")
                elif self.ha.call("media_play", entity):
                    self._event(st, "resume", entity, reason)
                else:
                    self._event(st, "error", entity, "media_play failed")
            self._save(st)

    def recover(self):
        """After a service restart: a pause we still hold is released once max_hold_s has passed."""
        cfg = self.settings()
        with _Locked(self.state_file):
            st = self._load()
            holding, last = bool(st["paused"]), st["last_activation"]
        if holding:
            self._arm("hold", max(1.0, last + cfg["max_hold_s"] - self.clock()), self._hold_expired, last)


_default = None
_default_lock = threading.Lock()


def default() -> TvPause:
    global _default
    with _default_lock:
        if _default is None:
            _default = TvPause()
        return _default
