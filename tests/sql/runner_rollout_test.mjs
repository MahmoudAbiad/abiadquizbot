// Verify the exact atomic installer assembled from reviewed migration sources.
import {PGlite} from '@electric-sql/pglite';
import {readFileSync, writeFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
const names=['0017_educational_runner.sql','0018_runner_image_cache.sql',
 '20261020191247_runner_wallet.sql','20261020193546_runner_knowledge_car.sql'];
const tables=['runner_settings','runner_images','runner_rounds','runner_image_gc',
 'runner_wallets','runner_tracks','runner_mastery','runner_daily_study'];
const sources=names.map(n=>readFileSync(new URL(`../../db/migrations/${n}`,import.meta.url),'utf8'));
const fingerprint=createHash('sha256').update(sources.join('\n')).digest('hex');
const preflight=`
begin;
set local lock_timeout='5s';
set local statement_timeout='60s';
select pg_advisory_xact_lock(21401, 1021);
do $$ begin
 if to_regclass('public.users') is null or to_regclass('public.quizzes') is null then
  raise exception 'Missing parent tables'; end if;
 if not exists(select 1 from pg_attribute where attrelid='public.users'::regclass and attname='user_id' and atttypid='bigint'::regtype and not attisdropped)
 or not exists(select 1 from pg_attribute where attrelid='public.quizzes'::regclass and attname='id' and atttypid='uuid'::regtype and not attisdropped) then
  raise exception 'Unexpected parent key types'; end if;
 if exists(select 1 from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and left(c.relname,7)='runner_')
 or exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public' and left(p.proname,7)='runner_') then
  raise exception 'Existing Runner objects: inspect before installation'; end if;
 if not exists(select 1 from public.feature_flags where key='runner_enabled' and enabled=false) then
  raise exception 'Runner flag must exist and remain disabled'; end if;
 if not (select relrowsecurity from pg_class where oid='public.feature_flags'::regclass)
 or has_table_privilege('anon','public.feature_flags','UPDATE')
 or has_table_privilege('authenticated','public.feature_flags','UPDATE') then
  raise exception 'Feature flag protection is missing'; end if;
end $$;
`;
const body=sources.map((s,i)=>`-- Source ${names[i]}\n`+s.replace(/^begin;\s*$/gmi,'').replace(/^commit;\s*$/gmi,'')).join('\n');
const verify=`
do $$ declare t text; f regprocedure; begin
 foreach t in array array[${tables.map(t=>`'${t}'`).join(',')}] loop
  if not (select relrowsecurity from pg_class where oid=('public.'||t)::regclass) then raise exception 'Missing RLS: %',t; end if;
  if has_table_privilege('anon','public.'||t,'SELECT,INSERT,UPDATE,DELETE')
  or has_table_privilege('authenticated','public.'||t,'SELECT,INSERT,UPDATE,DELETE') then raise exception 'Public table privilege: %',t; end if;
  if not has_table_privilege('service_role','public.'||t,'SELECT')
  or not has_table_privilege('service_role','public.'||t,'INSERT')
  or not has_table_privilege('service_role','public.'||t,'UPDATE')
  or not has_table_privilege('service_role','public.'||t,'DELETE') then raise exception 'Missing server privilege: %',t; end if;
 end loop;
 if (select count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public' and left(p.proname,7)='runner_')<>8 then raise exception 'Unexpected Runner function count'; end if;
 for f in select p.oid::regprocedure from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public' and left(p.proname,7)='runner_' loop
  if has_function_privilege('anon',f,'EXECUTE') or has_function_privilege('authenticated',f,'EXECUTE') then raise exception 'Public function privilege: %',f; end if;
  if (select prosecdef from pg_proc where oid=f) then raise exception 'Privileged function: %',f; end if;
 end loop;
end $$;
comment on table public.runner_rounds is 'Runner atomic rollout; source sha256 ${fingerprint}';
commit;
select 'runner_schema_verified_flag_off' as result;
`;
const installer=`-- Reviewed additive installer. Target: pqawoanhbgvdtbdhnbod only.\n-- Verify backup and project identity before execution. No activation or existing user-data changes.\n-- Source SHA256 ${fingerprint}\n${preflight}${body}${verify}`;
if(process.env.RUNNER_INSTALLER_OUTPUT) writeFileSync(process.env.RUNNER_INSTALLER_OUTPUT,installer);
const db=new PGlite();
await db.exec(`create role anon;create role authenticated;create role service_role bypassrls;
 create table users(user_id bigint primary key); create table quizzes(id uuid primary key,quiz_data jsonb);
 create table feature_flags(key text primary key,enabled boolean); alter table feature_flags enable row level security;
 insert into feature_flags values('runner_enabled',false),('existing_game',true);
 insert into users values(7); insert into quizzes values('11111111-1111-1111-1111-111111111111','[]');`);
await db.exec(installer);
assert.deepEqual((await db.query('select * from users')).rows,[{user_id:7}]);
assert.equal((await db.query('select count(*)::int n from quizzes')).rows[0].n,1);
assert.equal((await db.query("select enabled from feature_flags where key='runner_enabled'")).rows[0].enabled,false);
assert.equal((await db.query("select enabled from feature_flags where key='existing_game'")).rows[0].enabled,true);
await assert.rejects(()=>db.exec(installer),/Existing Runner objects/);
await db.exec('rollback');
await db.exec('begin;drop table runner_daily_study;rollback;');
assert.ok((await db.query("select to_regclass('runner_daily_study') t")).rows[0].t);
await db.close();
const failed=new PGlite();
await failed.exec(`create role anon;create role authenticated;create role service_role bypassrls;
 create table users(user_id bigint primary key);create table quizzes(id uuid primary key,quiz_data jsonb);
 create table feature_flags(key text primary key,enabled boolean);alter table feature_flags enable row level security;
 insert into feature_flags values('runner_enabled',false);`);
await assert.rejects(()=>failed.exec(installer.replace('comment on table public.runner_rounds',"select 1/0;\ncomment on table public.runner_rounds")),/division by zero/);
await failed.exec('rollback');
assert.equal((await failed.query("select count(*)::int n from pg_tables where schemaname='public' and left(tablename,7)='runner_'")).rows[0].n,0);
await failed.close();
console.log('Runner atomic installer: eight protected tables/functions, parent data preserved, flag off and repeat-install rejection PASS');
