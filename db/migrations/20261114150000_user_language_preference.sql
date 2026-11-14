-- =============================================================================
-- 20261114150000_user_language_preference.sql
-- دعم تفضيل اللغة للمستخدمين (ar, en, tr, fr, de, fa) في جدول users.
-- حقل اختياري بقيمة افتراضية 'ar' متوافق بالكامل مع الكود الحالي.
-- =============================================================================
BEGIN;

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS language text NOT NULL DEFAULT 'ar'
    CHECK (language IN ('ar', 'en', 'tr', 'fr', 'de', 'fa'));

COMMENT ON COLUMN public.users.language IS
    'لغة واجهة المستخدم المفضلة في البوت (ar, en, tr, fr, de, fa).';

COMMIT;
