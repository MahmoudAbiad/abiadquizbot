import {GameAudio} from './audio.js';
import {phaseSpeed,stepObjects,visibleObject,objectAlpha,useClassic} from './world.js';
const $=id=>document.getElementById(id),tg=window.Telegram?.WebApp;
tg?.ready();tg?.expand();tg?.disableVerticalSwipes?.();
const params=new URLSearchParams(location.search),base={init_data:tg?.initData||'',quiz_id:params.get('quiz_id'),launch:params.get('launch')||''};
const audio=new GameAudio(),canvas=$('world'),ctx=canvas.getContext('2d',{alpha:false});
let w=0,h=0,state='home',lane=1,visualLane=1,jump=0,clock=0,runTime=0,lastSpawn=0,objects=[],coins=0,score=0,lives=1,strikes=0,round=null,question=null,deadline=0,last=0,raf=0,config=null,disposed=false,paused=false,answerPending=false,final=false,retryAt=0,gone=false,blocked=false,retries=0,pageHidden=false,loadGeneration=0,foregroundWaiters=[];
const reduced=window.matchMedia?.('(prefers-reduced-motion: reduce)').matches||false;
let phase='RUN',phaseTime=0,approachWaiters=[],effects=[],classic=false,resolveResult=null;
function setPhase(value){phase=value;phaseTime=0;$('game').dataset.phase=value;}
const show=(id,value)=>$(id).hidden=!value;
function notice(text){$('notice').textContent=text;show('notice',true);clearTimeout(notice.timeout);notice.timeout=setTimeout(()=>show('notice',false),6500);}
async function api(path,extra={}){const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),120000);try{const r=await fetch('/api/runner/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...base,...extra}),signal:controller.signal});let data=null;try{data=await r.json();}catch{}if(!r.ok){const d=data?.detail,err=Error(typeof d==='string'?d:(d&&d.message)||'تعذر تنفيذ الطلب.');err.code=(d&&d.code)||(r.status===410?'round_expired':null);err.status=r.status;throw err;}return data;}catch(e){if(!e.status){e.code='network';e.message=e.name==='AbortError'?'انتهت مهلة الاتصال.':'تعذر الاتصال بالشبكة.';}throw e;}finally{clearTimeout(timeout);}}
function isGone(e){return e&&(e.code==='round_over'||e.code==='round_expired');}
function isBlocked(e){return e&&(e.code==='quiz_changed'||e.code==='auth');}
function roundGone(text){gone=true;state='gone';audio.setMode('think');['question','choices','controls','hint','resume'].forEach(id=>show(id,false));show('feedback',true);$('feedbackIcon').textContent='⌛';$('feedbackTitle').textContent='انتهت هذه الجولة';$('answerText').textContent=text||'يمكنك البدء بجولة جديدة.';$('explanation').textContent='';$('resultStats').textContent='';$('continue').textContent='ابدأ جولة جديدة ←';}
function roundBlocked(text){roundGone(text||'ارجع للبوت وافتح اللعبة من جديد.');blocked=true;$('feedbackIcon').textContent='🔒';$('feedbackTitle').textContent='تعذر متابعة الجولة';$('continue').textContent='إغلاق';}
function roundPayload(){return {round_id:round.round_id,version:round.version};}
function isForeground(){return !document.hidden&&!pageHidden&&!paused;}
function wakeForeground(){if(!isForeground())return;const pending=foregroundWaiters.splice(0);pending.forEach(resolve=>resolve());}
function waitForForeground(){return isForeground()?Promise.resolve():new Promise(resolve=>foregroundWaiters.push(resolve));}
function ensureCurrentLoad(id){if(id!==loadGeneration||disposed||final||gone){const error=new Error('تحميل قديم');error.code='superseded';throw error;}}
function imageDecoded(img){
 if(typeof img.decode==='function')return img.decode();
 return new Promise((resolve,reject)=>{
  if(img.complete){if(img.naturalWidth>0)resolve();else reject(Error('image failed'));return;}
  img.addEventListener('load',resolve,{once:true});
  img.addEventListener('error',()=>reject(Error('image failed')),{once:true});
 });
}
async function decodeAll(imgs){
 if(!imgs.length)return;
 let timer;
 try{
  await Promise.race([
   Promise.all(imgs.map(imageDecoded)),
   new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('decode timeout')),8000);})
  ]);
 }catch(e){const error=new Error('تعذر تحميل صور السؤال. تحقق من الاتصال وأعد المحاولة.');error.code='image_unavailable';throw error;}
 finally{clearTimeout(timer);}
}
function resize(){w=canvas.clientWidth;h=canvas.clientHeight;const d=Math.min(devicePixelRatio||1,2);canvas.width=Math.round(w*d);canvas.height=Math.round(h*d);ctx.setTransform(d,0,0,d,0,0);if(state==='question')positionGates();}
window.addEventListener('resize',resize);resize();
function poly(points,fill){ctx.fillStyle=fill;ctx.beginPath();points.forEach(([x,y],i)=>i?ctx.lineTo(x,y):ctx.moveTo(x,y));ctx.closePath();ctx.fill();}
function roundRect(x,y,width,height,r,fill){ctx.fillStyle=fill;ctx.beginPath();ctx.roundRect(x,y,width,height,r);ctx.fill();}
function point(l,z){const t=Math.max(0,z),road=w*(.12+.82*t);return {x:w/2+(l-1)*road/3,y:h*.34+t*t*h*.72,scale:.15+t*.95,road};}
function person(x,y,size,cop=false,phase=0){ctx.save();ctx.translate(x,y);ctx.scale(size,size);ctx.strokeStyle=cop?'#16243b':'#173449';ctx.lineWidth=10;ctx.lineCap='round';let step=Math.sin(phase)*10;
 ctx.beginPath();ctx.moveTo(-7,-20);ctx.lineTo(-10+step,4);ctx.moveTo(7,-20);ctx.lineTo(10-step,4);ctx.stroke();
 roundRect(-16,-57,32,40,9,cop?'#1f3654':'#54dbcf');ctx.strokeStyle=cop?'#1f3654':'#54dbcf';ctx.beginPath();ctx.moveTo(-14,-48);ctx.lineTo(-23,-26-step*.5);ctx.moveTo(14,-48);ctx.lineTo(23,-26+step*.5);ctx.stroke();
 roundRect(-12,-78,24,25,10,'#e9ad7b');roundRect(-13,-81,26,11,5,cop?'#213959':'#212c3f');if(cop){roundRect(-17,-74,34,5,2,'#10223a');ctx.fillStyle='#ffcf6e';ctx.fillRect(5,-49,6,7);}else{roundRect(-12,-52,24,26,6,'#faad63');ctx.fillStyle='#fae7c5';ctx.fillRect(-5,-45,11,3);}ctx.restore();}
function draw(dt){clock+=dt;const active=['run','approach','question','resolve'].includes(state)&&!paused;phaseTime+=dt;if(phaseTime>=1.5)approachWaiters.splice(0).forEach(f=>f());if(active){const speed=phaseSpeed(phase,phaseTime,reduced);runTime+=dt*speed;jump=Math.max(0,jump-dt);visualLane+=(lane-visualLane)*Math.min(1,dt*15);updateWorld(dt*speed);}else visualLane+=(lane-visualLane)*Math.min(1,dt*12);
 if(state==='resolve'&&phaseTime>=(reduced?.1:.55))finishResolve();
 const sky=ctx.createLinearGradient(0,0,0,h);sky.addColorStop(0,'#081b31');sky.addColorStop(.55,'#256075');sky.addColorStop(1,'#142c43');ctx.fillStyle=sky;ctx.fillRect(0,0,w,h);
 ctx.fillStyle='#ffe6b1';ctx.beginPath();ctx.arc(w*.2,h*.18,27,0,Math.PI*2);ctx.fill();
 for(let i=0;i<18;i++){let x=(i*71)%w,hh=30+(i*43)%100;roundRect(x,h*.37-hh,34,hh,3,i%2?'#183b50':'#1d465b');ctx.fillStyle='#8ac9c333';for(let j=0;j<4;j++)ctx.fillRect(x+8,h*.37-hh+14+j*18,5,7);}
 poly([[w*.41,h*.34],[w*.59,h*.34],[w*1.05,h],[w*-.05,h]],'#1c354a');
 poly([[w*.4,h*.34],[w*.41,h*.34],[0,h],[w*-.05,h]],'#5cb5b4');poly([[w*.59,h*.34],[w*.6,h*.34],[w*1.05,h],[w,h]],'#5cb5b4');
 for(let i=0;i<15;i++){const z=((i/15+(state==='run'?runTime*.12:runTime*.12))%1);const p=point(0,z),p2=point(0,z+.025);ctx.fillStyle='#bce0d33b';[-.5,.5].forEach(l=>{const a=point(l+1,z),b=point(l+1,z+.025);poly([[a.x-1,a.y],[a.x+1,a.y],[b.x+2,b.y],[b.x-2,b.y]],'#bce0d350');});}
 objects.filter(visibleObject).slice().sort((a,b)=>a.z-b.z).forEach(o=>{const p=point(o.lane,o.z);ctx.save();ctx.globalAlpha=objectAlpha(o);if(o.kind==='coin'){ctx.fillStyle='#ffc76b';ctx.beginPath();ctx.ellipse(p.x,p.y-17*p.scale,9*p.scale,12*p.scale,0,0,Math.PI*2);ctx.fill();ctx.strokeStyle='#fff1b3';ctx.lineWidth=2;ctx.stroke();}else{roundRect(p.x-23*p.scale,p.y-35*p.scale,46*p.scale,35*p.scale,4,'#e98b69');ctx.fillStyle='#ffe6bb';ctx.fillRect(p.x-21*p.scale,p.y-26*p.scale,42*p.scale,8*p.scale); }ctx.restore();});
 drawGates();drawEffects(dt);
 const p=point(visualLane,.81),lift=jump>0?Math.sin((.72-jump)/.72*Math.PI)*h*.15:0;
 ctx.fillStyle='#0005';ctx.beginPath();ctx.ellipse(p.x,p.y+6,25,8,0,0,Math.PI*2);ctx.fill();person(p.x,p.y-lift,Math.min(w/340,1.35),false,clock*(state==='question'?3:13));
 if(strikes>0||state==='caught'){const distance=state==='caught'?Math.min(1,(clock-caughtAt)/.8):.4;person(p.x+45*(1-distance),p.y+95*(1-distance),Math.min(w/340,1.35),true,clock*15);}
}
let caughtAt=0;
function updateWorld(dt){
 if(state==='run'&&runTime-lastSpawn>.68){lastSpawn=runTime;objects.push({lane:Math.floor(Math.random()*3),z:.02,kind:Math.random()<.65?'coin':'barrier',done:false});}
 if(state==='run'||state==='approach'){
  const step=stepObjects(objects,dt,visualLane,jump);objects=step.objects;
  if(step.coins){coins+=step.coins;audio.play('coin');hud();}
  if(step.dodges)audio.play('dodge');
  if(!reduced)for(const o of step.effects){const p=point(o.lane,o.z);effects.push({x:p.x,y:p.y-18,age:0,kind:o.kind});}effects=effects.slice(-24);
  if(step.hits){strikes+=step.hits;audio.play('near');tg?.HapticFeedback?.impactOccurred('medium');if(strikes>=2){caughtAt=clock;state='caught';audio.play('caught');showEnd('أمسك بك الشرطي!','تفادَ الحواجز أو اقفز فوقها في المحاولة القادمة.');api('finish',roundPayload()).catch(()=>{});return;}}
 }
 if(state==='run'&&runTime>=8)loadQuestion();
}
function gateZ(){if(phase==='APPROACH')return reduced?.62:.08+.54*Math.min(1,phaseTime/1.5);if(phase==='RESOLVE')return .62+.23*Math.min(1,phaseTime/.55);return .62;}
function positionGates(){if(classic)return;const z=gateZ();document.querySelectorAll('.lane').forEach((b,i)=>{const p=point(i,z);b.style.left=p.x+'px';b.style.top=p.y+'px';b.style.width=p.road/3*.92+'px';});}
function drawGates(){
 if(!['approach','question','resolve'].includes(state)||classic)return;
 const z=gateZ();for(let i=0;i<3;i++){const p=point(i,z),width=p.road/3*.95,height=110;const correct=resolveResult&&question.options[i].id===resolveResult.correct_id;
 const open=state==='resolve'&&correct;ctx.save();ctx.globalAlpha=open?Math.max(.1,1-phaseTime/.55):1;
 roundRect(p.x-width/2-4,p.y-height-8,width+8,height+12,10,correct?'#62e1af':i===lane?'#ffcf80':'#548d9f');
 roundRect(p.x-width/2,p.y-height,width,height,7,'#0d293dcc');ctx.restore();}
 if(phase!=='DECIDE')positionGates();
}
function drawEffects(dt){for(const e of effects){e.age+=dt;const t=Math.min(1,e.age/.22);ctx.save();ctx.globalAlpha=1-t;ctx.fillStyle=e.kind==='coin'?'#ffdb83':'#efa17d';
 for(let i=0;i<5;i++){const angle=i*Math.PI*.4;ctx.beginPath();ctx.arc(e.x+Math.cos(angle)*t*32,e.y-28*t+Math.sin(angle)*t*24,3*(1-t)+1,0,Math.PI*2);ctx.fill();}
 if(e.kind==='coin'){const x=e.x+(w*.84-e.x)*t,y=e.y+(85-e.y)*t;ctx.beginPath();ctx.arc(x,y,10+4*t,0,Math.PI*2);ctx.fill();}ctx.restore();}effects=effects.filter(e=>e.age<.22);}
function hud(){$('coins').textContent=coins;$('score').textContent=score;$('lives').textContent=lives;}
function selectLane(value){lane=Math.max(0,Math.min(2,value));document.querySelectorAll('.lane').forEach((el,i)=>el.classList.toggle('selected',i===lane));}
function move(d){if(state==='run'||state==='question'||state==='approach')selectLane(lane+d);}
function doJump(){if(state==='run'&&jump===0){jump=.72;audio.play('jump');}}
let touch=null;canvas.addEventListener('pointerdown',e=>{touch={x:e.clientX,y:e.clientY};canvas.setPointerCapture(e.pointerId);});canvas.addEventListener('pointerup',e=>{if(!touch)return;const dx=e.clientX-touch.x,dy=e.clientY-touch.y;if(Math.abs(dx)>25&&Math.abs(dx)>Math.abs(dy))move(dx>0?1:-1);else if(dy< -25)doJump();touch=null;});
window.addEventListener('keydown',e=>{if(e.target.tagName==='INPUT')return;if(['ArrowLeft','ArrowRight','ArrowUp',' ','Enter'].includes(e.key))e.preventDefault();if(e.key==='ArrowLeft')move(-1);if(e.key==='ArrowRight')move(1);if(e.key==='ArrowUp'||e.key===' ')doJump();if(e.key==='Enter'&&state==='question')submit(false);});
$('left').onclick=()=>move(-1);$('right').onclick=()=>move(1);$('jump').onclick=doJump;
function soundButtons(){$('music').classList.toggle('muted',!audio.music);$('sfx').classList.toggle('muted',!audio.sfx);$('music').setAttribute('aria-pressed',String(audio.music));$('sfx').setAttribute('aria-pressed',String(audio.sfx));}
$('music').onclick=()=>{audio.toggleMusic();soundButtons();};$('sfx').onclick=()=>{audio.toggleSfx();soundButtons();};soundButtons();
async function refreshConfig(){config=await api('config');$('title').textContent=config.title;$('start').disabled=false;show('settingsButton',config.can_edit);}
async function start(){if(state==='loading')return;state='loading';$('start').disabled=true;audio.mode='run';audio.start().catch(()=>{});try{round=await api('start');coins=0;score=0;lives=round.lives;strikes=0;objects=[];lane=1;visualLane=1;runTime=0;lastSpawn=0;final=false;gone=false;blocked=false;retries=0;loadGeneration++;hud();show('home',false);show('feedback',false);show('hud',true);beginRun();}catch(e){state='home';audio.stop();notice(e.message);$('start').disabled=false;show('home',true);}}
$('start').onclick=start;
function beginRun(){state='run';setPhase('RUN');effects=[];resolveResult=null;$('choices').classList.remove('gated');runTime=0;lastSpawn=0;objects=[];jump=0;audio.setMode('run');show('question',false);show('choices',false);show('feedback',false);show('controls',true);show('hint',true);}
async function loadQuestion(){
 if(state!=='run'&&state!=='retry')return;
 const generation=++loadGeneration;
 state='approach';setPhase('APPROACH');classic=false;show('hint',false);show('controls',false);show('question',false);show('choices',false);show('feedback',false);audio.setMode('think');
 try{
  const next=await api('question',roundPayload());ensureCurrentLoad(generation);
  question=next;round.version=next.version;
  const imgs=[],content=$('questionContent');content.replaceChildren();
  if(question.game_image_url){const img=new Image();img.alt=question.text;img.src=question.game_image_url;content.append(img);imgs.push(img);}
  else content.textContent=question.text;
  $('number').textContent=`السؤال ${question.number} / ${question.total}`;$('lanes').replaceChildren();
  question.options.forEach((o,i)=>{const b=document.createElement('button');b.className='lane';b.setAttribute('aria-label',o.text);const tag=document.createElement('span');tag.className='letter';tag.textContent=['المسار الأيسر','المسار الأوسط','المسار الأيمن'][i];b.append(tag);if(o.image_url){const img=new Image();img.src=o.image_url;img.alt=o.text;b.append(img);imgs.push(img);}else{const span=document.createElement('span');span.textContent=o.text;b.append(span);}b.onclick=()=>selectLane(i);$('lanes').append(b);});
  await decodeAll(imgs);ensureCurrentLoad(generation);
  classic=useClassic(question.options,imgs.slice(question.game_image_url?1:0),h,w);
  $('choices').classList.toggle('gated',!classic);
  if(!reduced&&phaseTime<1.5)await new Promise(resolve=>approachWaiters.push(resolve));ensureCurrentLoad(generation);
  // Never send /ready while the app is hidden or waiting for the user to resume.
  await waitForForeground();ensureCurrentLoad(generation);
  const readySent=performance.now(),ready=await api('ready',roundPayload());
  // /ready could complete after the user backgrounds the app. Do not expose
  // the question until foreground; preserve server time elapsed in deadline.
  await waitForForeground();ensureCurrentLoad(generation);
  retries=0;deadline=readySent+ready.remaining_seconds*1000;state='question';setPhase('DECIDE');selectLane(lane);positionGates();
  show('question',true);show('choices',true);$('confirm').disabled=false;
 }catch(e){
  if(e.code==='superseded'||generation!==loadGeneration)return;
  if(isBlocked(e)){roundBlocked(e.message);return;}
  if(isGone(e)){roundGone(e.message);return;}
  if(e.code==='stale_version'&&retries<3){retries++;state='retry';loadQuestion();return;}
  if(++retries>3){roundGone('تعذر تحميل السؤال بعد عدة محاولات. ابدأ جولة جديدة.');return;}
  state='retry';notice(e.message);show('feedback',true);$('feedbackTitle').textContent='تعذر تحميل السؤال';$('answerText').textContent='اضغط لإعادة المحاولة.';$('explanation').textContent='';$('resultStats').textContent='';$('continue').textContent='إعادة المحاولة';
 }
}
async function submit(timeout){if(state!=='question'||answerPending)return;answerPending=true;if(timeout)notice('انتهى وقت السؤال');$('confirm').disabled=true;
 try{const result=await api('answer',{...roundPayload(),option_id:timeout?null:question.options[lane].id});round.version=result.version;lives=result.lives;score=result.score;hud();resolveResult={...result,timeout};state='resolve';setPhase('RESOLVE');$('game').classList.toggle('impact',!result.correct&&!reduced);audio.play(result.correct?'correct':'wrong');if(!result.correct){audio.play('near');tg?.HapticFeedback?.impactOccurred('medium');}if(result.status==='caught')audio.play('caught');
 document.querySelectorAll('.lane').forEach((b,i)=>{b.disabled=true;const correct=question.options[i].id===result.correct_id;b.classList.toggle('correct',correct);b.classList.toggle('closed',!correct);b.setAttribute('aria-label',question.options[i].text+(correct?' — الإجابة الصحيحة':''));});
 if(!reduced&&result.correct){const p=point(lane,.81);effects.push({x:p.x,y:p.y-55,age:0,kind:'coin'});}
 }catch(e){if(isBlocked(e)){roundBlocked(e.message);return;}if(isGone(e)){roundGone(e.message);return;}if(e.code==='stale_version'){state='retry';loadQuestion();return;}notice(e.message);retryAt=performance.now()+5000;$('confirm').disabled=false;}finally{answerPending=false;}}
function finishResolve(){const result=resolveResult;if(!result)return;state=result.status==='caught'?'caught':'feedback';caughtAt=clock;$('game').classList.remove('impact');audio.setMode('think');show('choices',false);show('question',false);show('feedback',true);$('feedbackIcon').textContent=result.correct?'✦':'♡';$('feedbackTitle').textContent=result.timeout?'انتهى وقت السؤال':result.correct?'إجابة صحيحة!':(result.status==='caught'?'أمسك بك الشرطي!':'فرصة أخرى، ركّز!');$('answerText').textContent='الإجابة الصحيحة: '+result.correct_text;$('explanation').textContent=result.explanation;final=result.status!=='running';$('resultStats').textContent=final?`نقاط المعرفة ${score} · عملات الركض ${coins}`:'';$('continue').textContent=final?'العب من جديد ←':'تابع الركض ←';if(final)setTimeout(()=>{if(final)audio.stop();},900);}
$('confirm').onclick=()=>submit(false);
function showEnd(title,explanation){final=true;loadGeneration++;approachWaiters.splice(0).forEach(f=>f());show('controls',false);show('hint',false);show('feedback',true);$('feedbackTitle').textContent=title;$('answerText').textContent='';$('explanation').textContent=explanation;$('resultStats').textContent=`نقاط المعرفة ${score} · عملات الركض ${coins}`;$('continue').textContent='العب من جديد ←';setTimeout(()=>{if(final)audio.stop();},900);}
$('continue').onclick=()=>{if(blocked){if(tg){tg.close();return;}blocked=false;gone=false;show('feedback',false);show('home',true);state='home';$('start').disabled=false;return;}if(gone){gone=false;show('feedback',false);state='home';start();return;}if(state==='retry'){show('feedback',false);loadQuestion();}else if(final){show('feedback',false);show('home',true);state='home';$('start').disabled=false;}else beginRun();};
$('settingsButton').onclick=()=>{show('home',false);show('settings',true);$('livesInput').value=config.settings.lives;$('uniform').value=config.settings.uniform_seconds||'';$('estimate').disabled=['done','running'].includes(config.settings.ai_status);$('estimate').textContent=config.settings.ai_status==='done'?'تم حفظ اقتراح AI':config.settings.ai_status==='running'?'التهيئة قيد التنفيذ':'تقدير AI مرة واحدة';$('timings').replaceChildren();config.questions.forEach((q,i)=>{const label=document.createElement('label');label.className='timing';const span=document.createElement('span');span.textContent=`${i+1}. ${q.text.slice(0,90)}`;const small=document.createElement('small');small.textContent=`المعتمد: ${q.seconds}ث · AI: ${q.ai_seconds??'غير متاح'}`;span.append(small);const input=document.createElement('input');input.type='number';input.min=10;input.max=300;input.dataset.key=q.key;input.value=config.settings.manual_times[q.key]||'';label.append(span,input);$('timings').append(label);});};
$('closeSettings').onclick=()=>{show('settings',false);show('home',true);};
$('saveSettings').onclick=async()=>{const manual_times={};for(const input of $('timings').querySelectorAll('input')){if(input.value){const value=Number(input.value);if(!Number.isInteger(value)||value<10||value>300){notice('الوقت بين 10 و300 ثانية.');return;}manual_times[input.dataset.key]=value;}}try{await api('settings',{version:config.settings.version,lives:Number($('livesInput').value),uniform_seconds:$('uniform').value?Number($('uniform').value):null,manual_times});await refreshConfig();$('closeSettings').click();notice('تم حفظ التوقيت والفرص.');}catch(e){notice(e.message);await refreshConfig().catch(()=>{});}};
$('estimate').onclick=async()=>{$('estimate').disabled=true;try{await api('estimate');await refreshConfig();$('settingsButton').click();notice('حُفظ تقدير AI لإعادة استخدامه.');}catch(e){notice(e.message);$('estimate').disabled=false;}};
function animate(now){if(disposed)return;const dt=Math.min(.04,(now-last)/1000||.016);last=now;if(!paused){draw(dt);if(state==='question'){const left=Math.max(0,(deadline-now)/1000);$('timer').textContent=Math.ceil(left)+' ث';$('timebar').style.transform=`scaleX(${left/question.seconds})`;if(left<=0&&!answerPending&&now>=retryAt)submit(true);}}raf=requestAnimationFrame(animate);}
raf=requestAnimationFrame(animate);
document.addEventListener('visibilitychange',()=>{if(document.hidden){paused=true;audio.suspend();}else if(round&&!final){show('resume',true);}else{paused=false;wakeForeground();}});
$('resume').onclick=()=>{if(document.hidden||pageHidden)return;paused=false;last=performance.now();audio.resume();show('resume',false);wakeForeground();};
function cleanup(){disposed=true;loadGeneration++;approachWaiters.splice(0).forEach(f=>f());cancelAnimationFrame(raf);audio.stop();objects=[];if(round&&!final){fetch('/api/runner/finish',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...base,...roundPayload()}),keepalive:true}).catch(()=>{});}}
window.addEventListener('pagehide',()=>{pageHidden=true;paused=true;audio.suspend();});
window.addEventListener('pageshow',e=>{pageHidden=false;if(!e.persisted){wakeForeground();return;}disposed=false;cancelAnimationFrame(raf);last=performance.now();raf=requestAnimationFrame(animate);if(round&&!final&&!gone){paused=true;show('resume',true);if(['question','approach','loading','retry'].includes(state)){state='retry';loadQuestion();}}else if(!round||final)refreshConfig().catch(()=>{});});$('exit').onclick=()=>{cleanup();tg?.close();if(!tg){show('home',true);$('title').textContent='انتهت الجولة. أعد فتح اللعبة من البوت.';}};
if(base.init_data&&base.quiz_id)refreshConfig().catch(e=>{$('title').textContent=e.message;});else $('title').textContent='افتح اللعبة من زر «العب رحلة أبيض» داخل البوت التجريبي.';
