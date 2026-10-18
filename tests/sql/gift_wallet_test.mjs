// Run with a locally installed @electric-sql/pglite (no remote database needed).
import { PGlite } from '@electric-sql/pglite';
import { readFileSync, readdirSync } from 'node:fs';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
const db = new PGlite();
const baseline=readFileSync('db/migrations/0001_baseline.sql','utf8');
await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;');
for(const table of ['users','app_settings','usage_events']) {
 const start=baseline.indexOf(`CREATE TABLE public.${table} (`);
 const end=baseline.indexOf('\n);',start)+3;
 assert(start>=0,table);
 await db.exec(baseline.slice(start,end));
}
await db.exec("INSERT INTO public.app_settings(key,value) VALUES('welcome_points',150),('referral_bonus_points',25);");
await db.exec("INSERT INTO public.users(user_id,free_points,paid_points) VALUES(99,50,25);");
const migration=readdirSync('db/migrations').find(f=>f.endsWith('_gift_wallet_codes.sql'));
await db.exec(readFileSync(`db/migrations/${migration}`,'utf8'));
await db.exec(readFileSync('db/migrations/20261018093843_wallet_debit_numeric_arithmetic.sql','utf8'));
const scalar=async (sql,args=[]) => (await db.query(sql,args)).rows[0]?.v;
const rpc=async (name,args) => scalar(`SELECT public.${name}(${args.map((_,i)=>'$'+(i+1)).join(',')}) AS v`,args);
const user=async id => rpc('register_user_with_gift',[JSON.stringify({user_id:id,username:'user'+id,first_name:'طالب',last_name:'اختبار',last_renewal:'2026-10-08',join_source:'bot'})]);
const balance=async id => Number(await scalar('SELECT coalesce(sum(gift_points),0) AS v FROM public.wallet_gift_balances(ARRAY[$1::bigint])',[id]));
const code=async (max=1,days=30,expiry=null) => {
 const id=randomUUID();const text='ABIAD-'+randomUUID().replaceAll('-','').toUpperCase();
 await db.query('INSERT INTO public.gift_codes(id,code,title,points,max_uses,gift_valid_days,redeem_expires_at,created_by) VALUES($1,$2,$3,40,$4,$5,$6,1)',[id,text,'هدية اختبار',max,days,expiry]);return {id,text};
};
const debit=async (id,amount) => rpc('wallet_debit',[id,amount,1,randomUUID()]);
let checks=0;
assert.deepEqual((await db.query('SELECT free_points,paid_points FROM public.users WHERE user_id=99')).rows[0],{free_points:50,paid_points:25});checks++;
assert.equal(await user(1),true);assert.equal(await user(1),false);
assert.equal(await balance(1),150);
assert.equal(Number(await scalar('SELECT free_points AS v FROM public.users WHERE user_id=1')),0);
assert.equal(Number(await scalar("SELECT round(extract(epoch FROM expires_at-granted_at)/86400) AS v FROM public.point_gifts WHERE user_id=1")),30);checks++;
await user(2);await user(3);
let c=await code();
assert.equal((await rpc('redeem_gift_code',[1,c.text.toLowerCase()])).status,'ok');
assert.equal((await rpc('redeem_gift_code',[1,c.text])).status,'already_used');
assert.equal((await rpc('redeem_gift_code',[2,c.text])).status,'exhausted');
assert.equal(Number(await scalar('SELECT used_count AS v FROM public.gift_codes WHERE id=$1',[c.id])),1);checks++;
c=await code(2);
assert.equal((await rpc('redeem_gift_code',[1,c.text])).status,'ok');
assert.equal((await rpc('redeem_gift_code',[2,c.text])).status,'ok');
assert.equal((await rpc('redeem_gift_code',[3,c.text])).status,'exhausted');checks++;
c=await code(null,0);
assert.equal((await rpc('redeem_gift_code',[1,c.text])).expires_at,null);
await db.query('UPDATE public.gift_codes SET enabled=false WHERE id=$1',[c.id]);
assert.equal((await rpc('redeem_gift_code',[2,c.text])).status,'disabled');checks++;
c=await code(null,30,'2000-01-01T00:00:00Z');
assert.equal((await rpc('redeem_gift_code',[2,c.text])).status,'expired');
assert.equal((await rpc('redeem_gift_code',[2,'ABIAD-00000000000000000000'])).status,'invalid');checks++;
// Use deterministic balances: free first, nearest-expiring gifts next, paid last.
await db.exec('DELETE FROM public.gift_code_redemptions; DELETE FROM public.point_gifts; UPDATE public.users SET free_points=10,paid_points=30;');
const gids=[randomUUID(),randomUUID(),randomUUID()];
for(let i=0;i<3;i++) await db.query("INSERT INTO public.point_gifts(id,user_id,source,source_key,title,original_amount,remaining_amount,expires_at) VALUES($1,1,'admin',$2,'test',20,20,CASE WHEN $3::int=0 THEN NULL ELSE now()+make_interval(days=>$3) END)",[gids[i],String(i),[5,1,0][i]]);
const receipt=await debit(1,65);
assert.equal(receipt.debited_free,10);assert.equal(receipt.debited_gift,55);assert.equal(receipt.debited_paid,0);
assert.equal(Number(await scalar('SELECT remaining_amount AS v FROM public.point_gifts WHERE id=$1',[gids[1]])),0);
assert.equal(Number(await scalar('SELECT remaining_amount AS v FROM public.point_gifts WHERE id=$1',[gids[2]])),5);
assert.equal(receipt.remaining_points,35);checks++;
const paid=await debit(1,15);assert.equal(paid.debited_gift,5);assert.equal(paid.debited_paid,10);
assert.equal(await debit(1,21),null);checks++;
// Repeat partial refund then full: no double credit, preserve each gift identity.
assert.equal(await rpc('wallet_refund',[1,receipt.debit_id,0.5]),true);
const half=await balance(1);
assert.equal(await rpc('wallet_refund',[1,receipt.debit_id,0.5]),true);assert.equal(await balance(1),half);
assert.equal(await rpc('wallet_refund',[1,receipt.debit_id,1]),true);assert.equal(await balance(1),55);
assert.equal(await rpc('wallet_refund',[1,receipt.debit_id,1]),true);assert.equal(await balance(1),55);
assert.equal(await rpc('wallet_refund',[2,receipt.debit_id,1]),false);checks++;
// Refund an expired gift without extending it or converting it into paid points.
await db.query("UPDATE public.point_gifts SET expires_at=now()-interval '1 day' WHERE user_id=1");
const before=Number(await scalar('SELECT paid_points AS v FROM public.users WHERE user_id=1'));
await rpc('wallet_refund',[1,paid.debit_id,1]);
assert.equal(await balance(1),0);assert.equal(Number(await scalar('SELECT paid_points AS v FROM public.users WHERE user_id=1')),before+10);checks++;
// Revocation and expiry editing; revoked gifts cannot be reactivated by editing expiry.
assert.equal(await rpc('admin_edit_point_gift',[1,gids[0],'expiry','2099-01-01']),true);assert.equal(await balance(1),20);
assert.equal(await rpc('admin_edit_point_gift',[1,gids[0],'revoke',null]),true);assert.equal(await balance(1),0);
assert.equal(await rpc('admin_edit_point_gift',[1,gids[0],'expiry',null]),false);checks++;
// New referral award is atomic and idempotent, directed only to gifts.
await db.exec("UPDATE public.users SET referred_by=2 WHERE user_id=3; INSERT INTO public.usage_events(user_id,event_type) VALUES(3,'quiz_generated');");
assert.equal((await rpc('reward_referral_gift',[3])).points,25);
assert.equal(await rpc('reward_referral_gift',[3]),null);assert.equal(await balance(2),25);checks++;
// Check service-role-only new API and table access.
assert.equal(await scalar("SELECT has_function_privilege('anon','public.redeem_gift_code(bigint,text)','EXECUTE') AS v"),false);
assert.equal(await scalar("SELECT has_table_privilege('authenticated','public.point_gifts','SELECT') AS v"),false);
assert.equal(await scalar("SELECT has_function_privilege('service_role','public.wallet_debit(bigint,numeric,integer,uuid)','EXECUTE') AS v"),true);checks++;
console.log(`${checks} SQL wallet scenarios passed`);
await db.close();
