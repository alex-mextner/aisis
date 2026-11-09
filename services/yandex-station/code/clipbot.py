"""The clip feed's own Telegram bot: voice notes with buttons, taps and replies applied without an agent session.

The main `tg` bot cannot do this: its updates belong to the tg-ctl daemon, which has no hook for a service and
answers an unknown button with «expired». A second bot made only for the feed owns its own getUpdates (nothing to
steal), so the service can send each clip as a voice note with inline buttons and apply what the owner taps or
replies by itself:

    ✅ Верно    🔇 Шум / не речь
    ✏️ Исправить (asks for the real words; a reply to the clip message works too)    ⏭ Пропустить

The token lives in a private env file (CLIPFEED_BOT_TOKEN=…, default ~/.config/aisis/clipfeed-bot.env) that is
picked up without a restart; it is never printed or logged. Only the owner's chat is served: the owner chat id is
`tg auth status`' chat id (or CLIPFEED_OWNER_CHAT_ID in the same file), every other sender is ignored. Telegram only
lets a bot write to a user who pressed Start, so until then the service reports `need_start` and the feed keeps
using the `tg` CLI.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import cliptruth

LOG = logging.getLogger("aisis.clipbot")

API_BASE = "https://api.telegram.org"
TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")
CHAT_RE = re.compile(r"^chat:.*\bid (\d+)\s*$", re.M)
POLL_TIMEOUT_S = 25
RETRY_S = 10
PROMPT_KEY = "prompt:"
VERDICT_ECHO_CHARS = 80
BOT_FOOTER = "Кнопки ниже или ответом на сообщение: что сказали на самом деле. Могли говорить издалека, начало или конец могло обрезаться."

ACTIONS = {"ok": "correct", "no": "noise"}


class BotError(Exception):
    """A Telegram Bot API refusal or a transport failure; never carries the token."""

    def __init__(self, code: int, description: str):
        super().__init__("%s %s" % (code, description))
        self.code, self.description = code, description


class BotUnavailable(Exception):
    """The bot cannot deliver right now (no token, owner has not pressed Start, API down): use the tg CLI."""


def env_path() -> Path:
    config = Path(os.environ.get("AISIS_CONFIG_DIR", str(Path.home() / ".config" / "aisis")))
    return Path(os.environ.get("AISIS_CLIPFEED_BOT_ENV", str(config / "clipfeed-bot.env")))


def read_env() -> dict[str, str]:
    values = {}
    try:
        text = env_path().read_text()
    except OSError:
        return values
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            values[key.strip()] = value.strip().strip("'\"")
    return values


def owner_chat_id(env: dict[str, str]) -> int | None:
    configured = env.get("CLIPFEED_OWNER_CHAT_ID", "")
    if configured.isdigit():
        return int(configured)
    try:
        done = subprocess.run(["tg", "auth", "status"], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                              env=_tg_env())
    except (OSError, subprocess.SubprocessError):
        return None
    match = CHAT_RE.search(done.stdout)
    return int(match.group(1)) if match else None


def _tg_env() -> dict:
    env = dict(os.environ)
    home = Path.home()
    env["PATH"] = os.pathsep.join([str(home / ".local" / "bin"), str(home / ".bun" / "bin"), env.get("PATH", "")])
    return env


def encode_multipart(fields: dict[str, str], file_field: str, path: Path) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, name, value)).encode())
    head = ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: application/octet-stream\r\n\r\n'
            % (boundary, file_field, path.name)).encode()
    parts.append(head + path.read_bytes() + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    return b"".join(parts), "multipart/form-data; boundary=%s" % boundary


class BotApi:
    def __init__(self, token: str, base: str | None = None):
        self._token = token
        self._base = (base or os.environ.get("AISIS_CLIPFEED_API_BASE") or API_BASE).rstrip("/")

    def call(self, method: str, params: dict | None = None, upload: tuple[str, Path] | None = None, timeout: float = 40):
        url = "%s/bot%s/%s" % (self._base, self._token, method)
        params = params or {}
        if upload:
            body, ctype = encode_multipart({k: v if isinstance(v, str) else json.dumps(v) for k, v in params.items()}, upload[0], upload[1])
        else:
            body, ctype = json.dumps(params).encode(), "application/json"
        request = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
            except ValueError:
                payload = {"ok": False, "error_code": exc.code, "description": "http error"}
        except (OSError, ValueError) as exc:
            raise BotError(0, "transport: %s" % type(exc).__name__) from None
        if not payload.get("ok"):
            raise BotError(int(payload.get("error_code") or 0), str(payload.get("description") or "")[:200])
        return payload["result"]


def keyboard(clip_id: str) -> dict:
    return {"inline_keyboard": [
        [{"text": "✅ Верно", "callback_data": "v:%s:ok" % clip_id}, {"text": "🔇 Шум", "callback_data": "v:%s:no" % clip_id}],
        [{"text": "✏️ Исправить", "callback_data": "v:%s:ed" % clip_id}, {"text": "⏭ Пропустить", "callback_data": "v:%s:sk" % clip_id}],
    ]}


def verdict_line(stored: dict) -> str:
    if stored["label"] == "noise":
        return "🔇 Эталон: не речь"
    if stored["label"] == "correct":
        return "✅ Эталон: верно"
    text = stored["truth"] if len(stored["truth"]) <= VERDICT_ECHO_CHARS else stored["truth"][:VERDICT_ECHO_CHARS - 1] + "…"
    wer = "" if stored["wer"] is None else " · WER %d%%" % round(stored["wer"] * 100)
    return "✏️ Эталон: «%s»%s" % (text, wer)


class BotService:
    """Sender + inbox of the feed bot. `db` is the dialogs DB; everything else is read from the private env file."""

    def __init__(self, db: str, now=time.time, owner=owner_chat_id):
        self.db, self.now, self._owner = db, now, owner
        self._api: BotApi | None = None
        self._api_key = None
        self._owner_id: int | None = None
        self._blocked_until = 0.0
        self.status = ""  # unknown until the first check writes it

    # ---- state -------------------------------------------------------------------------------------------------

    def _set_status(self, status: str) -> None:
        if status == self.status:
            return
        self.status = status
        conn = cliptruth.connect(self.db)
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO clip_feed_state(k, v) VALUES('bot_status', ?)", (status,))
        finally:
            conn.close()

    def api(self) -> BotApi | None:
        """The API client for the configured token (re-read when the env file changes), None when there is no token."""
        try:
            key = env_path().stat().st_mtime_ns
        except OSError:
            self._api, self._api_key = None, None
            self._set_status("none")
            return None
        if key != self._api_key:
            env = read_env()
            token = env.get("CLIPFEED_BOT_TOKEN", "")
            self._api_key = key
            self._owner_id = None
            self._api = BotApi(token) if TOKEN_RE.match(token) else None
            if self._api is None:
                self._set_status("bad_token")
        return self._api

    def owner(self) -> int | None:
        if self._owner_id is None:
            self._owner_id = self._owner(read_env())
        return self._owner_id

    def available(self) -> bool:
        return self.api() is not None and self.now() >= self._blocked_until

    # ---- sending -----------------------------------------------------------------------------------------------

    def post(self, clip: dict, caption: str, audio: Path) -> int:
        """Send the clip as a voice note with the buttons; returns the message id. BotUnavailable → caller uses tg."""
        api, owner = self.api(), self.owner()
        if api is None or owner is None or self.now() < self._blocked_until:
            raise BotUnavailable("no bot")
        voice = audio.suffix == ".ogg"
        params = {"chat_id": owner, "caption": caption, "reply_markup": keyboard(clip["id"])}
        if voice and clip.get("dur") is not None:
            params["duration"] = max(1, round(clip["dur"]))
        try:
            sent = api.call("sendVoice" if voice else "sendDocument", params, upload=("voice" if voice else "document", audio))
        except BotError as exc:
            if exc.code in (400, 403, 401):  # not started / blocked / bad token: tg covers, retry the bot later
                self._blocked_until = self.now() + 60
                self._set_status("need_start" if exc.code in (400, 403) else "bad_token")
            elif exc.code == 429:
                self._blocked_until = self.now() + 30
            LOG.warning("clip_bot_send_failed code=%s", exc.code)
            raise BotUnavailable(str(exc.code)) from None
        self._set_status("ready")
        return int(sent["message_id"])

    # ---- inbox -------------------------------------------------------------------------------------------------

    def _offset(self, conn) -> int:
        row = conn.execute("SELECT v FROM clip_feed_state WHERE k='bot_offset'").fetchone()
        return int(row["v"]) if row else 0

    def poll_once(self, timeout: int = POLL_TIMEOUT_S) -> int:
        """One getUpdates round; returns how many updates were handled. Offsets persist so a restart replays nothing."""
        api = self.api()
        if api is None:
            return 0
        conn = cliptruth.connect(self.db)
        try:
            offset = self._offset(conn)
        finally:
            conn.close()
        updates = api.call("getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
                           timeout=timeout + 15)
        for update in updates:
            try:
                self.handle(api, update)
            except Exception:  # one bad update must not wedge the queue
                LOG.exception("clip_bot_update_failed")
            conn = cliptruth.connect(self.db)
            try:
                with conn:
                    conn.execute("INSERT OR REPLACE INTO clip_feed_state(k, v) VALUES('bot_offset', ?)", (str(update["update_id"] + 1),))
            finally:
                conn.close()
        return len(updates)

    def loop(self) -> None:
        while True:
            try:
                if self.api() is None:
                    time.sleep(RETRY_S)
                    continue
                self.poll_once()
            except BotError as exc:
                if exc.code == 409:
                    self._set_status("conflict")  # another consumer of this token: its owner must stop it
                elif exc.code == 401:
                    self._set_status("bad_token")
                LOG.warning("clip_bot_poll_failed code=%s", exc.code)
                time.sleep(RETRY_S)
            except Exception:
                LOG.exception("clip_bot_loop_failed")
                time.sleep(RETRY_S)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.loop, daemon=True, name="clip-bot-inbox")
        thread.start()
        return thread

    # ---- handling ----------------------------------------------------------------------------------------------

    def handle(self, api: BotApi, update: dict) -> None:
        owner = self.owner()
        callback = update.get("callback_query")
        message = update.get("message")
        sender = (callback or message or {}).get("from", {}).get("id")
        if owner is None or sender != owner:
            return  # nobody but the owner is served, and nobody else gets an answer
        if callback:
            self._callback(api, callback)
        elif message:
            self._message(api, message)

    def _feed_row(self, conn, msg_id: int):
        return conn.execute("SELECT clip_id, caption FROM clip_feed WHERE tg_msg_id=? AND via='bot' AND state='sent'", (msg_id,)).fetchone()

    def _apply(self, api: BotApi, clip_id: str, msg_id: int, caption: str | None, label: str, text: str | None) -> str | None:
        """Store the verdict and show it under the clip. Returns an error text for the owner, None on success."""
        try:
            stored = cliptruth.set_truth(self.db, clip_id, label, text, source="tg-bot", tg_msg_id=msg_id)
        except cliptruth.TruthError as exc:
            return "Не сохранено: %s" % exc
        if caption is not None:
            self._edit(api, msg_id, caption + "\n\n" + verdict_line(stored), clip_id)
        return None

    def _edit(self, api: BotApi, msg_id: int, caption: str, clip_id: str) -> None:
        try:
            api.call("editMessageCaption", {"chat_id": self.owner(), "message_id": msg_id, "caption": caption[:1024],
                                            "reply_markup": keyboard(clip_id)})
        except BotError as exc:
            if "not modified" not in exc.description:  # tapping the same verdict twice is fine
                LOG.warning("clip_bot_edit_failed code=%s", exc.code)

    def _answer(self, api: BotApi, callback_id: str, text: str) -> None:
        try:
            api.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})
        except BotError:
            pass  # a stale query id: the tap was still applied

    def _callback(self, api: BotApi, callback: dict) -> None:
        match = re.match(r"^v:([^:]+):(ok|no|ed|sk)$", callback.get("data") or "")
        message = callback.get("message") or {}
        msg_id = message.get("message_id")
        if not match or msg_id is None:
            return self._answer(api, callback["id"], "Кнопка устарела")
        clip_id, action = match.groups()
        conn = cliptruth.connect(self.db)
        try:
            row = self._feed_row(conn, msg_id)
        finally:
            conn.close()
        if row is None or row["clip_id"] != clip_id:
            return self._answer(api, callback["id"], "Клип не найден")
        caption = row["caption"] or message.get("caption") or ""
        if action in ACTIONS:
            error = self._apply(api, clip_id, msg_id, caption, ACTIONS[action], None)
            return self._answer(api, callback["id"], error or ("Верно ✅" if action == "ok" else "Не речь 🔇"))
        if action == "sk":
            conn = cliptruth.connect(self.db)
            try:
                with conn:
                    conn.execute("UPDATE clip_feed SET why='owner-skip' WHERE clip_id=?", (clip_id,))
            finally:
                conn.close()
            self._edit(api, msg_id, caption + "\n\n⏭ Пропущено", clip_id)
            return self._answer(api, callback["id"], "Пропущено")
        prompt = api.call("sendMessage", {"chat_id": self.owner(), "text": "✏️ Что сказали на самом деле? Ответьте на это сообщение.",
                                          "reply_to_message_id": msg_id,
                                          "reply_markup": {"force_reply": True, "input_field_placeholder": "что сказали", "selective": True}})
        conn = cliptruth.connect(self.db)
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO clip_feed_state(k, v) VALUES(?, ?)", (PROMPT_KEY + str(prompt["message_id"]), clip_id))
        finally:
            conn.close()
        self._answer(api, callback["id"], "Напишите, что сказали")

    def _message(self, api: BotApi, message: dict) -> None:
        text = (message.get("text") or "").strip()
        if text.startswith("/start"):
            self._set_status("ready")
            api.call("sendMessage", {"chat_id": self.owner(), "text": "Лента реплик подключена: записи придут сюда с кнопками."})
            return
        reply = message.get("reply_to_message")
        if not text or not reply:
            return
        target = reply["message_id"]
        conn = cliptruth.connect(self.db)
        try:
            prompt = conn.execute("SELECT v FROM clip_feed_state WHERE k=?", (PROMPT_KEY + str(target),)).fetchone()
            row = None
            if prompt:
                row = conn.execute("SELECT clip_id, caption, tg_msg_id FROM clip_feed WHERE clip_id=? AND via='bot'", (prompt["v"],)).fetchone()
            else:
                row = conn.execute("SELECT clip_id, caption, tg_msg_id FROM clip_feed WHERE tg_msg_id=? AND via='bot' AND state='sent'",
                                   (target,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return
        try:
            label, truth = cliptruth.parse_reply(text)
        except cliptruth.TruthError:
            return
        error = self._apply(api, row["clip_id"], row["tg_msg_id"], row["caption"], label, truth)
        if error:
            api.call("sendMessage", {"chat_id": self.owner(), "text": error, "reply_to_message_id": message["message_id"]})
            return
        if prompt:
            try:
                api.call("deleteMessage", {"chat_id": self.owner(), "message_id": target})
            except BotError:
                pass
        try:
            api.call("setMessageReaction", {"chat_id": self.owner(), "message_id": message["message_id"], "reaction": [{"type": "emoji", "emoji": "👍"}]})
        except BotError:
            pass
