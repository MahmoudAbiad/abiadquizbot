-- «حفل التكريم» للكويز الجماعي: صورة/فيديو بلهب + أسماء الأوائل + أغنية.
-- لم تُطبَّق بعد على أي قاعدة بيانات. جرّبها أولاً على Test_telegramquizbot ثم الإنتاج.
-- آمنة للتشغيل أكثر من مرة. شغّلها قبل أو بعد نشر الكود: الكود يكتشف وجود العمود
-- (ceremony_schema_ready) ويُخفي الميزة تلقائياً لحد ما ينضاف.

-- إعدادات الحفل (JSONB واحد): {enabled,title,count,format,music}
-- القيمة الفارغة {} = غير مفعّل (الافتراضي). ما بيحتاج last_name (بيستفيد منه لو موجود من migration الشهادات).
ALTER TABLE public.group_quiz_sessions
  ADD COLUMN IF NOT EXISTS ceremony_config jsonb NOT NULL DEFAULT '{}'::jsonb;
