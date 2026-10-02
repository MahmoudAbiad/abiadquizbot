-- 0003: وضع الإرسال "burst" (كل أسئلة الكويز الجماعي دفعة واحدة بفاصل قصير ثابت).
-- شغّله على قاعدة الإنتاج *قبل* نشر الكود الجديد.
ALTER TABLE public.group_quiz_sessions
  DROP CONSTRAINT IF EXISTS group_quiz_sessions_pacing_mode_check;
ALTER TABLE public.group_quiz_sessions
  ADD CONSTRAINT group_quiz_sessions_pacing_mode_check
  CHECK (pacing_mode = ANY (ARRAY['fixed_interval'::text, 'chain_to_timer'::text, 'burst'::text]));
