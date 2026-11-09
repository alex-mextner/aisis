"""Post-ASR corrector: what did the owner most probably say?

Whisper hears the speaker from across the room, so the text it returns is sometimes a homophone
(«ключи свет»), loses the first syllable/word (the endpointer opened late) or the tail (it closed
early). The fast-intent layer handles clear commands and is never delayed; only a phrase it could
not decide on (gray zone / no match, and short) comes here. One cheap Claude call (haiku, one
output line, a hard latency budget) returns the likeliest intended phrase and a confidence; the
caller acts on it above the act threshold, offers it back to the owner («…, верно?») in the confirm band and otherwise
behaves exactly as it did without us.

Pure logic (prompt, reply parsing, guards, settings) is separated from the one network call so
both are unit-testable; the call itself takes the credential list and an HTTP session.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import voice_vocab

LOG = logging.getLogger("aisis.asr_correct")

# confirm_threshold: from here the corrected phrase is offered back to the owner («…, верно?»);
# act_threshold: from here it is used as if the owner had said it.
DEFAULTS: dict[str, Any] = {"enabled": True, "confirm_threshold": 0.6, "act_threshold": 0.9, "model": "claude-haiku-4-5", "timeout_ms": 1200,
                            "max_words": 8}
INTENTS = {"M": "music", "T": "timer", "H": "home", "F": "fact", "S": "followup", "U": "unclear"}
MAX_TEXT_CHARS = 160
MAX_RECENT = 4

SYSTEM = """Ты — слой исправления распознавания речи умной колонки в квартире. Распознаватель (Whisper) выдал текст; верни, какую команду или вопрос хозяин вероятнее всего произнёс.

Условия записи:
- Хозяин говорит издалека, из другой части комнаты: слова размыты, звуково похожие слова путаются («ключи свет» вместо «включи свет», «пауса», «поставь таймер на десеть»).
- Начало и конец фразы могут быть обрезаны: у слова потерян первый слог или хвост («ключи свет», «громкос», «включи свет в гост»), у фразы — первое или последнее слово.
- Чаще всего хозяин хочет: музыку (исполнитель или жанр, пауза, дальше, громче, тише), таймеры и будильники, умный дом (свет, кондиционер, розетки, телевизор — по названиям из каталога), реже — узнать факт (время, погода, вопрос).
- Иногда это вообще не обращение к колонке (люди разговаривают, телевизор, эхо). Тогда ответ непонятно.

Правила:
1. Чини только то, что слышно: звуковые замены, слипшиеся или разорванные слова, обрезанное начало или конец слова, которое в тексте есть («громкос» → «громкость», «в гост» → «в гостиной»).
2. Не добавляй то, чего в тексте нет: глагол действия, предмет, комнату, число, время. Если потерян глагол («люстру», «кондиционер», «вруби», «убавь» без предмета) или число («громкость», «таймер на») — верни 0|U|. Исключение: глагол однозначен по остальным словам («таймер на десять минут» → «поставь таймер на десять минут»).
3. «ключи», «лючи», «ючи» перед предметом — это «включи» или «выключи» с потерянным началом, и по тексту не разобрать. Верни самый вероятный вариант, но уверенность не выше 60. Так же с «громче/тише» и любыми противоположными действиями.
4. Слово рядом с числом, которое не похоже ни на что осмысленное («урок из 5», «кромкость 7», «громкасть пять»), — скорее всего название параметра: громкость, яркость, температура. Раз число названо, а команда одна по смыслу, это не угадывание глагола: «урок из 5» → «громкость 5».
5. Вопросы о состоянии («горит ли свет», «включён ли чайник», «какая громкость», «что включено») не превращай в команды: если сомневаешься — 0|U|.
6. Названия устройств, комнат и исполнителей бери точно из каталога, если слово на них похоже.
7. Результат — полная короткая фраза так, как сказал бы хозяин: без «пожалуйста», без пояснений. Если текст уже верен — верни его без изменений с высокой уверенностью.
8. Недавние реплики помогают: «да», «нет», число, «на кухне» могут быть ответом на вопрос колонки.
9. Команды меняют физический мир: лучше 0|U|, чем неверное действие.

Ответ — ровно одна строка: уверенность|тип|текст
Уверенность — целое 0..100. Тип: M музыка, T таймер или будильник, H умный дом, F вопрос или факт, S ответ на вопрос колонки, U непонятно. Для U пиши 0|U|
Больше ничего не пиши.

Примеры:
ключи свет на кухне → 55|H|включи свет на кухне
фключи освещение в стутии → 93|H|включи освещение в студии
громкос → 0|U|
урок из 5 → 92|H|громкость 5
кромкость семь → 93|H|громкость семь
полиция → 0|U|
прибавь громкос → 90|M|прибавь громкость
включи свет в гост → 85|H|включи свет в гостиной
таймер на десять минут → 85|T|поставь таймер на десять минут
люстру → 0|U|
горит ли ночник → 95|H|горит ли ночник
включи зверей → 86|M|включи Звери
громче → 97|M|громче
поставь таймер на → 0|U|
ну а потом он мне говорит → 0|U|"""


def settings_path() -> Path:
    config = Path(os.environ.get("AISIS_CONFIG_DIR", str(Path.home() / ".config" / "aisis")))
    return Path(os.environ.get("AISIS_ASR_SETTINGS", str(config / "asr.json")))


def load_settings() -> dict[str, Any]:
    """Defaults overridden per field by ~/.config/aisis/asr.json; an invalid value keeps its default."""
    settings = dict(DEFAULTS)
    try:
        raw = json.loads(settings_path().read_text())
    except (OSError, ValueError):
        return settings
    if not isinstance(raw, dict):
        return settings
    for name, default in DEFAULTS.items():
        value = raw.get(name)
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, str):
            ok = isinstance(value, str) and bool(value) and len(value) < 80
        elif isinstance(default, float):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1
        else:
            ok = isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5000
        if ok:
            settings[name] = value
    return settings


_WAKE = re.compile(r"^\s*(?:алиса|яндекс)\b[\s,.!:-]*", re.I)


def strip_wake_word(text: str) -> str:
    """«Алиса, громкость 5» -> «громкость 5»: the wake word opens a phrase, it is not part of the command."""
    return _WAKE.sub("", text, count=1).strip() or text.strip()


def words(text: str) -> list[str]:
    return re.sub(r"[^a-zа-я0-9 -]+", " ", text.lower().replace("ё", "е")).split()


def user_message(text: str, alternatives: list[str], recent: list[dict]) -> str:
    lines = []
    for turn in recent[-MAX_RECENT:]:
        role = "Колонка" if turn.get("role") == "assistant" else "Хозяин"
        lines.append(f"{role}: {str(turn.get('text', ''))[:MAX_TEXT_CHARS]}")
    block = "Недавний диалог:\n" + "\n".join(lines) + "\n" if lines else ""
    alts = [a for a in alternatives if a and a != text][:4]
    return block + (f"Другие варианты распознавания: {' | '.join(alts)}\n" if alts else "") + f"Распознано: {text}"


@dataclass(frozen=True)
class Verdict:
    text: str
    confidence: float
    intent: str

    def usable(self) -> bool:
        return self.intent != "unclear" and bool(self.text)


_REPLY = re.compile(r"^\s*(\d{1,3})\s*\|\s*([MTHFSU])\s*\|\s*(.*?)\s*$")


def parse_reply(reply: str, raw: str) -> Verdict:
    """The model's one line -> Verdict; anything malformed or implausible is «unclear» (never a guess)."""
    unclear = Verdict("", 0.0, "unclear")
    line = reply.strip().splitlines()[0] if reply.strip() else ""
    match = _REPLY.match(line)
    if not match:
        return unclear
    confidence = min(int(match.group(1)), 100) / 100
    intent = INTENTS[match.group(2)]
    text = match.group(3).strip().strip("«»\"'")
    if intent == "unclear" or not text or len(text) > MAX_TEXT_CHARS:
        return unclear
    # A correction repairs a short utterance; a much longer answer is an invention, not a repair.
    if len(words(text)) > len(words(raw)) + 4:
        return unclear
    return Verdict(text, confidence, intent)


def invented_words(raw: str, corrected: str) -> list[str]:
    """Words of the correction with no counterpart in what was heard: not a repair of a sound but an addition.

    Only a correction longer than what was heard can add anything. A word counts as heard when it is (or contains, or is contained in) a raw word, or sounds like one
    («ключи» ~ «включи», «громкос» ~ «громкость»). The typical addition is a lost verb: «свет в прихожей» -> «включи …»,
    and which verb was lost (on or off) is exactly what the model cannot know.
    """
    heard = words(raw)
    if len(words(corrected)) <= len(heard):
        return []  # a substitution («урок из 5» -> «громкость 5») repairs what was heard; only extra words are filled in
    added = []
    for word in words(corrected):
        if any(word == h or (len(h) >= 3 and (h in word or word in h)) or difflib.SequenceMatcher(None, word, h).ratio() >= 0.6
               for h in heard):
            continue
        added.append(word)
    return added


_OPPOSITES = (("включи", "выключи", "отключи"), ("включить", "выключить", "отключить"), ("включай", "выключай", "отключай"))


def lost_on_off(raw: str, corrected: str) -> bool:
    """«ключи свет» -> «включи свет»: what survived is the tail of both «включи» and «выключи» (and «отключи»)."""
    heard, fixed = words(raw), words(corrected)
    if not heard or not fixed or heard[0] == fixed[0]:
        return False
    return any(fixed[0] in group and all(verb.endswith(heard[0]) for verb in group) for group in _OPPOSITES)


def decide(verdict: Verdict, raw: str, settings: dict[str, Any]) -> dict[str, Any]:
    """The response the container acts on: `mode` is act | confirm | ignore, nothing else needs interpreting."""
    changed = verdict.usable() and words(verdict.text) != words(raw)
    mode = "ignore"
    if settings["enabled"] and verdict.usable() and changed:
        if verdict.confidence >= settings["act_threshold"]:
            mode = "act"
        elif verdict.confidence >= settings["confirm_threshold"]:
            mode = "confirm"
        # An added word (a lost verb the model filled in) is never acted on unasked, except the unambiguous timer verb.
        if mode == "act" and verdict.intent != "timer" and invented_words(raw, verdict.text):
            mode = "confirm"
        # A cut-off «ключи» cannot tell on from off, however sure the model sounds.
        if mode == "act" and lost_on_off(raw, verdict.text):
            mode = "confirm"
    return {"text": verdict.text if verdict.usable() else raw, "confidence": verdict.confidence,
            "intent": verdict.intent, "changed": changed, "mode": mode}


def unfit(text: str, settings: dict[str, Any]) -> bool:
    return len(words(text)) > settings["max_words"] or not words(text)


# ── the model call ───────────────────────────────────────────────────────────────────────────

def make_post(session: Any, claude_primary: Any) -> Callable[..., Any]:
    """`post(credential, body, timeout) -> reply text` over a keep-alive session; reuses the router's error mapping.

    Same request and failure semantics as claude_primary.anthropic_send, but on one long-lived connection:
    a fresh TLS handshake per phrase would eat a quarter of the latency budget.
    """
    import aiohttp

    async def post(credential: Any, body: dict, timeout: float) -> str:
        headers = {"x-api-key": credential.secret, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        try:
            async with session.post(claude_primary.API_URL, headers=headers, json=body, allow_redirects=False,
                                    timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                data = await response.json(content_type=None)
                return claude_primary.parse_response(response.status, dict(response.headers), data)
        except aiohttp.ClientError:  # a timeout is not the key's fault: it propagates without a cooldown
            raise claude_primary.Failover("transport_unavailable", 30) from None

    return post


class Corrector:
    """Holds credentials, a keep-alive HTTP session and a per-key cooldown; one `correct()` per phrase."""

    def __init__(self, load_credentials: Callable[[], list], post: Callable[..., Any],
                 catalog: Callable[[], voice_vocab.Catalog], log_db: str | None = None,
                 settings: Callable[[], dict[str, Any]] = load_settings, now: Callable[[], float] = time.monotonic):
        self._load = load_credentials
        self._post = post
        self._catalog = catalog
        self._log_db = log_db
        self._settings = settings
        self._now = now
        self._credentials: list = []
        self._loaded = 0.0
        self._cooldown: dict[str, float] = {}
        self._next = 0

    def _keys(self) -> list:
        if not self._credentials or self._now() - self._loaded > 300:
            self._credentials = self._load()
            self._loaded = self._now()
        live = [c for c in self._credentials if self._cooldown.get(c.fingerprint, 0) <= self._now()]
        if not live:
            return []
        self._next = (self._next + 1) % len(live)
        return live[self._next:] + live[:self._next]

    async def _ask(self, settings: dict[str, Any], system: str, message: str, budget: float) -> tuple[str, str]:
        """(model reply, status). Never raises: any failure is a status and the caller keeps the raw text."""
        deadline = self._now() + budget
        body = {"model": settings["model"], "max_tokens": 48, "temperature": 0,
                "system": system, "messages": [{"role": "user", "content": message}]}
        for credential in self._keys():
            remaining = deadline - self._now()
            if remaining < 0.15:
                return "", "timeout"
            try:
                reply = await asyncio.wait_for(self._post(credential, body, remaining), remaining)
                return reply, "ok"
            except asyncio.TimeoutError:
                return "", "timeout"
            except Exception as error:  # claude_primary.Failover carries a cooldown; anything else is transport
                cooldown = float(getattr(error, "cooldown_seconds", 30))
                self._cooldown[credential.fingerprint] = self._now() + min(cooldown, 600)
                LOG.warning("asr_correct key_failed code=%s cooldown=%ss", type(error).__name__, int(cooldown))
        return "", "no_key"

    async def correct(self, text: str, alternatives: list[str] | None = None, recent: list[dict] | None = None,
                      source: str = "") -> dict[str, Any]:
        settings = self._settings()
        started = self._now()
        raw = strip_wake_word(text)
        result: dict[str, Any] = {"text": raw, "confidence": 0.0, "intent": "unclear", "changed": False, "mode": "ignore"}
        status = "ok"
        if not settings["enabled"]:
            status = "disabled"
        elif unfit(raw, settings):
            status = "skipped_length"
        else:
            catalog = await asyncio.get_running_loop().run_in_executor(None, self._catalog)
            system = SYSTEM + "\n\nКаталог:\n" + voice_vocab.corrector_catalog(catalog)
            reply, status = await self._ask(settings, system, user_message(raw, alternatives or [], recent or []),
                                            settings["timeout_ms"] / 1000)
            if status == "ok":
                result = decide(parse_reply(reply, raw), raw, settings)
                if result["intent"] == "unclear":
                    status = "unclear"
        result["status"] = status
        result["ms"] = round((self._now() - started) * 1000)
        result["model"] = settings["model"]
        if self._log_db and status not in ("disabled", "skipped_length"):
            await asyncio.get_running_loop().run_in_executor(None, log_correction, self._log_db, raw, result, source)
        return result


# ── the log: raw vs corrected, for the dashboard and for scoring against owner-verified text ──────

LOG_SCHEMA = ("CREATE TABLE IF NOT EXISTS asr_corrections(id INTEGER PRIMARY KEY, ts REAL NOT NULL, raw TEXT NOT NULL, "
              "corrected TEXT NOT NULL, confidence REAL, intent TEXT, mode TEXT NOT NULL, status TEXT NOT NULL, "
              "ms INTEGER, model TEXT, source TEXT)",
              "CREATE INDEX IF NOT EXISTS asr_corrections_ts ON asr_corrections(ts)")


def log_correction(db_path: str, raw: str, result: dict[str, Any], source: str = "") -> None:
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            for statement in LOG_SCHEMA:
                conn.execute(statement)
            conn.execute("INSERT INTO asr_corrections(ts,raw,corrected,confidence,intent,mode,status,ms,model,source) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (time.time(), raw, result["text"], result["confidence"], result["intent"], result["mode"],
                          result["status"], result["ms"], result["model"], source))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as error:
        LOG.warning("asr_correct log_failed class=%s", type(error).__name__)
