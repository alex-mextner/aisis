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
    async def test_stale_target_never_adopts_human_url_only_match(self):
        driver=self.driver([('human','https://chatgpt.com/c/owned'),('unrelated','https://chatgpt.com/c/other')])
        with self.assertRaises(server.Refusal):
            await driver.page_for({'target':'stale','url':'https://chatgpt.com/c/owned'},lambda *a:None)
        self.assertEqual(driver.context.created,0)
        self.assertTrue(all(not page.navigations for page in driver.context.pages))
    async def test_missing_owned_url_restores_only_that_url(self):
        driver=self.driver([('unrelated','https://chatgpt.com/c/other')])
        remembered=[]
        page,target=await driver.page_for({'target':'stale','url':'https://chatgpt.com/c/owned'},lambda *a,**kw:remembered.append((a,kw)))
        self.assertEqual(page.navigations,['https://chatgpt.com/c/owned'])
        self.assertEqual(driver.context.pages[0].navigations,[])
        self.assertEqual(remembered,[(('created-1','https://chatgpt.com/c/owned'),{'navigation_state':'pending'}),
            (('created-1','https://chatgpt.com/c/owned'),{'navigation_state':'ready'})])
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



class TabTransferLedgerTests(unittest.TestCase):
    def setUp(self):
        import hashlib
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'pm.sqlite';self.ledger=server.Ledger(self.path)
        self.allowed={'a','b','c'}
        self.prompt='anchored prompt a';self.digest=hashlib.sha256(self.prompt.encode()).hexdigest()
        self.ledger.reserve({'station_id':'a','turn_id':'old-a','text':self.prompt})
        self.ledger.remember('a','owned','https://chatgpt.com/c/a','anchor-a',self.digest)
        self.ledger.mark('old-a','complete')
    def tearDown(self):self.tmp.cleanup()
    def destination(self,station='b'):
        self.ledger.reserve({'station_id':station,'turn_id':'new-'+station,'text':'new prompt'})
        return self.ledger.session(station)
    def test_claim_moves_exclusive_target_atomically_and_preserves_both_histories(self):
        import hashlib
        self.ledger.reserve({'station_id':'b','turn_id':'old-b','text':'old b'})
        digest=hashlib.sha256(b'old b').hexdigest()
        self.ledger.remember('b','lost-b','https://chatgpt.com/c/b','anchor-b',digest)
        self.ledger.mark('old-b','complete')
        expected=self.destination();candidate=self.ledger.reusable_sessions('b',self.allowed)[0]
        self.ledger.claim_transfer(candidate,'b','new-b',expected)
        source=self.ledger.session('a');dest=self.ledger.session('b')
        self.assertEqual((source['target'],source['url'],source['anchor_turn'],source['anchor_digest']),('',candidate['url'],'anchor-a',self.digest))
        self.assertEqual((dest['target'],dest['url'],dest['anchor_turn'],dest['anchor_digest']),('owned','https://chatgpt.com/c/b','anchor-b',digest))
        self.assertEqual(self.ledger.db.execute("SELECT station FROM sessions WHERE target='owned'").fetchall(),[('b',)])
        self.assertEqual(self.ledger.db.execute('SELECT state FROM tab_transfers').fetchall(),[('pending',)])
    def test_changed_candidate_rolls_back_without_releasing_source(self):
        self.destination();candidate=self.ledger.reusable_sessions('b',self.allowed)[0]
        self.ledger.remember('a','owned','https://chatgpt.com/c/a','changed-anchor',self.digest)
        with self.assertRaises(server.Refusal):self.ledger.claim_transfer(candidate,'b','new-b',None)
        self.assertEqual(self.ledger.session('a')['target'],'owned')
        self.assertIsNone(self.ledger.session('b'))
    def test_pending_transfer_blocks_both_stations_after_restart(self):
        self.destination();candidate=self.ledger.reusable_sessions('b',self.allowed)[0]
        self.ledger.claim_transfer(candidate,'b','new-b',None)
        restarted=server.Ledger(self.path)
        for station in ('a','b'):
            with self.subTest(station=station),self.assertRaises(server.Refusal):
                restarted.reserve({'station_id':station,'turn_id':'again-'+station,'text':'do not send'})
        self.assertEqual(restarted.reusable_sessions('c',self.allowed),[])
        self.assertEqual(restarted.session('b')['navigation_state'],'transfer_pending')
    def test_source_must_be_allowed_anchored_complete_and_without_unresolved_turn(self):
        self.assertEqual(self.ledger.reusable_sessions('b',{'b'}),[])
        self.ledger.mark('old-a','uncertain')
        self.assertEqual(self.ledger.reusable_sessions('b',self.allowed),[])
    def test_later_failed_pre_dispatch_turn_does_not_starve_completed_anchor(self):
        self.ledger.reserve({'station_id':'a','turn_id':'stopped-a','text':'refused before page allocation'})
        self.ledger.mark('stopped-a','failed')
        candidate=self.ledger.reusable_sessions('b',self.allowed)[0]
        self.assertEqual(candidate['anchor_digest'],self.digest)
    def test_newer_completed_prompt_with_mismatched_anchor_is_never_reused(self):
        self.ledger.reserve({'station_id':'a','turn_id':'newer-a','text':'different complete prompt'})
        self.ledger.mark('newer-a','complete')
        self.assertEqual(self.ledger.reusable_sessions('b',self.allowed),[])
    def test_changed_destination_claim_rolls_back_source_release(self):
        expected=self.destination();candidate=self.ledger.reusable_sessions('b',self.allowed)[0]
        self.ledger.remember('b','unexpected-target','https://chatgpt.com/c/b')
        with self.assertRaises(server.Refusal):self.ledger.claim_transfer(candidate,'b','new-b',expected)
        self.assertEqual(self.ledger.session('a')['target'],'owned')
        self.assertEqual(self.ledger.session('b')['target'],'unexpected-target')
    def test_atomic_claim_refuses_saved_destination_missing_either_anchor(self):
        for index,(anchor,digest) in enumerate([(None,self.digest),('anchor-b',None)]):
            with self.subTest(anchor=anchor,digest=digest):
                self.ledger.remember('b','lost-b','https://chatgpt.com/c/b')
                self.ledger.db.execute('UPDATE sessions SET anchor_turn=?,anchor_digest=? WHERE station=?',(anchor,digest,'b'));self.ledger.db.commit()
                turn='legacy-direct-'+str(index)
                self.ledger.reserve({'station_id':'b','turn_id':turn,'text':'new prompt'})
                candidate=self.ledger.reusable_sessions('b',self.allowed)[0]
                with self.assertRaises(server.Refusal):self.ledger.claim_transfer(candidate,'b',turn,self.ledger.session('b'))
                self.assertEqual(self.ledger.session('a')['target'],'owned')
                self.assertEqual(self.ledger.session('b')['target'],'lost-b')
                self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM tab_transfers').fetchone()[0],0)
                self.ledger.mark(turn,'failed')
    def test_distinct_stations_cannot_alias_the_same_conversation(self):
        self.ledger.remember('b','lost-b','https://chatgpt.com/c/a','anchor-a',self.digest)
        expected=self.destination();candidate=self.ledger.reusable_sessions('b',self.allowed)[0]
        with self.assertRaises(server.Refusal):self.ledger.claim_transfer(candidate,'b','new-b',expected)
        self.assertEqual(self.ledger.session('a')['target'],'owned')

class TabReuseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import hashlib
        from contextlib import asynccontextmanager
        import sqlite3
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'pm.sqlite';self.ledger=server.Ledger(self.path)
        self.guard=sqlite3.connect(':memory:')
        self.guard.executescript('CREATE TABLE controls(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE attempts(chat TEXT,state TEXT);')
        from types import ModuleType,SimpleNamespace
        from unittest.mock import patch
        core=ModuleType('agenttools_chatgpt.core');core.Observation=SimpleNamespace
        observation_patch=patch.dict('sys.modules',{'agenttools_chatgpt.core':core})
        observation_patch.start();self.addCleanup(observation_patch.stop)
        self.safety_observations=[]
        class Store:
            @asynccontextmanager
            async def async_guard(store):yield self.guard
            def quota(store,db=None):
                connection=self.guard if db is None else db
                connection.execute("INSERT OR REPLACE INTO controls VALUES('astra_quota','blocked')")
                if db is None:connection.commit()
            def stop(store,chat='*',reason='manual stop',db=None):
                connection=self.guard if db is None else db
                connection.execute('INSERT OR REPLACE INTO controls VALUES(?,?)',('stop:'+chat,reason))
                if db is None:connection.commit()
            def _api_allowed(store,db,chat,model,effort,verified):
                return not db.execute("SELECT 1 FROM controls WHERE key IN ('stop:*',?,'astra_quota')",('stop:'+chat,)).fetchone() and not db.execute("SELECT 1 FROM attempts WHERE chat=? AND state IN ('reserved','uncertain')",(chat,)).fetchone()
        class Page:
            def __init__(page,target,url,human=False):page.identity=target;page.url=url;page.human=human;page.navigation=[];page.hidden=True;page.draft='';page.running=False;page.challenge=False;page.quota=False;page.ambiguous=False;page.refuse=False;page.throw=False;page.load_error=False;page.history_length=2
            def is_closed(page):return False
            async def goto(page,*a,**kw):raise AssertionError('No tab creation/goto during full-capacity reuse')
            async def evaluate(page,script,args):
                self.assertFalse(page.human);self.assertEqual(args['action'],'transfer')
                if page.refuse:return False
                page.navigation.append(args['destination']);page.url=args['destination']
                if page.throw:raise RuntimeError('Execution context destroyed after native navigation')
                return True
            async def wait_for_url(page,expected,**kw):
                self.assertEqual(page.url,expected)
        self.Page=Page
        self.humans=[Page('human-'+str(i),'https://chatgpt.com/c/human-'+str(i),True) for i in range(5)]
        self.worker=Page('worker','https://chatgpt.com/c/a')
        self.context=type('Context',(),{'pages':[*self.humans,self.worker]})()
        self.driver=server.CDPDriver.__new__(server.CDPDriver)
        self.driver.context=self.context;self.driver.max_tabs=6;self.driver.initial_target='';self.driver.initial_used=True
        self.driver.ledger=self.ledger;self.driver.allowed={'a','b','c'};self.driver.store=Store();self.driver.script=server.DOM
        class GuardianPolicy:
            # Installed Guardian contract: quota delegates to Store.quota globally;
            # challenge delegates to Store.stop for the observed chat only.
            def record_safety(guardian,observation,db=None):
                self.safety_observations.append(observation)
                if observation.challenge:self.driver.store.stop(observation.chat or '*','human challenge',db)
                if observation.quota:self.driver.store.quota(db)
        self.driver.guardian=GuardianPolicy()
        self.histories={};self.source_prompt='previous complete a'
        self.ledger.reserve({'station_id':'a','turn_id':'old-a','text':self.source_prompt})
        self.ledger.remember('a','worker','https://chatgpt.com/c/a','anchor-a',hashlib.sha256(self.source_prompt.encode()).hexdigest())
        self.ledger.mark('old-a','complete')
        self.histories['https://chatgpt.com/c/a']=[{'id':'anchor-a','user':self.source_prompt,'assistant':'prior answer','state':'complete'}]
        async def target(page):return page.identity
        async def snapshot(page,expected_chat=None):
            self.assertFalse(page.human)
            if expected_chat and page.url!=expected_chat:raise server.Refusal('conversation_changed')
            return {'url':page.url,'chat':page.url,'hidden':page.hidden,'draft':page.draft,
                'running':page.running,'challenge':page.challenge,'quota':page.quota,
                'ambiguous':page.ambiguous or (page.load_error and bool(page.navigation)),'root_match':'attribute',
                'turns':list(self.histories.get(page.url,[]))}
        async def dispatch(operation):return await operation
        async def create():raise AssertionError('Must never create seventh tab')
        self.driver.target=target;self.driver.snapshot=snapshot;self.driver.dispatch=dispatch;self.driver.new_background_page=create
    async def asyncTearDown(self):self.guard.close();self.tmp.cleanup()
    async def allocate(self,station='b',turn='new-b'):
        payload={'station_id':station,'turn_id':turn,'text':'new prompt '+turn}
        self.ledger.reserve(payload);session=self.ledger.session(station)
        def remember(target,url,*args,**kw):self.ledger.remember(station,target,url,*args,**kw)
        return await self.driver.page_for(session,remember,station=station,turn_id=turn)
    async def test_five_human_tabs_and_one_history_two_worker_serve_a_b_c_a_b_without_close_or_create(self):
        import hashlib
        for index,station in enumerate(('b','c','a','b')):
            page,target=await self.allocate(station,'turn-'+str(index))
            self.assertIs(page,self.worker);self.assertEqual(target,'worker')
            expected='https://chatgpt.com/c/'+station
            self.assertEqual(page.url,expected if station in ('a','b') and expected in self.histories else 'https://chatgpt.com/')
            prompt='completed '+station+str(index);anchor='anchor-'+str(index)
            page.url=expected
            history=self.histories.setdefault(expected,[]);history.append({'id':anchor,'user':prompt,'assistant':'answer','state':'complete'})
            self.ledger.remember(station,target,expected,anchor,hashlib.sha256(prompt.encode()).hexdigest())
            # The allocated request's digest must represent the same completed prompt.
            self.ledger.db.execute('UPDATE turns SET digest=? WHERE id=?',(hashlib.sha256(prompt.encode()).hexdigest(),'turn-'+str(index)));self.ledger.db.commit()
            self.ledger.mark('turn-'+str(index),'complete')
            self.assertEqual(len(self.context.pages),6);self.assertEqual(page.history_length,2)
        self.assertEqual(self.worker.navigation,['https://chatgpt.com/','https://chatgpt.com/','https://chatgpt.com/c/a','https://chatgpt.com/c/b'])
        self.assertTrue(all(not page.navigation for page in self.humans))
        self.assertTrue(all(self.ledger.session(station)['url']=='https://chatgpt.com/c/'+station for station in ('a','b','c')))
    async def test_unsafe_source_ui_never_transfers(self):
        for key,value in [('hidden',False),('draft','human draft'),('running',True),('challenge',True),('quota',True),('ambiguous',True)]:
            with self.subTest(key=key):
                setattr(self.worker,key,value)
                with self.assertRaises(server.Refusal):await self.allocate('b','blocked-'+key)
                self.ledger.mark('blocked-'+key,'failed')
                setattr(self.worker,key,True if key=='hidden' else '' if key=='draft' else False)
        self.assertEqual(self.worker.navigation,[]);self.assertEqual(self.ledger.session('a')['target'],'worker')
    async def test_source_controls_or_unresolved_intents_never_transfer(self):
        for key,value in [('stop:https://chatgpt.com/c/a','manual'),('watch:https://chatgpt.com/c/a','0'),('watch:https://chatgpt.com/c/a','1'),('stop:*','manual'),('astra_quota','blocked')]:
            with self.subTest(key=key,value=value):
                self.guard.execute('INSERT OR REPLACE INTO controls VALUES(?,?)',(key,value));self.guard.commit()
                with self.assertRaises(server.Refusal):await self.allocate('b','blocked-'+key+value)
                self.ledger.mark('blocked-'+key+value,'failed');self.guard.execute('DELETE FROM controls');self.guard.commit()
        self.guard.execute('INSERT INTO attempts VALUES(?,?)',(self.worker.url,'uncertain'));self.guard.commit()
        with self.assertRaises(server.Refusal):await self.allocate('b','blocked-intent')
        self.assertEqual(self.worker.navigation,[])
    async def test_replaced_source_target_or_url_is_not_overwritten(self):
        self.worker.url='https://chatgpt.com/c/human-navigation'
        with self.assertRaises(server.Refusal):await self.allocate()
        self.assertEqual(self.worker.navigation,[])
    async def test_nested_dispatch_timeout_drains_outer_cancellation_without_sending_past_deadline(self):
        inner_timed_out=asyncio.Event();outer_cancel_drained=asyncio.Event();renderer_release=asyncio.Event();resumed=[]
        original_evaluate=self.worker.evaluate
        async def evaluate(script,args):
            result=await original_evaluate(script,args)
            await renderer_release.wait()
            return result
        self.worker.evaluate=evaluate
        async def protected(operation):
            task=asyncio.create_task(operation)
            try:
                return await asyncio.wait_for(asyncio.shield(task),.005)
            except BaseException as original:
                inner_timed_out.set()
                while not task.done():
                    try:await asyncio.shield(task)
                    except asyncio.CancelledError:outer_cancel_drained.set()
                # Match installed protected_dispatch: rethrow the ORIGINAL inner
                # TimeoutError after swallowing outer cancellation during drain.
                raise original
        self.driver.dispatch=protected
        async def ask(payload,session,remember,dispatching):
            await self.driver.page_for(session,remember,station=payload['station_id'],turn_id=payload['turn_id'])
            resumed.append('would_send_past_deadline');dispatching()
            return {'text':'must not escape','conversation_id':'https://chatgpt.com/'}
        owner=type('Owner',(),{})();owner.ready=True;owner.ask=ask
        service=server.PromptService(owner,self.ledger,{'a','b'},request_timeout=.02)
        operation=asyncio.create_task(service.submit({'station_id':'b','turn_id':'nested-timeout','text':'never send'}))
        await asyncio.wait_for(inner_timed_out.wait(),1)
        await asyncio.wait_for(outer_cancel_drained.wait(),1)
        renderer_release.set()
        with self.assertRaises(server.Refusal) as caught:await operation
        self.assertEqual(caught.exception.code,'request_timeout')
        self.assertEqual(resumed,[])
        self.assertEqual(self.worker.navigation,['https://chatgpt.com/'])
        self.assertEqual(self.ledger.turn_status('nested-timeout'),'failed')
        self.assertEqual(self.ledger.db.execute('SELECT state FROM tab_transfers').fetchone()[0],'pending')
        await service.drain()

    async def test_destination_composer_and_saved_history_may_hydrate_after_domcontentloaded(self):
        import hashlib
        self.ledger.reserve({'station_id':'b','turn_id':'saved-b','text':'saved question b'})
        self.ledger.remember('b','missing-b','https://chatgpt.com/c/b','saved-anchor-b',hashlib.sha256(b'saved question b').hexdigest())
        self.ledger.mark('saved-b','complete')
        self.histories['https://chatgpt.com/c/b']=[{'id':'saved-anchor-b','user':'saved question b','assistant':'saved answer b','state':'complete'}]
        original_snapshot=self.driver.snapshot;observed=[]
        async def snapshot(page,expected_chat=None):
            result=await original_snapshot(page,expected_chat=expected_chat)
            if page.navigation:
                observed.append('destination')
                if len(observed)==1:return dict(result,ambiguous=True,loading=True,turns=[])
                if len(observed)==2:return dict(result,ambiguous=False,loading=False,turns=[])
            return result
        self.driver.snapshot=snapshot
        page,target=await self.allocate()
        self.assertEqual(len(observed),3)
        self.assertEqual(self.worker.navigation,['https://chatgpt.com/c/b'])
        self.assertEqual(self.ledger.db.execute('SELECT state FROM tab_transfers').fetchone()[0],'complete')
        self.assertEqual(self.ledger.session('b')['anchor_turn'],'saved-anchor-b')

    async def test_destination_history_prefix_can_hydrate_before_saved_anchor(self):
        import hashlib
        self.ledger.reserve({'station_id':'b','turn_id':'saved-b-prefix','text':'saved question b'})
        self.ledger.remember('b','missing-b','https://chatgpt.com/c/b','saved-anchor-b',hashlib.sha256(b'saved question b').hexdigest())
        self.ledger.mark('saved-b-prefix','complete')
        prefix={'id':'older-prefix','user':'earlier question','assistant':'earlier answer','state':'complete'}
        anchor={'id':'saved-anchor-b','user':'saved question b','assistant':'saved answer b','state':'complete'}
        self.histories['https://chatgpt.com/c/b']=[prefix,anchor]
        original_snapshot=self.driver.snapshot;observed=[]
        async def snapshot(page,expected_chat=None):
            result=await original_snapshot(page,expected_chat=expected_chat)
            if page.navigation:
                observed.append('destination')
                if len(observed)==1:return dict(result,ambiguous=False,loading=False,turns=[prefix])
            return result
        self.driver.snapshot=snapshot
        await self.allocate()
        self.assertEqual(len(observed),2)
        self.assertEqual(self.worker.navigation,['https://chatgpt.com/c/b'])
        self.assertEqual(self.ledger.db.execute('SELECT state FROM tab_transfers').fetchone()[0],'complete')

    async def test_missing_destination_anchor_stays_pending_until_request_deadline(self):
        import hashlib
        self.ledger.reserve({'station_id':'b','turn_id':'saved-b-missing','text':'saved question b'})
        self.ledger.remember('b','missing-b','https://chatgpt.com/c/b','saved-anchor-b',hashlib.sha256(b'saved question b').hexdigest())
        self.ledger.mark('saved-b-missing','complete')
        self.histories['https://chatgpt.com/c/b']=[{'id':'different-history','user':'unrelated','assistant':'answer','state':'complete'}]
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(.02):await self.allocate()
        self.assertEqual(self.worker.navigation,['https://chatgpt.com/c/b'])
        self.assertEqual(self.ledger.db.execute('SELECT state FROM tab_transfers').fetchone()[0],'pending')

    async def test_context_loss_allows_readonly_same_target_settlement_without_second_navigation(self):
        self.worker.throw=True
        page,target=await self.allocate()
        self.assertEqual(self.worker.navigation,['https://chatgpt.com/'])
        self.assertEqual(self.ledger.db.execute('SELECT state FROM tab_transfers').fetchone()[0],'complete')
    async def test_navigation_refusal_or_unloaded_destination_leaves_durable_pending_claim(self):
        self.worker.refuse=True
        with self.assertRaises(server.Refusal):await self.allocate()
        self.assertEqual(self.worker.navigation,[])
        restarted=server.Ledger(self.path)
        for station in ('a','b'):
            with self.subTest(station=station),self.assertRaises(server.Refusal):restarted.reserve({'station_id':station,'turn_id':'after-'+station,'text':'no retry'})
    async def test_correct_destination_url_without_loaded_ui_does_not_finalize_claim(self):
        self.worker.load_error=True
        with self.assertRaises(server.Refusal):await self.allocate()
        self.assertEqual(self.ledger.db.execute('SELECT state FROM tab_transfers').fetchone()[0],'pending')
    async def test_existing_destination_history_must_match_saved_anchor_before_claim_finishes(self):
        import hashlib
        self.ledger.reserve({'station_id':'b','turn_id':'old-b','text':'saved prompt b'})
        self.ledger.remember('b','lost-b','https://chatgpt.com/c/b','anchor-b',hashlib.sha256(b'saved prompt b').hexdigest())
        self.ledger.mark('old-b','complete')
        self.histories['https://chatgpt.com/c/b']=[{'id':'anchor-b','user':'unrelated prompt','assistant':'answer','state':'complete'}]
        with self.assertRaises(server.Refusal) as caught:await self.allocate()
        self.assertEqual(caught.exception.code,'transfer_anchor_changed')
        self.assertEqual(self.ledger.db.execute('SELECT state FROM tab_transfers').fetchone()[0],'pending')
        self.assertEqual(self.worker.navigation,['https://chatgpt.com/c/b'])
    async def test_prepared_but_failed_source_draft_remains_nonreusable(self):
        self.ledger.reserve({'station_id':'a','turn_id':'prepared-a','text':'prepared draft'})
        self.ledger.mark('prepared-a','failed');self.worker.draft='prepared draft'
        with self.assertRaises(server.Refusal):await self.allocate()
        self.assertEqual(self.worker.navigation,[])
    async def test_source_global_stop_failed_attempt_does_not_starve_slot_after_resume(self):
        self.ledger.reserve({'station_id':'a','turn_id':'stopped-a','text':'refused before allocation'})
        self.ledger.mark('stopped-a','failed')
        page,target=await self.allocate()
        self.assertIs(page,self.worker);self.assertEqual(target,'worker')


@unittest.skipUnless(shutil.which('node'),'Node is required for the injected JS boundary')
class TransferDOMTests(unittest.TestCase):
    def fixture(self,variant='safe'):
        shared="const all=(s,r=document)=>r.querySelectorAll(s);const visible=e=>!!e&&e.isConnected&&e.getClientRects().length>0;const rootSelector='ROOT';const conversationRoot=()=>({root,match:'attribute',error:''});const thinkingFailure=()=>({count:0});"
        script=server.DOM.replace('/*@conversation-root*/',shared)
        setup=r"""
global.performance={timeOrigin:Date.now(),now:()=>0};global.location={origin:'https://chatgpt.com',pathname:'/c/owned',href:'https://chatgpt.com/c/owned'};
let closes=0,replacements=[],running=false,ambiguous=false,quota=false,stateValue='complete';
const element=(text='',attrs={})=>({textContent:text,innerText:text,isConnected:true,getClientRects:()=>[{}],getAttribute:k=>attrs[k]||null,closest:()=>null,parentElement:{closest:()=>null}});
const composer=element(''),trigger=element('Thinking'),profile=element(''),user=element('completed prompt'),assistant=element('answer'),state=element('');
state.getAttribute=k=>k==='data-talvt-turn-state'?stateValue:null;
const turn=element('',{'data-turn-key':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'});
turn.querySelectorAll=s=>s==='[data-user-message-bubble="true"]'?[user]:s==='[data-markdown-copy]'?[assistant]:s==='[data-talvt-turn-state]'?[state]:[];
const root=element('',{'data-chatgpt-conversation-selection-target':'owned'});
root.querySelectorAll=s=>s==='[data-turn-key]'?[turn]:[];
global.window={close:()=>{throw new Error('FORBIDDEN_WINDOW_CLOSE')}};global.history={length:2};global.opener=null;location.replace=url=>replacements.push(url);
global.document={title:'ChatGPT',visibilityState:'hidden',querySelectorAll:s=>{
 if(s.includes('data-composer-markdown'))return [composer];
 if(s.includes('data-codex-intelligence-trigger'))return [trigger];
 if(s==='button[aria-label="Open profile menu"]')return ambiguous?[]:[profile];
 if(s==='button[aria-label="Stop"]')return running?[element('Stop')]:[];
 if(s==='[role="alert"]')return quota?[element('GPT-6 quota')]:[];
 return [];
}};
"""
        changes={'draft':'composer.textContent="human draft";', 'foreground':'document.visibilityState="visible";',
            'running':'running=true;', 'challenge':'document.title="Verify you are human";',
            'quota':'quota=true;', 'ambiguous':'ambiguous=true;', 'anchor':'user.textContent="changed prompt";',
            'unfinished':'stateValue="in_progress";', 'url':'location.pathname="/c/human";location.href="https://chatgpt.com/c/human";'}
        source=setup+'const run=('+script+');const expected=run({action:"snapshot",expected_chat:"https://chatgpt.com/c/owned"});'
        source+=changes.get(variant,'')
        deadline='Date.now()-1' if variant=='expired' else 'Date.now()+1000'
        source+='let result=false,error="";try{result=run({action:"transfer",destination:"https://chatgpt.com/c/b",expected,expected_chat:"https://chatgpt.com/c/owned",deadline:'+deadline+'})}catch(e){error=String(e)}console.log(JSON.stringify({result,closes,replacements,error}));'
        result=subprocess.run(['node','-e',source],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)
    def test_history_two_cdp_tab_uses_exact_native_replace_without_window_close(self):
        self.assertEqual(self.fixture(),{'result':True,'closes':0,'replacements':['https://chatgpt.com/c/b'],'error':''})
    def test_changed_or_unsafe_renderer_state_never_navigates(self):
        for variant in ('draft','foreground','running','challenge','quota','ambiguous','anchor','unfinished','url','expired'):
            with self.subTest(variant=variant):
                result=self.fixture(variant)
                self.assertEqual(result['closes'],0)
                self.assertEqual(result['replacements'],[])
                self.assertFalse(result['result'])



class NoPostAnswerCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_within_tight_outer_gap_without_a_cleanup_task(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=server.Ledger(Path(directory)/'pm.sqlite');driver=FakeDriver();cleanup=[]
            async def forbidden_cleanup(*args):cleanup.append(True);await asyncio.Event().wait()
            driver.retire_completed=forbidden_cleanup
            service=server.PromptService(driver,ledger,{'a'},request_timeout=.02)
            result=await asyncio.wait_for(service.submit({'station_id':'a','turn_id':'one','text':'hello'}),.05)
            self.assertEqual(result['text'],'reply');self.assertEqual(cleanup,[])
            self.assertEqual(ledger.turn_status('one'),'complete');await service.drain()




class ExistingOwnedOverCapacityTests(unittest.IsolatedAsyncioTestCase):
    driver=RestartRecoveryTests.driver
    async def test_existing_exact_owned_target_remains_usable_with_seven_pages(self):
        driver=self.driver([('owned','https://chatgpt.com/c/owned')]+[(str(i),'https://chatgpt.com/c/human'+str(i)) for i in range(6)])
        page,target=await driver.page_for({'target':'owned','url':'https://chatgpt.com/c/owned'},lambda *a,**kw:None)
        self.assertEqual(target,'owned');self.assertEqual(driver.context.created,0)
        self.assertEqual(page.navigations,[])
    async def test_seven_pages_still_block_new_allocation(self):
        driver=self.driver([(str(i),'https://chatgpt.com/c/human'+str(i)) for i in range(7)])
        with self.assertRaises(server.Refusal):await driver.page_for(None,lambda *a,**kw:None)
        self.assertEqual(driver.context.created,0)

class ReopenedAndMultipleCandidateTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=TabReuseTests.asyncSetUp
    asyncTearDown=TabReuseTests.asyncTearDown
    allocate=TabReuseTests.allocate
    async def reopen_released(self,fail=False):
        await self.allocate('b','first-transfer')
        self.ledger.mark('first-transfer','failed')
        self.context.pages.remove(self.worker)  # external closure, not a server action
        page=self.Page('new-a','about:blank')
        async def goto(url,**kw):
            if fail:raise RuntimeError('navigation failed')
            page.url=url;page.navigation.append(url)
        page.goto=goto
        async def background():self.context.pages.append(page);return page,page.identity
        self.driver.new_background_page=background
        try:await self.allocate('a','reopen-a')
        finally:self.ledger.mark('reopen-a','failed')
        return page
    async def test_released_reopen_clears_state_and_can_transfer_again(self):
        page=await self.reopen_released()
        self.assertEqual(self.ledger.session('a')['navigation_state'],'ready')
        self.assertEqual(self.ledger.session('a')['anchor_turn'],'anchor-a')
        found,target=await self.allocate('c','after-reopen')
        self.assertIs(found,page);self.assertEqual(target,'new-a')
        self.assertEqual(self.ledger.session('a')['navigation_state'],'released')
        self.assertEqual(self.ledger.session('c')['target'],'new-a')
    async def test_saved_url_failed_navigation_records_failed_and_does_not_reuse_blank(self):
        with self.assertRaises(server.Refusal) as caught:await self.reopen_released(fail=True)
        self.assertEqual(caught.exception.code,'navigation_failed')
        self.assertEqual(self.ledger.session('a')['navigation_state'],'failed')
        self.assertEqual(self.ledger.session('a')['anchor_turn'],'anchor-a')
        with self.assertRaises(server.Refusal):await self.allocate('c','after-failed-reopen')
        self.assertEqual(self.ledger.session('a')['target'],'new-a')
    def second_candidate(self):
        import hashlib
        self.context.pages.remove(self.humans[-1])
        page=self.Page('worker-c','https://chatgpt.com/c/c');self.context.pages.append(page)
        prompt='previous complete c'
        self.ledger.reserve({'station_id':'c','turn_id':'old-c','text':prompt})
        self.ledger.remember('c',page.identity,page.url,'anchor-c',hashlib.sha256(prompt.encode()).hexdigest())
        self.ledger.mark('old-c','complete')
        self.histories[page.url]=[{'id':'anchor-c','user':prompt,'assistant':'prior c answer','state':'complete'}]
        return page
    async def test_first_foreground_candidate_does_not_starve_second_idle_candidate(self):
        second=self.second_candidate();self.worker.hidden=False
        page,target=await self.allocate()
        self.assertIs(page,second);self.assertEqual(target,'worker-c')
        self.assertEqual(self.worker.navigation,[])
        self.assertEqual(self.ledger.session('a')['target'],'worker')
    async def test_observed_source_quota_or_challenge_never_falls_back_without_db_control(self):
        second=self.second_candidate()
        for flag,code in [('quota','quota_blocked'),('challenge','human_attention_required')]:
            with self.subTest(flag=flag):
                setattr(self.worker,flag,True)
                with self.assertRaises(server.Refusal) as caught:await self.allocate('b','unsafe-'+flag)
                self.assertEqual(caught.exception.code,code)
                self.ledger.mark('unsafe-'+flag,'failed');setattr(self.worker,flag,False)
        self.assertEqual(self.worker.navigation,[]);self.assertEqual(second.navigation,[])
        self.assertEqual(dict(self.guard.execute('SELECT key,value FROM controls')),{'astra_quota':'blocked','stop:https://chatgpt.com/c/a':'human challenge'})
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM tab_transfers').fetchone()[0],0)
    async def test_all_busy_candidates_leave_ownership_and_ui_untouched(self):
        second=self.second_candidate();self.worker.draft='human draft';second.hidden=False
        with self.assertRaises(server.Refusal):await self.allocate()
        self.assertEqual(self.worker.navigation,[]);self.assertEqual(second.navigation,[])
        self.assertEqual(self.ledger.session('a')['target'],'worker');self.assertEqual(self.ledger.session('c')['target'],'worker-c')
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM tab_transfers').fetchone()[0],0)
    async def test_source_watch_blocks_only_that_candidate_without_clearing_control(self):
        second=self.second_candidate()
        self.guard.execute('INSERT INTO controls VALUES(?,?)',('watch:https://chatgpt.com/c/a','0'));self.guard.commit()
        page,target=await self.allocate()
        self.assertIs(page,second);self.assertEqual(self.worker.navigation,[])
        self.assertEqual(self.guard.execute('SELECT key,value FROM controls').fetchall(),[('watch:https://chatgpt.com/c/a','0')])
    async def test_global_stop_cannot_be_bypassed_by_later_safe_candidate(self):
        second=self.second_candidate();self.worker.hidden=False
        self.guard.execute("INSERT INTO controls VALUES('stop:*','manual')");self.guard.commit()
        with self.assertRaises(server.Refusal):await self.allocate()
        self.assertEqual(self.worker.navigation,[]);self.assertEqual(second.navigation,[])
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM tab_transfers').fetchone()[0],0)
    async def test_postclaim_refusal_never_falls_back_to_second_candidate(self):
        second=self.second_candidate();self.worker.refuse=True
        with self.assertRaises(server.Refusal) as caught:await self.allocate()
        self.assertEqual(caught.exception.code,'tab_transfer_refused')
        self.assertEqual(second.navigation,[])
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM tab_transfers').fetchone()[0],1)
        self.assertEqual(self.ledger.db.execute('SELECT state FROM tab_transfers').fetchone()[0],'pending')




class SafetyObservationAndPreclaimTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=TabReuseTests.asyncSetUp
    asyncTearDown=TabReuseTests.asyncTearDown
    allocate=TabReuseTests.allocate
    second_candidate=ReopenedAndMultipleCandidateTests.second_candidate
    def owned_b(self):
        page=self.second_candidate()
        self.ledger.db.execute("UPDATE sessions SET station='b' WHERE station='c'")
        self.ledger.db.execute("UPDATE turns SET station='b' WHERE station='c'");self.ledger.db.commit()
        return page
    async def assert_healthy_owned_b_blocked(self):
        from types import SimpleNamespace
        calls=[]
        self.driver.browser=SimpleNamespace(is_connected=lambda:True)
        async def page_for(*args,**kw):calls.append('page_for');raise AssertionError('Quota must block before touching healthy B')
        self.driver.page_for=page_for
        with self.assertRaises(server.Refusal) as caught:
            await self.driver.ask({'station_id':'b','turn_id':'no-send-b','text':'no request'},self.ledger.session('b'),lambda *a,**kw:None,lambda:calls.append('dispatch'))
        self.assertEqual(caught.exception.code,'guardian_stopped');self.assertEqual(calls,[])
    async def test_source_quota_is_recorded_and_blocks_subsequent_healthy_owned_station(self):
        healthy=self.owned_b();self.worker.quota=True
        with self.assertRaises(server.Refusal):await self.allocate('c','source-quota')
        self.ledger.mark('source-quota','failed')
        self.assertEqual(self.guard.execute("SELECT value FROM controls WHERE key='astra_quota'").fetchone(),('blocked',))
        self.assertEqual(len(self.safety_observations),1)
        await self.assert_healthy_owned_b_blocked()
        self.assertEqual(self.worker.navigation,[]);self.assertEqual(healthy.navigation,[])
    async def test_destination_quota_is_recorded_pending_claim_blocks_healthy_owned_station(self):
        healthy=self.owned_b();capture=self.driver.snapshot
        async def snapshot(page,expected_chat=None):
            value=await capture(page,expected_chat=expected_chat)
            return dict(value,quota=True) if page.navigation else value
        self.driver.snapshot=snapshot
        with self.assertRaises(server.Refusal):await self.allocate('c','destination-quota')
        self.assertEqual(self.guard.execute("SELECT value FROM controls WHERE key='astra_quota'").fetchone(),('blocked',))
        self.assertEqual(self.ledger.db.execute('SELECT state FROM tab_transfers').fetchone()[0],'pending')
        await self.assert_healthy_owned_b_blocked()
        self.assertEqual(healthy.navigation,[])
    async def test_source_challenge_records_only_existing_scoped_policy(self):
        healthy=self.owned_b();self.worker.challenge=True
        with self.assertRaises(server.Refusal):await self.allocate('c','source-challenge')
        self.assertEqual(self.guard.execute('SELECT key,value FROM controls').fetchall(),[('stop:https://chatgpt.com/c/a','human challenge')])
        self.assertTrue(self.driver.store._api_allowed(self.guard,healthy.url,'','',False))
    async def test_destination_challenge_records_only_observed_scope_and_preserves_pending(self):
        self.owned_b();capture=self.driver.snapshot
        async def snapshot(page,expected_chat=None):
            value=await capture(page,expected_chat=expected_chat)
            return dict(value,challenge=True) if page.navigation else value
        self.driver.snapshot=snapshot
        with self.assertRaises(server.Refusal):await self.allocate('c','destination-challenge')
        self.assertEqual(self.guard.execute('SELECT key,value FROM controls').fetchall(),[('stop:https://chatgpt.com/','human challenge')])
        self.assertEqual(self.ledger.db.execute('SELECT state FROM tab_transfers').fetchone()[0],'pending')
    async def test_ordinary_candidate_draft_never_records_a_global_safety_observation(self):
        self.worker.draft='ordinary human draft'
        with self.assertRaises(server.Refusal):await self.allocate()
        self.assertEqual(self.safety_observations,[])
        self.assertEqual(self.guard.execute('SELECT count(*) FROM controls').fetchone()[0],0)
    async def test_saved_destination_missing_either_anchor_rejects_before_claim_or_navigation(self):
        import hashlib
        for index,(anchor,digest) in enumerate([(None,hashlib.sha256(b'saved b').hexdigest()),('anchor-b',None)]):
            with self.subTest(anchor=anchor,digest=digest):
                self.ledger.remember('b','lost-b','https://chatgpt.com/c/b')
                self.ledger.db.execute('UPDATE sessions SET anchor_turn=?,anchor_digest=? WHERE station=?',(anchor,digest,'b'));self.ledger.db.commit()
                with self.assertRaises(server.Refusal):await self.allocate('b','legacy-'+str(index))
                self.ledger.mark('legacy-'+str(index),'failed')
                self.assertEqual(self.ledger.session('a')['target'],'worker')
                self.assertEqual(self.ledger.session('b')['target'],'lost-b')
        self.assertEqual(self.worker.navigation,[])
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM tab_transfers').fetchone()[0],0)

class BlankRepairAboveCapacityTests(unittest.IsolatedAsyncioTestCase):
    driver=RestartRecoveryTests.driver
    async def test_proven_owned_failed_blank_repairs_same_target_with_seven_pages(self):
        driver=self.driver([('owned','about:blank')]+[(str(i),'https://chatgpt.com/c/human'+str(i)) for i in range(6)])
        records=[]
        page,target=await driver.page_for({'target':'owned','url':'https://chatgpt.com/','navigation_state':'failed','can_recreate_homepage':True},lambda *a,**kw:records.append((a,kw)))
        self.assertEqual(target,'owned');self.assertEqual(driver.context.created,0)
        self.assertEqual(page.navigations,['https://chatgpt.com/'])
        self.assertEqual(records[-1][1],{'navigation_state':'ready'})
        self.assertTrue(all(not p.navigations for p in driver.context.pages[1:]))




class SavedAnchorPreparationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=TabReuseTests.asyncSetUp
    asyncTearDown=TabReuseTests.asyncTearDown
    async def reach_prepare(self,*,later_from=None,anchored=True,root_match='local-chatgpt'):
        import copy
        import hashlib
        from types import SimpleNamespace
        prompt='saved destination prompt';chat='https://chatgpt.com/c/b'
        anchor={'id':'anchor-b','user':prompt,'assistant':'saved answer','state':'complete'}
        later={'id':'later-human','user':'later hydrated human turn','assistant':'human reply','state':'complete'}
        if anchored:
            self.ledger.reserve({'station_id':'b','turn_id':'old-b','text':prompt})
            self.ledger.remember('b','missing-b',chat,'anchor-b',hashlib.sha256(prompt.encode()).hexdigest())
            self.ledger.mark('old-b','complete');self.histories[chat]=[anchor]
        snapshots=[];prepared=[];capture=self.driver.snapshot
        async def snapshot(page,expected_chat=None,allow_created_chat=False):
            value=await capture(page,expected_chat=expected_chat)
            value.update(signature='verified-model',root_selection='fixture',loading=False)
            if page.navigation:
                snapshots.append('destination')
                value['root_match']=root_match if anchored else ''
                value['turns']=copy.deepcopy([anchor,later] if anchored and later_from is not None and len(snapshots)>=later_from else [anchor] if anchored else [])
            return value
        self.driver.snapshot=snapshot
        async def noop(*a,**kw):pass
        self.driver.browser=SimpleNamespace(is_connected=lambda:True)
        self.driver.disconnected=asyncio.Event();self.driver.transport=SimpleNamespace(seed=noop)
        class Locator:
            def __init__(self,selector):self.selector=selector
            async def wait_for(self,**kw):pass
            async def count(self):return 1
            async def all_text_contents(self):return ['GPT-6'] if 'menuitemradio' in self.selector else []
        class Handle:
            async def dispose(self):pass
        self.worker.locator=lambda selector:Locator(selector)
        self.worker.keyboard=SimpleNamespace(press=noop)
        evaluate=self.worker.evaluate
        async def renderer(script,args):
            if args.get('action')=='transfer':return await evaluate(script,args)
            if args.get('action')=='open-model-menu':return True
            raise AssertionError('Unexpected renderer action; no send is permitted')
        async def handle(script,args):
            if args.get('action')=='probe':return Handle()
            if args.get('action')=='prepare':
                prepared.append([turn['id'] for turn in args['expected']['turns']])
                raise server.Refusal('fixture_prepare_reached')
            raise AssertionError('Unexpected renderer handle action')
        self.worker.evaluate=renderer;self.worker.evaluate_handle=handle
        service=server.PromptService(self.driver,self.ledger,self.driver.allowed)
        with self.assertRaises(server.Refusal) as caught:
            await service.submit({'station_id':'b','turn_id':'lineage-check','text':'offline request never dispatched',
                'conversation_id':chat if anchored else None})
        await service.drain()
        return caught.exception.code,prepared,snapshots
    async def test_later_turn_after_settlement_cannot_become_initial_local_root_baseline(self):
        code,prepared,snapshots=await self.reach_prepare(later_from=2)
        self.assertEqual(code,'owned_history_changed');self.assertEqual(prepared,[])
        self.assertEqual(self.ledger.db.execute('SELECT state FROM tab_transfers').fetchone()[0],'complete')
        self.assertEqual(self.ledger.turn_status('lineage-check'),'failed')
    async def test_later_turn_after_settlement_cannot_become_initial_canonical_root_baseline(self):
        code,prepared,snapshots=await self.reach_prepare(later_from=2,root_match='attribute')
        self.assertEqual(code,'owned_history_changed');self.assertEqual(prepared,[])
    async def test_later_turn_at_model_probe_cannot_reach_prepare(self):
        code,prepared,snapshots=await self.reach_prepare(later_from=3)
        self.assertEqual(code,'owned_history_changed');self.assertEqual(prepared,[])
    async def test_unchanged_saved_last_anchor_allows_normal_preparation_boundary(self):
        code,prepared,snapshots=await self.reach_prepare()
        self.assertEqual(code,'fixture_prepare_reached');self.assertEqual(prepared,[['anchor-b']])
    async def test_new_homepage_without_anchor_keeps_first_turn_behavior(self):
        code,prepared,snapshots=await self.reach_prepare(anchored=False)
        self.assertEqual(code,'fixture_prepare_reached');self.assertEqual(prepared,[[]])

if __name__=='__main__':unittest.main()
