-- REVIEW ONLY: not applied to any remote database; not an automatic migration.
-- Verify database identity and all existing feature_flags consumers first.
begin;
alter table public.feature_flags enable row level security;
revoke all on public.feature_flags from public, anon, authenticated;
grant select, insert, update, delete on public.feature_flags to service_role;
insert into public.feature_flags(key, enabled) values ('runner_enabled', false)
on conflict (key) do nothing;
commit;
