-- migration_ai_generation_log_thoughts_tokens.sql
-- يضيف عمود thoughts_tokens لجدول ai_generation_log (توكنز "تفكير" موديلات reasoning
-- مثل gemini-3.6-flash - محاسَبة ضمن total_tokens من Google لكنها منفصلة عن output_tokens
-- الظاهر بالرد. راجع helpers/gemini_helper.py::_record_token_usage للتفصيل الكامل).
--
-- ✅ تم تطبيق هذا الملف مباشرة على قاعدة البيانات (مشروع pqawoanhbgvdtbdhnbod) عبر
-- Supabase MCP بتاريخ 2026-09-19. مرفق هنا فقط للتوثيق/إعادة التطبيق على بيئة أخرى
-- (مثلاً بيئة تطوير أو مشروع Supabase مختلف) - لا حاجة لتشغيله يدوياً على نفس المشروع.

ALTER TABLE ai_generation_log
  ADD COLUMN IF NOT EXISTS thoughts_tokens integer NOT NULL DEFAULT 0;

DROP VIEW IF EXISTS daily_ai_token_usage;

CREATE VIEW daily_ai_token_usage AS
SELECT
  date_trunc('day', created_at) AS day,
  provider,
  model_name,
  count(*) AS generations_count,
  sum(questions_count) AS questions_generated,
  sum(input_tokens) AS total_input_tokens,
  sum(output_tokens) AS total_output_tokens,
  sum(thoughts_tokens) AS total_thoughts_tokens,
  sum(total_tokens) AS total_tokens
FROM ai_generation_log
GROUP BY date_trunc('day', created_at), provider, model_name
ORDER BY date_trunc('day', created_at) DESC, model_name;
