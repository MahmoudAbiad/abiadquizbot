-- TEST ONLY: Telegram authentication is enforced by runner_api; no browser DB access.
begin;
create table public.runner_wallets (
 user_id bigint primary key references public.users(user_id) on delete cascade,
 coins bigint not null default 0 check(coins >= 0),
 jetpack_owned boolean not null default false,
 version bigint not null default 0,
 updated_at timestamptz not null default now()
);
-- Round cleanup cascades tracks, but never the player's wallet or purchases.
create table public.runner_tracks (
 round_id uuid not null references public.runner_rounds(id) on delete cascade,
 segment integer not null check(segment >= 0),
 objects jsonb not null check(jsonb_typeof(objects) = 'array' and jsonb_array_length(objects) <= 11),
 started_at timestamptz not null default clock_timestamp(),
 claimed integer[] not null default '{}',
 primary key(round_id,segment)
);
alter table public.runner_wallets enable row level security;
alter table public.runner_tracks enable row level security;
revoke all on public.runner_wallets, public.runner_tracks from public, anon, authenticated;
grant select, insert, update, delete on public.runner_wallets, public.runner_tracks to service_role;

create function public.runner_buy_jetpack(p_user bigint) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare w public.runner_wallets; outcome text;
begin
 insert into public.runner_wallets(user_id) values(p_user) on conflict do nothing;
 select * into w from public.runner_wallets where user_id=p_user for update;
 if w.jetpack_owned then outcome := 'owned';
 elsif w.coins < 100 then outcome := 'insufficient';
 else
  update public.runner_wallets set coins=coins-100, jetpack_owned=true,
   version=version+1, updated_at=clock_timestamp() where user_id=p_user returning * into w;
  outcome := 'purchased';
 end if;
 return jsonb_build_object('coins',w.coins,'jetpack_owned',w.jetpack_owned,'version',w.version,'purchase_status',outcome);
end $$;

create function public.runner_begin_track(p_user bigint,p_round uuid,p_segment integer,p_objects jsonb) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare r public.runner_rounds; t public.runner_tracks;
begin
 select * into r from public.runner_rounds where id=p_round and user_id=p_user for update;
 if not found or r.status <> 'running' or r.cursor <> p_segment or r.issued_at is not null or r.expires_at < clock_timestamp() then
  return jsonb_build_object('error','stale_track');
 end if;
 insert into public.runner_tracks(round_id,segment,objects) values(p_round,p_segment,p_objects) on conflict do nothing;
 select * into t from public.runner_tracks where round_id=p_round and segment=p_segment;
 return jsonb_build_object('segment',t.segment,'objects',t.objects,'claimed',t.claimed);
end $$;

create function public.runner_claim_coins(p_user bigint,p_round uuid,p_segment integer,p_ids integer[]) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare t public.runner_tracks; w public.runner_wallets; earned integer[]; elapsed double precision;
begin
 -- Lock the round as well: cleanup/quiz deletion cannot erase the claim receipt mid-credit.
 perform 1 from public.runner_rounds where id=p_round and user_id=p_user and expires_at > clock_timestamp() for update;
 if not found then return jsonb_build_object('error','round_expired'); end if;
 select * into t from public.runner_tracks where round_id=p_round and segment=p_segment for update;
 if not found then return jsonb_build_object('error','stale_track'); end if;
 elapsed := extract(epoch from clock_timestamp()-t.started_at);
 -- Count only server-issued coin IDs that had enough real time to reach the player.
 -- The client submits identifiers, never a balance or an amount to credit.
 select coalesce(array_agg(distinct (o->>'id')::integer),'{}'::integer[]) into earned
 from jsonb_array_elements(t.objects) o
 where o->>'kind'='coin' and (o->>'id')::integer=any(p_ids)
  and not ((o->>'id')::integer=any(t.claimed))
  and (o->>'at')::double precision + 0.77/0.31 <= elapsed + 0.05;
 insert into public.runner_wallets(user_id) values(p_user) on conflict do nothing;
 if cardinality(earned)>0 then
  update public.runner_tracks set claimed=claimed||earned where round_id=p_round and segment=p_segment;
  update public.runner_wallets set coins=coins+cardinality(earned),version=version+1,updated_at=clock_timestamp() where user_id=p_user;
 end if;
 select * into w from public.runner_wallets where user_id=p_user;
 return jsonb_build_object('coins',w.coins,'jetpack_owned',w.jetpack_owned,'version',w.version,
  'accepted',(select claimed from public.runner_tracks where round_id=p_round and segment=p_segment));
end $$;

revoke all on function public.runner_buy_jetpack(bigint), public.runner_begin_track(bigint,uuid,integer,jsonb), public.runner_claim_coins(bigint,uuid,integer,integer[]) from public,anon,authenticated;
grant execute on function public.runner_buy_jetpack(bigint), public.runner_begin_track(bigint,uuid,integer,jsonb), public.runner_claim_coins(bigint,uuid,integer,integer[]) to service_role;
commit;
