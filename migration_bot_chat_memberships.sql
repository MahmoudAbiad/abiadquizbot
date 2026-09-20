-- ============================================================
-- Migration: bot_chat_memberships (بند 3 - دعم القنوات) — راجع group_quiz_decisions.md
--
-- تمت المعالجة فعلياً على القاعدة الحية (project pqawoanhbgvdtbdhnbod) عبر
-- Supabase MCP بطلب صريح من صاحب المشروع، وتم التحقق (أعمدة + RLS مفعّل + صفر
-- policies). هالملف للتوثيق/المرجعية بس، نفس نمط ملفات migration_group_quiz_*.sql.
-- آمن للتشغيل عدة مرات (IF NOT EXISTS).
--
-- الغرض: آخر حالة عضوية معروفة للبوت بكل محادثة (قناة/غروب). بتنكتب حصراً من
-- معالج `my_chat_member` (handlers/group_quiz.py::track_bot_chat_membership)،
-- وبتنقرأ بشاشة "📢 شارك مع قناة" لعرض القنوات اللي البوت أدمن فيها وعنده صلاحية
-- نشر، وكفحص وقائي قبل بدء جلسة بقناة.
--
-- ⚠️ بلا FK على added_by (نفس قرار group_quiz_participants.user_id): اللي رقّى
-- البوت ممكن ما يكون عمل /start بالخاص مع البوت أبداً.
-- RLS مفعّل بلا policies = نفس إعداد جداول الكويز الجماعي الثلاثة (service_role بس).
-- ============================================================

CREATE TABLE IF NOT EXISTS public.bot_chat_memberships (
    chat_id           BIGINT PRIMARY KEY,
    chat_type         TEXT NOT NULL,          -- 'channel' / 'supergroup' / 'group'
    chat_title        TEXT,
    status            TEXT NOT NULL,          -- آخر حالة عضوية معروفة للبوت: 'administrator', 'member', 'left', 'kicked'...
    can_post_messages BOOLEAN,                -- من ChatMemberAdministrator (للقنوات بس)؛ NULL لو مش أدمن
    added_by          BIGINT,                 -- user_id اللي رقّى البوت لأدمن (my_chat_member.from_user لحظة الترقية)
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.bot_chat_memberships ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.bot_chat_memberships IS
  'Last known membership status of the bot in each chat, fed by Telegram my_chat_member updates. Used by the channel group-quiz flow.';
COMMENT ON COLUMN public.bot_chat_memberships.added_by IS
  'user_id who promoted the bot to administrator (only overwritten on a non-admin -> admin transition; no FK by design).';
