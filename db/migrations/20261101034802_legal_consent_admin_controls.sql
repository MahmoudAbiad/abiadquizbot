-- Atomic admin bump for the terms/privacy version counters.
BEGIN;

CREATE OR REPLACE FUNCTION public.bump_legal_document_version(p_document_type text)
RETURNS integer
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $$
DECLARE
    setting_key text;
    document_title text;
    document_url text;
    current_version integer;
    next_version integer;
BEGIN
    IF p_document_type = 'terms' THEN
        setting_key := 'terms_version';
        document_title := 'شروط الاستخدام';
        document_url := 'https://abiad.me/terms-of-service.html';
    ELSIF p_document_type = 'privacy' THEN
        setting_key := 'privacy_version';
        document_title := 'سياسة الخصوصية';
        document_url := 'https://abiad.me/privacy-policy.html';
    ELSE
        RAISE EXCEPTION 'invalid legal document type';
    END IF;

    SELECT value::integer
      INTO current_version
      FROM public.app_settings
     WHERE key = setting_key
     FOR UPDATE;

    IF NOT FOUND OR current_version < 1 THEN
        RAISE EXCEPTION 'legal document version setting is missing or invalid';
    END IF;

    next_version := current_version + 1;

    UPDATE public.app_settings
       SET value = next_version::double precision,
           updated_at = now()
     WHERE key = setting_key;

    INSERT INTO public.legal_documents(
        document_type, version, title, url, source_updated_on
    )
    VALUES (
        p_document_type,
        next_version,
        document_title,
        document_url,
        (now() AT TIME ZONE 'Asia/Damascus')::date
    )
    ON CONFLICT (document_type, version) DO UPDATE SET
        title = EXCLUDED.title,
        url = EXCLUDED.url,
        source_updated_on = EXCLUDED.source_updated_on;

    RETURN next_version;
END;
$$;

REVOKE ALL ON FUNCTION public.bump_legal_document_version(text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.bump_legal_document_version(text) TO service_role;

COMMIT;
