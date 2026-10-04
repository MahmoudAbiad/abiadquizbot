-- =============================================================================
-- 0006_payments.sql  —  نظام الشحن المؤتمت (شام كاش + MacroDroid) - المرحلة الأولى
--
-- الجداول الأربعة كلها RLS مفعّل بلا أي policy: البوت يتصل بـ service_role (يتجاوز RLS)،
-- و anon/authenticated ممنوعين تماماً. الدالة المالية payment_credit_order قابلة للتنفيذ
-- من service_role فقط (بعكس الدوال المالية القديمة القابلة للتنفيذ من anon - انظر db/README.md).
--
-- آمن إعادة التشغيل (IF NOT EXISTS / OR REPLACE / ON CONFLICT DO NOTHING).
-- لا توجد مفاتيح أجنبية على users عمداً: السجل المالي لا يجب أن يمنع حذف مستخدم ولا أن
-- يتأثر به، ولا نريد قفلاً على جدول users أثناء الـ migration.
-- =============================================================================

-- ---------- 1) الباقات (تُعدَّل بـ SQL مثل ai_model_pricing؛ لا باقة نشطة افتراضياً) ----------
CREATE TABLE IF NOT EXISTS public.payment_packages (
  id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  points      integer       NOT NULL CHECK (points > 0),
  price       numeric(14,2) NOT NULL CHECK (price > 0),
  currency    text          NOT NULL DEFAULT 'SYP' CHECK (currency IN ('SYP', 'USD')),
  label       text,
  active      boolean       NOT NULL DEFAULT false,
  sort_order  integer       NOT NULL DEFAULT 0,
  created_at  timestamptz   NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.payment_packages IS
  'باقات شحن النقاط. active=false لا تظهر للطلاب. السعر بعملة الباقة (SYP/USD).';

-- ---------- 2) طلبات الشحن ----------
CREATE TABLE IF NOT EXISTS public.payment_orders (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id          bigint        NOT NULL,
  package_id       integer,
  points           integer       NOT NULL CHECK (points > 0),
  amount           numeric(14,2) NOT NULL CHECK (amount > 0),
  currency         text          NOT NULL CHECK (currency IN ('SYP', 'USD')),
  method           text          NOT NULL DEFAULT 'shamcash',
  code             text          NOT NULL,
  status           text          NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'awaiting_verification', 'manual_review',
                                     'paid', 'rejected', 'expired', 'cancelled')),
  created_at       timestamptz   NOT NULL DEFAULT now(),
  expires_at       timestamptz   NOT NULL,
  claimed_at       timestamptz,
  paid_at          timestamptz,
  receipt_id       bigint,
  decided_by       text,
  decision_note    text,
  group_chat_id    bigint,
  group_message_id bigint,
  updated_at       timestamptz   NOT NULL DEFAULT now()
);

-- الكود فريد بين الطلبات المفتوحة فقط (يُعاد استخدامه بعد انتهاء الطلب).
CREATE UNIQUE INDEX IF NOT EXISTS payment_orders_open_code_uq
  ON public.payment_orders (code)
  WHERE status IN ('pending', 'awaiting_verification', 'manual_review');

-- طلب مفتوح واحد فقط لكل طالب (يمنع الإغراق وتشتيت المطابقة).
CREATE UNIQUE INDEX IF NOT EXISTS payment_orders_open_user_uq
  ON public.payment_orders (user_id)
  WHERE status IN ('pending', 'awaiting_verification', 'manual_review');

CREATE INDEX IF NOT EXISTS payment_orders_status_expires_idx
  ON public.payment_orders (status, expires_at);

-- ---------- 3) الأحداث الخام الواردة من الجوال (للتدقيق وللـ watchdog) ----------
CREATE TABLE IF NOT EXISTS public.payment_events (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  device_id         text,
  event_type        text        NOT NULL,
  notification_text text,
  screen_text       text,
  processed         boolean     NOT NULL DEFAULT false,
  received_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS payment_events_pending_idx
  ON public.payment_events (received_at) WHERE processed = false;
COMMENT ON TABLE public.payment_events IS
  'أحداث خام من MacroDroid. screen_text قد يحوي أسماء أطراف أخرى: يُقتطع ويُحذف بعد مدة احتفاظ قصيرة.';

-- ---------- 4) الإيصالات المستخرجة من سجل التحويلات ----------
CREATE TABLE IF NOT EXISTS public.payment_receipts (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tx_id            text          NOT NULL,
  counterparty     text,
  amount           numeric(14,2),
  currency         text,
  tx_time          timestamptz,
  note             text,
  raw_card         text,
  status           text          NOT NULL DEFAULT 'new'
                   CHECK (status IN ('new', 'credited', 'dismissed', 'baseline')),
  matched_order_id bigint,
  event_id         bigint,
  created_at       timestamptz   NOT NULL DEFAULT now(),
  updated_at       timestamptz   NOT NULL DEFAULT now()
);
-- رقم العملية فريد: نفس الحوالة لا تُسجَّل ولا تُشحن مرتين مهما أعاد الجوال الإرسال.
CREATE UNIQUE INDEX IF NOT EXISTS payment_receipts_tx_id_uq
  ON public.payment_receipts (tx_id);

-- ---------- 5) RLS: مفعّل بلا policies + سحب الصلاحيات ----------
ALTER TABLE public.payment_packages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.payment_orders   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.payment_events   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.payment_receipts ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.payment_packages, public.payment_orders,
              public.payment_events,   public.payment_receipts FROM anon, authenticated;

-- ---------- 6) دالة الشحن الذرية ----------
-- ⚠️ حُدِّثت بـ 0007_payment_credit_hardening.sql (باراميتر رابع). لا تُعد تشغيل هذا القسم بعد تطبيق 0007.
-- تقفل الطلب (FOR UPDATE)، تتحقق من الحالة والإيصال، تضيف paid_points، وتعلّم الطلب والإيصال
-- كلها بمعاملة واحدة. آمنة لإعادة الاستدعاء: الاستدعاء الثاني يرجع 'already_paid' بلا إضافة.
-- يُسمح بشحن طلب 'expired' (دفعة متأخرة وافق عليها الأدمن يدوياً) لكن ليس rejected/cancelled.
CREATE OR REPLACE FUNCTION public.payment_credit_order(
  p_order_id   bigint,
  p_receipt_id bigint DEFAULT NULL,
  p_actor      text   DEFAULT 'system'
)
RETURNS TABLE(result text, order_user_id bigint, credited_points integer,
              new_paid_points double precision, new_total_points double precision)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
  v_order   public.payment_orders%ROWTYPE;
  v_rstatus text;
  v_free    double precision;
  v_paid    double precision;
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

  IF p_receipt_id IS NOT NULL THEN
    SELECT status INTO v_rstatus FROM public.payment_receipts WHERE id = p_receipt_id FOR UPDATE;
    IF NOT FOUND THEN
      result := 'receipt_not_found'; RETURN NEXT; RETURN;
    END IF;
    IF v_rstatus = 'credited' THEN
      result := 'receipt_used'; RETURN NEXT; RETURN;
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

REVOKE ALL ON FUNCTION public.payment_credit_order(bigint, bigint, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.payment_credit_order(bigint, bigint, text) TO service_role;

-- ---------- 7) مفتاح التحكم (مطفي افتراضياً) ----------
INSERT INTO public.feature_flags (key, enabled) VALUES ('payments_enabled', false)
ON CONFLICT (key) DO NOTHING;
