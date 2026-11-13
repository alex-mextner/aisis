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
async function load(current='on',appId='com.netflix.ninja',responseStatus=200,stale=false){
 const calls=[],state={state:current,attributes:{app_id:appId}};
 const fetch=async(url,options={})=>{
  const pathname=new URL(url).pathname;
  if(options.method==='POST'){calls.push({path:pathname,body:JSON.parse(options.body)});return new Response('[]',{status:responseStatus})}
  if(pathname==='/api/states/media_player.televizor')return new Response(JSON.stringify(state),{headers:stale?{'x-aisis-cache':'stale'}:{}});
  if(pathname==='/api/states')return new Response('[]');
  return new Response('');
 };
 const context=vm.createContext({console,fetch,Response,AbortSignal,URL,Map,Set,Date,JSON,Number,String,Promise,
  setTimeout:fn=>{queueMicrotask(fn);return 1},clearTimeout:()=>{},
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
 return {calls,match:entry.namespace.matchFastIntent};
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
