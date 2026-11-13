import assert from 'node:assert/strict';
import { test } from 'node:test';
import { existsSync, readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const source=new URL('../src/backend/tv-app-intents.ts',import.meta.url);
const api=existsSync(source)?await import(pathToFileURL(source.pathname).href):{};
function parser(text){assert.equal(typeof api.parseTvAppIntent,'function','explicit TV app intent parser is not implemented');return api.parseTvAppIntent(text)}
const positive=[
 ['открой Netflix','netflix'],['включи нетфликс','netflix'],['запусти Нетфликс на телевизоре','netflix'],
 ['открой YouTube','youtube'],['включи Ютуб на телевизоре','youtube'],['запусти Ютьюб','youtube'],
 ['Алиса, пожалуйста, открой Ютуб на телевизоре!','youtube'],['включи, пожалуйста, netflix','netflix'],
 ['открой ютуб, пожалуйста','youtube'],['  ЗАПУСТИ NETFLIX НА ТВ.  ','netflix'],
 ['включи ютюб на телике','youtube'],['открой Нетфликс на телеке','netflix'],
 ['открой на телевизоре YouTube','youtube'],['запусти на телике Ютьюб','youtube']
];
test('explicit imperative app requests resolve to the TV scripts independent of speaker room',()=>{
 for(const [text,app] of positive){
  assert.deepEqual(parser(text),{
   app,scriptEntity:'script.living_room_'+app,
   packageId:app==='netflix'?'com.netflix.ninja':'com.google.android.youtube.tv',
   label:app==='netflix'?'Netflix':'YouTube'
  },text);
 }
});
test('questions, negation, music and other controls do not launch TV apps',()=>{
 for(const text of [
  'не включай нетфликс','не открывай ютуб','пожалуйста не запусти Netflix',
  'как открыть Netflix','можешь включить ютуб?',
  'не включай ютюб на телике','как открыть на телеке ютюб','открой на колонке ютюб','открой Youtube?','открой ютуб？',
  'почему не работает Netflix','Netflix','YouTube на телевизоре',
  'выключи Netflix','останови ютуб','пауза на телевизоре','громкость ютуба',
  'включи музыку Netflix','включи песню YouTube','запусти плейлист Ютуб',
  'включи Netflix и выключи свет','не нужно, открой ютуб',
  'открой ютуб на колонке','открой Netflix в студии','открой youtube.com',
  'открой Netflix, не надо','когда я скажу открой Netflix',
  '',null,{},['включи ютуб']
 ])assert.equal(parser(text),null,JSON.stringify(text));
});
test('successful launch calls one exact HA script and confirms fresh powered native app state',async()=>{
 assert.equal(typeof api.executeTvAppIntent,'function','TV app action boundary is not implemented');
 for(const app of ['netflix','youtube']){
  const intent=parser('открой '+app),calls=[];let reads=0;
  const result=await api.executeTvAppIntent(intent,{
   callScript:async entity=>calls.push(entity),
   readNative:async()=>++reads===1?{state:'off',attributes:{app_id:intent.packageId}}:{state:'on',attributes:{app_id:intent.packageId}},
   sleep:async()=>{}
  });
  assert.deepEqual(calls,[intent.scriptEntity]);assert.equal(result.status,'confirmed');assert.equal(reads,2);
 }
});
test('cached activity while off, wrong app, unavailable or missing native state never confirms launch',async()=>{
 assert.equal(typeof api.executeTvAppIntent,'function','TV app action boundary is not implemented');
 const intent=parser('включи Netflix');
 for(const entity of [
  {state:'off',attributes:{app_id:intent.packageId}},
  {state:'unknown',attributes:{app_id:intent.packageId}},
  {state:'unavailable',attributes:{app_id:intent.packageId}},
  {state:'on',attributes:{app_id:'com.google.android.youtube.tv'}},
  {state:'on',attributes:{current_activity:intent.packageId}},
  undefined
 ]){
  let calls=0,reads=0;const result=await api.executeTvAppIntent(intent,{
   callScript:async()=>{calls++},readNative:async()=>{reads++;return entity},sleep:async()=>{}
  });
  assert.equal(result.status,'pending');assert.equal(calls,1);assert.ok(reads>0&&reads<=12);
 }
});
test('uncertain service failure never retries launch or speaks success',async()=>{
 assert.equal(typeof api.executeTvAppIntent,'function','TV app action boundary is not implemented');
 let calls=0,reads=0;
 await assert.rejects(()=>api.executeTvAppIntent(parser('запусти ютуб'),{
  callScript:async()=>{calls++;throw Error('HTTP timeout')},readNative:async()=>{reads++;return {state:'on'}},sleep:async()=>{}
 }),/HTTP timeout/);
 assert.equal(calls,1);assert.equal(reads,0);
});

async function flushMicrotasks(){for(let i=0;i<12;i++)await Promise.resolve()}
for(const slowSuccess of [true,false])test('observation deadline bounds '+(slowSuccess?'slow success':'a never-resolving read'),async(t)=>{
 t.mock.timers.enable({apis:['Date','setTimeout'],now:2000000000000});
 const cleared=t.mock.method(globalThis,'clearTimeout');
 const intent=parser('открой Netflix');
 let calls=0,reads=0,outcome,release;
  const field=new Promise(resolve=>{release=resolve});
  if(slowSuccess)setTimeout(()=>release({state:'on',attributes:{app_id:intent.packageId}}),4500);
  const task=api.executeTvAppIntent(intent,{
   callScript:async()=>{calls++},readNative:async()=>{reads++;return await field}
  }).then(result=>{outcome=result});
  await flushMicrotasks();
  assert.equal(calls,1);assert.equal(reads,1);
  t.mock.timers.tick(3999);await flushMicrotasks();assert.equal(outcome,undefined);
  t.mock.timers.tick(1);await flushMicrotasks();
  try{assert.deepEqual(outcome,{status:'pending'},slowSuccess?'late success must not exceed the budget':'hung read must not exceed the budget')}
  finally{release({state:'on',attributes:{app_id:intent.packageId}});await flushMicrotasks()}
  await task;
  assert.equal(calls,1);assert.equal(reads,1);assert.equal(outcome.status,'pending');
  t.mock.timers.tick(1000);await flushMicrotasks();
 assert.ok(cleared.mock.callCount()>=1,'deadline timers must be cleared after each observation');
});
test('script acknowledgement time consumes the same bounded reply budget and fast success clears its timer',async(t)=>{
 t.mock.timers.enable({apis:['Date','setTimeout'],now:2000000000000});
 const cleared=t.mock.method(globalThis,'clearTimeout');
 const intent=parser('открой YouTube');
 let calls=0,outcome,release;
 const read=new Promise(resolve=>{release=resolve});
 const task=api.executeTvAppIntent(intent,{
  callScript:async()=>{calls++;await new Promise(resolve=>setTimeout(resolve,3000))},
  readNative:async()=>await read
 }).then(result=>{outcome=result});
 await flushMicrotasks();
 t.mock.timers.tick(3000);await flushMicrotasks();
 t.mock.timers.tick(999);await flushMicrotasks();assert.equal(outcome,undefined);
 t.mock.timers.tick(1);await flushMicrotasks();
 try{assert.deepEqual(outcome,{status:'pending'},'an accepted script cannot restart the observation budget')}
 finally{release({state:'on',attributes:{app_id:intent.packageId}});await flushMicrotasks()}
 await task;assert.equal(calls,1);
 const before=cleared.mock.callCount();
 assert.deepEqual(await api.executeTvAppIntent(intent,{
  callScript:async()=>{},readNative:async()=>({state:'on',attributes:{app_id:intent.packageId}})
 }),{status:'confirmed'});
 assert.ok(cleared.mock.callCount()>before,'fast success must clear its outstanding timeout');
});
