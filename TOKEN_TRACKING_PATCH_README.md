# Token Tracking Patch — شرح وخطوات التطبيق

## شو تغيّر (3 تعديلات مترابطة)

### 1) `helpers/gemini_helper.py`
- ContextVar جديد `_last_token_usage_var` بيتسجّل فيه input/output/total tokens من
  `usage_metadata` (نفس شكلها على Gemini AI Studio وVertex AI) فور نجاح أي محاولة.
- دالة مساعدة جديدة `_record_token_usage(response)` استبدلت كل أسطر استخراج
  `total_token_count` المتفرقة (4 أماكن) — بقيت نفس القيمة المُرجعة (توافقية كاملة،
  صفر تغيير على أي توقيع دالة قديم عدا `_generate_single_attempt`، راجع أدناه).
- `_generate_single_attempt` (مسار Super PDF/Super Images المتوازي عبر `asyncio.gather`)
  صار يرجّع 4 قيم بدل 2 (`questions, total_tokens, input_tokens, output_tokens`) لأن
  ContextVar ما بينتقل من مهمة فرعية (sub-task) لمهمة الأب تلقائياً — التوكنز هون
  بتنجمع وتنسجّل يدوياً بـ`_generate_super_pdf`/`_generate_super_images`.
- `get_last_generation_metadata()` هلق بترجع كمان `input_tokens`/`output_tokens`/
  `total_tokens` مع `provider`/`model`/`duration_seconds` القديمة.

### 2) `services/quiz_service.py`
- بعد `generate_quiz_smart(...)` مباشرة، صرنا نستدعي `get_last_generation_metadata()`
  فعلياً (كانت مستوردة بدون استخدام) ونستخرج منها `total_tokens` الحقيقي.
- `save_file_quiz_multiple(..., total_tokens=0, ...)` صارت `total_tokens=generation_total_tokens`
  (الرقم الحقيقي بدل الصفر الثابت).
- إضافة `asyncio.create_task(log_ai_generation(...))` — يسجّل صف بجدول
  `ai_generation_log` (كان فارغاً بالكامل) بكل توليد ناجح: provider, model, duration,
  عدد الأسئلة, input/output/total tokens. غير حرج (fire-and-forget) — لو فشل التسجيل،
  ما بيوقف تسليم الكويز للطالب.

### 3) `helpers/supabase_helper.py`
- دالة جديدة `log_ai_generation(...)` تكتب بجدول `ai_generation_log`.

### 4) `migration_ai_generation_log_tokens.sql` (ملف جديد)
- يضيف أعمدة `input_tokens`/`output_tokens`/`total_tokens` لجدول `ai_generation_log`
  الموجود أصلاً (كان بدون أي عمود توكنز).
- يضيف View مساعد `daily_ai_token_usage` (مجمّع يومي حسب provider+model) — للفحص
  اليدوي من SQL Editor أو كأساس لأمر إداري بالبوت لاحقاً.
- **لا يحسب تكلفة بالدولار عمداً** — الأسعار بتتغيّر (راجع نقاش السعر التمهيدي/القياسي
  لـ Gemini 3.8 Flash) وأي رقم مُثبّت بالـ View هيصير غلط بسرعة. احسب التكلفة بطبقة
  التطبيق (بقراءة السعر الحالي، مثلاً من `app_settings`).

## خطوات التطبيق

1. **شغّل الميغريشن أولاً** (يدوياً عبر Supabase SQL Editor، نفس أسلوب باقي الميغريشنز
   بالمشروع) — `migration_ai_generation_log_tokens.sql`. آمنة للتشغيل أكتر من مرة
   (`IF NOT EXISTS` بكل مكان).
2. استبدل 3 الملفات (`helpers/gemini_helper.py`, `services/quiz_service.py`,
   `helpers/supabase_helper.py`) بالنسخ المرفقة.
3. أعد تشغيل البوت.
4. تحقق: ولّد كويز تجريبي، وبعدها شغّل من SQL Editor:
   ```sql
   SELECT * FROM ai_generation_log ORDER BY created_at DESC LIMIT 5;
   ```
   المفروض تشوف صف جديد فيه provider/model_name وinput_tokens/output_tokens غير صفريين.

## معروف وغير مُغطّى بهالباتش (نطاق مقصود، مو نسيان)

- **مسار Groq النصي السريع** (`_generate_text_quiz` بـ`gemini_helper.py`): ما بيرجّع
  توكنز إطلاقاً حالياً (نطاق منفصل، Groq مو جزء من نقاش Vertex/تسعير Gemini). لو بدك
  نغطّيه لاحقاً، Groq API متوافق مع شكل OpenAI (`response.usage.prompt_tokens` /
  `completion_tokens`) — تعديل مشابه تماماً.
- **`services/audio_service.py`** (تفريغ/تلخيص المحاضرات الصوتية): التوكنز عندها
  بتتحسب فعلياً (`token_count` من `generate_text_with_cascade`) بس بتنكتب بـ log file
  بس (`log_info`/`log_warning`) — مو بقاعدة البيانات، فبتضيع مع أول log rotation.
  تحتاج تعديل مشابه لو بدك تتبّعها كمان (نفس نمط `log_ai_generation`).
- **أمر إداري بالبوت** (`/ai_usage` أو زر بلوحة الأدمن) لعرض الاستهلاك من داخل تيليجرام
  مباشرة: البنية التحتية (الجدول + الـ View) جاهزة الآن، بس الأمر نفسه لسا ما انكتب —
  خطوة منفصلة لو حابب نكملها بعدين.
