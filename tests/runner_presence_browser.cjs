// Published/local UI with API fixtures; no actual player records are changed.
const {chromium}=require('playwright'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH,headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:390,height:844},reducedMotion:'reduce'}),requests=[],errors=[];
 let online=12,unavailable=false;
 page.on('pageerror',e=>errors.push(e.message));
 await page.clock.install();await page.clock.pauseAt(new Date(Date.now()+1000));
 await page.route('**/*',async route=>{
  const u=new URL(route.request().url());
  if(u.hostname==='telegram.org')return route.fulfill({contentType:'text/javascript',body:'window.Telegram={WebApp:{initData:"fixture",ready(){},expand(){},disableVerticalSwipes(){}}}'});
  if(u.pathname.startsWith('/api/runner/')){
   const op=u.pathname.split('/').pop(),body=route.request().postDataJSON();requests.push({op,...body});
   if(op==='presence')return route.fulfill({status:unavailable?503:200,json:unavailable?{}:{online}});
   const data={config:{title:'رحلة المعرفة',wallet:{coins:0,knowledge:0,knowledge_earned:0,car_level:0,equipped:'none',version:0},settings:{lives:3},questions:[]},start:{round_id:'round-1',version:0,lives:3,total:1},track:{objects:[],claimed:[],equipment:'none',car_level:0},question:{text:'ما عاصمة سورية؟',options:[{id:0,text:'دمشق'},{id:1,text:'حلب'},{id:2,text:'حمص'}],number:1,total:1,seconds:300,version:0},ready:{remaining_seconds:300},answer:{correct:true,correct_id:0,correct_text:'دمشق',explanation:'',lives:3,score:100,status:'completed',version:1},finish:{saved:true},wallet:{coins:0,version:0}}[op];
   return route.fulfill({json:data});
  }
  if(process.env.RUNNER_TEST_ORIGIN)return route.continue();
  return route.fulfill({path:path.join(__dirname,'..',u.pathname)});
 });
 const origin=process.env.RUNNER_TEST_ORIGIN||'http://runner.test';
 await page.goto(origin+'/webapp/runner/index.html?quiz_id=q');await page.clock.runFor(100);
 await page.waitForFunction(()=>!document.querySelector('#onlinePlayers').hidden);assert.match(await page.textContent('#onlinePlayers'),/١٢|12/);
 assert.equal(requests.filter(x=>x.op==='presence').length,1);assert.equal(requests.find(x=>x.op==='presence').active,undefined);
 await page.screenshot({path:path.join(process.env.RUNNER_SCREENSHOT_DIR||require('os').tmpdir(),'runner-online-home.png')});
 await page.click('#start',{force:true});await page.clock.runFor(200);await page.waitForFunction(()=>document.querySelector('#game').dataset.phase==='RUN');
 await page.clock.runFor(100);assert.equal(requests.filter(x=>x.op==='presence').at(-1).active,true);
 await page.clock.runFor(14500);await page.waitForSelector('#choices:not([hidden])');
 assert.equal(await page.locator('#onlinePlayers').isVisible(),false);
 await page.clock.runFor(21000);assert.equal(requests.filter(x=>x.op==='presence').at(-1).active,true);
 assert.ok(requests.filter(x=>x.op==='presence').length<6);
 await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));});
 await page.clock.runFor(100);assert.equal(requests.filter(x=>x.op==='presence').at(-1).active,false);
 const before=requests.filter(x=>x.op==='presence').length;await page.clock.runFor(60000);assert.equal(requests.filter(x=>x.op==='presence').length,before);
 await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'));});
 await page.click('#resume',{force:true});await page.clock.runFor(100);assert.equal(requests.filter(x=>x.op==='presence').at(-1).active,true);
 await page.locator('.lane').nth(0).click({force:true});await page.click('#confirm',{force:true});await page.clock.runFor(500);
 assert.equal(requests.filter(x=>x.op==='presence').at(-1).active,false);
 await page.click('#continue',{force:true});online=0;await page.clock.runFor(21000);await page.waitForFunction(()=>document.querySelector('#onlinePlayers').textContent.match(/٠|0/));
 unavailable=true;await page.clock.runFor(21000);await page.waitForFunction(()=>document.querySelector('#onlinePlayers').hidden);
 online=7;unavailable=false;await page.clock.runFor(21000);await page.waitForFunction(()=>!document.querySelector('#onlinePlayers').hidden&&document.querySelector('#onlinePlayers').textContent.match(/٧|7/));
 assert.deepEqual(errors,[]);console.log(JSON.stringify({origin,passed:true,checks:['home-count','home-only','authenticated-round-heartbeats','question-remains-active','background-leave','no-background-poll','resume','completion-leave','real-zero','failure-hides','recovery'],errors}));
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
