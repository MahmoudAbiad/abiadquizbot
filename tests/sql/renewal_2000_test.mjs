import { PGlite } from '@electric-sql/pglite';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
const db = new PGlite();
const read = p => readFileSync(p,'utf8');
await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;');
const baseline=read('db/migrations/0001_baseline.sql');
for(const table of ['users','app_settings']) {
 const start=baseline.indexOf(`CREATE TABLE public.${table} (`);
 await db.exec(baseline.slice(start,baseline.indexOf('\n);',start)+3));
}
const start=baseline.indexOf('CREATE OR REPLACE FUNCTION public.check_and_apply_daily_renewal_atomic(');
const tail=baseline.slice(start);
const delimiter=tail.match(/AS (\$[^$]*\$)/i)[1];
const bodyStart=tail.indexOf(delimiter)+delimiter.length;
await db.exec(tail.slice(0,tail.indexOf(delimiter,bodyStart)+delimiter.length+1));
await db.exec(read('db/migrations/20261026235246_daily_renewal_notifications.sql').split('-- SCHEDULER:')[0]);
await db.exec(read('db/migrations/20261027001007_daily_renewal_delivery_revision.sql'));
// Exercise the production registration expression without creating welcome gifts.
await db.exec(`CREATE FUNCTION public.register_user_with_gift(p_user jsonb) RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN INSERT INTO public.users(user_id,first_name,last_renewal,free_points,paid_points)
 VALUES((p_user->>'user_id')::bigint,'new',(p_user->>'last_renewal')::date,0,0); RETURN true; END; $$;
INSERT INTO public.app_settings(key,value) VALUES('daily_renewal_points',50);
UPDATE public.daily_renewal_config SET start_day='2026-10-28';
INSERT INTO public.daily_renewal_runs(day,amount,report_status) VALUES('2026-10-28',50,'sent');
INSERT INTO public.users(user_id,first_name,free_points,paid_points,last_renewal,joined_at)
 SELECT i,'student',7,80,'2026-10-28',now()-interval '3 days' FROM generate_series(1,3) i;
UPDATE public.users SET renewal_notifications=false WHERE user_id=2;
UPDATE public.users SET bot_delivery_state='blocked' WHERE user_id=3;`);
const operation=read('db/operations/renewal_2000_transition.sql');
await db.exec(operation);
await db.exec(operation); // The transition itself can be safely retried.
const scalar=async(sql)=>(await db.query(sql)).rows[0]?.v;
for(const [at,expected] of [
 ['2026-10-28 16:59:59+00','2026-10-28'],
 ['2026-10-28 17:00:00+00','2026-10-29'],
 ['2026-10-28 21:00:00+00','2026-10-29'],
 ['2026-10-29 16:59:59+00','2026-10-29'],
 ['2026-10-29 17:00:00+00','2026-10-30'],
]) assert.equal(await scalar(`SELECT public.daily_renewal_cycle_day('${at}')::text AS v`),expected);
await db.exec(`CREATE OR REPLACE FUNCTION public.daily_renewal_cycle_day(p_at timestamptz DEFAULT now())
RETURNS date LANGUAGE sql STABLE SECURITY INVOKER SET search_path='' AS $$
 SELECT ((coalesce(nullif(current_setting('test.clock',true),'')::timestamptz,p_at) AT TIME ZONE 'Asia/Damascus')+interval '4 hours')::date; $$;
SET test.clock='2026-10-28 19:00:00+00';`);
assert.equal(await scalar('SELECT public.daily_renewal_begin()::text AS v'),'2026-10-29');
assert.equal(Number(await scalar('SELECT free_points AS v FROM public.users WHERE user_id=1')),50);
assert.equal(Number(await scalar('SELECT paid_points AS v FROM public.users WHERE user_id=1')),80);
assert.equal(await scalar("SELECT status AS v FROM public.daily_renewal_deliveries WHERE user_id=2"),'disabled');
assert.equal(await scalar("SELECT status AS v FROM public.daily_renewal_deliveries WHERE user_id=3"),'excluded');
assert.equal(await scalar("SELECT report_status AS v FROM public.daily_renewal_runs WHERE day='2026-10-28'"),'sent');
await db.exec('UPDATE public.users SET free_points=3 WHERE user_id=1');
await scalar('SELECT public.daily_renewal_begin() AS v');
assert.equal(Number(await scalar('SELECT free_points AS v FROM public.users WHERE user_id=1')),3);
await db.exec("SET test.clock='2026-10-28 21:00:00+00'");
await db.query("SELECT * FROM public.check_and_apply_daily_renewal_atomic(1,'2099-01-01',50)");
assert.equal(Number(await scalar('SELECT free_points AS v FROM public.users WHERE user_id=1')),3,'midnight/legacy client must not refill');
await db.exec("SET test.clock='2026-10-29 16:59:59+00'");
await scalar('SELECT public.daily_renewal_begin() AS v');
assert.equal(Number(await scalar('SELECT free_points AS v FROM public.users WHERE user_id=1')),3);
const claim=await scalar('SELECT public.daily_renewal_claim() AS v');
assert.equal(claim.day,'2026-10-29','the new cycle remains claimable past midnight');
await db.query('SELECT public.daily_renewal_finish($1,$2,$3,$4,$5,$6)',[claim.day,claim.user_id,claim.token,'sent','',0]);
await db.query('SELECT public.register_user_with_gift($1)',[{user_id:4,last_renewal:'2026-10-28'}]);
assert.equal(await scalar("SELECT last_renewal::text AS v FROM public.users WHERE user_id=4"),'2026-10-29');
await db.exec("SET test.clock='2026-10-29 17:00:00+00'");
assert.equal(await scalar('SELECT public.daily_renewal_begin()::text AS v'),'2026-10-30');
assert.equal(Number(await scalar('SELECT free_points AS v FROM public.users WHERE user_id=1')),50);
await db.exec('UPDATE public.users SET free_points=2 WHERE user_id=1');
await db.query("SELECT * FROM public.check_and_apply_daily_renewal_atomic(1,'2026-10-29',50)");
assert.equal(Number(await scalar('SELECT free_points AS v FROM public.users WHERE user_id=1')),2,'lazy fallback respects server cycle');
assert.equal(await scalar("SELECT has_function_privilege('anon','public.daily_renewal_cycle_day(timestamptz)','EXECUTE') AS v"),false);
await db.close();
console.log('PASS: 20:00 boundaries, one-time transition, no midnight reset, preserved history/paid balances, opt-outs, blocked users, registration, lazy fallback, durable claims and idempotency.');
