// Exercise the real canvas and controls with bounded API fixtures.
const {chromium}=require('playwright'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH,headless:true,args:['--no-sandbox']}),results=[];
 for(const mode of ['ground-correct','jetpack-wrong','tesla-timeout']){
  const item=mode.startsWith('ground')?'none':mode.split('-')[0],page=await browser.newPage({viewport:{width:390,height:844}}),errors=[],claims=[];
  let answered=0,retry=true;
  page.on('pageerror',e=>errors.push(e.message));
  await page.clock.install({time:new Date('2026-10-22T12:00:00Z')});await page.clock.pauseAt(new Date('2026-10-22T12:00:01Z'));
  await page.addInitScript(()=>{
   const proto=CanvasRenderingContext2D.prototype;
   const fillRect=proto.fillRect,ellipse=proto.ellipse,roundRect=proto.roundRect,moveTo=proto.moveTo,lineTo=proto.lineTo,beginPath=proto.beginPath,fill=proto.fill;
   let vertices=[];window.canvasScene={coins:[],obstacles:[],marks:[]};
   proto.fillRect=function(x,y,w,h){if(x===0&&y===0&&h>500)window.canvasScene={coins:[],obstacles:[],marks:[]};return fillRect.call(this,x,y,w,h);};
   proto.ellipse=function(...args){if(this.fillStyle==='#ffc76b')window.canvasScene.coins.push(args.slice(0,4));return ellipse.apply(this,args);};
   proto.roundRect=function(...args){if(this.fillStyle==='#e98b69')window.canvasScene.obstacles.push(args.slice(0,4));return roundRect.apply(this,args);};
   proto.beginPath=function(...args){vertices=[];return beginPath.apply(this,args);};
   proto.moveTo=function(...args){vertices.push(args);return moveTo.apply(this,args);};proto.lineTo=function(...args){vertices.push(args);return lineTo.apply(this,args);};
   proto.fill=function(...args){if(this.fillStyle==='#bce0d350'||this.fillStyle.startsWith('rgba(188, 224, 211,'))window.canvasScene.marks.push(vertices.slice());return fill.apply(this,args);};
  });
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());
   if(u.hostname==='telegram.org')return route.fulfill({contentType:'text/javascript',body:'window.Telegram={WebApp:{initData:"fixture",ready(){},expand(){},disableVerticalSwipes(){}}}'});
   if(u.pathname.startsWith('/api/runner/')){
    const op=u.pathname.split('/').pop(),b=route.request().postDataJSON();let data;
    const wallet={coins:claims.length,knowledge:0,knowledge_earned:0,jetpack_owned:true,car_level:3,equipped:item,version:claims.length};
    if(op==='config')data={title:'استمرارية الطريق',wallet,settings:{lives:3},questions:[]};
    if(op==='start'){answered=0;data={round_id:'round',version:0,lives:3,total:3};}
    if(op==='track'){
     if(b.segment===1&&retry){retry=false;return route.abort('failed');}
     data={equipment:item,car_level:3,objects:b.segment?[]:[{id:8,at:6.3,lane:0,kind:'barrier'},{id:9,at:7,lane:1,kind:'coin'}],claimed:[]};
    }
    if(op==='question')data={text:'اختر الإجابة الصحيحة',options:[{id:0,text:'صحيح'},{id:1,text:'وسط'},{id:2,text:'آخر'}],number:answered+1,total:3,seconds:30,version:answered};
    if(op==='ready')data={remaining_seconds:mode.endsWith('timeout')?5:30};
    if(op==='answer'){answered++;data={correct:mode.endsWith('correct'),correct_id:0,correct_text:'صحيح',explanation:'شرح',lives:2,score:100,version:answered,status:'running'};}
    if(op==='coins'){claims.push(b);data={...wallet,coins:claims.length,version:claims.length,accepted:b.coin_ids};}
    if(op==='finish')data={saved:true};return route.fulfill({json:data});
   }
   if(process.env.RUNNER_TEST_ORIGIN)return route.continue();
   return route.fulfill({path:path.join(__dirname,'..',u.pathname)});
  });
  await page.goto((process.env.RUNNER_TEST_ORIGIN||'http://runner.test')+'/webapp/runner/index.html?quiz_id=q',{waitUntil:'domcontentloaded',timeout:90000});
  await page.waitForFunction(()=>!document.querySelector('#start').disabled);await page.click('#start');await page.waitForFunction(()=>document.querySelector('#game').dataset.phase==='RUN');
  await page.clock.runFor(14000);await page.waitForSelector('#choices:not([hidden])');await page.clock.runFor(20);
  const snapshot=()=>page.evaluate(()=>({distance:Number(document.querySelector('#game').dataset.distance),lane:document.querySelector('#game').dataset.playerLane,...window.canvasScene}));
  const stopped=await snapshot();assert.equal(stopped.coins.length,1);assert.equal(stopped.obstacles.length,1);assert.equal(stopped.marks.length,30);
  await page.clock.runFor(1000);assert.deepEqual(await snapshot(),stopped);
  // Choosing a different answer column cannot move the runner to that lane.
  await page.locator('.lane').nth(mode.endsWith('correct')?0:2).click({force:true});await page.clock.runFor(100);assert.deepEqual(await snapshot(),stopped);
  if(mode.endsWith('timeout'))await page.clock.runFor(5000);else await page.click('#confirm',{force:true});
  await page.clock.runFor(700);await page.waitForSelector('#feedback:not([hidden])');assert.deepEqual(await snapshot(),stopped);
  await page.click('#continue',{force:true});await page.waitForFunction(()=>document.querySelector('#feedbackTitle').textContent.includes('تعذر تجهيز'));
  await page.clock.runFor(500);assert.deepEqual(await snapshot(),stopped); // Failed track load also preserves the scene.
  await page.click('#continue',{force:true});await page.waitForFunction(()=>document.querySelector('#game').dataset.phase==='RUN');
  assert.deepEqual(await snapshot(),stopped);await page.clock.runFor(300);const moved=await snapshot();
  assert.ok(moved.distance>stopped.distance);assert.equal(moved.coins.length,1);assert.equal(moved.obstacles.length,1);assert.notDeepEqual(moved.marks,stopped.marks);
  const coinZ=s=>Math.sqrt((s.coins[0][1]-844*.34+17*.15)/(844*.72-17*.95-(item==='none'?0:844*.16/.81**2)));
  const obstacleZ=s=>Math.sqrt((s.obstacles[0][1]-844*.34+35*.15)/(844*.72-35*.95));
  const markZ=s=>Math.sqrt((s.marks[0][0][1]/844-.34)/.72),delta=moved.distance-stopped.distance;
  assert.ok(Math.abs(coinZ(moved)-coinZ(stopped)-delta)<2e-6);
  assert.ok(Math.abs(obstacleZ(moved)-obstacleZ(stopped)-delta)<2e-6);
  assert.ok(Math.abs((markZ(moved)-markZ(stopped)+1)%1-delta)<2e-6);
  const shots=process.env.RUNNER_SCREENSHOT_DIR||require('os').tmpdir();await page.screenshot({path:path.join(shots,'runner-continuity-'+mode+'.png')});
  await page.clock.runFor(2000);assert.equal(claims.length,1);assert.equal(claims[0].segment,0);assert.deepEqual(claims[0].coin_ids,[9]);
  assert.equal(await page.getAttribute('#flight','aria-pressed'),item==='none'?'false':'true');assert.deepEqual(errors,[]);
  // Only an explicit new round resets the distance and objects.
  await page.reload({waitUntil:'domcontentloaded'});await page.waitForFunction(()=>!document.querySelector('#start').disabled);await page.click('#start');await page.waitForFunction(()=>document.querySelector('#game').dataset.phase==='RUN');await page.clock.runFor(20);
  const restarted=await snapshot();assert.ok(restarted.distance<.02);assert.equal(restarted.coins.length,0);assert.equal(restarted.obstacles.length,0);
  results.push({mode,questionAndFeedbackFrozen:true,answerPreservesLane:true,trackRetryPreservesScene:true,objectsResume:true,sharedRoadMovement:true,oldSegmentCoinCredited:true,newRoundResets:true,distanceBefore:stopped.distance,distanceAfter:moved.distance,errors});await page.close();
 }
 await browser.close();console.log(JSON.stringify({origin:process.env.RUNNER_TEST_ORIGIN||'local',api:'fixtures',results}));
})().catch(e=>{console.error(e);process.exit(1)});
