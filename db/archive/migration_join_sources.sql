-- ============================================================================
-- طرق الانضمام للبوت (Join Sources) — additive فقط، لا يغيّر أي سلوك قائم
-- ============================================================================

-- 1) أعمدة تتبع مصدر الانضمام على users
ALTER TABLE public.users
  ADD COLUMN IF NOT EXISTS join_source  text,
  ADD COLUMN IF NOT EXISTS join_quiz_id uuid REFERENCES public.quizzes(id) ON DELETE SET NULL;

ALTER TABLE public.users DROP CONSTRAINT IF EXISTS users_join_source_check;
ALTER TABLE public.users ADD CONSTRAINT users_join_source_check
  CHECK (join_source IS NULL OR join_source IN ('bot','shared_link','referral','website'));

COMMENT ON COLUMN public.users.join_source IS
  'كيف انضم المستخدم: bot (مباشرة) | shared_link (رابط كويز مشترك) | referral (رابط إحالة) | website (abiad.me). NULL = غير معروف (انضم قبل بدء التتبع 2026-08-30).';
COMMENT ON COLUMN public.users.join_quiz_id IS
  'عند join_source = shared_link: الكويز الذي جاء المستخدم من خلال رابطه.';

CREATE INDEX IF NOT EXISTS users_join_source_joined_at_idx
  ON public.users (join_source, joined_at DESC);
CREATE INDEX IF NOT EXISTS users_join_quiz_id_idx
  ON public.users (join_quiz_id) WHERE join_quiz_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS usage_events_type_created_idx
  ON public.usage_events (event_type, created_at DESC);

-- 2) Backfill للمستخدمين الحاليين
-- الإحالات: مصدر الحقيقة هو users.referred_by (أدق من أحداث joined_via_referral)
UPDATE public.users SET join_source = 'referral'
WHERE referred_by IS NOT NULL AND join_source IS NULL;

-- الرابط المشترك: حدث shared_link_opened خلال 60 ثانية بعد الانضمام
UPDATE public.users u SET join_source = 'shared_link'
WHERE u.join_source IS NULL
  AND u.joined_at >= '2026-08-30'
  AND EXISTS (SELECT 1 FROM public.usage_events e
              WHERE e.user_id = u.user_id AND e.event_type = 'shared_link_opened'
                AND e.created_at BETWEEN u.joined_at AND u.joined_at + interval '60 seconds');

-- الكويز المرتبط: أول quiz_started بأصل shared بعد الانضمام مباشرة
UPDATE public.users u SET join_quiz_id = sub.qid
FROM (
  SELECT DISTINCT ON (u2.user_id) u2.user_id, (e.metadata->>'quiz_id')::uuid AS qid
  FROM public.users u2
  JOIN public.usage_events e ON e.user_id = u2.user_id
   AND e.event_type = 'quiz_started' AND e.metadata->>'origin' = 'shared'
   AND e.metadata->>'quiz_id' ~ '^[0-9a-fA-F-]{36}$'
   AND e.created_at BETWEEN u2.joined_at AND u2.joined_at + interval '120 seconds'
  WHERE u2.join_source = 'shared_link'
  ORDER BY u2.user_id, e.created_at
) sub
WHERE u.user_id = sub.user_id
  AND EXISTS (SELECT 1 FROM public.quizzes q WHERE q.id = sub.qid);

-- الموقع: bot_start جديد بمصدر website
UPDATE public.users u SET join_source = 'website'
WHERE u.join_source IS NULL
  AND EXISTS (SELECT 1 FROM public.usage_events e
              WHERE e.user_id = u.user_id AND e.event_type = 'bot_start'
                AND e.metadata->>'source' = 'website' AND e.metadata->>'status' = 'new'
                AND e.created_at BETWEEN u.joined_at AND u.joined_at + interval '60 seconds');

-- الباقي بعد بدء التتبع = دخول مباشر للبوت. ما قبل التتبع يبقى NULL (غير معروف) عمداً.
UPDATE public.users SET join_source = 'bot'
WHERE join_source IS NULL AND joined_at >= '2026-08-30';

-- 3) دوال الإحصاء (service_role فقط)
CREATE OR REPLACE FUNCTION public.admin_join_stats(p_days int)
RETURNS TABLE(source text, cnt bigint)
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(join_source, 'unknown') AS source, COUNT(*)::bigint
  FROM public.users
  WHERE joined_at >= now() - make_interval(days => p_days)
  GROUP BY 1;
$$;

CREATE OR REPLACE FUNCTION public.admin_joined_users(
  p_source text, p_days int, p_limit int DEFAULT 10, p_offset int DEFAULT 0)
RETURNS TABLE(user_id bigint, username text, first_name text, joined_at timestamptz,
              detail text, total_count bigint)
LANGUAGE sql STABLE AS $$
  SELECT u.user_id, u.username, u.first_name, u.joined_at,
         CASE u.join_source
           WHEN 'shared_link' THEN (SELECT q.source_title FROM public.quizzes q WHERE q.id = u.join_quiz_id)
           WHEN 'referral'    THEN (SELECT COALESCE('@' || NULLIF(r.username, 'Unknown'), r.first_name, r.user_id::text)
                                    FROM public.users r WHERE r.user_id = u.referred_by)
         END AS detail,
         COUNT(*) OVER() AS total_count
  FROM public.users u
  WHERE u.joined_at >= now() - make_interval(days => p_days)
    AND COALESCE(u.join_source, 'unknown') = p_source
  ORDER BY u.joined_at DESC
  LIMIT p_limit OFFSET p_offset;
$$;

-- قائمة الروابط المشتركة: تشغيلات (quiz_started origin=shared) + انضمامات (users.join_quiz_id)
CREATE OR REPLACE FUNCTION public.admin_shared_links_stats(
  p_days int, p_limit int DEFAULT 5, p_offset int DEFAULT 0)
RETURNS TABLE(quiz_id uuid, title text, owner_id bigint, owner_name text,
              runs bigint, unique_runners bigint, joins bigint, total_count bigint)
LANGUAGE sql STABLE AS $$
  WITH runs AS (
    SELECT (e.metadata->>'quiz_id')::uuid AS qid,
           COUNT(*) AS runs, COUNT(DISTINCT e.user_id) AS uniq
    FROM public.usage_events e
    WHERE e.event_type = 'quiz_started' AND e.metadata->>'origin' = 'shared'
      AND e.metadata->>'quiz_id' ~ '^[0-9a-fA-F-]{36}$'
      AND e.created_at >= now() - make_interval(days => p_days)
    GROUP BY 1
  ), joins AS (
    SELECT join_quiz_id AS qid, COUNT(*) AS joins
    FROM public.users
    WHERE join_source = 'shared_link' AND join_quiz_id IS NOT NULL
      AND joined_at >= now() - make_interval(days => p_days)
    GROUP BY 1
  ), merged AS (
    SELECT COALESCE(r.qid, j.qid) AS qid,
           COALESCE(r.runs, 0) AS runs, COALESCE(r.uniq, 0) AS uniq, COALESCE(j.joins, 0) AS joins
    FROM runs r FULL JOIN joins j ON j.qid = r.qid
  )
  SELECT q.id, q.source_title, q.creator_id,
         COALESCE('@' || NULLIF(o.username, 'Unknown'), o.first_name, q.creator_id::text),
         m.runs, m.uniq, m.joins, COUNT(*) OVER()
  FROM merged m
  JOIN public.quizzes q ON q.id = m.qid
  LEFT JOIN public.users o ON o.user_id = q.creator_id
  ORDER BY m.runs DESC, m.joins DESC, q.source_title
  LIMIT p_limit OFFSET p_offset;
$$;

REVOKE ALL ON FUNCTION public.admin_join_stats(int) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_joined_users(text,int,int,int) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.admin_shared_links_stats(int,int,int) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_join_stats(int) TO service_role;
GRANT EXECUTE ON FUNCTION public.admin_joined_users(text,int,int,int) TO service_role;
GRANT EXECUTE ON FUNCTION public.admin_shared_links_stats(int,int,int) TO service_role;

-- 4) ترتيب المُحيلين (قائمة الإحالات)
CREATE OR REPLACE FUNCTION public.admin_referrers_stats(
  p_days int, p_limit int DEFAULT 10, p_offset int DEFAULT 0)
RETURNS TABLE(referrer_id bigint, username text, first_name text,
              referred_count bigint, last_join timestamptz, total_count bigint)
LANGUAGE sql STABLE AS $$
  WITH g AS (
    SELECT referred_by AS rid, COUNT(*) AS c, MAX(joined_at) AS last_join
    FROM public.users
    WHERE referred_by IS NOT NULL
      AND joined_at >= now() - make_interval(days => p_days)
    GROUP BY 1
  )
  SELECT g.rid, r.username, r.first_name, g.c, g.last_join, COUNT(*) OVER()
  FROM g LEFT JOIN public.users r ON r.user_id = g.rid
  ORDER BY g.c DESC, g.last_join DESC
  LIMIT p_limit OFFSET p_offset;
$$;
REVOKE ALL ON FUNCTION public.admin_referrers_stats(int,int,int) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_referrers_stats(int,int,int) TO service_role;
