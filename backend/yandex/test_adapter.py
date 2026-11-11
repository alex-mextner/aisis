"""Behavioral integration checks for the adapter's boundary contracts."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import io
import wave
import shutil
from aiohttp import web

from aiohttp.test_utils import TestClient, TestServer
from adapter import Adapter, ERROR_REPLY, NO_SPEECH_REPLY, run_command


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Adapter({"AISIS_STATE_DB": self.tmp.name + "/state.db", "AISIS_ALLOWED_STATIONS": "a,b"})
        self.client = TestClient(TestServer(self.app.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def request(self, station, text="hello"):
        ws = await self.client.ws_connect("/processor")
        await ws.send_json({"type": "prepare", "data": {}})
        await ws.send_json({"type": "process", "data": {"text": text, "metadata": {"stationId": station}}})
        result = await ws.receive_json(timeout=3)
        await ws.close()
        return result

    async def test_unconfigured_station_allowlist_fails_before_opening_state(self):
        for value in ("", " , ", "\t"):
            with self.subTest(value=value):
                path = self.tmp.name + "/unconfigured.db"
                with self.assertRaisesRegex(ValueError, "AISIS_ALLOWED_STATIONS"):
                    Adapter({"AISIS_STATE_DB": path, "AISIS_ALLOWED_STATIONS": value})
                self.assertFalse(Path(path).exists())

    async def test_direct_pm_rejects_preserved_old_outer_timeout_and_invalid_budgets(self):
        cases = [
            {"AISIS_CHATGPT_TIMEOUT": "180"},
            {"AISIS_PM_REPLY_TIMEOUT": "nan"},
            {"AISIS_PM_HTTP_TIMEOUT": "inf"},
            {"AISIS_PM_REQUEST_TIMEOUT": "0"},
            {"AISIS_PM_REPLY_TIMEOUT": "-1"},
            {"AISIS_PM_HTTP_TIMEOUT": "230"},
            {"AISIS_PM_REQUEST_TIMEOUT": "invalid"},
        ]
        for index, settings in enumerate(cases):
            with self.subTest(settings=settings):
                path = self.tmp.name + f"/invalid-budget-{index}.db"
                with self.assertRaisesRegex(ValueError, "timeout|TIMEOUT"):
                    Adapter({"AISIS_STATE_DB": path, "AISIS_ALLOWED_STATIONS": "a",
                        "AISIS_PM_URL": "http://127.0.0.1:31118/turn", **settings})
                self.assertFalse(Path(path).exists())

    async def test_direct_pm_accepts_coherent_defaults(self):
        adapter = Adapter({"AISIS_STATE_DB": self.tmp.name + "/valid-budget.db",
            "AISIS_ALLOWED_STATIONS": "a", "AISIS_PM_URL": "http://127.0.0.1:31118/turn"})
        adapter.store.db.close()

    async def test_custom_command_without_pm_url_keeps_independent_outer_timeout(self):
        adapter = Adapter({"AISIS_STATE_DB": self.tmp.name + "/custom-timeout.db",
            "AISIS_ALLOWED_STATIONS": "a", "AISIS_CHATGPT_TIMEOUT": "180"})
        adapter.store.db.close()

    async def test_blank_external_event_is_rejected_before_empty_speech_reply(self):
        async def turn(*args):
            self.fail("External event reached ChatGPT")
        self.app.turn = turn
        ws = await self.client.ws_connect("/processor")
        await ws.send_json({"type": "process", "data": {"isExternalEvent": True,
            "text": "  ", "metadata": {"stationId": "a"}}})
        response = await ws.receive_json(timeout=1)
        await ws.close()
        self.assertEqual(response["data"]["text"], ERROR_REPLY)
        self.assertEqual(self.app.store.db.execute("SELECT count(*) FROM turns").fetchone()[0], 0)

    async def test_empty_recognition_asks_to_repeat_without_chatgpt_or_turn_record(self):
        async def turn(*args):
            self.fail("Empty recognition reached ChatGPT")
        self.app.turn = turn
        response = await self.request("a", "  ")
        self.assertEqual(response["data"]["text"], NO_SPEECH_REPLY)
        self.assertTrue(response["data"]["finished"])
        self.assertEqual(self.app.store.db.execute("SELECT count(*) FROM turns").fetchone()[0], 0)

    async def test_unknown_station_never_calls_chatgpt(self):
        async def turn(*args):
            self.fail("Unknown station reached ChatGPT")
        self.app.turn = turn
        response = await self.request("not-allowed")
        self.assertEqual(response["data"]["text"], ERROR_REPLY)
        self.assertEqual(self.app.store.db.execute("SELECT count(*) FROM turns").fetchone()[0], 0)

    async def test_missing_identity_never_calls_chatgpt(self):
        called = False
        async def turn(*args):
            nonlocal called
            called = True
            return "bad"
        self.app.turn = turn
        response = await self.request(None)
        self.assertFalse(called)
        self.assertEqual(response["data"]["text"], ERROR_REPLY)
        self.assertTrue(response["data"]["finished"])

    async def test_two_speakers_keep_distinct_durable_conversations(self):
        helper = Path(self.tmp.name)/"bridge.py"
        helper.write_text('import sys,json\np=json.load(sys.stdin)\nprint(json.dumps({"text":p["conversation_id"] or "new", "conversation_id":p["conversation_id"] or "chat-"+p["station_id"]}))\n')
        self.app.command = [sys.executable, str(helper)]
        self.assertEqual((await self.request("a"))["data"]["text"], "new")
        self.assertEqual((await self.request("b"))["data"]["text"], "new")
        self.assertEqual((await self.request("a"))["data"]["text"], "chat-a")
        self.assertEqual(self.app.store.conversation("b"), "chat-b")
        with self.app.store.db:
            rows = self.app.store.db.execute("SELECT status FROM turns").fetchall()
        self.assertEqual(rows, [("complete",)]*3)
        # Transcript must not be persisted as chat history by this adapter.
        self.assertNotIn(b"hello", Path(self.tmp.name + "/state.db").read_bytes())

    async def test_failed_command_is_not_retried(self):
        count = Path(self.tmp.name)/"count"
        helper = Path(self.tmp.name)/"fail.py"
        helper.write_text(f'from pathlib import Path\np=Path({str(count)!r})\np.write_text(p.read_text()+"x" if p.exists() else "x")\nraise SystemExit(1)\n')
        self.app.command = [sys.executable, str(helper)]
        response = await self.request("a")
        self.assertEqual(response["data"]["text"], ERROR_REPLY)
        self.assertEqual(count.read_text(), "x")
        self.assertEqual(self.app.store.db.execute("SELECT status FROM turns").fetchone()[0], "failed")

    async def test_disconnect_cancels_running_turn(self):
        started = asyncio.Event()
        stopped = asyncio.Event()
        async def turn(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.app.turn = turn
        ws = await self.client.ws_connect("/processor")
        await ws.send_json({"type": "process", "data": {"text": "hello", "metadata": {"stationId": "a"}}})
        await asyncio.wait_for(started.wait(), 1)
        await ws.close()
        await asyncio.wait_for(stopped.wait(), 1)

    async def test_timeout_terminates_command(self):
        with self.assertRaises(asyncio.TimeoutError):
            await run_command([sys.executable, "-c", "import time;time.sleep(30)"], {}, .05)

    async def test_external_event_never_calls_chatgpt(self):
        ws = await self.client.ws_connect("/processor")
        await ws.send_json({"type": "process", "data": {"isExternalEvent": True, "text": "push", "metadata": {"stationId": "a"}}})
        response = await ws.receive_json(timeout=1)
        self.assertEqual(response["data"]["text"], ERROR_REPLY)
        await ws.close()

    async def test_only_known_voice_and_opus(self):
        r = await self.client.post("/v1/audio/speech", json={"input": "hello", "voice": "other"})
        self.assertEqual(r.status, 400)
        r = await self.client.post("/v1/audio/speech", json={"input": "hello", "voice": "Брачо", "response_format": "mp3"})
        self.assertEqual(r.status, 400)
        r = await self.client.post("/v1/audio/speech", json={"input": "hello", "voice": "Брачо", "response_format": "opus"})
        self.assertEqual(r.status, 503)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    async def test_fish_audio_is_fully_read_and_encoded_as_real_ogg_opus(self):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(bytes(3200))
        audio = buf.getvalue()
        async def fish(request):
            self.assertEqual(request.headers["model"], "s2.1-pro-free")
            body = await request.json()
            self.assertEqual(body["reference_id"], "5cd1ffa5dbb4419cb5766435139c6d92")
            response = web.StreamResponse(headers={"Content-Type": "audio/wav"})
            await response.prepare(request)
            await response.write(audio[:44])
            await asyncio.sleep(.02)
            await response.write(audio[44:])
            await response.write_eof()
            return response
        fish_app = web.Application()
        fish_app.router.add_post("/tts", fish)
        server = TestServer(fish_app)
        await server.start_server()
        try:
            self.app.env["FISH_API_KEY"] = "test-local-fake"
            self.app.env["FISH_TTS_URL"] = str(server.make_url("/tts"))
            response = await self.client.post("/v1/audio/speech", json={"input": "test", "voice": "Брачо", "response_format": "opus"})
            self.assertEqual(response.status, 200)
            ogg = await response.read()
            self.assertTrue(ogg.startswith(b"OggS"))
            self.assertIn(b"OpusHead", ogg[:256])
        finally:
            await server.close()


if __name__ == "__main__":
    unittest.main()
