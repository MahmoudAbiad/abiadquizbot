-- TEST ONLY. All RPC calls originate in the Telegram-authenticated Runner API.
begin;
alter table public.runner_wallets
 add column knowledge bigint not null default 0 check(knowledge >= 0),
 add column knowledge_earned bigint not null default 0 check(knowledge_earned >= knowledge),
 add column car_level integer not null default 0 check(car_level between 0 and 3),
 add column equipped text not null default 'none' check(equipped in ('none','jetpack','tesla'));
update public.runner_wallets set equipped='jetpack' where jetpack_owned;
alter table public.runner_tracks
 add column equipment text not null default 'none',
 add column car_level integer not null default 0;
create table public.runner_mastery (
 user_id bigint not null references public.users(user_id) on delete cascade,
 question_key text not null check(length(question_key)=64),
 mastered_at timestamptz not null default clock_timestamp(),
 primary key(user_id,question_key)
);
create table public.runner_daily_study (
 user_id bigint not null references public.users(user_id) on delete cascade,
 study_day date not null,
 question_key text not null check(length(question_key)=64),
 primary key(user_id,study_day,question_key)
);
alter table public.runner_mastery enable row level security;
alter table public.runner_daily_study enable row level security;
revoke all on public.runner_mastery,public.runner_daily_study from public,anon,authenticated;
grant select,insert,update,delete on public.runner_mastery,public.runner_daily_study to service_role;

create function public.runner_wallet_state(p_user bigint) returns jsonb
language sql stable security invoker set search_path = '' as $$
 select coalesce((select to_jsonb(w)-'user_id'-'updated_at' from public.runner_wallets w where user_id=p_user),
  '{"coins":0,"jetpack_owned":false,"version":0,"knowledge":0,"knowledge_earned":0,"car_level":0,"equipped":"none"}'::jsonb)
  || jsonb_build_object('daily',jsonb_build_object('day',(now() at time zone 'Asia/Damascus')::date,
   'answered',least(5,(select count(*) from public.runner_daily_study where user_id=p_user and study_day=(now() at time zone 'Asia/Damascus')::date)), 'target',5));
$$;

create function public.runner_buy_car(p_user bigint,p_target integer) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare w public.runner_wallets; price integer; outcome text;
begin
 insert into public.runner_wallets(user_id) values(p_user) on conflict do nothing;
 select * into w from public.runner_wallets where user_id=p_user for update;
 if p_target not between 1 and 3 then outcome:='invalid_level';
 elsif w.car_level>=p_target then outcome:='owned';
 elsif p_target<>w.car_level+1 then outcome:='invalid_level';
 else
  price:=case p_target when 1 then 1000 when 2 then 500 when 3 then 5000 end;
  if w.knowledge<price then outcome:='insufficient';
  else
   update public.runner_wallets set knowledge=knowledge-price,car_level=p_target,equipped='tesla',
    version=version+1,updated_at=clock_timestamp() where user_id=p_user;
   outcome:='purchased';
  end if;
 end if;
 return public.runner_wallet_state(p_user)||jsonb_build_object('purchase_status',outcome);
end $$;

create function public.runner_equip(p_user bigint,p_item text) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare w public.runner_wallets;
begin
 insert into public.runner_wallets(user_id) values(p_user) on conflict do nothing;
 select * into w from public.runner_wallets where user_id=p_user for update;
 if p_item not in ('none','jetpack','tesla') or p_item is null
  or (p_item='jetpack' and not w.jetpack_owned) or (p_item='tesla' and w.car_level=0) then
  return public.runner_wallet_state(p_user)||jsonb_build_object('error','not_owned');
 end if;
 if w.equipped<>p_item then
  update public.runner_wallets set equipped=p_item,version=version+1,updated_at=clock_timestamp() where user_id=p_user;
 end if;
 return public.runner_wallet_state(p_user);
end $$;

-- Commit the answer, daily progress, mastery receipt and spendable reward atomically.
-- The Python server computes the trusted answer transition; no browser-supplied score.
create function public.runner_commit_answer(p_user bigint,p_round uuid,p_quiz uuid,p_version integer,p_key text,p_update jsonb,p_response jsonb) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare r public.runner_rounds; reward integer:=0; inserted integer; response jsonb;
begin
 select * into r from public.runner_rounds where id=p_round and user_id=p_user and quiz_id=p_quiz for update;
 if not found or r.expires_at<clock_timestamp() then return jsonb_build_object('error','round_expired'); end if;
 if r.version=p_version+1 and r.last_response is not null then return r.last_response; end if;
 if r.version<>p_version or r.status<>'running' then return jsonb_build_object('error','stale_version'); end if;
 if length(p_key)<>64 then raise exception 'Invalid mastery key'; end if;
 insert into public.runner_wallets(user_id) values(p_user) on conflict do nothing;
 -- One wallet lock order for every reward, coin claim, purchase and equipment change.
 perform 1 from public.runner_wallets where user_id=p_user for update;
 if (p_response->>'attempted')::boolean then
  insert into public.runner_daily_study(user_id,study_day,question_key)
   values(p_user,(now() at time zone 'Asia/Damascus')::date,p_key) on conflict do nothing;
 end if;
 if (p_response->>'correct')::boolean then
  insert into public.runner_mastery(user_id,question_key) values(p_user,p_key) on conflict do nothing;
  get diagnostics inserted=row_count;
  if inserted=1 then reward:=100; end if;
 end if;
 update public.runner_wallets set knowledge=knowledge+reward,knowledge_earned=knowledge_earned+reward,
  version=version+1,updated_at=clock_timestamp() where user_id=p_user;
 response:=p_response||jsonb_build_object('version',p_version+1,'knowledge_awarded',reward,'wallet',public.runner_wallet_state(p_user));
 update public.runner_rounds set lives=(p_update->>'lives')::integer,cursor=(p_update->>'cursor')::integer,
  status=p_update->>'status',score=(p_update->>'score')::integer,issued_at=null,version=p_version+1,
  last_response=response,finished_at=case when p_update->>'status'<>'running' then clock_timestamp() else finished_at end
  where id=p_round;
 return response;
end $$;

revoke all on function public.runner_wallet_state(bigint),public.runner_buy_car(bigint,integer),public.runner_equip(bigint,text),public.runner_commit_answer(bigint,uuid,uuid,integer,text,jsonb,jsonb) from public,anon,authenticated;
grant execute on function public.runner_wallet_state(bigint),public.runner_buy_car(bigint,integer),public.runner_equip(bigint,text),public.runner_commit_answer(bigint,uuid,uuid,integer,text,jsonb,jsonb) to service_role;
-- Existing coin and jetpack RPCs are extended below without changing their contracts.

create or replace function public.runner_buy_jetpack(p_user bigint) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare w public.runner_wallets; outcome text;
begin
 insert into public.runner_wallets(user_id) values(p_user) on conflict do nothing;
 select * into w from public.runner_wallets where user_id=p_user for update;
 if w.jetpack_owned then outcome := 'owned';
 elsif w.coins < 100 then outcome := 'insufficient';
 else
  update public.runner_wallets set coins=coins-100, jetpack_owned=true,equipped='jetpack',
   version=version+1, updated_at=clock_timestamp() where user_id=p_user returning * into w;
  outcome := 'purchased';
 end if;
 return public.runner_wallet_state(p_user)||jsonb_build_object('purchase_status',outcome);
end $$;


create or replace function public.runner_begin_track(p_user bigint,p_round uuid,p_segment integer,p_objects jsonb) returns jsonb
language plpgsql security invoker set search_path = '' as $$
declare r public.runner_rounds; t public.runner_tracks;
begin
 select * into r from public.runner_rounds where id=p_round and user_id=p_user for update;
 if not found or r.status <> 'running' or r.cursor <> p_segment or r.issued_at is not null or r.expires_at < clock_timestamp() then
  return jsonb_build_object('error','stale_track');
 end if;
 insert into public.runner_tracks(round_id,segment,objects,equipment,car_level) values(p_round,p_segment,p_objects,coalesce((select equipped from public.runner_wallets where user_id=p_user),'none'),coalesce((select car_level from public.runner_wallets where user_id=p_user),0)) on conflict do nothing;
 select * into t from public.runner_tracks where round_id=p_round and segment=p_segment;
 return jsonb_build_object('segment',t.segment,'objects',t.objects,'claimed',t.claimed,'equipment',t.equipment,'car_level',t.car_level);
end $$;


create or replace function public.runner_claim_coins(p_user bigint,p_round uuid,p_segment integer,p_ids integer[]) returns jsonb
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
 return public.runner_wallet_state(p_user)||jsonb_build_object('accepted',(select claimed from public.runner_tracks where round_id=p_round and segment=p_segment));
end $$;


commit;
