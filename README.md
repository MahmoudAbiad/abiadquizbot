# 🚀 Quiz Maker Bot - abiadquizbot

> **Telegram Bot مدعوم بـ AI لتوليد الكويزات الذكية من PDF والصور والنصوص**

## ✨ المميزات:

- 🤖 **توليد أسئلة ذكية** عبر مزوّدين قابلين للإدارة من لوحة الأدمن: Gemini وVertex وواجهات متوافقة مع OpenAI مثل Groq. راجع [دليل المزوّدين](docs/ai-providers.md) لتطبيق ترحيل قاعدة البيانات والإعداد.
- 📄 **معالجة PDF والصور والألبومات** مع استخراج نص تلقائي
- 📐 **كويزات رياضية بصيغة صورة** (LaTeX) عند اكتشاف محتوى رياضي تلقائياً
- 🎯 **تخصيص نوع الأسئلة والصعوبة** حسب المادة (رياضيات/إنجليزي/عام)
- 💾 **كاش ذكي للكويزات** مع فلترة حسب النوع/الصعوبة وسقف مستقل لكل تركيبة
- ⭐ **مفضلة منظمة بأقسام** لحفظ الكويزات
- 🔗 **مشاركة الكويزات** عبر روابط مباشرة
- 🏆 **لوحة شرف** لكل كويز (نشر/إخفاء النتيجة اختياري من الطالب)
- 📁 **تصدير Word/PDF** بأكثر من ستايل تنسيق
- ⚡ **تشغيل عبر Webhook (FastAPI)** أو Polling محلياً
- 💰 **نظام نقاط وإحالات**
- 📊 **لوحة أدمن**: إدارة مستخدمين، ملاحظات، تحليلات استخدام شاملة

---

## 🏗️ الهيكل الفعلي للمشروع:

```
abiadquizbot/
├── main.py                      # نقطة الدخول (Webhook أو Polling حسب WEBHOOK_URL)
├── webhook_server.py            # خادم FastAPI لوضع الـ Webhook
├── config.py                    # إعداد البوت، Redis، حالات FSM
├── constants.py                 # الثوابت والبرومبتات ورسائل الواجهة
├── keyboards.py                 # كل لوحات الأزرار (Inline Keyboards)
├── validators.py                # التحقق من صحة المدخلات
├── middlewares.py                # الحماية من التكرار (Throttling)
├── logger.py                    # نظام التسجيل
├── utils.py                     # أدوات مساعدة عامة
├── requirements.txt
├── Dockerfile / Procfile / app.json   # ملفات النشر (Docker / Heroku)
├── assets/fonts/                # خطوط تصدير PDF (راجع assets/fonts/README.md)
│
├── db/                          # هيكل قاعدة البيانات (baseline مرقّم + seed) — انظر db/README.md
├── docs/                        # التوثيق (analytics, token-tracking, deploy)
├── tests/                       # smoke test + gemini_equivalence_harness
├── webapp/                      # صفحات الـ Mini App (رفع الملفات/الصوت، تعديل الأسئلة)
│
├── handlers/                    # معالجات أوامر وأزرار البوت
│   ├── start.py, tutorial.py, favorites.py, sharing.py, export.py, leaderboard.py
│   ├── files.py                 # استقبال الملفات/الصور/النصوص + الكاش + بدء التوليد
│   ├── quiz_options.py          # اختيار نوع وصعوبة الأسئلة
│   ├── quiz_runner.py           # تشغيل الكويز سؤالاً سؤالاً + شاشة النتيجة
│   ├── group_quiz.py            # تشغيل الكويز ضمن الغروبات/القنوات
│   ├── audio.py, quiz_delete.py
│   └── admin/                   # لوحة الأدمن (dashboard, users, analytics, ai_control, settings, ...)
│
├── services/                    # منطق العمل (بلا تفاعل مباشر مع تيليجرام)
│   ├── quiz_service.py, quiz_engine.py, quiz_permissions.py, flow_state.py
│   ├── subject_classifier.py    # تصنيف المادة (رياضيات/إنجليزي/عام)
│   ├── file_service.py, audio_service.py, audio_flow_service.py, audio_duration_service.py
│   ├── image_quiz_renderer.py, latex_text.py, export_service.py
│   └── group_quiz_store.py, group_permissions.py
│
└── helpers/
    ├── gemini_helper/           # واجهة الكويزات: البرومبت والتقسيم والدمج؛ التنفيذ في services/ai
    ├── supabase_helper/         # حزمة: مقسّمة حسب المجال (users, quiz_cache, analytics, ...)
    ├── ai_models_helper.py, settings_helper.py, r2_helper.py, points_calculator.py
```

---

## 🎯 البدء السريع:

### الخطوة 1: متغيرات البيئة
انسخ `.env.example` إلى `.env` وعبّئ القيم (الأسماء كلها مقروءة فعلياً من الكود). بدون `WEBHOOK_URL` يعمل البوت بوضع Polling.

### الخطوة 2: التثبيت
```bash
pip install -r requirements.txt
```

### الخطوة 3: قاعدة البيانات (Supabase)
شغّل بالترتيب على مشروع Supabase جديد: `db/migrations/0001_baseline.sql` ثم `db/migrations/0002_seed_config.sql`.
التفاصيل وحالة التحقق في `db/README.md`. ملفات `db/archive/migration_*.sql` تاريخية ولا تُشغَّل على قاعدة جديدة.

### الخطوة 4: خطوط تصدير PDF
راجع `assets/fonts/README.md` لتفاصيل الخطوط المطلوبة (موجودة أصلاً بهذا المستودع).

### الخطوة 5: التشغيل المحلي
```bash
python main.py
```
بدون تعيين `WEBHOOK_URL` بالـ `.env`، يشتغل البوت تلقائياً بوضع **Polling** (مناسب للتطوير المحلي).

---

## 🚀 النشر

### Docker
```bash
docker build -t abiadquizbot .
docker run --env-file .env abiadquizbot
```

### Heroku / أي منصة تدعم Procfile
```bash
git push heroku main
```
(`Procfile` يشغّل `python main.py`، و`app.json` يحدد بيئة Python 3.11)

### Azure وغيره
راجع `docs/deploy.md`. أي نشر يعطيك عنوان URL ثابتاً: عيّنه بـ `WEBHOOK_URL` ليعمل بوضع **Webhook**.

---

## 📚 وثائق إضافية

- **[docs/deploy.md](docs/deploy.md)** — دليل النشر العام (Heroku/Docker/Azure) واستكشاف الأخطاء
- **[docs/DEPLOYMENT_NOTES.md](docs/DEPLOYMENT_NOTES.md)** — النقل بين المنصات وقائمة فحص يوم النقل
- **[docs/analytics.md](docs/analytics.md)** — التحليلات وتتبع الأخطاء والنشاط ولوحة الأدمن
- **[docs/token-tracking.md](docs/token-tracking.md)** — تتبع التوكنز والتكلفة
- **[db/README.md](db/README.md)** — هيكل قاعدة البيانات وبناء قاعدة جديدة
- **[docs/CURRENT_STATE.md](docs/CURRENT_STATE.md)** — القرارات المعمارية والمعلومات التقنية (يُقرأ قبل أي تعديل)
- **[docs/HISTORY_LOG.md](docs/HISTORY_LOG.md)** — سجل تاريخي (قد يكون قديماً، لا يُعتمد عليه)
- **[assets/fonts/README.md](assets/fonts/README.md)** — خطوط تصدير PDF

---

## 📝 ملاحظات

- لا تشارك ملف `.env` علناً (يحتوي مفاتيح حساسة).
- استخدم أكثر من مفتاح Gemini إذا كان عدد الطلاب كبيراً (تناوب تلقائي عند استنفاد الحصة).
- السجلات (logs) تُطبع عبر `logger.py`، ويمكن ربطها بـ Sentry عبر `SENTRY_DSN`.

---

**تم تطوير هذا المشروع لتوفير تجربة كويز ذكية وسريعة! 🎓**
