"""tts_voice routing: Брачо (Fish) by default, Yandex SpeechKit only when selected, local fallback."""
import io
import os
import shutil
import stat
import tempfile
import unittest
import wave
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import tts_settings
from adapter import Adapter

FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg")


def wav_bytes() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(3200))
    return buf.getvalue()


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"AISIS_CONFIG_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_default_is_bracho_and_missing_file_is_safe(self):
        self.assertEqual(tts_settings.load()["tts_voice"], "bracho")
        self.assertEqual(tts_settings.effective_voice(), "bracho")

    def test_speechkit_without_key_flags_needs_key_and_runs_keyless(self):
        tts_settings.update({"tts_voice": "alice", "alice_engine": "speechkit"})
        status = tts_settings.status()
        self.assertTrue(status["needs_key"])
        self.assertEqual(status["effective_engine"], "keyless")

    def test_alice_defaults_to_keyless_engine(self):
        tts_settings.update({"tts_voice": "alice"})
        status = tts_settings.status()
        self.assertFalse(status["needs_key"])
        self.assertEqual(status["effective_engine"], "keyless")

    def test_key_is_private_and_never_in_status(self):
        tts_settings.update({"tts_voice": "alice", "alice_engine": "speechkit"})
        tts_settings.write_key("AQVNsecretkey123")
        self.assertEqual(stat.S_IMODE(tts_settings.key_path().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(tts_settings.settings_path().stat().st_mode), 0o600)
        status = tts_settings.status()
        self.assertTrue(status["key_configured"])
        self.assertEqual(status["effective_voice"], "alice")
        self.assertNotIn("AQVNsecretkey123", str(status))
        tts_settings.clear_key()
        self.assertFalse(tts_settings.status()["key_configured"])

    def test_invalid_values_rejected_and_not_persisted(self):
        for bad in ({"tts_voice": "bob"}, {"yandex_voice": "x"}, {"yandex_emotion": "x"},
                    {"yandex_folder_id": "a b"}, {"nope": "1"}, {"tts_voice": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tts_settings.update(bad)
        self.assertEqual(tts_settings.load()["tts_voice"], "bracho")
        with self.assertRaises(ValueError):
            tts_settings.write_key("has space")

    def test_corrupt_file_falls_back_to_defaults(self):
        tts_settings.settings_path().write_text("{not json")
        self.assertEqual(tts_settings.load()["tts_voice"], "bracho")


@unittest.skipUnless(FFMPEG, "ffmpeg not installed")
class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"AISIS_CONFIG_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = {"fish": 0, "yandex": 0}
        self.yandex_status = 200
        self.yandex_form = None
        self.yandex_auth = None
        audio = wav_bytes()

        async def fish(request):
            self.calls["fish"] += 1
            return web.Response(body=audio, content_type="audio/wav")

        async def yandex(request):
            self.calls["yandex"] += 1
            self.yandex_form = await request.post()
            self.yandex_auth = request.headers.get("Authorization")
            if self.yandex_status != 200:
                return web.Response(status=self.yandex_status, text="nope")
            return web.Response(body=audio, content_type="audio/ogg")

        upstream = web.Application()
        upstream.router.add_post("/fish", fish)
        upstream.router.add_post("/yandex", yandex)
        self.upstream = TestServer(upstream)
        await self.upstream.start_server()
        self.adapter = Adapter({"AISIS_STATE_DB": self.tmp.name + "/state.db", "AISIS_ALLOWED_STATIONS": "a",
            "AISIS_MEMORY": "0", "FFMPEG": FFMPEG, "FISH_API_KEY": "fake-fish",
            "FISH_TTS_URL": str(self.upstream.make_url("/fish")),
            "YANDEX_TTS_URL": str(self.upstream.make_url("/yandex")),
            # Port 1 refuses connections: a test that reaches the keyless engine fails instead of hitting the real uniproxy.
            "AISIS_UNIPROXY_URL": "ws://127.0.0.1:1/"})
        self.client = TestClient(TestServer(self.adapter.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()
        self.tmp.cleanup()

    async def speak(self, headers=None):
        return await self.client.post("/v1/audio/speech", headers=headers or {},
            json={"input": "Привет", "voice": "Брачо", "response_format": "opus"})

    async def assert_ogg(self, response):
        self.assertEqual(response.status, 200)
        ogg = await response.read()
        self.assertTrue(ogg.startswith(b"OggS"))
        self.assertIn(b"OpusHead", ogg[:256])

    async def test_default_uses_fish_only(self):
        await self.assert_ogg(await self.speak())
        self.assertEqual(self.calls, {"fish": 1, "yandex": 0})

    async def test_alice_with_key_uses_yandex_only(self):
        tts_settings.update({"tts_voice": "alice", "alice_engine": "speechkit", "yandex_folder_id": "b1gfolder"})
        tts_settings.write_key("AQVNkey")
        await self.assert_ogg(await self.speak())
        self.assertEqual(self.calls, {"fish": 0, "yandex": 1})
        self.assertEqual(self.yandex_auth, "Api-Key AQVNkey")
        self.assertEqual(self.yandex_form["voice"], "alena")
        self.assertEqual(self.yandex_form["emotion"], "good")
        self.assertEqual(self.yandex_form["folderId"], "b1gfolder")
        self.assertEqual(self.yandex_form["text"], "Привет")
        health = await (await self.client.get("/healthz")).json()
        self.assertEqual(health["alice_tts"]["ok"], True)

    async def test_setting_is_read_per_request(self):
        tts_settings.write_key("AQVNkey")
        await self.assert_ogg(await self.speak())
        tts_settings.update({"tts_voice": "alice", "alice_engine": "speechkit"})
        await self.assert_ogg(await self.speak())
        tts_settings.update({"tts_voice": "bracho"})
        await self.assert_ogg(await self.speak())
        self.assertEqual(self.calls, {"fish": 2, "yandex": 1})

    async def test_alice_without_key_falls_back_to_bracho_without_calling_yandex(self):
        tts_settings.update({"tts_voice": "alice", "alice_engine": "speechkit"})
        await self.assert_ogg(await self.speak())
        self.assertEqual(self.calls, {"fish": 1, "yandex": 0})

    async def test_yandex_failure_falls_back_to_bracho_and_is_reported(self):
        tts_settings.update({"tts_voice": "alice", "alice_engine": "speechkit"})
        tts_settings.write_key("AQVNkey")
        self.yandex_status = 403
        await self.assert_ogg(await self.speak())
        self.assertEqual(self.calls, {"fish": 1, "yandex": 1})
        health = await (await self.client.get("/healthz")).json()
        self.assertEqual(health["alice_tts"]["ok"], False)
        self.assertEqual(health["alice_tts"]["status"], 502)

    async def test_pinned_alice_never_falls_back(self):
        tts_settings.update({"alice_engine": "speechkit"})
        pin = {"X-AISIS-Voice": "alice"}
        self.assertEqual((await self.speak(pin)).status, 502)  # no key: keyless engine, refused by the test URL
        tts_settings.write_key("AQVNkey")
        self.yandex_status = 500
        self.assertEqual((await self.speak(pin)).status, 502)
        self.assertEqual(self.calls["fish"], 0)

    async def test_pinned_bracho_ignores_alice_setting(self):
        tts_settings.update({"tts_voice": "alice", "alice_engine": "speechkit"})
        tts_settings.write_key("AQVNkey")
        await self.assert_ogg(await self.speak({"X-AISIS-Voice": "bracho"}))
        self.assertEqual(self.calls, {"fish": 1, "yandex": 0})

    async def test_unknown_pin_rejected(self):
        self.assertEqual((await self.speak({"X-AISIS-Voice": "bob"})).status, 400)

    async def test_over_limit_text_falls_back(self):
        tts_settings.update({"tts_voice": "alice", "alice_engine": "speechkit"})
        tts_settings.write_key("AQVNkey")
        response = await self.client.post("/v1/audio/speech",
            json={"input": "а" * 5001, "voice": "Брачо", "response_format": "opus"})
        await self.assert_ogg(response)
        self.assertEqual(self.calls, {"fish": 1, "yandex": 0})


if __name__ == "__main__":
    unittest.main()
