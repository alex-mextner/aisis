"""Speaker settings: whitelist, desired->sync overlay mapping, device readback, mute state.

Why this exists: the speaker fetches its config (get_sync_info) from the kit
(~/lite-kit/scripts/equasar_server.py), which serves a static template, NOT the
quasar-server DB. A PATCH to quasar-server only pings the speaker to re-sync, so
the values would never change. The dashboard therefore (1) writes a sparse
sync-format overlay file that the kit merges into its answer at request time,
(2) PATCHes quasar-server (DB + re-sync ping), and (3) reads the config the
device actually persisted (/data/quasar/data/user_config.dat over adb) to report
whether each key really landed.

Everything here is pure or takes its I/O as parameters so it is unit-testable;
the thin real-I/O helpers (adb, files) are at the bottom.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import tts_settings

DEVICE_CONFIG_FILE = "/data/quasar/data/user_config.dat"
DAT_MARKER = '{"account_config"'
OVERLAY_NAME = "speaker-sync-overlay.json"
UI_STATE_NAME = "speaker-ui.json"
DEFAULT_RESTORE_LEVEL = 3
FLOAT_TOL = 1e-6
SETTINGS_WAIT_S = 25.0


@dataclass(frozen=True)
class Setting:
    key: str
    type: str                      # bool | enum | range | int
    sync: tuple[str, ...]          # path under the sync payload's `config`: (account_config|device_config, ...)
    quasar: tuple[str, ...]        # path inside quasarConfig (camelCase)
    options: tuple[str, ...] = ()
    min: float | None = None
    max: float | None = None
    step: float | None = None


def _acc(key, typ, sync_tail, quasar_tail, **kw):
    return Setting(key, typ, ("account_config",) + sync_tail, ("accountConfig",) + quasar_tail, **kw)


def _dev(key, typ, sync_tail, quasar_tail, **kw):
    return Setting(key, typ, ("device_config",) + sync_tail, ("deviceConfig",) + quasar_tail, **kw)


SETTINGS: tuple[Setting, ...] = (
    _acc("account.spotter", "enum", ("spotter",), ("spotter",), options=("alisa", "yandex", "yasmina")),
    _acc("account.smartActivation", "bool", ("smartActivation",), ("smartActivation",)),
    _acc("account.alwaysOnMicForShortcuts", "bool", ("alwaysOnMicForShortcuts",), ("alwaysOnMicForShortcuts",)),
    _acc("account.aliceProactivity", "bool", ("aliceProactivity",), ("aliceProactivity",)),
    _acc("account.jingle", "bool", ("jingle",), ("jingle",)),
    _acc("account.aliceAdaptiveVolume", "bool", ("aliceAdaptiveVolume", "enabled"), ("aliceAdaptiveVolume", "enabled")),
    _acc("account.crossfade", "bool", ("audio_player", "crossfadeEnabled"), ("audioPlayer", "crossfadeEnabled")),
    _acc("account.contentAccess", "enum", ("contentAccess",), ("contentAccess",),
         options=("without", "children", "medium", "safe")),
    _acc("account.childContentAccess", "enum", ("childContentAccess",), ("childContentAccess",),
         options=("without", "children", "safe")),
    _acc("account.doNotUseUserLogs", "bool", ("doNotUseUserLogs",), ("doNotUseUserLogs",)),
    _acc("account.saveHistoryUsage", "bool", ("saveHistoryUsage",), ("saveHistoryUsage",)),
    _dev("device.dnd", "bool", ("dndMode", "enabled"), ("dndMode", "enabled")),
    _dev("device.led.brightnessAuto", "bool", ("led", "brightness", "auto"), ("led", "brightness", "auto")),
    _dev("device.led.brightness", "range", ("led", "brightness", "value"), ("led", "brightness", "value"),
         min=0.0, max=1.0, step=0.05),
    _dev("device.led.idleAnimation", "bool", ("led", "idle_animation"), ("led", "idleAnimation")),
    _dev("device.led.equalizerAuto", "bool", ("led", "music_equalizer_visualization", "auto"),
         ("led", "musicEqualizerVisualization", "auto")),
    _dev("device.led.equalizerStyle", "enum", ("led", "music_equalizer_visualization", "style"),
         ("led", "musicEqualizerVisualization", "style"),
         options=("lava_beat", "blink", "polar_shining", "none")),
    _dev("device.led.timeFormat", "enum", ("led", "time_visualization", "format"),
         ("led", "timeVisualization", "format"), options=("24h", "12h")),
    _dev("device.standby.enabled", "bool", ("standby", "deepStandbyEnabled"), ("standby", "deepStandbyEnabled")),
    _dev("device.standby.timeoutMinutes", "int", ("standby", "deepStandbyTimeoutMinutes"),
         ("standby", "deepStandbyTimeoutMinutes"), min=10, max=1440),
)
BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}


def schema() -> list[dict]:
    """Frontend schema. `verifiable`: the key lives in the sync config the device persists to
    user_config.dat, so it can be read back (a given firmware may still never write it: the
    per-request result then says "unverifiable")."""
    out = []
    for s in SETTINGS:
        item: dict = {"key": s.key, "type": s.type, "verifiable": True}
        if s.options:
            item["options"] = list(s.options)
        if s.min is not None:
            item["min"] = s.min
        if s.max is not None:
            item["max"] = s.max
        if s.step is not None:
            item["step"] = s.step
        out.append(item)
    return out


# ---------- validation ----------
def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_value(setting: Setting, value):
    """Strictly typed value; raises ValueError with a Russian message."""
    k = setting.key
    if setting.type == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{k}: нужно true/false")
        return value
    if setting.type == "enum":
        if not isinstance(value, str) or value not in setting.options:
            raise ValueError(f"{k}: допустимо {', '.join(setting.options)}")
        return value
    if setting.type == "int":
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{k}: нужно целое число")
        if not (setting.min <= value <= setting.max):
            raise ValueError(f"{k}: диапазон {int(setting.min)}..{int(setting.max)}")
        return value
    if setting.type == "range":
        if not _is_number(value):
            raise ValueError(f"{k}: нужно число")
        if not (setting.min - FLOAT_TOL <= value <= setting.max + FLOAT_TOL):
            raise ValueError(f"{k}: диапазон {setting.min}..{setting.max}")
        return round(round(value / setting.step) * setting.step, 2)
    raise ValueError(f"{k}: неизвестный тип")  # pragma: no cover


def validate_changes(changes) -> dict:
    """Whitelist + strict typing of a {dotted key: value} dict. Unknown key -> ValueError."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("changes: нужен непустой объект {ключ: значение}")
    out = {}
    for key, value in changes.items():
        setting = BY_KEY.get(key) if isinstance(key, str) else None
        if setting is None:
            raise ValueError(f"неизвестный ключ: {key}")
        out[key] = validate_value(setting, value)
    return out


# ---------- nested path helpers ----------
def _set_path(tree: dict, path: tuple[str, ...], value) -> None:
    node = tree
    for part in path[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = node[part] = {}
        node = nxt
    node[path[-1]] = value


def _get_path(tree, path: tuple[str, ...]):
    node = tree
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def deep_merge(base: dict, patch: dict) -> dict:
    """Return a new dict: `patch` merged recursively over `base` (patch wins; dicts merge)."""
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = json.loads(json.dumps(v)) if isinstance(v, (dict, list)) else v
    return out


def sync_overlay(changes: dict) -> dict:
    """Sparse sync-format overlay: {account_config:{...}, device_config:{...}} (only touched keys)."""
    tree: dict = {}
    for key, value in changes.items():
        _set_path(tree, BY_KEY[key].sync, value)
    return tree


def quasar_patch(changes: dict) -> dict:
    """Sparse quasarConfig patch (camelCase) for PATCH /devices/:duid."""
    tree: dict = {}
    for key, value in changes.items():
        _set_path(tree, BY_KEY[key].quasar, value)
    return {"quasarConfig": tree}


def flatten_sync(account_config, device_config) -> dict:
    """{dotted key: value|None} from sync-format sections (device user_config.dat)."""
    sections = {"account_config": account_config, "device_config": device_config}
    return {s.key: _get_path(sections.get(s.sync[0]), s.sync[1:]) for s in SETTINGS}


def flatten_quasar(quasar_config) -> dict:
    """{dotted key: value|None} from a quasarConfig (quasar-server DB)."""
    return {s.key: _get_path(quasar_config, s.quasar) for s in SETTINGS}


def values_equal(a, b) -> bool:
    if a is None or b is None:
        return False
    if _is_number(a) and _is_number(b):
        return abs(float(a) - float(b)) <= FLOAT_TOL
    return type(a) is type(b) and a == b


# ---------- device file ----------
def parse_user_config(raw: bytes):
    """Embedded JSON object of user_config.dat (starts at '{"account_config"') or None."""
    text = raw.decode("utf-8", "replace")
    start = text.find(DAT_MARKER)
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def device_values(config) -> dict:
    """Flat dotted values the device persisted ({key: None} for every key when unavailable)."""
    if not isinstance(config, dict):
        return {s.key: None for s in SETTINGS}
    return flatten_sync(config.get("account_config"), config.get("device_config"))


def classify(desired, device, adb_online: bool) -> str:
    """"yes" matches; "unverifiable" = adb offline, key absent from the device file or nothing desired;
    else "pending" (the device holds a different value)."""
    if values_equal(desired, device):
        return "yes"
    if not adb_online or device is None or desired is None:
        return "unverifiable"
    return "pending"


def resync_desired(server_config, overlay: dict) -> dict:
    """What a re-sync should deliver per key: the overlay value if the owner set one, else the
    quasar-server DB value; keys with neither are left out."""
    server = flatten_quasar(server_config)
    overlaid = flatten_sync(overlay.get("account_config"), overlay.get("device_config"))
    merged = {k: overlaid[k] if overlaid[k] is not None else server[k] for k in server}
    return {k: v for k, v in merged.items() if v is not None}


def parse_volume(value, max_level: int) -> int:
    """Integer volume 0..max_level (bool/float-with-fraction/str rejected); raises ValueError."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("volume: нужно целое число")
    if not 0 <= value <= max_level:
        raise ValueError(f"volume: диапазон 0..{max_level}")
    return value


# ---------- overlay file ----------
def config_dir() -> Path:
    return tts_settings.config_dir()


def overlay_path() -> Path:
    return Path(os.environ.get("AISIS_SYNC_OVERLAY", str(config_dir() / OVERLAY_NAME)))


def ui_state_path() -> Path:
    return config_dir() / UI_STATE_NAME


def read_json_file(path: Path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_overlay(path: Path | None = None) -> dict:
    data = read_json_file(path or overlay_path())
    return data if data else {}


def overlay_active(path: Path | None = None) -> bool:
    ov = load_overlay(path)
    return any(isinstance(ov.get(sec), dict) and ov[sec] for sec in ("account_config", "device_config"))


def save_overlay_changes(changes: dict, path: Path | None = None) -> dict:
    """Merge new changes into the persisted overlay (sticky: the desired state survives restarts)."""
    merged = deep_merge(load_overlay(path), sync_overlay(changes))
    tts_settings._write_private(path or overlay_path(), json.dumps(merged, ensure_ascii=False, indent=1) + "\n")
    return merged


# ---------- mute (volume 0 + remembered restore level) ----------
def load_ui_state(path: Path | None = None) -> dict:
    data = read_json_file(path or ui_state_path()) or {}
    lvl = data.get("restoreLevel")
    return {"restoreLevel": lvl if isinstance(lvl, int) and not isinstance(lvl, bool) and lvl > 0 else None}


def save_ui_state(state: dict, path: Path | None = None) -> None:
    tts_settings._write_private(path or ui_state_path(), json.dumps(state, indent=1) + "\n")


def mute_plan(muted: bool, current_level: int | None, restore_level: int | None, max_level: int):
    """(target_level, new_restore_level). Mute remembers the audible level; unmute restores it
    (>=1, default 3, capped at the speaker maximum). Muting while already at 0 keeps the memory;
    unmuting while already audible is a no-op that keeps the current level."""
    if muted:
        if current_level is not None and current_level > 0:
            return 0, current_level
        return 0, restore_level
    if current_level is not None and current_level > 0:
        return current_level, restore_level
    target = restore_level if restore_level and restore_level > 0 else DEFAULT_RESTORE_LEVEL
    return max(1, min(target, max_level)), restore_level


def ui_mute(current_level: int | None, restore_level: int | None) -> dict:
    return {"muted": current_level == 0, "restoreLevel": restore_level}


# ---------- apply + readback orchestration ----------
ReadDevice = Callable[[], "DeviceRead"]


@dataclass
class DeviceRead:
    online: bool
    values: dict            # flat dotted -> value|None
    age_s: float | None = None


def build_results(desired: dict, server_flat: dict, dev: DeviceRead) -> list[dict]:
    return [{"key": k, "desired": v, "server": server_flat.get(k), "device": dev.values.get(k),
             "applied": classify(v, dev.values.get(k), dev.online)} for k, v in desired.items()]


def wait_for_device(desired: dict, read_device: ReadDevice, *, max_wait_s: float = 25.0,
                    interval_s: float = 2.0, sleep=time.sleep, clock=time.monotonic):
    """Poll the device file until every desired key matches (or adb is offline, or timeout).
    Returns (last DeviceRead, waited seconds)."""
    t0 = clock()
    dev = read_device()
    while dev.online and not all(values_equal(v, dev.values.get(k)) for k, v in desired.items()):
        if clock() - t0 + interval_s > max_wait_s:
            break
        sleep(interval_s)
        dev = read_device()
    return dev, round(clock() - t0, 1)


# ---------- real I/O ----------
def adb_online(adb: str, timeout: float = 6) -> bool:
    try:
        p = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.startswith(adb) and line.split()[-1] == "device" for line in p.stdout.splitlines())


def adb_read_device_config(adb: str, timeout: float = 10) -> DeviceRead:
    """user_config.dat as the speaker persisted it (read-only: cat + date)."""
    if not adb_online(adb):
        return DeviceRead(False, device_values(None))
    try:
        cat = subprocess.run(["adb", "-s", adb, "shell", "cat", DEVICE_CONFIG_FILE],
                             capture_output=True, timeout=timeout)
        stamp = subprocess.run(["adb", "-s", adb, "shell", f"date -r {DEVICE_CONFIG_FILE} +%s; date +%s"],
                               capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return DeviceRead(False, device_values(None))
    if cat.returncode != 0:
        return DeviceRead(False, device_values(None))
    age = None
    try:
        file_ts, now_ts = (int(x) for x in stamp.stdout.split()[:2])
        age = max(0.0, float(now_ts - file_ts))
    except ValueError:
        pass
    return DeviceRead(True, device_values(parse_user_config(cat.stdout)), age)
