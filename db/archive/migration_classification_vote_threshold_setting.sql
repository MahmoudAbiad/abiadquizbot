-- ============================================================
-- Migration: عتبة تثبيت تصنيف المادة كإعداد قابل للتعديل من لوحة الأدمن
-- قبل هذه الهجرة: CLASSIFICATION_VOTE_THRESHOLD كانت ثابتاً في constants.py منفصلاً
-- تماماً عن p_threshold DEFAULT 3 في دالة vote_on_classification (راجع
-- migration_classification_votes.sql) - مصدرا حقيقة مستقلان بلا أي ربط برمجي بينهما،
-- بالصدفة يحملان نفس الرقم (3) دون أي ضمان بقائهما متطابقين مستقبلاً.
-- بعد هذه الهجرة: القيمة تُقرأ من app_settings (نفس آلية نقاط الترحيب/التجديد اليومي/
-- مكافأة الإحالة الموجودة أصلاً - راجع helpers/settings_helper.py)، وتُمرَّر صراحة لـ
-- RPC عند كل تصويت (راجع helpers/supabase_helper.py::submit_classification_vote) -
-- فأصبح app_settings مصدر الحقيقة الوحيد الفعلي.
-- آمن للتشغيل عدة مرات (ON CONFLICT DO NOTHING - لا يكتب فوق قيمة عدّلها الأدمن يدوياً
-- إن أُعيد تشغيل هذه الهجرة لاحقاً بالخطأ).
-- ============================================================

INSERT INTO app_settings (key, value)
VALUES ('classification_vote_threshold', 3)
ON CONFLICT (key) DO NOTHING;
