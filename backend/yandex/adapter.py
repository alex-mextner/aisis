"""Local Quasar processor and OpenAI-compatible Брачо speech gateway.

The ChatGPT command consumes one JSON line on stdin and returns one JSON object
{text, conversation_id}. It owns the browser; this service never retries a turn.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import time
import uuid
from collections import defaultdict
from typing import Any

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

LOG = logging.getLogger("aisis.yandex")
BRACHO_REFERENCE = "5cd1ffa5dbb4419cb5766435139c6d92"
ERROR_REPLY = "Не удалось получить ответ ChatGPT. Проверь авторизацию и подключение. Я не буду повторять запрос автоматически."


NO_SPEECH_REPLY = "Не расслышал. Повтори, пожалуйста."


class TurnError(Exception):
    pass


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (station TEXT PRIMARY KEY, conversation TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, station TEXT, status TEXT, started REAL)")
        self.db.execute("UPDATE turns SET status='interrupted' WHERE status='running'")
        self.db.commit()
        os.chmod(path, 0o600)

    def conversation(self, station: str) -> str | None:
        row = self.db.execute("SELECT conversation FROM sessions WHERE station=?", (station,)).fetchone()
        return row[0] if row else None

    def begin(self, station: str) -> str:
        turn = uuid.uuid4().hex
        self.db.execute("INSERT INTO turns VALUES (?,?,?,?)", (turn, station, "running", time.time()))
        self.db.commit()
        return turn

    def finish(self, turn: str, status: str, station: str, conversation: str | None = None):
        self.db.execute("UPDATE turns SET status=? WHERE id=?", (status, turn))
        if conversation:
            self.db.execute("INSERT INTO sessions VALUES (?,?) ON CONFLICT(station) DO UPDATE SET conversation=excluded.conversation", (station, conversation))
        self.db.commit()


async def run_command(command: list[str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """No shell, no automatic retry; kill children after disconnect or timeout."""
    proc = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        limit=1024 * 1024)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate((json.dumps(payload, ensure_ascii=False) + "\n").encode()), timeout)
        if proc.returncode or len(stdout) > 1024 * 1024:
            raise TurnError("chatgpt_command_failed")
        result = json.loads(stdout)
        if not isinstance(result, dict) or not isinstance(result.get("text"), str) or not result["text"].strip():
            raise TurnError("chatgpt_invalid_response")
        if result.get("conversation_id") is not None and not isinstance(result["conversation_id"], str):
            raise TurnError("chatgpt_invalid_conversation")
        return result
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
        raise


class Adapter:
    def __init__(self, env: dict[str, str] | None = None):
        self.env = env if env is not None else dict(os.environ)
        self.allowed = {station.strip() for station in self.env.get("AISIS_ALLOWED_STATIONS", "").split(",") if station.strip()}
        if not self.allowed:
            raise ValueError("AISIS_ALLOWED_STATIONS must contain at least one speaker ID")
        self.store = Store(self.env.get("AISIS_STATE_DB", str(Path.home()/".local/state/aisis-yandex/adapter.sqlite3")))
        self.command = json.loads(self.env.get("AISIS_CHATGPT_COMMAND", "[]"))
        if not isinstance(self.command, list) or not all(isinstance(x, str) for x in self.command):
            raise ValueError("AISIS_CHATGPT_COMMAND must be a JSON argv array")
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.tts_limit = asyncio.Semaphore(2)
        self.http: ClientSession | None = None

    async def start(self, _app):
        self.http = ClientSession(timeout=ClientTimeout(total=60))

    async def close(self, _app):
        await self.http.close()
        self.store.db.close()

    def authorize(self, request):
        token = self.env.get("AISIS_ADAPTER_TOKEN")
        if token and not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
            raise web.HTTPUnauthorized()

    def fish_key(self):
        key = self.env.get("FISH_API_KEY", "")
        if self.env.get("FISH_API_KEY_FILE"):
            key = Path(self.env["FISH_API_KEY_FILE"]).read_text().strip()
        return key

    async def health(self, request):
        return web.json_response({"service": "aisis-yandex", "processor_configured": bool(self.command),
            "tts_configured": bool(self.fish_key()), "automatic_retries": False})

    async def turn(self, station: str, text: str):
        if not self.command:
            raise TurnError("chatgpt_not_configured")
        async with self.locks[station]:
            turn = self.store.begin(station)
            try:
                result = await run_command(self.command, {"station_id": station, "text": text,
                    "conversation_id": self.store.conversation(station), "turn_id": turn},
                    float(self.env.get("AISIS_CHATGPT_TIMEOUT", "250")))
                self.store.finish(turn, "complete", station, result.get("conversation_id"))
                return result["text"].strip()
            except asyncio.CancelledError:
                self.store.finish(turn, "interrupted", station)
                raise
            except Exception:
                self.store.finish(turn, "failed", station)
                raise

    async def processor(self, request):
        self.authorize(request)
        ws = web.WebSocketResponse(max_msg_size=65536, heartbeat=30)
        await ws.prepare(request)
        pending: asyncio.Task | None = None

        async def process(data):
            try:
                station = data.get("metadata", {}).get("stationId")
                text = data.get("text")
                if not isinstance(station, str) or not station or station in {"unknown", "undefined"}:
                    raise TurnError("missing_station_id")
                if station not in self.allowed:
                    raise TurnError("station_not_allowed")
                if isinstance(text, str) and not text.strip():
                    if not ws.closed:
                        await ws.send_json({"type": "partialResponse", "data": {"text": NO_SPEECH_REPLY,
                            "directives": [], "finished": True, "shouldListen": False}})
                    return
                if not isinstance(text, str) or len(text) > 16000:
                    raise TurnError("invalid_text")
                if data.get("isExternalEvent"):
                    raise TurnError("external_events_disabled")
                reply = await self.turn(station, text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("processor_failed class=%s", type(exc).__name__)
                reply = ERROR_REPLY
            if not ws.closed:
                await ws.send_json({"type": "partialResponse", "data": {"text": reply,
                    "directives": [], "finished": True, "shouldListen": False}})

        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                        if payload.get("type") == "prepare":
                            continue
                        if payload.get("type") != "process" or not isinstance(payload.get("data"), dict):
                            await ws.close(code=1008, message=b"invalid process envelope")
                            break
                        if pending and not pending.done():
                            await ws.close(code=1008, message=b"turn already running")
                            break
                        pending = asyncio.create_task(process(payload["data"]))
                    except (ValueError, AttributeError):
                        await ws.close(code=1008, message=b"invalid JSON")
                        break
        finally:
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        return ws

    async def speech(self, request):
        self.authorize(request)
        body = await request.json()
        text = body.get("input")
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise web.HTTPBadRequest(text="invalid speech input")
        if body.get("voice") not in (None, "Брачо", "bracho", BRACHO_REFERENCE):
            raise web.HTTPBadRequest(text="only Брачо is configured")
        if body.get("response_format", "opus") != "opus":
            raise web.HTTPBadRequest(text="only opus is supported")
        key = self.fish_key()
        if not key:
            raise web.HTTPServiceUnavailable(text="Fish voice credentials not configured")
        async with self.tts_limit:
            # Fish HTTP JSON supports fixed voice reference and wav. Always transcode
            # to Ogg Opus: quasar's downstream decoder expects an Ogg container.
            async with self.http.post(self.env.get("FISH_TTS_URL", "https://api.fish.audio/v1/tts"),
                headers={"Authorization": "Bearer " + key, "model": self.env.get("FISH_MODEL", "s2.1-pro-free")},
                json={"text": text, "reference_id": BRACHO_REFERENCE, "format": "wav",
                    "latency": "balanced"}, allow_redirects=False) as result:
                if result.status != 200:
                    LOG.warning("fish_tts_failed status=%s", result.status)
                    raise web.HTTPBadGateway(text="Fish synthesis failed")
                chunks = []
                size = 0
                async for chunk in result.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 8 * 1024 * 1024:
                        raise web.HTTPBadGateway(text="speech response too large")
                    chunks.append(chunk)
                raw = b"".join(chunks)
            proc = await asyncio.create_subprocess_exec(self.env.get("FFMPEG", "ffmpeg"),
                "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "48000",
                "-c:a", "libopus", "-b:a", "64k", "-f", "ogg", "pipe:1",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                ogg, _ = await asyncio.wait_for(proc.communicate(raw), 30)
            except BaseException:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                raise
            if proc.returncode or not ogg.startswith(b"OggS") or b"OpusHead" not in ogg[:256]:
                raise web.HTTPBadGateway(text="Opus encoding failed")
            return web.Response(body=ogg, content_type="audio/ogg")

    def app(self):
        app = web.Application(client_max_size=65536)
        app.router.add_get("/healthz", self.health)
        app.router.add_get("/processor", self.processor)
        app.router.add_post("/v1/audio/speech", self.speech)
        app.on_startup.append(self.start)
        app.on_cleanup.append(self.close)
        return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(Adapter().app(), host="127.0.0.1", port=int(os.environ.get("AISIS_ADAPTER_PORT", "31117")), access_log=None)
