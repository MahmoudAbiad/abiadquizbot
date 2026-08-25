# 🤖 Quiz Maker Bot - Webhook Version for Azure

نسخة محسّنة من بوت الكويزات تدعم **Webhook** للعمل على **Azure** والسحابات الأخرى!

## ✨ الميزات الرئيسية:

- ✅ **دعم Webhook كامل** - تشغيل على Azure App Service
- ✅ **FastAPI** - سرعة واستقرار عالي
- ✅ **Telegram Bot API** - باستخدام aiogram 3.x
- ✅ **Google Gemini AI** - استخراج النصوص وتوليد الأسئلة
- ✅ **Supabase Database** - إدارة المستخدمين والإحصائيات
- ✅ **معالجة PDF و الصور** - دعم كامل لملفات متعددة
- ✅ **نظام النقاط** - مكافآت وإحالات
- ✅ **أوامر إدارية** - إدارة المستخدمين والنقاط
- ✅ **تسجيل شامل** - logging للعمليات والأخطاء

---

## 🚀 طريقة النشر على Azure:

### المتطلبات الأساسية:
1. ✅ حساب Microsoft Azure
2. ✅ Bot Token من Telegram
3. ✅ مفاتيح Google Gemini API (3 على الأقل)
4. ✅ قاعدة بيانات Supabase مع جدول `users`
5. ✅ Git و GitHub account

---

## 📋 خطوات النشر:

### الخطوة 1️⃣: تحضير المتغيرات البيئية

انسخ ملف `.env.example` إلى `.env` وأضف بيانات مشروعك:

```bash
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
GEMINI_API_KEYS=KEY1,KEY2,KEY3
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-anon-key
ADMIN_ID=your-telegram-id
WEBHOOK_URL=https://your-app-name.azurewebsites.net
PORT=8000
```

### الخطوة 2️⃣: إنشاء Azure App Service

```bash
# 1. إنشاء مجموعة موارد
az group create --name quiz-bot-rg --location eastus

# 2. إنشاء App Service Plan
az appservice plan create \
  --name quiz-bot-plan \
  --resource-group quiz-bot-rg \
  --sku B1 \
  --is-linux

# 3. إنشاء Web App
az webapp create \
  --resource-group quiz-bot-rg \
  --plan quiz-bot-plan \
  --name your-unique-app-name \
  --runtime "PYTHON|3.11"
```

### الخطوة 3️⃣: إعداد متغيرات البيئة في Azure

```bash
az webapp config appsettings set \
  --resource-group quiz-bot-rg \
  --name your-unique-app-name \
  --settings \
    BOT_TOKEN="YOUR_TOKEN" \
    GEMINI_API_KEYS="KEY1,KEY2,KEY3" \
    SUPABASE_URL="YOUR_URL" \
    SUPABASE_KEY="YOUR_KEY" \
    ADMIN_ID="YOUR_ID" \
    WEBHOOK_URL="https://your-app-name.azurewebsites.net" \
    PORT=8000
```

### الخطوة 4️⃣: نشر الكود من GitHub

```bash
# 1. ادفع الكود إلى GitHub
git add .
git commit -m "Bot webhook version for Azure"
git push origin main

# 2. اربط مستودع GitHub مع Azure
az webapp deployment github-actions add \
  --repo YOUR_GITHUB_REPO \
  --branch main \
  --resource-group quiz-bot-rg \
  --name your-unique-app-name
```

### الخطوة 5️⃣: تحديث Webhook في Telegram

بعد نشر التطبيق، قم بتحديث webhook الخاص بـ Telegram:

```bash
curl "https://api.telegram.org/botYOUR_TOKEN/setWebhook" \
  -d "url=https://your-app-name.azurewebsites.net/webhook"
```

---

## 🏃 التشغيل المحلي للاختبار:

### بـ Polling (للتطوير):

```bash
# بدون تعيين WEBHOOK_URL ستعمل بوضع Polling
python main.py
```

### بـ Webhook (اختياري محلياً):

```bash
# اضبط WEBHOOK_URL على localhost (مع استخدام ngrok)
pip install ngrok
ngrok http 8000

# ثم عدّل .env وأضف:
WEBHOOK_URL=https://xxx-ngrok.io
python main.py
```

---

## 📦 المعلومات المطلوبة منك:

لكي يعمل المشروع بنجاح على Azure، تأكد من أن لديك:

### 1. **Telegram Bot Token** ✅
```
اطلب من @BotFather على Telegram
```

### 2. **Google Gemini API Keys** ✅
```
من Google AI Studio: https://aistudio.google.com/app/apikey
(حد أدنى 3 مفاتيح للتناوب)
```

### 3. **Supabase Setup** ✅
```
جدول 'users' مع الأعمدة:
- user_id (int, primary key)
- username (text)
- points (int)
- total_questions (int)
- referred_by (int, nullable)
- last_renewal (date)
- created_at (timestamp)
```

### 4. **Azure Account** ✅
```
حساب نشط مع quota للـ App Services
```

### 5. **اسم التطبيق الفريد** ✅
```
مثلاً: my-quiz-bot-xyz-2024
(يجب أن يكون فريداً على azure.com)
```

---

## 🔄 الفرق بين النسخة القديمة والجديدة:

| المميز | الإصدار القديم | الإصدار الجديد |
|--------|------------|------------|
| **طريقة التشغيل** | Polling | Webhook ✅ |
| **الاستضافة** | محلي فقط | Azure, Heroku, Docker ✅ |
| **السرعة** | بطء (استطلاع دوري) | سريع جداً ✅ |
| **استهلاك الموارد** | عالي | منخفض ✅ |
| **الإطار** | بدون ويب | FastAPI ✅ |
| **الموثوقية** | متوسط | عالية جداً ✅ |

---

## 🛠️ استكشاف الأخطاء:

### المشكلة: "Connection timeout"
```
✅ الحل: تأكد من إعدادات جدار الحماية في Azure
تأكد من أن PORT مضبوط بشكل صحيح
```

### المشكلة: "Webhook failed"
```
✅ الحل: جرّب إعادة تعيين الـ webhook:
curl "https://api.telegram.org/botTOKEN/deleteWebhook"
curl "https://api.telegram.org/botTOKEN/setWebhook?url=YOUR_URL"
```

### المشكلة: "API quota exhausted"
```
✅ الحل: تم بناء نظام تناوب تلقائي لمفاتيح Gemini
سيتم اختيار مفتاح آخر تلقائياً
```

---

## 📞 الدعم والمساعدة:

إذا واجهت أي مشاكل:

1. **افحص السجلات (Logs)** في Azure Portal
2. **جرّب Webhook checker**: https://webhook.site
3. **اختبر Telegram API**: https://api.telegram.org/botTOKEN/getMe

---

## 📝 ملاحظات مهمة:

- ✅ المشروع يدعم Python 3.11+
- ✅ البيانات الحساسة في `.env` لن تُرفع على GitHub (استخدم `.gitignore`)
- ✅ السجلات محفوظة تلقائياً في مجلد `logs/`
- ✅ الملفات المحملة محفوظة في مجلد `downloads/`

---

**استمتع بـ Bot قوي وسريع! 🚀**

---

## 🎯 الخطوة التالية:

بعد نشر المشروع على Azure، يمكنك:
- ✅ إضافة مميزات جديدة
- ✅ تحسين واجهة المستخدم
- ✅ إضافة نظام دفع
- ✅ التوسع إلى لغات أخرى

**تم إنشاء هذا المشروع بواسطة AI** 🤖
