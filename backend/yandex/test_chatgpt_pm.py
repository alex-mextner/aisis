import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer
from chatgpt_pm import run


class BridgeClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        self.status = 200
        async def complete(request):
            self.calls.append(await request.json())
            if self.status != 200:
                return web.json_response({"error": "unavailable"}, status=self.status)
            return web.json_response({"choices": [{"message": {"content": "answer"}}]})
        app = web.Application()
        app.router.add_post("/v1/chat/completions", complete)
        self.server = TestServer(app)
        await self.server.start_server()
        self.env = patch.dict(os.environ, {
            "AISIS_CHATGPT_WEB_URL": str(self.server.make_url("/v1/chat/completions")),
            "AISIS_CHATGPT_HISTORY": self.tmp.name + "/history.db",
            "AISIS_PM_URL": "",
        })
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        await self.server.close()
        self.tmp.cleanup()

    async def test_speaker_continuity_and_isolation(self):
        first = await run({"station_id": "a", "text": "one", "turn_id": "1"})
        await run({"station_id": "b", "text": "other", "turn_id": "2"})
        await run({"station_id": "a", "text": "follow", "turn_id": "3", "conversation_id": first["conversation_id"]})
        self.assertEqual(len(self.calls[0]["messages"]), 1)
        self.assertEqual(len(self.calls[1]["messages"]), 1)
        self.assertEqual(self.calls[2]["messages"], [
            {"role": "user", "content": "one"}, {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "follow"}])

    async def test_failure_is_never_replayed(self):
        self.status = 503
        with self.assertRaises(RuntimeError):
            await run({"station_id": "a", "text": "one", "turn_id": "1"})
        self.status = 200
        with self.assertRaisesRegex(RuntimeError, "previous_turn_pending"):
            await run({"station_id": "a", "text": "two", "turn_id": "2"})
        self.assertEqual(len(self.calls), 1)

    async def test_paid_or_remote_endpoint_refused(self):
        os.environ["AISIS_CHATGPT_WEB_URL"] = "https://api.openai.com/v1/chat/completions"
        with self.assertRaisesRegex(RuntimeError, "existing_local"):
            await run({"station_id": "a", "text": "one", "turn_id": "1"})
        self.assertEqual(len(self.calls), 0)

    async def test_attached_pm_server_receives_exact_station_turn(self):
        seen = []
        async def turn(request):
            seen.append((request.headers.get("Authorization"), await request.json()))
            return web.json_response({"text": "pm reply", "conversation_id": "https://chatgpt.com/c/owned"})
        app = web.Application()
        app.router.add_post("/turn", turn)
        server = TestServer(app)
        await server.start_server()
        try:
            with patch.dict(os.environ, {"AISIS_PM_URL": str(server.make_url("/turn")), "AISIS_PM_TOKEN": "x"*32}):
                payload = {"station_id": "a", "text": "question", "turn_id": "turn-one", "conversation_id": None}
                result = await run(payload)
            self.assertEqual(result, {"text": "pm reply", "conversation_id": "https://chatgpt.com/c/owned"})
            self.assertEqual(seen, [("Bearer " + "x"*32, payload)])
        finally:
            await server.close()


if __name__ == "__main__":
    unittest.main()
