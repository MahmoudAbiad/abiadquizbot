-- ============================================================
-- 0008_challenge_content.sql — بنك اختبارات التحديات (جداول المحتوى فقط)
-- التصميم الكامل: docs/challenges/DESIGN.md
--
-- ⚠️ لم يُطبَّق على أي قاعدة بيانات. لا تطبّقه قبل أخذ Backup مؤكد (docs/challenges/BACKUP.md).
-- Additive فقط: لا يعدّل ولا يحذف أي جدول/عمود موجود. آمن للتشغيل أكثر من مرة.
-- Rollback: 0008_challenge_content_rollback.sql (يحذف الكائنات الجديدة فقط).
-- ============================================================

-- ---------- 1) الهرم: فرع → مادة → وحدة ----------
CREATE TABLE IF NOT EXISTS public.challenge_branches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 80),
  sort_order integer NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS challenge_branches_name_uq
  ON public.challenge_branches (lower(btrim(name))) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS public.challenge_subjects (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  branch_id uuid NOT NULL REFERENCES public.challenge_branches(id),
  name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 80),
  sort_order integer NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS challenge_subjects_name_uq
  ON public.challenge_subjects (branch_id, lower(btrim(name))) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS public.challenge_units (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  subject_id uuid NOT NULL REFERENCES public.challenge_subjects(id),
  name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 80),
  sort_order integer NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS challenge_units_name_uq
  ON public.challenge_units (subject_id, lower(btrim(name))) WHERE status = 'active';

-- ---------- 2) الاختبارات والأسئلة (Snapshot مستقل عن جدول quizzes) ----------
CREATE TABLE IF NOT EXISTS public.challenge_tests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  unit_id uuid NOT NULL REFERENCES public.challenge_units(id),
  title text NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 120),
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('draft','active','disabled','archived')),
  source_type text NOT NULL CHECK (source_type IN ('json_upload','existing_abiad_quiz','manual_future')),
  -- مرجع للتتبع فقط (بلا FK عمداً حتى لا يتأثر الاختبار بحذف الكويز الأصلي)
  source_reference text,
  content_hash text NOT NULL,
  question_count integer NOT NULL DEFAULT 0 CHECK (question_count >= 0),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by_admin bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  disabled_at timestamptz,
  archived_at timestamptz
);
-- Idempotency: نفس المحتوى لا يتكرر بالوحدة نفسها (المؤرشف لا يمنع إعادة الإضافة)
CREATE UNIQUE INDEX IF NOT EXISTS challenge_tests_unit_hash_uq
  ON public.challenge_tests (unit_id, content_hash) WHERE status <> 'archived';
CREATE INDEX IF NOT EXISTS challenge_tests_unit_status_idx
  ON public.challenge_tests (unit_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS public.challenge_questions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  test_id uuid NOT NULL REFERENCES public.challenge_tests(id),
  question_order integer NOT NULL CHECK (question_order >= 0),
  question_text text NOT NULL,
  options jsonb NOT NULL CHECK (jsonb_typeof(options) = 'array' AND jsonb_array_length(options) BETWEEN 2 AND 10),
  correct_option_id integer NOT NULL CHECK (correct_option_id >= 0),
  explanation text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT challenge_questions_order_uq UNIQUE (test_id, question_order),
  CONSTRAINT challenge_questions_correct_in_range
    CHECK (correct_option_id < jsonb_array_length(options))
);

-- ---------- 3) الاستيراد الذري (اختبار + أسئلته بمعاملة واحدة) ----------
CREATE OR REPLACE FUNCTION public.challenge_import_test(
  p_unit_id uuid, p_title text, p_source_type text, p_source_reference text,
  p_content_hash text, p_admin_id bigint, p_questions jsonb, p_metadata jsonb DEFAULT '{}'::jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
  v_test_id uuid;
  v_existing uuid;
  v_count integer;
BEGIN
  IF p_questions IS NULL OR jsonb_typeof(p_questions) <> 'array' OR jsonb_array_length(p_questions) = 0 THEN
    RAISE EXCEPTION 'no_questions';
  END IF;

  -- الوحدة (والمادة والفرع) لازم تكون فعّالة
  PERFORM 1 FROM challenge_units u
    JOIN challenge_subjects s ON s.id = u.subject_id AND s.status = 'active'
    JOIN challenge_branches b ON b.id = s.branch_id AND b.status = 'active'
   WHERE u.id = p_unit_id AND u.status = 'active';
  IF NOT FOUND THEN RAISE EXCEPTION 'unit_not_active'; END IF;

  SELECT id INTO v_existing FROM challenge_tests
   WHERE unit_id = p_unit_id AND content_hash = p_content_hash AND status <> 'archived' LIMIT 1;
  IF v_existing IS NOT NULL THEN
    RETURN jsonb_build_object('created', false, 'test_id', v_existing);
  END IF;

  v_count := jsonb_array_length(p_questions);
  BEGIN
    INSERT INTO challenge_tests (unit_id, title, status, source_type, source_reference,
                                 content_hash, question_count, metadata, created_by_admin)
    VALUES (p_unit_id, p_title, 'active', p_source_type, p_source_reference,
            p_content_hash, v_count, COALESCE(p_metadata, '{}'::jsonb), p_admin_id)
    RETURNING id INTO v_test_id;
  EXCEPTION WHEN unique_violation THEN
    -- سباق: ضغطتان متزامنتان؛ نرجع الاختبار الذي سبق
    SELECT id INTO v_existing FROM challenge_tests
     WHERE unit_id = p_unit_id AND content_hash = p_content_hash AND status <> 'archived' LIMIT 1;
    RETURN jsonb_build_object('created', false, 'test_id', v_existing);
  END;

  INSERT INTO challenge_questions (test_id, question_order, question_text, options,
                                   correct_option_id, explanation, metadata)
  SELECT v_test_id, (t.ord - 1)::int, t.q->>'question', t.q->'options',
         (t.q->>'correct_option_id')::int, NULLIF(t.q->>'explanation',''),
         COALESCE(t.q->'metadata', '{}'::jsonb)
    FROM jsonb_array_elements(p_questions) WITH ORDINALITY AS t(q, ord);

  RETURN jsonb_build_object('created', true, 'test_id', v_test_id, 'question_count', v_count);
END $$;

-- ---------- 4) ما هو "قابل للعب": اختبار active + كل أسلافه active + أسئلة ≥ 1 ----------
CREATE OR REPLACE VIEW public.challenge_playable_units AS
SELECT b.id AS branch_id, b.name AS branch_name, b.sort_order AS branch_order,
       s.id AS subject_id, s.name AS subject_name, s.sort_order AS subject_order,
       u.id AS unit_id, u.name AS unit_name, u.sort_order AS unit_order,
       count(t.id)::int AS active_test_count
  FROM challenge_units u
  JOIN challenge_subjects s ON s.id = u.subject_id AND s.status = 'active'
  JOIN challenge_branches b ON b.id = s.branch_id AND b.status = 'active'
  JOIN challenge_tests t ON t.unit_id = u.id AND t.status = 'active' AND t.question_count > 0
 WHERE u.status = 'active'
 GROUP BY b.id, s.id, u.id;

-- ---------- 5) الأمان: service_role فقط (نفس نمط جداول الدفع) ----------
ALTER TABLE public.challenge_branches  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.challenge_subjects  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.challenge_units     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.challenge_tests     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.challenge_questions ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.challenge_branches, public.challenge_subjects, public.challenge_units,
              public.challenge_tests, public.challenge_questions,
              public.challenge_playable_units FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.challenge_import_test(uuid,text,text,text,text,bigint,jsonb,jsonb)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.challenge_import_test(uuid,text,text,text,text,bigint,jsonb,jsonb)
  TO service_role;

-- ---------- 6) Feature flag: مطفأ افتراضياً (عكس الافتراضي العام للفلاجات) ----------
INSERT INTO public.feature_flags (key, enabled) VALUES ('challenge_system_enabled', false)
ON CONFLICT (key) DO NOTHING;
