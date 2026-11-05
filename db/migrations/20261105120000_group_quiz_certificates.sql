-- شهادات التقدير للأوائل بالكويز الجماعي (المرحلة ب).
-- لم تُطبَّق بعد على أي قاعدة بيانات. جرّبها أولاً على Test_telegramquizbot ثم الإنتاج.
-- آمنة للتشغيل أكثر من مرة. شغّلها قبل أو بعد نشر الكود: الكود يكتشف وجود الأعمدة
-- (cert_schema_ready) ويُخفي الميزة تلقائياً لحد ما تنضاف.

-- إعدادات الشهادة (JSONB واحد): {enabled,title,theme,show_date,show_rank,show_score,issuer,count}
-- القيمة الفارغة {} = غير مفعّلة (الافتراضي).
ALTER TABLE public.group_quiz_sessions
  ADD COLUMN IF NOT EXISTS cert_config jsonb NOT NULL DEFAULT '{}'::jsonb;

-- اسم العائلة من حساب تيليجرام (يُطبع على الشهادة فقط). يُحذف مع الجلسة (ON DELETE CASCADE الموجود).
ALTER TABLE public.group_quiz_participants
  ADD COLUMN IF NOT EXISTS last_name text;
