-- =============================================================================
-- 0002_seed_config.sql  —  بيانات الإعدادات المرجعية فقط (لا بيانات مستخدمين)
-- لقطة من الإنتاج بتاريخ 2026-10-02. آمن إعادة تشغيله (ON CONFLICT DO NOTHING).
-- ملاحظة: أسعار ai_model_pricing كلها verified=false (تقديرية)؛ راجعها من فواتير فعلية.
-- ملاحظة: ترتيب cascade أُعيد ترقيمه بالإنتاج (2026-10-02) ليبدأ من 1 بلا فراغ، مع حفظ الترتيب النسبي.
-- =============================================================================

INSERT INTO public.app_settings (key, value, label) VALUES
  ('classification_vote_threshold', 3,   NULL),
  ('daily_renewal_points',          25,  'نقاط التجديد اليومي المجاني'),
  ('referral_bonus_points',         25,  'مكافأة الإحالة عند نجاح كويز الصديق المُحال'),
  ('welcome_points',                150, 'نقاط الترحيب عند أول تسجيل')
ON CONFLICT (key) DO NOTHING;

INSERT INTO public.feature_flags (key, enabled) VALUES
  ('classification_vote_prompt', false)
ON CONFLICT (key) DO NOTHING;

INSERT INTO public.ai_model_slots (slot, provider, model_name, display_order, is_enabled) VALUES
  ('cascade',   'gemini', 'gemini-3.8-flash',      1, true),
  ('cascade',   'gemini', 'gemini-3.7-flash',      2, true),
  ('cascade',   'gemini', 'gemini-3.6-flash',      3, true),
  ('cascade',   'gemini', 'gemini-3.5-flash',      4, true),
  ('cascade',   'vertex', 'gemini-3.8-flash',      5, true),
  ('cascade',   'gemini', 'gemini-3.5-flash-lite', 6, true),
  ('detection', 'gemini', 'gemini-3.5-flash-lite', 1, true),
  ('groq_fast', 'groq',   'openai/gpt-oss-120b',   1, true)
ON CONFLICT (slot, provider, model_name) DO NOTHING;

INSERT INTO public.ai_model_pricing (provider, model_name, input_price_per_million, output_price_per_million, verified, notes) VALUES
  ('gemini', 'gemini-3.5-flash',      1.5,  9,    false, 'رقم من مصدر واحد غير رسمي بحثت فيه - غير موثوق كفاية، تأكد منه'),
  ('gemini', 'gemini-3.5-flash-lite', 0.3,  2.5,  false, 'مصادر متضاربة بشدة على هالموديل (شفت أرقام من 0.10 لـ0.30) - لازم تتأكد بنفسك'),
  ('gemini', 'gemini-3.6-flash',      0.75, 3.75, false, 'سعر ترويجي معلن من Google لغاية 2026-12-31 - غير مؤكد من فاتورتك'),
  ('gemini', 'gemini-3.7-flash',      0.75, 3.75, false, 'سعر ترويجي معلن من Google لغاية 2026-12-31 - غير مؤكد من فاتورتك'),
  ('gemini', 'gemini-3.8-flash',      0.75, 3.75, false, 'سعر ترويجي معلن من Google لغاية 2026-12-31 (بيتضاعف بعدها) - غير مؤكد من فاتورتك'),
  ('groq',   'openai/gpt-oss-120b',   0.15, 0.75, false, 'سعر Groq الرسمي المتداول - أعلى ثقة نسبياً من باقي الصفوف، بس ما تأكدت من فاتورتك مباشرة'),
  ('vertex', 'gemini-3.8-flash',      0.75, 3.75, false, 'نفس سعر AI Studio تقريباً حسب المصادر - غير مؤكد من فاتورة Vertex الفعلية')
ON CONFLICT (provider, model_name) DO NOTHING;
