-- DESTRUCTIVE TO GAME HISTORY ONLY. Export challenge_* game tables before using.
-- Keep 0008 content bank. Disable flags + stop game workers before this rollback.
BEGIN;
DROP FUNCTION IF EXISTS public.challenge_claim_notifications(integer);
DROP FUNCTION IF EXISTS public.challenge_find_land(bigint,integer,bigint,integer);
DROP FUNCTION IF EXISTS public.challenge_advance_attack(uuid,bigint,text,integer,integer,text);
DROP FUNCTION IF EXISTS public.challenge_finish_attack(uuid,text);
DROP FUNCTION IF EXISTS public.challenge_start_attack(bigint,bigint,uuid,uuid,text,jsonb);
DROP FUNCTION IF EXISTS public.challenge_attack_state(uuid,bigint);
DROP FUNCTION IF EXISTS public.challenge_touch_profile(bigint,text,uuid);
DROP FUNCTION IF EXISTS public.challenge_question_score(boolean,numeric,jsonb);
DROP TABLE IF EXISTS public.challenge_notifications;
DROP TABLE IF EXISTS public.challenge_events;
DROP TABLE IF EXISTS public.challenge_match_answers;
DROP TABLE IF EXISTS public.challenge_match_players;
DROP TABLE IF EXISTS public.challenge_matches;
DROP VIEW IF EXISTS public.challenge_subject_stats;
DROP TABLE IF EXISTS public.challenge_unit_stats;
DROP TABLE IF EXISTS public.challenge_towers;
DROP TABLE IF EXISTS public.challenge_profiles;
DELETE FROM public.feature_flags WHERE key IN('async_attacks_enabled','spectator_mode_enabled');
COMMIT;
