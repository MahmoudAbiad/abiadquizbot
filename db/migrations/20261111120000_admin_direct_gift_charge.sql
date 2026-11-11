-- Additive migration: atomic direct gift grant for single-user admin messages.
BEGIN;

CREATE OR REPLACE FUNCTION public.admin_grant_user_gift(
  p_user_id bigint,
  p_source_key text,
  p_title text,
  p_points numeric,
  p_valid_days integer
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $$
DECLARE
  u public.users%rowtype;
  g public.point_gifts%rowtype;
  expiry timestamptz;
  gifts_total numeric;
BEGIN
  IF p_points IS NULL OR p_points <= 0 OR p_points > 1000000 OR p_points <> round(p_points, 2) THEN
    RAISE EXCEPTION 'invalid points amount';
  END IF;
  IF p_valid_days IS NULL OR p_valid_days < 0 OR p_valid_days > 3650 THEN
    RAISE EXCEPTION 'invalid valid_days';
  END IF;
  IF p_title IS NULL OR length(btrim(p_title)) NOT BETWEEN 1 AND 100 THEN
    RAISE EXCEPTION 'invalid title';
  END IF;
  IF p_source_key IS NULL OR length(btrim(p_source_key)) NOT BETWEEN 1 AND 120 THEN
    RAISE EXCEPTION 'invalid source_key';
  END IF;

  SELECT * INTO u FROM public.users WHERE user_id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN jsonb_build_object('status', 'missing_user');
  END IF;

  expiry := CASE WHEN p_valid_days = 0 THEN NULL ELSE clock_timestamp() + make_interval(days => p_valid_days) END;

  INSERT INTO public.point_gifts(
    user_id, source, source_key, title, original_amount, remaining_amount, expires_at
  )
  VALUES (
    p_user_id, 'admin', btrim(p_source_key), btrim(p_title), p_points, p_points, expiry
  )
  ON CONFLICT (user_id, source, source_key) DO NOTHING;

  SELECT * INTO g
  FROM public.point_gifts
  WHERE user_id = p_user_id AND source = 'admin' AND source_key = btrim(p_source_key);

  SELECT coalesce(sum(remaining_amount), 0) INTO gifts_total
  FROM public.point_gifts
  WHERE user_id = p_user_id
    AND remaining_amount > 0
    AND revoked_at IS NULL
    AND (expires_at IS NULL OR expires_at > clock_timestamp());

  RETURN jsonb_build_object(
    'status', 'ok',
    'gift_id', g.id,
    'title', g.title,
    'points', g.original_amount,
    'expires_at', g.expires_at,
    'gift_points', gifts_total,
    'total_points', coalesce(u.free_points, 0)::numeric + coalesce(u.paid_points, 0)::numeric + gifts_total
  );
END;
$$;

REVOKE ALL ON FUNCTION public.admin_grant_user_gift(bigint, text, text, numeric, integer) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.admin_grant_user_gift(bigint, text, text, numeric, integer) TO service_role;

COMMIT;
