"""Owner-editable global prompt and tone preset shared by the adapter and the dashboard.

The owner's choice lives in ~/.config/aisis/user-prompt.json: `preset` (tone),
`profanity` (bool) and `text` (free text, up to MAX_CHARS). The file is read at turn
time, so a dashboard change reaches the next brain turn without restarting anything.
The tone and profanity fragments and the default text are plain Russian files in
prompts/ beside this module: edit them to change what a preset means.

The system only passes the owner's words through: assemble() puts them after the
base persona and before memory, with control characters and square brackets removed
so the text cannot close or fake the surrounding `[...]` blocks.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from pathlib import Path

MAX_CHARS = 4000
PRESETS = {"official": "Официально", "neutral": "Нейтрально", "direct": "Прямо", "harsh": "Резко"}
DEFAULT_PRESET = "harsh"
DEFAULT_PROFANITY = True

OWNER_HEADER = ("Инструкции владельца (приоритетнее стиля по умолчанию; формат голосового ответа из установки выше "
                "остаётся в силе):")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u2028\u2029]")


def config_dir() -> Path:
    return Path(os.environ.get("AISIS_CONFIG_DIR", str(Path.home() / ".config" / "aisis")))


def settings_path() -> Path:
    return Path(os.environ.get("AISIS_USER_PROMPT", str(config_dir() / "user-prompt.json")))


def prompts_dir() -> Path:
    return Path(os.environ.get("AISIS_PROMPTS_DIR", str(Path(__file__).resolve().parent / "prompts")))


def _fragment(name: str) -> str:
    try:
        return (prompts_dir() / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def preset_fragment(preset: str) -> str:
    return _fragment(f"tone-{preset}.txt")


def profanity_fragment(allowed: bool) -> str:
    return _fragment("profanity-on.txt" if allowed else "profanity-off.txt")


def default_text() -> str:
    return _fragment("user-prompt-default.txt")


def sanitize(text: str) -> str:
    """Plain text safe to embed in a `[...]` block."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text).replace("[", "(").replace("]", ")")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def validate(settings: dict) -> dict:
    """Normalised copy of a complete settings dict; ValueError (Russian text) on a bad field."""
    preset, profanity, text = settings.get("preset"), settings.get("profanity"), settings.get("text")
    if preset not in PRESETS:
        raise ValueError("неизвестный пресет тона: " + str(preset)[:40])
    if not isinstance(profanity, bool):
        raise ValueError("profanity должно быть true/false")
    if not isinstance(text, str):
        raise ValueError("text должно быть строкой")
    if len(text) > MAX_CHARS:
        raise ValueError(f"текст длиннее {MAX_CHARS} символов ({len(text)})")
    return {"preset": preset, "profanity": profanity, "text": text}


def defaults() -> dict:
    return {"preset": DEFAULT_PRESET, "profanity": DEFAULT_PROFANITY, "text": default_text()}


def load() -> dict:
    """Saved settings over the defaults; a missing/corrupt file or one bad field falls back per field."""
    settings = defaults()
    try:
        raw = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if not isinstance(raw, dict):
        return settings
    for name in settings:
        if name in raw:
            try:
                validate({**settings, name: raw[name]})
            except ValueError:
                continue
            settings[name] = raw[name]
    return settings


def save(changes: dict) -> dict:
    """Merge validated `changes` into the saved settings; nothing is written when any field is bad."""
    unknown = set(changes) - {"preset", "profanity", "text"}
    if unknown:
        raise ValueError("неизвестные поля: " + ", ".join(sorted(unknown)))
    merged = validate({**load(), **changes})
    path = settings_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".user-prompt.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return merged


def reset() -> dict:
    """Back to the shipped defaults (removes the saved file)."""
    settings_path().unlink(missing_ok=True)
    return load()


def assemble(base_persona: str, settings: dict | None = None) -> str:
    """The persona block: base persona, the fixed not-heard rule, tone preset, profanity switch, then the owner's text.

    Empty only when there is nothing to say (no base persona and everything blank).
    """
    settings = load() if settings is None else settings
    parts = [base_persona.strip(), _fragment("rule-not-heard.txt"), preset_fragment(settings["preset"]),
             profanity_fragment(settings["profanity"])]
    owner = sanitize(settings["text"])[:MAX_CHARS]
    if owner:
        parts.append(OWNER_HEADER + "\n" + owner)
    return "\n\n".join(part for part in parts if part)


def base_persona_from_env_file(path: Path | None = None) -> str:
    """AISIS_PERSONA as the adapter's runtime.env sets it (the dashboard has no adapter environment)."""
    path = path or Path(os.environ.get("AISIS_RUNTIME_ENV", str(config_dir() / "runtime.env")))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        if line.startswith("AISIS_PERSONA="):
            try:
                parts = shlex.split(line.split("=", 1)[1])
            except ValueError:
                return ""
            return parts[0].strip() if parts else ""
    return ""
