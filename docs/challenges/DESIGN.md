# تصميم نظام التحديات (الميزة كاملة) — v1

> يغطي المحتوى (البرومبت 1، **منفَّذ**) واللعبة (البرومبتان 2 و3، **تصميم فقط**). أبسط تصميم يحقق المطلوب.

## 1) ما وُجد بالمشروع وأُعيد استخدامه
| الموجود | الاستخدام |
|---|---|
| `handlers/admin/*` + `IsAdminFilter` (مقارنة `from_user.id` بـ `ADMIN_ID` لكل رسالة/ضغطة) | راوتر الأدمن الجديد خلف نفس الفلتر، ومسجَّل في `handlers/admin/__init__.py` |
| `feature_flags` + `is_feature_enabled/set_feature_flag` + `FEATURE_FLAGS_REGISTRY` | `challenge_system_enabled` (مطفأ افتراضياً بالقاعدة؛ الكود يقرأه بـ `default=False`) |
| `quizzes.quiz_data` (jsonb: `question, options, correct_option_id (0-based), explanation, image_url?`) | هي صيغة الأسئلة المعتمدة في التحديات — لا Schema جديد، فقط Adapter (`from_existing_quiz`) |
| نمط migrations المرقّمة + RLS مفعّل بلا policies + `REVOKE` من anon/authenticated (جداول الدفع) | نفسه تماماً |
| FSM على Redis، `safe_edit_text` | FSM نفسه؛ `_render` مع fallback لرسالة جديدة |
لا وُجد نظام فروع/مواد/وحدات سابق (`subject_type` بـ `quizzes` تصنيف AI حر، لا علاقة له).

## 2) جداول المحتوى (مطبَّقة كملف 0008 — غير مطبَّقة على أي قاعدة)
```
challenge_branches(id, name, sort_order, status active|archived, created_at, updated_at, archived_at)
challenge_subjects(id, branch_id→branches, name, sort_order, status, …)
challenge_units   (id, subject_id→subjects, name, sort_order, status, …)
challenge_tests   (id, unit_id→units, title, status draft|active|disabled|archived,
                   source_type json_upload|existing_abiad_quiz|manual_future, source_reference (بلا FK),
                   content_hash, question_count, metadata jsonb, created_by_admin, created_at, updated_at,
                   disabled_at, archived_at)
challenge_questions(id, test_id→tests, question_order, question_text, options jsonb(2..10),
                    correct_option_id (< len(options)), explanation, metadata jsonb)
```
قرارات:
- **الأسماء فريدة بين الفعّالين فقط** (`lower(btrim(name))` + parent) → الأرشفة تحرّر الاسم.
- **لا حذف فيزيائي**: كل الحذف = `archived`. لا cascade: ما تحت العنصر المؤرشف يختفي من اللعب لأن "قابل للعب" يشترط أسلافاً فعّالين، والبيانات تبقى كما هي.
- **الأسئلة غير قابلة للتعديل بعد الاستيراد** (لا واجهة تعديل). التعديل = اختبار جديد + أرشفة القديم. لذلك المباريات القديمة (البرومبت 2/3) تكفيها المراجع `test_id` + `question_id` دون نسخ إضافي، وتبقى مفهومة للأبد.
- **`status='draft'`** محجوز لـ `manual_future`؛ الاستيراد الحالي يعتمد مباشرة كـ `active` (الاعتماد يتم قبل الحفظ بشاشة الملخص).
- **الاختبار قابل للعب** ⇔ `status='active'` ∧ `question_count>0` ∧ الوحدة والمادة والفرع `active` (view `challenge_playable_units`).
- **`question_count` من المحتوى الفعلي** (لا افتراض 10).
- لا `needs_review` ولا مراجعة مجتمعية.

### آلة حالات الاختبار
`active ⇄ disabled`، و`active|disabled|draft → archived` (نهائي). التنفيذ بـ `UPDATE … WHERE status=<السابقة>` فلا تتعارض ضغطتان متزامنتان.

## 3) Flows
- **إدارة المادة/الوحدة**: الأدمن → 🏆 إدارة التحديات → فرع → مادة → وحدة؛ إضافة / اسم / ترتيب (رقم) / أرشفة (بتأكيد). القوائم بـ Pagination (8).
- **رفع JSON**: وحدة → 📤 رفع JSON → ملف → تحقق كامل (حتى 5 أخطاء مفصّلة) → ملخص (الاسم/المسار/العدد/المصدر) → ✅ اعتماد | ❌ إلغاء | ✏️ تغيير الاسم → RPC ذري.
- **استيراد كويز موجود**: وحدة → 🧩 → معرّف UUID أو share code → نفس الملخص والتأكيد. يُنسخ المحتوى فقط (Snapshot مستقل)؛ `source_reference` = معرّف الكويز للتتبع (بلا FK) فحذف الأصل لا يؤثر. لا تُنقل بيانات المالك (لا `creator_id/likes/share_code`).
- **Idempotency**: `content_hash` = SHA-256 لـ (السؤال+الخيارات+الإجابة) مرتّبة — لا العنوان ولا الشرح. فهرس فريد جزئي `(unit_id, content_hash) WHERE status<>'archived'` + فحص داخل الدالة + معالجة `unique_violation` للسباق. وبالواجهة: تفريغ حالة FSM قبل الاستيراد فالضغطة الثانية تُرفض.
- **تعطيل/أرشفة**: من شاشة الاختبار. المؤرشف يسمح بإعادة إضافة نفس المحتوى.

## 4) JSON المدعوم
```json
{"title": "الحركة — اختبار 1",
 "questions": [{"question": "…", "options": ["أ","ب","ج"], "correct_option_id": 1, "explanation": "اختياري"}]}
```
أو قائمة أسئلة مباشرة (نفس `quiz_data`؛ العنوان من اسم الملف). حدود: 512KB، 200 سؤال، 2–10 خيارات (حد Telegram Poll)، بلا خيارات فارغة/مكررة، `correct_option_id` عدد صحيح 0-based ضمن النطاق. `image_url` (https فقط) يُحفظ بـ `metadata`.

## 5) واجهات جاهزة للمراحل التالية (`helpers/supabase_helper/challenges.py`)
`get_playable_branches()`, `get_playable_subjects(branch_id)`, `get_playable_units(subject_id)`,
`pick_active_test(unit_id, exclude_test_ids=())` (بلا `ORDER BY RANDOM`: أعمدة خفيفة لاختبارات الوحدة الفعّالة ثم اختيار بالذاكرة، مع تفضيل غير المكرَّر وfallback لعدم الفراغ)،
`get_test_questions_public(test_id)` (**لا يجلب** `correct_option_id` ولا `explanation` من القاعدة أصلاً)،
`get_test_questions_admin(test_id)` (للمعاينة فقط)، و`strip_answers()` بـ `services/challenge_content.py`.
المطلوب بالمواجهة: عند الاستخدام تحقق من `is_feature_enabled('challenge_system_enabled', default=False)`، وإن لم توجد وحدات: «📚 لا توجد اختبارات متاحة لهذه الوحدة حاليًا.»

## 6) تصميم جداول اللعبة (مؤجَّل للبرومبت 2 — لا يُنفَّذ الآن)
قاعدة: منفصلة تماماً عن `users.points` (النقاط المدفوعة). Lazy creation. تُنشأ بـ migration `0009`.
```
challenge_profiles(user_id PK bigint, xp int, trophies int, level int, created_at)         -- lazy
challenge_lands   (user_id PK, created_at)                                                  -- أرض اللاعب
challenge_towers  (id, user_id, subject_id→challenge_subjects, level int, xp int, unique(user_id,subject_id))
                  -- برج لكل مادة؛ قوته من تقدمه الدراسي (XP) لا من نقاط مدفوعة
challenge_matches (id, kind async_attack|live|friend_link, status created|active|finished|cancelled|expired,
                   unit_id, test_id, created_at, started_at, finished_at, server_deadline, result jsonb)
                  -- test_id + ids الأسئلة كافيان للتاريخ لأن الأسئلة غير قابلة للتعديل
challenge_match_players(match_id, user_id, role attacker|defender|player, score, trophies_delta, xp_delta, unique(match_id,user_id))
challenge_match_answers(match_id, user_id, question_id, option_id, answered_at_server, is_correct, unique(match_id,user_id,question_id))
                  -- UNIQUE يمنع الإجابة المزدوجة؛ الزمن والتصحيح Server-Side فقط
```
Redis للحالة اللحظية (مؤقتات/Spectators) مع TTL؛ النتيجة النهائية دائماً بـ Postgres. Ranks (Bronze…) تُشتق من `trophies` بدالة، بلا جداول Leagues/Seasons الآن.

## 7) تنفيذ المرحلة الثانية (0009)

تم تنفيذ الأرض والأبراج والهجوم غير المتزامن محليًا. المرجع التفصيلي الحالي هو `HANDOFF_2.md` و`services/challenge_rules.json`.
تغييرات واعية عن التصميم الأولي في §6:
- الأرض = صف `challenge_profiles` بدل جدول lands زائد.
- المستويات مشتقة من XP وسقف دقة الأداء، وليست أعمدة قابلة للتناقض.
- حالات الجلسة `created/in_progress/completed/expired/abandoned`.
- Snapshot حقيقي للأسئلة + قواعد الحساب + أسماء العرض، إضافةً إلى IDs، لثبات تاريخ الهجمات.
- جداول إضافية للإحصاءات/الأحداث/outbox؛ لا بث Realtime في هذه المرحلة.
- لا جدول paid points ولا علاقة بـ users.points؛ مفتاح هوية Telegram داخل جداول اللعبة فقط.
- 0008 و0009 لا تزالان غير مطبقتين على Supabase في جلسة المرحلة الثانية.
