# الشحن الذاتي (شام كاش + MacroDroid) — المرحلة الأولى

**الأصل = التقاط + مراجعة يدوية.** كل دفعة تصل مجموعة الأدمن بأزرار (✅ تأكيد / ❌ رفض / 🚫 تجاهل)،
والشحن الفعلي بدالة ذرية `payment_credit_order`. الاستثناء الوحيد هو زر **🤖 شحن تلقائي** (مطفي افتراضياً)
الموضح بقسم «الشحن التلقائي» تحت.

## التشغيل (بالترتيب)
1. **متغيرات Heroku:** `PAYMENT_WEBHOOK_SECRET` (`openssl rand -hex 32`)، `PAYMENT_ADMIN_GROUP_ID=-1004031122773`،
   `SHAMCASH_TRANSFER_CODE`، وصورة QR بـ `assets/shamcash_qr.jpg` (اختياري). البوت لازم يكون عضواً بالمجموعة.
2. **الباقات:** مُدخلة بجدول `payment_packages` (200 نقطة/65، 500/130، 1000/260 ل.س، `active=true`) وتطابق
   `RECHARGE_PACKAGES` بـ `handlers/start.py`. ⚠️ الأسعار بمكانين: الجدول (للشحن الذاتي) والقاموس
   (لشاشة الدعم الاحتياطية)؛ عدّلهما معاً. مثال إضافة باقة:
   ```sql
   INSERT INTO payment_packages (points, price, currency, label, active, sort_order)
   VALUES (100, 5000, 'SYP', NULL, true, 1);
   ```
3. **شغّل الفلاغ** `payments_enabled` من لوحة الأدمن → مفاتيح التحكم. قبل ذلك تبقى شاشة الشحن القديمة (الدعم).
4. **MacroDroid:** التعديلات أدناه.

## الـ endpoint
`POST https://bot.abiad.me/api/payments/macrodroid?event_type=<النوع>&device_id=phone1`
Header: `X-Payment-Secret: <السر>` — Content-Type: `text/plain` — الجسم: النص الخام فقط (لا JSON).

| `event_type` | الجسم | متى |
|---|---|---|
| `notification` | نص الإشعار (`{not_title}` + `{notification}`) | أول خطوة بـ Macro الإشعار |
| `screen` | متغير `screen_data` | بعد Read Screen Contents |
| `manual_request` | نص الإشعار | أي فشل أو رفض أو لا رد → معالجة يدوية فورية بالمجموعة |
| `heartbeat` | فاضي | كل 5 دقائق |

(JSON وform ما زالا مقبولين، لكن النص الخام الأسلم لأن علامات الاقتباس و`&` و`=` بالنص العربي ما تكسره.)
الرد: 200 = وصل وتسجّل · 403 = السر غلط · 503 = السر غير مضبوط بالسيرفر · 429 = طلبات كثيرة.

## إعداد MacroDroid خطوة بخطوة
**0) صلاحيات الجوال (مرة وحدة):** Notification Access، Accessibility، Display over other apps، Device Administrator (لقفل الشاشة)،
واستثناء MacroDroid من توفير البطارية (سامسونغ: Battery → Background usage limits → Never sleeping apps). خلّي الجوال موصول بالشاحن.

**1) اختبار الاتصال (Macro تجريبي):** Trigger: Floating Button أو Shortcut. Action: HTTP Request POST على
`...?event_type=manual_request&device_id=phone1` + Header السر + Content-Type `text/plain` + الجسم `اختبار`.
احفظ الـ Response Code بمتغير واعرضه بـ Toast. النتيجة: 200 ورسالة «معالجة يدوية مطلوبة» بالمجموعة = كل شي سليم.

**2) Macro أ — التقاط الإشعار (بلا UI):** Trigger: Notification Received ← ShamCash (Any Content بالبداية).
Action: نفس الـ HTTP لكن `event_type=notification` والجسم `{not_title}` سطر جديد `{notification}`.

**3) Macro ب — التحقق (الـ macro الحالي):**
- استبدل شرط `Screen On` بـ `Device Locked`.
- بعد كتلة الـ dialog وفتح القفل: `If proceed = False` ← POST بـ `manual_request` (+ نص الإشعار) ← إيقاف.
- بعد Read Screen Contents: POST بـ `event_type=screen` والجسم متغير `screen_data` (اختره من زر + للـ magic text).
- بالنهاية: Launch Home Screen ثم Lock Screen.
- فعّل منع إعادة التشغيل أثناء التنفيذ.

**4) Macro ج — heartbeat:** Trigger: Regular Interval كل 5 دقائق. Action: POST بـ `event_type=heartbeat` وجسم فاضي.

**5) أول دفعة حقيقية صغيرة:** بعد وصولها شوف `payment_events` ورسالة المجموعة وابعت لي نص الإشعار و`screen_data`.

## ضمانات التصميم
- كل حدث يُسجَّل خاماً قبل التحليل؛ `tx_id` فريد (UNIQUE) فلا تكرار شحن.
- أول تشغيل: الحوالات القديمة تُسجَّل `baseline` ولا تُعرض كدفعات.
- إشعار بلا حدث سجل خلال 5 دقائق → تنبيه المجموعة تلقائياً (الجوال طافي/بلا نت).
- طلب `pending` ينتهي بعد 60 دقيقة؛ دفعة متأخرة لطلب منتهي تصل المجموعة وتُشحن بتأكيد يدوي.
- الأزرار محصورة بـ `ADMIN_ID` (+ `PAYMENT_ADMIN_IDS`)؛ الشحن المزدوج مستحيل (قفل الطلب والإيصال).
- أحداث `payment_events` الخام (قد تحوي أسماء أطراف أخرى) تُحذف بعد 14 يوماً.

## 🤖 الشحن التلقائي (مفتاح `payments_auto_confirm`)
**المسار:** لوحة الأدمن ← الإعدادات العامة ← مفاتيح التحكم ← «الشحن الذاتي» ← زر «🤖 شحن تلقائي». مطفي افتراضياً
(غياب الصف بجدول `feature_flags` = مطفي؛ لا يحتاج أي migration). يعمل فقط لو `payments_enabled` مفعّل أيضاً.

عند التفعيل تُشحن الدفعة **تلقائياً فقط لو تحققت كل الشروط** (المنطق النقي: `payment_parser.auto_confirm_verdict` + حراسة `payment_service._try_auto_confirm`):
1. الملاحظة تحوي كوداً واحداً فقط (6 أرقام) وهو كود طلب مفتوح.
2. حالة الطلب `pending` أو `awaiting_verification` (المنتهي `expired` و`manual_review` يدويان دائماً).
3. المبلغ والعملة مطابقان للطلب تماماً.
4. وقت الحوالة معلوم، لا يسبق إنشاء الطلب بأكثر من دقيقتين، ولا يتجاوز انتهاءه بأكثر من دقيقتين، وليس بالمستقبل، ولا أقدم من 15 دقيقة (`PAYMENT_AUTO_MAX_RECEIPT_AGE_MINUTES`).
5. ليست أول تشغيل للنظام (دفعات خط الأساس لا تُشحن أبداً).
6. **الحوالة هي أحدث بطاقة (الأولى) بسجل شام كاش.** بطاقة محقونة بملاحظة حوالة (سطور مزيفة) لا تكون الأولى أبداً؛ أي بطاقة أخرى تصل للأدمن يدوياً مع تحذير «ليست أحدث بطاقة».
7. نقاط الطلب ≤ `PAYMENT_AUTO_MAX_POINTS_PER_ORDER` (افتراضي 1000) ومجموع الشحن التلقائي بآخر 24 ساعة + هذا الطلب ≤ `PAYMENT_AUTO_DAILY_MAX_POINTS` (افتراضي 5000). تُضبط من Heroku:
   `heroku config:set PAYMENT_AUTO_DAILY_MAX_POINTS=3000 -a <app>`
8. الدالة الذرية نفسها (بعد **migration 0007**) تعيد فحص حالة الطلب ومطابقة مبلغ/عملة الإيصال (`p_require_match`) وترفض الإيصالات `dismissed/baseline`.

النتيجة: شحن عبر `payment_credit_order` نفسها (`decided_by = auto:shamcash`)، إشعار للطالب، رسالة ملخّص للمجموعة **مع زر «🛑 إيقاف الشحن التلقائي»** (يطفئ المفتاح فوراً)، وإزالة أزرار
بطاقة «طالب أكّد الدفع». أي شرط ناقص أو خطأ قبل الشحن = المسار اليدوي كما هو، مع سطر يوضح سبب عدم المصادقة التلقائية.
ضغط الطالب «دفعت/إلغاء» بعد شحنه تلقائياً يُخبره بأن رصيده أُضيف ويزيل الأزرار.

⚠️ **قبل تشغيل المفتاح:** طبّق `db/migrations/0007_payment_credit_hardening.sql` على Supabase (SQL Editor، كاملاً دفعة واحدة). بدونه يفشل الشحن التلقائي بأمان
(خطأ داخلي ← مراجعة يدوية) ولا يتأثر الشحن اليدوي. نص «ساعة إلى 48 ساعة» بشاشة الطالب يتبدّل تلقائياً بنص الشحن السريع عند التفعيل.

## 🛰️ ملاحظات Heroku
- أحداث `screen`/`manual_request` لا تُعلَّم معالَجة إلا بنهاية المعالجة. لو أُعيد تشغيل الـ dyno (نشر/دورة يومية) وسط المعالجة يلتقطها الـ watchdog بعد 3 دقائق
  (`PAYMENT_STALLED_EVENT_MINUTES`) ويرسل تنبيهاً للمجموعة بدل أن تضيع الدفعة بصمت.
- طلب `awaiting_verification` بلا قرار أدمن ينتهي بعد 48 ساعة (`PAYMENT_AWAITING_EXPIRY_HOURS`) ليتحرر الطالب لطلب جديد؛ تبقى الدفعة المتأخرة قابلة للشحن اليدوي.
- حدّ Upstash Redis: لو نفد، قراءة المفاتيح تعود لـ Supabase (أبطأ) والـ rate-limit/heartbeat قد يفشلان؛ الشحن نفسه (Supabase) لا يتأثر.

ℹ️ **اتجاه الحوالة (وارد/صادر) لا يُفحص عمداً** (قرار صاحب المشروع): كود الطلب سري ويُعطى لصاحب الطلب فقط، فوجوده
بالملاحظة كافٍ. لا حاجة لإشعار MacroDroid ولا لكلمات اتجاه؛ المصادقة تعتمد على حدث السجل (`screen`) وحده.

## ما ليس جاهزاً بعد
ربط heartbeat بقرار «التحقق اليدوي من البداية»، وعرض قائمة الطلبات المعلّقة بلوحة الأدمن.
