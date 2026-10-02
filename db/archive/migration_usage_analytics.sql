-- ============================================================
-- Migration: Usage Analytics & Tracking
-- ينشئ جدولين جديدين لتتبع نمط استخدام الطلاب للبوت:
--   1) usage_events   -> سجل عام لكل حدث (بدء البوت، رفع ملف، مشاركة...)
--   2) quiz_attempts  -> تفاصيل كل محاولة كويز (المصدر، المدة، الإكمال، النتيجة)
-- آمن للتشغيل عدة مرات (IF NOT EXISTS في كل مكان).
-- ============================================================

-- 1) سجل الأحداث العام
CREATE TABLE IF NOT EXISTS usage_events (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_usage_events_user_id    ON usage_events(user_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_event_type ON usage_events(event_type);
CREATE INDEX IF NOT EXISTS idx_usage_events_created_at ON usage_events(created_at);

-- 2) محاولات الكويز (كل مرة يبدأ فيها طالب كويز، بغض النظر إذا أكمله أو لا)
CREATE TABLE IF NOT EXISTS quiz_attempts (
    id BIGSERIAL PRIMARY KEY,
    client_ref TEXT UNIQUE,                 -- معرّف يُنشأ فوراً بجانب البوت (بدون انتظار DB) لضمان عدم تأخير الطالب أبداً
    user_id BIGINT NOT NULL,
    quiz_id UUID REFERENCES quizzes(id) ON DELETE SET NULL,
    source_type TEXT NOT NULL,              -- file | photo | album | text | shared | favorite | cached_file | admin_test
    total_questions INT NOT NULL DEFAULT 0,
    score INT NOT NULL DEFAULT 0,
    is_completed BOOLEAN NOT NULL DEFAULT false,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    duration_seconds INT
);

CREATE INDEX IF NOT EXISTS idx_quiz_attempts_user_id    ON quiz_attempts(user_id);
CREATE INDEX IF NOT EXISTS idx_quiz_attempts_quiz_id    ON quiz_attempts(quiz_id);
CREATE INDEX IF NOT EXISTS idx_quiz_attempts_started_at ON quiz_attempts(started_at);
CREATE INDEX IF NOT EXISTS idx_quiz_attempts_source     ON quiz_attempts(source_type);
CREATE INDEX IF NOT EXISTS idx_quiz_attempts_client_ref ON quiz_attempts(client_ref);

-- ملاحظة: إذا كنت قد شغّلت نسخة سابقة من هذه الهجرة بدون عمود client_ref، هذا السطر يضيفه بأمان الآن:
ALTER TABLE quiz_attempts ADD COLUMN IF NOT EXISTS client_ref TEXT UNIQUE;

-- 3) فيو مساعد اختياري: المستخدمون النشطون يومياً (يمكن استخدامه مباشرة من SQL Editor للفحص اليدوي)
CREATE OR REPLACE VIEW daily_active_users AS
SELECT
    date_trunc('day', created_at) AS day,
    COUNT(DISTINCT user_id)       AS active_users
FROM usage_events
GROUP BY 1
ORDER BY 1 DESC;
