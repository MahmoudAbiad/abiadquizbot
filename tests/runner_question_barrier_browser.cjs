// Real frontend/canvas, API fixtures; optional published test origin.
const {chromium}=require('playwright'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH,headless:true,args:['--no-sandbox']}),results=[];
 for(const scenario of [
  {name:'ground-correct',item:'none',correct:true},
  {name:'jetpack-correct',item:'jetpack',correct:true},
  {name:'tesla-wrong',item:'tesla',correct:false},
  {name:'jetpack-timeout',item:'jetpack',timeout:true},
  {name:'compact-long',item:'tesla',correct:true,compact:true,reduced:true}
 ]){
  const page=await browser.newPage({viewport:{width:390,height:scenario.compact?600:844}}),errors=[];let answers=[];
  page.on('pageerror',e=>errors.push(e.message));await page.clock.install();
  if(scenario.reduced)await page.emulateMedia({reducedMotion:'reduce'});
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());
   if(u.hostname==='telegram.org')return route.fulfill({contentType:'text/javascript',body:'window.Telegram={WebApp:{initData:"fixture",ready(){},expand(){},disableVerticalSwipes(){}}}'});
   if(u.pathname.startsWith('/api/runner/')){
    const op=u.pathname.split('/').pop(),wallet={coins:0,knowledge:0,knowledge_earned:0,jetpack_owned:true,car_level:3,equipped:scenario.item,version:0};let data;
    if(op==='config')data={title:'اختبار حاجز السؤال',wallet,can_edit:false,settings:{lives:3},questions:[]};
    if(op==='start')data={round_id:'r',version:0,lives:3,total:3};
    if(op==='track')data={equipment:scenario.item,car_level:3,objects:[],claimed:[]};
    if(op==='question')data={text:'ما ناتج ٢ + ٢؟',options:[{id:0,text:scenario.compact?'إجابة طويلة لاختبار التفاف النص '.repeat(7):'٣'},{id:1,text:'٤'},{id:2,text:'٥'}],seconds:30,number:1,total:3,version:0};
    if(op==='ready')data={remaining_seconds:scenario.timeout?5:30};
    if(op==='answer'){const b=route.request().postDataJSON();answers.push(b);data={correct:!!scenario.correct,correct_id:1,correct_text:'٤',explanation:'٢ + ٢ = ٤',lives:scenario.correct?3:2,score:scenario.correct?100:0,status:'running',version:1,knowledge_awarded:0};}
    if(op==='finish')data={saved:true};return route.fulfill({json:data});
   }
   if(process.env.RUNNER_TEST_ORIGIN)return route.continue();
   return route.fulfill({path:path.join(__dirname,'..',u.pathname)});
  });
  await page.goto((process.env.RUNNER_TEST_ORIGIN||'http://runner.test')+'/webapp/runner/index.html?quiz_id=q',{waitUntil:'domcontentloaded',timeout:90000});
  await page.waitForFunction(()=>!document.querySelector('#start').disabled);await page.click('#start');await page.waitForFunction(()=>document.querySelector('#game').dataset.phase==='RUN');
  await page.clock.runFor(1000);const far=Number(await page.getAttribute('#game','data-barrier-z'));
  assert.ok(far<0);assert.equal(await page.locator('#question').isVisible(),false);
  await page.clock.runFor(6000);const nearer=Number(await page.getAttribute('#game','data-barrier-z'));assert.ok(nearer>far&&nearer<.62);
  const shots=process.env.RUNNER_SCREENSHOT_DIR||require('os').tmpdir();
  if(scenario.name==='jetpack-correct')await page.screenshot({path:path.join(shots,'runner-road-barrier-approaching.png')});
  await page.clock.runFor(6000);await page.waitForSelector('#question:not([hidden])');
  assert.equal(await page.locator('#choices').evaluate(e=>e.classList.contains('gated')),false);
  assert.equal(await page.getAttribute('#game','data-barrier'),'closed');assert.equal(await page.locator('.lane').count(),3);
  if(scenario.name==='jetpack-correct'||scenario.compact)await page.screenshot({path:path.join(shots,'runner-road-barrier-'+scenario.name+'-question.png')});
  const overlaps=await page.evaluate(()=>{const q=document.querySelector('#question').getBoundingClientRect();return [...document.querySelectorAll('.lane')].some(e=>{const r=e.getBoundingClientRect();return r.top<q.bottom||r.left<0||r.right>innerWidth;});});assert.equal(overlaps,false);
  if(!scenario.timeout){await page.locator('.lane').nth(scenario.correct?1:0).click();await page.click('#confirm');}
  else await page.clock.runFor(5100);
  if(!scenario.reduced){await page.clock.runFor(180);assert.equal(await page.getAttribute('#game','data-barrier'),scenario.correct?'open':'impact');}
  await page.clock.runFor(650);await page.waitForSelector('#feedback:not([hidden])');assert.equal(answers.length,1);
  assert.equal(answers[0].option_id,scenario.timeout?null:scenario.correct?1:0);
  assert.equal(await page.textContent('#lives'),scenario.correct?'3':'2');
  await page.click('#continue');await page.waitForFunction(()=>document.querySelector('#game').dataset.phase==='RUN');
  assert.equal(await page.getAttribute('#flight','aria-pressed'),scenario.item==='none'?'false':'true');
  assert.deepEqual(errors,[]);results.push({scenario:scenario.name,persistentApproach:true,classicAnswers:true,outcome:scenario.correct?'open-and-pass':'collision',flightPreserved:true,errors});await page.close();
 }
 await browser.close();console.log(JSON.stringify({origin:process.env.RUNNER_TEST_ORIGIN||'local',api:'fixtures',results}));
})().catch(e=>{console.error(e);process.exit(1)});
