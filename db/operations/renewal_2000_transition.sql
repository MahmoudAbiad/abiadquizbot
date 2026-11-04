-- Run after deploying the 20:00 notification text. One-time transition:
-- after 20:00 the next cycle key is new, so the durable outbox creates one
-- exceptional renewal automatically. No old runs or deliveries are rewritten.
BEGIN;
CREATE OR REPLACE FUNCTION public.daily_renewal_cycle_day(p_at timestamptz DEFAULT now())
RETURNS date LANGUAGE sql STABLE SECURITY INVOKER SET search_path='' AS $$
 SELECT ((p_at AT TIME ZONE 'Asia/Damascus') + interval '4 hours')::date;
$$;
REVOKE ALL ON FUNCTION public.daily_renewal_cycle_day(timestamptz) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.daily_renewal_cycle_day(timestamptz) TO service_role;

DO $patch$
DECLARE old_def text; new_def text; fn text;
BEGIN
 FOR fn IN SELECT unnest(ARRAY['public.daily_renewal_begin()','public.daily_renewal_claim()'])
 LOOP
  SELECT pg_get_functiondef(fn::regprocedure) INTO old_def;
  IF position('public.daily_renewal_cycle_day()' IN old_def)>0 THEN CONTINUE; END IF;
  IF position('(now() AT TIME ZONE ''Asia/Damascus'')::date' IN old_def)=0 THEN
   RAISE EXCEPTION 'Unexpected definition for %',fn;
  END IF;
  new_def:=replace(old_def,'(now() AT TIME ZONE ''Asia/Damascus'')::date','public.daily_renewal_cycle_day()');
  IF fn='public.daily_renewal_begin()' THEN
   new_def:=replace(new_def,'u.joined_at < (d::timestamp AT TIME ZONE ''Asia/Damascus'')','u.joined_at < now()');
  END IF;
  EXECUTE new_def;
 END LOOP;

 SELECT pg_get_functiondef('public.check_and_apply_daily_renewal_atomic(bigint,date,double precision)'::regprocedure) INTO old_def;
 IF position('today_date := public.daily_renewal_cycle_day();' IN old_def)=0 THEN
  new_def:=regexp_replace(old_def,'\mbegin\M',E'begin\n today_date := public.daily_renewal_cycle_day();','i');
  IF new_def=old_def THEN RAISE EXCEPTION 'Unexpected lazy renewal definition'; END IF;
  EXECUTE new_def;
 END IF;

 SELECT pg_get_functiondef('public.register_user_with_gift(jsonb)'::regprocedure) INTO old_def;
 IF position('public.daily_renewal_cycle_day()' IN old_def)=0 THEN
  new_def:=replace(old_def,'(p_user->>''last_renewal'')::date','public.daily_renewal_cycle_day()');
  IF new_def=old_def THEN RAISE EXCEPTION 'Unexpected registration definition'; END IF;
  EXECUTE new_def;
 END IF;
END;
$patch$;
COMMIT;

