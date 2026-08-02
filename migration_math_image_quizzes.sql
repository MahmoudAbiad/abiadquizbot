-- ============================================================
-- Migration: Math Image Quiz Mode (نمط الكويز المصوّر LaTeX)
-- يضيف الدعم اللازم في قاعدة البيانات لميزة "الكويز المصوّر":
--   1) عمود is_math_quiz على جدول quizzes -> يُستخدم للتقارير/الفلترة
--      الإدارية فقط؛ منطق التشغيل الفعلي يعتمد على مفتاح "is_math" داخل
--      كل سؤال ضمن quiz_data (JSONB) لأنه يسري تلقائياً عبر كل مسارات
--      إعادة الاستخدام (كاش، مفضلة، مشاركة) دون أي تعديل إضافي هناك.
--   2) باكت Storage عام (quiz-images) لتخزين صور الأسئلة الرياضية
--      المُصاغة بـ LaTeX، بحيث تُرسم وتُرفع مرة واحدة فقط لكل سؤال، ثم
--      يُعاد استخدام رابطها العام مباشرة مع bot.send_photo في كل مرة
--      يُشغَّل فيها نفس الكويز المخزّن (كاش) لاحقاً.
-- آمن للتشغيل عدة مرات (IF NOT EXISTS / ON CONFLICT في كل مكان).
-- ============================================================

-- 1) عمود تمييز الكويزات الرياضية على الجدول المركزي
ALTER TABLE quizzes
    ADD COLUMN IF NOT EXISTS is_math_quiz BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_quizzes_is_math_quiz ON quizzes(is_math_quiz);

-- 2) باكت تخزين صور الأسئلة (عام: bot.send_photo يستقبل رابط HTTP مباشرة)
INSERT INTO storage.buckets (id, name, public)
VALUES ('quiz-images', 'quiz-images', true)
ON CONFLICT (id) DO NOTHING;

-- قراءة عامة لأي شخص يملك الرابط (لازمة كي يعرض Telegram الصورة عبر الرابط العام)
DROP POLICY IF EXISTS "Public read access for quiz images" ON storage.objects;
CREATE POLICY "Public read access for quiz images"
ON storage.objects FOR SELECT
USING (bucket_id = 'quiz-images');

-- الرفع/الحذف محصور بمفتاح service_role فقط (المستخدم من طرف سيرفر البوت،
-- عبر SUPABASE_KEY في env)، وليس بأي عميل عام آخر
DROP POLICY IF EXISTS "Service role manages quiz images" ON storage.objects;
CREATE POLICY "Service role manages quiz images"
ON storage.objects FOR ALL
USING (bucket_id = 'quiz-images' AND auth.role() = 'service_role')
WITH CHECK (bucket_id = 'quiz-images' AND auth.role() = 'service_role');
