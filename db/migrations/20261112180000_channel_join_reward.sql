-- =============================================================================
-- 20261112180000_channel_join_reward.sql
-- 1) تخفيض هدية انضمام المستخدم الجديد من 150 إلى 100 نقطة.
-- 2) إضافة إعداد مكافأة الاشتراك في قناة إعلامات البوت (50 نقطة لمرة واحدة).
-- 3) دالة ذرية claim_channel_join_gift تمنح 50 نقطة لمرة واحدة فقط لكل مستخدم.
-- =============================================================================

UPDATE public.app_settings
SET value = 100, updated_at = now()
WHERE key = 'welcome_points';

INSERT INTO public.app_settings (key, value, label)
VALUES ('channel_join_bonus_points', 50, 'مكافأة الاشتراك في قناة إعلامات البوت (لمرة واحدة)')
ON CONFLICT (key) DO UPDATE
SET value = EXCLUDED.value, label = EXCLUDED.label, updated_at = now();

CREATE OR REPLACE FUNCTION public.register_user_with_gift(p_user jsonb)
 RETURNS boolean
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
DECLARE uid bigint := (p_user->>'user_id')::bigint; amount numeric; days integer;
BEGIN
 INSERT INTO public.users(user_id, username, first_name, last_name, free_points, paid_points,
   total_questions, referred_by, last_renewal, join_source, join_quiz_id)
 VALUES(uid,p_user->>'username',p_user->>'first_name',p_user->>'last_name',0,0,0,
   (p_user->>'referred_by')::bigint,public.daily_renewal_cycle_day(),
   p_user->>'join_source',(p_user->>'join_quiz_id')::uuid)
 ON CONFLICT(user_id) DO NOTHING;
 IF NOT FOUND THEN RETURN false; END IF;
 amount := coalesce((SELECT value FROM public.app_settings WHERE key='welcome_points'),100);
 days := coalesce((SELECT value FROM public.app_settings WHERE key='welcome_gift_days'),30);
 IF amount > 0 THEN
 INSERT INTO public.point_gifts(user_id,source,source_key,title,original_amount,remaining_amount,expires_at)
 VALUES(uid,'welcome','welcome','هدية الانضمام',amount,amount,
 CASE WHEN days=0 THEN NULL ELSE now()+make_interval(days=>days) END);
 END IF;
 RETURN true;
END; $function$;

CREATE OR REPLACE FUNCTION public.claim_channel_join_gift(p_user_id bigint)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO ''
AS $function$
DECLARE
  v_amount numeric;
  v_days integer;
  v_expires timestamptz;
  v_gift_id uuid;
BEGIN
  PERFORM 1 FROM public.users WHERE user_id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN
    RETURN jsonb_build_object('status', 'missing_user');
  END IF;

  IF EXISTS (
    SELECT 1 FROM public.point_gifts
    WHERE user_id = p_user_id AND source = 'welcome' AND source_key = 'channel_join'
  ) THEN
    RETURN jsonb_build_object('status', 'already_claimed');
  END IF;

  v_amount := coalesce((SELECT value FROM public.app_settings WHERE key = 'channel_join_bonus_points'), 50);
  v_days := coalesce((SELECT value FROM public.app_settings WHERE key = 'welcome_gift_days'), 30);
  IF v_amount <= 0 THEN
    v_amount := 50;
  END IF;
  v_expires := CASE WHEN v_days = 0 THEN NULL ELSE now() + make_interval(days => v_days) END;

  INSERT INTO public.point_gifts(
    user_id, source, source_key, title, original_amount, remaining_amount, expires_at
  )
  VALUES (
    p_user_id, 'welcome', 'channel_join', 'هدية الاشتراك في قناة البوت', v_amount, v_amount, v_expires
  )
  ON CONFLICT (user_id, source, source_key) DO NOTHING
  RETURNING id INTO v_gift_id;

  IF v_gift_id IS NULL THEN
    RETURN jsonb_build_object('status', 'already_claimed');
  END IF;

  RETURN jsonb_build_object(
    'status', 'ok',
    'gift_id', v_gift_id,
    'points', v_amount,
    'expires_at', v_expires
  );
END;
$function$;

REVOKE ALL ON FUNCTION public.claim_channel_join_gift(bigint) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_channel_join_gift(bigint) TO service_role;
