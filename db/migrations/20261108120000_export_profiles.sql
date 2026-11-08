-- إعدادات التصدير الدائمة لكل مستخدم («حفظ كإعداداتي»): اسم الأستاذ، الصف، الأعمدة، المسودة، ...
-- لا تُخزَّن هنا إعدادات خاصة بكويز بعينه (العنوان/المادة/التاريخ/المدة/العلامة/عدد الأسئلة بالصفحة).
-- لم تُطبَّق بعد على أي قاعدة بيانات. جرّبها أولاً على قاعدة الاختبار ثم الإنتاج.
-- آمنة للتشغيل أكثر من مرة. الكود يتحمّل غياب الجدول (يعمل بإعدادات الجلسة فقط ويعرض تعذّر الحفظ).
BEGIN;

CREATE TABLE IF NOT EXISTS public.export_profiles (
    user_id    bigint PRIMARY KEY REFERENCES public.users(user_id) ON DELETE CASCADE,
    profile    jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT export_profiles_profile_is_object CHECK (jsonb_typeof(profile) = 'object'),
    CONSTRAINT export_profiles_profile_size CHECK (pg_column_size(profile) <= 4096)
);

-- RLS مفعّل بلا policies: الوصول من البوت فقط عبر service_role (يتجاوز RLS)، ولا وصول لـ anon/authenticated.
ALTER TABLE public.export_profiles ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.export_profiles FROM anon, authenticated;

COMMIT;
