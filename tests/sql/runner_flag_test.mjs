import {PGlite} from '@electric-sql/pglite';
import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
const db = new PGlite();
await db.exec(`create role anon; create role authenticated; create role service_role bypassrls;
create table public.feature_flags(key text primary key, enabled boolean not null default true);
grant all on public.feature_flags to anon, authenticated;
insert into feature_flags values ('payments_enabled',true);`);
const sql=readFileSync(new URL('../../db/review/runner_feature_flag_hardening.sql',import.meta.url),'utf8');
await db.exec(sql);
for (const role of ['anon','authenticated']) {
  for (const op of ['SELECT','INSERT','UPDATE','DELETE']) {
    const r=await db.query('select has_table_privilege($1, $2, $3) allowed',[role,'public.feature_flags',op]);
    assert.equal(r.rows[0].allowed,false);
  }
}
assert.equal((await db.query("select relrowsecurity from pg_class where relname='feature_flags'")).rows[0].relrowsecurity,true);
assert.equal((await db.query("select enabled from feature_flags where key='runner_enabled'")).rows[0].enabled,false);
await db.exec("set role service_role; update feature_flags set enabled=true where key='runner_enabled'; reset role;");
await db.exec(sql);
assert.equal((await db.query("select enabled from feature_flags where key='runner_enabled'")).rows[0].enabled,true);
assert.equal((await db.query("select enabled from feature_flags where key='payments_enabled'")).rows[0].enabled,true);
await db.close();
console.log('Runner flag: default off, RLS, denied anonymous reads/writes, service toggle, repeat-safe seed PASS');
