-- Admin-only APIs. Apply to the test database before running the test bot.
BEGIN;
ALTER TABLE public.gift_codes ADD COLUMN archived_at timestamptz,
 ADD COLUMN gifts_revoked_at timestamptz,
 ADD CONSTRAINT gift_codes_inactive_history CHECK
 ((archived_at IS NULL AND gifts_revoked_at IS NULL) OR NOT enabled);
CREATE INDEX point_gifts_source_idx ON public.point_gifts(source, source_key, user_id);

-- Redemption and bulk revocation share a code lock BEFORE taking user locks.
-- Debit/refund keep their existing user -> gift order. No cross-order deadlocks.
CREATE OR REPLACE FUNCTION public.redeem_gift_code(p_user_id bigint, p_code text)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE c public.gift_codes%rowtype; u public.users%rowtype; gid uuid; expiry timestamptz;
BEGIN
 SELECT * INTO c FROM public.gift_codes WHERE code=upper(btrim(p_code));
 IF NOT FOUND THEN RETURN jsonb_build_object('status','invalid'); END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('gift-code:'||c.id::text,0));
 SELECT * INTO u FROM public.users WHERE user_id=p_user_id FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','missing_user'); END IF;
 SELECT * INTO c FROM public.gift_codes WHERE id=c.id FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','invalid'); END IF;
 IF EXISTS(SELECT 1 FROM public.gift_code_redemptions WHERE code_id=c.id AND user_id=p_user_id) THEN
 RETURN jsonb_build_object('status','already_used'); END IF;
 IF NOT c.enabled OR c.archived_at IS NOT NULL OR c.gifts_revoked_at IS NOT NULL THEN
 RETURN jsonb_build_object('status','disabled'); END IF;
 IF c.redeem_expires_at IS NOT NULL AND c.redeem_expires_at<=clock_timestamp() THEN
 RETURN jsonb_build_object('status','expired'); END IF;
 IF c.max_uses IS NOT NULL AND c.used_count>=c.max_uses THEN
 RETURN jsonb_build_object('status','exhausted'); END IF;
 expiry:=CASE WHEN c.gift_valid_days=0 THEN NULL ELSE clock_timestamp()+make_interval(days=>c.gift_valid_days) END;
 INSERT INTO public.point_gifts(user_id,source,source_key,title,original_amount,remaining_amount,expires_at)
 VALUES(p_user_id,'code',c.id::text,c.title,c.points,c.points,expiry) RETURNING id INTO gid;
 INSERT INTO public.gift_code_redemptions(code_id,user_id,username,display_name,gift_id)
 VALUES(c.id,p_user_id,u.username,coalesce(nullif(btrim(concat_ws(' ',u.first_name,u.last_name)),''),u.username,p_user_id::text),gid);
 UPDATE public.gift_codes SET used_count=used_count+1 WHERE id=c.id;
 RETURN jsonb_build_object('status','ok','points',c.points,'expires_at',expiry,'gift_id',gid);
END; $$;

CREATE FUNCTION public.admin_gift_code_summary(p_code_id uuid)
RETURNS jsonb LANGUAGE sql SECURITY INVOKER SET search_path = '' AS $$
 SELECT jsonb_build_object('affected',count(*),'remaining_points',coalesce(sum(remaining_amount),0))
 FROM public.point_gifts WHERE source='code' AND source_key=p_code_id::text AND revoked_at IS NULL
  AND remaining_amount>0 AND (expires_at IS NULL OR expires_at>clock_timestamp());
$$;

CREATE FUNCTION public.admin_gift_code_action(p_code_id uuid,p_action text)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE c public.gift_codes%rowtype; affected integer:=0; amount numeric:=0;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('gift-code:'||p_code_id::text,0));
 IF p_action='revoke_all' THEN
  PERFORM 1 FROM public.users u WHERE EXISTS(SELECT 1 FROM public.point_gifts g
   WHERE g.user_id=u.user_id AND g.source='code' AND g.source_key=p_code_id::text)
   ORDER BY u.user_id FOR UPDATE;
 END IF;
 SELECT * INTO c FROM public.gift_codes WHERE id=p_code_id FOR UPDATE;
 IF NOT FOUND THEN RETURN jsonb_build_object('status','missing'); END IF;
 CASE p_action
 WHEN 'archive' THEN
  UPDATE public.gift_codes SET archived_at=coalesce(archived_at,now()),enabled=false WHERE id=p_code_id;
 WHEN 'restore' THEN
  -- Restoring a list entry never silently reactivates redemption.
  UPDATE public.gift_codes SET archived_at=NULL,enabled=false WHERE id=p_code_id;
 WHEN 'enable' THEN
  IF c.archived_at IS NOT NULL OR c.gifts_revoked_at IS NOT NULL THEN
   RETURN jsonb_build_object('status','cannot_enable'); END IF;
  UPDATE public.gift_codes SET enabled=true WHERE id=p_code_id;
 WHEN 'disable' THEN
  UPDATE public.gift_codes SET enabled=false WHERE id=p_code_id;
 WHEN 'delete' THEN
  IF c.used_count>0 OR EXISTS(SELECT 1 FROM public.gift_code_redemptions WHERE code_id=p_code_id)
   OR EXISTS(SELECT 1 FROM public.point_gifts WHERE source='code' AND source_key=p_code_id::text) THEN
   RETURN jsonb_build_object('status','used'); END IF;
  DELETE FROM public.gift_codes WHERE id=p_code_id;
 WHEN 'revoke_all' THEN
  IF c.gifts_revoked_at IS NOT NULL THEN RETURN jsonb_build_object('status','already_revoked'); END IF;
  SELECT count(*),coalesce(sum(remaining_amount),0) INTO affected,amount FROM public.point_gifts
   WHERE source='code' AND source_key=p_code_id::text AND revoked_at IS NULL
    AND remaining_amount>0 AND (expires_at IS NULL OR expires_at>clock_timestamp());
  UPDATE public.gift_codes SET enabled=false,gifts_revoked_at=now() WHERE id=p_code_id;
  -- Keep amounts/history, including spent gifts. The marker prevents a later
  -- refund from making a revoked campaign's points spendable again.
  UPDATE public.point_gifts SET revoked_at=coalesce(revoked_at,now())
   WHERE source='code' AND source_key=p_code_id::text;
 ELSE RAISE EXCEPTION 'invalid code action';
 END CASE;
 RETURN jsonb_build_object('status','ok','affected',affected,'remaining_points',amount);
END; $$;

CREATE TABLE public.admin_gift_campaigns (
 id uuid PRIMARY KEY, created_by bigint NOT NULL, title text NOT NULL CHECK(length(title) BETWEEN 1 AND 100),
 points numeric(14,2) NOT NULL CHECK(points>0 AND points<=100000),
 valid_days integer NOT NULL CHECK(valid_days BETWEEN 0 AND 3650),
 message text NOT NULL CHECK(length(message) BETWEEN 1 AND 2000),
 status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','running','completed','cancelled')),
 created_at timestamptz NOT NULL DEFAULT now(), confirmed_at timestamptz, completed_at timestamptz,
 report_chat_id bigint, report_message_id bigint, report_done boolean NOT NULL DEFAULT false
);
CREATE TABLE public.admin_gift_recipients (
 campaign_id uuid NOT NULL REFERENCES public.admin_gift_campaigns(id), user_id bigint NOT NULL,
 gift_id uuid REFERENCES public.point_gifts(id) ON DELETE SET NULL,
 grant_status text NOT NULL DEFAULT 'pending' CHECK(grant_status IN ('pending','granted','skipped')),
 notice_status text NOT NULL DEFAULT 'pending' CHECK(notice_status IN ('pending','sending','sent','blocked','failed','uncertain','skipped')),
 claim_token uuid, claimed_at timestamptz, retry_at timestamptz, attempts integer NOT NULL DEFAULT 0,
 last_error text, PRIMARY KEY(campaign_id,user_id)
);
CREATE INDEX admin_gift_recipient_queue_idx ON public.admin_gift_recipients(campaign_id,grant_status,notice_status,user_id);
ALTER TABLE public.admin_gift_campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.admin_gift_recipients ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.admin_gift_campaigns, public.admin_gift_recipients FROM PUBLIC,anon,authenticated;
GRANT ALL ON public.admin_gift_campaigns, public.admin_gift_recipients TO service_role;

CREATE FUNCTION public.admin_gift_campaign_prepare(p_id uuid,p_created_by bigint,p_title text,
 p_points numeric,p_valid_days integer,p_message text)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE c public.admin_gift_campaigns%rowtype; total bigint; fresh boolean;
BEGIN
 IF p_points<>round(p_points,2) THEN RAISE EXCEPTION 'invalid precision'; END IF;
 INSERT INTO public.admin_gift_campaigns(id,created_by,title,points,valid_days,message)
 VALUES(p_id,p_created_by,p_title,p_points,p_valid_days,p_message) ON CONFLICT(id) DO NOTHING;
 fresh:=FOUND;
 SELECT * INTO c FROM public.admin_gift_campaigns WHERE id=p_id FOR UPDATE;
 IF (c.created_by,c.title,c.points,c.valid_days,c.message) IS DISTINCT FROM
    (p_created_by,p_title,p_points,p_valid_days,p_message) THEN RAISE EXCEPTION 'draft mismatch'; END IF;
 -- Insert recipients only at first preparation, never expand an existing preview.
 IF fresh THEN
  INSERT INTO public.admin_gift_recipients(campaign_id,user_id)
  SELECT p_id,user_id FROM public.users WHERE user_id>0;
 END IF;
 SELECT count(*) INTO total FROM public.admin_gift_recipients WHERE campaign_id=p_id;
 RETURN jsonb_build_object('id',p_id,'recipients',total,'total_points',total*c.points,'status',c.status);
END; $$;

CREATE FUNCTION public.admin_gift_campaign_confirm(p_id uuid,p_created_by bigint,p_chat_id bigint,p_message_id bigint)
RETURNS text LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE c public.admin_gift_campaigns%rowtype;
BEGIN
 SELECT * INTO c FROM public.admin_gift_campaigns WHERE id=p_id FOR UPDATE;
 IF NOT FOUND OR c.created_by<>p_created_by THEN RETURN 'missing'; END IF;
 IF c.status='draft' THEN
  IF c.created_at<now()-interval '30 minutes' THEN RETURN 'expired'; END IF;
  IF NOT EXISTS(SELECT 1 FROM public.admin_gift_recipients WHERE campaign_id=p_id) THEN RETURN 'empty'; END IF;
  UPDATE public.admin_gift_campaigns SET status='running',confirmed_at=now(),
   report_chat_id=p_chat_id,report_message_id=p_message_id WHERE id=p_id;
  RETURN 'running';
 END IF;
 RETURN c.status;
END; $$;

CREATE FUNCTION public.admin_gift_campaign_grant_step(p_id uuid,p_limit integer DEFAULT 50)
RETURNS integer LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE c public.admin_gift_campaigns%rowtype; r record; gid uuid; processed integer:=0;
BEGIN
 SELECT * INTO c FROM public.admin_gift_campaigns WHERE id=p_id FOR UPDATE;
 IF NOT FOUND OR c.status<>'running' THEN RETURN 0; END IF;
 FOR r IN SELECT * FROM public.admin_gift_recipients WHERE campaign_id=p_id AND grant_status='pending'
  ORDER BY user_id LIMIT greatest(1,least(p_limit,100)) FOR UPDATE LOOP
  PERFORM 1 FROM public.users WHERE user_id=r.user_id FOR UPDATE;
  IF NOT FOUND THEN
   UPDATE public.admin_gift_recipients SET grant_status='skipped',notice_status='skipped'
    WHERE campaign_id=p_id AND user_id=r.user_id;
  ELSE
   INSERT INTO public.point_gifts(user_id,source,source_key,title,original_amount,remaining_amount,expires_at)
   VALUES(r.user_id,'admin','campaign:'||p_id::text,c.title,c.points,c.points,
    CASE WHEN c.valid_days=0 THEN NULL ELSE clock_timestamp()+make_interval(days=>c.valid_days) END)
    ON CONFLICT(user_id,source,source_key) DO NOTHING;
   SELECT id INTO gid FROM public.point_gifts WHERE user_id=r.user_id AND source='admin' AND source_key='campaign:'||p_id::text;
   UPDATE public.admin_gift_recipients SET grant_status='granted',gift_id=gid
    WHERE campaign_id=p_id AND user_id=r.user_id;
  END IF;
  processed:=processed+1;
 END LOOP;
 RETURN processed;
END; $$;

CREATE FUNCTION public.admin_gift_campaign_claim(p_id uuid,p_limit integer DEFAULT 10)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE r record; output jsonb:='[]'; token uuid;
BEGIN
 IF NOT EXISTS(SELECT 1 FROM public.admin_gift_campaigns WHERE id=p_id AND status='running') THEN RETURN output; END IF;
 -- Telegram has no send idempotency key. Ambiguous crashed sends are reported,
 -- never blindly re-sent. Gifts themselves are already durably granted once.
 UPDATE public.admin_gift_recipients SET notice_status='uncertain',last_error='worker interrupted during send'
  WHERE campaign_id=p_id AND notice_status='sending' AND claimed_at<clock_timestamp()-interval '2 minutes';
 FOR r IN SELECT n.user_id,n.gift_id,g.expires_at FROM public.admin_gift_recipients n
  LEFT JOIN public.point_gifts g ON g.id=n.gift_id
  WHERE n.campaign_id=p_id AND n.grant_status='granted' AND n.notice_status='pending'
   AND (n.retry_at IS NULL OR n.retry_at<=clock_timestamp()) ORDER BY n.user_id
  LIMIT greatest(1,least(p_limit,10)) FOR UPDATE OF n SKIP LOCKED LOOP
  token:=gen_random_uuid();
  UPDATE public.admin_gift_recipients SET notice_status='sending',claim_token=token,
   claimed_at=clock_timestamp(),attempts=attempts+1 WHERE campaign_id=p_id AND user_id=r.user_id;
  output:=output||jsonb_build_array(jsonb_build_object('user_id',r.user_id,'gift_id',r.gift_id,'expires_at',r.expires_at,'token',token));
 END LOOP;
 RETURN output;
END; $$;

CREATE FUNCTION public.admin_gift_campaign_notice(p_id uuid,p_user_id bigint,p_token uuid,p_status text,p_error text DEFAULT '',p_retry integer DEFAULT 0)
RETURNS boolean LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
BEGIN
 IF p_status NOT IN ('sent','blocked','failed','uncertain','pending') THEN RAISE EXCEPTION 'invalid status'; END IF;
 UPDATE public.admin_gift_recipients SET notice_status=CASE WHEN p_status='pending' AND attempts>=5 THEN 'failed' ELSE p_status END,
  retry_at=CASE WHEN p_status='pending' THEN clock_timestamp()+make_interval(secs=>greatest(1,p_retry)) ELSE NULL END,
  last_error=left(p_error,500)
 WHERE campaign_id=p_id AND user_id=p_user_id AND claim_token=p_token AND notice_status='sending';
 RETURN FOUND;
END; $$;

CREATE FUNCTION public.admin_gift_campaign_status(p_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE counts jsonb; c public.admin_gift_campaigns%rowtype;
BEGIN
 SELECT * INTO c FROM public.admin_gift_campaigns WHERE id=p_id FOR UPDATE;
 IF NOT FOUND THEN RETURN NULL; END IF;
 SELECT jsonb_build_object('recipients',count(*),'granted',count(*) FILTER(WHERE grant_status='granted'),
  'grant_pending',count(*) FILTER(WHERE grant_status='pending'),'skipped',count(*) FILTER(WHERE grant_status='skipped'),
  'sent',count(*) FILTER(WHERE notice_status='sent'),'blocked',count(*) FILTER(WHERE notice_status='blocked'),
  'failed',count(*) FILTER(WHERE notice_status='failed'),'uncertain',count(*) FILTER(WHERE notice_status='uncertain'),
  'notice_pending',count(*) FILTER(WHERE notice_status IN ('pending','sending')))
 INTO counts FROM public.admin_gift_recipients WHERE campaign_id=p_id;
 IF c.status='running' AND (counts->>'grant_pending')::bigint=0 AND (counts->>'notice_pending')::bigint=0 THEN
  UPDATE public.admin_gift_campaigns SET status='completed',completed_at=now() WHERE id=p_id RETURNING * INTO c;
 END IF;
 RETURN to_jsonb(c)||counts;
END; $$;

REVOKE ALL ON FUNCTION public.admin_gift_code_summary(uuid),public.admin_gift_code_action(uuid,text),
 public.admin_gift_campaign_prepare(uuid,bigint,text,numeric,integer,text),
 public.admin_gift_campaign_confirm(uuid,bigint,bigint,bigint),public.admin_gift_campaign_grant_step(uuid,integer),
 public.admin_gift_campaign_claim(uuid,integer),public.admin_gift_campaign_notice(uuid,bigint,uuid,text,text,integer),
 public.admin_gift_campaign_status(uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.admin_gift_code_summary(uuid),public.admin_gift_code_action(uuid,text),
 public.admin_gift_campaign_prepare(uuid,bigint,text,numeric,integer,text),
 public.admin_gift_campaign_confirm(uuid,bigint,bigint,bigint),public.admin_gift_campaign_grant_step(uuid,integer),
 public.admin_gift_campaign_claim(uuid,integer),public.admin_gift_campaign_notice(uuid,bigint,uuid,text,text,integer),
 public.admin_gift_campaign_status(uuid) TO service_role;
COMMIT;
