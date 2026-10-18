-- Removes the API only; permanently deleted content cannot be restored by rollback.
BEGIN;
DROP FUNCTION IF EXISTS public.challenge_delete_archived_branch(uuid);
COMMIT;
