// Browser integration: durable balance, one-time debit, unlimited flight, offline receipts.
const {chromium}=require('playwright'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH,args:['--no-sandbox'],headless:true});
 const page=await browser.newPage({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
 let wallet={coins:100,jetpack_owned:false,version:0,knowledge:0,knowledge_earned:0,car_level:0,equipped:'none'},buyCalls=0,offline=false,claimed=new Set();const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 const init=new URLSearchParams({user:JSON.stringify({id:7})}).toString();
 await page.route('**/*',async route=>{
  const u=new URL(route.request().url());
  if(u.hostname==='telegram.org')return route.fulfill({contentType:'text/javascript',body:`window.Telegram={WebApp:{initData:${JSON.stringify(init)},ready(){},expand(){},disableVerticalSwipes(){}}}`});
  if(u.pathname.startsWith('/api/runner/')){
   const op=u.pathname.split('/').pop(),body=route.request().postDataJSON();let result;
   if(op==='config')result={title:'رحلة تجريبية',wallet,can_edit:false,settings:{lives:1},questions:[]};
   if(op==='wallet')result=wallet;
   if(op==='buy-jetpack'){buyCalls++;if(!wallet.jetpack_owned){wallet={coins:wallet.coins-100,jetpack_owned:true,equipped:'jetpack',version:wallet.version+1};}result={...wallet,purchase_status:'purchased'};}
   if(op==='start')result={round_id:'r',lives:1,version:0,total:1};
   if(op==='track')result={segment:0,claimed:[],objects:[{id:0,at:.7,lane:1,kind:'coin'},{id:1,at:1.4,lane:1,kind:'barrier'},{id:2,at:2.1,lane:1,kind:'barrier'}]};
   if(op==='coins'){if(offline)return route.abort('internetdisconnected');for(const id of body.coin_ids)if(!claimed.has(id)){claimed.add(id);wallet={...wallet,coins:wallet.coins+1,version:wallet.version+1};}result={...wallet,accepted:[...claimed]};}
   if(op==='question')result={text:'٢ + ٢؟',options:[{id:0,text:'٣'},{id:1,text:'٤'},{id:2,text:'٥'}],number:1,total:1,seconds:30,version:0};
   if(op==='ready')result={remaining_seconds:30};
   if(op==='answer')result={correct:true,correct_id:1,correct_text:'٤',score:100,lives:1,version:1,status:'completed'};
   if(op==='finish')result={saved:true};
   return route.fulfill({json:result});
  }
  const file=path.join(__dirname,'..',u.pathname);return fs.existsSync(file)?route.fulfill({path:file}):route.abort();
 });
 const shots=process.env.RUNNER_SCREENSHOT_DIR||require('os').tmpdir();
 await page.goto('http://runner.test/webapp/runner/index.html?quiz_id=q');await page.waitForFunction(()=>!document.querySelector('#start').disabled);
 assert.equal(await page.textContent('#homeBalance'),'100');await page.click('#storeButton');
 await page.screenshot({path:path.join(shots,'runner-store.png')});
 await page.dblclick('#buyJetpack');await page.waitForFunction(()=>document.querySelector('#buyJetpack').textContent.includes('تمتلك'));
 assert.equal(wallet.coins,0);assert.equal(buyCalls,1);
 await page.reload();await page.waitForFunction(()=>!document.querySelector('#start').disabled);
 assert.equal(await page.textContent('#homeBalance'),'0');await page.click('#storeButton');assert.equal(await page.isDisabled('#buyJetpack'),true);
 await page.screenshot({path:path.join(shots,'runner-store-owned.png')});await page.click('#closeStore');
 offline=true;await page.click('#start');await page.waitForFunction(()=>document.querySelector('#game').dataset.phase==='RUN');
 assert.equal(await page.getAttribute('#flight','aria-pressed'),'true');
 await page.click('#flight');assert.equal(await page.getAttribute('#flight','aria-pressed'),'false');await page.click('#flight');
 await page.waitForFunction(()=>Number(document.querySelector('#coins').textContent)>0);
 await page.screenshot({path:path.join(shots,'runner-flight.png')});
 await page.waitForFunction(()=>document.querySelector('#game').dataset.phase==='DECIDE');
 assert.equal(await page.textContent('#lives'),'1');assert.equal(await page.isVisible('#question'),true);assert.equal(claimed.size,0);
 await page.reload();await page.waitForFunction(()=>!document.querySelector('#start').disabled);
 assert.equal(await page.textContent('#homeBalance'),'1'); // pending receipt restored before network recovery
 offline=false;await page.click('#storeButton');await page.waitForFunction(()=>document.querySelector('#saveStatus').textContent.includes('الرصيد محفوظ'));
 assert.equal(wallet.coins,1);assert.equal(wallet.jetpack_owned,true);
 await page.reload();await page.waitForFunction(()=>!document.querySelector('#start').disabled);assert.equal(await page.textContent('#homeBalance'),'1');
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.deepEqual(errors,[]);
 await page.setViewportSize({width:320,height:568});await page.click('#storeButton');assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 console.log(JSON.stringify({balanceSurvivesReload:true,permanentOwnership:true,singleDebit:buyCalls===1,flightAvoidsTwoBarriers:true,questionsWhileFlying:true,offlineReceiptRecovered:true,errors}));
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
