# ميزة "تشغيل الكويز ضمن الغروبات" — ملخّص التسليم

هاد الأرشيف فيه كل الملفات الجديدة/المعدَّلة لتنفيذ الخطوات ١-٨ من
`group_quiz_feature_plan.md`. فكّه فوق جذر المشروع مباشرة (بنفس بنية
المجلدات: `services/`, `handlers/`, وملفات جذر المشروع).

## ⚠️ الـ migrations الثلاثة أصلاً مُطبَّقة على Supabase

`migration_group_quiz_show_names.sql`, `migration_group_quiz_anonymous_mode.sql`,
و`migration_group_quiz_due_index.sql` **مُطبَّقة فعلياً** على مشروع
`telegramquizbot` (`pqawoanhbgvdtbdhnbod`) مباشرة عبر MCP أثناء الجلسة -
موجودة هون بس للتوثيق والسجل (وكلها `IF NOT EXISTS` فآمنة تُشغَّل يدوياً
كمان لو حبيت، ما رح تعمل أي شي مزدوج).

## الملفات

| الملف | جديد / معدَّل |
|---|---|
| `services/group_permissions.py` | 🆕 جديد بالكامل (خطوة ١) |
| `services/group_quiz_store.py` | 🆕 جديد بالكامل (خطوات ٤-٥) |
| `handlers/group_quiz.py` | 🆕 جديد بالكامل (خطوات ٤-٨) |
| `services/quiz_engine.py` | ✏️ إضافي فقط - `open_period`/`poll_meta`/`is_anonymous` (خطوات ٢، ٨) |
| `handlers/sharing.py` | ✏️ زر "شغّل الكويز ضمن غروب" (خطوة ٣) |
| `handlers/__init__.py` | ✏️ تسجيل `group_quiz_router` |
| `keyboards.py` | ✏️ كيبوردات الإعداد الجماعي (خطوات ٤، ٧، ٨) |
| `main.py` | ✏️ تسجيل الراوتر + تشغيل النبضة الدورية بوضع polling |
| `webhook_server.py` | ✏️ تشغيل النبضة الدورية بوضع webhook |

## قبل الديبلوي

1. **تأكد إنه `handlers/quiz_runner.py` و`services/quiz_engine.py` الأصليين
   عندك متطابقين مع نسخة المشروع يلي شتغلنا عليها** - التعديلات على
   `quiz_engine.py` هون إضافية بالكامل (بارامترات اختيارية بقيم افتراضية)،
   بس لو عندك تعديلات محلية عليه من بعد ما اخدت نسخة عنه، لازم تدمجها يدوياً
   بدل الاستبدال المباشر.
2. **`allowed_updates` بـ `webhook_server.py`** أصلاً فيها `poll` و
   `poll_answer` من قبل - ما احتجنا نضيف شي هون.
3. القرار المفتوح بخصوص خصوصية "من صوّت لمين" على الاستفتاء نفسه (مش شغلنا -
   قيد من تيليجرام) موثّق بتعليقات `keyboards.py`/`handlers/group_quiz.py`
   لو حدا احتاج يرجعلها لاحقاً.

## لسا ما تنفّذ (اختياري، ذُكر بالمحادثة بس مش جزء من الخطة الأصلية)

- تفعيل RLS مع policies على الـ ١٨ جدول التانية بالمشروع (تحذير أمني من
  Supabase advisor، غير متعلّق بميزة الكويز الجماعي - راجع المحادثة).
