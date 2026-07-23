-- ============================================================================
-- فهارس اختيارية لتسريع الاستعلامات الجديدة (لوحة الإدارة: fetchall، الطلاب
-- النشطون، إجمالي الأسئلة المحلولة). آمنة 100% للتشغيل على قاعدة بياناتك
-- الحالية مباشرة (IF NOT EXISTS)، لا تحذف ولا تعدّل أي بيانات، فقط تسرّع القراءة.
-- شغّلها من Supabase → SQL Editor. اختيارية وليست إلزامية، لكن يُنصح بها بمجرد
-- ما يكبر عدد الطلاب/الأحداث المسجّلة.
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_quiz_attempts_user_id ON public.quiz_attempts(user_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_created_at ON public.usage_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_events_user_id ON public.usage_events(user_id);
CREATE INDEX IF NOT EXISTS idx_quiz_responses_user_id ON public.quiz_responses(user_id);
CREATE INDEX IF NOT EXISTS idx_quiz_responses_quiz_id ON public.quiz_responses(quiz_id);
