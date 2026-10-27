-- Preserve newer membership events even when timestamps have equal precision.
ALTER TABLE public.users ADD COLUMN bot_delivery_revision bigint NOT NULL DEFAULT 0;
CREATE OR REPLACE FUNCTION public.daily_renewal_delivery_stamp() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
BEGIN
 NEW.bot_delivery_updated_at:=clock_timestamp();
 NEW.bot_delivery_revision:=OLD.bot_delivery_revision+1;
 RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION public.daily_renewal_finish(p_day date,p_user bigint,p_token uuid,p_status text,p_error text,p_retry integer DEFAULT 0)
RETURNS boolean LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE revision bigint;
BEGIN
 IF p_status NOT IN ('sent','pending','blocked','unavailable','failed','uncertain') THEN RAISE EXCEPTION 'invalid status'; END IF;
 UPDATE public.daily_renewal_deliveries SET status=p_status,error=left(coalesce(p_error,''),500),
 finished_at=CASE WHEN p_status='pending' THEN NULL ELSE now() END
 WHERE day=p_day AND user_id=p_user AND token=p_token AND status='sending'
 RETURNING (user_snapshot->>'bot_delivery_revision')::bigint INTO revision;
 IF NOT FOUND THEN RETURN false; END IF;
 IF p_status='pending' THEN
   UPDATE public.daily_renewal_runs SET not_before=greatest(not_before,now()+make_interval(secs=>greatest(p_retry,1))) WHERE day=p_day;
 ELSIF p_status IN ('blocked','unavailable') THEN
   UPDATE public.users SET bot_delivery_state=p_status WHERE user_id=p_user AND bot_delivery_revision=revision;
 END IF;
 RETURN true;
END; $$;
-- CREATE OR REPLACE preserves the original service-role-only function grants.
