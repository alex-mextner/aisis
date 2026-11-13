import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
const transpile=code=>ts.transpileModule(code,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText;
import { AsyncLocalStorage } from 'node:async_hooks';

const matcher=process.env.AISIS_ACTIVE_MATCHER_SOURCE;
assert.ok(matcher,'AISIS_ACTIVE_MATCHER_SOURCE must be the captured actual production matcher or its patched result');
const file=fs.readFileSync(matcher,'utf8');
const sleep=()=>Promise.resolve();
function importedNames(specifier){
const names=new Set();
for(const match of file.matchAll(/import\s+([\s\S]*?)\s+from\s+['"]([^'"]+)['"]/g)){
 if(match[2]!==specifier)continue;
 if(match[1].trim().startsWith('{')){
  for(const name of match[1].slice(1,-1).split(',')){const item=name.trim().replace(/^type\s+/,'');if(item)names.add(item)}
 }else names.add('default');
}
 return names;
}
async function load(current='on',appId='com.netflix.ninja',responseStatus=200,stale=false,native={}){
 const calls=[],reads=[],state={state:current,attributes:native.attributes??{app_id:appId}};
 const fetch=async(url,options={})=>{
  const pathname=new URL(url).pathname;
  if(options.method==='POST'){calls.push({path:pathname,body:JSON.parse(options.body)});return new Response('[]',{status:responseStatus})}
  if(pathname==='/api/states/'+(native.entity??'media_player.televizor')){reads.push(native.entity??'media_player.televizor');return new Response(JSON.stringify(state),{headers:stale?{'x-aisis-cache':'stale'}:{}})}
  if(pathname==='/api/states/sensor.living_room_fireplace'){
   reads.push('sensor.living_room_fireplace');
   const proof={state:'playing',attributes:{owned:true,file_matches:true,repeat:native.repeat??'one',observed_at:new Date().toISOString(),...native.proof}};
   return new Response(JSON.stringify(proof),{headers:native.proofStale?{'x-aisis-cache':'stale'}:{}});
  }
  if(pathname==='/api/states')return new Response('[]');
  return new Response('');
 };
 const context=vm.createContext({console,fetch,Response,AbortSignal,URL,Map,Set,Date,JSON,Number,String,Promise,
  setTimeout:(fn,ms)=>{if(ms>=1000)return setTimeout(fn,ms);queueMicrotask(fn);return 0},clearTimeout:timer=>clearTimeout(timer),
  process:{env:{AISIS_HA_URL:'http://ha.test',AISIS_HA_TOKEN:'test-only-token',AISIS_FAST_EARCON:'off'}}});
 const generic={
  allowCorrection:()=>false,confirmationQuestion:()=>'',describeCorrection:()=>'',eligible:()=>false,endsMidPhrase:()=>false,noteAsked:()=>{},requestCorrection:async()=>null,shadowOnly:()=>true,
  parseEndDialog:()=>null,matchEntityQuery:()=>null,
  composeNumeral:()=>({value:0}),editDistance:()=>99,NUMBER_WORDS:{},normalize:s=>s.toLowerCase().replace(/ё/g,'е').trim(),parseNumbers:()=>[],splitSentences:s=>[s],tokenize:s=>s.split(/\s+/),
  decideCatalog:()=>({tier:'llm'}),isNarrativeVerbForm:()=>false,LIGHT_WORDS:[],OFF_VERBS:['выключ'],ON_VERBS:['включ'],parseUtterance:()=>({tokens:[]}),
  tryMediaControl:async()=>null,tryMusicIntent:async()=>null,parseMediaControl:()=>null,shadowClassify:()=>{},
  DEFAULT_VOLUME_STEP_PCT:10,doneText:()=>'',failureText:()=>'',findSetting:()=>null,getSettings:()=>[],getValue:()=>null,matchSettingQuery:()=>null,postValue:()=>null,queryAnswer:()=>'',refreshSettings:async()=>{},volumeLevels:()=>1,volumeStepPct:()=>10,
  latestAction:()=>null,latestTurn:()=>null,parseRepeat:()=>null,recordTurn:()=>{},
  getLogger:()=>({info:()=>{},debug:()=>{},warn:()=>{},error:()=>{}}),encodeProtobufStruct:x=>x,default:{},
  AsyncLocalStorage,readFileSync:()=>{throw Error('no config file')},writeFileSync:()=>{}
 };
 const cache=new Map();
 async function linker(specifier){
  if(cache.has(specifier))return cache.get(specifier);
  let mod;
  if(specifier==='./tv-app-intents'){
   const code=fs.readFileSync(new URL('../src/backend/tv-app-intents.ts',import.meta.url),'utf8');
   mod=new vm.SourceTextModule(transpile(code),{context,identifier:'tv-app-intents'});
  }else{
   const names=importedNames(specifier);
   mod=new vm.SyntheticModule([...names],function(){for(const name of names)this.setExport(name,generic[name])},{context});
  }
  cache.set(specifier,mod);await mod.link(linker);return mod;
 }
 const entry=new vm.SourceTextModule(transpile(file),{context,identifier:'actual-ha-intents'});
 await entry.link(linker);await entry.evaluate();
 return {calls,reads,match:entry.namespace.matchFastIntent};
}
test('actual matcher routes app before generic device/music recognition, using nonblocking script service',async()=>{
 for(const [phrase,entity,packageId] of [
  ['открой Netflix','script.living_room_netflix','com.netflix.ninja'],
  ['включи Ютуб на телевизоре','script.living_room_youtube','com.google.android.youtube.tv'],
  ['запусти Ютьюб','script.living_room_youtube','com.google.android.youtube.tv']
 ]){
  const api=await load('on',packageId);
  const result=await api.match(phrase,{origin:'voice'});
  assert.deepEqual(api.calls,[{path:'/api/services/script/turn_on',body:{entity_id:entity}}],phrase);
  assert.equal(result.kind,'reply');assert.equal(result.text,null);
 }
});
test('actual matcher preserves service error and stale/off state boundaries',async()=>{
 for(const [status,state,stale] of [[500,'on',false],[202,'on',false],[200,'off',false],[200,'on',true]]){
  const api=await load(state,'com.netflix.ninja',status,stale);
  const result=await api.match('включи Netflix на телевизоре');
  assert.equal(api.calls.length,1);
  assert.equal(api.calls[0].path,'/api/services/script/turn_on');
  assert.equal(result.kind,'reply');assert.equal(typeof result.text,'string');assert.ok(result.text.length);
 }
});

test('actual matcher targets Kodi for fireplace and never dispatches a TV app script',async()=>{
 const marker='fireplace-fastily-1080p.webm';
 for(const phrase of ['открой камин на проекторе','включи камин','запусти на проекторе камин']){
  const api=await load('playing',undefined,200,false,{entity:'media_player.home',attributes:{media_title:marker}});
  const result=await api.match(phrase,{origin:'voice'});
  assert.deepEqual(api.calls,[{path:'/api/services/script/turn_on',body:{entity_id:'script.living_room_fireplace'}}]);
  assert.ok(api.reads.includes('media_player.home'));assert.ok(api.reads.includes('sensor.living_room_fireplace'));assert.ok(api.reads.every(id=>['media_player.home','sensor.living_room_fireplace'].includes(id)));
  assert.equal(result.kind,'reply');assert.equal(result.text,null);
 }
});
test('actual fireplace matcher preserves service failure, stale and wrong-video boundaries',async()=>{
 for(const [status,state,stale,title,repeat='one'] of [[500,'playing',false,'fireplace-fastily-1080p.webm'],[202,'playing',false,'fireplace-fastily-1080p.webm'],[200,'idle',false,'fireplace-fastily-1080p.webm'],[200,'playing',true,'fireplace-fastily-1080p.webm'],[200,'playing',false,'another.webm'],[200,'playing',false,'fireplace-fastily-1080p.webm','off'],[200,'playing',false,'fireplace-fastily-1080p.webm','all']]){
  const api=await load(state,undefined,status,stale,{entity:'media_player.home',attributes:{media_title:title},repeat});
  const result=await api.match('открой камин на проекторе');
  assert.equal(api.calls.length,1);assert.equal(api.calls[0].path,'/api/services/script/turn_on');
  assert.equal(api.calls[0].body.entity_id,'script.living_room_fireplace');
  assert.equal(result.kind,'reply');assert.equal(typeof result.text,'string');assert.ok(result.text.length);
 }
});

test('actual fireplace matcher rejects stale or foreign readiness even with the right playing title',async()=>{
 for(const native of [
  {proof:{owned:false}},{proof:{file_matches:false}},
  {proof:{observed_at:'2000-01-01T00:00:00Z'}},{proofStale:true}
 ]){
  const api=await load('playing',undefined,200,false,{entity:'media_player.home',attributes:{media_title:'fireplace-fastily-1080p.webm'},...native});
  const result=await api.match('включи камин на проекторе');
  assert.equal(api.calls.length,1);assert.equal(result.kind,'reply');assert.equal(typeof result.text,'string');assert.ok(result.text.length);
 }
});
