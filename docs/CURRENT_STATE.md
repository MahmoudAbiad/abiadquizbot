# المرجع المعماري والحالة الحالية للمشروع — abiadquizbot (`CURRENT_STATE.md`)

> **آخر تحديث شامل:** نوفمبر 2026  
> **الغرض من هذا الملف:** يمثّل هذا المستند المرجع المعماري الموحّد للحالة الفعلية للكود المصدري في المستودع. يُقرأ قبل إجراء أي تعديل جديد لفهم البنية الحالية، تدفق البيانات، القيود المعمارية، وترتيب الطبقات. للسجل التاريخي المتسلسل للتعديلات السابقة راجع [`docs/HISTORY_LOG.md`](HISTORY_LOG.md).

## 1. نظرة معمارية عامة

يعمل المشروع كبوت تيليجرام غير متزامن (Async Python 3.11 / `aiogram v3`) مع خادم ويب مدمج (`FastAPI` + `uvicorn`) وسيرفر رسم وتصدير منفصل اختياري (`render_worker.py`).

### المكونات الأساسية للبنية التحتية:
1. **عملية البوت الرئيسية (`main.py` / `webhook_server.py`):**
   - في بيئة الإنتاج (عند ضبط `WEBHOOK_URL`): تشغّل خادم FastAPI الذي يستقبل تحديثات تيليجرام على `/webhook` ويودعها ذرياً في طابور Redis (`WebhookQueue`) قبل إرجاع `200 OK`، بالإضافة لتقديم تطبيقات الـ Mini Apps (`/webapp`) ونقاط نهاية الرفع، الدفع، لعبة Runner، وإحصاءات الموقع العام.
   - في بيئة التطوير (بدون `WEBHOOK_URL`): تشغّل وضع `Polling` مع تشغيل نفس الحلقات الخلفية الدورية.
2. **سيرفر الرسم والتصدير المنفصل (`render_worker.py`):**
   - يعمل من نفس المستودع عند ضبط `APP_ROLE=render_worker`.
   - لا يستورد تيليجرام أو Supabase أو Redis، بل يدير مسبح عمليات فرعية (`ProcessPoolExecutor` بعمليتين معزولتين) لتنفيذ مهام Matplotlib / Pillow / ReportLab / python-docx / ffmpeg الثقيلة دون استهلاك ذاكرة عملية البوت الرئيسية (المحدودة بـ 512MB).
3. **قاعدة البيانات (`Supabase / PostgreSQL`):**
   - المخزن الدائم للمستخدمين، المحفظة الذرية والهدايا، الكويزات، المفضلة، لوحة الشرف، جلسات الكويز الجماعي وأقفالها، ملفات تعريف التصدير، الموافقات القانونية، سجلات الدفع، بنك التحديات والمباريات، بيانات لعبة Runner، والتحليلات.
4. **التخزين السريع والحالة (`Redis / Upstash`):**
   - يُستخدم لتخزين حالات FSM (بانتهاء صلاحية 24 ساعة)، طابور الـ Webhook الموثوق، أقفال المستخدمين الموزعة (`UserLock`)، كاش تصنيف المحتوى (`subject_classify:v3:`)، نقاط حفظ دفعات التوليد (`batch_generation` لمدة ساعتين)، لقطات التدرب على الأخطاء، حالة وضع الصيانة، وحضور المشاهدين في التحديات.
5. **التخزين السحابي للملفات (`Cloudflare R2` + `Supabase Storage`):**
   - **Cloudflare R2 (`helpers/r2_helper.py`):** يستقبل الملفات الكبيرة المرفوعة مباشرة من متصفح الطالب عبر روابط موقّعة (`Presigned PUT URLs`) للمستندات (حتى 100MB)، الصوتيات (حتى 250MB)، وألبومات الصور (حتى 50 صورة)، ويُنظَّف تلقائياً بعد المعالجة أو عبر حلقة التنظيف الدورية.
   - **Supabase Storage (`quiz-images`):** يحفظ صور الأسئلة الرياضية المرسومة وصور لعبة Runner.

## 2. ترتيب الـ Middlewares والـ Routers (حرج جداً)

يعتمد سلوك البوت على الترتيب الدقيق لتسجيل الـ Middlewares والـ Routers في `main.py`:

### أولاً: Update Outer Middlewares (تُنفَّذ على كل التحديثات بالترتيب)
1. **`GroupQuizOnlyMiddleware` (`middlewares.py`):** يعزل محادثات المجموعات تماماً عن معالجات الطلاب الفردية؛ لا يسمح في المجموعات إلا بأوامر إطلاق الكويز الجماعي (`/groupquiz`، `/start gq_...`) وبادئات أزرار الكويز الجماعي (`gql:`, `gqa:`, `gqn:`, `gqk:`, `gqt:`, `gqp:`, `gqc:`, `gqe:`, `gqs:`, `gqg:`, `gqgr:`, `gqchl:`, `gqchr:`, `gqchp:`).
2. **`ErrorTrackingMiddleware` (`middlewares.py`):** يربط كل تحديث بمعرّف المستخدم عبر `error_context.py` لتسجيل أي خطأ في لوحة تحليلات الأدمن تلقائياً، ويلتقط الاستثناءات غير المعالجة لإرسال اعتذار آمن للطالب.
3. **`MaintenanceMiddleware` (`middlewares.py`):** يفحص حالة الصيانة في Redis (`maintenance:<bot_id>`)؛ يمنع غير الأدمن من التفاعل أثناء الصيانة بمعدل تنبيه واحد كل 10 ثوانٍ لكل مستخدم.

### ثانياً: Message & Callback Inner Middlewares
1. **`ThrottlingMiddleware`:** حماية من التكرار السريع (1.2 ثانية للرسائل، 0.8 ثانية للأزرار) مع إعفاء الرسائل المعاد تمريرها بعد الموافقة القانونية (`consent_resumed=True`).
2. **`PracticeCallbackGuard` (`services/mistake_practice.py`):** يمنع تداخل أزرار الحفظ/المشاركة/إعادة اللعب القديمة مع جلسة «التدرب على الأخطاء» النشطة.
3. **`UsageCallbackMiddleware` (`middlewares.py`):** يسجّل نوايا استخدام الأزرار في المحادثات الخاصة بالخلفية دون حفظ بيانات حساسة.
4. **`ConsentMiddleware` (`services/consent.py`):** بوابة الموافقة على شروط الخدمة وسياسة الخصوصية. تحفظ الرسالة أو الألبوم أو ضغطة الزر المعلّقة في Redis (لمدة 15 دقيقة) وتستأنف تنفيذها تلقائياً فور ضغط «أوافق».

### ثالثاً: ترتيب الـ Routers في `dp.include_routers`
```text
1. consent_router              (أزرار الموافقة القانونية)
2. settings_router             (إعدادات الطالب وتفضيلات الإشعارات)
3. runner_router               (إطلاق لعبة رحلة أبيض)
4. session_navigation_router   (حماية الجلسات النشطة: استئناف / إنهاء ومتابعة / إلغاء)
5. challenge_game_router       (التحديات: الأرض والأبراج والهجمات غير المتزامنة cg:*)
6. challenge_live_router       (المواجهات المباشرة الحية lv:* + المشاهدة sp:*)
7. group_quiz_router           (الكويز الجماعي — يسبق start_router وquiz_runner_router لالتقاط /start gq_ وإجابات الاستفتاء الجماعية)
8. group_quiz_cert_router      (استقبال نصوص شهادات التقدير وحفل التكريم بخاص الأدمن)
9. payments_router             (الشحن الذاتي وأزرار مراجعة الدفعات pay:*)
10. gifts_router               (المحفظة وأكواد الهدايا)
11. start_router               (الترحيب، القائمة الرئيسية، الحساب، المساعدة)
12. admin_router               (لوحة تحكم الأدمن بكافة أقسامها الـ 15)
13. sharing_router             (مشاركة الكويزات)
14. audio_router               (المحاضرات الصوتية — يسبق files_router لالتقاط إلغاء الطلب الصوتي)
15. files_router               (استقبال الملفات، الصور، النصوص، Excel، الكاش، وبدء التوليد)
16. quiz_options_router        (اختيار نوع الأسئلة والصعوبة)
17. quiz_runner_router         (حل الكويز الفردي، النتيجة، المراجعة، التدرب على الأخطاء، وتعديل الأسئلة)
18. favorites_router           (المفضلة والأقسام)
19. leaderboard_router         (لوحة الشرف)
20. export_router              (تصدير Word / PDF / Excel وإعدادات الورقة)
21. quiz_delete_router         (حذف الكويز نهائياً)
```

## 3. الأنظمة الفرعية وتفاصيلها التقنية الحالية

### 3.1 طبقة الذكاء الاصطناعي وتوليد الكويزات (`services/ai/` + `helpers/gemini_helper/`)
- **فصل المسؤوليات:**
  - حزمة `helpers/gemini_helper/` تحتفظ بواجهات الاستدعاء العامة (`generate_quiz_smart`, `generate_structured_with_cascade`, `generate_text_with_cascade`) وتدير تخطيط البرومبت، تقسيم الصفحات/الصور، وتنسيق الدفعات (`batch_generation.py`).
  - حزمة `services/ai/` تدير العقود المحايدة (`contracts.py`)، المحولات (`adapters.py` لـ Gemini وVertex وOpenAI-compatible وCloudflare Vision)، تدوير المفاتيح والتبريد (`runtime.py` و`rate_limits.py`)، ميزانيات الوقت (`time_budget.py`)، مستويات التفكير (`thinking.py`)، جلسات الملفات (`file_session.py`)، والتشخيصات (`diagnostics.py`).
- **التوليد الموحّد بالدفعات (`batch_generation.py`):**
  - يعالج طلبات التوليد الموسّع (>30 سؤالاً أو >15 صفحة/صورة) وطلبات **استخراج الاختبارات الجاهزة** (`QUIZ_EXTRACTION_MODE_AS_IS` / `AI_SOLVE`) عبر دفعات سقفها 20 سؤالاً للدفعة.
  - يحفظ الدفعات المنجزة في Redis لمدة ساعتين لاستئنافها عند إعادة المحاولة، ويعيد استخدام الملفات المرفوعة إلى Gemini Files API ضمن نفس المفتاح عبر `GeminiFileSession`.
- **تقدير الوقت والنبضة (`generation_eta.py` + `generation_heartbeat.py`):**
  - استُبدلت عبارات الانتظار العشوائية في مسار توليد الكويز بحلقة نبضة تحسب `typical / slow / ceiling` وتحدّث شريط التقدم كل 5–15 ثانية دون عداد سالب.

### 3.2 النظام المالي، المحفظة الذرية، الهدايا، والشحن الذاتي
- **المحفظة الذرية (`wallet_debit` في SQL + `helpers/supabase_helper/users.py` & `gifts.py`):**
  - الأرصدة مقسمة إلى: `free_points` (تتجدد يومياً)، `point_gifts` (هدايا بصلاحية زمنية)، و`paid_points` (رصيد مدفوع دائم).
  - يتم الخصم ذرياً عبر RPC `wallet_debit` بالترتيب: المجاني ← الهدايا الأقدم انتهاءً ← المدفوع، ويُسجّل إيصال تقسيم الخصم في `wallet_debits` لضمان استرجاع كل جزء لمصدره الأصلي عند الفشل (`refund_user_points_split`).
  - تميّز طبقة التطبيق عبر `WalletDebitError` (`services/wallet_errors.py`) بين نقص الرصيد الفعلي وبين أخطاء الشبكة/RPC التقنية حتى لا تُحذف مدخلات الطالب عند حدوث خطأ تقني عابر.
- **التجديد اليومي (`services/daily_renewal.py`):**
  - يعمل عبر `pg_cron` الساعة **20:00 بتوقيت دمشق** (`daily_renewal_cycle_day`) لتحديث `free_points` إلى الحد اليومي، مع عامل خلفية يرسل الإشعارات للطلاب الذين فعّلوا الإشعار وتقرير إجمالي للأدمن.
- **الشحن الذاتي (`services/payment_service.py` + `payment_parser.py`):**
  - يستقبل أحداث تطبيق MacroDroid على `/api/payments/macrodroid` (`notification`, `screen`, `manual_request`, `heartbeat`).
  - يدعم التأكيد اليدوي من مجموعة الأدمن أو **الشحن التلقائي** (`payments_auto_confirm`) بشروط صارمة (تطابق المبلغ والعملة، كود طلب وحيد من 6 أرقام، أحدث بطاقة في شاشة شام كاش، وعمر الحوالة ≤ 15 دقيقة، ضمن السقف اليومي وسقف الطلب).

### 3.3 نظام التصدير وسيرفر الرسم المنفصل (`export_service.py` + `render_worker.py`)
- **إعدادات الورقة (`services/export_options.py` + `export_layout.py`):**
  - شاشة إعدادات مقسمة إلى 3 أقسام فرعية (`بيانات الورقة`، `التخطيط والمظهر`، `الإجابات والطالب`) بالإضافة إلى `قوالب جاهزة` و`تعبئة سريعة` برسالة واحدة، وحفظ الإعدادات الدائمة في جدول `export_profiles`.
  - تدعم حتى **16 سؤالاً بالصفحة**، تقسيم الصفحة إلى **عمودين** بخط فاصل اختياري، ومسودة حل تحت كل سؤال (أسطر / شبكة مربعات / إطار فارغ).
- **حماية الذاكرة والتفويض (`offload.py` + `processing_limits.py`):**
  - تشترك عمليات التوليد الثقيلة والتصدير المحلي في `HEAVY_PROCESSING_SEMAPHORE` (حد 2 لكل عملية) بالإضافة إلى طابور التصدير (`EXPORT_SLOT` بحد أقصى 8 طلبات بالانتظار، طلب واحد لكل مستخدم).
  - عند تفعيل `OFFLOAD_ENABLED=1`، تُرسل طلبات رسم الصور، الشهادات، حفل التكريم، وتصدير PDF/Word إلى `render_worker.py` عبر HTTP محمي بـ `X-Worker-Secret`.

### 3.4 الكويز الجماعي (`handlers/group_quiz.py` + `services/group_*`)
- **الإعداد الموحد (`gqs:`):** شاشة ملخص واحدة في خاص الأدمن تتحكم بنوع الجلسة (تنافسي/مجهول)، إظهار الأسماء، عدد الأوائل (`top_n`)، المؤقت، الفاصل الزمني أو الإرسال دفعة واحدة (`burst`)، قفل المحادثة، الشهادات، وحفل التكريم.
- **قفل المحادثة (`group_quiz_lock.py`):** يحفظ الصلاحيات الأصلية للمجموعة في جدول `group_quiz_chat_locks` قبل تقييد إرسال الرسائل، ويستعيدها حتمياً عند نهاية الجلسة أو عبر حلقة النبضة عند إعادة تشغيل السيرفر.
- **التكريم البصري (`certificate_renderer.py` + `ceremony_renderer.py`):** بعد نشر الترتيب النهائي، يرسل البوت (اختيارياً) فيديو/صورة حفل التكريم باللهب والموسيقى متبوعاً بألبوم شهادات التقدير للمتصدرين.

### 3.5 التحديات (`challenge_*`) ولعبة «رحلة أبيض» (`runner_*`)
- **التحديات:** تعمل بالكامل بالاعتماد على PostgreSQL RPCs الذرية (دون الاعتماد على FSM لمجرى المباراة). تشمل بنك المحتوى (`0008`)، الأرض والأبراج والهجمات غير المتزامنة (`0009` + `0011`–`0014`)، المواجهات الحية المباشرة (`0010`)، وضع المشاهدة الحية للمدافع (`0015`)، وأرشفة/حذف الفروع (`0016`).
- **لعبة رحلة أبيض (Runner):** تعمل كتطبيق ويب مصغر (`/webapp/runner`) يتواصل مع `/api/runner/*`. محمية بحراسة ثلاثية (`RUNNER_ENABLED=1` + مطابقة مرجع مشروع Supabase + مفتاح `runner_enabled` في لوحة الأدمن).

## 4. الحلقات الخلفية الدورية (Background Loops)

تعمل الحلقات التالية في كلا وضعَي التشغيل (`Webhook` في `webhook_server.py` و`Polling` في `main.py`):

| الحلقة | الملف المصدر | الوظيفة والدورية |
|---|---|---|
| `group_quiz_heartbeat_loop` | `handlers/group_quiz.py` | إرسال الأسئلة المجدولة، إغلاق الجلسات المنتهية، واستعادة أقفال المجموعات (كل 12 ثانية) |
| `group_quiz_cleanup_loop` | `services/group_quiz_store.py` | حذف جلسات الكويز الجماعي المنتهية الأقدم من 90 يوماً والجلسات المعلقة (كل 12 ساعة) |
| `challenge_heartbeat_loop` | `handlers/challenge_game.py` | متابعة مهل الهجمات غير المتزامنة وإرسال إشعارات التحديات |
| `challenge_live_loop` | `handlers/challenge_live.py` | مطابقة طابور المواجهات المباشرة ومتابعة مؤقتات الأسئلة الحية |
| `challenge_spectator_loop` | `handlers/challenge_spectator.py` | تحديث شاشات المشاهدين وتنظيف جلسات المشاهدة المنتهية في Redis |
| `runner_cleanup_loop` | `services/runner_cleanup.py` | تنظيف جولات وصور لعبة Runner المنتهية من Storage والقاعدة (كل ساعة عند التفعيل) |
| `pending_cleanup_loop` | `handlers/session_navigation.py` | تنظيف طلبات الرفع المعلّقة المنتهية (>30 دقيقة) من R2 وRedis (كل دقيقة) |
| `gift_campaign_loop` | `services/gift_campaigns.py` | تنفيذ منح وإرسال إشعارات حملات الهدايا الجماعية المباشرة |
| `daily_renewal_loop` | `services/daily_renewal.py` | إرسال إشعارات التجديد اليومي للطلاب وتقرير الأدمن اليومي |
| `scheduled_analytics_batch_loop` | `webhook_server.py` | تفريغ طابور أحداث التحليلات من Redis إلى Supabase (كل 60 ثانية — وضع Webhook) |
| `scheduled_cleanup_loop` | `webhook_server.py` | تنظيف التنزيلات المحلية (>48 ساعة)، ملفات R2 اليتيمة (>ساعة)، والأحداث/الكويزات القديمة (كل 12 ساعة — وضع Webhook) |
| `payment_watchdog_loop` | `services/payment_service.py` | إنهاء طلبات الدفع المنتهية والتنبيه عن إشعارات الدفع اليتيمة أو العالقة (كل 60 ثانية — وضع Webhook) |

## 5. نقاط نهاية HTTP (`webhook_server.py` & `render_worker.py`)

### خادم البوت (`webhook_server.py`)
- `GET/HEAD /` و`GET/HEAD /health`: فحص جاهزية السيرفر.
- `POST /webhook`: استقبال تحديثات تيليجرام (محمي بـ `X-Telegram-Bot-Api-Secret-Token`).
- `GET /api/public-stats`: إحصاءات الموقع العام (عدد الطلاب والأسئلة، مفعّل عند `PUBLIC_WEBSITE_STATS_ENABLED=true`).
- `POST /api/payments/macrodroid`: استقبال أحداث الدفع من تطبيق MacroDroid (محمي بـ `X-Payment-Secret`).
- `POST /api/audio-upload/init` و`/complete`: رفع المحاضرات الصوتية عبر Mini App إلى R2.
- `POST /api/file-upload/init` و`/complete`: رفع المستندات عبر Mini App إلى R2.
- `POST /api/image-upload/init` و`/complete`: رفع ألبومات الصور عبر Mini App إلى R2.
- `POST /api/upload-failure`: تسجيل أخطاء الرفع من المتصفح في التحليلات.
- `POST /api/question-edit/fetch` و`/save`: جلب وحفظ تعديلات الأسئلة الرياضية والجداول والمصفوفات.
- `GET/POST /api/runner/*` (`services/runner_api.py`): نقاط نهاية لعبة «رحلة أبيض» (`/config`, `/start`, `/question`, `/ready`, `/answer`, `/track`, `/coins`, `/buy-jetpack`, `/buy-car`, `/equip`, `/abandon`, `/settings`, `/ai-times`).

### سيرفر الرسم المنفصل (`render_worker.py`)
- `GET/HEAD /` و`GET/HEAD /health`: فحص جاهزية الـ Worker (يعلن الجاهزية بعد إحماء عمليتي الرسم `WORKER_WARMUP=1`).
- `POST /v1/render/question-image`: رسم صورة PNG لسؤال رياضي أو سؤال لعبة Runner.
- `POST /v1/render/certificate`: رسم صورة PNG لشهادة تقدير الكويز الجماعي.
- `POST /v1/render/ceremony`: رسم صورة PNG أو توليد فيديو MP4 لحفل التكريم.
- `POST /v1/export/quiz`: بناء ملف PDF أو Word لكويز كامل (مع دعم كافة إعدادات `export_options`).
- `POST /v1/export/document`: بناء ملف PDF أو Word لنص حر (تفريغ/ملخص محاضرة صوتية).

## 6. قواعد الاستمرارية والفحص قبل أي تعديل جديد

1. **فحص تصادم بادئات `callback_data`:**
   تأكد دائماً من عدم تداخل أي `callback_data` جديد مع البادئات المسجلة حالياً (مثل `qtype_`, `qdiff_`, `qcount_`, `cachefilter_`, `use_multi_`, `xo|`, `gqs:`, `gql:`, `gqa:`, `gqn:`, `gqk:`, `gqt:`, `gqp:`, `gqc:`, `gqe:`, `gqg:`, `gqch`, `pay:`, `gift`, `cg:`, `lv:`, `sp:`, `consent:`, `snav:`, `mprac:`, `student_`, `admin_`) والتزم بحد تيليجرام الأقصى (64 بايت).
2. **الأمان المالي:**
   لا تخصم نقاطاً إلا عبر الدوال الذرية المعتمدة (`update_user_stats` / `wallet_debit`)، وتعامل دائماً مع `WalletDebitError` بشكل منفصل عن رفض الرصيد (`None`)، واحتفظ ببيانات الخصم (`debit_id` / تقسيم الأرصدة) لضمان الاسترجاع الكامل أو الجزئي عبر `refund_user_points_split` عند فشل التوليد.
3. **التحقق الآلي قبل التسليم:**
   ```bash
   python3 -m compileall -q .
   python3 tests/smoke_import_test.py
   python3 -m unittest discover -s tests -p 'test_*.py'
   ```
