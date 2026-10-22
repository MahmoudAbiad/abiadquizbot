// Actual canvas previews and arrival timing; API fixtures, no player data writes.
const {chromium}=require('playwright'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH,headless:true,args:['--no-sandbox']}),results=[];
 for(const mode of ['arabic','long-flight','math-delayed-options','broken-math']){
  const page=await browser.newPage({viewport:{width:390,height:844}}),calls=[],errors=[];let releaseOption,optionRequested=false,broken=mode==='broken-math';
  const optionGate=new Promise(r=>{releaseOption=r;}),math=mode.includes('math');
  const text=mode==='long-flight'?'ما العلاقة بين الدراسة المنتظمة وتثبيت المعلومات في الذاكرة؟ '.repeat(9):'ما عاصمة سورية؟';
  page.on('pageerror',e=>errors.push(e.message));await page.clock.install({time:new Date('2026-10-22T12:00:00Z')});await page.clock.pauseAt(new Date('2026-10-22T12:00:01Z'));
  await page.addInitScript(()=>{
   const p=CanvasRenderingContext2D.prototype,text=p.fillText,draw=p.drawImage,rect=p.fillRect;
   window.wallFrame=[];
   p.fillText=function(value,...args){if(this.canvas.width===640&&this.canvas.height===360)(this.canvas.wallTexts??=[]).push(value);return text.call(this,value,...args);};
   p.fillRect=function(x,y,w,h){if(this.canvas.id==='world'&&x===0&&y===0&&h>500)window.wallFrame=[];return rect.call(this,x,y,w,h);};
   p.drawImage=function(source,...args){if(this.canvas.id==='world'&&source.wallTexts)window.wallFrame.push({texts:source.wallTexts,rect:args});return draw.call(this,source,...args);};
  });
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());
   if(u.hostname==='telegram.org')return route.fulfill({contentType:'text/javascript',body:'window.Telegram={WebApp:{initData:"fixture",ready(){},expand(){},disableVerticalSwipes(){}}}'});
   if(u.pathname==='/math.svg')return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="600" height="180"><rect width="600" height="180" fill="white"/><text x="140" y="110" font-size="56">2 + 2 = ?</text></svg>'});
   if(u.pathname==='/option.svg'){
    optionRequested=true;if(broken)return route.abort('failed');if(mode==='math-delayed-options')await optionGate;
    return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="150" height="70"><text x="50" y="50" font-size="40">4</text></svg>'});
   }
   if(u.pathname.startsWith('/api/runner/')){
    const op=u.pathname.split('/').pop();calls.push(op);let data;
    if(op==='config')data={title:'السؤال على الجدار',wallet:{coins:0,knowledge:0,knowledge_earned:0,car_level:3,equipped:mode==='arabic'?'none':'tesla',jetpack_owned:true,version:0},settings:{lives:3},questions:[]};
    if(op==='start')data={round_id:'r',version:0,lives:3,total:2};
    if(op==='track')data={equipment:mode==='arabic'?'none':'tesla',car_level:3,objects:[],claimed:[]};
    if(op==='question')data={text,game_image_url:math?'/math.svg':null,options:[{id:0,text:'الخيار السري أ',image_url:math?'/option.svg':null},{id:1,text:'دمشق'},{id:2,text:'الخيار السري ج'}],number:1,total:2,seconds:30,version:0};
    if(op==='ready')data={remaining_seconds:30};
    if(op==='answer')data={correct:true,correct_id:1,correct_text:'دمشق',score:100,lives:3,status:'running',version:1};
    if(op==='finish')data={saved:true};return route.fulfill({json:data});
   }
   if(process.env.RUNNER_TEST_ORIGIN)return route.continue();return route.fulfill({path:path.join(__dirname,'..',u.pathname)});
  });
  await page.goto((process.env.RUNNER_TEST_ORIGIN||'http://runner.test')+'/webapp/runner/index.html?quiz_id=q',{waitUntil:'domcontentloaded',timeout:90000});await page.waitForFunction(()=>!document.querySelector('#start').disabled);
  await page.click('#start');await page.waitForFunction(()=>document.querySelector('#game').dataset.phase==='RUN');
  await page.clock.runFor(7800);
  assert.equal(calls.filter(x=>x==='question').length,1);assert.equal(calls.includes('ready'),false);assert.equal(await page.locator('#question').isVisible(),false);assert.equal(await page.locator('#choices').isVisible(),false);
  const frame=await page.evaluate(()=>window.wallFrame);assert.equal(frame.length,1);assert.ok(frame[0].texts.some(t=>t.includes('السؤال 1')));assert.ok(!frame[0].texts.some(t=>t.includes('الخيار السري')));
  if(!math)assert.ok(frame[0].texts.some(t=>t.includes(mode==='arabic'?'ما عاصمة':'ما العلاقة')));
  const shots=process.env.RUNNER_SCREENSHOT_DIR||require('os').tmpdir();await page.screenshot({path:path.join(shots,'runner-wall-preview-'+mode+'.png')});
  await page.clock.runFor(6200);
  if(math){await page.waitForFunction(()=>document.querySelector('#game').dataset.barrierZ==='0.700');assert.equal(optionRequested,true);
   if(broken){await page.waitForSelector('#feedback:not([hidden])');assert.equal(calls.includes('ready'),false);broken=false;await page.click('#continue',{force:true});}
   else{assert.equal(calls.includes('ready'),false);const before=await page.getAttribute('#game','data-distance');await page.clock.runFor(1000);assert.equal(await page.getAttribute('#game','data-distance'),before);}
   releaseOption();
  }
  await page.waitForSelector('#choices:not([hidden])');assert.equal(calls.filter(x=>x==='ready').length,1);assert.equal(await page.getAttribute('#game','data-barrier-z'),'0.700');
  assert.equal(calls.filter(x=>x==='question').length,mode==='broken-math'?2:1);assert.deepEqual(errors,[]);
  results.push({mode,questionOnApproachingWall:true,noAnswersOnWall:true,noEarlyTimer:true,interfaceAtArrival:true,imagesGateTimer:true,errors});await page.close();
 }
 await browser.close();console.log(JSON.stringify({origin:process.env.RUNNER_TEST_ORIGIN||'local',api:'fixtures',results}));
})().catch(e=>{console.error(e);process.exit(1)});
