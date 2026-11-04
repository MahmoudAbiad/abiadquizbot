// Real independent PostgreSQL sessions. Refuses remote/non-loopback databases.
import pg from 'pg';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';

const source = process.env.TEST_POSTGRES_URL;
if (!source) throw new Error('TEST_POSTGRES_URL is required (local disposable PostgreSQL only)');
const url = new URL(source);
if (!['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) throw new Error('Remote database refused');
const admin = new pg.Client({connectionString: source});
const database = `runner_test_${randomUUID().replaceAll('-', '')}`;
let pool;
const createdRoles = [];
await admin.connect();
try {
  await admin.query(`create database "${database}"`);
  url.pathname = `/${database}`;
  pool = new pg.Pool({connectionString: url.toString(), max: 20});
  for (const role of ['anon', 'authenticated', 'service_role']) {
    const exists = await admin.query('select 1 from pg_roles where rolname=$1', [role]);
    if (!exists.rowCount) {
      await admin.query(`create role ${role}${role === 'service_role' ? ' bypassrls' : ''}`);
      createdRoles.push(role);
    }
  }
  await pool.query(`create table public.users(user_id bigint primary key);
    create table public.quizzes(id uuid primary key,quiz_data jsonb);
    insert into users values(7);
    insert into quizzes values('11111111-1111-1111-1111-111111111111','[]');`);
  for (const file of ['0017_educational_runner.sql', '0018_runner_image_cache.sql',
    '20261020191247_runner_wallet.sql', '20261020193546_runner_knowledge_car.sql']) {
    await pool.query(readFileSync(new URL(`../../db/migrations/${file}`, import.meta.url), 'utf8'));
  }
  // Hold all sessions simultaneously so this cannot accidentally use one backend.
  const clients = await Promise.all(Array.from({length: 20}, () => pool.connect()));
  const pids = await Promise.all(clients.map(c => c.query('select pg_backend_pid() pid')));
  assert.equal(new Set(pids.map(r => r.rows[0].pid)).size, 20);
  clients.forEach(c => c.release());
  const many = fn => Promise.all(Array.from({length: 20}, (_, i) => fn(i)));
  const rpc = async (name, args, placeholders) =>
    (await pool.query(`select public.${name}(${placeholders}) value`, args)).rows[0].value;
  const wallet = () => rpc('runner_wallet_state', [7], '$1');
  const rid = randomUUID(), qid = '11111111-1111-1111-1111-111111111111';
  await pool.query(`insert into runner_rounds(id,user_id,quiz_id,questions,lives) values($1,7,$2,'[]',1)`, [rid, qid]);
  await pool.query(`insert into runner_tracks(round_id,segment,objects,started_at)
    values($1,0,'[{"id":0,"kind":"coin","at":0,"lane":0}]',clock_timestamp()-interval '30 seconds')`, [rid]);
  await many(() => rpc('runner_claim_coins', [7, rid, 0, [0]], '$1,$2,$3,$4'));
  assert.equal((await wallet()).coins, 1, 'concurrent coin retries credit once');
  await pool.query('update runner_wallets set coins=100 where user_id=7');
  const buys = await many(() => rpc('runner_buy_jetpack', [7], '$1'));
  assert.equal(buys.filter(x => x.purchase_status === 'purchased').length, 1);
  assert.equal((await wallet()).coins, 0);
  const update = {lives:1,cursor:1,status:'completed',score:100};
  const response = {correct:true,attempted:true,status:'completed',score:100};
  const args = [7, rid, qid, 0, 'a'.repeat(64), update, response];
  const answers = await many(() => rpc('runner_commit_answer', args, '$1,$2,$3,$4,$5,$6,$7'));
  assert.ok(answers.every(a => a.version === 1 && a.knowledge_awarded === 100));
  assert.equal((await wallet()).knowledge, 100, 'same-round retries reward once');
  const rounds = Array.from({length:20}, () => randomUUID());
  for (const r of rounds) await pool.query(`insert into runner_rounds(id,user_id,quiz_id,questions,lives)
    values($1,7,$2,'[]',1)`, [r, qid]);
  const mastery = await many(i => rpc('runner_commit_answer',
    [7, rounds[i], qid, 0, 'b'.repeat(64), update, response], '$1,$2,$3,$4,$5,$6,$7'));
  assert.equal(mastery.filter(x => x.knowledge_awarded === 100).length, 1);
  assert.equal((await wallet()).knowledge, 200, 'shared mastery across rounds rewards once');
  await pool.query('update runner_wallets set knowledge=6500,knowledge_earned=6500 where user_id=7');
  for (const level of [1,2,3]) {
    const cars = await many(() => rpc('runner_buy_car', [7, level], '$1,$2'));
    assert.equal(cars.filter(x => x.purchase_status === 'purchased').length, 1);
  }
  assert.equal((await wallet()).knowledge, 0);
  assert.equal((await wallet()).car_level, 3);
  assert.equal((await wallet()).knowledge_earned, 6500);
  console.log('Runner PostgreSQL: 20 distinct backends; concurrent coin, jetpack, answer, mastery and car retries PASS');
} finally {
  if (pool) await pool.end();
  await admin.query(`drop database if exists "${database}"`);
  for (const role of createdRoles) await admin.query(`drop role ${role}`);
  await admin.end();
}
