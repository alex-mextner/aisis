#!/usr/bin/env python3
"""Quasar PCM WebSocket -> existing Wyoming ASR with local WebRTC endpointing.

No model download or duplicate recognizer. Audio is held in RAM, at most30s per
utterance; it is never saved to disk. Wyoming sees complete speech utterances.
"""
from __future__ import annotations

import asyncio
from collections import deque
import json
import logging
import os
import time
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

LOG = logging.getLogger("aisis.stt")


async def write_event(writer, kind, data=None, payload=b""):
    # Wyoming framing: JSON header + LF, optional JSON data bytes, raw payload.
    raw = json.dumps(data or {}, ensure_ascii=False).encode()
    header = {"type": kind, "data_length": len(raw)}
    if payload:
        header["payload_length"] = len(payload)
    writer.write(json.dumps(header).encode() + b"\n" + raw + payload)
    await writer.drain()


async def read_event(reader):
    line = await reader.readline()
    if not line or len(line) > 65536:
        raise RuntimeError("wyoming_invalid_header")
    event = json.loads(line)
    data = event.get("data", {})
    length = event.get("data_length", 0)
    payload_length = event.get("payload_length", 0)
    if not isinstance(length, int) or not 0 <= length <= 1048576 or not isinstance(payload_length, int) or not 0 <= payload_length <= 1048576:
        raise RuntimeError("wyoming_invalid_length")
    if length:
        data.update(json.loads(await reader.readexactly(length)))
    if payload_length:
        await reader.readexactly(payload_length)
    return event["type"], data


async def transcribe(uri, pcm, sample_rate, language="ru", timeout=30):
    address = urlsplit(uri)
    if address.scheme != "tcp" or not address.hostname or not address.port:
        raise ValueError("AISIS_WYOMING_URI must be tcp://host:port")
    reader, writer = await asyncio.wait_for(asyncio.open_connection(address.hostname, address.port), 5)
    try:
        async def exchange():
            await write_event(writer, "transcribe", {"language": language})
            fmt = {"rate": sample_rate, "width": 2, "channels": 1}
            await write_event(writer, "audio-start", fmt)
            for offset in range(0, len(pcm), 4096):
                await write_event(writer, "audio-chunk", fmt, pcm[offset:offset+4096])
            await write_event(writer, "audio-stop")
            for _ in range(100):
                kind, data = await read_event(reader)
                if kind == "error":
                    raise RuntimeError("wyoming_asr_error")
                if kind == "transcript":
                    text = data.get("text")
                    if not isinstance(text, str):
                        raise RuntimeError("wyoming_invalid_transcript")
                    return text.strip()
            raise RuntimeError("wyoming_missing_transcript")
        return await asyncio.wait_for(exchange(), timeout)
    finally:
        writer.close()
        await writer.wait_closed()


class Endpoint:
    """20ms frames,200ms preroll,800ms trailing silence and30s hard cap."""
    def __init__(self, vad, sample_rate=16000):
        self.vad = vad
        self.rate = sample_rate
        self.frame_size = sample_rate * 2 // 50
        self.pending = bytearray()
        self.preroll = deque(maxlen=10)
        self.audio = bytearray()
        self.voiced = 0
        self.silence = 0

    def feed(self, chunk):
        self.pending.extend(chunk)
        completed = []
        while len(self.pending) >= self.frame_size:
            frame = bytes(self.pending[:self.frame_size])
            del self.pending[:self.frame_size]
            voice = self.vad.is_speech(frame, self.rate)
            if not self.audio:
                self.preroll.append(frame)
                if not voice:
                    continue
                self.audio.extend(b"".join(self.preroll))
                self.preroll.clear()
            else:
                self.audio.extend(frame)
            if voice:
                self.voiced += 1
                self.silence = 0
            else:
                self.silence += 1
            if self.silence >= 40 or len(self.audio) >= self.rate * 2 * 30:
                audio = self.flush()
                if audio:
                    completed.append(audio)
        return completed

    def flush(self, include_partial=False):
        if include_partial and self.audio:
            self.audio.extend(self.pending[:len(self.pending)//2*2])
            self.pending.clear()
        audio = bytes(self.audio) if self.voiced >= 5 else b""
        self.audio.clear()
        self.voiced = self.silence = 0
        return audio


class STTGateway:
    def __init__(self, env=None, vad_factory=None, recognizer=transcribe):
        self.env = dict(os.environ) if env is None else env
        self.vad_factory = vad_factory
        self.recognizer = recognizer
        self.slots = asyncio.Semaphore(2)

    async def health(self, request):
        return web.json_response({"service": "aisis-wyoming-stt", "configured": bool(self.env.get("AISIS_WYOMING_URI")), "model_downloads": False})

    async def websocket(self, request):
        if not self.env.get("AISIS_WYOMING_URI"):
            raise web.HTTPServiceUnavailable(text="Wyoming recognizer not configured")
        if self.vad_factory is None:
            import webrtcvad
            factory = lambda: webrtcvad.Vad(2)
        else:
            factory = self.vad_factory
        ws = web.WebSocketResponse(max_msg_size=1024*1024, heartbeat=15)
        await ws.prepare(request)
        endpoint = Endpoint(factory())
        initialized = False
        last_audio = time.monotonic()
        recognition = None
        final_sent = False
        cancelled = False

        async def finish(pcm):
            nonlocal final_sent
            try:
                text = ""
                if pcm:
                    async with self.slots:
                        text = await asyncio.wait_for(self.recognizer(self.env["AISIS_WYOMING_URI"], pcm, endpoint.rate,
                            self.env.get("AISIS_STT_LANGUAGE", "ru")), 35)
                if not ws.closed and not cancelled and not final_sent:
                    final_sent = True
                    await ws.send_json({"text": text, "end_of_utt": True})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("stt_failed class=%s", type(exc).__name__)
                if not ws.closed:
                    await ws.close(code=1011, message=b"recognizer unavailable")

        def finalize(pcm):
            nonlocal recognition
            # Exactly one utterance/final per Quasar STT session. Native finish,
            # VAD, and idle endpointing all converge on this one task.
            if recognition is None and not final_sent:
                recognition = asyncio.create_task(finish(pcm))

        try:
            while not ws.closed:
                try:
                    message = await ws.receive(timeout=1.2)
                except asyncio.TimeoutError:
                    if endpoint.audio and recognition is None:
                        finalize(endpoint.flush(include_partial=True))
                    if time.monotonic() - last_audio > 40:
                        await ws.close(code=1000, message=b"idle audio stream")
                    continue
                if message.type == WSMsgType.TEXT:
                    config = json.loads(message.data)
                    if config.get("type") == "cancel":
                        cancelled = True
                        await ws.close(code=1000, message=b"audio cancelled")
                        break
                    if config.get("type") == "finish":
                        # Normal native EOF flushes immediately; it never cancels
                        # an ASR call already started by VAD.
                        finalize(endpoint.flush(include_partial=True))
                        continue
                    rate = config.get("sample_rate")
                    if initialized or type(rate) is not int or rate not in {8000, 16000, 32000, 48000}:
                        await ws.close(code=1008, message=b"invalid sample rate")
                        break
                    endpoint = Endpoint(factory(), rate)
                    initialized = True
                elif message.type == WSMsgType.BINARY:
                    if not initialized:
                        await ws.close(code=1008, message=b"sample rate required")
                        break
                    last_audio = time.monotonic()
                    if recognition is None and not final_sent:
                        for pcm in endpoint.feed(message.data):
                            finalize(pcm)
                elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("stt_failed class=%s", type(exc).__name__)
            await ws.close(code=1011, message=b"recognizer unavailable")
        finally:
            cancelled = True
            # A websocket transport close is cancellation, not normal EOF. The
            # native caller must send finish and wait for its final JSON first.
            if recognition is not None and not recognition.done():
                recognition.cancel()
                await asyncio.gather(recognition, return_exceptions=True)

        return ws

    def app(self):
        app = web.Application()
        app.router.add_get("/", self.websocket)
        app.router.add_get("/healthz", self.health)
        return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(STTGateway().app(), host="127.0.0.1", port=int(os.environ.get("AISIS_STT_PORT", "31121")), access_log=None)
