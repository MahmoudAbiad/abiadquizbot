# 🚀 Quiz Maker Bot - Webhook Version

> **Telegram Bot مدعوم بـ AI لتوليد الكويزات الذكية من PDF والصور**

## ✨ المميزات:

- 🤖 **توليد أسئلة ذكية** بمساعدة Google Gemini AI
- 📄 **معالجة PDF و الصور** مع OCR تلقائي
- ⚡ **تشغيل سريع** عبر Webhook
- ☁️ **استضافة سحابية** على Azure/Heroku/Docker
- 💾 **قاعدة بيانات** مع Supabase
- 👥 **إدارة مستخدمين** مع نظام النقاط والإحالات
- 📊 **إحصائيات شاملة** للمسؤولين

---

## 🎯 البدء السريع:

### الخطوة 1: الإعدادات
```bash
# 1. انسخ ملف البيئة
cp .env.example .env

# 2. املأ المعلومات المطلوبة (انظر SETUP_CHECKLIST.md)
nano .env
```

### الخطوة 2: التثبيت
```bash
# تثبيت المكتبات
pip install -r requirements.txt
```

### الخطوة 3: التشغيل المحلي
```bash
# بدون Webhook (وضع Polling)
python main.py
```

### الخطوة 4: النشر على Azure
```bash
# انظر AZURE_DEPLOYMENT.md للتفاصيل الكاملة
```

---

## 📋 الملفات المطلوبة:

**تم إنشاؤها:**
- ✅ `main.py` - نقطة البداية
- ✅ `webhook_server.py` - خادم FastAPI
- ✅ `config.py` - إعدادات البوت
- ✅ `constants.py` - الثوابت
- ✅ `logger.py` - نظام التسجيل
- ✅ `requirements.txt` - المكتبات

**انسخها من النسخة الأصلية:**
- 📄 `handlers/start.py`
- 📄 `handlers/quiz.py`
- 📄 `handlers/admin.py`
- 📄 `validators.py`
- 📄 `keyboards.py`
- 📄 `utils.py`
- 📄 `supabase_helper.py`
- 📄 `gemini_helper.py`

---

## 🔑 المعلومات المطلوبة:

انظر `SETUP_CHECKLIST.md` لتفاصيل مكتملة عن:
- Telegram Bot Token
- Google Gemini API Keys
- Supabase Configuration
- Azure Account Setup
- Admin ID

---

## 🗃️ جداول Supabase الإضافية

لإتاحة ميزات **مشاركة الكويز** و**القائمة المفضلة المنظمة**، أنشئ الجداول التالية في Supabase:

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

ملاحظات مهمة:

- جدول `favorite_quiz_sections` مخصص للأقسام فقط.
- جدول `favorite_quizzes` مخصص للكويزات المحفوظة فقط، ويرتبط بالقسم عبر `section_id`.
- في النشر الحالي، يتم استخدام `created_at` كمُعرّف عملي للكويز المحفوظ إذا لم يكن هناك `favorite_id`.
- حقل `title` داخل `favorite_quizzes` أصبح اسم الكويز المخصص الذي يختاره المستخدم قبل الحفظ.
- حقل `source_title` يحفظ اسم المصدر الأصلي لسهولة التتبع.
- يمكن حفظ الكويز داخل قسم أو بدون قسم، مع حد أقصى 20 قسمًا لكل مستخدم.
- يوجد تصفح مستقل للكويزات المحفوظة وتصفح مستقل للأقسام.
- من داخل المفضلة يمكن البحث والفرز حسب الأحدث أو حسب القسم.

إذا لم تُنشأ هذه الجداول أو الحقول الجديدة، سيستمر البوت في العمل للأوامر والاختبار العادي، لكن أزرار المشاركة والمفضلة المنظمة لن تحفظ البيانات بشكل دائم.

---

## 📚 الوثائق:

- **[AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md)** - شرح النشر على Azure
- **[SETUP_CHECKLIST.md](SETUP_CHECKLIST.md)** - قائمة التحقق الكاملة
- **[.env.example](.env.example)** - قالب متغيرات البيئة

---

## 🏗️ الهيكل:

```
my_quiz_bot_webhook/
├── main.py              # نقطة الدخول
├── config.py            # إعدادات البوت
├── webhook_server.py    # خادم Webhook
├── constants.py         # الثوابت
├── logger.py            # التسجيل
├── requirements.txt     # المكتبات
├── .env                 # متغيرات البيئة (لا تشارك!)
├── .env.example         # قالب
├── .gitignore           # ملفات Git المتجاهلة
├── Dockerfile           # للحاويات
├── Procfile             # لـ Heroku
├── app.json             # إعدادات التطبيق
├── AZURE_DEPLOYMENT.md  # شرح Azure
├── SETUP_CHECKLIST.md   # قائمة التحقق
├── handlers/
│   ├── start.py         # أوامر البدء
│   ├── quiz.py          # منطق الكويز
│   └── admin.py         # أوامر الإدارة
├── downloads/           # ملفات محملة
└── logs/                # السجلات
```

---

## 🚀 النشر:

### على Azure:
```bash
az webapp create --resource-group rg --plan plan --name app-name --runtime PYTHON|3.11
# ... ثم متابعة الخطوات في AZURE_DEPLOYMENT.md
```

### على Docker:
```bash
docker build -t quiz-bot .
docker run -e BOT_TOKEN=xxx -e WEBHOOK_URL=yyy quiz-bot
```

### على Heroku:
```bash
git push heroku main
```

---

## 🛠️ التطوير المحلي:

```bash
# تفعيل البيئة الافتراضية
python -m venv venv
source venv/bin/activate  # أو venv\Scripts\activate على Windows

# تثبيت الحزم
pip install -r requirements.txt

# التشغيل
python main.py
```

---

## 📝 الملاحظات:

- تحقق من `SETUP_CHECKLIST.md` أولاً
- اختبر محلياً قبل الرفع
- لا تشارك `.env` على GitHub
- استخدم 3 مفاتيح Gemini على الأقل
- فعّل Webhook بعد النشر

---

## 📞 الدعم:

إذا واجهت مشاكل:
1. افحص السجلات في `logs/`
2. تحقق من متغيرات `.env`
3. جرّب الاختبار المحلي أولاً

---

**تم تطوير هذا المشروع لتوفير تجربة كويز ذكية وسريعة! 🎓**
