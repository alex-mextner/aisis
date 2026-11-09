"""~1 s animation on the Station Mini 2 clock display when the speaker starts playing.

The Mini 2 clock is 32 LEDs: four 7-segment digits (A..G each) + PLUS1, PLUS2, MINUS, DEGREE.
The stock firmware draws it from the `draw_scled_animations` directive (what stock Alice and
AlexxIT/YandexStation `draw_animation` send). Verified on the speaker (firmware 0.369.1.46):

  payload = {animation_stop_policy: "PlayOnce",
             animations: [{base64_encoded_value: b64(frames), compression_type: "None"}]}
  frames  = frame*, frame = 0x00 + 32 brightness bytes + 2-byte big-endian delay in ms

Without `compression_type: "None"` the firmware logs "Unexpected compression type: Unknown" and
draws nothing. Brightness bytes are relative to the clock's own (ambient-dependent) level.
The ring (6 RGB zones) has no directive; it is only written from the firmware's `.led` player.

Trigger: the AirPlay receiver on the speaker logs `alice_airplay_state ... playing=true` when sound
actually starts (voice «включи X», dashboard, MA, a phone alike; the voice reply comes ~3-4 s
earlier). `ReceiverWatcher` tails that log over adb and fires the animation on the false→true edge,
fire-and-forget: nothing waits on the push and a failed push is only logged.
Settings (`~/.config/aisis/led-animation.json`) are read per start, so the dashboard
applies at once.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import os
import random
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Iterator

LOG = logging.getLogger("aisis.led")

QUASAR_API = os.environ.get("AISIS_QUASAR_API", "http://127.0.0.1:31116")
DUID = os.environ.get("AISIS_DUID", "M00ZRW3008RAJG")
PUSH_TIMEOUT_S = 3.0
MIN_GAP_S = 3.0          # AirPlay state flaps (pause/seek) must not retrigger the animation
FRAME_MS = 100           # target frame period (the display is read at ~10 fps)
MIN_DURATION_MS = 300
MAX_DURATION_MS = 3000
DEFAULTS = {"enabled": True, "animation": "bars", "duration_ms": 1000}

# 32-LED layout of the Mini 2 clock (verified by lighting each index in turn and reading sysfs).
_SEGMENTS = "ABCDEFG"
PLUS1, PLUS2, MINUS, DEGREE = 28, 29, 30, 31
LED_COUNT = 32
FULL = 255

Frame = tuple[list[int], int]  # (32 brightness bytes, delay ms)


def segment(digit: int, name: str) -> int:
    return digit * 7 + _SEGMENTS.index(name)


# A bar of height 0..5 on one digit fills from the bottom: D, E+C, G, F+B, A.
_BAR_STEPS = (("D",), ("E", "C"), ("G",), ("F", "B"), ("A",))
# Mirrored bar of height 0..3 grows from the middle bar G outwards: G, E+C+F+B, A+D.
_MIRROR_STEPS = (("G",), ("E", "C", "F", "B"), ("A", "D"))
BARS = 4  # one bar per digit


def _light(levels: list[int], digit: int, names: Iterable[str]) -> None:
    for name in names:
        levels[segment(digit, name)] = FULL


def _frame_count(duration_ms: int) -> int:
    return max(4, min(40, round(duration_ms / FRAME_MS)))


def _spectrum(count: int, rng: random.Random, top: int) -> list[list[float]]:
    """Winamp-style bar heights per frame: a bar jumps to a random new height on a beat and falls
    steadily otherwise. The first frame always hits and the last two only fall, so the show starts
    with sound and dies away before the clock comes back."""
    heights = [0.0] * BARS
    out = []
    for index in range(count):
        calm = index >= count - 2
        for bar in range(BARS):
            hit = rng.uniform(top * 0.4, top) if not calm and (index == 0 or rng.random() < 0.6) else 0.0
            heights[bar] = max(heights[bar] - top * 0.22, hit)
        out.append(list(heights))
    return out


def _bars(count: int, rng: random.Random) -> list[list[int]]:
    """Classic bars; PLUS1/PLUS2 flash on a loud beat."""
    out = []
    for heights in _spectrum(count, rng, 5):
        levels = [0] * LED_COUNT
        for digit, height in enumerate(heights):
            for step in _BAR_STEPS[:round(height)]:
                _light(levels, digit, step)
        if sum(heights) / BARS >= 3:
            levels[PLUS1] = levels[PLUS2] = FULL
        out.append(levels)
    return out


def _peaks(count: int, rng: random.Random) -> list[list[int]]:
    """Bars with a peak-hold marker: the top of each bar hangs and falls slower than the bar."""
    out = []
    hold = [0.0] * BARS
    for heights in _spectrum(count, rng, 5):
        levels = [0] * LED_COUNT
        for digit, height in enumerate(heights):
            hold[digit] = max(hold[digit] - 0.3, height)
            for step in _BAR_STEPS[:round(height)]:
                _light(levels, digit, step)
            marker = round(hold[digit])
            if marker > round(height) + 1:  # a gap under the marker, otherwise it is just a taller bar
                _light(levels, digit, _BAR_STEPS[marker - 1])
        out.append(levels)
    return out


def _mirror(count: int, rng: random.Random) -> list[list[int]]:
    """Bars grow from the middle and the display is symmetric: outer digits equal, inner digits equal."""
    out = []
    for heights in _spectrum(count, rng, 3):
        levels = [0] * LED_COUNT
        for digit, height in ((0, heights[0]), (1, heights[1]), (2, heights[1]), (3, heights[0])):
            for step in _MIRROR_STEPS[:round(height)]:
                _light(levels, digit, step)
        out.append(levels)
    return out


def _pulse(count: int, _rng: random.Random) -> list[list[int]]:
    """The middle bars and the dots breathe once: the «listening» look."""
    out = []
    for index in range(count):
        glow = int(FULL * (1 - math.cos(2 * math.pi * index / count)) / 2)
        levels = [0] * LED_COUNT
        for digit in range(BARS):
            levels[segment(digit, "G")] = glow
        for led_index in (PLUS1, PLUS2, MINUS):
            levels[led_index] = glow
        out.append(levels)
    return out


def _ant(count: int, _rng: random.Random) -> list[list[int]]:
    """One lit segment walking every LED of the clock in turn (the first wiring test; not offered)."""
    return [[FULL if led_index == index % LED_COUNT else 0 for led_index in range(LED_COUNT)] for index in range(count)]


Generator = Callable[[int, random.Random], list[list[int]]]
# Offered in the dashboard.
ANIMATIONS: dict[str, tuple[str, Generator]] = {
    "bars": ("Эквалайзер", _bars),
    "peaks": ("Эквалайзер с пиками", _peaks),
    "mirror": ("Зеркальный эквалайзер", _mirror),
    "pulse": ("Дыхание", _pulse),
}
# Frame sets kept for experiments; never chosen from settings.
LIBRARY: dict[str, Generator] = {"ant": _ant}


def frames(name: str, duration_ms: int, seed: int | None = None) -> list[Frame]:
    """Frames of an animation, delays adding up to ~`duration_ms`. Random shows are reproducible per `seed`."""
    generator = ANIMATIONS[name][1] if name in ANIMATIONS else LIBRARY.get(name)
    if generator is None:
        raise ValueError(f"unknown animation {name!r}")
    count = _frame_count(duration_ms)
    delay = max(20, duration_ms // count)
    return [(levels, delay) for levels in generator(count, random.Random(seed))]


def encode(frame_list: Iterable[Frame]) -> bytes:
    out = bytearray()
    for levels, delay_ms in frame_list:
        if len(levels) != LED_COUNT or not all(0 <= v <= 255 for v in levels) or not 0 <= delay_ms <= 0xFFFF:
            raise ValueError("frame must be 32 bytes 0..255 and a delay of 0..65535 ms")
        out += bytes([0, *levels]) + delay_ms.to_bytes(2, "big")
    return bytes(out)


def _struct_value(value: object) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, (int, float)):
        return {"numberValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, list):
        return {"listValue": {"values": [_struct_value(v) for v in value]}}
    if isinstance(value, dict):
        return {"structValue": {"fields": {k: _struct_value(v) for k, v in value.items()}}}
    raise TypeError(type(value).__name__)


def directive(name: str, duration_ms: int, seed: int | None = None) -> dict:
    """Raw quasar-server directive (`POST /devices/:duid/push/raw` body) drawing the animation once."""
    blob = base64.b64encode(encode(frames(name, duration_ms, seed))).decode()
    payload = {"animation_stop_policy": "PlayOnce",
               "animations": [{"base64_encoded_value": blob, "compression_type": "None"}]}
    return {"Name": "draw_scled_animations", "Type": "client_action", "IsLedSilent": True,
            "Payload": {"fields": {k: _struct_value(v) for k, v in payload.items()}}}


# ── settings ─────────────────────────────────────────────────────────────────

def settings_path() -> Path:
    default = Path.home() / ".config" / "aisis" / "led-animation.json"
    return Path(os.environ.get("AISIS_LED_ANIMATION_SETTINGS", str(default)))


def _validated(changes: dict) -> dict:
    out = {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            continue
        if key == "enabled":
            if not isinstance(value, bool):
                raise ValueError("enabled must be true/false")
        elif key == "animation":
            if value not in ANIMATIONS:
                raise ValueError("animation must be one of " + ", ".join(ANIMATIONS))
        elif isinstance(value, bool) or not isinstance(value, int) or not MIN_DURATION_MS <= value <= MAX_DURATION_MS:
            raise ValueError(f"duration_ms must be an integer {MIN_DURATION_MS}..{MAX_DURATION_MS}")
        out[key] = value
    return out


def load_settings() -> dict:
    settings = dict(DEFAULTS)
    try:
        stored = json.loads(settings_path().read_text())
    except (OSError, ValueError):
        return settings
    if isinstance(stored, dict):
        for key in DEFAULTS:  # per value: one bad field must not reset the others
            if key in stored:
                try:
                    settings.update(_validated({key: stored[key]}))
                except ValueError:
                    pass
    return settings


def update_settings(changes: dict) -> dict:
    settings = {**load_settings(), **_validated(changes)}
    path = settings_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as fh:
        json.dump(settings, fh, ensure_ascii=False)
    os.replace(fh.name, path)
    return settings


def options() -> list[dict]:
    return [{"id": key, "title": title} for key, (title, _) in ANIMATIONS.items()]


# ── playing ──────────────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_last = {"ts": 0.0, "ok": None, "err": None, "animation": None, "source": None}


def last_play() -> dict:
    with _state_lock:
        return dict(_last)


def _post(body: dict) -> None:
    request = urllib.request.Request(
        f"{QUASAR_API}/devices/{DUID}/push/raw", json.dumps(body).encode(),
        {"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=PUSH_TIMEOUT_S) as response:
        response.read()


def play_now(name: str, duration_ms: int, source: str) -> dict:
    """Push the animation and wait for the server's answer (dashboard test button)."""
    result = {"ok": True, "err": None}
    try:
        _post(directive(name, duration_ms))
    except (OSError, ValueError) as err:
        result = {"ok": False, "err": str(err)[:200]}
        LOG.warning("led_animation_push_failed source=%s err=%s", source, result["err"])
    with _state_lock:
        _last.update(ts=time.time(), animation=name, source=source, **result)
    return result


def trigger(source: str) -> bool:
    """Fire-and-forget start animation per the saved settings; False when off or inside the gap."""
    settings = load_settings()
    if not settings["enabled"]:
        return False
    with _state_lock:
        if time.time() - _last["ts"] < MIN_GAP_S:
            return False
        _last["ts"] = time.time()
    threading.Thread(target=play_now, args=(settings["animation"], settings["duration_ms"], source),
                     name="led-animation", daemon=True).start()
    return True


# ── receiver watcher ─────────────────────────────────────────────────────────

def parse_playing(line: str) -> bool | None:
    """`playing` of a receiver `alice_airplay_state` log line; None for any other line."""
    if "alice_airplay_state" not in line:
        return None
    if "playing=true" in line:
        return True
    if "playing=false" in line:
        return False
    return None


def start_edges(lines: Iterable[str]) -> Iterator[str]:
    """Yield each line on which the receiver went from not playing to playing."""
    playing = False
    for line in lines:
        state = parse_playing(line)
        if state is None:
            continue
        if state and not playing:
            yield line
        playing = state


class ReceiverWatcher:
    """Tails the receiver log on the speaker over adb; calls `on_start` when sound starts."""

    RETRY_MIN_S, RETRY_MAX_S = 5.0, 60.0

    def __init__(self, on_start: Callable[[str], object] = trigger):
        self.on_start = on_start
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="led-receiver-watch", daemon=True)
            self._thread.start()

    def _tail(self) -> Iterator[str]:
        import music_airplay
        proc = subprocess.Popen(
            ["adb", "-s", music_airplay.ADB, "shell", f"tail -n 0 -f {music_airplay.RECEIVER_LOG}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
        try:
            assert proc.stdout is not None
            yield from proc.stdout
        finally:
            proc.kill()
            proc.wait()

    def _run(self) -> None:
        delay = self.RETRY_MIN_S
        while True:
            began = time.monotonic()
            try:
                for _ in start_edges(self._tail()):
                    delay = self.RETRY_MIN_S
                    self.on_start("receiver")
            except Exception as err:  # keep the watcher alive whatever adb does
                LOG.warning("led_watch_error err=%s", str(err)[:200])
            if time.monotonic() - began > 60:
                delay = self.RETRY_MIN_S
            time.sleep(delay)
            delay = min(delay * 2, self.RETRY_MAX_S)


_watcher = ReceiverWatcher()


def start_watcher() -> None:
    """Idempotent; the adapter calls it when AISIS_LED_ANIMATION=on."""
    _watcher.start()
