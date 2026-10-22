// Only the aggregate count is returned; failures never interrupt a round.
function sessionId(){
 if(crypto.randomUUID)return crypto.randomUUID();
 const bytes=crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
 const hex=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
 return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
export function createPresence({base,element,current,fetcher=fetch,now=()=>Date.now(),session=sessionId()}){
 let sequence=0,lastAt=-Infinity,lastKey='',lastActive=false,latest=0;
 function sync(force=false,keepalive=false){
  const s=current(),active=s.active,roundId=active?s.roundId:null,key=active?roundId:'idle';
  if(!force&&key===lastKey&&now()-lastAt<20000)return;
  if(!active&&!s.visible&&!lastActive)return;
  const leaving=lastActive&&!active;
  lastKey=key;lastActive=active;lastAt=now();const revision=++sequence;latest=revision;
  const body={...base,...(active||leaving?{session_id:session,sequence:revision,active,round_id:roundId}:{})};
  return fetcher('/api/runner/presence',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),keepalive,signal:AbortSignal.timeout(8000)})
   .then(async response=>{if(!response.ok)throw Error('unavailable');const data=await response.json();
    if(revision!==latest)return;if(!Number.isInteger(data.online)||data.online<0)throw Error('invalid');
    element.textContent=`يلعب الآن: ${data.online.toLocaleString('ar')} طالبًا`;element.hidden=false;
   }).catch(()=>{if(revision===latest)element.hidden=true;});
 }
 return {sync};
}
