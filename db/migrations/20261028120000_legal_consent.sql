-- =============================================================================
-- 20261028120000_legal_consent.sql  —  موافقة المستخدمين على شروط الخصوصية وسياسة الخدمة
-- user_consents       : الحالة الحالية لكل مستخدم (pending/accepted/declined + النسخ + التواريخ)
-- user_consent_events : سجل إضافة فقط (append-only) لكل قرار فعلي (يثبت من وافق على أي نسخة ومتى)
-- record_consent      : RPC ذري وidempotent (ضغطات مكررة/متزامنة لا تكتب شيئاً جديداً)
-- mark_consent_prompted: تسجيل أول مرة عُرضت فيها الرسالة (مرة واحدة لكل مستخدم)
-- نسخ الوثائق تُقرأ من app_settings (terms_version / privacy_version). رفع الرقم = طلب موافقة جديدة.
-- لا يوجد FK على users عمداً: المستخدم الجديد يوافق قبل إنشاء حسابه.
-- آمن إعادة التشغيل.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.user_consents (
  user_id           bigint PRIMARY KEY,
  status            text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','declined')),
  terms_version     integer NOT NULL DEFAULT 0,
  privacy_version   integer NOT NULL DEFAULT 0,
  first_prompted_at timestamptz NOT NULL DEFAULT now(),
  decided_at        timestamptz,
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.user_consent_events (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id         bigint NOT NULL,
  decision        text NOT NULL CHECK (decision IN ('accepted','declined')),
  terms_version   integer NOT NULL,
  privacy_version integer NOT NULL,
  decided_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS user_consent_events_user_idx
  ON public.user_consent_events (user_id, decided_at DESC);

-- نفس نهج الجداول الأحدث: RLS مفعّل بلا سياسات (البوت يستخدم service role).
ALTER TABLE public.user_consents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_consent_events ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION public.mark_consent_prompted(p_user_id bigint)
RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
  INSERT INTO public.user_consents (user_id) VALUES (p_user_id) ON CONFLICT (user_id) DO NOTHING;
$$;

CREATE OR REPLACE FUNCTION public.record_consent(
  p_user_id bigint, p_decision text, p_terms_version integer, p_privacy_version integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE cur public.user_consents%rowtype;
BEGIN
  IF p_decision NOT IN ('accepted','declined') THEN RAISE EXCEPTION 'invalid decision'; END IF;
  IF p_terms_version < 1 OR p_privacy_version < 1 THEN RAISE EXCEPTION 'invalid version'; END IF;

  INSERT INTO public.user_consents (user_id) VALUES (p_user_id) ON CONFLICT (user_id) DO NOTHING;
  SELECT * INTO cur FROM public.user_consents WHERE user_id = p_user_id FOR UPDATE;

  -- نفس القرار على نفس النسخ: لا تغيير ولا سطر جديد (يمنع تضخم الجدول بالضغط المتكرر).
  IF cur.status = p_decision AND cur.terms_version = p_terms_version
     AND cur.privacy_version = p_privacy_version THEN
    RETURN jsonb_build_object('changed', false, 'status', cur.status);
  END IF;

  UPDATE public.user_consents
     SET status = p_decision, terms_version = p_terms_version, privacy_version = p_privacy_version,
         decided_at = now(), updated_at = now()
   WHERE user_id = p_user_id;
  INSERT INTO public.user_consent_events (user_id, decision, terms_version, privacy_version)
  VALUES (p_user_id, p_decision, p_terms_version, p_privacy_version);

  RETURN jsonb_build_object('changed', true, 'status', p_decision);
END; $$;

INSERT INTO public.app_settings (key, value, label) VALUES
  ('terms_version', 1, 'نسخة سياسة الخدمة (ارفعها عند تعديل جوهري لطلب موافقة جديدة)'),
  ('privacy_version', 1, 'نسخة شروط الخصوصية (ارفعها عند تعديل جوهري لطلب موافقة جديدة)')
ON CONFLICT (key) DO NOTHING;
