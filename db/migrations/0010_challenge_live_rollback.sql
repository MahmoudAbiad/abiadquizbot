-- DESTRUCTIVE TO LIVE-MATCH HISTORY ONLY. Export live matches first. Keeps 0008/0009 and all async history.
-- Disable live_matches_enabled + stop live workers before running. Safe to re-run.
-- Note: the kind-guarded challenge_attack_state / challenge_advance_attack from 0010 are left in place on purpose
-- (they are behaviourally identical for async matches); re-running 0009 restores the original text if desired.
BEGIN;
DROP TRIGGER IF EXISTS challenge_block_async_while_live ON public.challenge_matches;
DROP FUNCTION IF EXISTS public.challenge_block_async_while_live();
DROP FUNCTION IF EXISTS public.challenge_live_due(integer);
DROP FUNCTION IF EXISTS public.challenge_live_attach_message(uuid,bigint,bigint);
DROP FUNCTION IF EXISTS public.challenge_live_leave(uuid,bigint);
DROP FUNCTION IF EXISTS public.challenge_live_tick(uuid);
DROP FUNCTION IF EXISTS public.challenge_live_answer(uuid,bigint,integer,integer,text);
DROP FUNCTION IF EXISTS public.challenge_live_progress(uuid,timestamptz);
DROP FUNCTION IF EXISTS public.challenge_live_finish(uuid,text,bigint);
DROP FUNCTION IF EXISTS public.challenge_live_apply(uuid,bigint,uuid,uuid,integer,integer,integer,bigint,integer,boolean,boolean,integer,integer);
DROP FUNCTION IF EXISTS public.challenge_live_try_match(bigint,jsonb);
DROP FUNCTION IF EXISTS public.challenge_live_expire_queue(integer);
DROP FUNCTION IF EXISTS public.challenge_live_cancel(bigint,text);
DROP FUNCTION IF EXISTS public.challenge_live_join(bigint,uuid,text,jsonb);
DROP FUNCTION IF EXISTS public.challenge_live_state(uuid,bigint);
DROP FUNCTION IF EXISTS public.challenge_live_queue_state(bigint);
DROP FUNCTION IF EXISTS public.challenge_live_range(numeric,jsonb);
DROP TABLE IF EXISTS public.challenge_live_queue;
DROP INDEX IF EXISTS public.challenge_one_active_live_defender;
DROP INDEX IF EXISTS public.challenge_live_countdown_due;
DROP INDEX IF EXISTS public.challenge_match_players_user;
-- Live rows would violate the narrower CHECK / kind usage: remove them (and their children) first.
DELETE FROM public.challenge_events WHERE match_id IN(SELECT id FROM public.challenge_matches WHERE kind='live');
DELETE FROM public.challenge_match_answers WHERE match_id IN(SELECT id FROM public.challenge_matches WHERE kind='live');
DELETE FROM public.challenge_match_players WHERE match_id IN(SELECT id FROM public.challenge_matches WHERE kind='live');
DELETE FROM public.challenge_notifications WHERE match_id IN(SELECT id FROM public.challenge_matches WHERE kind='live');
DELETE FROM public.challenge_matches WHERE kind='live' OR status='cancelled';
ALTER TABLE public.challenge_match_players DROP COLUMN IF EXISTS message_id;
ALTER TABLE public.challenge_matches DROP COLUMN IF EXISTS countdown_until;
ALTER TABLE public.challenge_matches DROP CONSTRAINT IF EXISTS challenge_matches_status_check;
ALTER TABLE public.challenge_matches ADD CONSTRAINT challenge_matches_status_check
  CHECK(status IN('created','in_progress','completed','expired','abandoned'));
DELETE FROM public.feature_flags WHERE key='live_matches_enabled';
COMMIT;
