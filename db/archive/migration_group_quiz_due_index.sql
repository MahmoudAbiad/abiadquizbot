-- ============================================================
-- Migration: index لنبضة الكويز الجماعي الدورية (fixed_interval)
-- راجع handlers/group_quiz.py::group_quiz_heartbeat_tick +
-- services/group_quiz_store.py::get_due_sessions - الاستعلام بيتكرر كل ١٢
-- ثانية على مستوى التطبيق كله، فبلا index بيصير full table scan متكرر على
-- group_quiz_sessions.
-- آمن للتشغيل عدة مرات (IF NOT EXISTS).
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_group_quiz_sessions_due
    ON public.group_quiz_sessions (status, pacing_mode, next_question_due_at)
    WHERE status = 'active';
