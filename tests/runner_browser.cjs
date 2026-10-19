// Local browser smoke test with mocked API; no production network calls.
const {chromium:pw}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs=require('fs');const path=require('path');
(async()=>{const browser=await pw.launch({...(process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH} : {}),args:["--no-sandbox","--no-zygote","--single-process","--disable-gpu","--disable-software-rasterizer"],headless:true});const page=await browser.newPage({viewport:{width:390,height:844},deviceScaleFactor:1,isMobile:true,hasTouch:true});let answer=0;const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{const url=new URL(route.request().url());if(url.hostname==='telegram.org'){return route.fulfill({contentType:'text/javascript',body:'window.Telegram={WebApp:{initData:"test",ready(){},expand(){},disableVerticalSwipes(){},close(){}}};'});}
 if(url.pathname.startsWith('/api/runner/')){const op=url.pathname.split('/').pop();let result;
 if(op==='config')result={title:'اختبار الرياضيات · رحلة تجريبية',can_edit:true,settings:{lives:1,version:0,ai_status:'new',manual_times:{},ai_times:{}},questions:[{key:'one',text:'ما ناتج ٢ + ٢؟',seconds:30}]};
 if(op==='start')result={round_id:'round',version:0,lives:1,total:2};
 if(op==='question')result={text:'ما ناتج ٢ + ٢؟',options:[{id:0,text:'٣'},{id:3,text:'٤'},{id:1,text:'٥'}],seconds:30,remaining_seconds:30,number:answer+1,total:2,version:answer};
 if(op==='answer'){const b=route.request().postDataJSON();const correct=b.option_id===3;answer++;result={correct,correct_text:'٤',explanation:'عند جمع العددين ٢ و٢ نحصل على ٤.',lives:correct?1:0,score:correct?100:0,status:correct?'running':'caught',version:answer};}
 if(op==='finish'||op==='settings')result={saved:true};return route.fulfill({json:result});}
 const file=path.join(__dirname,'..',url.pathname);if(fs.existsSync(file)&&fs.statSync(file).isFile())return route.fulfill({path:file});return route.abort();});
 // Deterministic coin-only sequence isolates question flow from obstacle randomness.
 await page.addInitScript('Math.random=()=>0.2');
 await page.goto('http://runner.test/webapp/runner/index.html?quiz_id=q');await page.waitForFunction(()=>!document.getElementById('start').disabled);
 await page.screenshot({path:(process.env.RUNNER_SCREENSHOT_DIR||'/tmp')+'/runner-home.png'});await page.click('#start');await page.waitForTimeout(9000);await page.waitForSelector('#question:not([hidden])');await page.screenshot({path:(process.env.RUNNER_SCREENSHOT_DIR||'/tmp')+'/runner-question.png'});
 await page.click('.lane:nth-child(2)');await page.click('#confirm');await page.waitForSelector('#feedback:not([hidden])');if(!(await page.textContent('#feedbackTitle')).includes('صحيحة'))throw Error('correct failed');
 await page.click('#continue');await page.waitForTimeout(9000);await page.waitForSelector('#question:not([hidden])');await page.click('.lane:nth-child(1)');await page.click('#confirm');await page.waitForSelector('#feedback:not([hidden])');if(!(await page.textContent('#feedbackTitle')).includes('الشرطي'))throw Error('wrong failed');await page.screenshot({path:(process.env.RUNNER_SCREENSHOT_DIR||'/tmp')+'/runner-caught.png'});
 await page.click('#music');await page.reload();await page.waitForSelector('#music.muted');
 console.log(JSON.stringify({flow:'start,run,question,correct,next,wrong,caught,music persistence',errors,overflow:await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)}));await browser.close();if(errors.length)process.exit(1);
})();
