# db/ — هيكل قاعدة البيانات

| الملف | المحتوى |
|---|---|
| `migrations/0001_baseline.sql` | هيكل الإنتاج كاملاً: 23 جدولاً، 2 views، 17 دالة، الفهارس، المفاتيح الأجنبية، RLS، والفروقات في الصلاحيات |
| `migrations/0006_payments.sql` | نظام الشحن الذاتي: `payment_packages/orders/events/receipts` (RLS مفعّل بلا policies) + دالة `payment_credit_order` (service_role فقط) + فلاغ `payments_enabled`. **مطبَّق على الإنتاج**، راجع `docs/payments.md` |
| `migrations/0007_payment_credit_hardening.sql` | تقوية `payment_credit_order`: باراميتر `p_require_match` للمسار التلقائي (حالة الطلب + مطابقة مبلغ/عملة الإيصال) ورفض إيصالات `dismissed/baseline`. يحذف overload القديم. **طبّقه قبل تفعيل «شحن تلقائي»**، ولا تُعد تشغيل 0006 بعده (يعيد الدالة القديمة) |
| `migrations/0008_challenge_content.sql` | جداول بنك التحديات (`challenge_branches/subjects/units/tests/questions`) + دالة استيراد ذري + view + فلاغ `challenge_system_enabled` (مطفأ). **غير مطبَّق** — راجع `docs/challenges/BACKUP.md` قبل التطبيق. Rollback: `0008_challenge_content_rollback.sql` |
| `migrations/0002_seed_config.sql` | بيانات الإعدادات المرجعية (`app_settings`, `feature_flags`, `ai_model_slots`, `ai_model_pricing`) بلا أي بيانات مستخدمين |

مولَّدان بتاريخ 2026-10-02 بقراءة مباشرة (read-only) من مشروع الإنتاج `telegramquizbot`. لم يُغيَّر شي في القاعدة.

## حالة التحقق
- **لم يُشغَّلا بعد على قاعدة فارغة.** جرّبهما على مشروع/فرع Supabase اختبار قبل أي اعتماد.
- بعد التجربة قارن النتيجة بالإنتاج (أعمدة، قيود، فهارس، دوال) للتأكد من التطابق.

## بناء قاعدة جديدة
1. أنشئ مشروع Supabase جديداً (Postgres 15+).
2. شغّل `0001_baseline.sql` ثم `0002_seed_config.sql` بالترتيب (SQL Editor أو `psql`).
3. اضبط `SUPABASE_URL` و`SUPABASE_KEY` (service_role) في بيئة البوت.
4. البيانات (المستخدمون والكويزات) لا تنتقل بهذه الملفات؛ تحتاج `pg_dump --data-only` منفصلاً.

## الملفات القديمة (`db/archive/`)
ملفات `migration_*.sql` نُقلت إلى `db/archive/`، وهي تاريخية (كل ما فيها مطبَّق فعلاً بالإنتاج وانعكس في الـ baseline). لا تُشغَّل على قاعدة جديدة. تُحفظ للمرجع فقط.
أي تعديل جديد على القاعدة يُكتب كملف مرقّم تالٍ: `0003_...sql`.

## ما يعكسه الـ baseline عمداً (ثغرات معروفة، لم تُعالَج)
- `quizzes` و`quiz_scores` و`usage_events` وغيرها (انظر الملف) عليها صلاحيات كاملة لـ `anon`/`authenticated` بلا RLS.
  الجداول `users` و`logs` و`favorite_quizzes` و`files_cache` سُحبت صلاحياتها منهما.
- الدوال المالية السبع (`admin_add_points_atomic` وأخواتها) و`vote_on_quiz` و`vote_on_classification` قابلة للتنفيذ من `anon`.
  `admin_*` الإحصائية و`ai_model_*` مقيّدة بـ `service_role`.
- قرار تأجيل الإصلاح بيد المالك؛ الـ baseline يطابق الإنتاج كما هو.

### 0009 — الأرض والأبراج والهجمات غير المتزامنة

ملف `migrations/0009_challenge_game.sql` يأتي بعد 0008 ويضيف اللعبة وRPCs الذرية والإحصاءات والأحداث والإشعارات.
**غير مطبّق على Supabase في جلسة التطوير.** خذ Backup وتحقق من الاسترجاع ثم جرّب على بيئة اختبار أولًا.
تفاصيل القواعد والتطبيق والرجوع: `docs/challenges/HANDOFF_2.md`, `BACKUP_2.md`, `TESTING_2.md`.
لا تشغّل ملفات `_rollback.sql` مع migrations التصاعدية؛ 0009 rollback يحذف بيانات اللعبة الجديدة فقط ويبقي بنك المحتوى.
