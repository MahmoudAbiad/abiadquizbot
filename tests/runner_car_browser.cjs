const {chromium}=require('playwright'),fs=require('fs'),path=require('path'),assert=require('assert/strict');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH,headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:390,height:844},isMobile:true,hasTouch:true});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 let w={coins:0,knowledge:6500,knowledge_earned:6500,car_level:0,equipped:'none',jetpack_owned:false,version:0,daily:{answered:0,target:5}},rid=0;
 const receipts=new Set(),mastered=new Set();let buys=[];
 await page.route('**/*',async route=>{const u=new URL(route.request().url());
  if(u.hostname==='telegram.org')return route.fulfill({contentType:'text/javascript',body:'window.Telegram={WebApp:{initData:"test",ready(){},expand(){},disableVerticalSwipes(){}}}'});
  if(u.pathname.startsWith('/api/runner/')){const op=u.pathname.split('/').pop(),b=route.request().postDataJSON();let data;
   if(op==='config')data={title:'اختبار التعلم والترقيات',can_edit:false,wallet:w,settings:{lives:1},questions:[]};
   if(op==='wallet')data=w;
   if(op==='buy-car'){buys.push(b.target_level);if(w.car_level<b.target_level){const price=[1000,500,5000][w.car_level];w={...w,knowledge:w.knowledge-price,car_level:b.target_level,equipped:'tesla',version:w.version+1};}data={...w,purchase_status:'purchased'};}
   if(op==='equip'){w={...w,equipped:b.item,version:w.version+1};data=w;}
   if(op==='start')data={round_id:'r'+(++rid),version:0,lives:1,total:1};
   if(op==='track')data={segment:0,equipment:w.equipped,car_level:w.car_level,claimed:[],objects:[{id:0,at:.7,kind:'coin',lane:0},{id:1,at:1,kind:'coin',lane:2},{id:2,at:1.3,kind:'coin',lane:0},{id:3,at:2,kind:'barrier',lane:0}]};
   if(op==='coins'){for(const id of b.coin_ids){const k=b.round_id+':'+id;if(!receipts.has(k)){receipts.add(k);w={...w,coins:w.coins+1,version:w.version+1};}}data={...w,accepted:b.coin_ids};}
   if(op==='question')data={text:'٢ + ٢؟',options:[{id:0,text:'٣'},{id:1,text:'٤'},{id:2,text:'٥'}],seconds:30,number:1,total:1,version:0};
   if(op==='ready')data={remaining_seconds:30};
   if(op==='answer'){const reward=mastered.has('q')?0:100;mastered.add('q');w={...w,knowledge:w.knowledge+reward,knowledge_earned:w.knowledge_earned+reward,version:w.version+1,daily:{answered:1,target:5}};data={correct:true,correct_id:1,correct_text:'٤',explanation:'٢ + ٢ = ٤',lives:1,score:100,status:'completed',version:1,knowledge_awarded:reward,wallet:w};}
   if(op==='finish')data={saved:true};return route.fulfill({json:data});}
  const f=path.join(__dirname,'..',u.pathname);return fs.existsSync(f)?route.fulfill({path:f}):route.abort();
 });
 const url='http://runner.test/webapp/runner/index.html?quiz_id=q',shots=process.env.RUNNER_SCREENSHOT_DIR||require('os').tmpdir();
 const load=async()=>{await page.goto(url);await page.waitForFunction(()=>!document.querySelector('#start').disabled);};
 const shop=async()=>{await page.click('#storeButton');await page.waitForSelector('#store:not([hidden])');};
 const start=async level=>{await page.click('#closeStore');await page.click('#start');await page.waitForFunction(l=>document.querySelector('#game').dataset.carLevel===String(l)&&document.querySelector('#game').dataset.phase==='RUN',level);};
 const answer=async()=>{await page.waitForFunction(()=>document.querySelector('#game').dataset.phase==='DECIDE');await page.locator('.lane').filter({hasText:'٤'}).click();await page.click('#confirm');await page.waitForSelector('#feedback:not([hidden])');};
 await load();await shop();assert.equal(await page.locator('.shop-item').count(),2);
 await page.dblclick('#buyCar');assert.equal(w.car_level,1);assert.equal(w.knowledge,5500);assert.deepEqual(buys,[1]);await start(1);
 assert.equal(await page.isVisible('#flight'),false);await page.waitForTimeout(4500);assert.equal(await page.textContent('#coins'),'0');await page.screenshot({path:path.join(shots,'runner-car-ground.png')});
 await load();await shop();await page.click('#buyCar');assert.equal(w.car_level,2);assert.equal(w.knowledge,5000);await start(2);
 assert.equal(await page.isVisible('#flight'),false);await page.waitForFunction(()=>Number(document.querySelector('#coins').textContent)===3);await page.screenshot({path:path.join(shots,'runner-car-magnet.png')});
 await load();await shop();await page.click('#buyCar');assert.equal(w.car_level,3);assert.equal(w.knowledge,0);assert.equal(w.knowledge_earned,6500);await start(3);
 assert.equal(await page.getAttribute('#flight','aria-pressed'),'true');await page.waitForTimeout(2000);await page.screenshot({path:path.join(shots,'runner-car-flight.png')});
 await answer();assert.ok((await page.textContent('#knowledgeReward')).includes('+100'));assert.equal(w.knowledge,100);assert.equal(w.knowledge_earned,6600);
 await load();assert.ok((await page.textContent('#dailyProgress')).includes('1 / 5'));await shop();await page.screenshot({path:path.join(shots,'runner-car-store.png')});assert.equal(await page.isDisabled('#buyCar'),true);await start(3);await answer();
 assert.equal(w.knowledge,100);assert.equal(w.knowledge_earned,6600);assert.ok((await page.textContent('#knowledgeReward')).includes('مراجعة'));
 await load();await shop();await page.click('#equipNone');assert.equal(w.equipped,'none');await load();await shop();assert.equal(await page.getAttribute('#equipNone','aria-pressed'),'true');
 await page.setViewportSize({width:320,height:568});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.deepEqual(errors,[]);
 console.log(JSON.stringify({twoPermanentItems:true,prices:[1000,500,5000],level1Ground:true,level2Magnet:true,level3Flight:true,achievementPreserved:true,masteryRewardOnce:true,dailyProgress:true,equipmentSaved:true,errors}));await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
