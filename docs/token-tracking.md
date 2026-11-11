# تتبّع التوكنز والتكلفة وسجل التوليد (`token-tracking.md`)

يوثّق هذا الملف كيفية التقاط وتسجيل استهلاك التوكنز وتكلفة الذكاء الاصطناعي عبر جميع المزوّدين وعرضها في لوحة تحكم الأدمن.

## 1. كيف يُلتقط الاستهلاك أثناء التنفيذ (`services/ai/`)

- **التقاط موحّد عبر جميع المزوّدين (`services/ai/adapters.py`):**
  - **Gemini & Vertex AI:** يقرأ المحوّل `prompt_token_count`، `candidates_token_count`، `thoughts_token_count`، و`total_token_count` من `usage_metadata`.
  - **الواجهات المتوافقة مع OpenAI (Groq، OpenAI، OpenRouter) وCloudflare Workers AI:** يقرأ المحوّل `prompt_tokens`، `completion_tokens`، و`total_tokens` من حقل `usage` في الاستجابة. (ملاحظة: في بروتوكول Chat Completions تدخل توكنز التفكير أصلاً ضمن `completion_tokens`، لذلك تُترك `thoughts_tokens=0` لتفادي احتساب تكلفتها مرتين).
- **العزل لكل مهمة (`services/ai/state.py`):**
  - تُحفظ بيانات آخر توليد ناجح (`provider`, `model`, `duration_seconds`, `input_tokens`, `output_tokens`, `thoughts_tokens`, `total_tokens`, `cascade_rank`, `cascade_total`) في متغيرات سياق معزولة (`ContextVars`) لكل مهمة `asyncio`.
- **تجميع الدفعات المتوازية والمتتابعة (`batch_generation.py` & `generation.py`):**
  - في مسارات التوليد بالدفعات (`batch_generation`) ومسارات `Super PDF` / `Super Images`، يتم جمع التوكنز والمدة عبر جميع الدفعات/الأجزاء وحفظ المجموع الكلي في سياق المهمة الأم.

## 2. أين تُسجَّل البيانات

1. **توليد الكويزات (`services/quiz_service.py`):**
   - يحفظ إجمالي التوكنز مع سجل الكويز في جدول `quizzes`.
   - يكتب صفاً تفصيلياً في جدول `ai_generation_log` عبر `log_ai_generation()` بمهمة خلفية (`spawn`) دون تعطيل تسليم الكويز للطالب.
   - يرفق بيانات التوكنز والمزوّد والموديل وترتيب الكاسكيد وقناة الإدخال (`telegram` أو `web_upload`) في حدث `quiz_generated` داخل جدول `usage_events`.
2. **تفريغ وتلخيص المحاضرات الصوتية (`handlers/audio.py` + `services/ai_audit.py`):**
   - تلتقط دالة `build_audio_audit_metadata(get_last_generation_metadata(), ...)` بيانات الموديل الفعلي، المزوّد، المدة، التوكنز، ومؤشر الاقتصاص (`partial_result`) وقناة الرفع، وتخزّنها في أحداث `audio_transcribed` و`audio_summarized` داخل `usage_events` لتظهر مباشرة في سجل توليد لوحة الأدمن.

## 3. الجداول وحساب التكلفة

- **`ai_generation_log`:** يخزّن سجلات توليد الكويزات (`provider`, `model_name`, `duration_seconds`, `questions_count`, `input_tokens`, `output_tokens`, `thoughts_tokens`, `total_tokens`).
- **`daily_ai_token_usage` (View):** تجميع يومي لاستهلاك التوكنز حسب المزوّد والنموذج (شاملاً `total_thoughts_tokens`).
- **`ai_model_pricing`:** يخزّن سعر المليون توكن للإدخال (`input_price_per_million`) والإخراج (`output_price_per_million`) مع علم `verified`.
  - تُحسب التكلفة التقديرية في طبقة التطبيق (`get_ai_model_pricing()` بكاش 60 ثانية، ودالة `_estimate_quiz_cost` في `handlers/admin/ai_control.py`).
  - تُحتسب توكنز التفكير (`thoughts_tokens`) بسعر توكنز الإخراج.

## 4. العرض في لوحة الأدمن

من **لوحة الأدمن (`/admin`) ← 🤖 التحكم بالذكاء الاصطناعي ← 📊 سجل توليد الكويزات**:
- يعرض السجل الموحّد عمليات توليد الكويزات بالإضافة إلى عمليات تفريغ وتلخيص الصوت.
- يوضّح لكل عملية: نوع العملية وقناة الرفع (`تيليجرام` / `رفع ويب`)، المزوّد والموديل، المدة بالثواني، تفصيل التوكنز (`📥 إدخال + 📤 إخراج + 🧠 تفكير = الإجمالي`)، التكلفة التقديرية بالدولار، وترتيب النموذج الفائز في الكاسكيد (`#1 من N`).
- يعرض رأس الشاشة إجمالي التكلفة التقديرية لآخر 7 أيام.

## 5. ملاحظات تشغيلية

- لتأكيد أسعار نموذج معيّن في جدول `ai_model_pricing` وإزالة علامة التحذير التقريبية `⚠️` من بجانب تكلفته، حدّث صفه في قاعدة البيانات بعد مطابقته مع فاتورة المزوّد الفعلية:
  ```sql
  UPDATE public.ai_model_pricing
  SET input_price_per_million = 0.15,
      output_price_per_million = 0.60,
      verified = true
  WHERE provider = 'gemini' AND model_name = 'gemini-2.5-flash';
  ```
- عند إضافة مزوّد أو نموذج جديد من لوحة الأدمن، أضف صف تسعيره في `ai_model_pricing` إذا أردت حساب تكلفته بالدولار في التقارير.
