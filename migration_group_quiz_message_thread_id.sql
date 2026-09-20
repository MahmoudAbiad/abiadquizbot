-- بند 1.7 (دعم التوبيكس / Forum supergroups) — راجع group_quiz_decisions.md
-- تمت المعالجة فعلياً على قاعدة البيانات الحية (project pqawoanhbgvdtbdhnbod) عبر
-- Supabase MCP وقت التنفيذ. هالملف للتوثيق/المرجعية بس، نفس نمط ملفات
-- migration_group_quiz_*.sql الموجودة بالمشروع.

ALTER TABLE group_quiz_sessions
  ADD COLUMN IF NOT EXISTS message_thread_id BIGINT;

COMMENT ON COLUMN group_quiz_sessions.message_thread_id IS
  'Telegram forum-topic thread id (message_thread_id) captured at session creation, passed back to every send_poll/send_message/send_photo call so the quiz stays inside the same topic it was started from. NULL = ordinary chat/group (no topics), or supergroup without forum mode.';
