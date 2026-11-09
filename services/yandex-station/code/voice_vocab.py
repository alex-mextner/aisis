"""Domain vocabulary of the speaker: one source for the Whisper bias prompt and the ASR corrector.

The owner talks to the speaker from across the room about the same few things: music, timers and
alarms, the smart home, now and then a fact. The words that matter (commands, the Home Assistant
friendly names, the artists in the music library) are small and known, so the recognizer is
biased towards them instead of being left to guess a Russian homophone («ключи» for «включи»).

`Catalog` is the live part (HA entity names, areas, library artists), refreshed by whoever needs it;
everything here is pure except `fetch_catalog`, whose I/O is injected so it can be unit-tested.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

# Command vocabulary, grouped as the owner uses it. Order is the priority when the Whisper prompt
# has to be cut to its token budget.
VERBS = ("включи", "выключи", "поставь", "запусти", "останови", "пауза", "продолжи", "стоп", "дальше", "назад",
         "громче", "тише", "сделай", "добавь", "убавь", "отмени", "сколько осталось")
TIMER_WORDS = ("таймер", "будильник", "разбуди", "напомни", "через", "минут", "секунд", "час", "полчаса")
MUSIC_WORDS = ("музыку", "песню", "трек", "альбом", "плейлист", "радио", "рок", "джаз", "классику")
HOME_WORDS = ("свет", "люстру", "ночник", "кондиционер", "чайник", "телевизор", "розетку", "яркость", "температуру")
NUMBER_WORDS = ("пять", "десять", "пятнадцать", "двадцать", "тридцать", "сорок пять", "двадцать три")
# Generic rooms: the live area list from HA replaces them when it is reachable.
DEFAULT_AREAS = ("кухня", "гостиная", "спальня", "студия", "прихожая", "второй этаж")

# Entity domains a person speaks to. Scripts/automations/sensors are plumbing, not names.
SPOKEN_DOMAINS = frozenset({"light", "switch", "climate", "media_player", "fan", "cover", "lock", "vacuum",
                            "humidifier", "scene"})
_CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")
# Technical names (brackets, dashes, any Latin word) are never what a person says out loud in Russian.
_PLUMBING = re.compile(r"[()\[\]_:/→↔—–]|[A-Za-z]{3,}")
# Library artists such as «#2Маши» (a leading symbol) are tags, not something one says.
_ARTIST = re.compile(r"^\w[\w .&'’-]*$")
MAX_NAME_WORDS = 4
MAX_NAME_CHARS = 32


@dataclass(frozen=True)
class Catalog:
    entities: tuple[str, ...] = ()
    areas: tuple[str, ...] = ()
    artists: tuple[str, ...] = ()
    fetched_at: float = field(default=0.0, compare=False)

    def empty(self) -> bool:
        return not (self.entities or self.areas or self.artists)


def _unique(items: list[str], limit: int) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        item = " ".join(item.split())
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            out.append(item)
        if len(out) >= limit:
            break
    return tuple(out)


def spoken_entity_names(states: list[dict]) -> tuple[str, ...]:
    """Friendly names a person could say out loud, taken from the HA /api/states list."""
    names: list[str] = []
    for state in states:
        entity_id = state.get("entity_id", "")
        if entity_id.partition(".")[0] not in SPOKEN_DOMAINS:
            continue
        name = (state.get("attributes") or {}).get("friendly_name")
        if not isinstance(name, str):
            continue
        name = name.strip()
        if (not _CYRILLIC.search(name) or len(name) > MAX_NAME_CHARS or len(name.split()) > MAX_NAME_WORDS
                or _PLUMBING.search(name)):
            continue
        names.append(name)
    return _unique(names, 60)


def catalog_from(states: list[dict], areas: list[str], artists: list[str], now: float | None = None) -> Catalog:
    return Catalog(spoken_entity_names(states), _unique([a for a in areas if isinstance(a, str)], 20),
                   _unique([a for a in artists if isinstance(a, str) and len(a) <= 40 and _ARTIST.match(a)], 40),
                   time.time() if now is None else now)


def fetch_json(url: str, body: dict | None = None, timeout: float = 4.0):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode()


def library_artists(limit: int = 40) -> list[str]:
    """Most played artists of the Music Assistant library (read-only, local)."""
    import music_airplay
    items = music_airplay.ma_call("music/artists/library_items", {"limit": limit, "order_by": "play_count_desc"})
    return [i["name"] for i in (items if isinstance(items, list) else items.get("items", [])) if isinstance(i.get("name"), str)]


def fetch_catalog(hacli_url: str = "http://127.0.0.1:8795", artists: Callable[[], list[str]] | None = None,
                  get: Callable[..., object] = fetch_json) -> Catalog:
    """Live catalog; every source is optional (a failed one just leaves its part empty)."""
    states: list[dict] = []
    areas: list[str] = []
    names: list[str] = []
    try:
        loaded = get(hacli_url + "/api/states")
        states = loaded if isinstance(loaded, list) else []
    except Exception:
        pass
    try:
        rendered = get(hacli_url + "/api/template", {"template": "{{ areas() | map('area_name') | list | tojson }}"})
        parsed = json.loads(rendered) if isinstance(rendered, str) else rendered
        areas = parsed if isinstance(parsed, list) else []
    except Exception:
        pass
    if artists is not None:
        try:
            names = artists()
        except Exception:
            pass
    return catalog_from(states, areas, names)


def _sentence(words: list[str]) -> str:
    return ", ".join(words)


def estimate_tokens(text: str) -> int:
    """Whisper tokens of Russian text without loading a tokenizer: measured 2.36 characters per token (large-v3-turbo)."""
    return math.ceil(len(text) / 2.3)


def whisper_prompt(catalog: Catalog, max_tokens: int = 150, count_tokens: Callable[[str], int] | None = None) -> str:
    """Prompt text for faster-whisper's `initial_prompt`.

    Whisper treats the prompt as the text that came just before the audio, so it is written the way
    a transcript of this speaker would read (capitalised imperatives, punctuation, spoken numbers)
    rather than as a bare word list. It is also what keeps the recognizer on «включи» when the
    first syllable was cut off. Only the first decoding window sees it, which is enough for the
    short utterances of a speaker. Parts are added by priority until `max_tokens` is spent
    (Whisper's hard limit is 224).
    """
    count = count_tokens or estimate_tokens
    areas = [a.lower() for a in (catalog.areas or DEFAULT_AREAS)]
    prompt = ("Включи свет на кухне. Выключи люстру. Громче. Тише. Громкость пять. Пауза. Дальше. Стоп. "
              "Поставь таймер на десять минут. Разбуди меня в семь тридцать.")
    # Lists in priority order, each filled with as many items as still fit: a long device list must not push the rooms out.
    for label, items, cap in (("Устройства: ", catalog.entities, 7), ("Комнаты: ", areas, 4), ("Включи музыку: ", catalog.artists, 8)):
        taken: list[str] = []
        for item in list(items)[:cap]:
            if count(f"{prompt} {label}{_sentence(taken + [item])}.") > max_tokens:
                break
            taken.append(item)
        if taken:
            prompt += f" {label}{_sentence(taken)}."
    climate = " Кондиционер, температура двадцать три градуса."
    if count(prompt + climate) <= max_tokens:
        prompt += climate
    return prompt


def hotwords(catalog: Catalog, limit: int = 40) -> list[str]:
    """Whole terms (device names, rooms, artists) for recognizers that take a hotword list."""
    return list(_unique(list(catalog.entities) + list(catalog.areas) + list(catalog.artists), limit))


def domain_context(catalog: Catalog, max_tokens: int = 150, count_tokens: Callable[[str], int] | None = None,
                   with_hotwords: bool = False) -> dict:
    """The `context` of a Wyoming transcribe request (stt-cli `stt serve` merges it with the profile's own dictionaries;
    a stock Wyoming server ignores it). Hotwords are opt-in: on far-field/cut-off speech faster-whisper's `hotwords`
    measured no better than no bias at all and worse on cut-off phrases (docs/voice-assistant-practices.md, section 7)."""
    context = {"prompt": whisper_prompt(catalog, max_tokens, count_tokens)}
    if with_hotwords:
        context["hotwords"] = hotwords(catalog)
    return context


class CatalogCache:
    """Catalog that never blocks the caller: `get()` returns what is cached and refreshes it in a thread when stale.

    The first call returns an empty catalog (the static part of the prompt is still useful); a failed refresh keeps
    the previous catalog."""

    def __init__(self, fetch: Callable[[], Catalog], ttl: float = 300.0, now: Callable[[], float] = time.monotonic):
        self._fetch, self._ttl, self._now = fetch, ttl, now
        self._catalog = Catalog()
        self._at: float | None = None
        self._busy = False
        self._lock = threading.Lock()

    def get(self) -> Catalog:
        with self._lock:
            stale = (self._at is None or self._now() - self._at > self._ttl) and not self._busy
            if stale:
                self._busy = True
        if stale:
            threading.Thread(target=self._refresh, daemon=True).start()
        return self._catalog

    def _refresh(self) -> None:
        try:
            fresh = self._fetch()
            if not fresh.empty():
                self._catalog = fresh
        except Exception:
            pass
        finally:
            with self._lock:
                self._at = self._now()
                self._busy = False


def corrector_catalog(catalog: Catalog) -> str:
    """Compact, line-based catalog block for the corrector prompt."""
    lines = []
    if catalog.entities:
        lines.append("Устройства: " + "; ".join(catalog.entities))
    areas = catalog.areas or DEFAULT_AREAS
    lines.append("Комнаты: " + "; ".join(areas))
    if catalog.artists:
        lines.append("Музыка в библиотеке: " + "; ".join(catalog.artists[:25]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    """`voice_vocab.py prompt|catalog [--static]`: what the recognizer and the corrector are given right now.

    `prompt` is also what to pass to a stock wyoming-faster-whisper as `--initial-prompt` when no stt-cli recognizer
    is in front of it (a static prompt: it cannot follow HA changes between restarts)."""
    import argparse
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("what", choices=("prompt", "catalog"))
    parser.add_argument("--static", action="store_true", help="verbs and generic rooms only, no live HA/Music Assistant lookup")
    args = parser.parse_args(argv)
    catalog = Catalog() if args.static else fetch_catalog(artists=library_artists)
    print(whisper_prompt(catalog) if args.what == "prompt" else corrector_catalog(catalog))


if __name__ == "__main__":
    main()
