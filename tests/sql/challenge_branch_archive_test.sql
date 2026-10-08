BEGIN;
DO $$
DECLARE b uuid; s uuid; u uuid; t uuid; q uuid; b2 uuid; uid bigint := -9223372036854775000;
BEGIN
 INSERT INTO challenge_branches(name) VALUES('archive-regression-'||gen_random_uuid()) RETURNING id INTO b;
 BEGIN
   PERFORM challenge_delete_archived_branch(b);
   RAISE EXCEPTION 'active branch was deleted';
 EXCEPTION WHEN raise_exception THEN
   IF SQLERRM <> 'branch_not_archived' THEN RAISE; END IF;
 END;
 INSERT INTO challenge_subjects(branch_id,name) VALUES(b,'subject') RETURNING id INTO s;
 INSERT INTO challenge_units(subject_id,name) VALUES(s,'unit') RETURNING id INTO u;
 INSERT INTO challenge_tests(unit_id,title,source_type,content_hash,created_by_admin)
   VALUES(u,'test','json_upload','hash',0) RETURNING id INTO t;
 INSERT INTO challenge_questions(test_id,question_order,question_text,options,correct_option_id)
   VALUES(t,0,'question','["a","b"]',0) RETURNING id INTO q;
 UPDATE challenge_branches SET status='archived' WHERE id=b;
 -- A late failure (at the final branch DELETE) must roll back earlier child deletes.
 INSERT INTO challenge_profiles(user_id,branch_id,xp,trophies) VALUES(uid,b,99,12);
 BEGIN
   PERFORM challenge_delete_archived_branch(b);
   RAISE EXCEPTION 'linked branch was deleted';
 EXCEPTION WHEN foreign_key_violation THEN NULL;
 END;
 ASSERT EXISTS(SELECT 1 FROM challenge_questions WHERE id=q), 'partial deletion';
 ASSERT EXISTS(SELECT 1 FROM challenge_tests WHERE id=t);
 ASSERT EXISTS(SELECT 1 FROM challenge_subjects WHERE id=s);
 ASSERT (SELECT xp=99 AND trophies=12 FROM challenge_profiles WHERE user_id=uid);
 DELETE FROM challenge_profiles WHERE user_id=uid;
 -- Restoration uniqueness is enforced by the existing partial unique index.
 INSERT INTO challenge_branches(name) SELECT name FROM challenge_branches WHERE id=b RETURNING id INTO b2;
 BEGIN
   UPDATE challenge_branches SET status='active' WHERE id=b;
   RAISE EXCEPTION 'duplicate restore accepted';
 EXCEPTION WHEN unique_violation THEN NULL;
 END;
 ASSERT (SELECT status='archived' FROM challenge_branches WHERE id=b);
 DELETE FROM challenge_branches WHERE id=b2;
 UPDATE challenge_branches SET status='active',archived_at=NULL WHERE id=b;
 ASSERT EXISTS(SELECT 1 FROM challenge_questions WHERE id=q);
 UPDATE challenge_branches SET status='archived' WHERE id=b;
 ASSERT challenge_delete_archived_branch(b);
 ASSERT NOT EXISTS(SELECT 1 FROM challenge_branches WHERE id=b);
 ASSERT NOT EXISTS(SELECT 1 FROM challenge_questions WHERE id=q);
 ASSERT NOT challenge_delete_archived_branch(b);
 ASSERT NOT has_function_privilege('anon','challenge_delete_archived_branch(uuid)','EXECUTE');
 ASSERT NOT has_function_privilege('authenticated','challenge_delete_archived_branch(uuid)','EXECUTE');
 ASSERT has_function_privilege('service_role','challenge_delete_archived_branch(uuid)','EXECUTE');
END $$;
ROLLBACK;
