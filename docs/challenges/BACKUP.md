# Backup — حالة وإجراء (قبل تطبيق 0008)

**الحالة: لم تُؤخذ نسخة احتياطية، ولم يُنفَّذ أي أمر على Supabase (لا قراءة ولا كتابة) من هذه الجلسة.**

السبب: قاعدة البرومبت 1 تمنع أي تعديل قاعدة بيانات دون Backup قابل للاسترجاع فعلياً. أدوات Supabase المتاحة بالجلسة (فروع/مشاريع/SQL) لا تتضمن أخذ نسخة احتياطية ولا pg_dump، و`create_branch` تنسخ الـ schema **دون بيانات الإنتاج** (نصّ الأداة) فليست نسخة احتياطية. لذلك كُتبت الـ migration كملف فقط.

## أخذ النسخة (يدوياً، قبل تطبيق 0008)
1. **Supabase Dashboard → Database → Backups**: نفّذ Backup/تحقق من وجود Daily أو PITR حديث (حسب الخطة).
2. **أو** `pg_dump` من جهازك بسلسلة الاتصال (Settings → Database → Connection string — لا تضعها بـ Git):
   `pg_dump "$DATABASE_URL" --no-owner --format=custom --file=abiad_pre_0008.dump`
3. **تحقق**: `pg_restore --list abiad_pre_0008.dump | head` ثم استرجعها إلى مشروع/قاعدة مؤقتة وقارن عدد صفوف `users` و`quizzes`.
4. سجّل (دون أسرار): تاريخ/وقت الأخذ، ما تشمله (كامل `public`)، مكان التخزين، وطريقة الاسترجاع (`pg_restore --clean --if-exists -d <target> abiad_pre_0008.dump`).

## تخفيف المخاطر (لا يلغي الشرط أعلاه)
0008 Additive بالكامل: لا تعدّل ولا تحذف أي كائن موجود (جداول `challenge_*` جديدة، دالة، view، وصف واحد في `feature_flags` بـ `ON CONFLICT DO NOTHING`). Rollback جاهز: `0008_challenge_content_rollback.sql`.
**جُرّبت فعلياً على Postgres 16 محلي مؤقت (لا Supabase)**: تطبيق، إعادة تطبيق (idempotent)، rollback، إعادة تطبيق، و12 سيناريو سلوكي (`tests/sql/challenge_content_test.sql`) — كلها نجحت.
