import {PGlite} from '@electric-sql/pglite';
import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
const db=new PGlite();
await db.exec(`create role anon; create role authenticated; create role service_role bypassrls;
create table users(user_id bigint primary key);create table quizzes(id uuid primary key);
insert into users values(7),(8);insert into quizzes values('11111111-1111-1111-1111-111111111111');`);
for(const file of ['0017_educational_runner.sql','20261020191247_runner_wallet.sql'])await db.exec(readFileSync(new URL('../../db/migrations/'+file,import.meta.url),'utf8'));
const rid='22222222-2222-2222-2222-222222222222';
await db.query(`insert into runner_rounds(id,user_id,quiz_id,questions,lives) values($1,7,'11111111-1111-1111-1111-111111111111','[{}]',2)`,[rid]);
const call=async(sql,args=[])=>(await db.query(sql,args)).rows[0].value;
const buy=()=>call('select runner_buy_jetpack(7) value');
assert.equal((await buy()).purchase_status,'insufficient');
await db.exec('update runner_wallets set coins=99');assert.equal((await buy()).purchase_status,'insufficient');
await db.exec('update runner_wallets set coins=107');
const bought=await buy();assert.equal(bought.coins,7);assert.equal(bought.jetpack_owned,true);
assert.equal((await buy()).coins,7); // retries never debit twice
const plan=[{id:0,at:.7,kind:'coin',lane:1},{id:1,at:1.4,kind:'barrier',lane:1},{id:2,at:2.1,kind:'coin',lane:2}];
const begin=objects=>call('select runner_begin_track(7,$1,0,$2) value',[rid,JSON.stringify(objects)]);
assert.deepEqual((await begin(plan)).objects,plan);
assert.deepEqual((await begin([])).objects,plan); // immutable track and timer on retry
const claim=(user,ids)=>call('select runner_claim_coins($1,$2,0,$3) value',[user,rid,ids]);
assert.equal((await claim(8,[0])).error,'round_expired');
assert.deepEqual((await claim(7,[0,2])).accepted,[]); // cannot collect before travel time
await db.exec("update runner_tracks set started_at=clock_timestamp()-interval '15 seconds'");
const credited=await claim(7,[0,0,1,2,9,999]);assert.equal(credited.coins,9);assert.deepEqual(credited.accepted,[0,2]);
assert.equal((await claim(7,[0,2])).coins,9); // duplicate receipts, including reload/retry
await db.exec("update runner_rounds set status='completed',score=100");
assert.equal((await claim(7,[0])).coins,9); // finish does not lose a late receipt
assert.equal((await db.query('select score from runner_rounds')).rows[0].score,100);
for(const table of ['runner_wallets','runner_tracks']){
 assert.equal((await db.query('select relrowsecurity from pg_class where relname=$1',[table])).rows[0].relrowsecurity,true);
 for(const role of ['anon','authenticated'])assert.equal((await db.query("select has_table_privilege($1,$2,'SELECT') allowed",[role,table])).rows[0].allowed,false);
}
for(const fn of ['runner_buy_jetpack(bigint)','runner_begin_track(bigint,uuid,integer,jsonb)','runner_claim_coins(bigint,uuid,integer,integer[])']){
 for(const role of ['anon','authenticated'])assert.equal((await db.query("select has_function_privilege($1,$2,'EXECUTE') allowed",[role,fn])).rows[0].allowed,false);
}
await db.exec('delete from quizzes'); // retention/quiz deletion must not delete ownership/balance
assert.equal((await db.query('select count(*) n from runner_tracks')).rows[0].n,0);
const kept=(await db.query('select * from runner_wallets')).rows[0];assert.equal(kept.coins,9);assert.equal(kept.jetpack_owned,true);
await db.close();console.log('wallet SQL: purchase price/insufficient/idempotence, timed bounded coin IDs, isolation, RLS/RPC privileges, retention PASS');
