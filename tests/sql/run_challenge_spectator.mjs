// node run_challenge_spectator.mjs /absolute/repo (isolated @electric-sql/pglite@0.3.10)
import { PGlite } from '@electric-sql/pglite';
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
const root=process.argv[2];
if(!root) throw Error('Pass repository root');
const read=p=>fs.readFileSync(path.join(root,p),'utf8');
const db=new PGlite();
await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
CREATE TABLE feature_flags(key text PRIMARY KEY,enabled boolean NOT NULL,updated_at timestamptz DEFAULT now());
GRANT SELECT ON feature_flags TO service_role;
CREATE TABLE app_settings(key text PRIMARY KEY,value double precision,label text,updated_at timestamptz DEFAULT now());
CREATE TABLE users(user_id bigint PRIMARY KEY,points integer,free_points integer,paid_points integer);
INSERT INTO users VALUES(880000000000001,321,25,296),(880000000000002,111,11,100);`);
for(const f of fs.readdirSync(path.join(root,'db/migrations')).sort())
 if(/^00(0[89]|1[0-5])_/.test(f)&&!f.includes('rollback')) await db.exec(read('db/migrations/'+f));
const definitions=async()=> (await db.query(`select proname,pg_get_functiondef(oid) ddl from pg_proc
 where pronamespace='public'::regnamespace and proname in('challenge_start_attack','challenge_claim_notifications') order by proname`)).rows;
const before=await definitions();
await db.exec(read('tests/sql/challenge_spectator_test.sql'));
console.log('PASS: spectator SQL authorization, projection, lifecycle, non-mutation and grants assertions');
await db.exec(read('db/migrations/0015_challenge_spectator_rollback.sql'));
assert.equal((await db.query("select to_regprocedure('challenge_spectator_state(uuid,bigint)') f")).rows[0].f,null);
assert.deepEqual(await definitions(),before);
await db.exec(read('db/migrations/0015_challenge_spectator.sql'));
await db.exec(read('db/migrations/0015_challenge_spectator.sql'));
assert.deepEqual(await definitions(),before);
await db.exec(read('tests/sql/challenge_spectator_test.sql'));
console.log('PASS: rollback, reapply, idempotence, 0011--0014 unchanged; assertions rerun');
console.log('ASSERT statements per validation:',(read('tests/sql/challenge_spectator_test.sql').match(/\bASSERT\b/g)||[]).length);
await db.close();
