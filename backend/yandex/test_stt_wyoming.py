import asyncio
import json
import unittest

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import WSMsgType
from stt_wyoming import Endpoint, STTGateway, read_event, transcribe, write_event


class TestVAD:
    def is_speech(self, frame, rate):
        return frame[0] == 1


class STTTests(unittest.IsolatedAsyncioTestCase):
    def test_chunk_boundaries_do_not_change_endpointing_or_bytes(self):
        voice = bytes([1, 0]) * 320
        silence = bytes(640)
        pcm = silence * 10 + voice * 20 + silence * 40
        whole = Endpoint(TestVAD()).feed(pcm)
        endpoint = Endpoint(TestVAD())
        split = []
        for n in range(0, len(pcm), 137):
            split.extend(endpoint.feed(pcm[n:n+137]))
        self.assertEqual(whole, split)
        self.assertEqual(len(whole), 1)
        self.assertIn(voice * 20, whole[0])
        self.assertEqual(Endpoint(TestVAD()).feed(silence * 100), [])

    async def test_actual_wyoming_framing_and_audio_lifecycle(self):
        seen = []
        pcm = b"\x01\x00" * 1000
        async def server(reader, writer):
            try:
                # Read complete payload here so the test independently verifies audio.
                while True:
                    header = json.loads(await reader.readline())
                    data = json.loads(await reader.readexactly(header.get("data_length", 0)))
                    audio = await reader.readexactly(header.get("payload_length", 0))
                    seen.append((header["type"], data, audio))
                    if header["type"] == "audio-stop":
                        break
                # Inline data is also a valid Wyoming variant.
                writer.write(b'{"type":"transcript","data":{"text":"hello"}}\n')
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        service = await asyncio.start_server(server, "127.0.0.1", 0)
        port = service.sockets[0].getsockname()[1]
        try:
            self.assertEqual(await transcribe(f"tcp://127.0.0.1:{port}", pcm, 16000), "hello")
            self.assertEqual([x[0] for x in seen], ["transcribe", "audio-start", "audio-chunk", "audio-stop"])
            self.assertEqual(seen[0][1], {"language": "ru"})
            self.assertEqual(seen[1][1], {"rate": 16000, "width": 2, "channels": 1})
            self.assertEqual(seen[2][2], pcm)
        finally:
            service.close()
            await service.wait_closed()

    async def test_quasar_websocket_sends_final_only_after_voice_boundary(self):
        received = []
        async def recognize(uri, pcm, rate, lang):
            received.append((pcm, rate, lang))
            return "test answer"
        gateway = STTGateway({"AISIS_WYOMING_URI": "tcp://local:10300"}, vad_factory=TestVAD, recognizer=recognize)
        client = TestClient(TestServer(gateway.app()))
        await client.start_server()
        try:
            ws = await client.ws_connect("/")
            await ws.send_json({"sample_rate": 16000})
            await ws.send_bytes(bytes([1,0])*320*20 + bytes(640)*40)
            self.assertEqual(await ws.receive_json(timeout=2), {"text": "test answer", "end_of_utt": True})
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0][1:], (16000, "ru"))
            await ws.close()
        finally:
            await client.close()

    async def _gateway(self, recognize):
        gateway = STTGateway({"AISIS_WYOMING_URI": "tcp://local:10300"}, vad_factory=TestVAD, recognizer=recognize)
        client = TestClient(TestServer(gateway.app()))
        await client.start_server()
        ws = await client.ws_connect("/")
        await ws.send_json({"sample_rate": 16000})
        return client, ws

    async def test_native_finish_flushes_speech_before_vad_silence(self):
        calls = []
        async def recognize(uri, pcm, rate, lang):
            calls.append(pcm)
            return "native final"
        client, ws = await self._gateway(recognize)
        try:
            pcm = bytes([1, 0])*320*10
            await ws.send_bytes(pcm)
            await ws.send_json({"type": "finish"})
            response = await ws.receive_json(timeout=1)
            self.assertEqual(response, {"text": "native final", "end_of_utt": True})
            self.assertEqual(calls, [pcm])
        finally:
            await ws.close()
            await client.close()

    async def test_native_finish_during_delayed_asr_does_not_cancel_or_duplicate_it(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def recognize(uri, pcm, rate, lang):
            calls.append(pcm)
            started.set()
            await release.wait()
            return "delayed final"
        client, ws = await self._gateway(recognize)
        try:
            await ws.send_bytes(bytes([1, 0])*320*10 + bytes(640)*40)
            await asyncio.wait_for(started.wait(), 1)
            await ws.send_json({"type": "finish"})
            await ws.send_json({"type": "finish"})
            release.set()
            self.assertEqual(await ws.receive_json(timeout=1), {"text": "delayed final", "end_of_utt": True})
            await ws.send_json({"type": "finish"})
            with self.assertRaises(asyncio.TimeoutError):
                await ws.receive(timeout=.05)
            self.assertEqual(len(calls), 1)
        finally:
            await ws.close()
            await client.close()

    async def test_explicit_cancel_while_asr_pending_never_delivers_a_late_final(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def recognize(uri, pcm, rate, lang):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        client, ws = await self._gateway(recognize)
        try:
            await ws.send_bytes(bytes([1, 0])*320*10 + bytes(640)*40)
            await asyncio.wait_for(started.wait(), 1)
            await ws.send_json({"type": "cancel"})
            message = await ws.receive(timeout=1)
            self.assertIn(message.type, {WSMsgType.CLOSE, WSMsgType.CLOSED})
            await ws.close()
            await asyncio.wait_for(cancelled.wait(), 1)
        finally:
            await client.close()

    async def test_transport_close_cancels_recognition_instead_of_treating_it_as_eof(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def recognize(uri, pcm, rate, lang):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        client, ws = await self._gateway(recognize)
        try:
            await ws.send_bytes(bytes([1, 0])*320*10 + bytes(640)*40)
            await asyncio.wait_for(started.wait(), 1)
            await ws.close()
            await asyncio.wait_for(cancelled.wait(), 1)
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
