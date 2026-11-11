# دليل النشر والتشغيل السحابي (`deploy.md`)

يوضح هذا الدليل كيفية تشغيل ونشر **بوت أبيض (`abiadquizbot`)** وسيرفر الرسم والتصدير المرافق له (`render_worker`) على مختلف المنصات. لملاحظات النقل بين المنصات وقائمة الفحص راجع [`docs/DEPLOYMENT_NOTES.md`](DEPLOYMENT_NOTES.md).

## 1. أوضاع التشغيل (`main.py`)

يحدد متغير البيئة `APP_ROLE` و`WEBHOOK_URL` طريقة عمل الحاوية عند تشغيل `python main.py`:

1. **وضع Webhook (الإنتاج — خادم البوت الرئيسي):**
   - يتفعّل عندما يكون `APP_ROLE` فارغاً و`WEBHOOK_URL=https://<your-domain>` مضبوطاً (بدون `/` في النهاية) مع `TELEGRAM_WEBHOOK_SECRET`.
   - يشغّل خادم FastAPI (`webhook_server:app`) عبر `uvicorn`.
   - عند الإقلاع يستدعي `set_webhook` مع **`drop_pending_updates=False`** للحفاظ على التحديثات المعلّقة أثناء إعادة التشغيل، ويودع التحديثات الواردة في طابور Redis الموثوق (`WebhookQueue`) قبل إرجاع `200 OK`.
   - نقطة فحص الجاهزية: `GET /health`.
2. **وضع Polling (التطوير المحلي):**
   - يتفعّل تلقائياً عندما يكون `WEBHOOK_URL` فارغاً.
   - يحذف أي Webhook قديم (`drop_pending_updates=True`) ويشغّل جميع الحلقات الخلفية الدورية (الكويز الجماعي، التحديات، التجديد اليومي، الهدايا، التنظيف) ثم يبدأ `dp.start_polling(bot)`.
3. **وضع سيرفر الرسم والتصدير المستقل (`APP_ROLE=render_worker`):**
   - عند ضبط `APP_ROLE=render_worker`، يحوّل `main.py` التنفيذ فوراً إلى `render_worker.py` (`os.execv`) دون استيراد تيليجرام أو Supabase أو Redis.
   - يمكن أيضاً تشغيله مباشرة بأمر `python render_worker.py` مع تثبيت `requirements.worker.txt`.

## 2. ملفات النشر حسب المنصة

| المنصة / الخدمة | الملفات المعتمدة | أمر التشغيل |
|---|---|---|
| **Heroku (البوت الرئيسي)** | `Procfile`, `app.json`, `.python-version` (3.11), `requirements.txt` | `env MALLOC_ARENA_MAX=2 MALLOC_TRIM_THRESHOLD_=131072 python main.py` |
| **Render / Heroku (سيرفر الرسم)** | `requirements.worker.txt`, `.python-version` (3.11) | `env MALLOC_ARENA_MAX=2 MALLOC_TRIM_THRESHOLD_=131072 python render_worker.py` |
| **Docker (أي منصة حاويات)** | `Dockerfile`, `.dockerignore` | مضمّن داخل `Dockerfile` (`CMD ["python", "main.py"]`) |

## 3. خطوات النشر

### أ) Heroku (المنصة الحالية للبوت الرئيسي)
```bash
# ضبط المتغيرات الأساسية
heroku config:set BOT_TOKEN=... ADMIN_ID=... SUPABASE_URL=... SUPABASE_KEY=... REDIS_URL=...
heroku config:set WEBHOOK_URL=https://<your-app>.herokuapp.com TELEGRAM_WEBHOOK_SECRET=...
heroku config:set GEMINI_API_KEYS=key1,key2 R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=...

# دفع الكود للنشر
git push heroku main
```

### ب) Docker
```bash
docker build -t abiadquizbot .
docker run -d --name abiadquizbot --env-file .env -p 8080:8080 abiadquizbot
```

### ج) سيرفر الرسم والتصدير المنفصل (`render_worker`)
لتخفيف حمل الذاكرة والمعالج عن حاوية البوت الرئيسية (512MB):
1. أنشئ خدمة ويب ثانية من نفس المستودع.
2. أمر البناء: `pip install -r requirements.worker.txt`.
3. أمر التشغيل: `python render_worker.py` (أو اترك `Procfile` واضبط `APP_ROLE=render_worker`).
4. اضبط على سيرفر الرسم: `OFFLOAD_WORKER_SECRET=<سر-مشترك-طويل>`، `WORKER_CONCURRENCY=2`، `WORKER_MAX_QUEUE=8`، `WORKER_WARMUP=1`.
5. اضبط على خادم البوت الرئيسي: `OFFLOAD_ENABLED=1`، `OFFLOAD_WORKER_URL=https://<worker-domain>`، ونفس `OFFLOAD_WORKER_SECRET`.
- التفاصيل الكاملة في [`docs/offload.md`](offload.md).

## 4. استكشاف الأخطاء الشائعة

- **فشل إقلاع السيرفر في وضع Webhook:** تأكد من ضبط `TELEGRAM_WEBHOOK_SECRET` ومن سلامة اتصال Redis وSupabase؛ صُمم `lifespan` في `webhook_server.py` ليفشل بصراحة (`Fail-Closed`) إذا تعذّر تسجيل الـ Webhook أو إطلاق المهام الدورية حتى لا يعمل السيرفر بصمت دون استقبال رسائل.
- **الـ Webhook لا يستقبل التحديثات:** افحص `https://api.telegram.org/bot<TOKEN>/getWebhookInfo` وتأكد من عدم وجود `/` زائدة في نهاية `WEBHOOK_URL`.
- **تأخر أول صورة بعد نشر `render_worker`:** تأكد من تفعيل `WORKER_WARMUP=1` ليقوم الـ Worker بإحماء عمليتي Matplotlib وبناء كاش الخطوط قبل إعلان جاهزيته على `/health`.
- **استنفاد حصة مزوّد الذكاء الاصطناعي:** ينتقل المحرك (`services/ai/runtime.py`) تلقائياً بين المفاتيح المتعددة والنماذج الاحتياطية المسجّلة في لوحة الأدمن (`🤖 التحكم بالذكاء الاصطناعي`).
