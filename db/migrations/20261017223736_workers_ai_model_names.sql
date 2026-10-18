-- Accept Workers AI model identifiers, e.g. @cf/meta/llama-3.1-8b-instruct.
-- Existing provider settings and model ordering are unchanged.
BEGIN;
DO $$
DECLARE fn regprocedure;
BEGIN
 FOREACH fn IN ARRAY ARRAY['public.ai_model_add(text,text,text)'::regprocedure,
                          'public.ai_model_update(bigint,text,text[])'::regprocedure] LOOP
   EXECUTE replace(pg_get_functiondef(fn),
     '^[A-Za-z0-9._/:-]{1,100}$', '^[A-Za-z0-9@._/:-]{1,100}$');
 END LOOP;
END;
$$;
COMMIT;
