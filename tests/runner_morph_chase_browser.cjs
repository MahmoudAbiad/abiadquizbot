// Real DOM, canvas and Web Animations; API fixtures, no production mutations.
const {chromium}=require('playwright'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH,headless:true,args:['--no-sandbox']}),results=[];
 for(const mode of ['math-correct','math-wrong','math-timeout','plain-reduced','chase-escape','chase-rapid']){
  const page=await browser.newPage({viewport:{width:390,height:844}}),calls=[],errors=[];let answered=0;
  const math=mode.startsWith('math'),chasing=mode.startsWith('chase'),reduced=mode==='plain-reduced';
  page.on('pageerror',e=>errors.push(e.message));await page.clock.install({time:new Date('2026-10-22T12:00:00Z')});await page.clock.pauseAt(new Date('2026-10-22T12:00:01Z'));
  if(reduced)await page.emulateMedia({reducedMotion:'reduce'});
  await page.addInitScript(()=>{
   window.wallCopies=0;window.holdMorph=false;
   const p=CanvasRenderingContext2D.prototype,fill=p.fillRect,draw=p.drawImage,animate=HTMLElement.prototype.animate;
   p.fillRect=function(x,y,w,h){if(this.canvas.id==='world'&&x===0&&y===0&&h>500)window.wallCopies=0;return fill.call(this,x,y,w,h);};
   p.drawImage=function(source,...args){if(this.canvas.id==='world'&&source.tagName==='CANVAS'&&source.width===640&&source.height===360)window.wallCopies++;return draw.call(this,source,...args);};
   HTMLElement.prototype.animate=function(frames,options){const animation=animate.call(this,frames,options);if(this.id==='question'){window.lastMorph=animation;window.morphFrames=frames;if(window.holdMorph)animation.pause();}return animation;};
  });
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());
   if(u.hostname==='telegram.org')return route.fulfill({contentType:'text/javascript',body:'window.Telegram={WebApp:{initData:"fixture",ready(){},expand(){},disableVerticalSwipes(){}}}'});
   if(u.pathname.endsWith('.svg'))return route.fulfill({contentType:'image/svg+xml',body:`<svg xmlns="http://www.w3.org/2000/svg" width="600" height="180"><rect width="600" height="180" fill="white"/><text x="140" y="115" font-size="65">${u.pathname.includes('question')?'2 + 2 = ?':u.pathname.includes('option2')?'4':'3'}</text></svg>`});
   if(u.pathname.startsWith('/api/runner/')){
    const op=u.pathname.split('/').pop(),b=route.request().postDataJSON();calls.push({op,...b});let data;
    if(op==='config')data={title:'انتقال السؤال والمطاردة',wallet:{coins:0,knowledge:0,knowledge_earned:0,jetpack_owned:false,car_level:0,equipped:'none',version:0},settings:{lives:3},questions:[]};
    if(op==='start'){answered=0;data={round_id:'r',version:0,lives:3,total:4};}
    if(op==='track')data={equipment:'none',car_level:0,claimed:[],objects:!chasing?[]:mode==='chase-rapid'?[{id:0,at:.7,lane:1,kind:'barrier'},{id:1,at:1.4,lane:1,kind:'barrier'}]:b.segment<2?[{id:0,at:.7,lane:1,kind:'barrier'}]:[]};
    if(op==='question')data={text:math?'ما ناتج $2+2$؟':'ما عاصمة سورية؟',game_image_url:math?'/question.svg':null,options:[{id:0,text:'$3$',image_url:math?'/option0.svg':null},{id:1,text:'$5$',image_url:math?'/option1.svg':null},{id:2,text:math?'$\\frac{8}{2}$':'دمشق',image_url:math?'/option2.svg':null}],number:answered+1,total:4,seconds:30,version:answered};
    if(op==='ready')data={remaining_seconds:mode==='math-timeout'?.5:30};
    if(op==='answer'){const correct=mode!=='math-wrong'&&mode!=='math-timeout';data={correct,correct_id:2,correct_text:math?'$\\frac{8}{2}$':'دمشق',explanation:'شرح محفوظ',lives:correct?3:2,score:100,status:'running',version:++answered};}
    if(op==='finish')data={saved:true};return route.fulfill({json:data});
   }
   if(process.env.RUNNER_TEST_ORIGIN)return route.continue();return route.fulfill({path:path.join(__dirname,'..',u.pathname)});
  });
  const url=(process.env.RUNNER_TEST_ORIGIN||'http://runner.test')+'/webapp/runner/index.html?quiz_id=q',shots=process.env.RUNNER_SCREENSHOT_DIR||require('os').tmpdir();
  await page.goto(url,{waitUntil:'domcontentloaded',timeout:90000});await page.waitForFunction(()=>!document.querySelector('#start').disabled);await page.click('#start');await page.waitForFunction(()=>document.querySelector('#game').dataset.phase==='RUN');
  if(mode==='chase-rapid'){
   await page.clock.runFor(4500);await page.waitForSelector('#feedback:not([hidden])');assert.ok((await page.textContent('#feedbackTitle')).includes('الشرطي'));assert.equal(calls.filter(x=>x.op==='finish').length,1);assert.equal(await page.getAttribute('#game','data-chase'),'caught');
  }else if(mode==='chase-escape'){
   await page.clock.runFor(14000);await page.waitForSelector('#choices:not([hidden])');const remaining=Number(await page.getAttribute('#game','data-chase-remaining'));assert.ok(remaining>0&&remaining<2);
   await page.clock.runFor(5000);assert.equal(Number(await page.getAttribute('#game','data-chase-remaining')),remaining);
   await page.locator('.lane').nth(2).click({force:true});await page.click('#confirm',{force:true});await page.clock.runFor(700);await page.waitForSelector('#feedback:not([hidden])');
   await page.clock.runFor(5000);assert.equal(Number(await page.getAttribute('#game','data-chase-remaining')),remaining);
   await page.click('#continue',{force:true});await page.waitForFunction(()=>document.querySelector('#game').dataset.phase==='RUN');await page.clock.runFor(2800);
   assert.equal(await page.getAttribute('#game','data-chase'),'clear');await page.screenshot({path:path.join(shots,'runner-cop-escaped.png')});
   await page.clock.runFor(1000);assert.equal(await page.getAttribute('#game','data-chase'),'near');assert.equal(calls.filter(x=>x.op==='finish').length,0);
   await page.reload({waitUntil:'domcontentloaded'});await page.waitForFunction(()=>!document.querySelector('#start').disabled);await page.click('#start');await page.waitForFunction(()=>document.querySelector('#game').dataset.phase==='RUN');await page.clock.runFor(20);assert.equal(await page.getAttribute('#game','data-chase'),'clear');
  }else{
   await page.evaluate(()=>{window.holdMorph=true;});await page.clock.runFor(14000);
   if(!reduced){
    await page.waitForFunction(()=>window.lastMorph?.playState==='paused');assert.equal(calls.filter(x=>x.op==='ready').length,0);assert.equal(await page.getAttribute('#game','data-question-presentation'),'morphing');assert.equal(await page.evaluate(()=>window.wallCopies),0);assert.equal(await page.locator('#choices').isVisible(),false);
    await page.evaluate(()=>{window.lastMorph.currentTime=210;});assert.notEqual(await page.locator('#question').evaluate(e=>getComputedStyle(e).transform),'none');await page.screenshot({path:path.join(shots,'runner-question-morph-'+mode+'.png')});
    await page.evaluate(()=>{window.lastMorph.finish();});
   }
   await page.waitForSelector('#choices:not([hidden])');assert.equal(await page.getAttribute('#game','data-question-presentation'),'answer');assert.equal(await page.evaluate(()=>window.wallCopies),0);assert.equal(calls.filter(x=>x.op==='ready').length,1);
   if(math)await page.evaluate(()=>{window.correctAnswerImage=document.querySelectorAll('.lane')[2].querySelector('img');});
   if(mode==='math-timeout')await page.clock.runFor(1100);else{await page.locator('.lane').nth(mode==='math-wrong'?0:2).click({force:true});await page.click('#confirm',{force:true});}
   await page.clock.runFor(700);await page.waitForSelector('#feedback:not([hidden])');
   if(math){assert.equal(await page.locator('#answerText img').count(),1);assert.ok((await page.getAttribute('#answerText img','src')).endsWith('/option2.svg'));assert.equal(await page.evaluate(()=>window.correctAnswerImage===document.querySelector('#answerText img')),true);assert.ok(!(await page.textContent('#answerText')).includes('\\frac'));await page.screenshot({path:path.join(shots,'runner-correct-answer-image-'+mode+'.png')});}
   else assert.ok((await page.textContent('#answerText')).includes('دمشق'));
   await page.click('#continue',{force:true});await page.waitForFunction(()=>document.querySelector('#game').dataset.phase==='RUN');await page.clock.runFor(100);assert.equal(await page.evaluate(()=>window.wallCopies),0); // Passed wall never regains its question.
  }
  assert.deepEqual(errors,[]);results.push({mode,passed:true,errors});await page.close();
 }
 await browser.close();console.log(JSON.stringify({origin:process.env.RUNNER_TEST_ORIGIN||'local',api:'fixtures',results}));
})().catch(e=>{console.error(e);process.exit(1)});
