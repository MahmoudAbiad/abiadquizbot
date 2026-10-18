-- Admin-configurable cooldown for the "land is under attack" push notification.
-- Requires 0012_challenge_daily_attack_limit.sql.
-- This setting affects attack_started notifications only; completion notifications keep their existing rule.
BEGIN;

INSERT INTO public.app_settings(key,value,label) VALUES
 ('challenge_attack_notification_cooldown_seconds',600,'مهلة تكرار إشعار تعرض الأرض للهجوم (ثانية)')
ON CONFLICT(key) DO NOTHING;

CREATE OR REPLACE FUNCTION public.challenge_start_attack(
 p_user bigint,p_target bigint,p_unit uuid,p_test uuid,p_request text,p_rules jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path=public AS $$
DECLARE a challenge_profiles; d challenge_profiles; t challenge_tests; u challenge_units;
 s challenge_subjects; b challenge_branches; mid uuid; qs jsonb; stamp timestamptz;
 eligible boolean; labels jsonb; n integer;
 cooldown_seconds integer; cooldown_reset_epoch double precision; cooldown_cutoff timestamptz;
 daily_attack_limit integer; attack_notification_cooldown_seconds integer; effective_rules jsonb;
BEGIN
 IF p_user=p_target THEN RAISE EXCEPTION 'self_attack'; END IF;
 IF p_request IS NULL OR length(p_request)>160 THEN RAISE EXCEPTION 'invalid_request'; END IF;

 PERFORM user_id FROM challenge_profiles WHERE user_id IN(p_user,p_target) ORDER BY user_id FOR UPDATE;
 SELECT * INTO a FROM challenge_profiles WHERE user_id=p_user;
 SELECT * INTO d FROM challenge_profiles WHERE user_id=p_target;
 IF a.user_id IS NULL OR d.user_id IS NULL THEN RAISE EXCEPTION 'profile_missing'; END IF;

 SELECT id INTO mid FROM challenge_matches WHERE attacker_id=p_user AND request_key=p_request;
 IF mid IS NOT NULL THEN RETURN challenge_attack_state(mid,p_user); END IF;

 IF NOT EXISTS(SELECT 1 FROM feature_flags WHERE key='challenge_system_enabled' AND enabled)
 OR NOT EXISTS(SELECT 1 FROM feature_flags WHERE key='async_attacks_enabled' AND enabled) THEN
  RAISE EXCEPTION 'game_disabled';
 END IF;

 IF EXISTS(SELECT 1 FROM challenge_matches WHERE attacker_id=p_user AND status IN('created','in_progress')) THEN
  RAISE EXCEPTION 'active_attack';
 END IF;

 stamp:=clock_timestamp();

 SELECT
  coalesce((SELECT value FROM app_settings WHERE key='challenge_target_cooldown_seconds'),
           (p_rules->>'target_cooldown_seconds')::double precision,21600),
  coalesce((SELECT value FROM app_settings WHERE key='challenge_cooldown_reset_epoch'),0),
  coalesce((SELECT value FROM app_settings WHERE key='challenge_daily_attack_limit'),
           (p_rules->>'daily_attack_limit')::double precision,20),
  coalesce((SELECT value FROM app_settings WHERE key='challenge_attack_notification_cooldown_seconds'),
           (p_rules->>'notification_cooldown_seconds')::double precision,600)
 INTO cooldown_seconds,cooldown_reset_epoch,daily_attack_limit,attack_notification_cooldown_seconds;

 cooldown_seconds:=greatest(0,least(2592000,cooldown_seconds)); -- 0..30 days
 cooldown_reset_epoch:=greatest(0,cooldown_reset_epoch);
 daily_attack_limit:=greatest(0,least(1000,daily_attack_limit));
 attack_notification_cooldown_seconds:=greatest(0,least(604800,attack_notification_cooldown_seconds)); -- 0..7 days

 cooldown_cutoff:=greatest(
  stamp-make_interval(secs=>cooldown_seconds),
  to_timestamp(cooldown_reset_epoch)
 );

 -- Only previous land attacks count toward the same-target cooldown.
 IF EXISTS(
  SELECT 1
  FROM challenge_matches
  WHERE attacker_id=p_user
    AND defender_id=p_target
    AND kind='async_attack'
    AND created_at>cooldown_cutoff
 ) THEN
  RAISE EXCEPTION 'target_cooldown';
 END IF;

 -- Daily limit is for land attacks only. Live/friend matches do not consume it.
 SELECT count(*) INTO n
 FROM challenge_matches
 WHERE attacker_id=p_user
   AND kind='async_attack'
   AND created_at>=date_trunc('day',stamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';

 IF n >= daily_attack_limit THEN
  RAISE EXCEPTION 'daily_limit';
 END IF;

 SELECT * INTO t FROM challenge_tests WHERE id=p_test AND unit_id=p_unit AND status='active' FOR SHARE;
 SELECT * INTO u FROM challenge_units WHERE id=p_unit AND status='active' FOR SHARE;
 SELECT * INTO s FROM challenge_subjects WHERE id=u.subject_id AND status='active' FOR SHARE;
 SELECT * INTO b FROM challenge_branches WHERE id=s.branch_id AND status='active' FOR SHARE;
 IF t.id IS NULL OR u.id IS NULL OR s.id IS NULL OR b.id IS NULL THEN RAISE EXCEPTION 'no_content'; END IF;
 IF a.branch_id IS DISTINCT FROM s.branch_id OR d.branch_id IS DISTINCT FROM s.branch_id THEN
  RAISE EXCEPTION 'branch_mismatch';
 END IF;

 SELECT jsonb_agg(x.q ORDER BY x.question_order) INTO qs FROM (
  SELECT question_order,jsonb_build_object(
   'id',id,'text',question_text,'options',options,
   'correct_option_id',correct_option_id,'image_url',metadata->>'image_url'
  ) q
  FROM challenge_questions
  WHERE test_id=p_test
  ORDER BY md5(id::text||p_request)
  LIMIT (p_rules->>'questions_per_attack')::integer
 ) x;
 IF qs IS NULL OR jsonb_array_length(qs)=0 THEN RAISE EXCEPTION 'no_content'; END IF;

 eligible:=NOT EXISTS(
  SELECT 1 FROM challenge_matches
  WHERE attacker_id=p_user
    AND content_hash=t.content_hash
    AND created_at>stamp-make_interval(secs=>(p_rules->>'repeat_content_seconds')::integer)
 );

 labels:=jsonb_build_object(
  'attacker',a.display_name,'defender',d.display_name,
  'subject',s.name,'unit',u.name,'test',t.title
 );

 effective_rules:=p_rules || jsonb_build_object(
  'target_cooldown_seconds',cooldown_seconds,
  'target_cooldown_reset_epoch',floor(cooldown_reset_epoch)::bigint,
  'daily_attack_limit',daily_attack_limit,
  'attack_notification_cooldown_seconds',attack_notification_cooldown_seconds
 );

 INSERT INTO challenge_matches(
  attacker_id,defender_id,unit_id,subject_id,test_id,content_hash,request_key,
  snapshot,rules,labels,server_deadline,reward_eligible,created_at
 )
 VALUES(
  p_user,p_target,p_unit,s.id,p_test,t.content_hash,p_request,
  qs,effective_rules,labels,
  stamp+make_interval(secs=>(p_rules->>'attack_seconds')::integer),eligible,stamp
 )
 RETURNING id INTO mid;

 UPDATE challenge_matches
 SET status='in_progress',started_at=stamp,question_opened_at=stamp,
     question_deadline=stamp+make_interval(secs=>(p_rules->>'question_seconds')::integer)
 WHERE id=mid;

 INSERT INTO challenge_match_players(match_id,user_id,role)
 VALUES(mid,p_user,'attacker'),(mid,p_target,'defender');

 INSERT INTO challenge_events(match_id,event_type,question_index)
 VALUES(mid,'attack_started',0);

 IF d.notifications_enabled
 AND d.last_seen>stamp-make_interval(secs=>(p_rules->>'online_seconds')::integer)
 AND (
  d.last_notification_at IS NULL
  OR d.last_notification_at<stamp-make_interval(secs=>attack_notification_cooldown_seconds)
 ) THEN
  INSERT INTO challenge_notifications(match_id,user_id,kind)
  VALUES(mid,p_target,'attack_started');
  UPDATE challenge_profiles SET last_notification_at=stamp WHERE user_id=p_target;
 END IF;

 RETURN challenge_attack_state(mid,p_user);
END $$;

COMMIT;
