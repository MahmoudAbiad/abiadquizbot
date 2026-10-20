// Node-only logic test for webapp/runner/game.js (no browser, no network, no new deps).
// Stubs the DOM/canvas/fetch and drives the real game.js through error and lifecycle paths.
import fs from 'node:fs';import os from 'node:os';import path from 'node:path';import assert from 'node:assert/strict';
import {pathToFileURL,fileURLToPath} from 'node:url';
const root=path.join(path.dirname(fileURLToPath(import.meta.url)),'..','webapp','runner');
const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'runner-ui-'));
fs.writeFileSync(path.join(tmp,'package.json'),'{"type":"module"}');
for(const f of ['game.js','audio.js'])fs.copyFileSync(path.join(root,f),path.join(tmp,f));
const gameUrl=pathToFileURL(path.join(tmp,'game.js')).href;

let loads=0;
const tick=()=>new Promise(r=>setImmediate(r));
function el(){const e={hidden:false,textContent:'',disabled:false,style:{},dataset:{},clientWidth:390,clientHeight:844,
  classList:{toggle(){},add(){},remove(){}},append(){},replaceChildren(){},setAttribute(){},addEventListener(){},setPointerCapture(){},
  querySelectorAll:()=>[],getContext:()=>new Proxy({},{get:(t,k)=>k in t?t[k]:k==='createLinearGradient'?()=>({addColorStop(){}}):()=>{},set:(t,k,v)=>(t[k]=v,true)})};return e;}

async function scenario(respond){
  const els=new Map(),handlers={},calls=[];let queue=null,now=0;
  const get=id=>{if(!els.has(id))els.set(id,el());return els.get(id);};
  globalThis.document={getElementById:get,querySelectorAll:()=>[],createElement:()=>el(),addEventListener(t,f){(handlers['doc:'+t]??=[]).push(f);},hidden:false};
  globalThis.window=globalThis;globalThis.addEventListener=(t,f)=>{(handlers[t]??=[]).push(f);};
  globalThis.localStorage={getItem:()=>null,setItem(){}};globalThis.location={search:'?quiz_id=q'};
  globalThis.devicePixelRatio=1;globalThis.Image=function(){return el();};
  globalThis.requestAnimationFrame=f=>{queue=f;return 1;};globalThis.cancelAnimationFrame=()=>{queue=null;};
  globalThis.Telegram={WebApp:{initData:'x',ready(){},expand(){},disableVerticalSwipes(){},close(){}}};
  Math.random=()=>0.2;
  globalThis.fetch=async(url,opts)=>{const op=String(url).split('/').pop();const body=opts?.body?JSON.parse(opts.body):{};calls.push(op);
    const r=await respond(op,body,calls.filter(c=>c===op).length);if(r==='network')throw new TypeError('offline');
    return {ok:r.status<400,status:r.status,json:async()=>r.body};};
  await import(gameUrl+'?s='+(++loads));await tick();
  const pump=async(until,max=600)=>{for(let i=0;i<max&&!until();i++){if(queue){const f=queue;queue=null;now+=40;f(now);}await tick();}};
  return {els,handlers,calls,pump,click:async id=>{await get(id).onclick();await tick();},fire:(t,e={})=>(handlers[t]||[]).forEach(f=>f(e)),raf:()=>queue!==null};
}
const ok=(body)=>({status:200,body});
const base=(op,count)=>({config:ok({title:'t',can_edit:false,settings:{lives:1,version:0},questions:[]}),
  start:ok({round_id:'r',version:0,lives:1,total:2}),
  question:ok({text:'س',options:[{id:0,text:'a'},{id:1,text:'b'},{id:2,text:'c'}],seconds:30,remaining_seconds:30,number:1,total:2,version:0}),
  answer:ok({correct:true,correct_text:'a',explanation:'e',lives:1,score:100,status:'running',version:1}),finish:ok({saved:true})}[op]);
const err=(status,code,message='x')=>({status,body:{detail:{message,code}}});
const toQuestion=async t=>{await t.click('start');await t.pump(()=>t.calls.includes('question'));await tick();};
let passed=0;const test=async(name,fn)=>{await fn();passed++;console.log('ok -',name);};

for(const [status,code] of [[409,'round_over'],[410,'round_expired']])
await test(String(status)+' '+code+' on /question shows new-round button that restarts',async()=>{
  const t=await scenario((op)=>op==='question'?err(status,code):base(op));await toQuestion(t);
  assert.equal(t.els.get('feedback').hidden,false);assert.match(t.els.get('feedbackTitle').textContent,/انتهت/);
  assert.match(t.els.get('continue').textContent,/جولة جديدة/);
  await t.click('continue');assert.equal(t.calls.filter(c=>c==='start').length,2);});

await test('stale_version on /answer silently refetches the question',async()=>{
  const t=await scenario((op)=>op==='answer'?err(409,'stale_version'):base(op));await toQuestion(t);
  const before=t.calls.filter(c=>c==='question').length;await t.click('confirm');await tick();
  assert.equal(t.calls.filter(c=>c==='question').length,before+1);assert.equal(t.els.get('feedback').hidden,true);});

await test('round_over on /answer shows the new-round screen',async()=>{
  const t=await scenario((op)=>op==='answer'?err(409,'round_over'):base(op));await toQuestion(t);await t.click('confirm');
  assert.match(t.els.get('continue').textContent,/جولة جديدة/);});

await test('repeated network failures end in new-round screen after retries',async()=>{
  const t=await scenario((op)=>op==='question'?'network':base(op));await toQuestion(t);
  for(let i=0;i<4&&!/جولة جديدة/.test(t.els.get('continue').textContent);i++)await t.click('continue');
  assert.match(t.els.get('continue').textContent,/جولة جديدة/);});

await test('pagehide never marks the round abandoned (/finish)',async()=>{
  const t=await scenario((op)=>base(op));await toQuestion(t);t.fire('pagehide');await tick();
  assert.ok(!t.calls.includes('finish'));});

await test('pageshow(persisted) revives loop and resyncs; non-persisted is ignored',async()=>{
  const t=await scenario((op)=>base(op));await toQuestion(t);t.fire('pagehide');
  const before=t.calls.filter(c=>c==='question').length;t.fire('pageshow',{persisted:false});await tick();
  assert.equal(t.calls.filter(c=>c==='question').length,before);
  t.fire('pageshow',{persisted:true});await tick();
  assert.ok(t.raf());assert.equal(t.calls.filter(c=>c==='question').length,before+1);assert.ok(!t.calls.includes('finish'));});

await test('explicit exit still reports the round as finished',async()=>{
  const t=await scenario((op)=>base(op));await toQuestion(t);await t.click('exit');
  assert.ok(t.calls.includes('finish'));});

console.log(String(passed)+' UI logic tests passed');fs.rmSync(tmp,{recursive:true,force:true});process.exit(0);
