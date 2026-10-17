BEGIN;

CREATE TABLE public.ai_providers (
    id text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9-]{0,23}$'),
    label text NOT NULL CHECK (length(trim(label)) BETWEEN 1 AND 80),
    transport text NOT NULL CHECK (transport IN ('gemini', 'vertex', 'openai_compatible')),
    base_url text,
    api_keys_env text CHECK (api_keys_env ~ '^[A-Z][A-Z0-9_]{0,99}$'),
    is_enabled boolean NOT NULL DEFAULT true,
    CHECK ((transport = 'openai_compatible' AND base_url IS NOT NULL AND base_url ~ '^https://[^/?#@]+(/[^?#]*)?$'
            AND api_keys_env IS NOT NULL)
        OR (transport = 'gemini' AND base_url IS NULL AND api_keys_env IS NOT NULL)
        OR (transport = 'vertex' AND base_url IS NULL AND api_keys_env IS NULL))
);
ALTER TABLE public.ai_providers ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.ai_providers FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.ai_providers TO service_role;

-- Preserve existing provider identifiers and model ordering.
INSERT INTO public.ai_providers (id, label, transport, base_url, api_keys_env) VALUES
 ('gemini', 'Gemini (Google AI Studio)', 'gemini', NULL, 'GEMINI_API_KEYS'),
 ('vertex', 'Vertex AI', 'vertex', NULL, NULL),
 ('groq', 'Groq', 'openai_compatible', 'https://api.groq.com/openai/v1', 'GROQ_API_KEY'),
 ('openai', 'OpenAI', 'openai_compatible', 'https://api.openai.com/v1', 'OPENAI_API_KEY');

ALTER TABLE public.ai_model_slots DROP CONSTRAINT ai_model_slots_provider_check;
ALTER TABLE public.ai_model_slots ADD CONSTRAINT ai_model_slots_provider_fkey
 FOREIGN KEY (provider) REFERENCES public.ai_providers(id) ON DELETE CASCADE;
ALTER TABLE public.ai_model_slots ADD COLUMN capabilities text[] NOT NULL DEFAULT ARRAY['text']::text[];
ALTER TABLE public.ai_model_slots ADD CONSTRAINT ai_model_slots_capabilities_check
 CHECK ('text' = ANY(capabilities) AND capabilities <@ ARRAY['text','image','pdf','audio']::text[]);
UPDATE public.ai_model_slots SET capabilities = ARRAY['text','image','pdf','audio']
 WHERE provider IN ('gemini','vertex');
UPDATE public.ai_model_slots SET capabilities = ARRAY['text','image']
 WHERE provider = 'groq' AND slot = 'detection';
-- Make the existing implicit Gemini -> Groq vision fallback visible/editable.
INSERT INTO public.ai_model_slots (slot,provider,model_name,display_order,is_enabled,capabilities)
SELECT 'detection','groq','qwen/qwen3.8-27b',coalesce(max(display_order),0)+1,true,ARRAY['text','image']
FROM public.ai_model_slots WHERE slot='detection'
HAVING NOT EXISTS (SELECT 1 FROM public.ai_model_slots WHERE slot='detection' AND provider='groq');


-- Former RPC hard-coded the list of companies and omitted the groq_fast slot.
CREATE OR REPLACE FUNCTION public.ai_model_add(p_slot text, p_provider text, p_model_name text)
RETURNS SETOF public.ai_model_slots LANGUAGE plpgsql SECURITY INVOKER
SET search_path = '' AS $$
DECLARE v_transport text;
BEGIN
 IF p_slot IS NULL OR p_slot NOT IN ('cascade','detection','groq_fast') THEN
   RAISE EXCEPTION 'Unknown slot';
 END IF;
 SELECT transport INTO v_transport FROM public.ai_providers WHERE id = p_provider;
 IF v_transport IS NULL THEN RAISE EXCEPTION 'Unknown provider'; END IF;
 IF p_model_name IS NULL OR trim(p_model_name) !~ '^[A-Za-z0-9._/:-]{1,100}$' THEN
   RAISE EXCEPTION 'Invalid model name';
 END IF;
 -- Serialize ordering edits, including the first row of an empty slot.
 PERFORM pg_advisory_xact_lock(hashtext('ai_model_slots:' || p_slot));
 RETURN QUERY INSERT INTO public.ai_model_slots
   (slot, provider, model_name, display_order, is_enabled, capabilities)
 SELECT p_slot, p_provider, trim(p_model_name), coalesce(max(display_order),0)+1, true,
   CASE WHEN v_transport IN ('gemini','vertex') THEN ARRAY['text','image','pdf','audio']
        ELSE ARRAY['text'] END
 FROM public.ai_model_slots WHERE slot = p_slot RETURNING *;
END;
$$;

CREATE FUNCTION public.ai_model_update(p_id bigint, p_model_name text, p_capabilities text[])
RETURNS SETOF public.ai_model_slots LANGUAGE plpgsql SECURITY INVOKER
SET search_path = '' AS $$
DECLARE v_transport text;
BEGIN
 IF p_model_name IS NULL OR trim(p_model_name) !~ '^[A-Za-z0-9._/:-]{1,100}$' THEN
   RAISE EXCEPTION 'Invalid model name';
 END IF;
 SELECT p.transport INTO v_transport FROM public.ai_model_slots m
 JOIN public.ai_providers p ON p.id = m.provider WHERE m.id = p_id;
 IF v_transport IS NULL THEN RAISE EXCEPTION 'Unknown model'; END IF;
 IF p_capabilities IS NULL OR NOT ('text' = ANY(p_capabilities)) OR
    NOT p_capabilities <@ ARRAY['text','image','pdf','audio']::text[] OR
    (v_transport = 'openai_compatible' AND NOT p_capabilities <@ ARRAY['text','image']::text[]) THEN
   RAISE EXCEPTION 'Invalid model capabilities for transport';
 END IF;
 RETURN QUERY UPDATE public.ai_model_slots SET model_name = trim(p_model_name),
    capabilities = p_capabilities, updated_at = now() WHERE id = p_id RETURNING *;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.ai_model_add(text,text,text) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.ai_model_update(bigint,text,text[]) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.ai_model_add(text,text,text) TO service_role;
GRANT EXECUTE ON FUNCTION public.ai_model_update(bigint,text,text[]) TO service_role;
-- SECURITY INVOKER needs explicit table/sequence privileges; public roles get none.
GRANT SELECT, INSERT, UPDATE, DELETE ON public.ai_model_slots TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.ai_model_slots_id_seq TO service_role;

COMMIT;
