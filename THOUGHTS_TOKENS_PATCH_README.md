# باتش: إضافة thoughts_tokens

## قاعدة البيانات
✅ تم تطبيقها مباشرة على Supabase (مشروع pqawoanhbgvdtbdhnbod) — ما في شي مطلوب منك هون.
- عمود `thoughts_tokens` (integer, default 0) مضاف لجدول `ai_generation_log`.
- الـ View `daily_ai_token_usage` مُحدَّث ليشمل `total_thoughts_tokens`.
- `migration_ai_generation_log_thoughts_tokens.sql` مرفق فقط للتوثيق/لو حبيت تطبقه على بيئة تانية.

## الملفات المعدّلة (3) — بدك تستبدلها على السيرفر
1. `helpers/gemini_helper.py`
   - `_record_token_usage()` هلق بيقرا `thoughts_token_count` من `usage_metadata` كمان.
   - مسار Super PDF/Super Images (`_generate_single_attempt` + التجميع بـ`_generate_super_pdf`/`_generate_super_images`) بيسحب ويجمع `thoughts_tokens` بنفس طريقة input/output.
2. `helpers/supabase_helper.py`
   - `log_ai_generation()` صار عندها parameter جديد `thoughts_tokens` وبتحفظه بالجدول.
3. `services/quiz_service.py`
   - بيمرّر `thoughts_tokens` من `generation_metadata` لـ `log_ai_generation()`.

## اختبار بعد الديبلوي
ولّد كويز تجريبي (يفضّل عبر موديل تفكير زي gemini-3.6-flash)، وبعدين:
```sql
SELECT input_tokens, output_tokens, thoughts_tokens, total_tokens
FROM ai_generation_log
ORDER BY created_at DESC LIMIT 1;
```
المفروض: `input_tokens + output_tokens + thoughts_tokens ≈ total_tokens` (تقريباً، مش بالضبط دايماً حسب رسوم Google الداخلية).
