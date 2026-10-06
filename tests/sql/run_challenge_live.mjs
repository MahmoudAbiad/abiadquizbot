// Phase 3B SQL scenarios for 0010. Run in an isolated dir with @electric-sql/pglite@0.3.10 installed:
//   node run_challenge_live.mjs /absolute/path/to/abiadquizbot
// PGlite = real PostgreSQL (WASM) but ONE connection: every "simultaneous" case below is checked as
// serialized semantics (lock order result / idempotency). It is NOT a multi-connection concurrency test;
// run tests/sql/live_concurrency.mjs against real PostgreSQL for that.
import { PGlite } from '@electric-sql/pglite';
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
const root = process.argv[2];
if (!root) throw new Error('Pass repository absolute path');
const read = p => fs.readFileSync(path.join(root, p), 'utf8');
const rules = JSON.parse(read('services/challenge_rules.json'));
const R = o => JSON.stringify({ ...rules, ...o });
const db = new PGlite();
let passed = 0;
const test = async (name, fn) => { await fn(); passed++; console.log(`PASS ${passed}: ${name}`); };
const query = async (sql, v = []) => (await db.query(sql, v)).rows;
const one = async (sql, v = []) => (await query(sql, v))[0];
const rpc = async (sql, v = []) => (await one('select ' + sql + ' as value', v)).value;
const fail = (sql, v, code) => assert.rejects(() => query(sql, v), e => e.message.includes(code));

await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
CREATE TABLE feature_flags(key text PRIMARY KEY,enabled boolean NOT NULL,updated_at timestamptz DEFAULT now());
CREATE TABLE users(user_id bigint PRIMARY KEY,points integer); INSERT INTO users VALUES(1001,1234),(1002,4321);`);
await db.exec(read('db/migrations/0008_challenge_content.sql'));
await db.exec(read('db/migrations/0009_challenge_game.sql'));
await db.exec(read('db/migrations/0010_challenge_live.sql'));
await test('0010 can be re-applied (idempotent)', () => db.exec(read('db/migrations/0010_challenge_live.sql')));
await test('live flag defaults to false and is a separate row', async () => {
  assert.equal((await one("select enabled from feature_flags where key='live_matches_enabled'")).enabled, false);
});

const b = (await one("insert into challenge_branches(name) values('علمي') returning id")).id;
const s = (await one("insert into challenge_subjects(branch_id,name) values($1,'فيزياء') returning id", [b])).id;
const U = (await one("insert into challenge_units(subject_id,name) values($1,'حركة') returning id", [s])).id;
const U2 = (await one("insert into challenge_units(subject_id,name) values($1,'ضوء') returning id", [s])).id;
const EMPTY = (await one("insert into challenge_units(subject_id,name) values($1,'فارغة') returning id", [s])).id;
const b2 = (await one("insert into challenge_branches(name) values('أدبي') returning id")).id;
const mkQs = n => JSON.stringify(Array.from({ length: n }, (_, i) => ({ question: `س${i}`, options: ['أ', 'ب', 'ج'], correct_option_id: 0 })));
const T = (await rpc("challenge_import_test($1,'official','json_upload',null,'h-main',1,$2::jsonb)", [U, mkQs(10)])).test_id;
await rpc("challenge_import_test($1,'official','json_upload',null,'h-u2',1,$2::jsonb)", [U2, mkQs(10)]);
const U3 = (await one("insert into challenge_units(subject_id,name) values($1,'طاقة') returning id", [s])).id;
await rpc("challenge_import_test($1,'official','json_upload',null,'h-u3',1,$2::jsonb)", [U3, mkQs(10)]);
const mkProfile = async (u, trophies = 0, branch = b) => {
  await rpc('challenge_touch_profile($1,$2,$3)', [u, 'p' + u, branch]);
  await query('update challenge_profiles set trophies=$2 where user_id=$1', [u, trophies]);
};
await db.exec("update feature_flags set enabled=true where key in('challenge_system_enabled','live_matches_enabled')");
const join = (u, key, unit = U, r = R({})) => rpc('challenge_live_join($1,$2,$3,$4::jsonb)', [u, unit, key, r]);
const tm = (u, r = R({})) => rpc('challenge_live_try_match($1,$2::jsonb)', [u, r]);
const qstate = u => rpc('challenge_live_queue_state($1)', [u]);
const mstate = (m, u) => rpc('challenge_live_state($1,$2)', [m, u]);
const ans = (m, u, i, o, k = null) => rpc('challenge_live_answer($1,$2,$3,$4,$5)', [m, u, i, o, k]);
const tick = m => rpc('challenge_live_tick($1)', [m]);
const leave = (m, u) => rpc('challenge_live_leave($1,$2)', [m, u]);
// Make the countdown elapsed + give question 0 a fresh 30 s window, then let the engine start the match.
const begin = async m => {
  await query(`update challenge_matches set countdown_until=clock_timestamp()-interval '1 second',
    question_opened_at=clock_timestamp()-interval '1 second',question_deadline=clock_timestamp()+interval '30 seconds',
    server_deadline=clock_timestamp()+interval '600 seconds' where id=$1`, [m]);
  return tick(m);
};
const pair = async (x, y, key, unit = U, r = R({})) => {
  await join(x, key + 'x', unit, r); await join(y, key + 'y', unit, r);
  const st = await tm(y, r); assert.equal(st.status, 'matched'); return st.match_id;
};
const playAll = async (m, ua, ub, correctA, correctB) => {
  let st;
  for (let i = 0; i < 10; i++) {
    await ans(m, ua, i, i < correctA ? 0 : 1, `${m}-a-${i}`);
    st = await ans(m, ub, i, i < correctB ? 0 : 1, `${m}-b-${i}`);
  }
  return st;
};
const count = async (sql, v = []) => (await one(`select count(*)::int n from (${sql}) z`, v)).n;

// ---------- queue ----------
for (const u of [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28]) await mkProfile(u, u * 10);
// branch b2 needs playable content, otherwise challenge_touch_profile (0009) rejects the profile with no_content
const s2 = (await one("insert into challenge_subjects(branch_id,name) values($1,'أدب') returning id", [b2])).id;
const UB2 = (await one("insert into challenge_units(subject_id,name) values($1,'نصوص') returning id", [s2])).id;
await rpc("challenge_import_test($1,'official','json_upload',null,'h-b2',1,$2::jsonb)", [UB2, mkQs(10)]);
await mkProfile(900, 0, b2);

await test('join respects flags', async () => {
  await db.exec("update feature_flags set enabled=false where key='live_matches_enabled'");
  await fail("select challenge_live_join(1,$1,'f1',$2)", [U, R({})], 'game_disabled');
  await db.exec("update feature_flags set enabled=true where key='live_matches_enabled'");
});
await test('join: waiting, same-key retry idempotent, different key while queued rejected', async () => {
  let st = await join(1, 'k1'); assert.equal(st.status, 'waiting');
  st = await join(1, 'k1'); assert.equal(st.status, 'waiting');
  assert.equal(await count('select 1 from challenge_live_queue where user_id=1'), 1);
  await fail("select challenge_live_join(1,$1,'k1b',$2)", [U, R({})], 'already_queued');
});
await test('join validates unit content and branch; unit is never swapped', async () => {
  await fail("select challenge_live_join(2,$1,'e1',$2)", [EMPTY, R({})], 'no_content');
  await fail("select challenge_live_join(900,$1,'e2',$2)", [U, R({})], 'branch_mismatch');
  await fail("select challenge_live_join(2,$1,'',$2)", [U, R({})], 'invalid_request');
});
await test('no candidate => stays waiting; other unit never matched', async () => {
  await join(2, 'k2', U2);
  assert.equal((await tm(2)).status, 'waiting');
  assert.equal((await tm(1)).status, 'waiting');
  assert.equal(await count("select 1 from challenge_matches where kind='live'"), 0);
  await rpc('challenge_live_cancel(2,null)');   // keep later scenarios isolated
});
await test('range: far trophies do not match at once, widen with waiting time', async () => {
  await mkProfile(30, 0); await mkProfile(31, 450);
  await join(30, 'r30', U3); await join(31, 'r31', U3);
  assert.equal((await tm(31)).status, 'waiting');
  await query("update challenge_live_queue set joined_at=clock_timestamp()-interval '45 seconds' where user_id in(30,31)");
  const st = await tm(31); assert.equal(st.status, 'matched');
  await leave(st.match_id, 31);   // free them (countdown => cancelled)
});
let M1;
await test('closest compatible player is paired; exactly ONE live match; canonical player order', async () => {
  await join(3, 'k3'); await join(4, 'k4');   // 1 (10 trophies) is waiting too: 3,4 are nearer to each other? 30,40 vs 10
  const st = await tm(4); assert.equal(st.status, 'matched'); M1 = st.match_id;
  const m = await one('select * from challenge_matches where id=$1', [M1]);
  assert.equal(m.kind, 'live'); assert.equal(m.status, 'created'); assert.ok(m.attacker_id < m.defender_id);
  assert.equal(await count("select 1 from challenge_matches where kind='live' and status<>'cancelled'"), 1);   // earlier range test left one cancelled match
  assert.equal(await count("select 1 from challenge_live_queue where match_id=$1 and status='matched'", [M1]), 2);
  assert.equal(await count('select 1 from challenge_match_players where match_id=$1', [M1]), 2);
  assert.equal((await qstate(1)).status, 'waiting');   // third player untouched
  await rpc('challenge_live_cancel(1,null)');
});
await test('duplicate matchmaking (retry / other worker) returns same match, creates nothing', async () => {
  assert.equal((await tm(4)).match_id, M1); assert.equal((await tm(3)).match_id, M1);
  assert.equal(await count("select 1 from challenge_matches where kind='live' and status<>'cancelled'"), 1);
});
await test('a matched player cannot join again, and cannot be paired with a 2nd opponent', async () => {
  await fail("select challenge_live_join(3,$1,'again',$2)", [U, R({})], 'in_live_match');
  await join(5, 'k5'); assert.equal((await tm(5)).status, 'waiting');
  assert.equal(await count("select 1 from challenge_live_queue where match_id=$1", [M1]), 2);
});
await test('cancel: idempotent, no zombie, stale key harmless, replayed join does not resurrect', async () => {
  assert.equal((await rpc("challenge_live_cancel(5,'k5')")).status, 'cancelled');
  assert.equal((await rpc("challenge_live_cancel(5,'k5')")).status, 'cancelled');
  assert.equal((await join(5, 'k5')).status, 'cancelled');           // retried old join: still cancelled
  assert.equal((await tm(5)).status, 'cancelled');
  await join(5, 'k5-new'); assert.equal((await rpc("challenge_live_cancel(5,'k5')")).status, 'waiting'); // stale cancel
  assert.equal((await rpc("challenge_live_cancel(5,'k5-new')")).status, 'cancelled');
  await join(6, 'k6'); assert.equal((await tm(6)).status, 'waiting');   // cancelled 5 is never chosen
});
await test('cancel never cancels a started match', async () => {
  const st = await rpc("challenge_live_cancel(3,null)");
  assert.equal(st.status, 'matched'); assert.equal((await one('select status from challenge_matches where id=$1', [M1])).status, 'created');
});
await test('async attack blocked while searching or in a live match', async () => {
  await mkProfile(40, 0);
  await db.exec("update feature_flags set enabled=true where key='async_attacks_enabled'");
  // user 4 is the defender-slot player of the live match M1 (queue row 'matched') => async start refused by trigger
  await fail("select challenge_start_attack(4,40,$1,$2,'as1',$3)", [U, T, R({})], 'in_live_queue');
});
await test('queue expiry marks overdue searches expired; expired never matched', async () => {
  await query("update challenge_live_queue set expires_at=clock_timestamp()-interval '1 second' where user_id=6");
  const gone = await rpc('challenge_live_expire_queue(10)'); assert.deepEqual(gone, [6]);
  assert.equal((await qstate(6)).status, 'expired');
  assert.equal((await rpc('challenge_live_expire_queue(10)')).length, 0);
  await join(7, 'k7'); assert.equal((await tm(7)).status, 'waiting');   // expired 6 is not a candidate
});

// ---------- match ----------
await test('countdown: no question exposed, answers refused until start', async () => {
  const st = await mstate(M1, 3); assert.equal(st.status, 'created'); assert.equal(st.question, null);
  const r = await ans(M1, 3, 0, 0, 'early'); assert.equal(r.status, 'created');
  assert.equal(await count('select 1 from challenge_match_answers where match_id=$1', [M1]), 0);
});
await test('both players see the same questions, order and deadline; no secrets in state', async () => {
  await begin(M1);
  const a = await mstate(M1, 3), c = await mstate(M1, 4);
  assert.equal(a.status, 'in_progress'); assert.deepEqual(a.question, c.question); assert.equal(a.deadline, c.deadline);
  for (const st of [a, c]) { const j = JSON.stringify(st); assert.ok(!j.includes('correct_option_id')); assert.ok(!j.includes('snapshot')); }
  assert.equal(await count('select 1 from challenge_match_answers where match_id=$1', [M1]), 0);
});
await test('opponent answer is hidden (only a boolean), pick/correctness revealed after resolution', async () => {
  let st = await ans(M1, 3, 0, 0, 'u1');
  assert.equal(st.me.answered, true); assert.equal(st.opponent.answered, false); assert.equal(st.index, 0);
  const other = await mstate(M1, 4); assert.equal(other.opponent.answered, true); assert.equal(other.opponent.score, 0);
  assert.ok(!JSON.stringify(other).includes('"option_id":0'));
  st = await ans(M1, 4, 0, 1, 'u2');
  assert.equal(st.index, 1); assert.equal(st.previous.correct_option_id, 0);
  assert.equal(st.previous.mine.is_correct, false); assert.equal(st.previous.opponent.is_correct, true);
});
await test('duplicate update / double tap / stale index / replay record exactly one answer', async () => {
  await ans(M1, 3, 1, 0, 'dup'); await ans(M1, 3, 1, 1, 'dup');     // same Telegram update
  await ans(M1, 3, 1, 1, 'tap2');                                    // second tap, other update id
  await ans(M1, 3, 0, 0, 'stale');                                   // old question
  assert.equal(await count('select 1 from challenge_match_answers where match_id=$1 and user_id=3 and question_index=1', [M1]), 1);
  assert.equal((await one('select option_id from challenge_match_answers where match_id=$1 and user_id=3 and question_index=1', [M1])).option_id, 0);
  // player 3 already answered q1 (replay returns state before option validation by design) -> validate on the opponent who has not answered
  assert.equal(await count('select 1 from challenge_match_answers where match_id=$1 and user_id=4 and question_index=1', [M1]), 0);
  await fail('select challenge_live_answer($1,4,1,99,\'bad\')', [M1], 'invalid_option');
  await fail('select challenge_live_answer($1,999,1,0,\'x\')', [M1], 'not_your_match');
});
await test('deadline rule: strictly before accepted, at/after = timeout (same as async)', async () => {
  const m = await one('select question_deadline d,question_index i from challenge_matches where id=$1', [M1]);
  await rpc('challenge_live_progress($1,$2::timestamptz - interval \'1 millisecond\')', [M1, m.d]);
  assert.equal((await one('select question_index i from challenge_matches where id=$1', [M1])).i, m.i);       // 1 ms early: still open
  await rpc('challenge_live_progress($1,$2::timestamptz)', [M1, m.d]);
  assert.equal((await one('select question_index i from challenge_matches where id=$1', [M1])).i, m.i + 1);  // exactly at deadline: resolved
  const t4 = await one('select * from challenge_match_answers where match_id=$1 and user_id=4 and question_index=$2', [M1, m.i]);
  assert.equal(t4.timed_out, true); assert.equal(t4.score, 0); assert.equal(t4.option_id, null);
});
await test('late answer after deadline is discarded as timeout and never double-scores; timer vs answer race is serialized', async () => {
  const m = await one('select question_index i from challenge_matches where id=$1', [M1]);
  await query("update challenge_matches set question_deadline=clock_timestamp()-interval '1 millisecond' where id=$1", [M1]);
  const st = await ans(M1, 3, m.i, 0, 'late'); assert.equal(st.index, m.i + 1);
  const rows = await query('select user_id,timed_out,score from challenge_match_answers where match_id=$1 and question_index=$2', [M1, m.i]);
  assert.equal(rows.length, 2); assert.ok(rows.every(r => r.timed_out && r.score === 0));
  assert.equal((await tick(M1)).index, m.i + 1);          // tick right after: nothing resolved twice
});
await test('worker restart recovery: due list is read from PostgreSQL and ticks are idempotent', async () => {
  await query("update challenge_matches set question_deadline=clock_timestamp()-interval '1 second' where id=$1", [M1]);
  const due = await rpc('challenge_live_due(10)'); assert.ok(due.includes(M1));
  const i0 = (await one('select question_index i from challenge_matches where id=$1', [M1])).i;
  await tick(M1); await tick(M1);
  assert.equal((await one('select question_index i from challenge_matches where id=$1', [M1])).i, i0 + 1);
});
await test('disabling the test mid-match keeps the pinned snapshot; new matches skip disabled tests', async () => {
  await join(8, 'k8'); await join(9, 'k9');               // both waiting while the test is still active
  await query("update challenge_tests set status='disabled' where id=$1", [T]);
  assert.equal((await mstate(M1, 3)).status, 'in_progress');
  assert.equal((await tm(9)).reason, 'no_content');       // only test is disabled => no AI fallback, no match
  await rpc("challenge_live_cancel(8,null)"); await rpc("challenge_live_cancel(9,null)");
  await fail("select challenge_live_join(8,$1,'k8b',$2)", [U, R({})], 'no_content');   // join itself is refused fast when no active test
  await query("update challenge_tests set status='active' where id=$1", [T]);
});
await test('full match: completed once; winner by score; XP/trophies once; queue freed; events', async () => {
  // finish M1 with remaining questions: A=3 always correct, B=4 wrong
  let st = await mstate(M1, 3);
  for (let i = st.index; i < 10; i++) { await ans(M1, 3, i, 0, `m1a${i}`); st = await ans(M1, 4, i, 1, `m1b${i}`); }
  const m = await one('select * from challenge_matches where id=$1', [M1]);
  assert.equal(m.status, 'completed'); assert.equal(m.result.winner_id, 3);
  const p3 = await one('select * from challenge_profiles where user_id=3'), p4 = await one('select * from challenge_profiles where user_id=4');
  assert.equal(p3.wins, 1); assert.equal(p4.losses, 1); assert.equal(p3.challenges, 1);
  const before = p3.xp;
  await ans(M1, 3, 9, 0, 'after-end'); await tick(M1); await leave(M1, 4);   // retries / second worker
  assert.equal((await one('select xp from challenge_profiles where user_id=3')).xp, before);
  assert.equal((await one('select challenges from challenge_profiles where user_id=3')).challenges, 1);
  assert.equal(await count("select 1 from challenge_events where match_id=$1 and event_type='match_completed'", [M1]), 1);
  assert.equal(await count("select 1 from challenge_live_queue where match_id=$1 and status='done'", [M1]), 2);
  assert.equal(p4.trophies, 40);                               // loser keeps trophies (live_loss_trophies = 0)
});
await test('rewards: only Challenge XP/trophies; paid points untouched; shared daily caps', async () => {
  assert.equal((await one('select points from users where user_id=1001')).points, 1234);
  assert.equal((await one('select points from users where user_id=1002')).points, 4321);
});
await test('draw: equal score, small trophy award, no XP, no wins/losses', async () => {
  await mkProfile(50, 0); await mkProfile(51, 0);
  const m = await pair(50, 51, 'draw', U2); await begin(m);
  const st = await playAll(m, 50, 51, 0, 0);
  const r = (await one('select result from challenge_matches where id=$1', [m])).result;
  assert.equal(r.draw, true); assert.equal(r.winner_id, null);
  const p = await one('select * from challenge_profiles where user_id=50');
  assert.equal(p.trophies, rules.live_draw_trophies); assert.equal(p.wins + p.losses, 0); assert.equal(p.challenges, 1);
});
await test('forfeit mid-match: leaver loses, opponent wins trophies but no XP; countdown leave = cancelled, no stats', async () => {
  await mkProfile(52, 0); await mkProfile(53, 0);
  const m = await pair(52, 53, 'ff', U2); await begin(m);
  await ans(m, 52, 0, 0, 'ff1'); await ans(m, 53, 0, 0, 'ff2');
  const st = await leave(m, 52); assert.equal(st.status, 'abandoned'); assert.equal(st.result.winner_id, 53);
  const w = await one('select * from challenge_profiles where user_id=53'), l = await one('select * from challenge_profiles where user_id=52');
  assert.equal(w.xp, 0); assert.equal(w.wins, 1); assert.equal(l.losses, 1);
  await mkProfile(54, 0); await mkProfile(55, 0);
  const c = await pair(54, 55, 'cc', U2); const cs = await leave(c, 54);
  assert.equal(cs.status, 'cancelled'); assert.equal((await one('select challenges from challenge_profiles where user_id=54')).challenges, 0);
  assert.equal((await qstate(54)).status, 'done');
});
await test('overall expiry: no rewards, no stats, queue freed, match_timeout event', async () => {
  await mkProfile(56, 0); await mkProfile(57, 0);
  const m = await pair(56, 57, 'ex', U2); await begin(m);
  await query("update challenge_matches set server_deadline=clock_timestamp()-interval '1 millisecond' where id=$1", [m]);
  assert.equal((await tick(m)).status, 'expired');
  assert.equal((await one('select challenges,xp from challenge_profiles where user_id=56')).challenges, 0);
  assert.equal(await count("select 1 from challenge_events where match_id=$1 and event_type='match_timeout'", [m]), 1);
  assert.equal((await qstate(57)).status, 'done');
});
await test('anti-farming: pair limit and content-repeat window stop repeated rewards', async () => {
  await mkProfile(60, 0); await mkProfile(61, 0);
  const r = R({ live_pair_daily_limit: 1, repeat_content_seconds: 1 });
  const m1 = await pair(60, 61, 'af1', U2, r); await begin(m1); await playAll(m1, 60, 61, 10, 5);
  const t1 = (await one('select trophies from challenge_profiles where user_id=60')).trophies; assert.ok(t1 > 0);
  await query("update challenge_matches set created_at=created_at-interval '10 seconds' where id=$1", [m1]);
  const m2 = await pair(60, 61, 'af2', U2, r); await begin(m2); await playAll(m2, 60, 61, 10, 5);
  const p = await one('select * from challenge_profiles where user_id=60');
  assert.equal(p.trophies, t1); assert.equal(p.xp, 100);       // 2nd match: pair limit reached => nothing new
  assert.equal(p.wins, 2);                                      // history/stats still recorded
});
await test('daily XP/trophy caps apply', async () => {
  await mkProfile(62, 0); await mkProfile(63, 0);
  const r = R({ daily_xp_limit: 25, daily_trophy_limit: 3 });
  const m = await pair(62, 63, 'cap', U2, r); await begin(m); await playAll(m, 62, 63, 10, 10);
  const p = await one('select xp,trophies from challenge_profiles where user_id=62'); assert.equal(p.xp, 25); assert.ok(p.trophies <= 3);
});
await test('async RPCs cannot see or drive live matches; live data never in async history', async () => {
  await mkProfile(64, 0); await mkProfile(65, 0);
  const m = await pair(64, 65, 'iso', U2); await begin(m);
  const lo = Math.min(64, 65);
  await fail('select challenge_attack_state($1,$2)', [m, lo], 'not_your_attack');
  await fail("select challenge_advance_attack($1,$2,'abandon')", [m, lo], 'not_your_attack');
  await leave(m, 64);
});
await test('failure while settling rolls the whole answer back; retry settles once', async () => {
  await mkProfile(66, 0); await mkProfile(67, 0);
  const m = await pair(66, 67, 'rb', U2); await begin(m);
  for (let i = 0; i < 9; i++) { await ans(m, 66, i, 0, `rb-a${i}`); await ans(m, 67, i, 0, `rb-b${i}`); }
  await ans(m, 66, 9, 0, 'rb-a9');
  await db.exec(`CREATE FUNCTION fail_live() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected_failure'; END $$;
   CREATE TRIGGER test_fail BEFORE INSERT ON challenge_towers FOR EACH ROW EXECUTE FUNCTION fail_live();`);
  await fail("select challenge_live_answer($1,67,9,0,'rb-b9')", [m], 'injected_failure');
  assert.equal(await count('select 1 from challenge_match_answers where match_id=$1 and question_index=9', [m]), 1);
  assert.equal((await one('select status from challenge_matches where id=$1', [m])).status, 'in_progress');
  await db.exec('DROP TRIGGER test_fail ON challenge_towers; DROP FUNCTION fail_live();');
  const st = await ans(m, 67, 9, 0, 'rb-b9'); assert.equal(st.status, 'completed');
  assert.equal((await one('select challenges from challenge_profiles where user_id=66')).challenges, 1);
});
await test('security: RLS on, anon/authenticated denied, service_role allowed', async () => {
  assert.equal((await one("select relrowsecurity r from pg_class where relname='challenge_live_queue'")).r, true);
  await db.exec('SET ROLE anon');
  await assert.rejects(() => query('select * from challenge_live_queue'), e => /permission denied/i.test(e.message));
  await assert.rejects(() => query('select challenge_live_join(1,$1,\'x\',$2)', [U, R({})]), e => /permission denied/i.test(e.message));
  await db.exec('RESET ROLE'); await db.exec('SET ROLE service_role');
  await query('select count(*) from challenge_live_queue'); await db.exec('RESET ROLE');
});
await test('pool lookup can use the pool index (bounded range, no random sort)', async () => {
  await db.exec('SET enable_seqscan=off');
  const plan = JSON.stringify(await query("explain select user_id from challenge_live_queue where status='waiting' and unit_id=$1 and trophies between 0 and 500 order by abs(trophies-100),joined_at,user_id limit 1", [U]));
  await db.exec('RESET enable_seqscan');
  assert.ok(plan.includes('challenge_live_queue_pool'));
});
await test('paid points never changed during the whole run', async () => {
  assert.equal((await one('select sum(points)::int s from users')).s, 5555);
});
console.log(`ALL ${passed} LIVE SQL SCENARIOS PASSED`);
await db.close();
