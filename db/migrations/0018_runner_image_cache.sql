-- TEST ONLY: verify RUNNER_TEST_SUPABASE_REF before applying.
begin;
alter table public.runner_images add column if not exists created_at timestamptz not null default now();
alter table public.runner_images add column if not exists expires_at timestamptz not null default now() + interval '7 days';
create index if not exists runner_images_expiry on public.runner_images(expires_at);
create index if not exists runner_rounds_retention on public.runner_rounds(expires_at);
create index if not exists runner_rounds_finished_retention on public.runner_rounds(finished_at) where finished_at is not null;
create index if not exists runner_rounds_quiz on public.runner_rounds(quiz_id);

-- Durable tombstones survive FK cascades and Storage outages. No browser grants.
create table if not exists public.runner_image_gc (
 quiz_id uuid primary key,
 created_at timestamptz not null default now()
);
create index if not exists runner_image_gc_created on public.runner_image_gc(created_at);
alter table public.runner_image_gc enable row level security;
revoke all on public.runner_image_gc from public, anon, authenticated;
grant select, insert, update, delete on public.runner_image_gc to service_role;
create or replace function public.runner_queue_deleted_images()
returns trigger language plpgsql security invoker set search_path = '' as $$
begin
 insert into public.runner_image_gc(quiz_id) values(old.id) on conflict(quiz_id) do nothing;
 return old;
end;
$$;
revoke all on function public.runner_queue_deleted_images() from public, anon, authenticated;
drop trigger if exists runner_queue_deleted_images on public.quizzes;
create trigger runner_queue_deleted_images after delete on public.quizzes
for each row execute function public.runner_queue_deleted_images();
commit;
