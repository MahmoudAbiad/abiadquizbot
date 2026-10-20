// Run with node; requires @electric-sql/pglite (same harness as other SQL tests).
import {PGlite} from '@electric-sql/pglite';
import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
const db=new PGlite();
await db.exec(`create role anon; create role authenticated; create role service_role bypassrls;
create table public.users(user_id bigint primary key); create table public.quizzes(id uuid primary key,quiz_data jsonb);
insert into users values(7); insert into quizzes values('11111111-1111-1111-1111-111111111111','[{"options":["a","b","c","d"]}]');`);
await db.exec(readFileSync(new URL('../../db/migrations/0017_educational_runner.sql',import.meta.url),'utf8'));
await db.exec(readFileSync(new URL('../../db/migrations/0018_runner_image_cache.sql',import.meta.url),'utf8'));
const tables=['runner_settings','runner_images','runner_rounds','runner_image_gc'];
for(const t of tables){const {rows}=await db.query(`select relrowsecurity from pg_class where relname=$1`,[t]);assert.equal(rows[0].relrowsecurity,true);
 for(const role of ['anon','authenticated']){const r=await db.query(`select has_table_privilege($1,$2,'SELECT') allowed`,[role,t]);assert.equal(r.rows[0].allowed,false);}}
await db.exec(`insert into runner_settings(quiz_id,ai_times) values('11111111-1111-1111-1111-111111111111','{"q":45}');
update runner_settings set uniform_seconds=60,version=version+1 where version=0;`);
assert.equal((await db.query('select ai_times from runner_settings')).rows[0].ai_times.q,45);
assert.equal((await db.query('update runner_settings set lives=3 where version=0 returning *')).rows.length,0);
assert.equal((await db.query('select jsonb_array_length(quiz_data->0->\'options\') n from quizzes')).rows[0].n,4);
let rejected=false;try{await db.exec('update runner_settings set lives=0');}catch{rejected=true;}assert.equal(rejected,true);
await db.exec(`insert into runner_images(quiz_id,question_key,part,game_image_url) values('11111111-1111-1111-1111-111111111111','q','question','url');`);
assert.equal((await db.query('select count(*) n from runner_images where expires_at>now()')).rows[0].n,1);
await db.exec(`update runner_images set expires_at=now()-interval '1 day'`);
assert.equal((await db.query('select count(*) n from runner_images where expires_at>now()')).rows[0].n,0);
for(const name of ['runner_images_expiry','runner_rounds_retention','runner_rounds_finished_retention'])assert.equal((await db.query('select count(*) n from pg_indexes where indexname=$1',[name])).rows[0].n,1);
await db.exec(`delete from quizzes;`);
assert.equal((await db.query('select count(*) n from runner_images')).rows[0].n,0);
assert.equal((await db.query('select count(*) n from runner_image_gc')).rows[0].n,1);assert.equal((await db.query('select * from runner_settings')).rows.length,0);
await db.close();console.log('runner SQL: RLS, grants, CAS, constraints, isolation and cascade PASS');
