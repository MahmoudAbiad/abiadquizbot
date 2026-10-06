// REAL multi-connection concurrency checks for 0010. NOT RUN in the authoring environment (no PostgreSQL,
// no network). Run against a DISPOSABLE test database that already has 0008+0009+0010 applied:
//   npm i --prefix "$d" pg && DATABASE_URL=postgres://... node live_concurrency.mjs /abs/repo
// It creates synthetic users (ids >= 7_000_000) and content, and never touches paid-points tables.
import pg from 'pg';
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
const root = process.argv[2];
const rules = JSON.parse(fs.readFileSync(path.join(root, 'services/challenge_rules.json'), 'utf8'));
const RULES = JSON.stringify(rules);
const url = process.env.DATABASE_URL; if (!url) throw new Error('DATABASE_URL required (disposable test DB only)');
const pool = new pg.Pool({ connectionString: url, max: Number(process.env.POOL_MAX || 40) });
const q = (sql, v = []) => pool.query(sql, v).then(r => r.rows);
const one = async (sql, v) => (await q(sql, v))[0];
const rpc = async (sql, v) => (await one('select ' + sql + ' as value', v)).value;
let passed = 0; const test = async (n, f) => { await f(); console.log(`PASS ${++passed}: ${n}`); };
const BASE = 7_000_000 + Math.floor(Math.random() * 1_000_000) * 100;
await q("update feature_flags set enabled=true where key in('challenge_system_enabled','live_matches_enabled')");
const b = (await one("insert into challenge_branches(name) values('conc-'||gen_random_uuid()) returning id")).id;
const s = (await one("insert into challenge_subjects(branch_id,name) values($1,'s') returning id", [b])).id;
const U = (await one("insert into challenge_units(subject_id,name) values($1,'u') returning id", [s])).id;
const qs = JSON.stringify(Array.from({ length: 10 }, (_, i) => ({ question: `q${i}`, options: ['a', 'b'], correct_option_id: 0 })));
await rpc("challenge_import_test($1,'official','json_upload',null,$2,1,$3::jsonb)", [U, 'h-' + BASE, qs]);
const mk = async n => { for (let i = 0; i < n; i++) await rpc('challenge_touch_profile($1,$2,$3)', [BASE + i, 'c' + i, b]); };
const noDeadlock = rs => rs.forEach(r => { if (r.status === 'rejected') assert.ok(!/deadlock/i.test(String(r.reason)), String(r.reason)); });

await test('20 players matched concurrently from separate connections: each in at most one match, no deadlock', async () => {
  await mk(20);
  for (let i = 0; i < 20; i++) await rpc('challenge_live_join($1,$2,$3,$4::jsonb)', [BASE + i, U, 'cc' + i, RULES]);
  const rs = await Promise.allSettled(Array.from({ length: 20 }, (_, i) => rpc('challenge_live_try_match($1,$2::jsonb)', [BASE + i, RULES])));
  noDeadlock(rs);
  // Heartbeat emulation: identical-trophy herd => every caller peeks the same nearest candidate, only some pairs form per round
  // (progress is guaranteed, throughput is not). Re-run rounds like the worker heartbeat does, bounded, and report how many were needed.
  let rounds = 0;
  for (; rounds < 30; rounds++) {
    const left = await q("select count(*)::int n from challenge_live_queue where status='waiting' and user_id between $1 and $2", [BASE, BASE + 19]);
    if (left[0].n === 0) break;
    noDeadlock(await Promise.allSettled(Array.from({ length: 20 }, (_, i) => rpc('challenge_live_try_match($1,$2::jsonb)', [BASE + i, RULES]))));
  }
  console.log(`  herd of 20 identical-trophy players settled after ${rounds + 1} concurrent round(s)`);
  const per = await q(`select user_id,count(*)::int n from challenge_match_players p join challenge_matches m on m.id=p.match_id
    where m.kind='live' and m.status in('created','in_progress') and user_id between $1 and $2 group by user_id`, [BASE, BASE + 19]);
  assert.ok(per.every(r => r.n === 1)); assert.equal(per.length, 20);
});
await test('cancel vs match race: never both cancelled and matched', async () => {
  await mk(40); const x = BASE + 30, y = BASE + 31;
  await rpc('challenge_live_join($1,$2,$3,$4::jsonb)', [x, U, 'rx', RULES]); await rpc('challenge_live_join($1,$2,$3,$4::jsonb)', [y, U, 'ry', RULES]);
  const rs = await Promise.allSettled([rpc('challenge_live_try_match($1,$2::jsonb)', [y, RULES]), rpc("challenge_live_cancel($1,'rx')", [x])]);
  noDeadlock(rs);
  const sx = (await one('select status,match_id from challenge_live_queue where user_id=$1', [x]));
  if (sx.status === 'cancelled') assert.equal(await one('select count(*)::int n from challenge_match_players where user_id=$1', [x]).then(r => r.n), 0);
  else assert.equal(sx.status, 'matched');
  await rpc('challenge_live_cancel($1,null)', [y]); await rpc('challenge_live_cancel($1,null)', [x]);   // isolation: nobody stays waiting for later scenarios
});
await test('simultaneous answers + duplicate update key + leave race: one answer row each, one settlement', async () => {
  const a = BASE + 40, c = BASE + 41; await mk(50);
  await rpc('challenge_live_join($1,$2,$3,$4::jsonb)', [a, U, 'sa', RULES]); await rpc('challenge_live_join($1,$2,$3,$4::jsonb)', [c, U, 'sc', RULES]);
  const st = await rpc('challenge_live_try_match($1,$2::jsonb)', [c, RULES]); const m = st.match_id; assert.ok(m);
  await q(`update challenge_matches set countdown_until=clock_timestamp()-interval '1s',question_opened_at=clock_timestamp()-interval '1s',
    question_deadline=clock_timestamp()+interval '60s',server_deadline=clock_timestamp()+interval '900s' where id=$1`, [m]);
  await rpc('challenge_live_tick($1)', [m]);
  const rs = await Promise.allSettled([
    rpc("challenge_live_answer($1,$2,0,0,'dupk')", [m, a]), rpc("challenge_live_answer($1,$2,0,0,'dupk')", [m, a]),
    rpc("challenge_live_answer($1,$2,0,1,'ck')", [m, c])]);
  noDeadlock(rs);
  assert.equal((await one('select count(*)::int n from challenge_match_answers where match_id=$1 and question_index=0 and user_id=$2', [m, a])).n, 1);
  const fin = await Promise.allSettled([rpc('challenge_live_leave($1,$2)', [m, a]), rpc('challenge_live_leave($1,$2)', [m, c]), rpc('challenge_live_tick($1)', [m])]);
  noDeadlock(fin);
  assert.equal((await one("select count(*)::int n from challenge_events where match_id=$1 and event_type in('match_abandoned','match_completed','match_timeout','match_cancelled')", [m])).n, 1);
  assert.equal((await one('select sum(challenges)::int n from challenge_profiles where user_id in($1,$2)', [a, c])).n, 2);
});

// ================= Phase 3B.5 additions: every scenario below uses independent pool connections (separate backends) =================
const sleep = ms => new Promise(r => setTimeout(r, ms));
const dead0 = Number((await one("select deadlocks from pg_stat_database where datname=current_database()")).deadlocks);
let peakConn = 0; const sampler = setInterval(async () => { try { peakConn = Math.max(peakConn, (await one("select count(*)::int n from pg_stat_activity where datname=current_database()")).n); } catch {} }, 40);
// 3C.5: real users table has free_points/paid_points (no `points`). Seed 3 sentinel users (removed at the end) so the check is not empty-vs-empty.
const SENT = [BASE - 1, BASE - 2, BASE - 3];
for (const [i, id] of SENT.entries()) await q('insert into users(user_id,free_points,paid_points) values($1,$2,$3)', [id, 12.5 + i, 100 + i]);
const POINTS_SQL = 'select user_id,free_points,paid_points from users order by user_id';
const usersBefore = JSON.stringify(await q(POINTS_SQL));
let seq = 0; let nextU = BASE + 100;
const mkUnit = async () => { const id = (await one("insert into challenge_units(subject_id,name) values($1,'cu'||$2) returning id", [s, String(++seq)])).id;
  await rpc("challenge_import_test($1,'official','json_upload',null,$2,1,$3::jsonb)", [id, `h-${BASE}-${seq}-${Math.random()}`, qs]); return id; };
const mkPair = async () => { const a = nextU++, c = nextU++; for (const u of [a, c]) await rpc('challenge_touch_profile($1,$2,$3)', [u, 'z' + u, b]); return [a, c]; };
const startMatch = async (unit, a, c, rules = RULES) => {
  for (const u of [a, c]) await rpc('challenge_live_join($1,$2,$3,$4::jsonb)', [u, unit, 'j' + u + '-' + Math.random(), rules]);
  const st = await rpc('challenge_live_try_match($1,$2::jsonb)', [c, rules]); assert.equal(st.status, 'matched'); return st.match_id; };
const begin = async m => { await q(`update challenge_matches set countdown_until=clock_timestamp()-interval '1s',question_opened_at=clock_timestamp()-interval '1s',
  question_deadline=clock_timestamp()+interval '60s',server_deadline=clock_timestamp()+interval '900s' where id=$1`, [m]); await rpc('challenge_live_tick($1)', [m]); };
const terminal = m => one("select count(*)::int n from challenge_events where match_id=$1 and event_type in('match_abandoned','match_completed','match_timeout','match_cancelled')", [m]).then(r => r.n);
const prof = u => one('select xp::int xp,trophies::int trophies,challenges::int challenges,wins::int wins,losses::int losses from challenge_profiles where user_id=$1', [u]);
const playFull = async (m, a, c, rules = RULES) => {   // a always right (option 0), c always wrong; 6 racing calls per question
  for (let i = 0; i < 10; i++) noDeadlock(await Promise.allSettled([
    rpc('challenge_live_answer($1,$2,$3,0,$4)', [m, a, i, `a${i}-${m}`]), rpc('challenge_live_answer($1,$2,$3,1,$4)', [m, c, i, `c${i}-${m}`]),
    rpc('challenge_live_answer($1,$2,$3,0,$4)', [m, a, i, `a${i}x-${m}`]), rpc('challenge_live_answer($1,$2,$3,0,$4)', [m, a, i, `a${i}-${m}`]),
    rpc('challenge_live_tick($1)', [m]), rpc('challenge_live_tick($1)', [m])])); };

await test('same pair, 8 racing matchmaking calls from separate connections: exactly one match, both players linked once', async () => {
  const unit = await mkUnit(); const [a, c] = await mkPair();
  for (const u of [a, c]) await rpc('challenge_live_join($1,$2,$3,$4::jsonb)', [u, unit, 'p' + u, RULES]);
  noDeadlock(await Promise.allSettled(Array.from({ length: 8 }, (_, i) => rpc('challenge_live_try_match($1,$2::jsonb)', [i % 2 ? a : c, RULES]))));
  assert.equal((await one("select count(*)::int n from challenge_matches where kind='live' and unit_id=$1 and status<>'cancelled'", [unit])).n, 1);
  const qr = await q('select user_id,status,match_id from challenge_live_queue where user_id in($1,$2)', [a, c]);
  assert.ok(qr.every(r => r.status === 'matched') && qr[0].match_id === qr[1].match_id);
  assert.equal((await one('select count(*)::int n from challenge_match_players where match_id=$1', [qr[0].match_id])).n, 2);
  await q("update challenge_matches set status='cancelled' where id=$1", [qr[0].match_id]);
});

await test('settlement concurrency: 6 racing calls per question incl. duplicate keys; completed once; XP/trophies/stats once; replays change nothing', async () => {
  const unit = await mkUnit(); const [a, c] = await mkPair(); const m = await startMatch(unit, a, c); await begin(m);
  await playFull(m, a, c);
  const mm = await one('select status from challenge_matches where id=$1', [m]); assert.equal(mm.status, 'completed');
  assert.equal(await terminal(m), 1);
  assert.equal((await one("select count(*)::int n from challenge_events where match_id=$1 and event_type='match_completed'", [m])).n, 1);
  assert.equal((await one('select count(*)::int n from challenge_match_answers where match_id=$1', [m])).n, 20);
  const pa = await prof(a), pc = await prof(c);
  assert.deepEqual([pa.xp, pa.trophies, pa.challenges, pa.wins, pa.losses], [100, 8, 1, 1, 0]);
  assert.deepEqual([pc.xp, pc.trophies, pc.challenges, pc.wins, pc.losses], [0, 0, 1, 0, 1]);
  const dl = await q('select user_id,xp_delta::int xp_delta,trophies_delta::int trophies_delta from challenge_match_players where match_id=$1 order by user_id', [m]);
  assert.deepEqual(dl.map(r => [r.xp_delta, r.trophies_delta]), [[100, 8], [0, 0]]);
  noDeadlock(await Promise.allSettled([...Array.from({ length: 6 }, () => rpc('challenge_live_tick($1)', [m])),
    rpc("challenge_live_answer($1,$2,9,0,'late1')", [m, a]), rpc("challenge_live_answer($1,$2,9,1,'late2')", [m, c]),
    rpc('challenge_live_leave($1,$2)', [m, a]), rpc('challenge_live_leave($1,$2)', [m, c])]));
  assert.deepEqual([(await prof(a)).xp, (await prof(a)).trophies, (await prof(a)).challenges], [100, 8, 1]);
  assert.equal(await terminal(m), 1); assert.equal((await one('select status from challenge_matches where id=$1', [m])).status, 'completed');
});

let acc = 0, tmo = 0;
await test('answer vs deadline (12 rounds, 3 ticker connections + 1 answer connection): deterministic by server time, one row, question resolved once', async () => {
  for (let it = 0; it < 12; it++) {
    const unit = await mkUnit(); const [a, c] = await mkPair(); const m = await startMatch(unit, a, c); await begin(m);
    await q("update challenge_matches set question_deadline=clock_timestamp()+interval '1500 milliseconds' where id=$1", [m]);   // wide window: answers must straddle the deadline even with pooler/network latency
    const dl = (await one('select question_deadline::text d from challenge_matches where id=$1', [m])).d;   // keep microseconds: compare inside PostgreSQL, not with JS Date
    const tickers = [0, 1, 2].map(async () => { for (let k = 0; k < 90; k++) { await rpc('challenge_live_tick($1)', [m]); await sleep(30); } });
    const ansP = (async () => { await sleep(600 + Math.floor(Math.random() * 1700)); /* 600..2300 ms: before AND after the 1500 ms deadline */ return rpc("challenge_live_answer($1,$2,0,0,$3)", [m, a, 'dl' + it + m]); })();
    noDeadlock(await Promise.allSettled([...tickers, ansP]));
    const rows = await q('select user_id,timed_out,(answered_at<$3::timestamptz) before from challenge_match_answers where match_id=$1 and question_index=0 and user_id=$2', [m, a, dl]);
    assert.equal(rows.length, 1);
    if (rows[0].timed_out) { tmo++; } else { acc++; assert.ok(rows[0].before, 'accepted answer must be strictly before the deadline'); }
    assert.equal((await one('select count(*)::int n from challenge_match_answers where match_id=$1 and question_index=0 and user_id=$2', [m, c])).n, 1);   // opponent timed out
    assert.equal((await one("select count(*)::int n from challenge_events where match_id=$1 and event_type='question_resolved' and question_index=0", [m])).n, 1);
    assert.equal(await terminal(m), 0);
    await q("update challenge_matches set status='cancelled' where id=$1", [m]);
  }
  console.log(`  deadline race outcomes: accepted=${acc} timed_out=${tmo}`);
  if (acc === 0 || tmo === 0) console.log('  WARNING: only one branch of the deadline race was exercised; widen/shift the sleep window');
});

await test('leave vs answer (10 rounds): one answer row at most, exactly one terminal event, abandoned => no XP, leaver does not win', async () => {
  for (let it = 0; it < 10; it++) {
    const unit = await mkUnit(); const [a, c] = await mkPair(); const m = await startMatch(unit, a, c); await begin(m);
    const jobs = [rpc("challenge_live_answer($1,$2,0,0,$3)", [m, a, 'la' + it + m]), rpc('challenge_live_leave($1,$2)', [m, c]), rpc('challenge_live_tick($1)', [m])];
    if (it % 2) jobs.reverse();
    noDeadlock(await Promise.allSettled(jobs));
    assert.ok((await one('select count(*)::int n from challenge_match_answers where match_id=$1 and question_index=0 and user_id=$2', [m, a])).n <= 1);
    assert.equal(await terminal(m), 1);
    assert.equal((await one('select status from challenge_matches where id=$1', [m])).status, 'abandoned');
    const pa = await prof(a), pc = await prof(c);
    assert.equal(pa.xp + pc.xp, 0); assert.equal(pc.wins, 0); assert.equal(pc.losses, 1); assert.equal(pa.wins, 1);
  }
});

await test('anti-farming: same pair repeated (repeat window off) => trophies stop after live_pair_daily_limit; retries never duplicate rewards', async () => {
  const unit = await mkUnit(); const [a, c] = await mkPair(); const R2 = JSON.stringify({ ...rules, repeat_content_seconds: 0 });
  const seen = [];
  for (let k = 0; k < 5; k++) {
    const m = await startMatch(unit, a, c, R2); await begin(m); await playFull(m, a, c, R2);
    assert.equal((await one('select status from challenge_matches where id=$1', [m])).status, 'completed');
    await Promise.allSettled([rpc('challenge_live_tick($1)', [m]), rpc('challenge_live_tick($1)', [m])]);
    const pa = await prof(a); seen.push([pa.xp, pa.trophies, pa.challenges]);
  }
  console.log('  pair series (xp,trophies,challenges after each match): ' + JSON.stringify(seen));
  const pa = await prof(a);
  assert.equal(pa.challenges, 5);
  assert.ok(pa.trophies <= 3 * rules.live_win_trophies, 'pair limit must cap trophies');
  assert.ok(pa.xp <= rules.daily_xp_limit, 'daily xp cap must hold');
});

await test('anti-farming: repeated same content inside the repeat window gives no second reward (default rules)', async () => {
  const unit = await mkUnit(); const [a, c] = await mkPair();
  const m1 = await startMatch(unit, a, c); await begin(m1); await playFull(m1, a, c);
  const t1 = (await prof(a)).trophies; assert.equal(t1, 8);
  const m2 = await startMatch(unit, a, c); await begin(m2); await playFull(m2, a, c);
  assert.equal((await one('select status from challenge_matches where id=$1', [m2])).status, 'completed');
  assert.equal((await prof(a)).trophies, t1); assert.equal((await prof(a)).xp, 100);
});

await test('paid points untouched by the whole concurrency run', async () => {
  assert.equal(JSON.stringify(await q(POINTS_SQL)), usersBefore);
});
clearInterval(sampler); await sleep(1500);
const dead1 = Number((await one("select deadlocks from pg_stat_database where datname=current_database()")).deadlocks);
console.log(`  connections: pool max=40, peak observed backends=${peakConn}; deadlocks during added scenarios: ${dead1 - dead0}`);
assert.equal(dead1 - dead0, 0, 'no deadlocks');

await q('delete from users where user_id = any($1)', [SENT]);
await q("update feature_flags set enabled=false where key in('challenge_system_enabled','live_matches_enabled')");
console.log('flags reset to false; sentinel users removed');
console.log(`ALL ${passed} CONCURRENCY CHECKS PASSED`); await pool.end();
