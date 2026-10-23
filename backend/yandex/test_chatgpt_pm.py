import os
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer
from chatgpt_pm import run,http_timeout


class BridgeClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls=[];self.status=200;self.error='guardian_stopped'
        async def turn(request):
            self.calls.append((request.headers.get('Authorization'),await request.json()))
            if self.status!=200:return web.json_response({'error':self.error},status=self.status)
            return web.json_response({'text':'pm reply','conversation_id':'https://chatgpt.com/c/owned'})
        app=web.Application();app.router.add_post('/turn',turn)
        self.server=TestServer(app);await self.server.start_server()
        self.env=patch.dict(os.environ,{'AISIS_PM_URL':str(self.server.make_url('/turn')),'AISIS_PM_TOKEN':'x'*32})
        self.env.start()
    async def asyncTearDown(self):
        self.env.stop();await self.server.close()
    async def test_exact_station_turn_is_forwarded_without_client_history(self):
        payload={'station_id':'a','text':'question','turn_id':'one','conversation_id':None}
        result=await run(payload)
        self.assertEqual(result,{'text':'pm reply','conversation_id':'https://chatgpt.com/c/owned'})
        self.assertEqual(self.calls,[('Bearer '+'x'*32,payload)])
    async def test_failure_is_not_retried_and_only_short_error_code_is_used(self):
        self.status=503
        with self.assertRaisesRegex(RuntimeError,'^pm_bridge_guardian_stopped$'):
            await run({'station_id':'a','text':'question','turn_id':'one'})
        self.assertEqual(len(self.calls),1)
    async def test_arbitrary_response_text_is_not_in_error(self):
        self.status=503;self.error='private request text must never be logged'
        with self.assertRaisesRegex(RuntimeError,'^pm_bridge_http_503$'):
            await run({'station_id':'a','text':'question','turn_id':'one'})
    async def test_paid_remote_or_missing_pm_endpoint_is_refused(self):
        for endpoint in ('https://api.openai.com/v1/chat/completions','http://example.com/turn',''):
            with patch.dict(os.environ,{'AISIS_PM_URL':endpoint}):
                with self.assertRaisesRegex(RuntimeError,'local_pm_bridge_required'):await run({})
        self.assertEqual(self.calls,[])
    async def test_legacy_bridge_env_is_never_used(self):
        with patch.dict(os.environ,{'AISIS_PM_URL':'','AISIS_CHATGPT_WEB_URL':str(self.server.make_url('/turn'))}):
            with self.assertRaisesRegex(RuntimeError,'local_pm_bridge_required'):await run({})
        self.assertEqual(self.calls,[])

class TimeoutTests(unittest.TestCase):
    def test_http_budget_is_separate_from_outer_adapter_budget(self):
        with patch.dict(os.environ,{'AISIS_CHATGPT_TIMEOUT':'250'},clear=True):
            self.assertEqual(http_timeout(),240)
    def test_invalid_http_budgets_fail_before_request(self):
        for value in ('nan','inf','0','-1','301'):
            with patch.dict(os.environ,{'AISIS_PM_HTTP_TIMEOUT':value}):
                with self.assertRaises(ValueError):http_timeout()
