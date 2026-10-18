-- Phase 3D: read-only, defender-only async spectator projection.
-- Does not replace start/advance/finish/notification RPCs (0011--0014).
BEGIN;
CREATE INDEX IF NOT EXISTS challenge_spectator_incoming
 ON public.challenge_matches(defender_id,created_at,id)
 WHERE kind='async_attack' AND status='in_progress';

CREATE OR REPLACE FUNCTION public.challenge_spectator_state(p_match uuid,p_user bigint)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=public AS $$
DECLARE m challenge_matches; q jsonb; previous jsonb; points integer; correct integer; resolved integer;
BEGIN
 SELECT * INTO m FROM challenge_matches
 WHERE id=p_match AND kind='async_attack' AND defender_id=p_user AND attacker_id<>p_user;
 IF NOT FOUND THEN RAISE EXCEPTION 'spectator_denied'; END IF;
 IF NOT EXISTS(SELECT 1 FROM feature_flags WHERE key='spectator_mode_enabled' AND enabled) THEN
  RAISE EXCEPTION 'spectator_disabled';
 END IF;
 -- STABLE gives all SELECTs the caller's single MVCC snapshot, even during an answer commit.
 -- No tick/advance here: watching cannot change timers, results, rewards or balances.
 IF m.status='in_progress' THEN q:=m.snapshot->m.question_index; END IF;
 SELECT coalesce(sum(a.score),0),count(*) FILTER(WHERE a.is_correct),count(*)
 INTO points,correct,resolved FROM challenge_match_answers a
 WHERE a.match_id=m.id AND a.user_id=m.attacker_id AND a.question_index<m.question_index;
 SELECT jsonb_build_object('index',a.question_index,'option_id',a.option_id,
   'correct_option_id',m.snapshot->a.question_index->'correct_option_id',
   'is_correct',a.is_correct,'timed_out',a.timed_out,'score',a.score)
 INTO previous FROM challenge_match_answers a
 WHERE a.match_id=m.id AND a.user_id=m.attacker_id AND a.question_index<m.question_index
 ORDER BY a.question_index DESC LIMIT 1;
 RETURN jsonb_build_object(
  'id',m.id,'status',m.status,'index',m.question_index,'total',jsonb_array_length(m.snapshot),
  'labels',jsonb_build_object('attacker',m.labels->>'attacker','subject',m.labels->>'subject','unit',m.labels->>'unit'),
  'question',CASE WHEN q IS NULL THEN NULL ELSE jsonb_build_object(
    'text',q->>'text','options',q->'options','image_url',q->>'image_url',
    'is_math',q->'is_math','table',q->'table','matrices',q->'matrices') END,
  'deadline',least(m.question_deadline,m.server_deadline),'server_now',statement_timestamp(),
  'score',points,'correct',correct,'resolved',resolved,'previous',previous,
  'result',CASE WHEN m.status IN('completed','expired','abandoned') THEN jsonb_build_object(
    'correct',m.result->'correct','total',m.result->'total','score',m.result->'score',
    'stars',m.result->'stars','attacker_won',m.result->'won',
    'attacker_xp',m.result->'xp','attacker_trophies',m.result->'trophies') ELSE NULL END);
END $$;

CREATE OR REPLACE FUNCTION public.challenge_spectator_incoming(p_user bigint,p_offset integer DEFAULT 0)
RETURNS jsonb LANGUAGE sql STABLE SECURITY INVOKER SET search_path=public AS $$
 SELECT coalesce(jsonb_agg(x.item ORDER BY x.created_at,x.id),'[]'::jsonb) FROM (
  SELECT m.created_at,m.id,jsonb_build_object('id',m.id,
    'attacker',m.labels->>'attacker','subject',m.labels->>'subject','unit',m.labels->>'unit') item
  FROM challenge_matches m WHERE m.defender_id=p_user AND m.attacker_id<>p_user
    AND m.kind='async_attack' AND m.status='in_progress'
    AND EXISTS(SELECT 1 FROM feature_flags WHERE key='spectator_mode_enabled' AND enabled)
  ORDER BY m.created_at,m.id LIMIT 6 OFFSET greatest(0,least(coalesce(p_offset,0),1000000))
 ) x
$$;
REVOKE ALL ON FUNCTION public.challenge_spectator_state(uuid,bigint),
 public.challenge_spectator_incoming(bigint,integer) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.challenge_spectator_state(uuid,bigint),
 public.challenge_spectator_incoming(bigint,integer) TO service_role;
COMMIT;
