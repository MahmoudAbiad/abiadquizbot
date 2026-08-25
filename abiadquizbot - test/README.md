# 🚀 Quiz Maker Bot - abiadquizbot

> **Telegram Bot مدعوم بـ AI لتوليد الكويزات الذكية من PDF والصور والنصوص**

## ✨ المميزات:

- 🤖 **توليد أسئلة ذكية** بمساعدة Google Gemini AI (مع Groq كمزوّد احتياطي لتوليد الأسئلة من النص)
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
├── migration_*.sql              # ملفات SQL لتحديث قاعدة بيانات Supabase
├── assets/fonts/, Amiri/         # خطوط تصدير PDF (راجع assets/fonts/README.md)
│
├── handlers/                    # كل معالجات أوامر وأزرار البوت
│   ├── start.py                 # /start، الروابط العميقة (deep links)
│   ├── tutorial.py              # الدليل التفاعلي السريع
│   ├── files.py                 # استقبال الملفات/الصور/النصوص + الكاش + بدء التوليد
│   ├── quiz_options.py          # اختيار نوع ومستوى صعوبة الأسئلة
│   ├── quiz_runner.py           # تشغيل الكويز سؤالاً سؤالاً + شاشة النتيجة
│   ├── leaderboard.py           # عرض لوحة الشرف + تبديل نشر/إخفاء النتيجة
│   ├── favorites.py             # القائمة المفضلة المنظمة بأقسام
│   ├── sharing.py               # إنشاء وفتح روابط مشاركة الكويز
│   ├── export.py                # تصدير الكويز إلى Word/PDF
│   └── admin/                   # لوحة الأدمن (مجلد فرعي)
│       ├── dashboard.py, users.py, feedbacks.py, analytics.py
│
├── services/                    # منطق العمل الأساسي (بلا تفاعل مباشر مع تيليجرام)
│   ├── quiz_service.py          # حساب سقف الكاش، تسمية نوع الأسئلة
│   ├── quiz_engine.py           # إرسال أسئلة الكويز (Poll عادي أو صورة+Poll رياضي)
│   ├── subject_classifier.py    # تصنيف المادة (رياضيات/إنجليزي/عام) عبر Gemini
│   ├── math_detector.py, english_detector.py   # ⚠️ DEPRECATED، غير مستوردة من أي مكان
│   ├── detection_common.py
│   ├── file_service.py          # استخراج نص من PDF/صور
│   ├── image_quiz_renderer.py   # رسم أسئلة LaTeX كصورة (Matplotlib)
│   └── export_service.py        # توليد ملفات Word/PDF
│
└── helpers/
    ├── gemini_helper.py         # تكامل Google Gemini (+ Groq كبديل للنص)
    ├── supabase_helper.py       # كل التعامل مع قاعدة بيانات Supabase
    └── points_calculator.py     # حساب تكلفة توليد الكويز بالنقاط
```

---

## 🎯 البدء السريع:

### الخطوة 1: متغيرات البيئة
أنشئ ملف `.env` بجذر المشروع وعبّي القيم التالية (كلها مقروءة فعلياً من الكود):

```bash
BOT_TOKEN=your_telegram_bot_token
ADMIN_ID=your_telegram_user_id

GEMINI_API_KEYS=KEY1,KEY2,KEY3      # مفتاح واحد أو أكثر مفصولين بفاصلة (تناوب تلقائي)
GROQ_API_KEY=your_groq_key          # اختياري - بديل لتوليد الأسئلة من النص فقط

SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_supabase_key

REDIS_URL=redis://localhost:6379    # اختياري، الافتراضي أعلاه لو لم يُحدَّد

SENTRY_DSN=your_sentry_dsn          # اختياري لتتبع الأخطاء

# للنشر بوضع Webhook فقط (اتركها فاضية للتشغيل المحلي بوضع Polling):
WEBHOOK_URL=https://your-domain.com
PORT=8080
```

### الخطوة 2: التثبيت
```bash
pip install -r requirements.txt
```

### الخطوة 3: قاعدة البيانات (Supabase)
شغّل ملفات الـ migration بترتيب `SQL Editor` على مشروع Supabase عندك:
- `migration_quiz_options.sql`
- `migration_usage_analytics.sql` (راجع تفاصيلها بـ `ANALYTICS_README.md`)
- `migration_math_image_quizzes.sql`

هذه الملفات آمنة للتشغيل على قاعدة بيانات فيها بيانات موجودة أصلاً (تستخدم `ADD COLUMN IF NOT EXISTS`
ونحوها، ولا تحذف أو تعدّل أي بيانات قائمة). بالإضافة لجداول تحتاج إنشاء يدوي إذا بدك ميزات
المشاركة والمفضلة المنظمة:

```sql
create table if not exists shared_quizzes (
	share_id text primary key,
	owner_id bigint not null,
	title text not null,
	quiz_data jsonb not null,
	created_at timestamptz not null default now()
);

create table if not exists favorite_quiz_sections (
	section_id text primary key,
	user_id bigint not null,
	title text not null,
	created_at timestamptz not null default now()
);

create table if not exists favorite_quizzes (
	user_id bigint not null,
	title text not null,
	source_title text,
	section_id text,
	quiz_data jsonb not null,
	created_at timestamptz not null default now()
);
```

ملاحظات:
- جدول `favorite_quiz_sections` مخصص للأقسام فقط، و`favorite_quizzes` للكويزات المحفوظة ويرتبط بالقسم عبر `section_id`.
- يمكن حفظ الكويز داخل قسم أو بدونه، مع حد أقصى 20 قسماً لكل مستخدم.
- إذا لم تُنشأ هذه الجداول، يستمر البوت بالعمل للتوليد والاختبار العادي، لكن أزرار المشاركة والمفضلة لن تحفظ البيانات بشكل دائم.

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

### Azure
راجع `AZURE_DEPLOYMENT.md` للتفاصيل الكاملة خطوة بخطوة (إنشاء App Service، ضبط متغيرات البيئة، ربط GitHub، تحديث الـ Webhook على تيليجرام).

بشكل عام، أي نشر سحابي يعطيك عنوان URL ثابت: عيّنه بمتغير `WEBHOOK_URL` ليشتغل البوت تلقائياً بوضع **Webhook** عبر FastAPI (`webhook_server.py`) بدل الـ Polling.

---

## 📚 وثائق إضافية

- **[AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md)** — شرح مفصّل للنشر على Azure
- **[ANALYTICS_README.md](ANALYTICS_README.md)** — نظام تتبع استخدام الطلاب ولوحة تحليلات الأدمن
- **[CURRENT_STATE.md](CURRENT_STATE.md)** — القرارات المعمارية، حالة الاختبار، والمعلومات التقنية الضرورية (ملف خفيف يُقرأ روتينياً قبل أي تعديل) — مرجع لأي AI أو مطوّر يكمل الشغل
- **[HISTORY_LOG.md](HISTORY_LOG.md)** — سجل تفصيلي لكل الإصلاحات والميزات بالترتيب الزمني (Groq/Gemini، الألبوم، استرجاع النقاط، تخصيص نوع/صعوبة الأسئلة، الكويز الرياضي المصوّر، لوحة الشرف...) — يُقرأ عند الحاجة فقط، وليس روتينياً
- **[assets/fonts/README.md](assets/fonts/README.md)** — الخطوط المطلوبة لتصدير PDF بالعربي

---

## 📝 ملاحظات

- لا تشارك ملف `.env` علناً (يحتوي مفاتيح حساسة).
- استخدم أكثر من مفتاح Gemini إذا كان عدد الطلاب كبيراً (تناوب تلقائي عند استنفاد الحصة).
- السجلات (logs) تُطبع عبر `logger.py`، ويمكن ربطها بـ Sentry عبر `SENTRY_DSN`.

---

**تم تطوير هذا المشروع لتوفير تجربة كويز ذكية وسريعة! 🎓**
