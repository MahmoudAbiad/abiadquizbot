-- Rollback لـ 0008: يحذف الكائنات الجديدة فقط. ⚠️ يحذف أي اختبارات تحديات أُضيفت (خذ تصدير أولاً).
DROP VIEW IF EXISTS public.challenge_playable_units;
DROP FUNCTION IF EXISTS public.challenge_import_test(uuid,text,text,text,text,bigint,jsonb,jsonb);
DROP TABLE IF EXISTS public.challenge_questions;
DROP TABLE IF EXISTS public.challenge_tests;
DROP TABLE IF EXISTS public.challenge_units;
DROP TABLE IF EXISTS public.challenge_subjects;
DROP TABLE IF EXISTS public.challenge_branches;
DELETE FROM public.feature_flags WHERE key = 'challenge_system_enabled';
