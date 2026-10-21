import {PGlite} from '@electric-sql/pglite';
import {readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
import assert from 'node:assert/strict';
const db=new PGlite(),qid=randomUUID();
await db.exec('create role anon;create role authenticated;create role service_role bypassrls;create table users(user_id bigint primary key);create table quizzes(id uuid primary key);insert into users values(7),(8)');
await db.query('insert into quizzes values($1)',[qid]);
for(const f of ['0017_educational_runner.sql','20261020191247_runner_wallet.sql','20261020193546_runner_knowledge_car.sql'])await db.exec(readFileSync(new URL('../../db/migrations/'+f,import.meta.url),'utf8'));
const call=async(sql,args=[])=>(await db.query(sql,args)).rows[0].value;
const wallet=()=>call('select runner_wallet_state(7) value');
const round=async()=>{const id=randomUUID();await db.query('insert into runner_rounds(id,user_id,quiz_id,questions,lives,issued_at) values($1,7,$2,\'[{}]\',2,100)',[id,qid]);return id;};
const transition={lives:2,cursor:1,status:'completed',score:100};
const answer=(rid,key,correct=true,attempted=true,update=transition)=>call('select runner_commit_answer(7,$1,$2,0,$3,$4,$5) value',[rid,qid,key,JSON.stringify(update),JSON.stringify({correct,attempted,score:correct?100:0,status:'completed',lives:2})]);
const key=i=>i.toString(16).padStart(64,'0');
let rid=await round();const a=await answer(rid,key(1));assert.equal(a.knowledge_awarded,100);assert.equal(a.wallet.knowledge,100);assert.deepEqual(await answer(rid,key(1)),a);
assert.equal((await answer(await round(),key(1))).knowledge_awarded,0);assert.equal((await wallet()).daily.answered,1);
assert.equal((await answer(await round(),key(2),false)).knowledge_awarded,0);assert.equal((await wallet()).daily.answered,2);
assert.equal((await answer(await round(),key(3),false,false)).wallet.daily.answered,2); // timeouts do not satisfy the goal
assert.equal((await answer(await round(),key(2))).knowledge_awarded,100); // wrong first attempt never consumes mastery
for(let i=4;i<=7;i++)await answer(await round(),key(i));
assert.equal((await wallet()).daily.answered,5);assert.equal((await wallet()).knowledge,600);
await db.query("update runner_daily_study set study_day=study_day-1 where user_id=7");
assert.equal((await wallet()).daily.answered,0);assert.equal((await wallet()).knowledge,600); // no penalty tomorrow
const rollbackRound=await round();let failed=false;try{await answer(rollbackRound,key(99),true,true,{...transition,lives:99});}catch{failed=true;}assert.ok(failed);
assert.equal((await wallet()).knowledge,600);assert.equal((await db.query('select count(*) n from runner_mastery where question_key=$1',[key(99)])).rows[0].n,0);
const other=await call('select runner_commit_answer(8,$1,$2,0,$3,$4,$5) value',[rollbackRound,qid,key(99),JSON.stringify(transition),'{}']);assert.equal(other.error,'round_expired');
const buy=level=>call('select runner_buy_car(7,$1) value',[level]);
assert.equal((await buy(1)).purchase_status,'insufficient');
await db.exec('update runner_wallets set knowledge=6500,knowledge_earned=10000 where user_id=7');
assert.equal((await buy(2)).purchase_status,'invalid_level');
assert.equal((await buy(1)).knowledge,5500);assert.equal((await buy(1)).knowledge,5500);
assert.equal((await buy(2)).knowledge,5000);assert.equal((await buy(2)).car_level,2);
assert.equal((await buy(3)).knowledge,0);assert.equal((await buy(3)).knowledge_earned,10000);assert.equal((await buy(3)).purchase_status,'owned');
assert.equal((await call("select runner_equip(8,'tesla') value")).error,'not_owned');
assert.equal((await call("select runner_equip(7,'none') value")).equipped,'none');
assert.equal((await call("select runner_equip(7,'tesla') value")).equipped,'tesla');
rid=await round();await db.query('update runner_rounds set issued_at=null where id=$1',[rid]);
const track=await call('select runner_begin_track(7,$1,0,$2) value',[rid,'[{"id":0,"at":0.7,"kind":"coin","lane":1}]']);assert.equal(track.equipment,'tesla');assert.equal(track.car_level,3);
await call("select runner_equip(7,'none') value");assert.deepEqual(await call('select runner_begin_track(7,$1,0,\'[]\') value',[rid]),track);
await db.exec("update runner_tracks set started_at=clock_timestamp()-interval '10 seconds'");
const credit=await call('select runner_claim_coins(7,$1,0,array[0]) value',[rid]);assert.equal(credit.knowledge_earned,10000);assert.equal(credit.car_level,3);
for(const t of ['runner_mastery','runner_daily_study']){
 assert.ok((await db.query('select relrowsecurity from pg_class where relname=$1',[t])).rows[0].relrowsecurity);
 for(const role of ['anon','authenticated'])assert.equal((await db.query("select has_table_privilege($1,$2,'SELECT') x",[role,t])).rows[0].x,false);
}
for(const fn of ['runner_wallet_state(bigint)','runner_buy_car(bigint,integer)','runner_equip(bigint,text)','runner_commit_answer(bigint,uuid,uuid,integer,text,jsonb,jsonb)'])for(const role of ['anon','authenticated'])assert.equal((await db.query("select has_function_privilege($1,$2,'EXECUTE') x",[role,fn])).rows[0].x,false);
await db.exec('delete from quizzes');assert.equal((await wallet()).car_level,3);assert.equal((await wallet()).knowledge_earned,10000);
assert.equal((await db.query('select count(*) n from runner_mastery')).rows[0].n,6);
await db.close();console.log('knowledge SQL: atomic answer/reward rollback, first mastery only, daily distinct/timeouts/no penalty, 1000+500+5000 prices, durable levels/equipment, RLS PASS');
