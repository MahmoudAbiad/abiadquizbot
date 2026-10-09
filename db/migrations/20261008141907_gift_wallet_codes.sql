-- Additive wallet migration. Existing free/paid balances and RPCs are untouched.
-- Apply before deploying the accompanying bot code. No historical balance conversion.
BEGIN;
CREATE TABLE public.gift_codes (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 code text NOT NULL UNIQUE CHECK (code ~ '^[A-Z0-9-]{12,64}$'),
 title text NOT NULL CHECK (length(title) BETWEEN 1 AND 100),
 points numeric(14,2) NOT NULL CHECK (points > 0 AND points < 100000000),
 max_uses integer CHECK (max_uses BETWEEN 1 AND 1000000),
 used_count integer NOT NULL DEFAULT 0 CHECK (used_count >= 0),
 redeem_expires_at timestamptz,
 gift_valid_days integer NOT NULL DEFAULT 30 CHECK (gift_valid_days BETWEEN 0 AND 3650),
 enabled boolean NOT NULL DEFAULT true,
 created_by bigint NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE public.point_gifts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id bigint NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
 source text NOT NULL CHECK (source IN ('code','welcome','referral','admin')),
 source_key text NOT NULL,
 title text NOT NULL,
 original_amount numeric(14,2) NOT NULL CHECK (original_amount > 0),
 remaining_amount numeric(14,2) NOT NULL CHECK (remaining_amount >= 0 AND remaining_amount <= original_amount),
 granted_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz,
 revoked_at timestamptz,
 UNIQUE(user_id, source, source_key)
);
CREATE INDEX point_gifts_wallet_idx ON public.point_gifts(user_id, expires_at, id) WHERE revoked_at IS NULL AND remaining_amount > 0;
CREATE INDEX point_gifts_history_idx ON public.point_gifts(user_id, granted_at DESC, id);
CREATE TABLE public.gift_code_redemptions (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 code_id uuid NOT NULL REFERENCES public.gift_codes(id),
 user_id bigint NOT NULL,
 username text,
 display_name text NOT NULL,
 gift_id uuid REFERENCES public.point_gifts(id) ON DELETE SET NULL,
 redeemed_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(code_id, user_id)
);
CREATE INDEX gift_code_redemptions_page_idx ON public.gift_code_redemptions(code_id,id);
CREATE INDEX gift_code_redemptions_gift_idx ON public.gift_code_redemptions(gift_id);
CREATE TABLE public.wallet_debits (
 id uuid PRIMARY KEY,
 user_id bigint NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
 free_amount numeric(14,2) NOT NULL,
 paid_amount numeric(14,2) NOT NULL,
 gifts jsonb NOT NULL DEFAULT '[]',
 refunded_ratio numeric NOT NULL DEFAULT 0 CHECK (refunded_ratio BETWEEN 0 AND 1),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX wallet_debits_user_idx ON public.wallet_debits(user_id, created_at);
ALTER TABLE public.gift_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.point_gifts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.gift_code_redemptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.wallet_debits ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.gift_codes, public.point_gifts, public.gift_code_redemptions, public.wallet_debits FROM anon, authenticated;
GRANT ALL ON public.gift_codes, public.point_gifts, public.gift_code_redemptions, public.wallet_debits TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.gift_code_redemptions_id_seq TO service_role;
INSERT INTO public.app_settings(key,value,label) VALUES
 ('welcome_gift_days',30,'صلاحية هدية الترحيب بالأيام (0 = بلا انتهاء)'),
 ('referral_gift_days',0,'صلاحية هدية الإحالة بالأيام (0 = بلا انتهاء)')
ON CONFLICT(key) DO NOTHING;

CREATE FUNCTION public.wallet_gift_balances(p_user_ids bigint[])
RETURNS TABLE(user_id bigint, gift_points numeric)
LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
 SELECT g.user_id, sum(g.remaining_amount) FROM public.point_gifts g
 WHERE g.user_id = ANY(p_user_ids) AND g.remaining_amount > 0 AND g.revoked_at IS NULL
 AND (g.expires_at IS NULL OR g.expires_at > statement_timestamp())
 GROUP BY g.user_id;
$$;

CREATE FUNCTION public.register_user_with_gift(p_user jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE uid bigint := (p_user->>'user_id')::bigint; amount numeric; days integer;
BEGIN
 INSERT INTO public.users(user_id, username, first_name, last_name, free_points, paid_points,
   total_questions, referred_by, last_renewal, join_source, join_quiz_id)
 VALUES(uid,p_user->>'username',p_user->>'first_name',p_user->>'last_name',0,0,0,
   (p_user->>'referred_by')::bigint,(p_user->>'last_renewal')::date,
   p_user->>'join_source',(p_user->>'join_quiz_id')::uuid)
 ON CONFLICT(user_id) DO NOTHING;
 IF NOT FOUND THEN RETURN false; END IF;
 amount := coalesce((SELECT value FROM public.app_settings WHERE key='welcome_points'),150);
 days := coalesce((SELECT value FROM public.app_settings WHERE key='welcome_gift_days'),30);
 IF amount > 0 THEN
 INSERT INTO public.point_gifts(user_id,source,source_key,title,original_amount,remaining_amount,expires_at)
 VALUES(uid,'welcome','welcome','هدية الانضمام',amount,amount,
 CASE WHEN days=0 THEN NULL ELSE now()+make_interval(days=>days) END);
 END IF;
 RETURN true;
END; $$;

CREATE FUNCTION public.redeem_gift_code(p_user_id bigint, p_code text)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE c public.gift_codes%rowtype; u public.users%rowtype; gid uuid; expiry timestamptz;
BEGIN
 -- Global lock order: user, then code; debit/refund/admin gift edits also lock user first.
 SELECT * INTO u FROM public.users WHERE user_id=p_user_id FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','missing_user'); END IF;
 SELECT * INTO c FROM public.gift_codes WHERE code=upper(btrim(p_code)) FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','invalid'); END IF;
 IF EXISTS(SELECT 1 FROM public.gift_code_redemptions WHERE code_id=c.id AND user_id=p_user_id) THEN
 RETURN jsonb_build_object('status','already_used'); END IF;
 IF NOT c.enabled THEN RETURN jsonb_build_object('status','disabled'); END IF;
 IF c.redeem_expires_at IS NOT NULL AND c.redeem_expires_at <= clock_timestamp() THEN
 RETURN jsonb_build_object('status','expired'); END IF;
 IF c.max_uses IS NOT NULL AND c.used_count >= c.max_uses THEN
 RETURN jsonb_build_object('status','exhausted'); END IF;
 expiry := CASE WHEN c.gift_valid_days=0 THEN NULL ELSE clock_timestamp()+make_interval(days=>c.gift_valid_days) END;
 INSERT INTO public.point_gifts(user_id,source,source_key,title,original_amount,remaining_amount,expires_at)
 VALUES(p_user_id,'code',c.id::text,c.title,c.points,c.points,expiry) RETURNING id INTO gid;
 INSERT INTO public.gift_code_redemptions(code_id,user_id,username,display_name,gift_id)
 VALUES(c.id,p_user_id,u.username,coalesce(nullif(btrim(concat_ws(' ',u.first_name,u.last_name)),''),u.username,p_user_id::text),gid);
 UPDATE public.gift_codes SET used_count=used_count+1 WHERE id=c.id;
 RETURN jsonb_build_object('status','ok','points',c.points,'expires_at',expiry,'gift_id',gid);
END; $$;

CREATE FUNCTION public.wallet_debit(p_user_id bigint, p_amount numeric, p_questions integer, p_debit_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE u public.users%rowtype; g record; gifts numeric; left_to_take numeric;
 f numeric; p numeric; take numeric; parts jsonb := '[]'; receipt public.wallet_debits%rowtype;
BEGIN
 IF p_amount IS NULL OR p_amount < 0 OR p_amount >= 100000000 OR p_amount <> round(p_amount,2) THEN
 RAISE EXCEPTION 'invalid amount'; END IF;
 SELECT * INTO u FROM public.users WHERE user_id=p_user_id FOR UPDATE;
 IF NOT FOUND THEN RETURN NULL; END IF;
 SELECT * INTO receipt FROM public.wallet_debits WHERE id=p_debit_id;
 IF FOUND THEN RAISE EXCEPTION 'duplicate debit id'; END IF;
 SELECT coalesce(sum(remaining_amount),0) INTO gifts FROM public.point_gifts
 WHERE user_id=p_user_id AND remaining_amount>0 AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>clock_timestamp());
 IF u.free_points::numeric+u.paid_points::numeric+gifts < p_amount THEN RETURN NULL; END IF;
 f:=least(u.free_points::numeric,p_amount); left_to_take:=p_amount-f;
 FOR g IN SELECT * FROM public.point_gifts WHERE user_id=p_user_id AND revoked_at IS NULL
   AND remaining_amount>0 AND (expires_at IS NULL OR expires_at>clock_timestamp())
   ORDER BY expires_at ASC NULLS LAST, granted_at, id FOR UPDATE
 LOOP
   EXIT WHEN left_to_take<=0;
   take:=least(g.remaining_amount,left_to_take);
   UPDATE public.point_gifts SET remaining_amount=remaining_amount-take WHERE id=g.id;
   parts:=parts||jsonb_build_array(jsonb_build_object('id',g.id,'amount',take));
   left_to_take:=left_to_take-take;
 END LOOP;
 p:=left_to_take;
 -- Expiry may pass between the pre-check and the lock; never overdraw paid points.
 IF p > u.paid_points::numeric THEN RAISE EXCEPTION 'balance expired during debit'; END IF;
 UPDATE public.users SET free_points=free_points-f,paid_points=paid_points-p,
 total_questions=coalesce(total_questions,0)+greatest(coalesce(p_questions,0),0) WHERE user_id=p_user_id;
 INSERT INTO public.wallet_debits(id,user_id,free_amount,paid_amount,gifts) VALUES(p_debit_id,p_user_id,f,p,parts);
 RETURN jsonb_build_object('remaining_points',u.free_points::numeric+u.paid_points::numeric+gifts-p_amount,
 'debited_free',f,'debited_paid',p,'debited_gift',p_amount-f-p,'debit_id',p_debit_id);
END; $$;

CREATE FUNCTION public.wallet_refund(p_user_id bigint, p_debit_id uuid, p_total_ratio numeric DEFAULT 1)
RETURNS boolean LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE d public.wallet_debits%rowtype; g jsonb; delta numeric;
BEGIN
 IF p_total_ratio IS NULL OR p_total_ratio<0 OR p_total_ratio>1 THEN RAISE EXCEPTION 'invalid ratio'; END IF;
 PERFORM 1 FROM public.users WHERE user_id=p_user_id FOR UPDATE;
 IF NOT FOUND THEN RETURN false; END IF;
 SELECT * INTO d FROM public.wallet_debits WHERE id=p_debit_id AND user_id=p_user_id FOR UPDATE;
 IF NOT FOUND THEN RETURN false; END IF;
 IF p_total_ratio<=d.refunded_ratio THEN RETURN true; END IF;
 UPDATE public.users SET
 free_points=free_points+round(d.free_amount*p_total_ratio,2)-round(d.free_amount*d.refunded_ratio,2),
 paid_points=paid_points+round(d.paid_amount*p_total_ratio,2)-round(d.paid_amount*d.refunded_ratio,2)
 WHERE user_id=p_user_id;
 FOR g IN SELECT * FROM jsonb_array_elements(d.gifts) LOOP
 delta:=round((g->>'amount')::numeric*p_total_ratio,2)-round((g->>'amount')::numeric*d.refunded_ratio,2);
 -- Preserve the original expiry/revocation; refunds cannot resurrect expired gifts.
 UPDATE public.point_gifts SET remaining_amount=remaining_amount+delta
 WHERE id=(g->>'id')::uuid AND user_id=p_user_id;
 END LOOP;
 UPDATE public.wallet_debits SET refunded_ratio=p_total_ratio WHERE id=p_debit_id;
 RETURN true;
END; $$;

CREATE FUNCTION public.reward_referral_gift(p_user_id bigint)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE rid bigint; amount numeric; days integer;
BEGIN
 SELECT referred_by INTO rid FROM public.users WHERE user_id=p_user_id;
 IF rid IS NULL OR rid=p_user_id THEN RETURN NULL; END IF;
 -- Sorted locks avoid crossed-referral deadlocks.
 PERFORM 1 FROM public.users WHERE user_id IN (p_user_id,rid) ORDER BY user_id FOR UPDATE;
 IF NOT EXISTS(SELECT 1 FROM public.users WHERE user_id=rid) OR
 NOT EXISTS(SELECT 1 FROM public.usage_events WHERE user_id=p_user_id AND event_type='quiz_generated') THEN RETURN NULL; END IF;
 UPDATE public.users SET referral_reward_awarded=true WHERE user_id=p_user_id AND referral_reward_awarded=false;
 IF NOT FOUND THEN RETURN NULL; END IF;
 amount:=coalesce((SELECT value FROM public.app_settings WHERE key='referral_bonus_points'),25);
 days:=coalesce((SELECT value FROM public.app_settings WHERE key='referral_gift_days'),0);
 IF amount>0 THEN
 INSERT INTO public.point_gifts(user_id,source,source_key,title,original_amount,remaining_amount,expires_at)
 VALUES(rid,'referral',p_user_id::text,'هدية دعوة صديق',amount,amount,
 CASE WHEN days=0 THEN NULL ELSE now()+make_interval(days=>days) END);
 END IF;
 RETURN jsonb_build_object('referrer_id',rid,'points',amount);
END; $$;

CREATE FUNCTION public.admin_edit_point_gift(p_user_id bigint,p_gift_id uuid,p_action text,p_expires_at timestamptz DEFAULT NULL)
RETURNS boolean LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
BEGIN
 PERFORM 1 FROM public.users WHERE user_id=p_user_id FOR UPDATE;
 IF p_action='revoke' THEN
 UPDATE public.point_gifts SET revoked_at=coalesce(revoked_at,now()) WHERE id=p_gift_id AND user_id=p_user_id;
 ELSIF p_action='expiry' THEN
 IF p_expires_at IS NOT NULL AND p_expires_at<=now() THEN RAISE EXCEPTION 'expiry must be in future'; END IF;
 UPDATE public.point_gifts SET expires_at=p_expires_at WHERE id=p_gift_id AND user_id=p_user_id AND revoked_at IS NULL;
 ELSE RAISE EXCEPTION 'invalid action'; END IF;
 RETURN FOUND;
END; $$;

-- Restrict only the new API. Existing RPC permissions are deliberately unchanged.
REVOKE ALL ON FUNCTION public.wallet_gift_balances(bigint[]), public.register_user_with_gift(jsonb),
 public.redeem_gift_code(bigint,text), public.wallet_debit(bigint,numeric,integer,uuid),
 public.wallet_refund(bigint,uuid,numeric), public.reward_referral_gift(bigint),
 public.admin_edit_point_gift(bigint,uuid,text,timestamptz) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.wallet_gift_balances(bigint[]), public.register_user_with_gift(jsonb),
 public.redeem_gift_code(bigint,text), public.wallet_debit(bigint,numeric,integer,uuid),
 public.wallet_refund(bigint,uuid,numeric), public.reward_referral_gift(bigint),
 public.admin_edit_point_gift(bigint,uuid,text,timestamptz) TO service_role;
COMMIT;
