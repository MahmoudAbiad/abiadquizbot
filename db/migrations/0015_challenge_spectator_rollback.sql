-- Additive 3D rollback: no game/history/content or feature flags are changed.
BEGIN;
DROP FUNCTION IF EXISTS public.challenge_spectator_state(uuid,bigint);
DROP FUNCTION IF EXISTS public.challenge_spectator_incoming(bigint,integer);
DROP INDEX IF EXISTS public.challenge_spectator_incoming;
COMMIT;
