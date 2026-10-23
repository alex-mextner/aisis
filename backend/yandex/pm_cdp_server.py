#!/usr/bin/env python3
"""User-requested voice turns in dedicated tabs of the existing pm-owned browser.

No Chrome lifecycle, private ChatGPT HTTP calls, model changes, profile adoption,
recovery enrollment, quota resets, control clearing, or automatic send retries.
The HTTP caller may disappear while a guarded browser operation drains safely.
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit
from aiohttp import web

LOG=logging.getLogger("aisis.pm")

def log_refusal(code,status):
    safe=code if isinstance(code,str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}",code) else "internal_error"
    LOG.warning("pm_turn_refused code=%s status=%d",safe,status if type(status) is int and 100<=status<=599 else 503)

class Refusal(Exception):
    def __init__(self, code, status=503):
        self.code, self.status = code, status
        super().__init__(code)

class Ledger:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        self.db.executescript('''
          PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS sessions(station TEXT PRIMARY KEY,target TEXT NOT NULL,url TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY,station TEXT NOT NULL,digest TEXT NOT NULL,status TEXT NOT NULL);
        ''')
        with self.db:
            columns={r[1] for r in self.db.execute('PRAGMA table_info(sessions)')}
            for column in ('anchor_turn','anchor_digest'):
                if column not in columns: self.db.execute('ALTER TABLE sessions ADD COLUMN '+column+' TEXT')
            self.db.execute("UPDATE turns SET status='uncertain' WHERE status IN ('reserved','dispatching')")
        path.chmod(0o600)
    def session(self, station):
        row=self.db.execute('SELECT target,url,anchor_turn,anchor_digest FROM sessions WHERE station=?',(station,)).fetchone()
        return dict(zip(('target','url','anchor_turn','anchor_digest'),row)) if row else None
    def remember(self, station, target, url, anchor_turn=None, anchor_digest=None):
        with self.db:
            owner=self.db.execute('SELECT station FROM sessions WHERE target=? AND station!=?',(target,station)).fetchone()
            if owner: raise Refusal('tab_owned_by_another_station',409)
            self.db.execute('''INSERT INTO sessions(station,target,url,anchor_turn,anchor_digest) VALUES(?,?,?,?,?)
                ON CONFLICT(station) DO UPDATE SET target=excluded.target,url=excluded.url,
                anchor_turn=COALESCE(excluded.anchor_turn,sessions.anchor_turn),
                anchor_digest=COALESCE(excluded.anchor_digest,sessions.anchor_digest)''',
                (station,target,url,anchor_turn,anchor_digest))
    def turn_status(self, turn):
        row=self.db.execute('SELECT status FROM turns WHERE id=?',(turn,)).fetchone()
        return row[0] if row else None
    def reserve(self, payload):
        station,turn=payload['station_id'],payload['turn_id']
        digest=hashlib.sha256(payload['text'].encode()).hexdigest()
        with self.db:
            if self.db.execute('SELECT 1 FROM turns WHERE id=?',(turn,)).fetchone():
                raise Refusal('duplicate_turn',409)
            if self.db.execute("SELECT 1 FROM turns WHERE station=? AND status IN ('uncertain','dispatching','reserved')",(station,)).fetchone():
                raise Refusal('station_uncertain',409)
            self.db.execute('INSERT INTO turns VALUES(?,?,?,?)',(turn,station,digest,'reserved'))
    def mark(self, turn, status):
        with self.db: self.db.execute('UPDATE turns SET status=? WHERE id=?',(status,turn))


def validate_owned_root(snapshot,session):
    """The observed local-chatgpt root is authority only with durable turn lineage.

    This is an adapter-only variant; the shared Guardian root helper stays intact.
    New submitted replies are instead bound by match_reply's exact prompt/new ID.
    """
    if snapshot.get('root_match')!='local-chatgpt': return
    if not session or session.get('url')!=snapshot.get('chat') or not session.get('anchor_turn') or not session.get('anchor_digest'):
        raise Refusal('local_root_unbound')
    anchors=[t for t in snapshot.get('turns',[]) if t.get('id')==session['anchor_turn']]
    if len(anchors)!=1 or hashlib.sha256(anchors[0].get('user','').encode()).hexdigest()!=session['anchor_digest']:
        raise Refusal('local_root_lineage_changed')

def match_reply(baseline, turns, prompt):
    """A UI turn group contains one user bubble and its assistant response."""
    if len(turns)<len(baseline) or [t['id'] for t in turns[:len(baseline)]] != [t['id'] for t in baseline]:
        raise Refusal('conversation_changed')
    if any(a.get('user') != b.get('user') or a.get('assistant') != b.get('assistant') or a.get('assistant_id') != b.get('assistant_id') for a,b in zip(baseline,turns)):
        raise Refusal('history_changed')
    added=turns[len(baseline):]
    if not added: return None
    if len(added)!=1 or added[0].get('user')!=prompt:
        raise Refusal('prompt_identity_changed')
    turn=added[0]
    if turn.get('state') in ('cancelled','failed'): raise Refusal('generation_failed')
    if turn.get('state')!='complete' or not turn.get('assistant','').strip(): return None
    return turn['id'],turn['assistant'].strip()

class PromptService:
    def __init__(self,driver,ledger,allowed):
        self.driver,self.ledger,self.allowed=driver,ledger,set(allowed)
        self.lock=asyncio.Lock()
        self.pending=set()
    def validate(self,payload):
        if not isinstance(payload,dict) or set(payload)-{'station_id','text','turn_id','conversation_id'}:
            raise Refusal('invalid_request',400)
        for key,limit in [('station_id',256),('turn_id',128),('text',16000)]:
            value=payload.get(key)
            if not isinstance(value,str) or not value.strip() or len(value)>limit:
                raise Refusal('invalid_'+key,400)
        if payload['station_id'] not in self.allowed: raise Refusal('station_not_allowed',403)
        if payload.get('conversation_id') is not None and not isinstance(payload['conversation_id'],str):
            raise Refusal('invalid_conversation',400)
    @property
    def ready(self):
        return bool(getattr(self.driver,'ready',True))
    async def submit(self,payload):
        self.validate(payload)
        if not self.ready: raise Refusal('browser_disconnected')
        # Atomic admission: there is no await between this check and ownership.
        # Busy callers are never queued behind a shielded browser operation.
        if self.pending: raise Refusal('browser_busy',429)
        task=asyncio.create_task(self._run(dict(payload)))
        self.pending.add(task)
        # Always retrieve exceptions even if the HTTP client disconnects.
        def done(t):
            self.pending.discard(t)
            if not t.cancelled(): t.exception()
        task.add_done_callback(done)
        return await asyncio.shield(task)
    async def _run(self,payload):
        async with self.lock:
            session=self.ledger.session(payload['station_id'])
            if payload.get('conversation_id') and (not session or payload['conversation_id']!=session['url']):
                raise Refusal('conversation_not_owned',409)
            self.ledger.reserve(payload)
            dispatched=False
            def remember(target,url,anchor_turn=None,anchor_digest=None):
                self.ledger.remember(payload['station_id'],target,url,anchor_turn,anchor_digest)
            def dispatching():
                nonlocal dispatched
                dispatched=True
                self.ledger.mark(payload['turn_id'],'dispatching')
            try:
                result=await self.driver.ask(payload,session,remember,dispatching)
                self.ledger.mark(payload['turn_id'],'complete')
                return result
            except BaseException as error:
                self.ledger.mark(payload['turn_id'],'uncertain' if dispatched else 'failed')
                if isinstance(error,Refusal): log_refusal(error.code,error.status)
                elif isinstance(error,asyncio.CancelledError): log_refusal('browser_task_cancelled',503)
                else: log_refusal('browser_operation_failed',503)
                raise
    async def drain(self):
        while self.pending:
            await asyncio.gather(*(asyncio.shield(t) for t in list(self.pending)),return_exceptions=True)

# This is an observed-UI adapter, separate from the generic HA fixture Contract.
# conversation_root.js is read from the installed shared pm package, unchanged.
DOM = r'''({action='snapshot', expected=null, prompt='', probe=null, prepared=null, deadline=0, expected_chat='', allow_created_chat=false}) => {
/*@conversation-root*/
// Reject a human-navigated tab before reading any conversation content.
const currentCanonical=(location.origin+location.pathname).replace(/\/$/,'');
if(expected_chat&&currentCanonical!==expected_chat.replace(/\/$/,'')&&!(allow_created_chat&&/^https:\/\/chatgpt\.com\/c\/[A-Za-z0-9_-]+$/.test(currentCanonical)))throw new Error('AISIS_OWNED_URL_CHANGED');
const one=s=>{const a=all(s).filter(visible);return a.length===1?a[0]:null};
const composerSelector='[contenteditable="true"][data-composer-markdown][role="textbox"][aria-label="Ask ChatGPT"]';
const triggerSelector='button[aria-label="Select ChatGPT model"][data-codex-intelligence-trigger="true"]';
const signature=el=>el?JSON.stringify([el.textContent,el.getAttribute('data-selected-reasoning-effort'),el.getAttribute('aria-label')]):'';
const timely=()=>performance.timeOrigin+performance.now()<deadline;
const composer=one(composerSelector),trigger=one(triggerSelector);
const canonical=()=>{const m=location.pathname.match(/^\/c\/([A-Za-z0-9_-]+)\/?$/);return location.origin==='https://chatgpt.com'?(m?'https://chatgpt.com/c/'+m[1]:location.pathname==='/'?'https://chatgpt.com/':''):''};
// Observed on the first AISIS turn: the public /c URL becomes permanent while
// the root retains local-chatgpt:<UUID>. Use this shape only in this adapter;
// Python validates prior durable turn lineage before any subsequent mutation.
const ownedRoot=()=>{
 const found=conversationRoot();
 if(found.root||found.error!=='root-attribute-mismatch')return found;
 const candidates=all(rootSelector).filter(visible);
 if(candidates.length!==1)return found;
 const selection=candidates[0].getAttribute('data-chatgpt-conversation-selection-target');
 if(!/^local-chatgpt:[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(selection))return found;
 return {...found,root:candidates[0],error:'',match:'local-chatgpt',selection};
};
const read=(ownedMenu=false)=>{
 const chat=canonical(),found=ownedRoot(),root=found.root;
 const dialogs=all('[role="dialog"],[role="alertdialog"]').filter(visible);
 const menus=all('[role="menu"]').filter(visible);
 const challenge=all('iframe[src*="captcha"],iframe[src*="challenges.cloudflare"],a[href="/auth/login"]').some(visible)||/verify you are human|checking your browser|just a moment/i.test(document.title);
 const alerts=all('[role="alert"]').filter(el=>visible(el)&&!el.closest('[data-turn-key]'));
 const quota=alerts.some(el=>/(?:GPT-6|Astra|GPT-5\.6 Sol|\bSol\b).*(?:limit|quota)|(?:limit|quota).*(?:GPT-6|Astra|GPT-5\.6 Sol|\bSol\b)/i.test(el.textContent));
 const running=all('button[aria-label="Stop"]').some(visible)||all('[data-talvt-turn-state="in_progress"]',root||document).some(visible);
 const attachments=all('[data-testid="attachment"],[data-testid="attachment-preview"],input[type="file"]').some(el=>visible(el)&&(el.tagName!=='INPUT'||el.files?.length));
 const turns=[];
 let ambiguous=!chat||!composer||!trigger||!one('button[aria-label="Open profile menu"]')||dialogs.length>0||(!ownedMenu&&menus.length>0)||attachments;
 if(chat!=='https://chatgpt.com/'&&!root)ambiguous=true;
 if(chat==='https://chatgpt.com/'&&all('[data-turn-key]').length)ambiguous=true;
 if(root){
  for(const t of all('[data-turn-key]',root)){
   const id=t.getAttribute('data-turn-key');
   const users=all('[data-user-message-bubble="true"]',t);
   const uuid=/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
   const units=all('[data-chatgpt-search-unit-key$=":assistant"]',t);
   let assistant='',assistant_id='';
   const copies=all('[data-markdown-copy]',t).filter(el=>!el.closest('[data-testid="chatgpt-subagent-activity"],[data-user-message-bubble="true"]')&&!el.parentElement?.closest('[data-markdown-copy]'));
   if(units.length>1||copies.length>1)ambiguous=true;
   if(units.length===1){
    const ids=[...new Set((units[0].getAttribute('data-chatgpt-search-message-ids')||'').trim().split(/\s+/).filter(Boolean))];
    if(ids.length!==1||!uuid.test(ids[0]))ambiguous=true;
    else assistant_id=ids[0];
    // Observed final answer body. Accessible H4 role heading is a sibling,
    // so extraction excludes it structurally without text substitution.
    const bodies=all('[data-markdown-text-style="assistant-message"]',units[0]).filter(el=>!el.parentElement?.closest('[data-markdown-text-style="assistant-message"]')&&!el.closest('[data-testid="chatgpt-subagent-activity"]'));
    if(bodies.length>1)ambiguous=true;
    if(bodies.length===1){
     const message=bodies[0].closest('[data-chatgpt-selection-message-id]');
     const conversation=bodies[0].closest('[data-chatgpt-selection-conversation-id]');
     if(!assistant_id||message?.getAttribute('data-chatgpt-selection-message-id')!==assistant_id||found.match==='local-chatgpt'&&conversation?.getAttribute('data-chatgpt-selection-conversation-id')!==found.selection)ambiguous=true;
     else assistant=bodies[0].innerText;
    }
   }
   if(!assistant&&copies.length===1)assistant=copies[0].innerText;
   const states=all('[data-talvt-turn-state]',t);
   if(!uuid.test(id)||users.length!==1||states.length>1)ambiguous=true;
   turns.push({id,user:users.length===1?users[0].textContent:'',assistant,assistant_id,state:thinkingFailure(t).count?'failed':states.length===1?states[0].getAttribute('data-talvt-turn-state'):'unknown'});
  }
  if(new Set(turns.map(t=>t.id)).size!==turns.length)ambiguous=true;
 }
 return {url:location.href,chat,turns,running,challenge,quota,ambiguous,draft:composer?.textContent||'',signature:signature(trigger),root_match:found.match||'',root_selection:root?.getAttribute('data-chatgpt-conversation-selection-target')||'',root_error:found.error||''};
};
if(action==='snapshot')return read();
if(action==='probe')return {url:location.href,trigger,signature:signature(trigger),model:expected.model,effort:expected.effort,at:performance.timeOrigin+performance.now()};
const same=(a,b)=>a&&b&&a.url===b.url&&a.chat===b.chat&&JSON.stringify(a.turns)===JSON.stringify(b.turns)&&a.signature===b.signature&&a.root_selection===b.root_selection;
const safe=(o,owned=false)=>!o.running&&!o.challenge&&!o.quota&&!o.ambiguous&&(owned?o.draft===prompt:!o.draft);
const verified=()=>probe&&probe.url===location.href&&probe.trigger===trigger&&probe.signature===signature(trigger)&&performance.timeOrigin+performance.now()-probe.at<30000&&(probe.model==='Astra'||probe.model==='Sol'&&probe.effort==='Extra High');
if(action==='open-model-menu'||action==='open-full-model-menu'){
 const full=action==='open-full-model-menu',o=read(full);
 if(!timely()||!safe(o)||!same(o,expected))return false;
 let control=trigger;
 const menus=all('[role="menu"]').filter(visible);
 if(full){
  if(menus.length!==1)return false;
  const items=all('[role="menuitem"]',menus[0]).filter(visible).filter(el=>el.getAttribute('aria-label')==='Select model'||el.textContent.trim()==='Select model');
  if(items.length!==1)return false;
  control=items[0];
 }else if(menus.length)return false;
 if(!control?.isConnected||!visible(control)||control.disabled||control.getAttribute('aria-disabled')==='true'||getComputedStyle(control).pointerEvents==='none')return false;
 const rect=control.getBoundingClientRect();
 if(rect.width<=0||rect.height<=0)return false;
 const top=document.elementFromPoint(rect.x+rect.width/2,rect.y+rect.height/2);
 if(!top||top!==control&&!control.contains(top)||!timely()||!same(read(full),expected))return false;
 // One exact, unobstructed UI control; no rAF actionability wait on hidden tabs.
 // This only opens model menus and never chooses a model or effort.
 control.click();return true;
}
if(action==='prepare'){
 const o=read(),result={ok:false,url:location.href,composer,trigger,root:ownedRoot().root,probe,baseline:o};
 if(!timely()||!safe(o)||!same(o,expected)||!verified())return result;
 composer.focus();
 const selection=getSelection(),range=document.createRange();range.selectNodeContents(composer);selection.removeAllRanges();selection.addRange(range);
 if(!timely()||!safe(read())||!same(read(),expected)||!verified()||document.activeElement!==composer)return result;
 result.ok=document.execCommand('insertText',false,prompt);return result;
}
if(action==='send'){
 const o=read(),send=one('button[aria-label="Send"]');
 if(!prepared?.ok||!timely()||!verified()||prepared.url!==location.href||prepared.composer!==composer||prepared.trigger!==trigger||prepared.root!==ownedRoot().root||!safe(o,true)||!same({...o,draft:''},prepared.baseline)||!send||send.disabled||send.getAttribute('aria-disabled')==='true')return false;
 send.click();return true;
}
return false;
}'''

class CDPDriver:
    def __init__(self,env):
        from agenttools_chatgpt.core import Store, Guardian, protected_dispatch
        from agenttools_chatgpt.state import state_path
        from agenttools_chatgpt import scripts
        self.env=env
        self.disconnected=asyncio.Event()
        self.workspace=env.get('AISIS_PM_WORKSPACE','home')
        explicit=env.get('AISIS_PM_GUARDIAN_STATE')
        path=state_path(self.workspace,Path(explicit) if explicit else None)
        if not path.is_file(): raise Refusal('guardian_state_missing')
        self.store=Store(path,account=self.workspace)
        self.guardian=Guardian(self.store,apply=False)
        self.dispatch=protected_dispatch
        self.script=DOM.replace('/*@conversation-root*/',(Path(scripts.__file__).parent/'conversation_root.js').read_text())
        self.endpoint=env.get('AISIS_PM_CDP','http://127.0.0.1:9226')
        self.initial_target=env.get('AISIS_PM_INITIAL_TARGET','')
        self.initial_used=False
        self.reply_timeout=float(env.get('AISIS_PM_REPLY_TIMEOUT','170'))
        self.max_tabs=int(env.get('AISIS_PM_MAX_TABS','6'))
    @property
    def ready(self):
        return hasattr(self,'browser') and self.browser.is_connected() and not self.disconnected.is_set()
    async def start(self):
        from agenttools_chatgpt.live import validate_cdp, TransportGuard
        from agenttools_chatgpt.startup import verify_browser,ProcFilesystem
        from playwright.async_api import async_playwright
        self.endpoint=validate_cdp(self.endpoint)
        port=urlsplit(self.endpoint).port
        await asyncio.to_thread(verify_browser,self.workspace,ProcFilesystem(),port=port)
        self.playwright=await async_playwright().start()
        self.browser=await self.playwright.chromium.connect_over_cdp(self.endpoint,timeout=10000,no_defaults=True)
        self.browser.on('disconnected',lambda *_:self.disconnected.set())
        if len(self.browser.contexts)!=1: raise Refusal('browser_context_ambiguous')
        self.context=self.browser.contexts[0]
        self.transport=TransportGuard(self.context,self.store)
    async def close(self):
        if hasattr(self,'transport'): self.transport.close()
        # Stopping this Playwright connection does not close the user's Chrome.
        if hasattr(self,'playwright'): await self.playwright.stop()
    async def target(self,page):
        session=await self.context.new_cdp_session(page)
        try: return (await session.send('Target.getTargetInfo'))['targetInfo']['targetId']
        finally: await session.detach()
    async def new_background_page(self):
        # Native Chrome tab creation keeps the user's current tab foregrounded.
        # A failed/uncertain creation is never retried here or closed implicitly.
        session=await self.browser.new_browser_cdp_session()
        try:
            created=await session.send('Target.createTarget',{'url':'about:blank','background':True})
        finally:
            await session.detach()
        identity=created.get('targetId')
        if not identity: raise Refusal('background_tab_creation_uncertain')
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            for page in self.context.pages:
                if not page.is_closed() and await self.target(page)==identity:
                    return page,identity
            await asyncio.sleep(.05)
        raise Refusal('background_tab_creation_uncertain')
    async def page_for(self,session,remember):
        target=(session or {}).get('target')
        if target and target==self.initial_target: self.initial_used=True
        if not target and self.initial_target and not self.initial_used:
            target=self.initial_target; self.initial_used=True
        expected=(session or {}).get('url','https://chatgpt.com/')
        pages=[p for p in self.context.pages if not p.is_closed()]
        if target:
            matches=[]
            for page in pages:
                if await self.target(page)==target: matches.append(page)
            exact=[p for p in pages if p.url.rstrip('/')==expected.rstrip('/')]
            if len(matches)>1 or len(exact)>1: raise Refusal('owned_tab_ambiguous')
            if matches:
                page=matches[0]
                # An existing target navigated by a human must never be overwritten.
                if page.url.rstrip('/')!=expected.rstrip('/'): raise Refusal('owned_tab_navigated')
                remember(target,expected)
                return page,target
            # Chrome restart changes target IDs. Only this station's durable,
            # permanent URL proves ownership; a lost homepage cannot be recovered.
            if not session or not re.fullmatch(r'https://chatgpt\.com/c/[A-Za-z0-9_-]+',expected):
                raise Refusal('owned_tab_missing')
            if exact:
                page=exact[0]
                target=await self.target(page)
                remember(target,expected)
                return page,target
        if len(pages)>=self.max_tabs: raise Refusal('tab_capacity')
        page,target=await self.new_background_page()
        # Persist the new target before navigation. A failed navigation stays open,
        # and its mismatched URL is refused on the next request, never auto-retried.
        remember(target,expected)
        try: await page.goto(expected,wait_until='domcontentloaded',timeout=30000)
        except Exception: raise Refusal('navigation_failed') from None
        return page,target
    async def snapshot(self,page,expected_chat=None,allow_created_chat=False):
        try:
            result=await asyncio.wait_for(page.evaluate(self.script,{'expected_chat':expected_chat or '', 'allow_created_chat':allow_created_chat}),timeout=5)
        except Exception as error:
            if 'AISIS_OWNED_URL_CHANGED' in str(error): raise Refusal('conversation_changed') from None
            raise
        if not isinstance(result,dict): raise Refusal('unknown_ui')
        return result
    def record(self,snapshot,db=None):
        from agenttools_chatgpt.core import Observation
        turns=snapshot.get('turns',[])
        latest=turns[-1]['id'] if turns else ''
        self.guardian.record_safety(Observation(chat=snapshot.get('chat',''),turn=latest,generation=latest,challenge=snapshot.get('challenge',False),quota=snapshot.get('quota',False)),db)
    def check(self,snapshot,*,draft='',session=None):
        self.record(snapshot)
        validate_owned_root(snapshot,session)
        if snapshot.get('challenge'): raise Refusal('human_attention_required')
        if snapshot.get('quota'): raise Refusal('quota_blocked',429)
        if snapshot.get('ambiguous') or snapshot.get('running') or snapshot.get('draft')!=draft:
            raise Refusal('ui_busy')
    async def model_probe(self,page,snapshot,session=None):
        """Read full selected-model UI; a generic Pro label is never evidence."""
        trigger=page.locator('button[aria-label="Select ChatGPT model"][data-codex-intelligence-trigger="true"]')
        if await trigger.count()!=1: raise Refusal('model_unknown')
        selected=[];effort=[]
        if page.url!=snapshot['url']: raise Refusal('conversation_changed')
        opened=await asyncio.wait_for(page.evaluate(self.script,{'action':'open-model-menu','expected_chat':snapshot['chat'],'expected':snapshot,'deadline':(time.time()+3)*1000}),timeout=5)
        if opened is not True: raise Refusal('model_menu_unsafe')
        try:
            full_opened=False
            for _ in range(30):
                selected=[t.strip() for t in await page.locator('[role="menu"] [role="menuitemradio"][aria-checked="true"][data-model-selected="true"]').all_text_contents()]
                effort=[t.strip() for t in await page.locator('[role="menu"] [role="status"]').all_text_contents()]
                if selected: break
                full=page.get_by_role('menuitem',name='Select model',exact=True)
                if not full_opened and await full.count()==1:
                    opened=await asyncio.wait_for(page.evaluate(self.script,{'action':'open-full-model-menu','expected_chat':snapshot['chat'],'expected':snapshot,'deadline':(time.time()+3)*1000}),timeout=5)
                    if opened is True: full_opened=True
                await asyncio.sleep(.05)
        finally:
            if page.url==snapshot['url']: await page.keyboard.press('Escape')
        if len(selected)!=1: raise Refusal('model_unknown')
        model={'GPT-6':'Astra','GPT-5.6 Sol':'Sol'}.get(selected[0])
        if not model: raise Refusal('model_unknown')
        if model=='Sol' and effort!=['Extra High, 4 of 5.']: raise Refusal('effort_unknown')
        current=await self.snapshot(page,expected_chat=snapshot['chat'])
        self.check(current,session=session)
        if current['chat']!=snapshot['chat'] or current['turns']!=snapshot['turns']:
            raise Refusal('conversation_changed')
        handle=await page.evaluate_handle(self.script,{'action':'probe','expected_chat':snapshot['chat'],'expected':{'model':model,'effort':'Extra High' if model=='Sol' else ''}})
        return model,('Extra High' if model=='Sol' else ''),handle,current
    async def ask(self,payload,session,remember,dispatching):
        if not self.browser.is_connected(): raise Refusal('browser_disconnected')
        # Check shared stops before creating any tab or changing a composer.
        expected=(session or {}).get('url','https://chatgpt.com/')
        async with self.store.async_guard() as db:
            if not self.store._api_allowed(db,expected,'','',False): raise Refusal('guardian_stopped')
        page,target=await self.page_for(session,remember)
        await self.transport.seed(page)
        try: await page.locator('[contenteditable="true"][data-composer-markdown][role="textbox"][aria-label="Ask ChatGPT"]').wait_for(state='visible',timeout=15000)
        except Exception: raise Refusal('composer_unavailable') from None
        initial=await self.snapshot(page,expected_chat=expected)
        self.check(initial,session=session)
        model,effort,probe,initial=await self.model_probe(page,initial,session=session)
        prepared=None
        chat=initial['chat']
        if not chat: raise Refusal('unsafe_origin')
        try:
            async with self.store.async_guard() as db:
                self.record(initial,db)
                if not self.store._api_allowed(db,chat,model,effort,True): raise Refusal('guardian_stopped')
                prepared=await self.dispatch(page.evaluate_handle(self.script,{'action':'prepare','expected_chat':chat,'expected':initial,'prompt':payload['text'],'probe':probe,'deadline':(time.time()+5)*1000}))
                ok=await (await prepared.get_property('ok')).json_value()
                if ok is not True: raise Refusal('preparation_refused')
            await page.locator('button[aria-label="Send"]').wait_for(state='visible',timeout=10000)
            async with self.store.async_guard() as db:
                current=await self.snapshot(page,expected_chat=chat)
                self.record(current,db)
                if not self.store._api_allowed(db,chat,model,effort,True): raise Refusal('guardian_stopped')
                # Mark before invocation; a renderer exception could follow a real click.
                dispatching()
                try:
                    sent=await self.dispatch(page.evaluate(self.script,{'action':'send','expected_chat':chat,'prompt':payload['text'],'probe':probe,'prepared':prepared,'deadline':(time.time()+5)*1000}))
                    if sent is not True: raise Refusal('send_refused')
                except BaseException:
                    self.store.stop(chat,'AISIS dispatch uncertain',db)
                    raise
            deadline=time.monotonic()+self.reply_timeout
            stable=None;since=0;submitted_id=None;permanent=None
            while time.monotonic()<deadline:
                current=await self.snapshot(page,expected_chat=permanent or chat,allow_created_chat=chat=='https://chatgpt.com/' and permanent is None)
                self.record(current)
                if current['challenge']: raise Refusal('human_attention_required')
                if current['quota']: raise Refusal('quota_blocked',429)
                nowchat=current['chat']
                if chat!='https://chatgpt.com/' and nowchat!=chat: raise Refusal('conversation_changed')
                if chat=='https://chatgpt.com/' and nowchat.startswith('https://chatgpt.com/c/'):
                    if permanent is not None and permanent!=nowchat: raise Refusal('conversation_changed')
                    permanent=nowchat;remember(target,permanent)
                if current['ambiguous']:
                    # SPA creation may show a user turn before URL/root settles.
                    # Read-only polling cannot dispatch again or accept an unclear reply.
                    await asyncio.sleep(.25)
                    continue
                reply=match_reply(initial['turns'],current['turns'],payload['text'])
                if reply and not current['running']:
                    turn,text=reply
                    generation=current['turns'][-1].get('assistant_id') or turn
                    identity=(turn,generation)
                    if submitted_id and identity!=submitted_id: raise Refusal('assistant_identity_changed')
                    submitted_id=identity
                    complete=(turn,generation,text)
                    if stable!=complete: stable=complete;since=time.monotonic()
                    elif time.monotonic()-since>=1.5:
                        resultchat=nowchat if nowchat.startswith('https://chatgpt.com/c/') else permanent
                        if not resultchat: raise Refusal('permanent_chat_missing')
                        remember(target,resultchat,turn,hashlib.sha256(payload['text'].encode()).hexdigest())
                        return {'text':text,'conversation_id':resultchat}
                else: stable=None;since=0
                await asyncio.sleep(.5)
            raise Refusal('reply_timeout',504)
        finally:
            if prepared is not None: await prepared.dispose()
            await probe.dispose()


def create_app(service,token):
    if not isinstance(token,str) or len(token)<24: raise ValueError('AISIS_PM_TOKEN must have 24+ characters')
    def authorize(request):
        if not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+token):
            raise web.HTTPUnauthorized()
    async def health(request):
        return web.json_response({'service':'aisis-pm-cdp','ready':service.ready,'automatic_retries':False,'pending':len(service.pending)},status=200 if service.ready else 503)
    async def turn(request):
        authorize(request)
        try:
            payload=await request.json()
            result=await service.submit(payload)
            return web.json_response(result)
        except Refusal as error:
            log_refusal(error.code,error.status)
            return web.json_response({'error':error.code},status=error.status)
        except (json.JSONDecodeError,UnicodeDecodeError): return web.json_response({'error':'invalid_json'},status=400)
        except asyncio.CancelledError: raise
        except Exception: return web.json_response({'error':'browser_operation_failed'},status=503)
    async def cleanup(app): await service.drain()
    app=web.Application(client_max_size=128*1024)
    app.router.add_get('/health',health)
    app.router.add_post('/turn',turn)
    app.on_cleanup.append(cleanup)
    return app

async def wait_for_stop_or_disconnect(stop,disconnected):
    tasks=[asyncio.create_task(stop.wait()),asyncio.create_task(disconnected.wait())]
    try:
        await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        if disconnected.is_set() and not stop.is_set(): raise Refusal('browser_disconnected')
    finally:
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

async def main():
    import signal
    env=dict(os.environ)
    allowed={x.strip() for x in env.get('AISIS_ALLOWED_STATIONS','').split(',') if x.strip()}
    if not allowed: raise SystemExit('AISIS_ALLOWED_STATIONS is required')
    token=env.get('AISIS_PM_TOKEN','')
    if len(token)<24: raise SystemExit('AISIS_PM_TOKEN must have 24+ characters')
    # Separate local service ledger, never a replacement for Guardian state.
    ledger=Ledger(env.get('AISIS_PM_STATE_DB',str(Path.home()/'.local/state/aisis/pm-turns.sqlite')))
    driver=CDPDriver(env)
    if driver.initial_target and ledger.db.execute('SELECT 1 FROM sessions WHERE target=?',(driver.initial_target,)).fetchone():
        driver.initial_used=True
    stop=asyncio.Event()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM): loop.add_signal_handler(sig,stop.set)
    runner=None
    try:
        await driver.start()
        service=PromptService(driver,ledger,allowed)
        runner=web.AppRunner(create_app(service,token))
        await runner.setup()
        await web.TCPSite(runner,'127.0.0.1',int(env.get('AISIS_PM_PORT','31118'))).start()
        await wait_for_stop_or_disconnect(stop,driver.disconnected)
    finally:
        if runner: await runner.cleanup()
        await driver.close()
        for sig in (signal.SIGINT,signal.SIGTERM): loop.remove_signal_handler(sig)

if __name__=='__main__': asyncio.run(main())
