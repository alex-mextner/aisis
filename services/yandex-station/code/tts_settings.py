"""Persisted TTS voice choice shared by the adapter, the dashboard and gen_fillers.

`tts_voice`: "bracho" (default, Fish) or "alice" (Yandex cloud: the reply text
leaves the host; opt-in, owner-approved tg#27883). The Alice voice has two engines
(`alice_engine`): "keyless" (default; the public Alice uniproxy websocket the
Yandex apps use, no key and no subscription; voice `alice_voice`, `shitova.gpu`
is Alice herself) and "speechkit" (Yandex Cloud SpeechKit v1, optional upgrade that
needs an API key; without one the keyless engine speaks instead).
Non-secret settings live in ~/.config/aisis/tts.json; the SpeechKit API
key lives alone in ~/.config/aisis/yandex-speechkit.key (0600) and is
never returned by status(). Both files are read at request time, so a change in
the dashboard applies to the next utterance without restarting anything.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

VOICES = ("bracho", "alice")
ALICE_ENGINES = ("keyless", "speechkit")
# Voices the keyless uniproxy accepts (probed: unknown names such as "alice" or
# "alena" are silently ignored by the server, so only verified names are listed).
ALICE_VOICES = ("shitova.gpu", "shitova", "oksana", "jane", "zahar", "ermil", "omazh")
# SpeechKit v1 voice names usable with `emotion` (ru-RU). "alice" is not an API
# voice; alena is the closest Alice-like timbre.
YANDEX_VOICES = ("alena", "jane", "oksana", "omazh", "zahar", "ermil", "filipp", "marina")
YANDEX_EMOTIONS = ("neutral", "good", "evil")
DEFAULTS = {"tts_voice": "bracho", "alice_engine": "keyless", "alice_voice": "shitova.gpu",
    "yandex_voice": "alena", "yandex_emotion": "good", "yandex_folder_id": ""}


def config_dir() -> Path:
    return Path(os.environ.get("AISIS_CONFIG_DIR", str(Path.home() / ".config" / "aisis")))


def settings_path() -> Path:
    return Path(os.environ.get("AISIS_TTS_SETTINGS", str(config_dir() / "tts.json")))


def key_path() -> Path:
    return Path(os.environ.get("AISIS_YANDEX_TTS_KEY_FILE", str(config_dir() / "yandex-speechkit.key")))


def load() -> dict[str, str]:
    """Settings with defaults; a missing/corrupt file or an invalid value falls back to the default
    (per value: one bad field must not reset the others)."""
    settings = dict(DEFAULTS)
    try:
        raw = json.loads(settings_path().read_text())
    except (OSError, ValueError):
        return settings
    if not isinstance(raw, dict):
        return settings
    for name in DEFAULTS:
        if isinstance(raw.get(name), str):
            try:
                validate({**DEFAULTS, name: raw[name]})
            except ValueError:
                continue
            settings[name] = raw[name]
    return settings


def validate(settings: dict[str, str]) -> None:
    if settings["tts_voice"] not in VOICES:
        raise ValueError("tts_voice must be one of " + ", ".join(VOICES))
    if settings["alice_engine"] not in ALICE_ENGINES:
        raise ValueError("alice_engine must be one of " + ", ".join(ALICE_ENGINES))
    if settings["alice_voice"] not in ALICE_VOICES:
        raise ValueError("alice_voice must be one of " + ", ".join(ALICE_VOICES))
    if settings["yandex_voice"] not in YANDEX_VOICES:
        raise ValueError("yandex_voice must be one of " + ", ".join(YANDEX_VOICES))
    if settings["yandex_emotion"] not in YANDEX_EMOTIONS:
        raise ValueError("yandex_emotion must be one of " + ", ".join(YANDEX_EMOTIONS))
    folder = settings["yandex_folder_id"]
    if len(folder) > 64 or not all(ch.isascii() and ch.isalnum() for ch in folder):
        raise ValueError("yandex_folder_id must be alphanumeric")


def _write_private(path: Path, data: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update(changes: dict) -> dict[str, str]:
    """Merge known keys into the stored settings (validated, atomic, 0600)."""
    settings = load()
    for name, value in changes.items():
        if name not in DEFAULTS:
            raise ValueError("unknown setting " + str(name))
        if not isinstance(value, str):
            raise ValueError(str(name) + " must be a string")
        settings[name] = value.strip()
    validate(settings)
    _write_private(settings_path(), json.dumps(settings, ensure_ascii=False, indent=1) + "\n")
    return settings


def read_key() -> str:
    try:
        return key_path().read_text().strip()
    except OSError:
        return ""


def normalize_key(key: str) -> str:
    """The key as it will be stored; ValueError (never echoing the key) when unusable."""
    if not isinstance(key, str):
        raise ValueError("invalid API key")
    key = key.strip()
    if not key or len(key) > 512 or any(ch.isspace() or ord(ch) < 33 or ord(ch) > 126 for ch in key):
        raise ValueError("invalid API key")
    return key


def write_key(key: str) -> None:
    _write_private(key_path(), normalize_key(key) + "\n")


def clear_key() -> None:
    key_path().unlink(missing_ok=True)


def effective_voice(settings: dict[str, str] | None = None) -> str:
    """The voice that will actually speak: the keyless engine needs no key, so selecting alice is enough."""
    settings = settings or load()
    return settings["tts_voice"]


def alice_engine(settings: dict[str, str] | None = None) -> str:
    """Engine that speaks the Alice voice: SpeechKit only when chosen and a key exists, else keyless."""
    settings = settings or load()
    return "speechkit" if settings["alice_engine"] == "speechkit" and read_key() else "keyless"


def effective_engine(settings: dict[str, str] | None = None) -> str | None:
    """Engine behind the selected voice; None while Bracho is selected."""
    settings = settings or load()
    return alice_engine(settings) if settings["tts_voice"] == "alice" else None


def voice_spec(settings: dict[str, str] | None = None, voice: str | None = None) -> str:
    """Identity of everything that shapes the audio of `voice` (default: the selected one);
    the cache key for fillers, so any change of engine/voice/emotion resynthesizes them."""
    settings = settings or load()
    if (voice or settings["tts_voice"]) != "alice":
        return "bracho"
    if alice_engine(settings) == "keyless":
        return "alice/keyless/" + settings["alice_voice"] + "/" + settings["yandex_emotion"]
    return "alice/speechkit/" + settings["yandex_voice"] + "/" + settings["yandex_emotion"]


def status() -> dict:
    """Public view for the UI: never contains the key itself."""
    settings = load()
    key_configured = bool(read_key())
    return {**settings, "key_configured": key_configured,
        "effective_voice": effective_voice(settings), "effective_engine": effective_engine(settings),
        "needs_key": settings["tts_voice"] == "alice" and settings["alice_engine"] == "speechkit" and not key_configured,
        "voices": list(VOICES), "alice_engines": list(ALICE_ENGINES), "alice_voices": list(ALICE_VOICES),
        "yandex_voices": list(YANDEX_VOICES), "yandex_emotions": list(YANDEX_EMOTIONS)}
