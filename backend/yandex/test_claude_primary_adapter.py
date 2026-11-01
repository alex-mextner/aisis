"""Real adapter -> JSON command -> loopback Anthropic/PM stand-ins. No real keys, no browser."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from adapter import Adapter, TurnError
import claude_primary as c

OLD_CONVERSATION = 'https://chatgpt.com/c/legacy-owned'
PM_CONVERSATION = 'https://chatgpt.com/c/pm-fallback-owned'
HERE = Path(c.__file__).parent


class AdapterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.secrets = ['sk-ant-usr-' + 'fixture' + str(n) * 30 for n in (1, 2, 3)]
        self.anthropic_status = 200
        self.anthropic_hits = []
        self.pm_hits = []
        self.pm_status = 200

        async def messages(request):
            await request.json()
            self.anthropic_hits.append(self.secrets.index(request.headers['x-api-key']) + 1)
            if self.anthropic_status != 200:
                return web.json_response({'error': {'type': 'api_error'}}, status=self.anthropic_status)
            return web.json_response({'content': [{'type': 'text', 'text': 'Ответ Claude'}],
                                      'stop_reason': 'end_turn'})

        async def pm_turn(request):
            payload = await request.json()
            self.pm_hits.append((request.headers.get('Authorization'), payload))
            if self.pm_status != 200:
                return web.json_response({'error': 'station_uncertain'}, status=self.pm_status)
            return web.json_response({'text': 'Ответ ChatGPT', 'conversation_id': PM_CONVERSATION})

        anthropic = web.Application()
        anthropic.router.add_post('/v1/messages', messages)
        pm = web.Application()
        pm.router.add_post('/turn', pm_turn)
        self.anthropic, self.pm = TestServer(anthropic), TestServer(pm)
        await self.anthropic.start_server()
        self.addAsyncCleanup(self.anthropic.close)
        await self.pm.start_server()
        self.addAsyncCleanup(self.pm.close)
        sources = []
        for label, secret in zip(('finance', 'calendar', 'report'), self.secrets, strict=True):
            path = self.root / f'{label}.env'
            path.write_text(f'CLAUDE_API_TOKEN={secret}\n')
            path.chmod(0o600)
            sources.append({'label': label, 'path': str(path), 'variables': ['CLAUDE_API_TOKEN']})
        sources_file = self.root / 'sources.json'
        sources_file.write_text(json.dumps(sources))
        sources_file.chmod(0o600)
        url = str(self.anthropic.make_url('/v1/messages'))
        script = (f'import sys;sys.path.insert(0,{str(HERE)!r});import claude_primary as c;'
                  f'c.API_URL={url!r};raise SystemExit(c.main())')
        # Production overlay values; the PM variables mirror runtime.env.
        self.env = {
            'AISIS_ALLOWED_STATIONS': 'a,b', 'AISIS_STATE_DB': str(self.root / 'adapter.sqlite3'),
            'AISIS_CHATGPT_COMMAND': json.dumps([sys.executable, '-c', script]),
            'AISIS_CHATGPT_TIMEOUT': '320', 'AISIS_ROUTER_TIMEOUT': '310', 'AISIS_CLAUDE_TIMEOUT': '20',
            'AISIS_PM_URL': str(self.pm.make_url('/turn')), 'AISIS_PM_TOKEN': 'p' * 32,
            'AISIS_PM_REPLY_TIMEOUT': '170', 'AISIS_PM_REQUEST_TIMEOUT': '230', 'AISIS_PM_HTTP_TIMEOUT': '240',
            'AISIS_CLAUDE_KEY_SOURCES': str(sources_file), 'AISIS_CLAUDE_STATE_DIR': str(self.root / 'claude'),
        }
        # The adapter hands its own process environment to the command.
        self.patch = patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.adapter = Adapter(dict(self.env))
        self.addCleanup(self.adapter.store.db.close)

    async def test_overlay_budgets_pass_adapter_validation_in_order(self):
        self.assertEqual(self.adapter.chatgpt_timeout, 320.0)
        settings = c.Settings.from_env(self.env)
        self.assertEqual((settings.attempt_timeout, settings.fallback_timeout, settings.total_timeout),
                         (20.0, 240.0, 310.0))
        self.assertLess(settings.total_timeout, self.adapter.chatgpt_timeout)

    async def test_claude_answer_keeps_old_conversation_and_never_touches_pm(self):
        self.adapter.store.finish(self.adapter.store.begin('a'), 'complete', 'a', OLD_CONVERSATION)
        self.assertEqual(await self.adapter.turn('a', 'Сколько будет шесть на семь?'), 'Ответ Claude')
        self.assertEqual(self.anthropic_hits, [1])
        self.assertEqual(self.pm_hits, [])
        self.assertEqual(self.adapter.store.conversation('a'), OLD_CONVERSATION)

    async def test_unavailable_keys_use_pm_exactly_once_then_cooldown_skips_keys(self):
        self.anthropic_status = 503
        self.assertEqual(await self.adapter.turn('a', 'первый'), 'Ответ ChatGPT')
        self.assertEqual(self.anthropic_hits, [1, 2, 3])
        self.assertEqual(len(self.pm_hits), 1)
        self.assertEqual(self.pm_hits[0][0], 'Bearer ' + 'p' * 32)
        self.assertEqual(self.adapter.store.conversation('a'), PM_CONVERSATION)
        self.assertEqual(await self.adapter.turn('a', 'второй'), 'Ответ ChatGPT')
        self.assertEqual(self.anthropic_hits, [1, 2, 3])
        self.assertEqual(len(self.pm_hits), 2)

    async def test_rejected_keys_are_all_tried_then_pm_answers_instead_of_the_turn_failing(self):
        self.anthropic_status = 404
        self.assertEqual(await self.adapter.turn('a', 'первый'), 'Ответ ChatGPT')
        self.assertEqual(self.anthropic_hits, [1, 2, 3])
        self.assertEqual(len(self.pm_hits), 1)

    async def test_guarded_pm_refusal_is_one_failed_turn_without_retry(self):
        self.anthropic_status = 503
        self.pm_status = 409
        with self.assertRaises(TurnError):
            await self.adapter.turn('a', 'нельзя')
        self.assertEqual(len(self.pm_hits), 1)
        self.assertEqual(self.anthropic_hits, [1, 2, 3])
        row = self.adapter.store.db.execute('SELECT status FROM turns').fetchall()
        self.assertEqual(row, [('failed',)])

    async def test_busy_station_lock_fails_closed_before_network_on_stderr(self):
        fd = c.station_lock(self.root / 'claude' / 'locks', 'a')
        try:
            with self.assertRaises(TurnError):
                await self.adapter.turn('a', 'занято')
        finally:
            os.close(fd)
        self.assertEqual(self.anthropic_hits, [])
        self.assertEqual(self.pm_hits, [])

    async def test_child_failure_is_stderr_json_with_empty_stdout_and_no_secret(self):
        proc = await asyncio.create_subprocess_exec(*json.loads(self.env['AISIS_CHATGPT_COMMAND']),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        payload = {'station_id': 'evil', 'turn_id': 'x', 'text': 'hi', 'conversation_id': None}
        out, err = await proc.communicate(json.dumps(payload).encode())
        self.assertEqual((proc.returncode, out), (1, b''))
        self.assertEqual(json.loads(err), {'error': 'station_not_allowed', 'automatic_replay': False})
        self.assertFalse((self.root / 'claude' / 'router.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
