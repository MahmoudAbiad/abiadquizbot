-- Let account deletion remove every directly owned row and dependent group session.
BEGIN;

ALTER TABLE public.quiz_responses
    DROP CONSTRAINT IF EXISTS quiz_responses_user_id_fkey;
ALTER TABLE public.quiz_responses
    ADD CONSTRAINT quiz_responses_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public.users(user_id) ON DELETE CASCADE;

ALTER TABLE public.group_quiz_sessions
    DROP CONSTRAINT IF EXISTS group_quiz_sessions_started_by_fkey;
ALTER TABLE public.group_quiz_sessions
    ADD CONSTRAINT group_quiz_sessions_started_by_fkey
    FOREIGN KEY (started_by) REFERENCES public.users(user_id) ON DELETE CASCADE;

ALTER TABLE public.group_quiz_sessions
    DROP CONSTRAINT IF EXISTS group_quiz_sessions_quiz_id_fkey;
ALTER TABLE public.group_quiz_sessions
    ADD CONSTRAINT group_quiz_sessions_quiz_id_fkey
    FOREIGN KEY (quiz_id) REFERENCES public.quizzes(id) ON DELETE CASCADE;

ALTER TABLE public.group_quiz_chat_locks
    DROP CONSTRAINT IF EXISTS group_quiz_chat_locks_session_id_fkey;
ALTER TABLE public.group_quiz_chat_locks
    ADD CONSTRAINT group_quiz_chat_locks_session_id_fkey
    FOREIGN KEY (session_id) REFERENCES public.group_quiz_sessions(id) ON DELETE CASCADE;

ALTER TABLE public.favorite_quizzes
    DROP CONSTRAINT IF EXISTS favorite_quizzes_section_id_fkey;
ALTER TABLE public.favorite_quizzes
    ADD CONSTRAINT favorite_quizzes_section_id_fkey
    FOREIGN KEY (section_id) REFERENCES public.favorite_quiz_sections(id) ON DELETE CASCADE;

COMMIT;