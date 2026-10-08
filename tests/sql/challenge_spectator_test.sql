-- Test database ONLY. All fixture rows and flag/settings changes roll back in a
-- subtransaction. Production functions run against real PostgreSQL relations.
-- Identity sequences may advance (normal PostgreSQL non-transactional sequences).
DO $test$
DECLARE
 b uuid; s uuid; u uuid; tid uuid; mid uuid; other uuid; live_mid uuid;
 a bigint:=880000000000001; d bigint:=880000000000002; c bigint:=880000000000003;
 cfg jsonb:='{"version": 1, "questions_per_attack": 10, "question_seconds": 30, "attack_seconds": 900, "star_thresholds": [0.6, 0.8, 1.0], "accuracy_points": 95, "speed_bonus": 5, "xp_per_correct": 10, "trophies_per_star": 5, "loss_trophies": 0, "target_cooldown_seconds": 21600, "daily_attack_limit": 20, "daily_xp_limit": 300, "daily_trophy_limit": 60, "repeat_content_seconds": 86400, "tower_level_base_xp": 100, "max_tower_level": 20, "matchmaking_range": 500, "online_seconds": 120, "notification_cooldown_seconds": 600, "live_questions": 10, "live_countdown_seconds": 5, "live_grace_seconds": 60, "live_queue_seconds": 120, "live_range_initial": 100, "live_range_step": 100, "live_range_step_seconds": 10, "live_win_trophies": 8, "live_draw_trophies": 2, "live_loss_trophies": 0, "live_pair_daily_limit": 3}'::jsonb;
 st jsonb; watched jsonb; before_game jsonb; after_game jsonb;
 users_before text; users_after text; before_start text; before_notice text;
 rejected boolean; who bigint; terminal text; roles text;
BEGIN
 SELECT md5(coalesce(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text),'[]'::jsonb)::text)
 INTO users_before FROM public.users x;
 SELECT pg_get_functiondef('challenge_start_attack(bigint,bigint,uuid,uuid,text,jsonb)'::regprocedure) INTO before_start;
 SELECT pg_get_functiondef('challenge_claim_notifications(integer)'::regprocedure) INTO before_notice;
 BEGIN
  IF EXISTS(SELECT 1 FROM challenge_profiles WHERE user_id IN(a,d,c)) THEN
   RAISE EXCEPTION 'spectator_fixture_ids_in_use';
  END IF;
  UPDATE feature_flags SET enabled=true WHERE key IN('challenge_system_enabled','async_attacks_enabled','spectator_mode_enabled');
  UPDATE app_settings SET value=0 WHERE key IN('challenge_target_cooldown_seconds','challenge_attack_notification_cooldown_seconds','challenge_completion_notification_cooldown_seconds');
  UPDATE app_settings SET value=20 WHERE key='challenge_daily_attack_limit';
  INSERT INTO challenge_branches(name) VALUES('3D-test-'||gen_random_uuid()) RETURNING id INTO b;
  INSERT INTO challenge_subjects(branch_id,name) VALUES(b,'رياضيات') RETURNING id INTO s;
  INSERT INTO challenge_units(subject_id,name) VALUES(s,'اشتقاق') RETURNING id INTO u;
  tid:=(challenge_import_test(u,'spectator fixture','json_upload',null,'3D-'||b,1,
   '[{"question":"نص مصدر","options":["ب","أ"],"correct_option_id":1,"explanation":"SECRET","metadata":{"image_url":"https://example.test/source.png","is_math":true,"table":{"rows":[[1]]},"matrices":[]}},
     {"question":"سؤال نصي","options":["خيار 1","خيار 2"],"correct_option_id":0},
     {"question":"سؤال ثالث","options":["1","2"],"correct_option_id":0}]'::jsonb)->>'test_id')::uuid;
  PERFORM challenge_touch_profile(a,'A',b);PERFORM challenge_touch_profile(d,'B',b);PERFORM challenge_touch_profile(c,'C',b);
  st:=challenge_start_attack(a,d,u,tid,'3d-first',cfg);mid:=(st->>'id')::uuid;
  -- 1: backend-authorized defender, exact current source/order.
  SET LOCAL ROLE service_role;
  watched:=challenge_spectator_state(mid,d);
  RESET ROLE;
  ASSERT watched->>'status'='in_progress','defender authorization';
  ASSERT watched->'question'->>'image_url'='https://example.test/source.png','source image';
  ASSERT watched->'question'->'options'='["ب","أ"]'::jsonb,'option ordering';
  ASSERT NOT (watched ? 'snapshot') AND NOT (watched->'question' ? 'correct_option_id')
    AND NOT (watched->'question' ? 'explanation') AND NOT (watched->'question' ? 'option_id')
    AND NOT (watched->'question' ? 'is_correct'),'current secrets';
  ASSERT watched->'previous'='null'::jsonb AND watched->>'score'='0','unresolved progress';
  -- 6: both non-defender identities rejected with same error.
  FOREACH who IN ARRAY ARRAY[a,c] LOOP
   rejected:=false;
   BEGIN PERFORM challenge_spectator_state(mid,who);
   EXCEPTION WHEN OTHERS THEN IF SQLERRM='spectator_denied' THEN rejected:=true; ELSE RAISE; END IF; END;
   ASSERT rejected,'unauthorized actor accepted';
  END LOOP;
  rejected:=false;
  BEGIN PERFORM challenge_spectator_state(gen_random_uuid(),d);
  EXCEPTION WHEN OTHERS THEN IF SQLERRM='spectator_denied' THEN rejected:=true; ELSE RAISE; END IF; END;
  ASSERT rejected,'missing match accepted';
  -- Synthesize a finished live match with valid constraints, inaccessible to this RPC.
  INSERT INTO challenge_matches(kind,status,attacker_id,defender_id,unit_id,subject_id,test_id,
    content_hash,request_key,snapshot,rules,labels,server_deadline)
   SELECT 'live','completed',attacker_id,defender_id,unit_id,subject_id,test_id,
    content_hash,'3d-live',snapshot,rules,labels,server_deadline FROM challenge_matches WHERE id=mid RETURNING id INTO live_mid;
  rejected:=false;
  BEGIN PERFORM challenge_spectator_state(live_mid,d);
  EXCEPTION WHEN OTHERS THEN IF SQLERRM='spectator_denied' THEN rejected:=true; ELSE RAISE; END IF; END;
  ASSERT rejected,'live match accepted';
  ASSERT jsonb_array_length(challenge_spectator_incoming(d))=1,'incoming live filter';
  ASSERT challenge_spectator_incoming(c)='[]'::jsonb,'incoming authorization';
  UPDATE feature_flags SET enabled=false WHERE key='spectator_mode_enabled';
  rejected:=false;
  BEGIN PERFORM challenge_spectator_state(mid,d);
  EXCEPTION WHEN OTHERS THEN IF SQLERRM='spectator_disabled' THEN rejected:=true; ELSE RAISE; END IF; END;
  ASSERT rejected AND challenge_spectator_incoming(d)='[]'::jsonb,'disabled flag';
  UPDATE feature_flags SET enabled=true WHERE key='spectator_mode_enabled';
  -- Read projection repeatedly and compare full match + player/profiles/answers, not just totals.
  SELECT jsonb_build_object('match',(SELECT to_jsonb(m) FROM challenge_matches m WHERE id=mid),
    'profiles',(SELECT jsonb_agg(to_jsonb(p) ORDER BY user_id) FROM challenge_profiles p WHERE user_id IN(a,d,c)),
    'players',(SELECT jsonb_agg(to_jsonb(p) ORDER BY user_id) FROM challenge_match_players p WHERE match_id=mid),
    'answers',(SELECT jsonb_agg(to_jsonb(p)) FROM challenge_match_answers p WHERE match_id=mid)) INTO before_game;
  PERFORM challenge_spectator_state(mid,d);PERFORM challenge_spectator_state(mid,d);PERFORM challenge_spectator_incoming(d);
  SELECT jsonb_build_object('match',(SELECT to_jsonb(m) FROM challenge_matches m WHERE id=mid),
    'profiles',(SELECT jsonb_agg(to_jsonb(p) ORDER BY user_id) FROM challenge_profiles p WHERE user_id IN(a,d,c)),
    'players',(SELECT jsonb_agg(to_jsonb(p) ORDER BY user_id) FROM challenge_match_players p WHERE match_id=mid),
    'answers',(SELECT jsonb_agg(to_jsonb(p)) FROM challenge_match_answers p WHERE match_id=mid)) INTO after_game;
  ASSERT before_game=after_game,'spectator changed game/result/XP/trophies/timers';
  st:=challenge_advance_attack(mid,a,'answer',0,1,'3d-answer-0');
  watched:=challenge_spectator_state(mid,d);
  ASSERT watched->>'index'='1' AND watched->'previous'->>'index'='0','previous question index';
  ASSERT watched->'previous'->>'option_id'='1' AND watched->'previous'->>'correct_option_id'='1'
   AND watched->'previous'->>'is_correct'='true','resolved answer';
  ASSERT NOT (watched->'question' ? 'correct_option_id') AND NOT (watched->'question' ? 'option_id'),'next answer leak';
  -- Presence never advances timeout. Only existing attack worker can do so.
  UPDATE challenge_matches SET question_deadline=clock_timestamp()-interval '1 second' WHERE id=mid;
  watched:=challenge_spectator_state(mid,d);
  ASSERT watched->>'index'='1','watching advanced timeout';
  st:=challenge_advance_attack(mid,a,'tick');watched:=challenge_spectator_state(mid,d);
  ASSERT watched->'previous'->>'timed_out'='true' AND watched->>'index'='2','timeout projection';
  st:=challenge_advance_attack(mid,a,'answer',2,0,'3d-answer-2');watched:=challenge_spectator_state(mid,d);
  ASSERT watched->>'status'='completed' AND watched->'question'='null'::jsonb,'completed projection';
  ASSERT watched->'result'->'attacker_xp'=st->'result'->'xp'
   AND watched->'result'->'attacker_trophies'=st->'result'->'trophies'
   AND watched->'result'->'attacker_won'=st->'result'->'won','final safe reward projection';
  ASSERT challenge_spectator_incoming(d)='[]'::jsonb,'completed hidden in incoming';
  FOREACH terminal IN ARRAY ARRAY['expired','abandoned'] LOOP
   st:=challenge_start_attack(a,d,u,tid,'3d-'||terminal,cfg);other:=(st->>'id')::uuid;
   IF terminal='expired' THEN
    UPDATE challenge_matches SET server_deadline=clock_timestamp()-interval '1 second' WHERE id=other;
    PERFORM challenge_advance_attack(other,a,'tick');
   ELSE PERFORM challenge_advance_attack(other,a,'abandon'); END IF;
   watched:=challenge_spectator_state(other,d);
   ASSERT watched->>'status'=terminal AND watched->'question'='null'::jsonb,'terminal status';
  END LOOP;
  -- Multiple current attacks, and stranger's own incoming must still be empty.
  st:=challenge_start_attack(a,d,u,tid,'3d-multi-a',cfg);
  PERFORM challenge_start_attack(c,d,u,tid,'3d-multi-c',cfg);
  ASSERT jsonb_array_length(challenge_spectator_incoming(d))=2,'multiple attacks';
  -- 0011--0014 settings still effective and their definitions are untouched.
  ASSERT (SELECT rules->>'daily_attack_limit'='20' AND rules->>'target_cooldown_seconds'='0'
    AND rules->>'attack_notification_cooldown_seconds'='0' FROM challenge_matches WHERE id=(st->>'id')::uuid),'settings snapshot';
  ASSERT pg_get_functiondef('challenge_start_attack(bigint,bigint,uuid,uuid,text,jsonb)'::regprocedure)=before_start,'0011--0013 changed';
  ASSERT pg_get_functiondef('challenge_claim_notifications(integer)'::regprocedure)=before_notice,'0014 changed';
  FOREACH roles IN ARRAY ARRAY['anon','authenticated'] LOOP
   ASSERT NOT has_function_privilege(roles,'challenge_spectator_state(uuid,bigint)','EXECUTE'),'public RPC privilege';
   ASSERT NOT has_function_privilege(roles,'challenge_spectator_incoming(bigint,integer)','EXECUTE'),'public list privilege';
   ASSERT NOT has_table_privilege(roles,'challenge_matches','SELECT') AND NOT has_table_privilege(roles,'challenge_match_answers','SELECT'),'raw access';
  END LOOP;
  rejected:=false;
  BEGIN
   SET LOCAL ROLE anon;
   PERFORM challenge_spectator_state(mid,d);
  EXCEPTION WHEN insufficient_privilege THEN rejected:=true;
  END;
  RESET ROLE;
  ASSERT rejected,'anon actually executed RPC';
  rejected:=false;
  BEGIN
   SET LOCAL ROLE authenticated;
   PERFORM challenge_spectator_state(mid,d);
  EXCEPTION WHEN insufficient_privilege THEN rejected:=true;
  END;
  RESET ROLE;
  ASSERT rejected,'authenticated actually executed RPC';
  ASSERT has_function_privilege('service_role','challenge_spectator_state(uuid,bigint)','EXECUTE'),'service grant';
  ASSERT (SELECT relrowsecurity FROM pg_class WHERE oid='challenge_matches'::regclass),'RLS';
  SELECT md5(coalesce(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text),'[]'::jsonb)::text)
  INTO users_after FROM public.users x;
  ASSERT users_before=users_after,'users including paid/free points changed';
  -- Sentinel caught outside this subtransaction: fixtures/settings/flags ALL roll back.
  RAISE EXCEPTION USING ERRCODE='PZ001',MESSAGE='spectator_validation_passed';
 EXCEPTION WHEN SQLSTATE 'PZ001' THEN NULL;
 END;
 ASSERT NOT EXISTS(SELECT 1 FROM challenge_profiles WHERE user_id IN(a,d,c)),'fixture leaked';
END $test$;
