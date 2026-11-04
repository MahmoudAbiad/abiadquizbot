-- Optional commemorative certificates for the top participants of group quizzes.
-- Certificates are not verification documents and contain no verification code.
ALTER TABLE public.group_quiz_sessions
    ADD COLUMN IF NOT EXISTS certificates_enabled boolean NOT NULL DEFAULT false;

ALTER TABLE public.group_quiz_participants
    ADD COLUMN IF NOT EXISTS last_name text;
