# تقرير التسليم 1 — بنك محتوى التحديات

> مرجع كافٍ لمن لم يرَ الجلسة. **الحالة بإيجاز: الكود والـ migration جاهزان ومُختبَران محلياً، لكن الـ migration غير مطبَّقة على Supabase (لا Backup قابل للأخذ بأدوات الجلسة) ولم يُجرَّب البوت على Telegram.**

## 1) ما نُفّذ
- Audit للمشروع (انظر `docs/challenges/DESIGN.md` §1) · وثيقة تصميم الميزة كاملة (محتوى + جداول اللعبة المؤجلة).
- `0008_challenge_content.sql` (+ rollback): جداول فرع/مادة/وحدة/اختبار/أسئلة، دالة استيراد ذري، view "قابل للعب"، RLS، فلاغ مطفأ.
- طبقة Repository + منطق نقي للتحقق + لوحة أدمن Telegram (رفع JSON، استيراد كويز موجود، إدارة الهرم والاختبارات، معاينة، Pagination).
- اختبارات: 25 Python + 12 سيناريو SQL.
- **لم يُنفَّذ**: Backup (لا أداة)، تطبيق الـ migration، أي تجربة على Telegram/Supabase الحقيقيين.

## 2) Branch والـ commit
- Branch محلي `feature/challenges` (من commit `baseline: project as received`)، commit: `feat(challenges): …`. **لا remote ولا Push** — المشروع وصل كملف zip بلا `.git`، فأنشأتُ repo محلياً داخل الـ zip المسلَّم (يحمل التاريخ). ادفعه لمستودعك يدوياً إن رغبت.

## 3) الملفات
جديد: `db/migrations/0008_challenge_content.sql` + `_rollback.sql` · `services/challenge_content.py` · `helpers/supabase_helper/challenges.py` · `handlers/admin/challenges.py` · `docs/challenges/{DESIGN,BACKUP,HANDOFF_1}.md` · `tests/test_challenge_content.py`, `tests/test_challenge_repo.py`, `tests/sql/challenge_content_test.sql`.
معدَّل (إضافات صغيرة فقط): `handlers/admin/__init__.py` (تسجيل الراوتر) · `keyboards.py` (زر «🏆 إدارة التحديات») · `constants.py` (مفتاح بالـ registry) · `helpers/supabase_helper/__init__.py` (استيراد الوحدة) · `db/README.md`.
وثيقة التصميم: **`docs/challenges/DESIGN.md`**.

## 4) الجداول/الـ migrations
`0008`: `challenge_branches`, `challenge_subjects`, `challenge_units`, `challenge_tests`, `challenge_questions`, الدالة `challenge_import_test(...)` (service_role فقط)، الـ view `challenge_playable_units`، وصف `feature_flags('challenge_system_enabled', false)`. أُعيد استخدام: `feature_flags`، صيغة `quizzes.quiz_data`. لم يُمسّ أي جدول/عمود موجود.

## 5) ملخص Schema والقرارات
انظر DESIGN §2 و§6. أهمها: Soft delete فقط بلا cascade؛ الأسماء فريدة بين الفعّالين؛ الأسئلة **غير قابلة للتعديل** بعد الاستيراد (تعديل = اختبار جديد) فتكفي المراجع `test_id/question_id` للمباريات القديمة؛ حالات الاختبار `draft→active⇄disabled→archived` (`draft` محجوز للمستقبل، الاستيراد يعتمد مباشرة `active`). جداول اللعبة المؤجلة (`challenge_profiles/lands/towers/matches/match_players/match_answers`) مصمَّمة بـ DESIGN §6 للبرومبت 2 (`0009`).

## 6) لوحة الإدارة والصلاحيات
`/admin` → «🏆 إدارة التحديات» (`admin_challenges`) → فرع → مادة → وحدة → اختبارات. كل callbacks بالبادئة `adm_ch:` (غير مستخدمة سابقاً). الحماية: الراوتر كله خلف `IsAdminFilter` (Server-Side، يقارن `from_user.id` بـ `ADMIN_ID` لكل رسالة وضغطة وملف داخل حالات FSM) + `chat.type == private`. اللوحة تعمل حتى والفلاغ مطفأ (لتجهيز المحتوى).

## 7) صيغة JSON
```json
{"title":"اسم الاختبار","questions":[{"question":"…","options":["أ","ب","ج"],"correct_option_id":1,"explanation":"اختياري"}]}
```
أو قائمة أسئلة مباشرة. `correct_option_id` يبدأ من 0. حدود: 512KB، 200 سؤال، 2–10 خيارات، لا خيارات فارغة/مكررة.

## 8) كويز أبيض → Challenge Test
الأدمن يرسل **UUID الكويز أو share code** → معاينة ملخص → اعتماد. يُنسخ محتوى الأسئلة (+ `image_url` وعلامات `is_math_quiz/subject_type/difficulty` بـ `metadata`) كنسخة مستقلة بجداول `challenge_*`. المصدر = `source_reference` نصي **بلا FK**، فحذف الكويز الأصلي لا يؤثر (اختبار SQL T10 + Python). لا تُنقل `creator_id/likes/share_code`. (لم أبنِ زر «إضافة إلى تحديات أبيض» داخل تفاصيل الكويز ولا قائمة «آخر الكويزات» — الإدخال بالمعرّف أبسط ولا يمسّ شاشات الكويز الحالية.)

## 9) واجهات للمراحل التالية (`helpers/supabase_helper/challenges.py`)
`get_playable_branches/subjects/units`, `pick_active_test(unit_id, exclude_test_ids)`, `get_test_questions_public` (بلا إجابات)، `get_test_questions_admin`, `is_feature_enabled('challenge_system_enabled', default=False)`; ومنطق نقي: `services/challenge_content.py` (`strip_answers`, `parse_challenge_json`, …).

## 10) Backup
**لم تُؤخذ نسخة.** أدوات الجلسة لا تدعم ذلك (و`create_branch` بلا بيانات إنتاج). الإجراء اليدوي والتحقق: `docs/challenges/BACKUP.md`. **لا تُطبَّق 0008 قبل ذلك.**

## 11) Rollback
الكود: `git revert`/حذف الفرع (التغييرات في ملفات موجودة 5 أسطر فقط). القاعدة: `0008_challenge_content_rollback.sql` (يحذف الجديد فقط — يحذف اختبارات التحديات المضافة).

## 12) الاختبارات المشغّلة
- Python: `python -m unittest discover -s tests` → 146 اختباراً (25 جديدة ✅). **3 أخطاء قائمة مسبقاً بالـ baseline** (`test_payment_service` import، واثنان من `test_pdf_*`) — أكّدتُ تطابقها على نسخة الـ zip الأصلية.
- SQL على Postgres 16 مؤقت محلي (ليس Supabase): تطبيق/إعادة/rollback + 12 سيناريو: أكثر من اختبار بوحدة ✅ · استيراد ذري وفشل بمنتصفه بلا بيانات جزئية ✅ · نفس المصدر مرتين ✅ · سؤال بلا/بإجابة خارج النطاق ✅ · تعطيل/وحدة فارغة/أرشفة وحدة ومادة لا تظهر قابلة للعب والمراجع تبقى ✅ · إعادة إضافة بعد أرشفة ✅ · RLS/صلاحيات anon ✅.
- JSON صحيح/تالف/ناقص، Snapshot مستقل، عدم تسريب بيانات المالك، منع كشف الإجابات، pick بلا تكرار: Python ✅.
- **غير مغطّى بالآلة**: handlers تيليجرام (compile فقط)، حماية الأدمن (تعتمد على `IsAdminFilter` الموجود)، تشغيل SQL داخل CI (يدوي بـ psql).

## 13) غير منفَّذ / مخاطر / ديون
- لا Backup/تطبيق migration/تجربة حقيقية. الـ migration مُختبرة على Postgres عادي؛ **جرّبها أولاً على فرع/مشروع Supabase اختباري**.
- قبل تطبيق 0008 يعرض مفتاح `challenge_system_enabled` بلوحة المفاتيح كـ«مفعّل» (افتراض غياب الصف)؛ بعدها مطفأ. الكود نفسه يقرأه `default=False`.
- لا إلغاء أرشفة، ولا تعديل أسئلة، ولا نقل اختبار بين وحدات، ولا قائمة «الأرشيف» بالواجهة (الأرشفة نهائية من البوت؛ تُعكس يدوياً بـ SQL).
- `pick_active_test` يجلب أعمدة خفيفة لاختبارات الوحدة الفعّالة (عددها صغير)؛ إن كبرت الوحدات جداً غيّره لـ offset عشوائي بعد `count`.
- الأدمن يصل لكل كويزات المستخدمين بالمعرّف (نفس سياسة الأدمن الحالية، service_role).
- ثغرات صلاحيات قديمة بالـ baseline (anon على `quizzes` بلا RLS) لم تُمسّ — جداول التحديات الجديدة محمية.

## 14) اختبار يدوي على Telegram (بعد تطبيق الـ migration على بيئة اختبار)
1. `/admin` → «🏆 إدارة التحديات» → ➕ فرع «علمي» → ➕ مادة «فيزياء» → ➕ وحدة «الحركة».
2. افتح الوحدة → 📤 رفع JSON → أرسل ملفاً صحيحاً → راجع الملخص → ✅ اعتماد → يظهر الاختبار (✅، عدد الأسئلة، المصدر JSON).
3. أرسل نفس الملف ثانية واعتمد → «موجود مسبقاً» ولا تكرار. اضغط ✅ مرتين بسرعة → مرة واحدة فقط.
4. أرسل ملفاً تالفاً (JSON مقطوع) وآخر بسؤال بلا `correct_option_id` → رسالة خطأ واضحة ولا شيء يُحفظ.
5. 🧩 من كويز أبيض → أرسل UUID/share code لكويز موجود → ملخص → اعتماد؛ ثم احذف الكويز الأصلي من البوت وتأكد أن الاختبار ما زال يُفتح ويُعاين.
6. افتح اختباراً → 👁 معاينة (✅ على الصحيحة) → ⛔ تعطيل ← الوحدة تعرض «لن تظهر للطلاب» → ✅ إعادة تفعيل → 🗄 أرشفة (بتأكيد).
7. أضف اختبارين بنفس الوحدة وتأكد من الظهور والـ Pagination (أكثر من 8).
8. بحساب غير الأدمن: أرسل `/admin` ثم حاول الضغط على زر `adm_ch:` قديم (من لقطة قديمة) → لا استجابة.
9. من «مفاتيح التحكم» تأكد أن «🏆 نظام التحديات» مطفأ بعد تطبيق الـ migration.
