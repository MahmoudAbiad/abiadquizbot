-- 0004: عدد الأوائل المعروضين في الترتيب النهائي للكويز الجماعي (1-20، الافتراضي 20 = السلوك القديم).
-- طُبّقت على قاعدة الإنتاج (telegramquizbot) عبر Supabase MCP بتاريخ 2026-10-02.
-- شغّلها *قبل* نشر الكود الجديد. آمنة للتشغيل أكثر من مرة.
ALTER TABLE public.group_quiz_sessions
  ADD COLUMN IF NOT EXISTS top_n integer NOT NULL DEFAULT 20;
ALTER TABLE public.group_quiz_sessions
  DROP CONSTRAINT IF EXISTS group_quiz_sessions_top_n_check;
ALTER TABLE public.group_quiz_sessions
  ADD CONSTRAINT group_quiz_sessions_top_n_check CHECK (top_n BETWEEN 1 AND 20);
