"""Persistent prompt ownership and account safety regression tests."""
import asyncio
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest

try:
    import pm_cdp_server as server
except ModuleNotFoundError:
    server = None

class PromptServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(server, 'persistent pm CDP server must exist')
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = server.Ledger(Path(self.tmp.name)/'pm.sqlite')
        self.driver = FakeDriver()
        self.service = server.PromptService(self.driver, self.ledger, {'a', 'b'})
    async def asyncTearDown(self):
        if hasattr(self, 'service'): await self.service.drain()
        if hasattr(self, 'tmp'): self.tmp.cleanup()
    def request(self, turn='one', station='a', text='hello', conversation=None):
        return dict(turn_id=turn,station_id=station,text=text,conversation_id=conversation)
    async def test_busy_request_is_rejected_without_deferred_send(self):
        self.driver.gate=asyncio.Event()
        first=asyncio.create_task(self.service.submit(self.request()))
        await self.driver.started.wait()
        try:
            with self.assertRaises(server.Refusal):
                await asyncio.wait_for(self.service.submit(self.request('two','b')),0.05)
        finally:
            self.driver.gate.set()
            await first
            await self.service.drain()
        self.assertEqual(self.driver.calls,1)
        self.assertIsNone(self.ledger.turn_status('two'))

    async def test_disconnected_driver_is_unready_and_never_admits_work(self):
        self.driver.ready=False
        with self.assertRaises(server.Refusal): await self.service.submit(self.request())
        self.assertEqual(self.driver.calls,0)
        self.assertFalse(self.service.ready)

    async def test_duplicate_turn_never_sends_twice(self):
        result=await self.service.submit(self.request())
        self.assertEqual(result['text'],'reply')
        with self.assertRaises(server.Refusal): await self.service.submit(self.request())
        self.assertEqual(self.driver.calls,1)
    async def test_station_continuity_is_durable_and_separate(self):
        a=await self.service.submit(self.request())
        b=await self.service.submit(self.request('two','b'))
        await self.service.submit(self.request('three','a',conversation=a['conversation_id']))
        self.assertNotEqual(a['conversation_id'],b['conversation_id'])
        self.assertEqual(self.driver.previous[-1]['url'],a['conversation_id'])
        self.assertNotIn(b'hello',Path(self.tmp.name,'pm.sqlite').read_bytes())
    async def test_cross_station_conversation_rejected_before_browser(self):
        a=await self.service.submit(self.request())
        with self.assertRaises(server.Refusal):
            await self.service.submit(self.request('two','b',conversation=a['conversation_id']))
        self.assertEqual(self.driver.calls,1)
    async def test_target_cannot_be_assigned_to_two_stations(self):
        self.ledger.remember('a','same-target','https://chatgpt.com/c/a')
        with self.assertRaises(server.Refusal):
            self.ledger.remember('b','same-target','https://chatgpt.com/c/a')

    async def test_unknown_station_rejected_before_browser(self):
        with self.assertRaises(server.Refusal): await self.service.submit(self.request(station='intruder'))
        self.assertEqual(self.driver.calls,0)
    async def test_cancelled_http_caller_does_not_cancel_browser_work(self):
        self.driver.gate=asyncio.Event()
        caller=asyncio.create_task(self.service.submit(self.request()))
        await self.driver.started.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError): await caller
        self.assertFalse(self.driver.cancelled)
        self.driver.gate.set()
        await self.service.drain()
        self.assertEqual(self.ledger.turn_status('one'),'complete')
        self.assertEqual(self.driver.calls,1)
    async def test_restart_blocks_uncertain_station_without_replay(self):
        self.ledger.reserve(self.request())
        self.ledger.mark('one','dispatching')
        restarted=server.Ledger(Path(self.tmp.name)/'pm.sqlite')
        service=server.PromptService(self.driver,restarted,{'a'})
        with self.assertRaises(server.Refusal): await service.submit(self.request('next'))
        self.assertEqual(self.driver.calls,0)
    async def test_submission_failure_remains_uncertain(self):
        self.driver.fail=True
        with self.assertRaises(server.Refusal): await self.service.submit(self.request())
        with self.assertRaises(server.Refusal): await self.service.submit(self.request('next'))
        self.assertEqual(self.driver.calls,1)

class DisconnectLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_is_a_failure_for_systemd_restart(self):
        stop=asyncio.Event();disconnected=asyncio.Event();disconnected.set()
        with self.assertRaises(server.Refusal) as caught:
            await server.wait_for_stop_or_disconnect(stop,disconnected)
        self.assertEqual(caught.exception.code,'browser_disconnected')
    async def test_operator_stop_is_normal_shutdown(self):
        stop=asyncio.Event();disconnected=asyncio.Event();stop.set()
        await server.wait_for_stop_or_disconnect(stop,disconnected)

class BrowserBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_owned_tab_is_created_in_background(self):
        class Page:
            def is_closed(self):return False
        page=Page();calls=[]
        class Context:pages=[]
        context=Context()
        class Session:
            async def send(self,method,params):
                calls.append((method,params));context.pages.append(page);return {'targetId':'new-background'}
            async def detach(self):pass
        class Browser:
            async def new_browser_cdp_session(self):return Session()
        driver=server.CDPDriver.__new__(server.CDPDriver)
        driver.context=context;driver.browser=Browser()
        async def target(p):return 'new-background'
        driver.target=target
        found,identity=await driver.new_background_page()
        self.assertIs(found,page)
        self.assertEqual(identity,'new-background')
        self.assertEqual(calls,[('Target.createTarget',{'url':'about:blank','background':True})])

    @unittest.skipUnless(shutil.which('node'),'Node is required for the injected JS boundary')
    async def test_unrelated_url_is_refused_before_any_dom_read(self):
        script=server.DOM.replace('/*@conversation-root*/','const all=()=>{throw new Error("unrelated_dom_read")};')
        source='global.location={origin:"https://chatgpt.com",pathname:"/c/unrelated",href:"https://chatgpt.com/c/unrelated"};try{('+script+')({expected_chat:"https://chatgpt.com/c/owned"});process.exit(2)}catch(e){if(!String(e).includes("AISIS_OWNED_URL_CHANGED")){process.stderr.write(String(e));process.exit(1)}}'
        result=subprocess.run(['node','-e',source],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    async def test_snapshot_supplies_dom_argument_object(self):
        class Page:
            async def evaluate(self,script,argument):
                if not isinstance(argument,dict): raise TypeError('destructured DOM argument must be object')
                return {'chat':'https://chatgpt.com/','turns':[]}
        driver=server.CDPDriver.__new__(server.CDPDriver)
        driver.script='({action="snapshot"})=>({})'
        snapshot=await driver.snapshot(Page())
        self.assertEqual(snapshot['turns'],[])

class RestartRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def driver(self,pages):
        class Page:
            def __init__(self,target,url): self.identity=target;self.url=url;self.navigations=[]
            def is_closed(self): return False
            async def goto(self,url,**kwargs): self.navigations.append(url);self.url=url
        class Context:
            def __init__(self): self.pages=[Page(*p) for p in pages];self.created=0
            async def new_page(self):
                self.created+=1;p=Page('created-'+str(self.created),'about:blank');self.pages.append(p);return p
        driver=server.CDPDriver.__new__(server.CDPDriver)
        driver.context=Context();driver.max_tabs=6;driver.initial_target='';driver.initial_used=False
        async def target(page):return page.identity
        driver.target=target
        async def background():
            p=await driver.context.new_page();return p,p.identity
        driver.new_background_page=background
        return driver
    async def test_stale_target_recovers_exact_owned_url_without_navigation(self):
        driver=self.driver([('fresh','https://chatgpt.com/c/owned'),('unrelated','https://chatgpt.com/c/other')])
        remembered=[]
        page,target=await driver.page_for({'target':'stale','url':'https://chatgpt.com/c/owned'},lambda *a:remembered.append(a))
        self.assertEqual(target,'fresh');self.assertEqual(page.navigations,[])
        self.assertEqual(remembered,[('fresh','https://chatgpt.com/c/owned')])
    async def test_missing_owned_url_restores_only_that_url(self):
        driver=self.driver([('unrelated','https://chatgpt.com/c/other')])
        remembered=[]
        page,target=await driver.page_for({'target':'stale','url':'https://chatgpt.com/c/owned'},lambda *a:remembered.append(a))
        self.assertEqual(page.navigations,['https://chatgpt.com/c/owned'])
        self.assertEqual(driver.context.pages[0].navigations,[])
        self.assertEqual(remembered,[('created-1','https://chatgpt.com/c/owned')])
    async def test_duplicate_owned_urls_are_ambiguous(self):
        driver=self.driver([('one','https://chatgpt.com/c/owned'),('two','https://chatgpt.com/c/owned')])
        with self.assertRaises(server.Refusal):
            await driver.page_for({'target':'stale','url':'https://chatgpt.com/c/owned'},lambda *a:None)
        self.assertEqual(driver.context.created,0)
    async def test_existing_target_navigation_is_not_overwritten(self):
        driver=self.driver([('alive','https://chatgpt.com/c/human-navigation')])
        with self.assertRaises(server.Refusal):
            await driver.page_for({'target':'alive','url':'https://chatgpt.com/c/owned'},lambda *a:None)
        self.assertEqual(driver.context.pages[0].navigations,[])
    async def test_missing_homepage_has_no_permanent_conversation_to_restore(self):
        driver=self.driver([])
        with self.assertRaises(server.Refusal):
            await driver.page_for({'target':'stale','url':'https://chatgpt.com/'},lambda *a:None)
        self.assertEqual(driver.context.created,0)

@unittest.skipUnless(shutil.which('node'),'Node is required for the injected JS boundary')
class BackgroundMenuTests(unittest.TestCase):
    def fixture(self,variant='normal',full=False):
        shared="const all=(s,r=document)=>r.querySelectorAll(s);const visible=e=>!!e&&e.isConnected&&e.getClientRects().length>0;const rootSelector='ROOT';const conversationRoot=()=>({root:null,error:'conversation-id-missing',match:''});const thinkingFailure=()=>({count:0});"
        script=server.DOM.replace('/*@conversation-root*/',shared)
        setup=r"""
global.performance={timeOrigin:Date.now(),now:()=>0};global.location={origin:'https://chatgpt.com',pathname:'/',href:'https://chatgpt.com/'};
let clicks=0,fullClicks=0,menuOpen=false;
const element=(text='',attrs={})=>({textContent:text,isConnected:true,disabled:false,getClientRects:()=>[{}],getBoundingClientRect:()=>({x:1,y:1,width:20,height:20}),getAttribute:k=>attrs[k]||null,contains:n=>n===child});
const child={};const trigger=element('Thinking',{'aria-label':'Select ChatGPT model'});trigger.click=()=>{clicks++;menuOpen=true};
const composer=element(''),profile=element(''),full=element('Select model',{'aria-label':'Select model'});full.click=()=>fullClicks++;
const menu=element('');menu.querySelectorAll=s=>s==='[role="menuitem"]'?[full]:[];
global.getComputedStyle=e=>({pointerEvents:'auto'});
global.document={title:'ChatGPT',elementFromPoint:()=>child,querySelectorAll:s=>{
 if(s.includes('data-composer-markdown'))return [composer];
 if(s.includes('data-codex-intelligence-trigger'))return [trigger];
 if(s==='button[aria-label="Open profile menu"]')return [profile];
 if(s==='[role="menu"]')return menuOpen?[menu]:[];
 if(s.includes('[role="menu"]')&&menuOpen)return [menu];
 return [];
}};
"""
        source=setup+'const run=('+script+');const expected=run({expected_chat:"https://chatgpt.com/"});'
        if variant=='draft':source+='composer.textContent="human draft";'
        if variant=='disabled':source+='trigger.disabled=true;'
        if variant=='occluded':source+='document.elementFromPoint=()=>({});'
        source+='const result=run({action:"open-model-menu",expected,expected_chat:"https://chatgpt.com/",deadline:Date.now()+3000});'
        if full:
            if variant=='unknown_submenu':source+='full.textContent="Choose another option";full.getAttribute=()=>null;'
            source+='const fullResult=run({action:"open-full-model-menu",expected,expected_chat:"https://chatgpt.com/",deadline:Date.now()+3000});'
        source+='console.log(JSON.stringify({result,clicks,fullClicks'+(',fullResult' if full else '')+'}));'
        result=subprocess.run(['node','-e',source],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)
    def test_observed_background_trigger_uses_one_guarded_native_click(self):
        self.assertEqual(self.fixture(),{'result':True,'clicks':1,'fullClicks':0})
    def test_changed_draft_blocks_native_menu_click(self):
        self.assertEqual(self.fixture('draft')['clicks'],0)
    def test_disabled_trigger_blocks_native_menu_click(self):
        self.assertEqual(self.fixture('disabled')['clicks'],0)
    def test_occluded_trigger_is_never_clicked(self):
        self.assertEqual(self.fixture('occluded')['clicks'],0)
    def test_exact_select_model_submenu_only_opens_full_model_panel(self):
        result=self.fixture(full=True)
        self.assertTrue(result['fullResult']);self.assertEqual(result['fullClicks'],1)
    def test_unrecognized_submenu_is_never_clicked(self):
        result=self.fixture('unknown_submenu',full=True)
        self.assertFalse(result['fullResult']);self.assertEqual(result['fullClicks'],0)

class SafeLoggingTests(unittest.TestCase):
    def test_only_short_error_code_is_logged(self):
        with self.assertLogs('aisis.pm',level='WARNING') as captured:
            server.log_refusal('conversation_changed',503)
            server.log_refusal('secret request text\nshould never be logged',503)
        text=' '.join(captured.output)
        self.assertIn('code=conversation_changed',text)
        self.assertIn('code=internal_error',text)
        self.assertNotIn('secret request',text)

class LocalRootIdentityTests(unittest.TestCase):
    def snapshot(self):
        return {'root_match':'local-chatgpt','chat':'https://chatgpt.com/c/owned','turns':[{'id':'anchor','user':'verified earlier prompt','assistant':'reply','state':'complete'}]}
    def session(self):
        import hashlib
        return {'url':'https://chatgpt.com/c/owned','anchor_turn':'anchor','anchor_digest':hashlib.sha256(b'verified earlier prompt').hexdigest()}
    def test_observed_local_root_requires_owned_prior_turn_and_prompt_digest(self):
        server.validate_owned_root(self.snapshot(),self.session())
    def test_unbound_local_root_is_refused(self):
        with self.assertRaises(server.Refusal):server.validate_owned_root(self.snapshot(),None)
    def test_local_root_with_wrong_prompt_is_refused(self):
        snapshot=self.snapshot();snapshot['turns'][0]['user']='another conversation'
        with self.assertRaises(server.Refusal):server.validate_owned_root(snapshot,self.session())
    def test_anchor_survives_target_only_session_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger=server.Ledger(Path(tmp)/'pm.sqlite')
            ledger.remember('a','target','https://chatgpt.com/c/owned','anchor','digest')
            ledger.remember('a','fresh-target','https://chatgpt.com/c/owned')
            self.assertEqual(ledger.session('a')['anchor_turn'],'anchor')
            self.assertEqual(ledger.session('a')['anchor_digest'],'digest')

class ReplyIdentityTests(unittest.TestCase):
    def setUp(self): self.assertIsNotNone(server,'persistent pm CDP server must exist')
    def test_accepts_only_exact_prompt_in_new_group(self):
        base=[{'id':'old','user':'earlier','assistant':'old reply','state':'complete'}]
        now=base+[{'id':'new','user':'hello','assistant':'answer','state':'complete'}]
        self.assertEqual(server.match_reply(base,now,'hello'),('new','answer'))
        now[-1]['user']='someone else'
        with self.assertRaises(server.Refusal): server.match_reply(base,now,'hello')
    def test_reordered_or_replaced_baseline_refused(self):
        base=[{'id':'old','user':'earlier','assistant':'old reply','state':'complete'}]
        now=[{'id':'changed','user':'earlier','assistant':'old reply','state':'complete'}]
        with self.assertRaises(server.Refusal): server.match_reply(base,now,'hello')
    def test_changed_assistant_generation_in_existing_history_is_refused(self):
        base=[{'id':'old','user':'earlier','assistant':'reply','assistant_id':'generation-one','state':'complete'}]
        now=[{**base[0],'assistant_id':'generation-two'}]
        with self.assertRaises(server.Refusal):server.match_reply(base,now,'hello')

    def test_running_answer_cannot_be_complete(self):
        self.assertIsNone(server.match_reply([],[{'id':'new','user':'hello','assistant':'partial','state':'in_progress'}],'hello'))

class FakeDriver:
    def __init__(self):
        self.ready=True; self.calls=0; self.previous=[]; self.started=asyncio.Event(); self.gate=None; self.cancelled=False; self.fail=False
    async def ask(self,payload,session,remember,dispatching):
        self.calls+=1; self.previous.append(session); self.started.set()
        try:
            if self.gate: await self.gate.wait()
            url=(session or {}).get('url') or 'https://chatgpt.com/c/'+payload['station_id']
            remember('target-'+payload['station_id'],url)
            dispatching()
            if self.fail: raise server.Refusal('ambiguous_send')
            return {'text':'reply','conversation_id':url}
        except asyncio.CancelledError:
            self.cancelled=True; raise


@unittest.skipUnless(shutil.which('node'),'Node is required for the injected JS boundary')
class SendReadinessTests(unittest.TestCase):
    def fixture(self,variant='normal'):
        shared="const all=(s,r=document)=>r.querySelectorAll(s);const visible=e=>!!e&&e.isConnected&&e.getClientRects().length>0;const rootSelector='ROOT';const conversationRoot=()=>({root:null,error:'conversation-id-missing',match:''});const thinkingFailure=()=>({count:0});"
        script=server.DOM.replace('/*@conversation-root*/',shared)
        setup=r"""
global.performance={timeOrigin:Date.now(),now:()=>0};global.location={origin:'https://chatgpt.com',pathname:'/',href:'https://chatgpt.com/'};let clicks=0;
const element=(text='',attrs={})=>({textContent:text,isConnected:true,disabled:false,getClientRects:()=>[{}],getAttribute:k=>attrs[k]||null});
const trigger=element('Thinking',{'aria-label':'Select ChatGPT model'}),composer=element(''),profile=element(''),send=element('');send.click=()=>clicks++;
global.document={title:'ChatGPT',querySelectorAll:s=>{if(s.includes('data-composer-markdown'))return [composer];if(s.includes('data-codex-intelligence-trigger'))return [trigger];if(s==='button[aria-label="Open profile menu"]')return [profile];if(s==='button[aria-label="Send"]')return [send];return []}};
"""
        source=setup+'const run=('+script+');const expected=run({expected_chat:"https://chatgpt.com/"});const probe=run({action:"probe",expected:{model:"Astra",effort:""}});const prepared={ok:true,url:location.href,composer,trigger,root:null,baseline:expected};composer.textContent="pending";'
        if variant=='disabled':source+='send.disabled=true;'
        if variant=='changed_composer':source+='prepared.composer={};'
        if variant=='changed_model':source+='trigger.textContent="Pro";'
        source+='const args={action:"send-status",prompt:"pending",probe,prepared,expected_chat:"https://chatgpt.com/",deadline:Date.now()+3000};const first=run(args);const initialClicks=clicks;send.disabled=false;const second=run(args);const sent=run({...args,action:"send"});console.log(JSON.stringify({first,second,initialClicks,clicks,sent}));'
        result=subprocess.run(['node','-e',source],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)
    def test_disabled_readiness_wait_is_read_only_then_final_send_rechecks(self):
        result=self.fixture('disabled')
        self.assertEqual(result['first'],{'ready':False,'reason':'send_disabled'})
        self.assertEqual(result['initialClicks'],0)
        self.assertEqual(result['second'],{'ready':True,'reason':'ready'})
        self.assertTrue(result['sent']);self.assertEqual(result['clicks'],1)
    def test_prepared_composer_change_is_never_retried_or_clicked(self):
        result=self.fixture('changed_composer')
        self.assertEqual(result['first'],{'ready':False,'reason':'composer_identity_changed'})
        self.assertEqual(result['sent'].get('clicked'),False);self.assertEqual(result['clicks'],0)
    def test_model_signature_change_is_never_clicked(self):
        result=self.fixture('changed_model')
        self.assertEqual(result['first'],{'ready':False,'reason':'model_evidence_changed'})
        self.assertEqual(result['sent'].get('clicked'),False);self.assertEqual(result['clicks'],0)

class SendReadinessPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_only_reads_until_enabled(self):
        calls=[]
        class Page:
            async def evaluate(self,script,payload):
                calls.append(payload['action'])
                return {'ready':len(calls)>1,'reason':'ready' if len(calls)>1 else 'send_disabled'}
        driver=server.CDPDriver.__new__(server.CDPDriver);driver.script='unused'
        await driver.wait_send_ready(Page(),'pending','https://chatgpt.com/c/owned',None,None)
        self.assertEqual(calls,['send-status','send-status'])
    async def test_identity_change_stops_without_retry(self):
        calls=[]
        class Page:
            async def evaluate(self,script,payload):
                calls.append(payload['action']);return {'ready':False,'reason':'composer_identity_changed'}
        driver=server.CDPDriver.__new__(server.CDPDriver);driver.script='unused'
        with self.assertRaises(server.Refusal) as caught:
            await driver.wait_send_ready(Page(),'pending','https://chatgpt.com/c/owned',None,None)
        self.assertEqual(caught.exception.code,'send_not_ready_composer_identity_changed')
        self.assertEqual(calls,['send-status'])

class HomepageRepairTests(unittest.IsolatedAsyncioTestCase):
    driver=RestartRecoveryTests.driver
    async def test_owned_homepage_target_wins_over_ordinary_homepage_tabs(self):
        driver=self.driver([('owned','https://chatgpt.com/'),('human','https://chatgpt.com/')])
        page,target=await driver.page_for({'target':'owned','url':'https://chatgpt.com/'},lambda *a,**kw:None)
        self.assertEqual(target,'owned');self.assertEqual(driver.context.created,0)
        self.assertEqual(page.navigations,[])
    async def test_lost_homepage_can_recreate_only_with_no_dispatch_proof(self):
        driver=self.driver([('human','https://chatgpt.com/')])
        records=[]
        page,target=await driver.page_for({'target':'lost','url':'https://chatgpt.com/','can_recreate_homepage':True},lambda *a,**kw:records.append((a,kw)))
        self.assertEqual(target,'created-1')
        self.assertEqual(page.navigations,['https://chatgpt.com/'])
        self.assertEqual(driver.context.pages[0].navigations,[])
    async def test_proven_failed_navigation_retries_only_the_owned_blank_target(self):
        driver=self.driver([('owned','about:blank')])
        page,target=await driver.page_for({'target':'owned','url':'https://chatgpt.com/','navigation_state':'failed','can_recreate_homepage':True},lambda *a,**kw:None)
        self.assertEqual(target,'owned');self.assertEqual(driver.context.created,0)
        self.assertEqual(page.navigations,['https://chatgpt.com/'])
    async def test_no_dispatch_proof_does_not_overwrite_human_navigation(self):
        driver=self.driver([('owned','https://chatgpt.com/c/human')])
        with self.assertRaises(server.Refusal):
            await driver.page_for({'target':'owned','url':'https://chatgpt.com/','navigation_state':'failed','can_recreate_homepage':True},lambda *a,**kw:None)
        self.assertEqual(driver.context.pages[0].navigations,[])
    async def test_navigation_failure_persists_explicit_failed_state(self):
        driver=self.driver([]);records=[]
        async def fail(*a,**kw):raise RuntimeError('navigation failure')
        original=driver.new_background_page
        async def background():
            page,target=await original();page.goto=fail;return page,target
        driver.new_background_page=background
        with self.assertRaises(server.Refusal):
            await driver.page_for(None,lambda *a,**kw:records.append((a,kw)))
        self.assertEqual(records[-1][1],{'navigation_state':'failed'})

class DeadlineTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=PromptServiceTests.asyncSetUp
    asyncTearDown=PromptServiceTests.asyncTearDown
    request=PromptServiceTests.request
    async def test_total_timeout_before_dispatch_is_failed_without_stop(self):
        self.service.request_timeout=.015;self.driver.gate=asyncio.Event()
        with self.assertRaises(server.Refusal) as caught:await self.service.submit(self.request())
        self.assertEqual(caught.exception.code,'request_timeout')
        self.assertEqual(self.ledger.turn_status('one'),'failed')
    async def test_total_timeout_after_dispatch_blocks_replay_and_stops_owned_chat(self):
        self.service.request_timeout=.015;stops=[]
        async def ask(payload,session,remember,dispatching):
            remember('owned','https://chatgpt.com/c/a');dispatching();await asyncio.Event().wait()
        async def stop(session):stops.append(session['url'])
        self.driver.ask=ask;self.driver.stop_uncertain=stop
        with self.assertRaises(server.Refusal) as caught:await self.service.submit(self.request())
        self.assertEqual(caught.exception.code,'request_timeout')
        self.assertEqual(self.ledger.turn_status('one'),'uncertain')
        self.assertEqual(stops,['https://chatgpt.com/c/a'])
        with self.assertRaises(server.Refusal):await self.service.submit(self.request('next'))
    async def test_inner_timeout_before_dispatch_is_failed_without_deadline_stop(self):
        stops=[]
        async def ask(payload,session,remember,dispatching):
            await asyncio.wait_for(asyncio.Event().wait(),timeout=.005)
        async def stop(session):stops.append(session)
        self.driver.ask=ask;self.driver.stop_uncertain=stop
        with self.assertRaises(TimeoutError):await self.service.submit(self.request())
        self.assertEqual(self.ledger.turn_status('one'),'failed')
        self.assertEqual(stops,[])
    async def test_inner_timeout_after_dispatch_is_uncertain_without_deadline_stop(self):
        stops=[]
        async def ask(payload,session,remember,dispatching):
            remember('owned','https://chatgpt.com/c/a');dispatching()
            await asyncio.wait_for(asyncio.Event().wait(),timeout=.005)
        async def stop(session):stops.append(session)
        self.driver.ask=ask;self.driver.stop_uncertain=stop
        with self.assertRaises(TimeoutError):await self.service.submit(self.request())
        self.assertEqual(self.ledger.turn_status('one'),'uncertain')
        self.assertEqual(stops,[])
        with self.assertRaises(server.Refusal):await self.service.submit(self.request('next'))
    async def test_no_dispatch_history_proof_rejects_every_dispatched_state(self):
        self.ledger.reserve(self.request())
        self.assertTrue(self.ledger.can_recreate_homepage('a'))
        for state in ('dispatching','uncertain','complete'):
            self.ledger.mark('one',state)
            self.assertFalse(self.ledger.can_recreate_homepage('a'))
        self.ledger.mark('one','failed')
        self.assertTrue(self.ledger.can_recreate_homepage('a'))
    async def test_navigation_failure_marker_is_durable(self):
        self.ledger.remember('a','target','https://chatgpt.com/',navigation_state='failed')
        self.assertEqual(self.ledger.session('a')['navigation_state'],'failed')
        self.ledger.remember('a','target','https://chatgpt.com/',navigation_state='ready')
        self.assertEqual(self.ledger.session('a')['navigation_state'],'ready')

class TimeoutControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_stop_is_scoped_and_preserves_existing_controls(self):
        from contextlib import asynccontextmanager
        import sqlite3
        db=sqlite3.connect(':memory:');db.execute('CREATE TABLE controls(key TEXT PRIMARY KEY,value TEXT)')
        controls={'stop:*':'transport HTTP 429; manual resume required','astra_quota':'blocked','stop:https://chatgpt.com/c/other':'manual','watch:https://chatgpt.com/c/owned':'0'}
        db.executemany('INSERT INTO controls VALUES(?,?)',controls.items())
        class Store:
            @asynccontextmanager
            async def async_guard(self):yield db
            def stop(self,chat,reason,connection):connection.execute('INSERT OR REPLACE INTO controls VALUES(?,?)',('stop:'+chat,reason))
        driver=server.CDPDriver.__new__(server.CDPDriver);driver.store=Store()
        await driver.stop_uncertain({'url':'https://chatgpt.com/'})
        self.assertEqual(dict(db.execute('SELECT * FROM controls')),controls)
        await driver.stop_uncertain({'url':'https://chatgpt.com/c/owned'})
        self.assertEqual(dict(db.execute('SELECT * FROM controls')),{**controls,'stop:https://chatgpt.com/c/owned':'AISIS request timeout'})
        db.execute("UPDATE controls SET value='human stop' WHERE key='stop:https://chatgpt.com/c/owned'")
        await driver.stop_uncertain({'url':'https://chatgpt.com/c/owned'})
        self.assertEqual(db.execute("SELECT value FROM controls WHERE key='stop:https://chatgpt.com/c/owned'").fetchone()[0],'human stop')
        db.close()
    async def test_total_timeout_keeps_admission_busy_until_inflight_driver_drains(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=server.Ledger(Path(directory)/'pm.sqlite');started=asyncio.Event();cancelled=asyncio.Event();finish=asyncio.Event();calls=[]
            class Driver:
                async def ask(self,payload,session,remember,dispatching):
                    calls.append(payload['turn_id']);remember('owned','https://chatgpt.com/c/owned');dispatching();started.set()
                    try:await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        cancelled.set();await finish.wait();raise
                async def stop_uncertain(self,session):pass
            service=server.PromptService(Driver(),ledger,{'a'},request_timeout=.015)
            payload={'station_id':'a','turn_id':'one','text':'hello'}
            first=asyncio.create_task(service.submit(payload));await started.wait();await cancelled.wait()
            with self.assertRaises(server.Refusal) as caught:await service.submit({**payload,'turn_id':'two'})
            self.assertEqual(caught.exception.code,'browser_busy');self.assertFalse(first.done())
            finish.set()
            with self.assertRaises(server.Refusal) as caught:await first
            self.assertEqual(caught.exception.code,'request_timeout')
            self.assertEqual(calls,['one']);self.assertEqual(ledger.turn_status('one'),'uncertain')
            self.assertIsNone(ledger.turn_status('two'))
            await service.drain()

class TimeoutConfigurationTests(unittest.TestCase):
    def test_invalid_server_budgets_are_rejected(self):
        for value in ('nan','inf','0','-1','301'):
            with self.assertRaises(ValueError):server.PromptService(None,None,{'a'},request_timeout=value)
    def test_server_total_budget_exceeds_reply_budget_and_precedes_http(self):
        self.assertEqual(server.PromptService(None,None,{'a'}).request_timeout,230)

if __name__=='__main__': unittest.main()
