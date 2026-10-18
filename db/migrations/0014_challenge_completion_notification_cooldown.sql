-- Admin-configurable cooldown for the "defense result" push notification.
-- Requires 0013_challenge_attack_notification_cooldown.sql.
-- Applies dynamically when completion notifications are claimed, including matches started before the setting changed.
BEGIN;

INSERT INTO public.app_settings(key,value,label) VALUES
 ('challenge_completion_notification_cooldown_seconds',600,'مهلة تكرار إشعار نتيجة الدفاع (ثانية)')
ON CONFLICT(key) DO NOTHING;

CREATE OR REPLACE FUNCTION public.challenge_claim_notifications(p_limit integer DEFAULT 20)
RETURNS SETOF public.challenge_notifications
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path=public
AS $$
DECLARE
 n challenge_notifications;
 allowed boolean;
 ids bigint[];
 completion_notification_cooldown_seconds integer;
BEGIN
 completion_notification_cooldown_seconds:=greatest(
  0,
  least(
   604800,
   coalesce(
    (SELECT value::integer
     FROM app_settings
     WHERE key='challenge_completion_notification_cooldown_seconds'),
    600
   )
  )
 );

 SELECT array_agg(x.id) INTO ids
 FROM (
  SELECT id
  FROM challenge_notifications
  WHERE claimed_at IS NULL
  ORDER BY id
  LIMIT least(greatest(p_limit,1),100)
  FOR UPDATE SKIP LOCKED
 ) x;

 PERFORM user_id
 FROM challenge_profiles
 WHERE user_id IN (
  SELECT user_id
  FROM challenge_notifications
  WHERE id=ANY(ids)
 )
 ORDER BY user_id
 FOR UPDATE;

 FOR n IN
  SELECT *
  FROM challenge_notifications
  WHERE id=ANY(ids)
  ORDER BY id
 LOOP
  UPDATE challenge_notifications
  SET claimed_at=clock_timestamp()
  WHERE id=n.id;

  IF n.kind='attack_completed' THEN
   UPDATE challenge_profiles
   SET last_completion_notification_at=clock_timestamp()
   WHERE user_id=n.user_id
     AND notifications_enabled
     AND (
      last_completion_notification_at IS NULL
      OR last_completion_notification_at
         < clock_timestamp()-make_interval(secs=>completion_notification_cooldown_seconds)
     );
   allowed:=FOUND;
  ELSE
   SELECT notifications_enabled
   INTO allowed
   FROM challenge_profiles
   WHERE user_id=n.user_id;
  END IF;

  IF allowed THEN
   RETURN NEXT n;
  END IF;
 END LOOP;
END $$;

COMMIT;
