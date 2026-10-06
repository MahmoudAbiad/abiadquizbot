-- Phase 3B. Local artifact only: NOT applied to any database. Run after 0008 + 0009.
-- Live (real-time) matches: matchmaking queue + shared-question match + settlement.
-- Backward compatible: only ADD columns/tables/indexes, widen one CHECK, and re-create two async RPCs
-- with a `kind='async_attack'` guard so live matches can never be driven by async functions.
--
-- Lock order used by EVERY new function (deterministic, deadlock-free):
--   1) challenge_profiles rows, ascending user_id, FOR NO KEY UPDATE
--      (NO KEY UPDATE does not conflict with the key-share locks FK checks take on profiles)
--   2) challenge_live_queue rows, ascending user_id, FOR UPDATE
--   3) content rows FOR SHARE in order test -> unit -> subject -> branch (same as 0009)
--   4) the challenge_matches row
BEGIN;

-- ---------- 1) schema ----------
ALTER TABLE public.challenge_matches DROP CONSTRAINT IF EXISTS challenge_matches_status_check;
ALTER TABLE public.challenge_matches ADD CONSTRAINT challenge_matches_status_check
  CHECK(status IN('created','in_progress','completed','expired','abandoned','cancelled'));
-- live: status 'created' = countdown (until countdown_until); players are stored in canonical order
-- attacker_id < defender_id (roles are 'player' in challenge_match_players).
ALTER TABLE public.challenge_matches ADD COLUMN IF NOT EXISTS countdown_until timestamptz;
ALTER TABLE public.challenge_match_players ADD COLUMN IF NOT EXISTS message_id bigint;

CREATE TABLE IF NOT EXISTS public.challenge_live_queue (
 user_id bigint PRIMARY KEY REFERENCES public.challenge_profiles(user_id),
 status text NOT NULL CHECK(status IN('waiting','matched','cancelled','expired','done')),
 branch_id uuid NOT NULL REFERENCES public.challenge_branches(id),
 subject_id uuid NOT NULL REFERENCES public.challenge_subjects(id),
 unit_id uuid NOT NULL REFERENCES public.challenge_units(id),
 trophies integer NOT NULL,
 request_key text NOT NULL,
 match_id uuid REFERENCES public.challenge_matches(id),
 joined_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK((status IN('matched','done'))=(match_id IS NOT NULL))
);
-- One row per user = a user can never hold two queue entries / two live matches.
-- Pool lookup: same unit, nearest trophies, bounded range scan on waiting rows only.
CREATE INDEX IF NOT EXISTS challenge_live_queue_pool ON public.challenge_live_queue(unit_id,trophies,user_id) WHERE status='waiting';
CREATE INDEX IF NOT EXISTS challenge_live_queue_expiry ON public.challenge_live_queue(expires_at) WHERE status='waiting';
-- Defense in depth: a user cannot sit in the defender slot of two active live matches
-- (attacker slot is already covered by challenge_one_active_attack from 0009).
CREATE UNIQUE INDEX IF NOT EXISTS challenge_one_active_live_defender ON public.challenge_matches(defender_id)
 WHERE kind='live' AND status IN('created','in_progress');
CREATE INDEX IF NOT EXISTS challenge_live_countdown_due ON public.challenge_matches(countdown_until)
 WHERE kind='live' AND status='created';
-- Daily reward caps + content-repeat check read a player's recent matches.
CREATE INDEX IF NOT EXISTS challenge_match_players_user ON public.challenge_match_players(user_id,match_id);

-- ---------- 2) async guards (live matches are invisible to async RPCs) ----------
-- Identical to 0009 except for the added `AND kind='async_attack'` filter (the only change).
-- Rollback of 0010 intentionally leaves these guarded versions in place (they are compatible).
CREATE OR REPLACE FUNCTION public.challenge_attack_state(p_match uuid,p_user bigint)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE m challenge_matches; q jsonb;
BEGIN
 SELECT * INTO m FROM challenge_matches WHERE id=p_match AND attacker_id=p_user AND kind='async_attack';
 IF NOT FOUND THEN RAISE EXCEPTION 'not_your_attack'; END IF;
 IF m.status='in_progress' THEN q:=m.snapshot->m.question_index; END IF;
 RETURN jsonb_build_object('id',m.id,'attacker_id',m.attacker_id,'status',m.status,
 'index',m.question_index,'total',jsonb_array_length(m.snapshot),'labels',m.labels,
 'question',CASE WHEN q IS NULL THEN NULL ELSE jsonb_build_object('text',q->>'text','options',q->'options',
 'image_url',q->>'image_url') END,'deadline',m.question_deadline,'server_now',clock_timestamp(),
 'result',m.result,'message_id',m.telegram_message_id);
END $$;

CREATE OR REPLACE FUNCTION public.challenge_advance_attack(p_match uuid,p_user bigint,
 p_action text DEFAULT 'resume',p_index integer DEFAULT NULL,p_option integer DEFAULT NULL,p_update text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE m challenge_matches; target bigint; stamp timestamptz; q jsonb; expired boolean; correct boolean;
 ms integer; pts integer; n integer;
BEGIN
 SELECT defender_id INTO target FROM challenge_matches WHERE id=p_match AND attacker_id=p_user AND kind='async_attack';
 IF NOT FOUND THEN RAISE EXCEPTION 'not_your_attack'; END IF;
 PERFORM user_id FROM challenge_profiles WHERE user_id IN(p_user,target) ORDER BY user_id FOR UPDATE;
 SELECT * INTO m FROM challenge_matches WHERE id=p_match FOR UPDATE;
 stamp:=clock_timestamp(); -- clock AFTER waiting for locks, never the client's timestamp.
 IF p_action<>'tick' THEN UPDATE challenge_profiles SET last_seen=stamp WHERE user_id=p_user; END IF;
 IF m.status<>'in_progress' THEN RETURN challenge_attack_state(p_match,p_user); END IF;
 IF p_action NOT IN('answer','resume','tick','abandon') THEN RAISE EXCEPTION 'invalid_action'; END IF;
 -- Retry of an accepted Telegram callback never consumes the next question.
 IF p_update IS NOT NULL AND EXISTS(SELECT 1 FROM challenge_match_answers WHERE user_id=p_user AND update_key=p_update) THEN
 RETURN challenge_attack_state(p_match,p_user); END IF;
 IF stamp>=m.server_deadline THEN
 PERFORM challenge_finish_attack(m.id,'expired'); RETURN challenge_attack_state(p_match,p_user); END IF;
 IF p_action='abandon' THEN
 PERFORM challenge_finish_attack(m.id,'abandoned'); RETURN challenge_attack_state(p_match,p_user); END IF;
 IF p_action='answer' AND p_index IS DISTINCT FROM m.question_index THEN
 RETURN challenge_attack_state(p_match,p_user); END IF;
 expired:=stamp>=m.question_deadline;
 IF p_action IN('tick','resume') AND NOT expired THEN RETURN challenge_attack_state(p_match,p_user); END IF;
 q:=m.snapshot->m.question_index;
 IF p_action='answer' AND (p_option IS NULL OR p_option<0 OR p_option>=jsonb_array_length(q->'options')) THEN
 RAISE EXCEPTION 'invalid_option'; END IF;
 correct:=NOT expired AND p_action='answer' AND p_option=(q->>'correct_option_id')::integer;
 ms:=least((m.rules->>'question_seconds')::integer*1000,
 greatest(0,floor(extract(epoch FROM stamp-m.question_opened_at)*1000)::integer));
 pts:=challenge_question_score(correct,ms::numeric/1000,m.rules);
 INSERT INTO challenge_match_answers(match_id,user_id,question_index,question_id,option_id,is_correct,score,elapsed_ms,timed_out,update_key)
 VALUES(m.id,p_user,m.question_index,(q->>'id')::uuid,CASE WHEN expired THEN NULL ELSE p_option END,
 correct,pts,ms,expired,p_update);
 INSERT INTO challenge_events(match_id,event_type,question_index)
 VALUES(m.id,CASE WHEN expired THEN 'question_timed_out' ELSE 'answer_received' END,m.question_index);
 n:=m.question_index+1;
 UPDATE challenge_matches SET question_index=n,question_opened_at=stamp,
 question_deadline=least(m.server_deadline,stamp+make_interval(secs=>(m.rules->>'question_seconds')::integer)) WHERE id=m.id;
 IF n>=jsonb_array_length(m.snapshot) THEN PERFORM challenge_finish_attack(m.id,'completed');
 ELSE INSERT INTO challenge_events(match_id,event_type,question_index) VALUES(m.id,'question_opened',n); END IF;
 RETURN challenge_attack_state(p_match,p_user);
END $$;

-- An async attack cannot start while the attacker is searching / inside a live match.
CREATE OR REPLACE FUNCTION public.challenge_block_async_while_live() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
BEGIN
 IF NEW.kind='async_attack' AND EXISTS(SELECT 1 FROM challenge_live_queue WHERE user_id=NEW.attacker_id AND status IN('waiting','matched')) THEN
  RAISE EXCEPTION 'in_live_queue'; END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS challenge_block_async_while_live ON public.challenge_matches;
CREATE TRIGGER challenge_block_async_while_live BEFORE INSERT ON public.challenge_matches
 FOR EACH ROW EXECUTE FUNCTION public.challenge_block_async_while_live();

-- ---------- 3) helpers ----------
CREATE OR REPLACE FUNCTION public.challenge_live_range(p_waited numeric,p_rules jsonb)
RETURNS integer LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=public AS $$
 SELECT least((p_rules->>'matchmaking_range')::integer,
  (p_rules->>'live_range_initial')::integer+(p_rules->>'live_range_step')::integer*
  floor(greatest(p_waited,0)/(p_rules->>'live_range_step_seconds')::numeric)::integer)
$$;

CREATE OR REPLACE FUNCTION public.challenge_live_queue_state(p_user bigint)
RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER SET search_path=public AS $$
 SELECT coalesce((SELECT jsonb_build_object('status',q.status,'unit_id',q.unit_id,'match_id',q.match_id,
   'joined_at',q.joined_at,'expires_at',q.expires_at,'waited_seconds',
   floor(extract(epoch FROM clock_timestamp()-q.joined_at))::integer,'server_now',clock_timestamp())
  FROM challenge_live_queue q WHERE q.user_id=p_user),jsonb_build_object('status','idle','server_now',clock_timestamp()))
$$;

-- Player-facing view of a live match. Never returns the snapshot. Reveals the correct option and both
-- players' picks ONLY for already-resolved questions; the open question exposes text/options only,
-- plus a boolean saying whether the opponent has answered (never what).
CREATE OR REPLACE FUNCTION public.challenge_live_state(p_match uuid,p_user bigint)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE m challenge_matches; opp bigint; n integer; q jsonb; prev jsonb; pa record; pb record;
 my_name text; opp_name text; my_score integer; opp_score integer; opp_done boolean; my_done boolean; my_opt integer;
BEGIN
 SELECT * INTO m FROM challenge_matches WHERE id=p_match AND kind='live' AND p_user IN(attacker_id,defender_id);
 IF NOT FOUND THEN RAISE EXCEPTION 'not_your_match'; END IF;
 opp:=CASE WHEN m.attacker_id=p_user THEN m.defender_id ELSE m.attacker_id END;
 n:=jsonb_array_length(m.snapshot);
 IF m.status='in_progress' AND m.question_index<n THEN q:=m.snapshot->m.question_index; END IF;
 my_name:=CASE WHEN m.attacker_id=p_user THEN m.labels->'p1'->>'name' ELSE m.labels->'p2'->>'name' END;
 opp_name:=CASE WHEN m.attacker_id=p_user THEN m.labels->'p2'->>'name' ELSE m.labels->'p1'->>'name' END;
 SELECT coalesce(sum(score),0)::integer INTO my_score FROM challenge_match_answers
  WHERE match_id=m.id AND user_id=p_user AND question_index<m.question_index;
 SELECT coalesce(sum(score),0)::integer INTO opp_score FROM challenge_match_answers
  WHERE match_id=m.id AND user_id=opp AND question_index<m.question_index;
 SELECT option_id,true INTO my_opt,my_done FROM challenge_match_answers
  WHERE match_id=m.id AND user_id=p_user AND question_index=m.question_index;
 my_done:=coalesce(my_done,false);
 opp_done:=EXISTS(SELECT 1 FROM challenge_match_answers WHERE match_id=m.id AND user_id=opp AND question_index=m.question_index);
 IF m.question_index>0 AND m.question_index<=n THEN
  SELECT option_id,is_correct,score,timed_out INTO pa FROM challenge_match_answers
   WHERE match_id=m.id AND user_id=p_user AND question_index=m.question_index-1;
  SELECT option_id,is_correct,score,timed_out INTO pb FROM challenge_match_answers
   WHERE match_id=m.id AND user_id=opp AND question_index=m.question_index-1;
  prev:=jsonb_build_object('index',m.question_index-1,
   'correct_option_id',(m.snapshot->(m.question_index-1)->>'correct_option_id')::integer,
   'mine',jsonb_build_object('option_id',pa.option_id,'is_correct',pa.is_correct,'score',pa.score,'timed_out',pa.timed_out),
   'opponent',jsonb_build_object('option_id',pb.option_id,'is_correct',pb.is_correct,'score',pb.score,'timed_out',pb.timed_out));
 END IF;
 RETURN jsonb_build_object('id',m.id,'kind','live','status',m.status,'index',m.question_index,'total',n,
  'labels',jsonb_build_object('subject',m.labels->>'subject','unit',m.labels->>'unit','test',m.labels->>'test'),
  'me',jsonb_build_object('id',p_user,'name',my_name,'score',my_score,'answered',my_done,'option_id',my_opt),
  'opponent',jsonb_build_object('id',opp,'name',opp_name,'score',opp_score,'answered',opp_done),
  'question',CASE WHEN q IS NULL THEN NULL ELSE jsonb_build_object('text',q->>'text','options',q->'options','image_url',q->>'image_url') END,
  'deadline',CASE WHEN m.status IN('created','in_progress') THEN m.question_deadline END,
  'countdown_until',m.countdown_until,'server_now',clock_timestamp(),'previous',prev,'result',m.result);
END $$;

-- ---------- 4) queue: join / cancel / expire ----------
CREATE OR REPLACE FUNCTION public.challenge_live_join(p_user bigint,p_unit uuid,p_request text,p_rules jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE a challenge_profiles; q challenge_live_queue; u challenge_units; s challenge_subjects; stamp timestamptz;
BEGIN
 IF p_request IS NULL OR length(p_request)=0 OR length(p_request)>160 THEN RAISE EXCEPTION 'invalid_request'; END IF;
 SELECT * INTO a FROM challenge_profiles WHERE user_id=p_user FOR NO KEY UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'profile_missing'; END IF;
 SELECT * INTO q FROM challenge_live_queue WHERE user_id=p_user FOR UPDATE;
 -- Retry / duplicate update of the SAME request: report current state, never re-queue
 -- (a retried join after cancel/expiry/finish must not resurrect a zombie search).
 IF FOUND AND q.request_key=p_request THEN RETURN challenge_live_queue_state(p_user); END IF;
 IF NOT EXISTS(SELECT 1 FROM feature_flags WHERE key='challenge_system_enabled' AND enabled)
  OR NOT EXISTS(SELECT 1 FROM feature_flags WHERE key='live_matches_enabled' AND enabled) THEN
  RAISE EXCEPTION 'game_disabled'; END IF;
 IF q.user_id IS NOT NULL THEN
  IF q.status='waiting' THEN RAISE EXCEPTION 'already_queued'; END IF;
  IF q.status='matched' AND EXISTS(SELECT 1 FROM challenge_matches WHERE id=q.match_id AND status IN('created','in_progress')) THEN
   RAISE EXCEPTION 'in_live_match'; END IF;
 END IF;
 IF EXISTS(SELECT 1 FROM challenge_matches WHERE attacker_id=p_user AND status IN('created','in_progress')) THEN
  RAISE EXCEPTION 'active_attack'; END IF;
 SELECT * INTO u FROM challenge_units WHERE id=p_unit AND status='active';
 SELECT * INTO s FROM challenge_subjects WHERE id=u.subject_id AND status='active';
 IF u.id IS NULL OR s.id IS NULL OR NOT EXISTS(SELECT 1 FROM challenge_playable_units WHERE unit_id=p_unit) THEN
  RAISE EXCEPTION 'no_content'; END IF;
 IF a.branch_id IS DISTINCT FROM s.branch_id THEN RAISE EXCEPTION 'branch_mismatch'; END IF;
 stamp:=clock_timestamp();
 INSERT INTO challenge_live_queue(user_id,status,branch_id,subject_id,unit_id,trophies,request_key,match_id,joined_at,expires_at,updated_at)
 VALUES(p_user,'waiting',s.branch_id,s.id,p_unit,a.trophies,p_request,NULL,stamp,
  stamp+make_interval(secs=>(p_rules->>'live_queue_seconds')::integer),stamp)
 ON CONFLICT(user_id) DO UPDATE SET status='waiting',branch_id=excluded.branch_id,subject_id=excluded.subject_id,
  unit_id=excluded.unit_id,trophies=excluded.trophies,request_key=excluded.request_key,match_id=NULL,
  joined_at=excluded.joined_at,expires_at=excluded.expires_at,updated_at=excluded.updated_at;
 UPDATE challenge_profiles SET last_seen=stamp WHERE user_id=p_user;
 RETURN challenge_live_queue_state(p_user);
END $$;

-- Idempotent. Cancels ONLY a waiting search (and, when p_request is given, only that search, so a replayed
-- old cancel cannot kill a newer search). A matched/started match is never cancelled here.
CREATE OR REPLACE FUNCTION public.challenge_live_cancel(p_user bigint,p_request text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE q challenge_live_queue;
BEGIN
 PERFORM user_id FROM challenge_profiles WHERE user_id=p_user FOR NO KEY UPDATE;
 SELECT * INTO q FROM challenge_live_queue WHERE user_id=p_user FOR UPDATE;
 IF NOT FOUND THEN RETURN challenge_live_queue_state(p_user); END IF;
 IF q.status='waiting' AND (p_request IS NULL OR q.request_key=p_request) THEN
  UPDATE challenge_live_queue SET status='cancelled',updated_at=clock_timestamp() WHERE user_id=p_user;
 END IF;
 RETURN challenge_live_queue_state(p_user);
END $$;

-- Worker-safe: single statement, SKIP LOCKED, only queue rows are touched (no lock-order hazard).
CREATE OR REPLACE FUNCTION public.challenge_live_expire_queue(p_limit integer DEFAULT 100)
RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path=public AS $$
 WITH due AS (SELECT user_id FROM challenge_live_queue WHERE status='waiting' AND expires_at<=clock_timestamp()
   ORDER BY expires_at LIMIT least(greatest(p_limit,1),500) FOR UPDATE SKIP LOCKED),
 gone AS (UPDATE challenge_live_queue q SET status='expired',updated_at=clock_timestamp() FROM due
   WHERE q.user_id=due.user_id RETURNING q.user_id)
 SELECT coalesce(jsonb_agg(user_id),'[]'::jsonb) FROM gone
$$;

-- ---------- 5) matchmaking ----------
-- One attempt: peek (lock-free) at the closest compatible waiting player, then lock the PAIR in ascending
-- user_id order, re-validate everything under the locks, and create exactly one match.
-- Result status: idle | waiting (retry=true if a candidate was lost to a race) | matched | created.
CREATE OR REPLACE FUNCTION public.challenge_live_try_match(p_user bigint,p_rules jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE me challenge_live_queue; c challenge_live_queue; cid bigint; stamp timestamptz; maxr integer;
 lo bigint; hi bigint; tid uuid; t challenge_tests; u challenge_units; s challenge_subjects; b challenge_branches;
 mid uuid; qs jsonb; n integer; lo_name text; hi_name text; a_busy boolean;
BEGIN
 stamp:=clock_timestamp(); maxr:=(p_rules->>'matchmaking_range')::integer;
 SELECT * INTO me FROM challenge_live_queue WHERE user_id=p_user;
 IF NOT FOUND THEN RETURN challenge_live_queue_state(p_user); END IF;
 IF me.status<>'waiting' THEN RETURN challenge_live_queue_state(p_user); END IF;
 -- Phase A: bounded index range scan (same unit, trophies within max range), nearest first. No random sort.
 SELECT x.user_id INTO cid FROM challenge_live_queue x
  WHERE x.status='waiting' AND x.unit_id=me.unit_id AND x.user_id<>p_user AND x.expires_at>stamp
   AND x.trophies BETWEEN me.trophies-maxr AND me.trophies+maxr
   AND abs(x.trophies-me.trophies)<=greatest(
     challenge_live_range(extract(epoch FROM stamp-me.joined_at),p_rules),
     challenge_live_range(extract(epoch FROM stamp-x.joined_at),p_rules))
  ORDER BY abs(x.trophies-me.trophies),x.joined_at,x.user_id LIMIT 1;
 IF cid IS NULL THEN RETURN challenge_live_queue_state(p_user); END IF;
 lo:=least(p_user,cid); hi:=greatest(p_user,cid);
 -- Phase B: deterministic lock order (profiles asc, then queue rows asc).
 PERFORM user_id FROM challenge_profiles WHERE user_id IN(lo,hi) ORDER BY user_id FOR NO KEY UPDATE;
 PERFORM user_id FROM challenge_live_queue WHERE user_id IN(lo,hi) ORDER BY user_id FOR UPDATE;
 stamp:=clock_timestamp();
 SELECT * INTO me FROM challenge_live_queue WHERE user_id=p_user;
 SELECT * INTO c FROM challenge_live_queue WHERE user_id=cid;
 IF me.status<>'waiting' THEN RETURN challenge_live_queue_state(p_user); END IF;   -- cancelled/matched meanwhile
 IF c.status<>'waiting' OR c.unit_id<>me.unit_id OR c.expires_at<=stamp OR me.expires_at<=stamp
  OR abs(c.trophies-me.trophies)>greatest(
    challenge_live_range(extract(epoch FROM stamp-me.joined_at),p_rules),
    challenge_live_range(extract(epoch FROM stamp-c.joined_at),p_rules)) THEN
  RETURN challenge_live_queue_state(p_user)||jsonb_build_object('retry',true); END IF;
 -- A player who started an async attack meanwhile stays queued but is not paired (slot index would clash).
 SELECT EXISTS(SELECT 1 FROM challenge_matches WHERE attacker_id IN(lo,hi) AND status IN('created','in_progress'))
  INTO a_busy;
 IF a_busy THEN RETURN challenge_live_queue_state(p_user)||jsonb_build_object('reason','busy'); END IF;
 -- Server-side official test: active only, prefers content neither player saw recently. No AI fallback.
 SELECT t2.id INTO tid FROM challenge_tests t2 WHERE t2.unit_id=me.unit_id AND t2.status='active' AND t2.question_count>0
  ORDER BY EXISTS(SELECT 1 FROM challenge_match_players mp JOIN challenge_matches m2 ON m2.id=mp.match_id
    WHERE mp.user_id IN(lo,hi) AND mp.role IN('attacker','player') AND m2.content_hash=t2.content_hash
     AND m2.created_at>stamp-make_interval(secs=>(p_rules->>'repeat_content_seconds')::integer)) ASC,
   md5(t2.id::text||lo::text||hi::text||stamp::text) LIMIT 1;
 SELECT * INTO t FROM challenge_tests WHERE id=tid AND status='active' FOR SHARE;
 SELECT * INTO u FROM challenge_units WHERE id=t.unit_id AND status='active' FOR SHARE;
 SELECT * INTO s FROM challenge_subjects WHERE id=u.subject_id AND status='active' FOR SHARE;
 SELECT * INTO b FROM challenge_branches WHERE id=s.branch_id AND status='active' FOR SHARE;
 IF t.id IS NULL OR u.id IS NULL OR s.id IS NULL OR b.id IS NULL THEN
  RETURN challenge_live_queue_state(p_user)||jsonb_build_object('reason','no_content'); END IF;
 -- Questions are chosen ONCE here and pinned; both players read the same snapshot, order and deadlines.
 mid:=gen_random_uuid();
 SELECT jsonb_agg(x.q ORDER BY x.question_order) INTO qs FROM (
  SELECT question_order,jsonb_build_object('id',id,'text',question_text,'options',options,
   'correct_option_id',correct_option_id,'image_url',metadata->>'image_url') q
  FROM challenge_questions WHERE test_id=t.id ORDER BY md5(id::text||mid::text)
  LIMIT (p_rules->>'live_questions')::integer) x;
 IF qs IS NULL OR jsonb_array_length(qs)=0 THEN
  RETURN challenge_live_queue_state(p_user)||jsonb_build_object('reason','no_content'); END IF;
 n:=jsonb_array_length(qs);
 SELECT display_name INTO lo_name FROM challenge_profiles WHERE user_id=lo;
 SELECT display_name INTO hi_name FROM challenge_profiles WHERE user_id=hi;
 INSERT INTO challenge_matches(id,kind,status,attacker_id,defender_id,unit_id,subject_id,test_id,content_hash,request_key,
   snapshot,rules,labels,question_index,question_opened_at,question_deadline,server_deadline,created_at,countdown_until,reward_eligible)
 VALUES(mid,'live','created',lo,hi,u.id,s.id,t.id,t.content_hash,'live:'||mid::text,qs,p_rules,
  jsonb_build_object('p1',jsonb_build_object('id',lo,'name',lo_name),'p2',jsonb_build_object('id',hi,'name',hi_name),
   'subject',s.name,'unit',u.name,'test',t.title),
  0,stamp+make_interval(secs=>(p_rules->>'live_countdown_seconds')::integer),
  stamp+make_interval(secs=>((p_rules->>'live_countdown_seconds')::integer+(p_rules->>'question_seconds')::integer)),
  stamp+make_interval(secs=>((p_rules->>'live_countdown_seconds')::integer+n*(p_rules->>'question_seconds')::integer
   +(p_rules->>'live_grace_seconds')::integer)),
  stamp,stamp+make_interval(secs=>(p_rules->>'live_countdown_seconds')::integer),false);
 INSERT INTO challenge_match_players(match_id,user_id,role) VALUES(mid,lo,'player'),(mid,hi,'player');
 UPDATE challenge_live_queue SET status='matched',match_id=mid,updated_at=stamp WHERE user_id IN(lo,hi);
 INSERT INTO challenge_events(match_id,event_type,question_index) VALUES(mid,'match_created',0);
 RETURN challenge_live_queue_state(p_user)||jsonb_build_object('created',true);
END $$;

-- ---------- 6) state machine ----------
-- created(countdown) -> in_progress -> completed | expired | abandoned ; created -> cancelled (leave in countdown)
-- Internal: caller already holds profile locks (asc) and the match row lock. Never answers for anyone.
CREATE OR REPLACE FUNCTION public.challenge_live_apply(p_match uuid,p_user bigint,p_subject uuid,p_unit uuid,
 p_total integer,p_score integer,p_correct integer,p_ms bigint,p_resp integer,p_won boolean,p_lost boolean,
 p_xp integer,p_trophy integer) RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
BEGIN
 UPDATE challenge_profiles SET xp=xp+p_xp,trophies=greatest(0,trophies+p_trophy),challenges=challenges+1,
  wins=wins+p_won::integer,losses=losses+p_lost::integer,correct=correct+p_correct,questions=questions+p_total,
  response_ms=response_ms+p_ms,responses=responses+p_resp,
  win_streak=CASE WHEN p_won THEN win_streak+1 WHEN p_lost THEN 0 ELSE win_streak END WHERE user_id=p_user;
 INSERT INTO challenge_towers(user_id,subject_id,xp,correct,questions) VALUES(p_user,p_subject,p_xp,p_correct,p_total)
 ON CONFLICT(user_id,subject_id) DO UPDATE SET xp=challenge_towers.xp+excluded.xp,
  correct=challenge_towers.correct+excluded.correct,questions=challenge_towers.questions+excluded.questions;
 INSERT INTO challenge_unit_stats(user_id,unit_id,challenges,wins,correct,questions,response_ms,responses)
 VALUES(p_user,p_unit,1,p_won::integer,p_correct,p_total,p_ms,p_resp)
 ON CONFLICT(user_id,unit_id) DO UPDATE SET challenges=challenge_unit_stats.challenges+1,
  wins=challenge_unit_stats.wins+excluded.wins,correct=challenge_unit_stats.correct+excluded.correct,
  questions=challenge_unit_stats.questions+excluded.questions,response_ms=challenge_unit_stats.response_ms+excluded.response_ms,
  responses=challenge_unit_stats.responses+excluded.responses;
 UPDATE challenge_match_players SET score=p_score,xp_delta=p_xp,trophies_delta=p_trophy WHERE match_id=p_match AND user_id=p_user;
END $$;

-- Internal settlement. Idempotent: a match that is no longer created/in_progress is left untouched, so
-- retries / two workers / two simultaneous finishing requests settle exactly once (the match row lock
-- serialises them and the loser of the race sees a terminal status and returns).
CREATE OR REPLACE FUNCTION public.challenge_live_finish(p_match uuid,p_status text,p_loser bigint DEFAULT NULL)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE m challenge_matches; n integer; stamp timestamptz:=clock_timestamp(); day0 timestamptz;
 sa integer:=0; sb integer:=0; ca integer:=0; cb integer:=0; msa bigint:=0; msb bigint:=0; ra integer:=0; rb integer:=0;
 winner bigint; draw boolean:=false; settle boolean; pair_ok boolean; rep_a boolean; rep_b boolean;
 xa integer:=0; xb integer:=0; ta integer:=0; tb integer:=0; ux_a integer; ux_b integer; ut_a integer; ut_b integer;
 ev text; res jsonb; r jsonb;
BEGIN
 SELECT * INTO m FROM challenge_matches WHERE id=p_match AND kind='live' FOR UPDATE;
 IF NOT FOUND OR m.status NOT IN('created','in_progress') THEN RETURN; END IF;
 IF p_status NOT IN('completed','expired','abandoned','cancelled') THEN RAISE EXCEPTION 'bad_transition'; END IF;
 n:=jsonb_array_length(m.snapshot); r:=m.rules; day0:=date_trunc('day',stamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
 IF p_status='completed' AND m.question_index<n THEN RAISE EXCEPTION 'unfinished_match'; END IF;
 settle:=p_status IN('completed','abandoned');
 IF settle THEN
  SELECT coalesce(sum(score),0),count(*) FILTER(WHERE is_correct),coalesce(sum(elapsed_ms) FILTER(WHERE NOT timed_out),0),
   count(*) FILTER(WHERE NOT timed_out) INTO sa,ca,msa,ra FROM challenge_match_answers WHERE match_id=m.id AND user_id=m.attacker_id;
  SELECT coalesce(sum(score),0),count(*) FILTER(WHERE is_correct),coalesce(sum(elapsed_ms) FILTER(WHERE NOT timed_out),0),
   count(*) FILTER(WHERE NOT timed_out) INTO sb,cb,msb,rb FROM challenge_match_answers WHERE match_id=m.id AND user_id=m.defender_id;
  IF p_status='completed' THEN
   winner:=CASE WHEN sa>sb THEN m.attacker_id WHEN sb>sa THEN m.defender_id END; draw:=(sa=sb);
  ELSE winner:=CASE WHEN p_loser=m.attacker_id THEN m.defender_id ELSE m.attacker_id END; END IF;
  -- Anti-farming: (1) limited rewarded matches per pair per 24h, (2) content repeated by a player within the
  -- repeat window earns that player nothing. Evaluated under the pair's profile locks, so it is race-free.
  pair_ok:=(SELECT count(*) FROM challenge_matches x WHERE x.kind='live' AND x.attacker_id=m.attacker_id AND x.defender_id=m.defender_id
    AND x.id<>m.id AND x.status IN('completed','abandoned') AND x.reward_eligible AND x.finished_at>stamp-interval '24 hours')
    <(r->>'live_pair_daily_limit')::integer;
  rep_a:=EXISTS(SELECT 1 FROM challenge_match_players mp JOIN challenge_matches x ON x.id=mp.match_id WHERE mp.user_id=m.attacker_id
    AND mp.role IN('attacker','player') AND x.id<>m.id AND x.content_hash=m.content_hash AND x.created_at<m.created_at
    AND x.created_at>stamp-make_interval(secs=>(r->>'repeat_content_seconds')::integer));
  rep_b:=EXISTS(SELECT 1 FROM challenge_match_players mp JOIN challenge_matches x ON x.id=mp.match_id WHERE mp.user_id=m.defender_id
    AND mp.role IN('attacker','player') AND x.id<>m.id AND x.content_hash=m.content_hash AND x.created_at<m.created_at
    AND x.created_at>stamp-make_interval(secs=>(r->>'repeat_content_seconds')::integer));
  -- Daily caps are shared with async rewards: both write the same xp_delta/trophies_delta columns.
  SELECT coalesce(sum(greatest(p.xp_delta,0)),0),coalesce(sum(greatest(p.trophies_delta,0)),0) INTO ux_a,ut_a
   FROM challenge_match_players p JOIN challenge_matches x ON x.id=p.match_id
   WHERE p.user_id=m.attacker_id AND x.id<>m.id AND x.finished_at>=day0;
  SELECT coalesce(sum(greatest(p.xp_delta,0)),0),coalesce(sum(greatest(p.trophies_delta,0)),0) INTO ux_b,ut_b
   FROM challenge_match_players p JOIN challenge_matches x ON x.id=p.match_id
   WHERE p.user_id=m.defender_id AND x.id<>m.id AND x.finished_at>=day0;
  -- XP only for a fully played match (a forfeit earns none). Trophies: win/draw only; loss penalty is config (default 0).
  IF p_status='completed' AND pair_ok AND NOT rep_a THEN
   xa:=least(ca*(r->>'xp_per_correct')::integer,greatest(0,(r->>'daily_xp_limit')::integer-ux_a)); END IF;
  IF p_status='completed' AND pair_ok AND NOT rep_b THEN
   xb:=least(cb*(r->>'xp_per_correct')::integer,greatest(0,(r->>'daily_xp_limit')::integer-ux_b)); END IF;
  IF pair_ok AND NOT rep_a AND (draw OR winner=m.attacker_id) THEN
   ta:=least(CASE WHEN draw THEN (r->>'live_draw_trophies')::integer ELSE (r->>'live_win_trophies')::integer END,
    greatest(0,(r->>'daily_trophy_limit')::integer-ut_a)); END IF;
  IF pair_ok AND NOT rep_b AND (draw OR winner=m.defender_id) THEN
   tb:=least(CASE WHEN draw THEN (r->>'live_draw_trophies')::integer ELSE (r->>'live_win_trophies')::integer END,
    greatest(0,(r->>'daily_trophy_limit')::integer-ut_b)); END IF;
  IF NOT draw AND winner=m.defender_id THEN
   ta:=-least((SELECT trophies FROM challenge_profiles WHERE user_id=m.attacker_id),(r->>'live_loss_trophies')::integer); END IF;
  IF NOT draw AND winner=m.attacker_id THEN
   tb:=-least((SELECT trophies FROM challenge_profiles WHERE user_id=m.defender_id),(r->>'live_loss_trophies')::integer); END IF;
  PERFORM challenge_live_apply(m.id,m.attacker_id,m.subject_id,m.unit_id,n,sa,ca,msa,ra,
   (NOT draw AND winner=m.attacker_id),(NOT draw AND winner=m.defender_id),xa,ta);
  PERFORM challenge_live_apply(m.id,m.defender_id,m.subject_id,m.unit_id,n,sb,cb,msb,rb,
   (NOT draw AND winner=m.defender_id),(NOT draw AND winner=m.attacker_id),xb,tb);
 END IF;
 res:=jsonb_build_object('reason',p_status,'total',n,'winner_id',winner,'draw',draw,'forfeit_by',p_loser,
  'players',CASE WHEN settle THEN jsonb_build_array(
    jsonb_build_object('user_id',m.attacker_id,'score',sa,'correct',ca,'xp',xa,'trophies',ta,'average_response_ms',CASE WHEN ra>0 THEN msa/ra ELSE 0 END),
    jsonb_build_object('user_id',m.defender_id,'score',sb,'correct',cb,'xp',xb,'trophies',tb,'average_response_ms',CASE WHEN rb>0 THEN msb/rb ELSE 0 END))
   ELSE '[]'::jsonb END);
 UPDATE challenge_matches SET status=p_status,finished_at=stamp,result=res,
  reward_eligible=(settle AND (xa>0 OR xb>0 OR ta>0 OR tb>0)) WHERE id=m.id;
 UPDATE challenge_live_queue SET status='done',updated_at=stamp WHERE user_id IN(m.attacker_id,m.defender_id) AND match_id=m.id;
 ev:=CASE p_status WHEN 'completed' THEN 'match_completed' WHEN 'abandoned' THEN 'match_abandoned'
  WHEN 'expired' THEN 'match_timeout' ELSE 'match_cancelled' END;
 INSERT INTO challenge_events(match_id,event_type) VALUES(m.id,ev);
END $$;

-- Internal. Caller holds profile locks (asc) + match lock and passes a stamp taken AFTER acquiring them.
-- Applies, in order: countdown end -> global expiry -> question resolution (both answered OR deadline passed).
-- A question is resolved exactly once: the resolver fills timeout rows for anyone who has not answered,
-- advances question_index and opens the next question (or settles). Idempotent by construction.
CREATE OR REPLACE FUNCTION public.challenge_live_progress(p_match uuid,p_stamp timestamptz)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE m challenge_matches; n integer; cnt integer; qsec integer; q jsonb; pl bigint;
BEGIN
 SELECT * INTO m FROM challenge_matches WHERE id=p_match AND kind='live';
 IF m.status='created' AND p_stamp>=m.countdown_until THEN
  UPDATE challenge_matches SET status='in_progress',started_at=m.countdown_until WHERE id=m.id;
  INSERT INTO challenge_events(match_id,event_type,question_index) VALUES(m.id,'match_started',0);
  m.status:='in_progress';
 END IF;
 IF m.status NOT IN('in_progress') THEN RETURN; END IF;
 IF p_stamp>=m.server_deadline THEN PERFORM challenge_live_finish(m.id,'expired'); RETURN; END IF;
 n:=jsonb_array_length(m.snapshot); qsec:=(m.rules->>'question_seconds')::integer;
 IF m.question_index>=n THEN PERFORM challenge_live_finish(m.id,'completed'); RETURN; END IF;
 SELECT count(*) INTO cnt FROM challenge_match_answers WHERE match_id=m.id AND question_index=m.question_index;
 -- Deadline rule (identical to async): an answer is accepted only strictly BEFORE the deadline.
 IF cnt<2 AND p_stamp<m.question_deadline THEN RETURN; END IF;
 q:=m.snapshot->m.question_index;
 FOR pl IN SELECT x FROM unnest(ARRAY[m.attacker_id,m.defender_id]) x ORDER BY x LOOP
  INSERT INTO challenge_match_answers(match_id,user_id,question_index,question_id,option_id,is_correct,score,elapsed_ms,timed_out)
  VALUES(m.id,pl,m.question_index,(q->>'id')::uuid,NULL,false,0,qsec*1000,true) ON CONFLICT(match_id,user_id,question_index) DO NOTHING;
 END LOOP;
 INSERT INTO challenge_events(match_id,event_type,question_index) VALUES(m.id,'question_resolved',m.question_index);
 UPDATE challenge_matches SET question_index=m.question_index+1,question_opened_at=p_stamp,
  question_deadline=least(m.server_deadline,p_stamp+make_interval(secs=>qsec)) WHERE id=m.id;
 IF m.question_index+1>=n THEN PERFORM challenge_live_finish(m.id,'completed');
 ELSE INSERT INTO challenge_events(match_id,event_type,question_index) VALUES(m.id,'question_opened',m.question_index+1); END IF;
END $$;

-- Player answer. Time is clock_timestamp() taken AFTER the pair + match locks are acquired; the client never
-- supplies time. Rules (identical to async: strictly before deadline = accepted; at/after = timeout):
--   before deadline -> recorded once, scored by the central formula
--   exactly at deadline / after -> question resolves as timeout for non-answerers; this option is discarded
-- Duplicate Telegram update (same update key), double tap, stale index and replays never insert a 2nd answer.
CREATE OR REPLACE FUNCTION public.challenge_live_answer(p_match uuid,p_user bigint,p_index integer,p_option integer,p_update text DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE lo bigint; hi bigint; m challenge_matches; stamp timestamptz; q jsonb; correct boolean; ms integer; pts integer;
BEGIN
 SELECT attacker_id,defender_id INTO lo,hi FROM challenge_matches WHERE id=p_match AND kind='live' AND p_user IN(attacker_id,defender_id);
 IF NOT FOUND THEN RAISE EXCEPTION 'not_your_match'; END IF;
 PERFORM user_id FROM challenge_profiles WHERE user_id IN(lo,hi) ORDER BY user_id FOR NO KEY UPDATE;
 SELECT * INTO m FROM challenge_matches WHERE id=p_match FOR UPDATE;
 stamp:=clock_timestamp();
 UPDATE challenge_profiles SET last_seen=stamp WHERE user_id=p_user;
 IF p_update IS NOT NULL AND EXISTS(SELECT 1 FROM challenge_match_answers WHERE user_id=p_user AND update_key=p_update) THEN
  RETURN challenge_live_state(p_match,p_user); END IF;
 PERFORM challenge_live_progress(p_match,stamp);
 SELECT * INTO m FROM challenge_matches WHERE id=p_match;
 IF m.status<>'in_progress' OR p_index IS DISTINCT FROM m.question_index THEN RETURN challenge_live_state(p_match,p_user); END IF;
 IF EXISTS(SELECT 1 FROM challenge_match_answers WHERE match_id=m.id AND user_id=p_user AND question_index=m.question_index) THEN
  RETURN challenge_live_state(p_match,p_user); END IF;
 q:=m.snapshot->m.question_index;
 IF p_option IS NULL OR p_option<0 OR p_option>=jsonb_array_length(q->'options') THEN RAISE EXCEPTION 'invalid_option'; END IF;
 correct:=p_option=(q->>'correct_option_id')::integer;
 ms:=least((m.rules->>'question_seconds')::integer*1000,greatest(0,floor(extract(epoch FROM stamp-m.question_opened_at)*1000)::integer));
 pts:=challenge_question_score(correct,ms::numeric/1000,m.rules);
 INSERT INTO challenge_match_answers(match_id,user_id,question_index,question_id,option_id,is_correct,score,elapsed_ms,timed_out,answered_at,update_key)
 VALUES(m.id,p_user,m.question_index,(q->>'id')::uuid,p_option,correct,pts,ms,false,stamp,p_update);
 INSERT INTO challenge_events(match_id,event_type,question_index) VALUES(m.id,'answer_received',m.question_index);
 PERFORM challenge_live_progress(p_match,stamp);   -- resolves now if both have answered
 RETURN challenge_live_state(p_match,p_user);
END $$;

-- Worker / UI heartbeat. No actor; safe to call from many workers: re-checks everything under the locks.
CREATE OR REPLACE FUNCTION public.challenge_live_tick(p_match uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE lo bigint; hi bigint; m challenge_matches;
BEGIN
 SELECT attacker_id,defender_id INTO lo,hi FROM challenge_matches WHERE id=p_match AND kind='live';
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found'; END IF;
 PERFORM user_id FROM challenge_profiles WHERE user_id IN(lo,hi) ORDER BY user_id FOR NO KEY UPDATE;
 PERFORM id FROM challenge_matches WHERE id=p_match FOR UPDATE;
 PERFORM challenge_live_progress(p_match,clock_timestamp());
 SELECT * INTO m FROM challenge_matches WHERE id=p_match;
 RETURN jsonb_build_object('id',m.id,'status',m.status,'index',m.question_index,'attacker_id',m.attacker_id,'defender_id',m.defender_id);
END $$;

-- Leave: countdown -> cancelled (no result/rewards); in progress -> abandoned (leaver loses, no XP for anyone).
CREATE OR REPLACE FUNCTION public.challenge_live_leave(p_match uuid,p_user bigint)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE lo bigint; hi bigint; m challenge_matches; stamp timestamptz;
BEGIN
 SELECT attacker_id,defender_id INTO lo,hi FROM challenge_matches WHERE id=p_match AND kind='live' AND p_user IN(attacker_id,defender_id);
 IF NOT FOUND THEN RAISE EXCEPTION 'not_your_match'; END IF;
 PERFORM user_id FROM challenge_profiles WHERE user_id IN(lo,hi) ORDER BY user_id FOR NO KEY UPDATE;
 PERFORM id FROM challenge_matches WHERE id=p_match FOR UPDATE;
 stamp:=clock_timestamp();
 PERFORM challenge_live_progress(p_match,stamp);
 SELECT * INTO m FROM challenge_matches WHERE id=p_match;
 IF m.status='created' THEN PERFORM challenge_live_finish(p_match,'cancelled',p_user);
 ELSIF m.status='in_progress' THEN PERFORM challenge_live_finish(p_match,'abandoned',p_user); END IF;
 RETURN challenge_live_state(p_match,p_user);
END $$;

CREATE OR REPLACE FUNCTION public.challenge_live_attach_message(p_match uuid,p_user bigint,p_message bigint)
RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path=public AS $$
 UPDATE challenge_match_players SET message_id=p_message WHERE match_id=p_match AND user_id=p_user
$$;

-- Read-only, lock-free list of matches a worker should tick (recovery after restart / Redis reset).
CREATE OR REPLACE FUNCTION public.challenge_live_due(p_limit integer DEFAULT 50)
RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER SET search_path=public AS $$
 SELECT coalesce(jsonb_agg(id),'[]'::jsonb) FROM (
  (SELECT id FROM challenge_matches WHERE kind='live' AND status='created' AND countdown_until<=clock_timestamp()
    ORDER BY countdown_until LIMIT least(greatest(p_limit,1),200))
  UNION ALL
  (SELECT id FROM challenge_matches WHERE kind='live' AND status='in_progress' AND question_deadline<=clock_timestamp()
    ORDER BY question_deadline LIMIT least(greatest(p_limit,1),200))) d
$$;

-- ---------- 7) security (same model as 0009: service_role only, RLS on, no client access) ----------
ALTER TABLE public.challenge_live_queue ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.challenge_live_queue FROM PUBLIC,anon,authenticated;
GRANT ALL ON public.challenge_live_queue TO service_role;
DO $$ DECLARE f record; BEGIN
 FOR f IN SELECT oid::regprocedure AS sig FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname IN(
  'challenge_live_range','challenge_live_queue_state','challenge_live_state','challenge_live_join','challenge_live_cancel',
  'challenge_live_expire_queue','challenge_live_try_match','challenge_live_apply','challenge_live_finish',
  'challenge_live_progress','challenge_live_answer','challenge_live_tick','challenge_live_leave',
  'challenge_live_attach_message','challenge_live_due','challenge_block_async_while_live',
  'challenge_attack_state','challenge_advance_attack') LOOP
  EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,anon,authenticated',f.sig);
  EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role',f.sig);
 END LOOP;
END $$;
INSERT INTO public.feature_flags(key,enabled) VALUES('live_matches_enabled',false) ON CONFLICT(key) DO NOTHING;
COMMIT;
