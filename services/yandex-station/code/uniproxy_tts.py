"""Keyless Yandex TTS: the Alice uniproxy websocket the Yandex apps/browser use.

No Yandex Cloud key and no subscription: the endpoint accepts the public
developer API key that ships in Yandex's own SDK docs (`developers-simple-api-key`).
The reply text is sent to Yandex (owner-approved for the `alice` voice only).

Protocol (probed live 2026-11-08): open wss://uniproxy.alice.yandex.net/uni.ws,
send System.SynchronizeState {auth_token, uuid, lang, voice} then TTS.Generate
{text, voice, lang, format: audio/opus, emotion, speed}. The server answers
SynchronizeStateResponse, TTS.Speak, binary frames (4-byte stream id + a plain
Ogg Opus stream) and a closing `streamcontrol`. An unknown voice name gets no
answer at all, which is why every wait here is bounded: callers fall back to
another voice rather than wait.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

from aiohttp import ClientError, ClientSession, WSMsgType

UNIPROXY_URL = "wss://uniproxy.alice.yandex.net/uni.ws"
PUBLIC_API_KEY = "developers-simple-api-key"
MAX_TEXT = 5000
# Connect + handshake + first audio frame; measured 0.2-0.6 s, so a miss means a dead endpoint.
FIRST_AUDIO_S = 1.8
# Silence between frames once audio flows.
IDLE_S = 1.5
MAX_AUDIO_BYTES = 8 * 1024 * 1024


class UniproxyError(Exception):
    """Synthesis failed or timed out; the message never contains the text."""


def _event(name: str, namespace: str, payload: dict) -> str:
    return json.dumps({"event": {"header": {"messageId": str(uuid.uuid4()), "name": name,
        "namespace": namespace}, "payload": payload}})


async def synthesize(session: ClientSession, text: str, voice: str, emotion: str = "good", *,
        speed: float = 1.0, url: str = UNIPROXY_URL, api_key: str = PUBLIC_API_KEY) -> bytes:
    """Ogg Opus audio of `text` in `voice`; raises UniproxyError on any failure or timeout."""
    if not text.strip() or len(text) > MAX_TEXT:
        raise UniproxyError("text empty or over the limit")
    started = time.monotonic()
    first_deadline = started + FIRST_AUDIO_S
    total_deadline = started + 4 + len(text) / 400
    session_id = str(uuid.uuid4())
    try:
        ws = await asyncio.wait_for(session.ws_connect(url, max_msg_size=0, heartbeat=None), FIRST_AUDIO_S)
    except (ClientError, asyncio.TimeoutError, OSError) as exc:
        raise UniproxyError("connect failed: " + type(exc).__name__) from None
    try:
        await ws.send_str(_event("SynchronizeState", "System", {"auth_token": api_key, "uuid": session_id,
            "lang": "ru-RU", "voice": voice}))
        await ws.send_str(_event("Generate", "TTS", {"text": text, "voice": voice, "lang": "ru-RU",
            "format": "audio/opus", "quality": "UltraHigh", "emotion": emotion, "speed": speed, "mds": False}))
        chunks: list[bytes] = []
        size = 0
        last = started
        while True:
            now = time.monotonic()
            limit = (first_deadline if not chunks else min(total_deadline, last + IDLE_S)) - now
            if limit <= 0:
                raise UniproxyError("timeout" if not chunks else "stalled")
            try:
                message = await asyncio.wait_for(ws.receive(), limit)
            except asyncio.TimeoutError:
                raise UniproxyError("timeout" if not chunks else "stalled") from None
            last = time.monotonic()
            if message.type == WSMsgType.BINARY:
                size += len(message.data) - 4
                if size > MAX_AUDIO_BYTES:
                    raise UniproxyError("audio too large")
                chunks.append(message.data[4:])
            elif message.type == WSMsgType.TEXT:
                try:
                    data = json.loads(message.data)
                except ValueError:
                    continue
                control = data.get("streamcontrol")
                if isinstance(control, dict):
                    if control.get("reason", 0) != 0:
                        raise UniproxyError("stream error reason=%s" % control.get("reason"))
                    break
                header = data.get("directive", {}).get("header", {}) if isinstance(data.get("directive"), dict) else {}
                if header.get("name") == "Error":
                    raise UniproxyError("server error")
            else:
                raise UniproxyError("socket closed")
    except (ClientError, OSError) as exc:
        raise UniproxyError("transport: " + type(exc).__name__) from None
    finally:
        try:
            await asyncio.wait_for(ws.close(), 0.5)
        except (ClientError, asyncio.TimeoutError, OSError):
            pass
    audio = b"".join(chunks)
    if not audio.startswith(b"OggS") or b"OpusHead" not in audio[:256]:
        raise UniproxyError("not an Ogg Opus stream")
    return audio
