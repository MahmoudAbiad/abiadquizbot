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
 if(/^00(0[89]|1[0-6])_/.test(f)&&!f.includes('rollback')) await db.exec(read('db/migrations/'+f));
await db.exec(read('tests/sql/challenge_branch_archive_test.sql'));
await db.exec(read('db/migrations/0016_challenge_branch_archive_rollback.sql'));
await db.exec(read('db/migrations/0016_challenge_branch_archive.sql'));
await db.exec(read('tests/sql/challenge_branch_archive_test.sql'));
console.log('PASS archive SQL: active rejection, atomic FK rollback, restore conflict, purge, repeat purge, grants; rollback/reapply');
await db.close();
