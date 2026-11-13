import assert from 'node:assert/strict';
import { test } from 'node:test';
import { pathToFileURL } from 'node:url';
const api=await import(pathToFileURL(new URL('../src/backend/tv-app-intents.ts',import.meta.url).pathname).href);
const marker='fireplace-fastily-1080p.webm';
const ready=()=>({state:'playing',attributes:{owned:true,file_matches:true,repeat:'one',observed_at:new Date().toISOString()}});
function parse(text){assert.equal(typeof api.parseFireplaceIntent,'function','fixed projector fireplace parser is not implemented');return api.parseFireplaceIntent(text)}
test('explicit fireplace imperatives always select the fixed projector script',()=>{
 for(const text of ['открой камин на проекторе','включи камин','запусти камин на проекторе','Алиса, пожалуйста, открой камин на проекторе!','открой на проекторе камин','включи, пожалуйста, камин']){
  assert.deepEqual(parse(text),{scriptEntity:'script.living_room_fireplace',label:'камин',marker},text);
  assert.equal(api.parseTvAppIntent(text),null);
 }
});
test('fireplace matcher rejects questions, negation, physical heaters, music and other targets',()=>{
 for(const text of ['как открыть камин на проекторе','не включай камин','почему камин не работает','открой камин?','открой камин на телевизоре','открой камин на телике','включи камин на колонке','включи песню камин','включи отопление камина','включи электрический камин','зажги камин','открой камин в студии','выключи камин','открой https://example.test/fireplace','открой камин и выключи телевизор','камин',null,{},'']){
  assert.equal(parse(text),null,JSON.stringify(text));
 }
});
test('fireplace acceptance requires native playing and fresh owned file/repeat proof',async()=>{
 assert.equal(typeof api.executeFireplaceIntent,'function','fixed projector fireplace action is not implemented');
 for(const title of [marker,'fireplace-fastily-1080p','/home/ultra/KodiLibrary/Ambience/'+marker]){
  const calls=[];let reads=0,proofs=0;const result=await api.executeFireplaceIntent(parse('открой камин на проекторе'),{
   callScript:async entity=>calls.push(entity),
   readNative:async()=>++reads===1?{state:'idle',attributes:{media_title:title}}:{state:'playing',attributes:{media_title:title}},
   readReadiness:async()=>{proofs++;return ready()},sleep:async()=>{}
  });
  assert.deepEqual(calls,['script.living_room_fireplace']);assert.equal(result.status,'confirmed');assert.equal(reads,2);assert.equal(proofs,2);
 }
});
test('power alone, cached identifiers, other videos and absent Kodi state remain pending with no replay',async()=>{
 assert.equal(typeof api.executeFireplaceIntent,'function','fixed projector fireplace action is not implemented');
 for(const state of [
  {state:'on',attributes:{media_title:marker}},{state:'off',attributes:{media_title:marker}},
  {state:'paused',attributes:{media_title:marker}},{state:'buffering',attributes:{media_title:marker}},
  {state:'playing',attributes:{media_title:'another-fireplace.webm'}},
  {state:'playing',attributes:{media_content_id:'/home/ultra/KodiLibrary/Ambience/'+marker}},
  {state:'playing',attributes:{app_id:'com.google.android.youtube.tv'}},undefined]){
  const calls=[];let reads=0;const result=await api.executeFireplaceIntent(parse('включи камин'),{
   callScript:async e=>calls.push(e),readNative:async()=>{reads++;return state},readReadiness:async()=>ready(),sleep:async()=>{}
  });
  assert.equal(result.status,'pending');assert.deepEqual(calls,['script.living_room_fireplace']);assert.ok(reads>0&&reads<=12);
 }
});
test('unknown, unowned, wrong-file, stale or nonlooping metadata cannot confirm fireplace',async()=>{
 const native={state:'playing',attributes:{media_title:marker}};
 const invalid=[
  undefined,{state:'playing',attributes:{}},
  {...ready(),state:'idle'}, {...ready(),state:'unavailable'},
  {...ready(),attributes:{...ready().attributes,owned:false}},
  {...ready(),attributes:{...ready().attributes,file_matches:false}},
  {...ready(),attributes:{...ready().attributes,repeat:'off'}},
  {...ready(),attributes:{...ready().attributes,repeat:'all'}},
  {...ready(),attributes:{...ready().attributes,observed_at:new Date(Date.now()-10000).toISOString()}},
  {...ready(),attributes:{...ready().attributes,observed_at:'not-a-date'}},
  {...ready(),attributes:{...ready().attributes,observed_at:'2099-01-01T00:00:00Z'}}
 ];
 for(const proof of invalid){
  let calls=0;const outcome=await api.executeFireplaceIntent(parse('включи камин'),{
   callScript:async()=>{calls++},readNative:async()=>native,readReadiness:async()=>proof,sleep:async()=>{}
  });
  assert.equal(outcome.status,'pending');assert.equal(calls,1);
 }
});
test('failed fireplace request is not retried and accepted request with lost readback stays pending',async()=>{
 assert.equal(typeof api.executeFireplaceIntent,'function','fixed projector fireplace action is not implemented');
 let writes=0,reads=0;
 await assert.rejects(()=>api.executeFireplaceIntent(parse('включи камин'),{
  callScript:async()=>{writes++;throw Error('uncertain HTTP timeout')},readNative:async()=>{reads++;return undefined},readReadiness:async()=>ready()
 }),/uncertain HTTP timeout/);
 assert.equal(writes,1);assert.equal(reads,0);
 const pending=await api.executeFireplaceIntent(parse('включи камин'),{
  callScript:async()=>{writes++},readNative:async()=>{throw Error('read failed')},readReadiness:async()=>ready()
 });
 assert.equal(writes,2);assert.equal(pending.status,'pending');
});
