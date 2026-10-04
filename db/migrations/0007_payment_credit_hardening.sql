-- =============================================================================
-- 0007_payment_credit_hardening.sql — تقوية دالة الشحن الذرية payment_credit_order
--
-- ⚠️ شغّله كاملاً دفعة واحدة (Supabase SQL Editor) *قبل* تفعيل مفتاح «شحن تلقائي» (payments_auto_confirm).
-- الشحن اليدوي يستمر بعمله قبل/بعد الـ migration (الباراميتر الجديد اختياري ويُرسَل من المسار التلقائي فقط).
--
-- التغييرات:
--  1) باراميتر جديد p_require_match (افتراضي false). عند true (المسار التلقائي):
--       - حالة الطلب لازم pending/awaiting_verification (لو انتهى بين القرار والشحن -> not_creditable)
--       - لازم إيصال، ومبلغه وعملته مطابقان للطلب تماماً (وإلا amount_mismatch)
--     = خط دفاع ثانٍ بقاعدة البيانات لو أخطأ منطق بايثون.
--  2) الإيصال المعلَّم dismissed/baseline لا يُشحن به أبداً (receipt_not_creditable): يمنع سباقاً بين
--     زر «تجاهل» والشحن التلقائي.
-- الدالة القديمة (3 باراميترات) تُحذف لأن بقاء overload ثانٍ يجعل PostgREST يفشل باختيار المرشّح.
-- آمن لإعادة التشغيل.
-- =============================================================================

DROP FUNCTION IF EXISTS public.payment_credit_order(bigint, bigint, text);

CREATE OR REPLACE FUNCTION public.payment_credit_order(
  p_order_id      bigint,
  p_receipt_id    bigint  DEFAULT NULL,
  p_actor         text    DEFAULT 'system',
  p_require_match boolean DEFAULT false
)
RETURNS TABLE(result text, order_user_id bigint, credited_points integer,
              new_paid_points double precision, new_total_points double precision)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  v_order     public.payment_orders%ROWTYPE;
  v_rstatus   text;
  v_ramount   numeric;
  v_rcurrency text;
  v_free      double precision;
  v_paid      double precision;
BEGIN
  SELECT * INTO v_order FROM public.payment_orders WHERE id = p_order_id FOR UPDATE;
  IF NOT FOUND THEN
    result := 'not_found'; RETURN NEXT; RETURN;
  END IF;

  order_user_id := v_order.user_id;
  credited_points := v_order.points;

  IF v_order.status = 'paid' THEN
    result := 'already_paid'; RETURN NEXT; RETURN;
  END IF;
  IF v_order.status IN ('rejected', 'cancelled') THEN
    result := 'not_creditable'; RETURN NEXT; RETURN;
  END IF;
  -- المسار التلقائي: لا يشحن إلا طلباً مفتوحاً فعلاً (المنتهي/المراجعة اليدوية يدويان دائماً).
  IF p_require_match AND v_order.status NOT IN ('pending', 'awaiting_verification') THEN
    result := 'not_creditable'; RETURN NEXT; RETURN;
  END IF;

  IF p_receipt_id IS NOT NULL THEN
    SELECT status, amount, currency INTO v_rstatus, v_ramount, v_rcurrency
    FROM public.payment_receipts WHERE id = p_receipt_id FOR UPDATE;
    IF NOT FOUND THEN
      result := 'receipt_not_found'; RETURN NEXT; RETURN;
    END IF;
    IF v_rstatus = 'credited' THEN
      result := 'receipt_used'; RETURN NEXT; RETURN;
    END IF;
    IF v_rstatus IN ('dismissed', 'baseline') THEN
      result := 'receipt_not_creditable'; RETURN NEXT; RETURN;
    END IF;
  END IF;

  IF p_require_match THEN
    IF p_receipt_id IS NULL
       OR v_ramount   IS DISTINCT FROM v_order.amount
       OR v_rcurrency IS DISTINCT FROM v_order.currency THEN
      result := 'amount_mismatch'; RETURN NEXT; RETURN;
    END IF;
  END IF;

  UPDATE public.users
  SET paid_points = COALESCE(public.users.paid_points, 0.0) + v_order.points
  WHERE user_id = v_order.user_id
  RETURNING public.users.free_points, public.users.paid_points INTO v_free, v_paid;
  IF NOT FOUND THEN
    result := 'user_not_found'; RETURN NEXT; RETURN;
  END IF;

  UPDATE public.payment_orders
  SET status = 'paid', paid_at = now(), updated_at = now(),
      receipt_id = p_receipt_id, decided_by = p_actor
  WHERE id = v_order.id;

  IF p_receipt_id IS NOT NULL THEN
    UPDATE public.payment_receipts
    SET status = 'credited', matched_order_id = v_order.id, updated_at = now()
    WHERE id = p_receipt_id;
  END IF;

  result := 'ok';
  new_paid_points := v_paid;
  new_total_points := COALESCE(v_free, 0.0) + v_paid;
  RETURN NEXT;
  RETURN;
END;
$function$;

REVOKE ALL ON FUNCTION public.payment_credit_order(bigint, bigint, text, boolean) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.payment_credit_order(bigint, bigint, text, boolean) TO service_role;
