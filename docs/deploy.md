# دليل النشر

للنقل بين المنصات (قائمة فحص يوم النقل، محاذير، ما يرتبط بمزوّد معيّن) انظر `DEPLOYMENT_NOTES.md` (بنفس المجلد). المنصة الحالية: Heroku.

## وضعا التشغيل

- **Polling**: `WEBHOOK_URL` فارغ. للتطوير المحلي: `python main.py`.
- **Webhook** (إنتاج): `WEBHOOK_URL=https://<دومينك>` بلا `/` بالنهاية، ومعه `TELEGRAM_WEBHOOK_SECRET` (مطلوب وإلا يفشل الإقلاع). عند الإقلاع يُنفَّذ `delete_webhook(drop_pending_updates=True)` ثم `set_webhook` تلقائياً على المسار `/webhook`، فلا حاجة لاستدعاء `setWebhook` يدوياً. الفحص: `/health`.

المتغيرات كلها في `.env.example`. `SUPABASE_KEY` هو مفتاح `service_role` (سر، يتجاوز RLS)، وليس `anon`.

## الملفات حسب المنصة

| المنصة | الملف |
|---|---|
| Heroku | `Procfile` (+ `app.json`, `.python-version` = 3.11) |
| Docker / أي منصة حاويات | `Dockerfile` |
| Azure App Service | نشر بـ Docker أو بـ Python 3.11 مع أمر تشغيل `python main.py` |

```bash
# Docker
docker build -t abiadquizbot .
docker run --env-file .env abiadquizbot

# Heroku
heroku config:set NAME=value
git push heroku main
```

## Azure (مختصر، غير مجرَّب حالياً)

```bash
az group create --name quiz-bot-rg --location eastus
az appservice plan create --name quiz-bot-plan --resource-group quiz-bot-rg --sku B1 --is-linux
az webapp create --resource-group quiz-bot-rg --plan quiz-bot-plan --name <اسم-فريد> --runtime "PYTHON|3.11"
az webapp config appsettings set --resource-group quiz-bot-rg --name <اسم-فريد> --settings KEY=VALUE ...
```

اضبط `WEBHOOK_URL=https://<اسم-فريد>.azurewebsites.net` و`PORT` حسب المنصة. استخدم أمر التشغيل نفسه الموجود في `Procfile`.

## استكشاف الأخطاء

- **Webhook لا يصل**: افحص `https://api.telegram.org/bot<TOKEN>/getWebhookInfo` (آخر خطأ يظهر هناك) وتأكد أن `WEBHOOK_URL` صحيح وأن `TELEGRAM_WEBHOOK_SECRET` مضبوط.
- **Connection timeout**: افحص أن `PORT` يطابق ما تتوقعه المنصة.
- **حصة Gemini منتهية**: الكاسكيد يتنقل بين المفاتيح والمزوّدين تلقائياً (`GEMINI_API_KEYS` مفصولة بفاصلة).
- السجلات: مجلد `logs/` محلياً، ومخرجات المنصة. وSentry عند ضبط `SENTRY_DSN`.
