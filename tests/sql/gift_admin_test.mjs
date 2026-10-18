// No remote credentials. PGlite locally; isolated PostgreSQL in CI for lock races.
import { PGlite } from '@electric-sql/pglite';
import { Pool } from 'pg';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';

const url = process.env.TEST_POSTGRES_URL;
if (url) {
  const parsed = new URL(url);
  assert.ok(['localhost', '127.0.0.1'].includes(parsed.hostname));
  assert.equal(parsed.pathname, '/gift_admin_test');
}
const pool = url ? new Pool({connectionString: url}) : null;
const db = pool ? {exec: sql => pool.query(sql), query: (sql, args) => pool.query(sql, args), close: () => pool.end()} : new PGlite();
const value = async (sql, args=[]) => (await db.query(sql, args)).rows[0].v;
const action = (id, name) => value('SELECT admin_gift_code_action($1,$2) AS v', [id, name]);
const redeem = (user, code) => value('SELECT redeem_gift_code($1,$2) AS v', [user, code]);
const status = id => value('SELECT admin_gift_campaign_status($1) AS v', [id]);

try {
  await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;');
  const baseline = fs.readFileSync('db/migrations/0001_baseline.sql', 'utf8');
  await db.exec(baseline.match(/CREATE TABLE public\.users \([\s\S]*?\n\);/)[0]);
  await db.exec(`CREATE TABLE app_settings(key text PRIMARY KEY,value double precision,label text);
    CREATE TABLE usage_events(user_id bigint,event_type text);
    INSERT INTO users(user_id,free_points,paid_points) VALUES(1,10,20),(2,11,21),(3,12,22);`);
  await db.exec(fs.readFileSync('db/migrations/20261008141907_gift_wallet_codes.sql', 'utf8'));
  await db.exec(fs.readFileSync('db/migrations/20261018084726_gift_admin_campaigns.sql', 'utf8'));
  await db.exec(fs.readFileSync('db/migrations/20261018093843_wallet_debit_numeric_arithmetic.sql', 'utf8'));
  await db.exec('GRANT ALL ON users,app_settings,usage_events TO service_role; SET ROLE service_role;');

  const codeId = randomUUID();
  const unusedId = randomUUID();
  await db.query(`INSERT INTO gift_codes(id,code,title,points,gift_valid_days,created_by)
    VALUES($1,'ABIAD-TEST-000001','Used code',100,30,999),($2,'ABIAD-TEST-000002','Unused code',50,0,999)`, [codeId,unusedId]);
  const first = await redeem(1, 'ABIAD-TEST-000001');
  assert.equal(first.status, 'ok');
  assert.equal((await redeem(1, 'ABIAD-TEST-000001')).status, 'already_used');
  assert.equal((await action(codeId, 'delete')).status, 'used');
  assert.equal((await action(unusedId, 'delete')).status, 'ok');
  assert.equal((await action(unusedId, 'delete')).status, 'missing');

  assert.equal((await action(codeId, 'archive')).status, 'ok');
  assert.equal((await redeem(2, 'ABIAD-TEST-000001')).status, 'disabled');
  assert.equal(Number(await value('SELECT gift_points AS v FROM wallet_gift_balances(ARRAY[1]::bigint[])')), 100);
  assert.equal((await action(codeId, 'enable')).status, 'cannot_enable');
  await action(codeId, 'restore');
  assert.equal((await redeem(2, 'ABIAD-TEST-000001')).status, 'disabled');
  await action(codeId, 'enable');
  assert.equal((await redeem(2, 'ABIAD-TEST-000001')).status, 'ok');

  // Spend from user 1's code gift after free balance. Free/paid buckets stay
  // untouched by revoke, as do gifts from any other source.
  const debitId = randomUUID();
  await value('SELECT wallet_debit(1,40,5,$1) AS v', [debitId]);
  await db.query(`INSERT INTO point_gifts(user_id,source,source_key,title,original_amount,remaining_amount)
    VALUES(1,'admin','other','Other gift',7,7)`);
  const bucketsBefore = (await db.query('SELECT user_id,free_points,paid_points FROM users ORDER BY user_id')).rows;
  const summary = await value('SELECT admin_gift_code_summary($1) AS v', [codeId]);
  assert.equal(summary.affected, 2);
  assert.equal(summary.remaining_points, 170);
  const revoked = await action(codeId, 'revoke_all');
  assert.equal(revoked.remaining_points, 170);
  assert.equal(revoked.affected, 2);
  assert.equal((await action(codeId, 'revoke_all')).status, 'already_revoked');
  assert.equal((await action(codeId, 'enable')).status, 'cannot_enable');
  assert.deepEqual((await db.query('SELECT user_id,free_points,paid_points FROM users ORDER BY user_id')).rows, bucketsBefore);
  assert.equal(Number(await value('SELECT remaining_amount AS v FROM point_gifts WHERE id=$1', [first.gift_id])), 70);
  await value('SELECT wallet_refund(1,$1) AS v', [debitId]);
  assert.equal(Number(await value('SELECT gift_points AS v FROM wallet_gift_balances(ARRAY[1]::bigint[])')), 7);
  assert.equal(Number(await value('SELECT count(*) AS v FROM gift_code_redemptions WHERE code_id=$1', [codeId])), 2);

  const campaignId = randomUUID();
  const prepare = id => value('SELECT admin_gift_campaign_prepare($1,999,\'Gift\',25.5,10,\'A message\') AS v', [id]);
  const preview = await prepare(campaignId);
  assert.equal(preview.recipients, 3);
  assert.equal(preview.total_points, 76.5);
  assert.equal(Number(await value("SELECT count(*) AS v FROM point_gifts WHERE source='admin' AND source_key=$1", ['campaign:'+campaignId])), 0);
  await db.exec('INSERT INTO users(user_id) VALUES(4);');
  assert.equal((await prepare(campaignId)).recipients, 3); // never expand a saved preview
  assert.equal(await value('SELECT admin_gift_campaign_confirm($1,888,999,1) AS v', [campaignId]), 'missing');
  assert.equal(await value('SELECT admin_gift_campaign_confirm($1,999,999,1) AS v', [campaignId]), 'running');
  assert.equal(await value('SELECT admin_gift_campaign_confirm($1,999,999,1) AS v', [campaignId]), 'running');
  // A deleted account is skipped, rather than aborting all the other grants.
  await db.exec('DELETE FROM users WHERE user_id=3;');
  await value('SELECT admin_gift_campaign_grant_step($1,1) AS v', [campaignId]);
  await value('SELECT admin_gift_campaign_grant_step($1,50) AS v', [campaignId]);
  assert.equal(await value('SELECT admin_gift_campaign_grant_step($1,50) AS v', [campaignId]), 0);
  assert.equal(Number(await value("SELECT count(*) AS v FROM point_gifts WHERE source='admin' AND source_key=$1", ['campaign:'+campaignId])), 2);
  let report = await status(campaignId);
  assert.equal(report.granted, 2);
  assert.equal(report.skipped, 1);
  const claimed = await value('SELECT admin_gift_campaign_claim($1,10) AS v', [campaignId]);
  assert.equal(claimed.length, 2);
  assert.equal((await value('SELECT admin_gift_campaign_claim($1,10) AS v', [campaignId])).length, 0);
  assert.equal(await value('SELECT admin_gift_campaign_notice($1,$2,$3,\'sent\') AS v', [campaignId,claimed[0].user_id,randomUUID()]), false);
  assert.equal(await value('SELECT admin_gift_campaign_notice($1,$2,$3,\'sent\') AS v', [campaignId,claimed[0].user_id,claimed[0].token]), true);
  // Crash after possibly sending: report ambiguity, do not send a duplicate.
  await db.query("UPDATE admin_gift_recipients SET claimed_at=now()-interval '3 minutes' WHERE campaign_id=$1 AND notice_status='sending'", [campaignId]);
  assert.equal((await value('SELECT admin_gift_campaign_claim($1,10) AS v', [campaignId])).length, 0);
  report = await status(campaignId);
  assert.equal(report.status, 'completed');
  assert.equal(report.sent, 1);
  assert.equal(report.uncertain, 1);
  assert.equal(await value('SELECT admin_gift_campaign_grant_step($1,50) AS v', [campaignId]), 0);

  const retryId = randomUUID();
  await prepare(retryId);
  await value('SELECT admin_gift_campaign_confirm($1,999,999,2) AS v', [retryId]);
  await value('SELECT admin_gift_campaign_grant_step($1) AS v', [retryId]);
  const claim = (await value('SELECT admin_gift_campaign_claim($1,1) AS v', [retryId]))[0];
  await value('SELECT admin_gift_campaign_notice($1,$2,$3,\'pending\',\'rate limit\',60) AS v', [retryId,claim.user_id,claim.token]);
  const other = await value('SELECT admin_gift_campaign_claim($1,10) AS v', [retryId]);
  assert.ok(other.every(r => r.user_id !== claim.user_id));

  const expiredId = randomUUID();
  await prepare(expiredId);
  await db.query("UPDATE admin_gift_campaigns SET created_at=now()-interval '31 minutes' WHERE id=$1", [expiredId]);
  assert.equal(await value('SELECT admin_gift_campaign_confirm($1,999,999,2) AS v', [expiredId]), 'expired');
  assert.equal(await value('SELECT admin_gift_campaign_grant_step($1) AS v', [expiredId]), 0);
  const cancelledId = randomUUID();
  await prepare(cancelledId);
  await db.query("UPDATE admin_gift_campaigns SET status='cancelled' WHERE id=$1", [cancelledId]);
  assert.equal(await value('SELECT admin_gift_campaign_confirm($1,999,999,2) AS v', [cancelledId]), 'cancelled');

  await db.exec('RESET ROLE');
  for (const role of ['anon','authenticated']) {
    assert.equal(await value(`SELECT has_function_privilege('${role}','admin_gift_code_action(uuid,text)','EXECUTE') AS v`), false);
    assert.equal(await value(`SELECT has_function_privilege('${role}','admin_gift_campaign_confirm(uuid,bigint,bigint,bigint)','EXECUTE') AS v`), false);
    assert.equal(await value(`SELECT has_table_privilege('${role}','admin_gift_recipients','SELECT') AS v`), false);
  }
  for (const table of ['admin_gift_campaigns','admin_gift_recipients']) {
    assert.equal(await value(`SELECT relrowsecurity AS v FROM pg_class WHERE oid='${table}'::regclass`), true);
  }

  if (pool) {
    // Force redemption to hold the per-code lock, then start bulk revoke. Both
    // must finish without deadlock; the new grant must be included in revoke.
    const id = randomUUID();
    await db.query("INSERT INTO gift_codes(id,code,title,points,gift_valid_days,created_by) VALUES($1,'ABIAD-RACE-00001','Race',10,0,999)", [id]);
    const a = await pool.connect(); const b = await pool.connect();
    try {
      await b.query("SET statement_timeout='5s'");
      await a.query("BEGIN; SET LOCAL statement_timeout='5s';");
      await a.query('SELECT redeem_gift_code(1,\'ABIAD-RACE-00001\')');
      const revoking = b.query('SELECT admin_gift_code_action($1,\'revoke_all\') AS v', [id]);
      await new Promise(resolve => setTimeout(resolve, 100));
      await a.query('COMMIT');
      assert.equal((await revoking).rows[0].v.affected, 1);
      assert.equal((await redeem(2, 'ABIAD-RACE-00001')).status, 'disabled');
      // Two grant workers on the same confirmed campaign still create one gift.
      const concurrent = randomUUID();
      await prepare(concurrent);
      await value('SELECT admin_gift_campaign_confirm($1,999,999,5) AS v', [concurrent]);
      await Promise.all([a.query('SELECT admin_gift_campaign_grant_step($1)', [concurrent]),
                         b.query('SELECT admin_gift_campaign_grant_step($1)', [concurrent])]);
      assert.equal(Number(await value("SELECT count(*) AS v FROM point_gifts WHERE source_key=$1", ['campaign:'+concurrent])), 3);
    } finally { a.release(); b.release(); }
  }
  // Reproduce legacy float balances: numeric deductions must not be converted
  // back into float arithmetic before assignment. This exact positive residue
  // previously became -4.33873497871556e-30 and violated users_free_points_check.
  await db.exec(`INSERT INTO users(user_id,free_points,paid_points) VALUES
    (101,2.6645352591003757e-15,9684.10),(102,0.1,0.2),(103,0,6),(104,3,25.30);`);
  for (const [userId, amount, expectedPaid] of [[101,6,9678.10],[102,0.3,0],[103,6,0],[104,28.30,0]]) {
    const id = randomUUID();
    const receipt = await value('SELECT wallet_debit($1,$2,5,$3) AS v', [userId,amount,id]);
    assert.ok(receipt);
    const row = (await db.query('SELECT free_points,paid_points,total_questions FROM users WHERE user_id=$1',[userId])).rows[0];
    assert.equal(Number(row.free_points),0);
    assert.equal(Number(row.paid_points),expectedPaid);
    assert.equal(Number(row.total_questions),5);
    assert.ok(receipt.debited_free>=0 && receipt.debited_paid>=0 && receipt.debited_gift>=0);
    assert.ok(Math.abs(receipt.debited_free+receipt.debited_paid+receipt.debited_gift-amount)<1e-12);
    assert.equal(await value('SELECT wallet_debit($1,10000,1,$2) AS v',[userId,randomUUID()]),null);
    assert.equal(Number(await value('SELECT count(*) AS v FROM wallet_debits WHERE user_id=$1',[userId])),1);
  }
  // Fractional gifts keep their normal priority; no insufficient-balance
  // rejection may leave a partial debit or a receipt behind.
  await db.exec(`INSERT INTO users(user_id,free_points,paid_points) VALUES(105,0.1,0.1);
    INSERT INTO point_gifts(user_id,source,source_key,title,original_amount,remaining_amount)
      VALUES(105,'admin','precision','Fractional gift',0.1,0.1);`);
  const fractionId=randomUUID();
  const fraction=await value('SELECT wallet_debit(105,0.30,5,$1) AS v',[fractionId]);
  assert.equal(fraction.debited_free,0.1);
  assert.equal(fraction.debited_gift,0.1);
  assert.equal(fraction.debited_paid,0.1);
  assert.equal(Number(await value('SELECT free_points+paid_points AS v FROM users WHERE user_id=105')),0);
  await value('SELECT wallet_refund(105,$1) AS v',[fractionId]);
  await value('SELECT wallet_refund(105,$1) AS v',[fractionId]);
  assert.equal(Number(await value('SELECT free_points AS v FROM users WHERE user_id=105')),0.1);
  assert.equal(Number(await value('SELECT paid_points AS v FROM users WHERE user_id=105')),0.1);
  assert.equal(Number(await value('SELECT gift_points AS v FROM wallet_gift_balances(ARRAY[105]::bigint[])')),0.1);
  assert.equal(await value("SELECT has_function_privilege('anon','wallet_debit(bigint,numeric,integer,uuid)','EXECUTE') AS v"),false);
  console.log('PASS gift admin SQL: archive/delete/revoke, refunds, snapshots, once-only grants, notices, recovery, permissions, decimal wallet precision'+(pool?', concurrent PostgreSQL locks':''));
} catch (error) {
  console.error('FAIL gift admin SQL:', error.message, error.detail || '', error.where || '', error.stack?.split('\n').slice(-3).join('\n'));
  process.exitCode = 1;
} finally { await db.close(); }
