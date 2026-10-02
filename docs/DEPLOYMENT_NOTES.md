# ملاحظات النشر والنقل

**المنصة الحالية: Heroku** (تأكيد المالك). سبق أن تنقّل البوت بين Render وRailway وAzure، فبعض الملفات والتعليقات تذكرها (انظر `docs/deploy.md` للدليل العام).
الهدف: إمكانية النقل لمنصة أخرى بأي وقت **دون تعديل كود**، فقط متغيرات بيئة وملف نشر مناسب.

> آخر تحديث: 2026-10-02. ما فُحص هو الكود ساكناً؛ لم تُجرَّب منصة بديلة فعلياً.

## 1) ما يجعل الكود محمولاً

- كل الإعدادات متغيرات بيئة. القائمة الكاملة بأسمائها (بلا قيم) في `.env.example`.
- `WEBHOOK_URL` موجود ← وضع Webhook (uvicorn + FastAPI، `/health` متاح). غير موجود ← وضع Polling.
- `WEBAPP_PUBLIC_BASE_URL` **مشتق تلقائياً** من `WEBHOOK_URL` (`constants.py`)، فلا يلزم ضبطه يدوياً. روابط الـ Mini App تتغير مع الدومين الجديد.
- الحالة كلها خارجية: Supabase (بيانات)، Cloudflare R2 (ملفات)، Redis/Upstash (FSM). مجلد `downloads/` مؤقت ويُمسح عند الإقلاع.
- ffmpeg عبر `imageio-ffmpeg` (pip)، بلا حزم نظام. يعمل على Heroku buildpack وDocker.

## 2) أي ملف نشر لأي منصة

| المنصة | الملف | ملاحظة |
|---|---|---|
| **Heroku (الحالية)** | `Procfile` (+ `app.json`، + `.python-version` = 3.11) | **لا تحذف `Procfile` ولا `app.json` ما دام المشروع على Heroku.** |
| غيرها (Docker) | `Dockerfile` (python:3.11-slim) | تأكد أن إصدار Python يطابق `.python-version` |
| Azure | حسب `docs/deploy.md` | دليل مختصر |

`Procfile` و`Dockerfile` كلاهما يضبط `MALLOC_ARENA_MAX=2` و`MALLOC_TRIM_THRESHOLD_=131072` (انظر البند 4-ب).

## 3) قائمة فحص يوم النقل

1. جهّز كل المتغيرات من `.env.example` على المنصة الجديدة، وانسخ القيم الحالية من Heroku (`heroku config -s`) إلى مكان آمن خارج المستودع.
2. اضبط `WEBHOOK_URL` على الدومين الجديد، وأبقِ `TELEGRAM_WEBHOOK_SECRET` (مطلوب وإلا يفشل الإقلاع).
3. **R2 CORS:** الرفع من المتصفح يتم مباشرة إلى R2 عبر presigned URLs. لو الـ CORS مقيّد بدومين Heroku أضف الدومين الجديد (لم يُتحقق منه بعد).
4. **روابط ثابتة خارج الكود:** BotFather (Menu Button/Web App) وموقع `abiad.me` قد تشير لدومين Heroku.
5. **اختر وقتاً هادئاً:** الإقلاع ينفّذ `delete_webhook(drop_pending_updates=True)`، فالرسائل المعلّقة لحظة التبديل تضيع.
6. **لا تشغّل نسختين معاً:** حلقات الخلفية (نبضة الكويز الجماعي، التنظيف، التحليلات) داخل العملية؛ أوقف القديمة فور سلامة الجديدة. وبوضع Polling لا تشغّل نسختين أبداً.
7. بعد النقل: افحص `/health`، وجرّب دورة كاملة (رفع ← توليد ← عرض الكويز) وزر صفحة الرفع (WebApp).

## 4) ضبط مرتبط بمنصة/مزوّد (أعد التحقق عند تغييره)

أ. **`config.py` (اتصالات Redis الخاملة):** `health_check_interval` و`socket_keepalive` و`retry_on_error` صُمّمت لمشكلة **Upstash عبر Heroku** (يقفل الاتصالات الخاملة بصمت). إن بقي نفس مزوّد Redis فلا شي يتغير؛ وإن تغيّر المزوّد تحقق من سلوكه.

ب. **حد الذاكرة:** `MALLOC_ARENA_MAX=2` و`MALLOC_TRIM_THRESHOLD_` مضبوطان لحد ذاكرة صغير (Heroku/Docker). راقب الاستهلاك على المنصة الجديدة؛ قد يكون التضييق زائداً أو غير كافٍ.

ج. **عميل Gemini (Singleton):** التصميم (`helpers/gemini_helper/clients.py`) نشأ بسبب Memory Exceeded رُصد أول مرة على Render، والسلوك عام لا يخص منصة بعينها.

د. **ffmpeg/ffprobe (`handlers/audio.py`):** الحل مصمَّم أصلاً لغياب `ffprobe` على Heroku buildpack، ولا يعتمد على النظام، فهو آمن على كل المنصات.

## 5) تنبيه
قاعدة البيانات لا يمكن إعادة بنائها من هذا المستودع (migrations ناقصة وغير مرقّمة، ولا يوجد schema أساسي). النقل لمشروع Supabase آخر يحتاج baseline من القاعدة الحية (`supabase db dump --schema-only`). هذا بند منفصل في الخطة.
