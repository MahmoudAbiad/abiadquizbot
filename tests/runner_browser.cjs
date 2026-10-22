// Local browser smoke test with mocked API; no production network calls.
const {chromium:pw}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs=require('fs');const path=require('path');
(async()=>{const browser=await pw.launch({...(process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH} : {}),args:["--no-sandbox"],headless:true});const page=await browser.newPage({viewport:{width:390,height:844},deviceScaleFactor:1,isMobile:true,hasTouch:true});let answer=0,mode='normal';const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{const url=new URL(route.request().url());if(url.hostname==='telegram.org'){return route.fulfill({contentType:'text/javascript',body:'window.Telegram={WebApp:{initData:"test",ready(){},expand(){},disableVerticalSwipes(){},close(){}}};'});}
 if(url.pathname.startsWith('/api/runner/')){const op=url.pathname.split('/').pop();let result;
 if(op==='config')result={title:'اختبار الرياضيات · رحلة تجريبية',can_edit:true,settings:{lives:1,version:0,ai_status:'new',manual_times:{},ai_times:{}},questions:[{key:'one',text:'ما ناتج ٢ + ٢؟',seconds:30}]};
 if(op==='track')result={segment:answer,objects:Array.from({length:10},(_,id)=>({id,at:.7*(id+1),lane:0,kind:'coin'})),claimed:[]};
 if(op==='coins')result={coins:route.request().postDataJSON().coin_ids.length,version:1,jetpack_owned:false,accepted:route.request().postDataJSON().coin_ids};
 if(op==='start'){answer=0;result={round_id:'round',version:0,lives:1,total:2};}
 if(op==='question')result={text:'ما ناتج ٢ + ٢؟',options:[{id:0,text:mode==='long'?'خيار عربي طويل '.repeat(8):'٣'},{id:3,text:'٤'},{id:1,text:'٥'}],seconds:30,remaining_seconds:30,started:false,number:answer+1,total:2,version:answer};
 if(op==='ready')result={started:true,remaining_seconds:mode==='timeout'?.08:30};
 if(op==='answer'){const b=route.request().postDataJSON();const correct=b.option_id===3;answer++;result={correct,correct_id:3,correct_text:'٤',explanation:'عند جمع العددين ٢ و٢ نحصل على ٤.',lives:correct?1:0,score:correct?100:0,status:correct?'running':'caught',version:answer};}
 if(op==='finish'||op==='settings')result={saved:true};return route.fulfill({json:result});}
 const file=path.join(__dirname,'..',url.pathname);if(fs.existsSync(file)&&fs.statSync(file).isFile())return route.fulfill({path:file});return route.abort();});
 // Deterministic coin-only sequence isolates question flow from obstacle randomness.
 await page.addInitScript('Math.random=()=>0.2');
 await page.goto('http://runner.test/webapp/runner/index.html?quiz_id=q');await page.waitForFunction(()=>!document.getElementById('start').disabled);
 const shots=process.env.RUNNER_SCREENSHOT_DIR||require('os').tmpdir();
 const snap=async name=>page.screenshot({path:path.join(shots,'runner-'+name+'.png')});
 const phase=async value=>page.waitForFunction(p=>document.getElementById('game').dataset.phase===p,value);
 await snap('home');await page.click('#start');await phase('RUN');await page.keyboard.press('ArrowLeft');await phase('APPROACH');if(Number(await page.textContent('#coins'))<=0)throw Error('keyboard lane/coin collection failed');await snap('approach');await phase('DECIDE');
 await page.waitForSelector('#question:not([hidden])');
 if(await page.locator('#choices').evaluate(e=>e.classList.contains('gated')))throw Error('answers must use the classic panel');
 const overlap=await page.evaluate(()=>{const q=document.querySelector('#question').getBoundingClientRect();return [...document.querySelectorAll('.lane')].some(e=>{const r=e.getBoundingClientRect();return r.top<q.bottom||r.right>innerWidth||r.left<0;});});
 if(overlap)throw Error('gate/question overlap or overflow');await snap('question');
 await page.click('.lane:nth-child(2)');await page.click('#confirm');await phase('RESOLVE');await snap('resolve');
 await page.waitForSelector('#feedback:not([hidden])');if(!(await page.textContent('#feedbackTitle')).includes('صحيحة'))throw Error('correct failed');
 await page.click('#continue');await phase('DECIDE');await page.click('.lane:nth-child(1)');await page.click('#confirm');await page.waitForSelector('#feedback:not([hidden])');if(!(await page.textContent('#feedbackTitle')).includes('الشرطي'))throw Error('wrong failed');await snap('caught');
 await page.click('#continue');mode='timeout';await page.click('#start');await page.waitForSelector('#feedback:not([hidden])');if(!(await page.textContent('#feedbackTitle')).includes('انتهى وقت'))throw Error('timeout failed');await snap('timeout');
 await page.click('#continue');mode='long';await page.emulateMedia({reducedMotion:'reduce'});await page.reload();await page.waitForFunction(()=>!document.getElementById('start').disabled);await page.click('#start');await phase('DECIDE');
 if(await page.locator('#choices').evaluate(e=>e.classList.contains('gated')))throw Error('long option fallback missing');await snap('classic');
 await page.click('#music');await page.reload();await page.waitForSelector('#music.muted');
 console.log(JSON.stringify({flow:'RUN,APPROACH,DECIDE,RESOLVE,correct,wrong,timeout,classic fallback,reduced motion,music persistence',errors,overflow:await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)}));await browser.close();if(errors.length)process.exit(1);
})();
