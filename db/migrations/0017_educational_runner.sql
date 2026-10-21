-- TEST ONLY. Apply manually after verifying the project reference.
-- Independent sidecars: never rewrite quizzes.quiz_data for game configuration.
begin;
create table public.runner_settings (
 quiz_id uuid primary key references public.quizzes(id) on delete cascade,
 lives integer not null default 1 check(lives between 1 and 5),
 uniform_seconds integer check(uniform_seconds between 10 and 300),
 manual_times jsonb not null default '{}'::jsonb,
 ai_times jsonb not null default '{}'::jsonb,
 ai_status text not null default 'new' check(ai_status in ('new','running','done','failed')),
 ai_started_at timestamptz,
 version integer not null default 0,
 updated_by bigint,
 updated_at timestamptz not null default now()
);
create table public.runner_images (
 quiz_id uuid references public.quizzes(id) on delete cascade,
 question_key text not null,
 part text not null,
 game_image_url text not null,
 primary key(quiz_id,question_key,part)
);
create table public.runner_rounds (
 id uuid primary key default gen_random_uuid(),
 user_id bigint not null references public.users(user_id) on delete cascade,
 quiz_id uuid not null references public.quizzes(id) on delete cascade,
 questions jsonb not null,
 cursor integer not null default 0,
 lives integer not null check(lives between 0 and 5),
 score integer not null default 0,
 status text not null default 'running' check(status in ('running','caught','completed','abandoned')),
 issued_at double precision,
 version integer not null default 0,
 last_response jsonb,
 created_at timestamptz not null default now(),
 expires_at timestamptz not null default now() + interval '2 hours',
 finished_at timestamptz
);
create index runner_rounds_user_recent on public.runner_rounds(user_id,created_at desc);
create index runner_rounds_expiry on public.runner_rounds(expires_at) where status='running';
alter table public.runner_settings enable row level security;
alter table public.runner_images enable row level security;
alter table public.runner_rounds enable row level security;
revoke all on public.runner_settings, public.runner_images, public.runner_rounds from public, anon, authenticated;
grant select,insert,update,delete on public.runner_settings, public.runner_images, public.runner_rounds to service_role;
-- Telegram auth and ownership checks live in the server. No browser DB access.
commit;
