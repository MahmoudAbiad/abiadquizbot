const {chromium}=require('playwright'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH,headless:true,args:['--no-sandbox']});const results=[];
 for(const item of ['jetpack','tesla']){
  const page=await browser.newPage({viewport:{width:390,height:844}});await page.clock.install();let answered=0;const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{const u=new URL(route.request().url());
   if(u.hostname==='telegram.org')return route.fulfill({contentType:'text/javascript',body:'window.Telegram={WebApp:{initData:"test",ready(){},expand(){},disableVerticalSwipes(){}}}'});
   if(u.pathname.startsWith('/api/runner/')){const op=u.pathname.split('/').pop();let data;
    const wallet={coins:0,knowledge:0,knowledge_earned:0,jetpack_owned:true,car_level:3,equipped:item,version:0};
    if(op==='config')data={title:'Flight state test',can_edit:false,settings:{lives:5},questions:[],wallet};
    if(op==='start')data={round_id:'r',version:0,lives:5,total:3};
    if(op==='track')data={segment:answered,equipment:answered&&item==='jetpack'?'tesla':item,car_level:3,objects:[],claimed:[]};
    if(op==='question')data={text:'٢ + ٢؟',options:[{id:0,text:'٣'},{id:1,text:'٤'},{id:2,text:'٥'}],seconds:30,number:answered+1,total:3,version:answered};
    if(op==='ready')data={remaining_seconds:30};
    if(op==='answer')data={correct:true,correct_id:1,correct_text:'٤',explanation:'٤',lives:5,score:100*(++answered),status:'running',version:answered,knowledge_awarded:0};
    if(op==='finish')data={saved:true};return route.fulfill({json:data});
   }
   if(process.env.RUNNER_TEST_ORIGIN)return route.continue();
   return route.fulfill({path:path.join(__dirname,'..',u.pathname)});
  });
  const origin=process.env.RUNNER_TEST_ORIGIN||'http://runner.test';await page.goto(origin+'/webapp/runner/index.html?quiz_id=q',{waitUntil:'domcontentloaded',timeout:90000});await page.waitForFunction(()=>!document.querySelector('#start').disabled,{},{timeout:90000});await page.click('#start');await page.waitForFunction(()=>document.querySelector('#game').dataset.phase==='RUN');
  assert.equal(await page.getAttribute('#flight','aria-pressed'),'true');await page.click('#flight');assert.equal(await page.getAttribute('#flight','aria-pressed'),'false');
  for(let i=0;i<2;i++){
   await page.clock.runFor(14000);await page.waitForFunction(()=>document.querySelector('#game').dataset.phase==='DECIDE');await page.locator('.lane').filter({hasText:'٤'}).click();await page.click('#confirm');await page.clock.runFor(650);await page.waitForSelector('#feedback:not([hidden])');await page.click('#continue');await page.waitForFunction(()=>document.querySelector('#game').dataset.phase==='RUN');
   assert.equal(await page.getAttribute('#flight','aria-pressed'),i===0?'false':'true');
   if(i===0){const shots=process.env.RUNNER_SCREENSHOT_DIR||require('os').tmpdir();await page.screenshot({path:path.join(shots,'runner-landed-after-question-'+item+'.png')});await page.click('#flight');}
  }
  assert.deepEqual(errors,[]);results.push({equipment:item,landingSurvivesQuestion:true,explicitTakeoffSurvivesNextQuestion:true,serverEquipmentRefreshPreservesChoice:true,errors});await page.close();
 }
 await browser.close();console.log(JSON.stringify({origin:process.env.RUNNER_TEST_ORIGIN||'local',results}));
})().catch(e=>{console.error(e);process.exit(1)});
