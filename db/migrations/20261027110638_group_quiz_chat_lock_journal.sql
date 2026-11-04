-- Additive, idempotent. Apply before deploying the matching application code.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
ALTER TABLE public.group_quiz_sessions
  ADD COLUMN IF NOT EXISTS lock_chat boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS public.group_quiz_chat_locks (
  chat_id bigint PRIMARY KEY CHECK (chat_id < 0),
  session_id uuid NOT NULL UNIQUE REFERENCES public.group_quiz_sessions(id) ON DELETE RESTRICT,
  original_permissions jsonb NOT NULL CHECK (jsonb_typeof(original_permissions) = 'object'),
  locked_permissions jsonb NOT NULL CHECK (jsonb_typeof(locked_permissions) = 'object'),
  state text NOT NULL DEFAULT 'prepared' CHECK (state IN ('prepared', 'locked')),
  created_at timestamptz NOT NULL DEFAULT now()
);
-- A pending unlock must survive cleanup, restarts and lost API responses.
-- Internal bot journal only: no client-facing policies or public grants.
ALTER TABLE public.group_quiz_chat_locks ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.group_quiz_chat_locks FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.group_quiz_chat_locks TO service_role;
COMMENT ON TABLE public.group_quiz_chat_locks IS
  'Internal durable quiz permission journal. Delete only after confirmed Telegram restoration.';
NOTIFY pgrst, 'reload schema';
COMMIT;
