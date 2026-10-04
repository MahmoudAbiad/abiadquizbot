-- اختبار سلوكي لـ 0008_challenge_content.sql. شغّله على قاعدة Postgres مؤقتة (ليس الإنتاج):
--   psql -v ON_ERROR_STOP=1 -d <scratch_db> -f db/migrations/0008_challenge_content.sql
--   psql -v ON_ERROR_STOP=1 -d <scratch_db> -f tests/sql/challenge_content_test.sql
-- (يتطلب جدول feature_flags وأدوار anon/authenticated/service_role كما في Supabase). يفشل بـ EXCEPTION عند أي خلل.
BEGIN;
DO $$
DECLARE
  b uuid; s uuid; u uuid; u2 uuid; r jsonb; r2 jsonb; t1 uuid; n int; failed boolean;
  q jsonb := '[{"question":"س1","options":["أ","ب"],"correct_option_id":1,"explanation":"ش"},
               {"question":"س2","options":["أ","ب","ج"],"correct_option_id":0}]';
BEGIN
  INSERT INTO challenge_branches(name) VALUES ('علمي') RETURNING id INTO b;
  INSERT INTO challenge_subjects(branch_id,name) VALUES (b,'فيزياء') RETURNING id INTO s;
  INSERT INTO challenge_units(subject_id,name) VALUES (s,'الحركة') RETURNING id INTO u;
  INSERT INTO challenge_units(subject_id,name) VALUES (s,'وحدة فارغة') RETURNING id INTO u2;

  -- 1) وحدة بلا اختبارات لا تظهر كقابلة للعب
  SELECT count(*) INTO n FROM challenge_playable_units WHERE unit_id IN (u,u2);
  IF n <> 0 THEN RAISE EXCEPTION 'T1 empty units must not be playable'; END IF;

  -- 2) أكثر من اختبار بوحدة + استيراد ذري + question_count من المحتوى الفعلي
  r := challenge_import_test(u,'اختبار 1','json_upload',NULL,'h1',1,q);
  IF NOT (r->>'created')::bool THEN RAISE EXCEPTION 'T2 not created'; END IF;
  t1 := (r->>'test_id')::uuid;
  SELECT count(*) INTO n FROM challenge_questions WHERE test_id=t1;
  IF n <> 2 THEN RAISE EXCEPTION 'T2 question rows=%', n; END IF;
  r2 := challenge_import_test(u,'اختبار 2','existing_abiad_quiz','some-quiz-id','h2',1,q);
  SELECT count(*) INTO n FROM challenge_tests WHERE unit_id=u;
  IF n <> 2 THEN RAISE EXCEPTION 'T2 multi tests=%', n; END IF;

  -- 3) Idempotency: نفس المحتوى مرتين لا يكرر
  r2 := challenge_import_test(u,'عنوان آخر','json_upload',NULL,'h1',1,q);
  IF (r2->>'created')::bool OR (r2->>'test_id')::uuid <> t1 THEN RAISE EXCEPTION 'T3 duplicate created'; END IF;
  SELECT count(*) INTO n FROM challenge_tests WHERE unit_id=u;
  IF n <> 2 THEN RAISE EXCEPTION 'T3 tests=%', n; END IF;

  -- 4) فشل بمنتصف الاستيراد (إجابة خارج النطاق بالسؤال الثاني) => لا اختبار نصف مكتمل
  failed := false;
  BEGIN
    PERFORM challenge_import_test(u,'تالف','json_upload',NULL,'h-bad',1,
      '[{"question":"ok","options":["أ","ب"],"correct_option_id":0},
        {"question":"bad","options":["أ","ب"],"correct_option_id":5}]');
  EXCEPTION WHEN OTHERS THEN failed := true; END;
  IF NOT failed THEN RAISE EXCEPTION 'T4 bad import should fail'; END IF;
  SELECT count(*) INTO n FROM challenge_tests WHERE content_hash='h-bad';
  IF n <> 0 THEN RAISE EXCEPTION 'T4 partial test left behind'; END IF;
  SELECT count(*) INTO n FROM challenge_questions WHERE question_text IN ('ok','bad');
  IF n <> 0 THEN RAISE EXCEPTION 'T4 partial questions left behind'; END IF;

  -- 5) اختبار بلا أسئلة مرفوض
  failed := false;
  BEGIN PERFORM challenge_import_test(u,'فارغ','json_upload',NULL,'h-empty',1,'[]');
  EXCEPTION WHEN OTHERS THEN failed := true; END;
  IF NOT failed THEN RAISE EXCEPTION 'T5 empty import accepted'; END IF;

  -- 6) الوحدة صارت قابلة للعب؛ تعطيل كل اختباراتها => تختفي
  SELECT active_test_count INTO n FROM challenge_playable_units WHERE unit_id=u;
  IF n <> 2 THEN RAISE EXCEPTION 'T6 playable count=%', n; END IF;
  UPDATE challenge_tests SET status='disabled', disabled_at=now() WHERE unit_id=u;
  SELECT count(*) INTO n FROM challenge_playable_units WHERE unit_id=u;
  IF n <> 0 THEN RAISE EXCEPTION 'T6 disabled tests still playable'; END IF;
  UPDATE challenge_tests SET status='active', disabled_at=NULL WHERE unit_id=u;

  -- 7) أرشفة الوحدة: تختفي من اللعب لكن الاختبارات والأسئلة تبقى (مراجع تاريخية)
  UPDATE challenge_units SET status='archived', archived_at=now() WHERE id=u;
  SELECT count(*) INTO n FROM challenge_playable_units WHERE unit_id=u;
  IF n <> 0 THEN RAISE EXCEPTION 'T7 archived unit still playable'; END IF;
  SELECT count(*) INTO n FROM challenge_questions WHERE test_id=t1;
  IF n <> 2 THEN RAISE EXCEPTION 'T7 history lost'; END IF;
  failed := false;
  BEGIN PERFORM challenge_import_test(u,'x','json_upload',NULL,'h-new',1,q);
  EXCEPTION WHEN OTHERS THEN failed := (SQLERRM = 'unit_not_active'); END;
  IF NOT failed THEN RAISE EXCEPTION 'T7 import into archived unit allowed'; END IF;
  UPDATE challenge_units SET status='active', archived_at=NULL WHERE id=u;

  -- 8) أرشفة مادة => كل وحداتها تخرج من اللعب بلا cascade
  UPDATE challenge_subjects SET status='archived' WHERE id=s;
  SELECT count(*) INTO n FROM challenge_playable_units WHERE subject_id=s;
  IF n <> 0 THEN RAISE EXCEPTION 'T8 archived subject still playable'; END IF;
  UPDATE challenge_subjects SET status='active' WHERE id=s;

  -- 9) أرشفة اختبار تسمح بإعادة إضافة نفس المحتوى
  UPDATE challenge_tests SET status='archived', archived_at=now() WHERE id=t1;
  r2 := challenge_import_test(u,'إعادة','json_upload',NULL,'h1',1,q);
  IF NOT (r2->>'created')::bool THEN RAISE EXCEPTION 'T9 re-import after archive blocked'; END IF;

  -- 10) حذف "كويز المصدر" لا يؤثر: لا FK ولا اعتماد على جدول quizzes
  IF EXISTS (SELECT 1 FROM information_schema.table_constraints c
             JOIN information_schema.constraint_column_usage k USING (constraint_name)
             WHERE c.table_name='challenge_tests' AND c.constraint_type='FOREIGN KEY' AND k.table_name='quizzes') THEN
    RAISE EXCEPTION 'T10 test depends on quizzes';
  END IF;

  -- 11) اسم مكرر بنفس المستوى مرفوض
  failed := false;
  BEGIN INSERT INTO challenge_units(subject_id,name) VALUES (s,'  الحركة ');
  EXCEPTION WHEN unique_violation THEN failed := true; END;
  IF NOT failed THEN RAISE EXCEPTION 'T11 duplicate name allowed'; END IF;

  -- 12) صلاحيات + فلاج
  IF has_table_privilege('anon','challenge_tests','SELECT') OR has_table_privilege('authenticated','challenge_questions','SELECT')
     OR has_table_privilege('anon','challenge_playable_units','SELECT')
     OR has_function_privilege('anon','challenge_import_test(uuid,text,text,text,text,bigint,jsonb,jsonb)','EXECUTE') THEN
    RAISE EXCEPTION 'T12 anon/authenticated have access';
  END IF;
  IF NOT has_function_privilege('service_role','challenge_import_test(uuid,text,text,text,text,bigint,jsonb,jsonb)','EXECUTE') THEN
    RAISE EXCEPTION 'T12 service_role lacks execute';
  END IF;
  IF (SELECT enabled FROM feature_flags WHERE key='challenge_system_enabled') IS DISTINCT FROM false THEN
    RAISE EXCEPTION 'T12 flag not seeded off';
  END IF;
  RAISE NOTICE 'ALL SQL TESTS PASSED';
END $$;
ROLLBACK;
