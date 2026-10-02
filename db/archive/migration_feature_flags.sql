-- ============================================================
-- Migration: Feature Flags (مفاتيح تحكم عامة للأدمن)
-- جدول عام لأي ميزة يحتاج الأدمن يقدر يشغّلها/يوقفها لايف من لوحة تيليجرام بدون أي
-- تعديل كود أو إعادة نشر. القيمة الافتراضية لأي مفتاح غير موجود بالجدول = مُفعَّل
-- (fail-safe: غياب الصف أو فشل الاتصال بقاعدة البيانات لا يعطّل أي ميزة قائمة).
-- راجع helpers/supabase_helper.py (is_feature_enabled/set_feature_flag) +
-- constants.py (FEATURE_FLAGS_REGISTRY) + handlers/admin/feature_flags.py للتفاصيل.
-- آمن للتشغيل عدة مرات (IF NOT EXISTS).
-- ============================================================

CREATE TABLE IF NOT EXISTS feature_flags (
    key TEXT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT true,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
