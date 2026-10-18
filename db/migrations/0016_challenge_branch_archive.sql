-- Admin-only permanent content deletion. Existing FK constraints protect game history.
BEGIN;
CREATE OR REPLACE FUNCTION public.challenge_delete_archived_branch(p_branch uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE b challenge_branches;
BEGIN
 SELECT * INTO b FROM challenge_branches WHERE id=p_branch FOR UPDATE;
 IF NOT FOUND THEN RETURN false; END IF;
 IF b.status <> 'archived' THEN RAISE EXCEPTION 'branch_not_archived'; END IF;
 -- Lock parents before descendants, preventing concurrent content insertion/moves.
 PERFORM id FROM challenge_subjects WHERE branch_id=p_branch ORDER BY id FOR UPDATE;
 PERFORM u.id FROM challenge_units u JOIN challenge_subjects s ON s.id=u.subject_id
   WHERE s.branch_id=p_branch ORDER BY u.id FOR UPDATE OF u;
 PERFORM t.id FROM challenge_tests t JOIN challenge_units u ON u.id=t.unit_id
   JOIN challenge_subjects s ON s.id=u.subject_id WHERE s.branch_id=p_branch
   ORDER BY t.id FOR UPDATE OF t;
 DELETE FROM challenge_questions WHERE test_id IN (
   SELECT t.id FROM challenge_tests t JOIN challenge_units u ON u.id=t.unit_id
   JOIN challenge_subjects s ON s.id=u.subject_id WHERE s.branch_id=p_branch);
 DELETE FROM challenge_tests WHERE unit_id IN (
   SELECT u.id FROM challenge_units u JOIN challenge_subjects s ON s.id=u.subject_id WHERE s.branch_id=p_branch);
 DELETE FROM challenge_units WHERE subject_id IN (SELECT id FROM challenge_subjects WHERE branch_id=p_branch);
 DELETE FROM challenge_subjects WHERE branch_id=p_branch;
 DELETE FROM challenge_branches WHERE id=p_branch;
 RETURN true;
 -- Any FK error rolls back EVERY deletion in this call, including earlier content deletes.
END $$;
REVOKE ALL ON FUNCTION public.challenge_delete_archived_branch(uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.challenge_delete_archived_branch(uuid) TO service_role;
COMMIT;
