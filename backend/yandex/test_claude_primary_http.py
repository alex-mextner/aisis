"""Real loopback HTTP + subprocess tests; no external provider or user keys."""
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
import claude_primary as c


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.requests=[];self.mode='success';self.redirect_hits=0
        self.secrets=['sk-ant-api03-test-'+str(n)*30 for n in (1,2,3)]
        async def endpoint(request):
            body=await request.json()
            slot=self.secrets.index(request.headers['x-api-key'])+1
            self.requests.append((slot,body,dict(request.headers)))
            if self.mode=='failover' and slot<3:
                return web.json_response({'error':{'type':'rate_limit_error'}},status=429,headers={'retry-after':'120'})
            if self.mode=='redirect':
                raise web.HTTPFound('/other')
            if self.mode=='huge':return web.Response(body=b'x'*(c.MAX_RESPONSE_BYTES+1))
            if self.mode=='timeout':await asyncio.sleep(.2)
            return web.json_response({'content':[{'type':'text','text':'Ответ из HTTP-стенда'}],'stop_reason':'end_turn'})
        async def other(request):
            self.redirect_hits+=1
            return web.json_response({'content':[{'type':'text','text':'wrong'}],'stop_reason':'end_turn'})
        app=web.Application();app.router.add_post('/v1/messages',endpoint);app.router.add_get('/other',other)
        self.server=TestServer(app);await self.server.start_server();self.addAsyncCleanup(self.server.close)
        self.url=str(self.server.make_url('/v1/messages'))
        self.state=c.State(self.root/'router.sqlite');self.addCleanup(self.state.close)
    def creds(self):return [c.Credential(f'slot{i}',s) for i,s in enumerate(self.secrets,1)]
    async def test_real_http_order_auth_and_complete_response(self):
        self.mode='failover'
        async def forbidden(_):self.fail('PM must not run after successful third key')
        router=c.Router(c.Settings(allowed=frozenset({'a'})),self.creds(),self.state,fallback=forbidden)
        with patch.object(c,'API_URL',self.url):
            result=await router.submit({'station_id':'a','turn_id':'real-http','text':'Тест','conversation_id':None})
        self.assertEqual(result['credential_slot'],'slot3')
        self.assertEqual([r[0] for r in self.requests],[1,2,3])
        self.assertTrue(all(r[2]['anthropic-version']=='2023-06-01' for r in self.requests))
        self.assertEqual(self.state.turn_status('real-http'),'complete')
    async def test_redirect_never_forwards_api_key(self):
        self.mode='redirect'
        with patch.object(c,'API_URL',self.url),self.assertRaises(c.RouterError):
            await c.anthropic_send(self.creds()[0],{'messages':[]},1)
        self.assertEqual(self.redirect_hits,0);self.assertEqual(len(self.requests),1)
    async def test_oversize_reply_is_bounded(self):
        self.mode='huge'
        with patch.object(c,'API_URL',self.url),self.assertRaisesRegex(c.RouterError,'provider_response_too_large'):
            await c.anthropic_send(self.creds()[0],{'messages':[]},1)
    async def test_network_timeout_is_single_attempt(self):
        self.mode='timeout'
        with patch.object(c,'API_URL',self.url),self.assertRaises(c.Failover):
            await c.anthropic_send(self.creds()[0],{'messages':[]},.03)
        self.assertEqual(len(self.requests),1)
    async def test_actual_json_subprocess_contract(self):
        sources=[]
        for i,secret in enumerate(self.secrets,1):
            p=self.root/f'bot{i}.env';p.write_text('CLAUDE_API_TOKEN='+secret+'\n');p.chmod(0o600)
            sources.append({'label':f'slot{i}','path':str(p),'variables':['CLAUDE_API_TOKEN']})
        sf=self.root/'sources.json';sf.write_text(json.dumps(sources));sf.chmod(0o600)
        env={**os.environ,'AISIS_ALLOWED_STATIONS':'a','AISIS_CHATGPT_TIMEOUT':'320',
             'AISIS_CLAUDE_KEY_SOURCES':str(sf),'AISIS_CLAUDE_STATE_DIR':str(self.root/'child')}
        # Override only inside this controlled test process, not via any deployed
        # environment option. Production has a fixed official HTTPS destination.
        script=f'import claude_primary as c;c.API_URL={self.url!r};raise SystemExit(c.main())'
        proc=await asyncio.create_subprocess_exec(sys.executable,'-c',script,cwd=str(Path(c.__file__).parent),env=env,
                    stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        payload={'station_id':'a','turn_id':'child','text':'hello','conversation_id':None}
        out,err=await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()),5)
        self.assertEqual(proc.returncode,0,err.decode())
        answer=json.loads(out);self.assertEqual(answer['provider'],'claude')
        self.assertEqual(answer['text'],'Ответ из HTTP-стенда');self.assertEqual(err,b'')
        for secret in self.secrets:self.assertNotIn(secret.encode(),out+err)
        self.assertEqual(len(self.requests),1)
    async def test_check_config_makes_no_http_calls(self):
        sources=[]
        for i,secret in enumerate(self.secrets,1):
            p=self.root/f'check{i}.env';p.write_text('CLAUDE_API_TOKEN='+secret);p.chmod(0o600)
            sources.append({'label':f'slot{i}','path':str(p),'variables':['CLAUDE_API_TOKEN']})
        sf=self.root/'check.json';sf.write_text(json.dumps(sources))
        env={**os.environ,'AISIS_ALLOWED_STATIONS':'a','AISIS_CHATGPT_TIMEOUT':'320','AISIS_CLAUDE_KEY_SOURCES':str(sf)}
        proc=await asyncio.create_subprocess_exec(sys.executable,str(Path(c.__file__)),'--check-config',env=env,
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await proc.communicate()
        self.assertEqual(proc.returncode,0,err.decode());self.assertTrue(json.loads(out)['ready'])
        self.assertEqual(self.requests,[])
    async def check_config(self,rows):
        sources=[]
        for label,variable,text in rows:
            p=self.root/(label+'.env');p.write_text(text);p.chmod(0o600)
            sources.append({'label':label,'path':str(p),'variables':[variable]})
        sf=self.root/'console.json';sf.write_text(json.dumps(sources))
        env={**os.environ,'AISIS_ALLOWED_STATIONS':'a','AISIS_CHATGPT_TIMEOUT':'320','AISIS_CLAUDE_KEY_SOURCES':str(sf)}
        proc=await asyncio.create_subprocess_exec(sys.executable,str(Path(c.__file__)),'--check-config',env=env,
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await proc.communicate()
        return proc.returncode,json.loads(out),err
    async def test_check_config_ready_for_console_keys_with_calendar_token2(self):
        user=['sk-ant-usr-'+'fixture'+str(n)*30 for n in (1,2,3)]
        shared=f'CLAUDE_API_TOKEN={user[0]}\nCLAUDE_API_TOKEN2={user[1]}\n'
        code,result,err=await self.check_config([('finance','CLAUDE_API_TOKEN',shared),
            ('calendar','CLAUDE_API_TOKEN2',shared),('report','CLAUDE_API_KEY',f'CLAUDE_API_KEY={user[2]}\n')])
        self.assertEqual((code,result['distinct_keys'],result['ready'],result['slots']),
                         (0,3,True,['finance','calendar','report']),err.decode())
        self.assertNotIn(user[0].encode(),err)
    async def test_check_config_reports_duplicate_calendar_token_as_not_ready(self):
        user=['sk-ant-usr-'+'fixture'+str(n)*30 for n in (1,3)]
        shared=f'CLAUDE_API_TOKEN={user[0]}\n'
        code,result,_=await self.check_config([('finance','CLAUDE_API_TOKEN',shared),
            ('calendar','CLAUDE_API_TOKEN',shared),('report','CLAUDE_API_KEY',f'CLAUDE_API_KEY={user[1]}\n')])
        self.assertEqual((code,result['distinct_keys'],result['ready']),(2,2,False))


if __name__=='__main__':unittest.main()
