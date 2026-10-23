import argparse
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

import operate


class EnvironmentTests(unittest.TestCase):
    def test_preserves_unquoted_json_and_internal_whitespace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.env"
            path.write_text('AISIS_TEST_COMMAND=["/usr/bin/python3", "voice worker.py"]\nAISIS_TEST_TEXT=two  spaces\n')
            with patch.dict(os.environ, {}, clear=True):
                operate.load_env(path)
                self.assertEqual(os.environ["AISIS_TEST_COMMAND"], '["/usr/bin/python3", "voice worker.py"]')
                self.assertEqual(os.environ["AISIS_TEST_TEXT"], "two  spaces")

    def test_removes_outer_quotes_and_preserves_empty_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.env"
            path.write_text("AISIS_TEST_COMMAND='[\"python\", \"voice.py\"]'\nAISIS_TEST_EMPTY=\n")
            with patch.dict(os.environ, {}, clear=True):
                operate.load_env(path)
                self.assertEqual(os.environ["AISIS_TEST_COMMAND"], '["python", "voice.py"]')
                self.assertEqual(os.environ["AISIS_TEST_EMPTY"], "")


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self): return self.body


class Session:
    def __init__(self, pm_ready=True): self.pm_ready = pm_ready
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def get(self, url, **kwargs):
        if ":31117/" in url:
            return Response({"processor_configured": True, "tts_configured": True})
        if ":31118/" in url:
            return Response({"ready": self.pm_ready})
        if ":31121/" in url:
            return Response({"configured": True})
        return Response([])


class OperationalProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unready_browser_is_failure_even_if_other_services_respond(self):
        args = argparse.Namespace(start=False, speech=False, chatgpt=False)
        with patch.object(operate, "ClientSession", return_value=Session(pm_ready=False)), patch.object(operate, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 3]))), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "required_services_unhealthy"):
                await operate.probe(args)

    async def test_all_required_components_ready_is_success(self):
        args = argparse.Namespace(start=False, speech=False, chatgpt=False)
        with patch.object(operate, "ClientSession", return_value=Session()), redirect_stdout(io.StringIO()):
            await operate.probe(args)


if __name__ == "__main__":
    unittest.main()
