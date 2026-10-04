# الشحن الذاتي (شام كاش + MacroDroid) — المرحلة الأولى

**المرحلة الأولى = التقاط + مراجعة يدوية.** لا شيء يُشحن تلقائياً: كل دفعة تصل مجموعة الأدمن
بأزرار (✅ تأكيد / ❌ رفض / 🚫 تجاهل)، والشحن الفعلي بدالة ذرية `payment_credit_order`.
القبول التلقائي يُبنى لاحقاً بعد رؤية إشعارات حقيقية.

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

## ما ليس جاهزاً بعد (المرحلة 2)
القبول التلقائي، وربط heartbeat بقرار «التحقق اليدوي من البداية»، وعرض قائمة الطلبات المعلّقة بلوحة الأدمن.
