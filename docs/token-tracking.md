# تتبّع التوكنز والتكلفة

## ما يحدث عند كل توليد ناجح

- `helpers/gemini_helper/` يلتقط `input/output/thoughts/total` من `usage_metadata` (نفس الشكل في Gemini AI Studio وVertex) ويحفظها في ContextVar.
- مسارا Super PDF وSuper Images (متوازيان عبر `asyncio.gather`) يجمعان التوكنز يدوياً لأن ContextVar لا ينتقل من المهام الفرعية إلى الأب.
- `get_last_generation_metadata()` تعيد المزوّد والموديل والمدة والتوكنز و`cascade_rank` / `cascade_total` (ترتيب الصف الفائز بالكاسكيد لحظة التوليد، ويبقى ثابتاً في السجل حتى لو تغيّر الترتيب لاحقاً).
- `services/quiz_service.py` يحفظ التوكنز الحقيقية مع الكويز، ويكتب صفاً في `ai_generation_log` عبر `log_ai_generation()` (`helpers/supabase_helper/quiz_cache.py`) بمهمة خلفية: فشله لا يوقف تسليم الكويز.
- حدث `quiz_generated` في `usage_events` يرفق التوكنز و`cascade_rank` و`generation_mode`.

## الجداول

- `ai_generation_log`: provider, model, المدة، عدد الأسئلة، `input_tokens`, `output_tokens`, `thoughts_tokens`, `total_tokens`.
- `daily_ai_token_usage` (View): تجميع يومي حسب provider+model، يشمل `total_thoughts_tokens`. لا يحسب تكلفة بالدولار عمداً.
- `ai_model_pricing`: سعر المليون توكن للإدخال والإخراج + `verified`. التكلفة تُحسب في طبقة التطبيق (`get_ai_model_pricing()` بكاش 60 ثانية، و`_estimate_quiz_cost` في `handlers/admin/ai_control.py`). توكنز التفكير تُحسب بسعر الإخراج.

## الشاشة

🤖 التحكم بالذكاء الاصطناعي ← 📊 سجل توليد الكويزات. لكل كويز: تفصيل التوكنز (📥 إدخال + 📤 إخراج + 🧠 تفكير = الإجمالي)، التكلفة التقديرية، والترتيب بالكاسكيد (`#2 من 6`). مسارات Super PDF/Images وGroq السريع خارج الترقيم وتظهر بوصف بدلاً من رقم. هيدر الشاشة يعرض التكلفة الإجمالية لآخر 7 أيام. الكويزات القديمة قبل التتبع تظهر بلا هذه الأسطر.

## بنود مفتوحة

1. **أسعار `ai_model_pricing` كلها `verified=false`** (تقريبية). طابقها مع فواتير فعلية ثم:
   ```sql
   UPDATE ai_model_pricing
   SET input_price_per_million = 0.00, output_price_per_million = 0.00, verified = true
   WHERE provider = '...' AND model_name = '...';
   ```
   إلى ذلك الحين تظهر ⚠️ بجانب كل تكلفة محسوبة منها.
2. مسار Groq النصي السريع لا يعيد توكنز (واجهة Groq متوافقة مع OpenAI: `usage.prompt_tokens` / `completion_tokens`).
3. `services/audio_service.py` تحسب التوكنز لكنها تكتبها في ملفات اللوغ فقط، لا في القاعدة.
4. لا يوجد أمر `/ai_usage` لعرض الاستهلاك من تيليجرام (الجدول والـ View جاهزان).
