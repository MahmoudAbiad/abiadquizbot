-- Daily free balance renewal and durable notification outbox. No messages are
-- sent by this migration. First scheduled run is the next Damascus midnight.
ALTER TABLE public.users
 ADD COLUMN renewal_notifications boolean NOT NULL DEFAULT true,
 ADD COLUMN bot_delivery_state text NOT NULL DEFAULT 'reachable'
   CHECK (bot_delivery_state IN ('reachable','blocked','unavailable')),
 ADD COLUMN bot_delivery_updated_at timestamptz NOT NULL DEFAULT now();

CREATE TABLE public.daily_renewal_config (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 start_day date NOT NULL
);
INSERT INTO public.daily_renewal_config(start_day)
VALUES ((now() AT TIME ZONE 'Asia/Damascus')::date + 1);
CREATE TABLE public.daily_renewal_runs (
 day date PRIMARY KEY,
 amount double precision NOT NULL CHECK(amount>=0),
 created_at timestamptz NOT NULL DEFAULT now(),
 not_before timestamptz NOT NULL DEFAULT now(),
 report_status text NOT NULL DEFAULT 'pending'
   CHECK(report_status IN ('pending','sending','sent','uncertain')),
 report_token uuid,
 report_claimed_at timestamptz,
 report_not_before timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE public.daily_renewal_deliveries (
 day date NOT NULL REFERENCES public.daily_renewal_runs(day),
 user_id bigint NOT NULL,
 user_snapshot jsonb NOT NULL,
 status text NOT NULL CHECK(status IN ('pending','sending','sent','disabled','excluded',
   'skipped','blocked','unavailable','failed','uncertain','expired')),
 error text NOT NULL DEFAULT '',
 token uuid,
 claimed_at timestamptz,
 finished_at timestamptz,
 PRIMARY KEY(day,user_id)
);
CREATE INDEX daily_renewal_pending ON public.daily_renewal_deliveries(day,user_id) WHERE status='pending';
CREATE INDEX daily_renewal_failures ON public.daily_renewal_deliveries(day DESC,user_id)
 WHERE status IN ('blocked','unavailable','failed','uncertain','expired');
CREATE INDEX users_bot_delivery_state ON public.users(bot_delivery_state) WHERE bot_delivery_state<>'reachable';
ALTER TABLE public.daily_renewal_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_renewal_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_renewal_deliveries ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.daily_renewal_config,public.daily_renewal_runs,public.daily_renewal_deliveries FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON public.daily_renewal_config,public.daily_renewal_runs,public.daily_renewal_deliveries TO service_role;

CREATE FUNCTION public.daily_renewal_delivery_stamp() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
BEGIN NEW.bot_delivery_updated_at:=clock_timestamp(); RETURN NEW; END; $$;
CREATE TRIGGER daily_renewal_delivery_stamp BEFORE UPDATE OF bot_delivery_state ON public.users
FOR EACH ROW EXECUTE FUNCTION public.daily_renewal_delivery_stamp();

CREATE FUNCTION public.daily_renewal_begin() RETURNS date
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE d date := (now() AT TIME ZONE 'Asia/Damascus')::date; a double precision; inserted date;
BEGIN
 -- Serializes midnight creation across scheduler and any number of bot workers.
 IF NOT pg_try_advisory_xact_lock(726102701) THEN RETURN NULL; END IF;
 IF d < (SELECT start_day FROM public.daily_renewal_config WHERE singleton) THEN RETURN NULL; END IF;
 IF EXISTS(SELECT 1 FROM public.daily_renewal_runs WHERE day=d) THEN RETURN d; END IF;
 SELECT greatest(0,coalesce((SELECT value FROM public.app_settings WHERE key='daily_renewal_points'),50)) INTO a;
 INSERT INTO public.daily_renewal_runs(day,amount) VALUES(d,a) ON CONFLICT DO NOTHING RETURNING day INTO inserted;
 IF inserted IS NULL THEN RETURN d; END IF;
 -- Preserve paid/gift buckets; never reset a balance already renewed lazily today.
 UPDATE public.users SET free_points=a,last_renewal=d WHERE last_renewal IS NULL OR last_renewal<d;
 INSERT INTO public.daily_renewal_deliveries(day,user_id,user_snapshot,status,error)
 SELECT d,u.user_id,to_jsonb(u),
 CASE WHEN u.bot_delivery_state<>'reachable' THEN 'excluded'
      WHEN NOT u.renewal_notifications THEN 'disabled' ELSE 'pending' END,
 CASE WHEN u.bot_delivery_state='blocked' THEN 'المستخدم حظر البوت — مستثنى قبل الإرسال'
      WHEN u.bot_delivery_state='unavailable' THEN 'الحساب غير متاح — مستثنى قبل الإرسال' ELSE '' END
 FROM public.users u WHERE u.joined_at < (d::timestamp AT TIME ZONE 'Asia/Damascus');
 UPDATE public.daily_renewal_deliveries SET status='expired',error='انتهى يوم الإشعار قبل الإرسال',finished_at=now()
 WHERE day<d AND status='pending';
 RETURN d;
END; $$;

CREATE FUNCTION public.daily_renewal_claim() RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE r public.daily_renewal_deliveries%rowtype; u public.users%rowtype; t uuid; a double precision;
BEGIN
 UPDATE public.daily_renewal_deliveries SET status='uncertain',error='انقطع العامل أثناء الإرسال؛ لا إعادة تلقائية لتفادي التكرار',finished_at=now()
 WHERE status='sending' AND claimed_at<now()-interval '2 minutes';
 FOR r IN SELECT q.* FROM public.daily_renewal_deliveries q JOIN public.daily_renewal_runs j USING(day)
 WHERE q.status='pending' AND q.day=(now() AT TIME ZONE 'Asia/Damascus')::date AND j.not_before<=now()
 ORDER BY q.day,q.user_id LIMIT 50 FOR UPDATE OF q SKIP LOCKED
 LOOP
   SELECT * INTO u FROM public.users WHERE user_id=r.user_id;
   IF NOT FOUND OR NOT u.renewal_notifications OR u.bot_delivery_state<>'reachable' THEN
     UPDATE public.daily_renewal_deliveries SET status='skipped',finished_at=now(),
       error=CASE WHEN u.bot_delivery_state='blocked' THEN 'المستخدم حظر البوت قبل الإرسال'
                  WHEN u.bot_delivery_state='unavailable' THEN 'الحساب غير متاح'
                  ELSE 'أوقف المستخدم الإشعارات أو حُذف حسابه من السجل' END
     WHERE day=r.day AND user_id=r.user_id;
     CONTINUE;
   END IF;
   t:=gen_random_uuid();
   UPDATE public.daily_renewal_deliveries SET status='sending',token=t,claimed_at=clock_timestamp(),user_snapshot=to_jsonb(u)
   WHERE day=r.day AND user_id=r.user_id;
   SELECT amount INTO a FROM public.daily_renewal_runs WHERE day=r.day;
   RETURN jsonb_build_object('day',r.day,'user_id',r.user_id,'token',t,'amount',a);
 END LOOP;
 RETURN NULL;
END; $$;

CREATE FUNCTION public.daily_renewal_finish(p_day date,p_user bigint,p_token uuid,p_status text,p_error text,p_retry integer DEFAULT 0)
RETURNS boolean LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE claimed timestamptz;
BEGIN
 IF p_status NOT IN ('sent','pending','blocked','unavailable','failed','uncertain') THEN RAISE EXCEPTION 'invalid status'; END IF;
 UPDATE public.daily_renewal_deliveries SET status=p_status,error=left(coalesce(p_error,''),500),
 finished_at=CASE WHEN p_status='pending' THEN NULL ELSE now() END
 WHERE day=p_day AND user_id=p_user AND token=p_token AND status='sending' RETURNING claimed_at INTO claimed;
 IF NOT FOUND THEN RETURN false; END IF;
 IF p_status='pending' THEN
   UPDATE public.daily_renewal_runs SET not_before=greatest(not_before,now()+make_interval(secs=>greatest(p_retry,1))) WHERE day=p_day;
 ELSIF p_status IN ('blocked','unavailable') THEN
   -- A newer unblock/incoming interaction must win over an older send failure.
   UPDATE public.users SET bot_delivery_state=p_status WHERE user_id=p_user AND bot_delivery_updated_at<=claimed;
 END IF;
 RETURN true;
END; $$;

CREATE FUNCTION public.daily_renewal_summary(p_day date) RETURNS jsonb
LANGUAGE sql STABLE SECURITY INVOKER SET search_path='' AS $$
 SELECT jsonb_build_object('day',p_day,'sent',count(*) FILTER(WHERE status='sent'),
 'failed',count(*) FILTER(WHERE status IN ('blocked','unavailable','failed')),
 'uncertain',count(*) FILTER(WHERE status IN ('uncertain','expired')),
 'excluded',count(*) FILTER(WHERE status='excluded'),'skipped',count(*) FILTER(WHERE status='skipped'),
 'pending',count(*) FILTER(WHERE status IN ('pending','sending')),
 'enabled',(SELECT count(*) FROM public.users WHERE renewal_notifications),
 'disabled',(SELECT count(*) FROM public.users WHERE NOT renewal_notifications))
 FROM public.daily_renewal_deliveries WHERE day=p_day;
$$;

CREATE FUNCTION public.daily_renewal_report_claim() RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE d date; t uuid;
BEGIN
 UPDATE public.daily_renewal_runs SET report_status='uncertain'
 WHERE report_status='sending' AND report_claimed_at<now()-interval '2 minutes';
 SELECT j.day INTO d FROM public.daily_renewal_runs j
 WHERE report_status='pending' AND report_not_before<=now() AND NOT EXISTS(
   SELECT 1 FROM public.daily_renewal_deliveries q WHERE q.day=j.day AND q.status IN ('pending','sending'))
 ORDER BY j.day LIMIT 1 FOR UPDATE SKIP LOCKED;
 IF NOT FOUND THEN RETURN NULL; END IF;
 t:=gen_random_uuid();
 UPDATE public.daily_renewal_runs SET report_status='sending',report_token=t,report_claimed_at=now() WHERE day=d;
 RETURN public.daily_renewal_summary(d)||jsonb_build_object('token',t);
END; $$;

CREATE FUNCTION public.daily_renewal_report_finish(p_day date,p_token uuid,p_status text,p_retry integer DEFAULT 0)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
BEGIN
 IF p_status NOT IN ('sent','pending','uncertain') THEN RAISE EXCEPTION 'invalid status'; END IF;
 UPDATE public.daily_renewal_runs SET report_status=p_status,
 report_not_before=now()+make_interval(secs=>greatest(p_retry,0))
 WHERE day=p_day AND report_token=p_token AND report_status='sending';
END; $$;

-- Restrict all new RPCs and the trigger function to backend credentials.
DO $$ DECLARE r record; BEGIN
 FOR r IN SELECT p.oid::regprocedure AS signature FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='public' AND p.proname LIKE 'daily_renewal_%'
 LOOP
   EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,anon,authenticated',r.signature);
   EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role',r.signature);
 END LOOP;
END; $$;

-- SCHEDULER: database renewal continues even while the Telegram worker is down.
-- Run at each minute boundary and let Asia/Damascus + the unique date gate decide
-- whether a new day has started. This also follows future timezone rule changes.
CREATE EXTENSION IF NOT EXISTS pg_cron WITH SCHEMA pg_catalog;
SELECT cron.schedule('daily-free-renewal-damascus','* * * * *',
 'SELECT public.daily_renewal_begin()');
