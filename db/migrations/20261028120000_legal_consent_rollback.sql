-- Rollback for 20261028120000_legal_consent.sql (يحذف سجلات الموافقة نهائياً!)
DROP FUNCTION IF EXISTS public.record_consent(bigint, text, integer, integer);
DROP FUNCTION IF EXISTS public.mark_consent_prompted(bigint);
DROP TABLE IF EXISTS public.user_consent_events;
DROP TABLE IF EXISTS public.user_consents;
DELETE FROM public.app_settings WHERE key IN ('terms_version','privacy_version');
