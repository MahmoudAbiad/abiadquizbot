-- Existing users.free_points/paid_points are double precision. Keep subtraction
-- in the same numeric domain used for validation and the debit receipt. Mixing
-- double precision and numeric could turn a positive near-zero free balance
-- into a negative residue, aborting an otherwise fully funded transaction.
-- No existing rows, prices, bucket priority or refund rules are changed.
CREATE OR REPLACE FUNCTION public.wallet_debit(p_user_id bigint, p_amount numeric, p_questions integer, p_debit_id uuid)
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
 UPDATE public.users SET free_points=u.free_points::numeric-f,paid_points=u.paid_points::numeric-p,
 total_questions=coalesce(total_questions,0)+greatest(coalesce(p_questions,0),0) WHERE user_id=p_user_id;
 INSERT INTO public.wallet_debits(id,user_id,free_amount,paid_amount,gifts) VALUES(p_debit_id,p_user_id,f,p,parts);
 RETURN jsonb_build_object('remaining_points',u.free_points::numeric+u.paid_points::numeric+gifts-p_amount,
 'debited_free',f,'debited_paid',p,'debited_gift',p_amount-f-p,'debit_id',p_debit_id);
END; $$;
