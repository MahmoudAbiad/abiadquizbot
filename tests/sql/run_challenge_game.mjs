// Run in an isolated directory with @electric-sql/pglite@0.3.10 installed.
// node run_challenge_game.mjs /absolute/path/to/abiadquizbot
// Real PostgreSQL 17 (WASM), single connection: NOT a multi-process concurrency test.
import { PGlite } from '@electric-sql/pglite';
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
const root = process.argv[2];
if (!root) throw new Error('Pass repository absolute path');
const read = p => fs.readFileSync(path.join(root,p),'utf8');
const rules = JSON.parse(read('services/challenge_rules.json'));
const db = new PGlite();
let passed=0;
const test=async(name,fn)=>{await fn();passed++;console.log(`PASS ${passed}: ${name}`)};
const query=async(sql,values=[]) => (await db.query(sql,values)).rows;
const one=async(sql,values=[]) => (await query(sql,values))[0];
const rpc=async(sql,values=[]) => (await one('select '+sql+' as value',values)).value;
const fail=async(sql,values,code)=>assert.rejects(()=>query(sql,values),e=>e.message.includes(code));
await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
CREATE TABLE feature_flags(key text PRIMARY KEY,enabled boolean NOT NULL,updated_at timestamptz DEFAULT now());
CREATE TABLE users(user_id bigint PRIMARY KEY,points integer); INSERT INTO users VALUES(11,1234),(22,4321);`);
await db.exec(read('db/migrations/0008_challenge_content.sql'));
await test('phase 1: 12 SQL scenarios',()=>db.exec(read('tests/sql/challenge_content_test.sql')));
await db.exec(read('db/migrations/0009_challenge_game.sql'));
await test('migration can be reapplied',()=>db.exec(read('db/migrations/0009_challenge_game.sql')));
await db.exec("UPDATE feature_flags SET enabled=true WHERE key IN('challenge_system_enabled','async_attacks_enabled')");
const b=(await one("insert into challenge_branches(name) values('علمي') returning id")).id;
const s=(await one("insert into challenge_subjects(branch_id,name) values($1,'فيزياء') returning id",[b])).id;
const u=(await one("insert into challenge_units(subject_id,name) values($1,'حركة') returning id",[s])).id;
const empty=(await one("insert into challenge_units(subject_id,name) values($1,'فارغة') returning id",[s])).id;
const qs=Array.from({length:10},(_,i)=>({question:`س${i}`,options:['أ','ب'],correct_option_id:0}));
const t=(await rpc("challenge_import_test($1,'official','json_upload',null,'hash1',1,$2::jsonb)",[u,JSON.stringify(qs)])).test_id;
for(const user of [11,22,33,44,55,66,77,88,99,111,122,133,144,155]) await rpc('challenge_touch_profile($1,$2,$3)',[user,'student'+user,b]);
const start=async(user,target,key,rule=rules,unit=u,testId=t)=>rpc('challenge_start_attack($1,$2,$3,$4,$5,$6::jsonb)',[user,target,unit,testId,key,JSON.stringify(rule)]);
const advance=async(id,user,action='resume',idx=null,opt=null,key=null)=>rpc('challenge_advance_attack($1,$2,$3,$4,$5,$6)',[id,user,action,idx,opt,key]);
const answerAll=async(state,user,correct=10)=>{for(let i=state.index;i<state.total;i++)state=await advance(state.id,user,'answer',i,i<correct?0:1,`${state.id}-${i}`);return state};
let m;
await test('lazy profile for old paid-points user is idempotent',async()=>{
 await rpc('challenge_touch_profile(11,\'renamed\',$1)',[b]);
 assert.equal((await one('select count(*)::int n from challenge_profiles where user_id=11')).n,1);
 assert.equal((await one('select xp from challenge_profiles where user_id=11')).xp,0);
});
await test('self attack rejected',()=>fail('select challenge_start_attack(11,11,$1,$2,\'self\',$3)',[u,t,JSON.stringify(rules)],'self_attack'));
await test('start pins snapshot, no answers leak; duplicate start uses same ID',async()=>{
 m=await start(11,22,'first'); assert.equal(m.status,'in_progress');assert.equal(m.total,10);
 assert.ok(!JSON.stringify(m).includes('correct_option_id'));
 assert.equal((await start(11,22,'first')).id,m.id);
 assert.equal((await one('select count(*)::int n from challenge_match_players where match_id=$1',[m.id])).n,2);
});
await test('one active attack per attacker',()=>fail('select challenge_start_attack(11,33,$1,$2,\'other\',$3)',[u,t,JSON.stringify(rules)],'active_attack'));
await test('wrong actor cannot read or answer',async()=>{
 await fail('select challenge_attack_state($1,33)',[m.id],'not_your_attack');
 await fail('select challenge_advance_attack($1,33,\'answer\',0,0,\'bad\')',[m.id],'not_your_attack');
});
await test('duplicate answers and Telegram retries do not consume another question',async()=>{
 m=await advance(m.id,11,'answer',0,0,'telegram-1');assert.equal(m.index,1);
 let retry=await advance(m.id,11,'answer',0,1,'telegram-1');assert.equal(retry.index,1);
 retry=await advance(m.id,11,'answer',0,1,'telegram-2');assert.equal(retry.index,1);
 assert.equal((await one('select count(*)::int n from challenge_match_answers where match_id=$1',[m.id])).n,1);
});
await test('out-of-range answers rejected',()=>fail('select challenge_advance_attack($1,11,\'answer\',1,99,\'invalid\')',[m.id],'invalid_option'));
await test('disable during attack preserves snapshot; blocks new attack',async()=>{
 await query("update challenge_tests set status='disabled' where id=$1",[t]);
 await fail('select challenge_start_attack(33,22,$1,$2,\'disabled\',$3)',[u,t,JSON.stringify(rules)],'no_content');
 m=await answerAll(m,11);assert.equal(m.result.stars,3);assert.equal(m.result.xp,100);assert.equal(m.result.trophies,15);
 await query("update challenge_tests set status='active' where id=$1",[t]);
});
await test('settlement is idempotent, defense and subject/unit stats persisted',async()=>{
 await advance(m.id,11,'answer',9,0,'again');
 let p=await one('select * from challenge_profiles where user_id=11');assert.equal(p.xp,100);assert.equal(p.wins,1);assert.equal(p.win_streak,1);
 assert.equal((await one('select defense_losses n from challenge_profiles where user_id=22')).n,1);
 assert.equal((await one('select xp from challenge_towers where user_id=11')).xp,100);
 assert.equal((await one('select wins from challenge_unit_stats where user_id=11')).wins,1);
});
await test('same target cooldown rejected after completion',()=>fail('select challenge_start_attack(11,22,$1,$2,\'repeat-target\',$3)',[u,t,JSON.stringify(rules)],'target_cooldown'));
await test('same content versus a new target awards no XP or trophies',async()=>{
 let a=await answerAll(await start(11,33,'repeat-content'),11);assert.equal(a.result.stars,3);assert.equal(a.result.xp,0);assert.equal(a.result.trophies,0);
});
await test('question deadline rejects arriving answer, counts timeout and advances once',async()=>{
 let a=await start(33,22,'timeout');
 await query("update challenge_matches set question_deadline=clock_timestamp()-interval '1 millisecond' where id=$1",[a.id]);
 a=await advance(a.id,33,'answer',0,0,'late');assert.equal(a.index,1);
 const ans=await one('select * from challenge_match_answers where match_id=$1',[a.id]);assert.equal(ans.is_correct,false);assert.equal(ans.score,0);assert.equal(ans.timed_out,true);
 a=await advance(a.id,33,'tick');assert.equal(a.index,1);
 await advance(a.id,33,'abandon');
});
await test('overall expiry records a permanent loss with zero XP',async()=>{
 let a=await start(44,22,'expire');await query("update challenge_matches set server_deadline=clock_timestamp()-interval '1 millisecond' where id=$1",[a.id]);
 a=await advance(a.id,44);assert.equal(a.status,'expired');assert.equal(a.result.xp,0);assert.equal(a.result.stars,0);
});
await test('empty and archived units reject new attacks, old history survives',async()=>{
 await fail('select challenge_start_attack(55,22,$1,$2,\'empty\',$3)',[empty,t,JSON.stringify(rules)],'no_content');
 await query("update challenge_units set status='archived' where id=$1",[u]);
 await fail('select challenge_start_attack(55,22,$1,$2,\'archived\',$3)',[u,t,JSON.stringify(rules)],'no_content');
 assert.equal((await rpc('challenge_attack_state($1,11)',[m.id])).result.stars,3);
 await query("update challenge_units set status='active' where id=$1",[u]);
});
await test('failure during final reward update rolls back answer and all stats',async()=>{
 let a=await start(55,22,'rollback');for(let i=0;i<9;i++)a=await advance(a.id,55,'answer',i,0,`rollback-${i}`);
 await db.exec(`CREATE FUNCTION fail_tower_test() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected_failure'; END $$;
 CREATE TRIGGER test_fail BEFORE INSERT ON challenge_towers FOR EACH ROW EXECUTE FUNCTION fail_tower_test();`);
 await fail("select challenge_advance_attack($1,55,'answer',9,0,'final-retry')",[a.id],'injected_failure');
 assert.equal((await one('select count(*)::int n from challenge_match_answers where match_id=$1',[a.id])).n,9);
 assert.equal((await one('select xp from challenge_profiles where user_id=55')).xp,0);
 assert.equal((await one('select question_index n from challenge_matches where id=$1',[a.id])).n,9);
 await db.exec('DROP TRIGGER test_fail ON challenge_towers; DROP FUNCTION fail_tower_test();');
 a=await advance(a.id,55,'answer',9,0,'final-retry');assert.equal(a.result.xp,100);
});
await test('daily attack limit, XP cap and trophy cap enforced',async()=>{
 const cap={...rules,daily_xp_limit:25,daily_trophy_limit:2,daily_attack_limit:1};
 let a=await answerAll(await start(66,22,'cap',cap),66);assert.equal(a.result.xp,25);assert.equal(a.result.trophies,2);
 await fail("select challenge_start_attack(66,33,$1,$2,'cap2',$3)",[u,t,JSON.stringify(cap)],'daily_limit');
});
await test('abandon even after correct answers cannot award XP',async()=>{
 let a=await start(77,22,'abandon');for(let i=0;i<8;i++)a=await advance(a.id,77,'answer',i,0,`abandon-${i}`);
 a=await advance(a.id,77,'abandon');assert.equal(a.result.xp,0);assert.equal(a.result.stars,0);
});
await test('new attacks honor flags but running attacks can finish',async()=>{
 let a=await start(88,22,'flag');await db.exec("update feature_flags set enabled=false where key='async_attacks_enabled'");
 await fail("select challenge_start_attack(99,22,$1,$2,'blocked',$3)",[u,t,JSON.stringify(rules)],'game_disabled');
 a=await answerAll(a,88,8);assert.equal(a.result.stars,2);assert.equal(a.result.xp,80);
 await db.exec("update feature_flags set enabled=true where key='async_attacks_enabled'");
});
await test('failed five-of-ten attack adds no XP',async()=>{
 let a=await answerAll(await start(99,22,'low'),99,5);assert.equal(a.result.stars,0);assert.equal(a.result.xp,0);
});
await test('search uses branch and excludes own land',async()=>{
 let d=await rpc('challenge_find_land(11)');assert.notEqual(d.user_id,11);
 const plan=await query("explain select user_id from challenge_profiles where branch_id=$1 and trophies between 0 and 500 and (trophies,user_id)>(0,0) order by trophies,user_id limit 1",[b]);
 assert.ok(JSON.stringify(plan).includes('challenge_profiles_matchmaking'));
});
await test('notification claims are once-only and completion pushes are throttled',async()=>{
 const first=await query('select * from challenge_claim_notifications(100)');
 assert.equal(first.filter(x=>x.user_id===22&&x.kind==='attack_completed').length,1);
 assert.equal((await query('select * from challenge_claim_notifications(100)')).length,0);
});
await test('opt-out notifications never delivered',async()=>{
 await db.exec('update challenge_profiles set notifications_enabled=false where user_id=122');
 await answerAll(await start(111,122,'optout'),111);
 assert.equal((await query('select * from challenge_claim_notifications(100)')).filter(x=>x.user_id===122).length,0);
});
await test('offline defender gets history and completion only',async()=>{
 await db.exec("update challenge_profiles set last_seen=clock_timestamp()-interval '1 day' where user_id=144");
 let a=await start(133,144,'offline');
 assert.equal((await query('select * from challenge_notifications where match_id=$1',[a.id])).length,0);
 a=await answerAll(a,133,6);assert.equal(a.result.stars,1);
 assert.equal((await query('select * from challenge_notifications where match_id=$1',[a.id])).length,1);
});
await test('server snapshots remain unchanged after rules change',async()=>{
 let a=await start(155,22,'snapshot');
 const row=await one('select rules,snapshot from challenge_matches where id=$1',[a.id]);
 assert.equal(row.rules.question_seconds,30);assert.equal(row.snapshot.length,10);
 await advance(a.id,155,'abandon');
});
await test('anon/authenticated cannot read private data or execute game RPCs',async()=>{
 for(const role of ['anon','authenticated']) {
 await db.exec(`SET ROLE ${role}`);
 await fail('select snapshot from challenge_matches',[],'permission denied');
 await fail("select challenge_attack_state($1,11)",[m.id],'permission denied');
 await db.exec('RESET ROLE');
 }
});
await test('service_role can read and use safe RPCs without default grants',async()=>{
 await db.exec('GRANT SELECT ON feature_flags TO service_role; SET ROLE service_role');
 assert.equal((await rpc('challenge_attack_state($1,11)',[m.id])).result.stars,3);
 await db.exec('RESET ROLE');
});
await test('paid points unchanged through every attack',async()=>{
 assert.deepEqual(await query('select * from users order by user_id'),[{user_id:11,points:1234},{user_id:22,points:4321}]);
});
await test('SQL scoring agrees with config boundaries',async()=>{
 for(const elapsed of [0,1,15,29.999,30,31]) {
 const score=await rpc('challenge_question_score(true,$1,$2)',[elapsed,JSON.stringify(rules)]);
 assert.equal(score,elapsed>=30?0:rules.accuracy_points+Math.floor(rules.speed_bonus*(1-elapsed/30)));
 }
});
await test('rollback preserves phase 1 and paid tables; migration reapplies',async()=>{
 await db.exec(read('db/migrations/0009_challenge_game_rollback.sql'));
 assert.equal((await one('select count(*)::int n from challenge_tests')).n,1);
 assert.equal((await one('select points from users where user_id=11')).points,1234);
 await db.exec(read('db/migrations/0009_challenge_game.sql'));
});
console.log(`\n${passed} game SQL scenarios passed. Engine: ${(await one('select version()')).version}`);
await db.close();
