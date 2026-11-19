"""Focused cache/usage contracts, using only a local HTTP fixture and fake keys."""
import asyncio
import copy
import io
from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import threading
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import claude_primary as c


class PrefixTests(unittest.TestCase):
    def setUp(self):
        self.router = c.Router(c.Settings(allowed=frozenset({'speaker'})), [], None)
        self.payload = dict(station_id='speaker', turn_id='one', text='Current private question',
                            conversation_id=None, persona='Actual stable owner instructions')

    def test_persona_precedes_dynamic_context_and_has_the_only_cache_boundary(self):
        self.router.validate(self.payload)
        first = self.router._request_body(self.payload, [])
        second = self.router._request_body({**self.payload, 'text': 'Different memory and question'}, [])
        self.assertEqual(first['system'], second['system'])
        self.assertEqual([b['text'] for b in first['system']], [c.SYSTEM, self.payload['persona']])
        self.assertNotIn('cache_control', first['system'][0])
        self.assertEqual(first['system'][-1]['cache_control'], {'type': 'ephemeral', 'ttl': '5m'})
        self.assertEqual(first['messages'][-1]['content'], self.payload['text'])

    def test_no_padding_without_persona(self):
        body = self.router._request_body({k: v for k, v in self.payload.items() if k != 'persona'}, [])
        self.assertEqual([b['text'] for b in body['system']], [c.SYSTEM])

    def test_reserve_preserves_persona_and_question_without_new_payload_fields(self):
        sent = c.reserve_payload(self.payload, [])
        self.assertNotIn('persona', sent)
        self.assertIn(self.payload['persona'], sent['text'])
        self.assertIn(self.payload['text'], sent['text'])
        self.assertLessEqual(len(sent['text']), 16000)

    def test_reserve_drops_overlong_context_before_the_current_question(self):
        payload = {**self.payload, 'persona': 'p' * 16000, 'text': 'q' * 16000}
        sent = c.reserve_payload(payload, [{'role': 'user', 'content': 'old'}])
        self.assertNotIn('persona', sent)
        self.assertEqual(sent['text'], payload['text'])

    def test_whitespace_persona_never_creates_an_empty_provider_block(self):
        body = self.router._request_body({**self.payload, 'persona': ' \n\t'}, [])
        self.assertEqual([block['text'] for block in body['system']], [c.SYSTEM])
        self.assertEqual(c.reserve_payload({**self.payload, 'persona': ' \n\t'}, [])['text'], self.payload['text'])

    def test_reserve_checks_serialized_bytes_even_without_history(self):
        payload = {**self.payload, 'persona': '😀' * 15000, 'text': 'Keep current question'}
        sent = c.reserve_payload(payload, [])
        self.assertLessEqual(len(json.dumps(sent).encode()), c.PM_REQUEST_BYTES)
        self.assertEqual(sent['text'], payload['text'])

    def test_reserve_preserves_valid_bare_payload_above_context_headroom(self):
        payload = {k: v for k, v in self.payload.items() if k != 'persona'}
        payload['text'] = '😀' * 10500
        self.router.validate(payload)
        size = len(json.dumps(payload).encode())
        self.assertGreater(size, c.PM_REQUEST_BYTES)
        self.assertLess(size, 128 * 1024)
        self.assertEqual(c.reserve_payload(payload, []), payload)
        payload['text'] += 'a' * (128 * 1024 - size)
        self.router.validate(payload)
        self.assertEqual(len(json.dumps(payload).encode()), 128 * 1024)
        with self.assertRaisesRegex(c.RouterError, '^chatgpt_payload_too_large$'):
            c.reserve_payload(payload, [])

    def test_invalid_persona_rejected_before_network(self):
        for persona in ([], 42, 'x' * 16001):
            with self.subTest(persona_type=type(persona).__name__), self.assertRaises(c.RouterError):
                self.router.validate({**self.payload, 'persona': persona})


class NativeUsageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'private' / 'usage.sqlite3'
        self.env = patch.dict(os.environ, {'AISIS_CLAUDE_USAGE_DB': str(self.db)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.credential = c.Credential('finance', 'sk-ant-api03-fixture-' + 'f' * 30)
        self.requests = 0
        self.arrived = asyncio.Event()
        self.mode = 'success'
        self.usage = {'input_tokens': 17, 'output_tokens': 5, 'cache_read_input_tokens': 1200,
                      'cache_creation_input_tokens': 0,
                      'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 0}}

        async def endpoint(request):
            self.requests += 1
            self.arrived.set()
            await request.json()
            headers = {'request-id': 'req_fixture', 'anthropic-organization-id': 'org_fixture'}
            if self.mode == 'timeout':
                await asyncio.sleep(.2)
            if self.mode == 'fail_first' and self.requests == 1:
                return web.json_response({'error': {'message': 'private text must never be logged'}},
                                         status=503, headers=headers)
            if self.mode == 'invalid':
                return web.json_response({'content': [], 'stop_reason': 'tool_use', 'usage': self.usage},
                                         headers=headers)
            return web.json_response({'id': 'msg_fixture', 'model': c.MODEL,
                                      'content': [{'type': 'text', 'text': 'Private answer'}],
                                      'stop_reason': 'end_turn', 'usage': self.usage}, headers=headers)

        app = web.Application()
        app.router.add_post('/v1/messages', endpoint)
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.addAsyncCleanup(c.flush_usage)
        self.url = patch.object(c, 'API_URL', str(self.server.make_url('/v1/messages')))
        self.url.start()
        self.addCleanup(self.url.stop)
        self.body = {'model': c.MODEL, 'system': [{'type': 'text', 'text': 'Private stable persona',
                     'cache_control': {'type': 'ephemeral', 'ttl': '5m'}}],
                     'messages': [{'role': 'user', 'content': 'Private question'}], 'max_tokens': 48}

    async def rows(self):
        await c.flush_usage()
        with sqlite3.connect(self.db) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT * FROM provider_requests ORDER BY started_at')]

    async def test_native_answer_keeps_raw_usage_and_records_once_without_content(self):
        reply = await c.anthropic_send(self.credential, self.body, 1, purpose='facts')
        self.assertEqual(reply, 'Private answer')
        self.assertEqual(reply.metadata['usage']['input_tokens'], 17)
        self.assertEqual(reply.metadata['usage']['cache_read_input_tokens'], 1200)
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['purpose'], row['credential_slot'], row['requested_model'],
                          row['response_model'], row['request_id'], row['organization_id']),
                         ('facts', 'finance', c.MODEL, c.MODEL, 'req_fixture', 'org_fixture'))
        self.assertEqual((row['input_tokens'], row['cache_read_input_tokens'], row['cache_creation_input_tokens']),
                         (17, 1200, 0))
        self.assertEqual(row['cache_creation_1h_input_tokens'], 0)
        self.assertEqual(row['outcome'], 'complete')
        saved = json.dumps(rows)
        for private in ('Private question', 'Private stable persona', 'Private answer', self.credential.secret):
            self.assertNotIn(private, saved)
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)

    async def test_missing_and_malformed_usage_remain_null_not_zero(self):
        self.usage = {'input_tokens': True, 'output_tokens': -2, 'cache_read_input_tokens': '0',
                      'cache_creation_input_tokens': 0}
        await c.anthropic_send(self.credential, self.body, 1)
        row = (await self.rows())[0]
        for name in ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
                     'cache_creation_5m_input_tokens', 'cache_creation_1h_input_tokens'):
            self.assertIsNone(row[name])
        self.assertEqual(row['cache_creation_input_tokens'], 0)

    async def test_failover_has_one_row_per_http_attempt_including_unknown_error_usage(self):
        self.mode = 'fail_first'
        state = c.State(self.root / 'router.sqlite3')
        self.addCleanup(state.close)
        router = c.Router(c.Settings(allowed=frozenset({'speaker'})),
                          [self.credential, c.Credential('calendar', 'other-fixture')], state)
        answer = await router.submit(dict(station_id='speaker', turn_id='turn_fixture',
                                          text='Private question', conversation_id=None))
        self.assertEqual(answer['credential_slot'], 'calendar')
        self.assertEqual(self.requests, 2)
        rows = await self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r['credential_slot'] for r in rows], ['finance', 'calendar'])
        self.assertEqual([r['http_status'] for r in rows], [503, 200])
        self.assertEqual([r['outcome'] for r in rows], ['server_unavailable', 'complete'])
        self.assertIsNone(rows[0]['input_tokens'])
        self.assertEqual(rows[0]['turn_id'], 'turn_fixture')
        self.assertEqual(answer['usage']['cache_read_input_tokens'], 1200)

    async def test_invalid_response_still_records_provider_usage(self):
        self.mode = 'invalid'
        with self.assertRaises(c.RouterError):
            await c.anthropic_send(self.credential, self.body, 1)
        row = (await self.rows())[0]
        self.assertEqual(row['input_tokens'], 17)
        self.assertEqual(row['outcome'], 'incomplete_answer')

    async def test_asr_reuses_session_and_preserves_timeout_semantics(self):
        self.mode = 'timeout'
        async with aiohttp.ClientSession(trust_env=False) as session:
            with self.assertRaises(asyncio.TimeoutError):
                await c.anthropic_send(self.credential, self.body, .02, session=session,
                                       purpose='asr', propagate_timeout=True)
            self.assertFalse(session.closed)
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['purpose'], row['outcome']), ('asr', 'transport_timeout'))
        self.assertIsNone(row['output_tokens'])

    async def test_cancelled_attempt_is_recorded_and_never_retried(self):
        self.mode = 'timeout'
        task = asyncio.create_task(c.anthropic_send(self.credential, self.body, 1))
        async with asyncio.timeout(3):
            await self.arrived.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.requests, 1)
        self.assertEqual((await self.rows())[0]['outcome'], 'cancelled')

    async def test_slow_ledger_cannot_expire_a_successful_network_attempt(self):
        state = c.State(self.root / 'router.sqlite3')
        self.addCleanup(state.close)
        router = c.Router(c.Settings(allowed=frozenset({'speaker'})), [self.credential], state)
        original_write = c.write_usage
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        def slow_write(row):
            entered.set()
            release.wait(3)
            original_write(row)
            completed.set()
        with patch.object(c, 'write_usage', side_effect=slow_write):
            try:
                reply = await router.submit(dict(station_id='speaker', turn_id='slow_ledger',
                                                 text='Private question', conversation_id=None))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.assertFalse(completed.is_set(), 'answer must return while the ledger is blocked')
            finally:
                release.set()
                await c.flush_usage()
        self.assertEqual(reply['provider'], 'claude')
        self.assertFalse(state.cooling(self.credential.fingerprint, time.time()))
        self.assertEqual((await self.rows())[0]['outcome'], 'complete')

    async def test_oversize_reserve_fails_before_network_without_uncertain_state(self):
        state = c.State(self.root / 'router.sqlite3')
        self.addCleanup(state.close)
        fallback = AsyncMock()
        router = c.Router(c.Settings(allowed=frozenset({'speaker'})), [], state, fallback=fallback)
        with self.assertRaisesRegex(c.RouterError, '^chatgpt_payload_too_large$'):
            await router.submit(dict(station_id='speaker', turn_id='oversize_reserve',
                                     text='😀' * 11000, conversation_id=None))
        fallback.assert_not_awaited()
        self.assertEqual(state.turn_status('oversize_reserve'), 'failed')

    async def test_unexpected_serialization_error_is_not_a_network_failure(self):
        with self.assertRaises(TypeError):
            await c.anthropic_send(self.credential, {'model': c.MODEL, 'invalid': object()}, 1)
        self.assertEqual((await self.rows())[0]['outcome'], 'router_error')

    async def test_explicit_asr_deadline_preserves_timeout_attribution(self):
        self.mode = 'timeout'
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(.02) as budget:
                await c.anthropic_send(self.credential, self.body, 1, purpose='asr',
                                       propagate_timeout=True, deadline=budget.when())
        self.assertEqual((await self.rows())[0]['outcome'], 'transport_timeout')

    async def test_existing_probe_checks_the_native_cache_block_shape(self):
        send = AsyncMock(return_value='OK')
        with patch.object(c, 'anthropic_send', send):
            await c.probe_keys(c.Settings(allowed=frozenset({'speaker'})), [self.credential])
        self.assertEqual(send.await_args.args[1]['system'], c.stable_system(c.SYSTEM))

    async def test_router_deadline_is_timeout_not_caller_cancellation(self):
        self.mode = 'timeout'
        state = c.State(self.root / 'router.sqlite3')
        self.addCleanup(state.close)
        async def reserve(payload):
            return {'text': 'Reserve answer', 'conversation_id': 'https://chatgpt.com/c/fixture'}
        router = c.Router(c.Settings(allowed=frozenset({'speaker'}), attempt_timeout=.02),
                          [self.credential], state, fallback=reserve)
        client_timeout = aiohttp.ClientTimeout
        with patch.object(aiohttp, 'ClientTimeout', side_effect=lambda **_: client_timeout(total=1)):
            await router.submit(dict(station_id='speaker', turn_id='expired_budget',
                                      text='Private question', conversation_id=None))
        await c.flush_usage()
        self.assertEqual((await self.rows())[0]['outcome'], 'transport_timeout')

    async def test_short_competing_write_does_not_drop_a_successful_attempt(self):
        await c.anthropic_send(self.credential, self.body, 1)
        await c.flush_usage()
        blocker = sqlite3.connect(self.db)
        blocker.execute('BEGIN IMMEDIATE')
        entered = threading.Event()
        original_write = c.write_usage
        def observed_write(row):
            entered.set()
            original_write(row)
        with patch.object(c, 'write_usage', side_effect=observed_write):
            try:
                reply = await c.anthropic_send(self.credential, self.body, 1)
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                await asyncio.sleep(.3)
            finally:
                blocker.rollback()
                blocker.close()
                await c.flush_usage()
        self.assertEqual(reply, 'Private answer')
        self.assertEqual(len(await self.rows()), 2)

    async def test_usage_storage_failure_cannot_lose_a_successful_answer(self):
        with patch.object(c, 'write_usage', side_effect=OSError('fixture disk full')):
            reply = await c.anthropic_send(self.credential, self.body, 1)
            await c.flush_usage()
        self.assertEqual(reply, 'Private answer')
        self.assertEqual(self.requests, 1)
        self.assertEqual(reply.metadata['usage']['input_tokens'], 17)


class DiagnosticTests(unittest.TestCase):
    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            previous_umask = os.umask(0o077)
            try:
                code = c.main(args)
            except SystemExit as exc:
                code = exc.code
            finally:
                os.umask(previous_umask)
        events = [json.loads(line) for line in err.getvalue().splitlines() if line.startswith('{')]
        path = Path(next(event['log'] for event in events if event.get('stage') == 'start'))
        self.addCleanup(path.unlink)
        return code, [json.loads(line) for line in path.read_text().splitlines()]

    def test_invoke_restores_process_umask(self):
        previous = os.umask(0o022)
        try:
            self.invoke(['--help'])
            actual = os.umask(0o022)
            self.assertEqual(actual, 0o022)
        finally:
            os.umask(previous)

    def test_argparse_exit_status_is_recorded_accurately(self):
        for args, expected in [(['--help'], 0), (['--not-a-real-option'], 2)]:
            with self.subTest(args=args):
                code, events = self.invoke(args)
                self.assertEqual(code, expected)
                self.assertEqual(events[-1]['exit'], expected)

    def test_fixed_error_is_persisted_when_adapter_discards_stderr(self):
        with patch.object(c.Settings, 'from_env', side_effect=c.RouterError('fixture_error')):
            code, events = self.invoke(['--check-config'])
        self.assertEqual(code, 1)
        self.assertIn({'error': 'fixture_error', 'automatic_replay': False}, events)

    def test_usage_warning_is_persisted_without_changing_success(self):
        def command(_):
            c.record_usage({})
            return 0
        with patch.object(c, 'command_main', side_effect=command), patch.object(c, 'write_usage', side_effect=OSError('fixture')):
            code, events = self.invoke([])
        self.assertEqual(code, 0)
        self.assertIn({'warning': 'claude_usage_unavailable', 'error_type': 'OSError'}, events)

    def test_string_compatible_answer_can_be_copied_without_losing_metadata(self):
        answer = c.ResponseText('Answer', {'usage': {'input_tokens': 1}})
        for copied in (copy.copy(answer), copy.deepcopy(answer)):
            self.assertEqual(copied, answer)
            self.assertEqual(copied.metadata, answer.metadata)


if __name__ == '__main__':
    unittest.main()
