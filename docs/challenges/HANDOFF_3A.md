# HANDOFF 3A — التحقق من Backup وجاهزية قاعدة البيانات

التاريخ: 2026-10-05. الفرع: `feature/challenges`.

> **النتيجة: لم تؤخذ Backup، ولم تُطبّق أي migration سحابية، ولم تتغير أي قاعدة بيانات.**
> بعد منح الوصول أُجري فحص Read-Only فقط (القسم 2). أدوات Supabase المتاحة تدعم SQL وmigrations وBranches ولا تدعم تصدير/Backup قابلة للاسترجاع، وشبكة bash معطّلة ولا يوجد `pg_dump`. وفق شرط البرومبت: تعذّر Backup ⇒ لا تغيير على القاعدة.

## 1. حالة الكود عند البداية

- المصدر: `abiadquizbot_after_prompt2.zip` (sha256 `821154ab032f1e4b4ab8b687e3f404304e2e4c327bbc91f7a21afc154c76a654`).
- الفرع `feature/challenges`، الشجرة نظيفة، لا remote.
- HEAD = `46c0837` (feat: persistent async attacks...)، يليه `2ba3618` و`2b35e79` و`1bf7e78`. **متوافق مع HANDOFF_2.md.**
- ملفات migration موجودة: 0008 و0009 مع rollback لكل منهما.
- فحص ثابت: `py_compile` للملفات الثلاثة الرئيسية للعبة ناجح، و`git diff --check` ناجح.
- الـ flags في الملفات: 0008 يضيف `challenge_system_enabled=false`، و0009 يضيف `async_attacks_enabled=false` و`spectator_mode_enabled=false`. الافتراضات آمنة **في الملفات** فقط؛ لم تُتحقق في قاعدة حية.

## 2. حالة Supabase عند البداية (فحص Read-Only فعلي، 2026-10-04 ~21:52 UTC)

**`telegramquizbot`** (`pqawoanhbgvdtbdhnbod`، ACTIVE_HEALTHY، PG 17.6، eu-central-1، حجم 21 MB، 29 جدولًا عامًا، 49 migration مسجلة آخرها `20261004043728`):
- 0008 غير مطبق، 0009 غير مطبق.
- لا جداول `challenge_*`، لا RPCs `challenge*`.
- لا صفوف flags لـ challenge/async/spectator (الجدول `feature_flags` موجود). الافتراضي الفعلي «مغلق» لأن الصفوف غائبة، ويتطلب كود 0008/0009 إدخالها بـ false.
- لا Branches.

**`Test_telegramquizbot`** (`xhlksusmczccqyvbxylo`، ACTIVE_HEALTHY، 12 MB): **غير صالح كبيئة اختبار لـ0008/0009 كما هو.** مخططه قديم ومختلف عن الإنتاج: 12 جدولًا فقط، 7 migrations (آخرها `20260824221209`)، **لا يوجد `public.feature_flags`**، بينما 0008 و0009 يعتمدان عليه. التطبيق عليه سيفشل أو يتطلب ترقيته أولًا.

باقي المشاريع غير نشطة (INACTIVE) ولا علاقة لها بالمهمة.


## 2ب. إعادة بناء بيئة الاختبار (بطلب المالك، 2026-10-05)

`Test_telegramquizbot` (`xhlksusmczccqyvbxylo`) مُسح مخطط `public` فيه بالكامل (جداول، views، دوال) وسجل migrations، ثم أُعيد بناؤه من `0001`→`0007` في الريبو. **الإنتاج لم يُلمس (قراءة فقط).**

- التحقق: بصمات md5 لـ columns/constraints/indexes/views/RLS/policies/table grants/function grants **متطابقة مع الإنتاج**؛ 27 جدولًا + 2 views + 18 دالة. الدوال متطابقة بعد توحيد `\r\n` (الإنتاج يخزن CRLF، لا فرق منطقي).
- بيانات منسوخة (إعدادات مرجعية فقط، بقيم الإنتاج الفعلية): app_settings (5)، feature_flags (3: كلها false)، ai_model_slots (8)، ai_model_pricing (7)، payment_packages (3). **ai_request_timeout = 180 في الإنتاج** (الريبو 0005 يذكر 120).
- **لم تُنسخ** بيانات المستخدمين/الكويزات/الدفع/السجلات (587 مستخدمًا، 568 كويزًا، إلخ) لأسباب خصوصية؛ users = 0.
- ما زال: لا Backup للإنتاج، ولم يُطبق 0008/0009 بعد.

## 3. Backup

**لم تؤخذ أي نسخة.** لا وقت ولا مشروع ولا checksum ولا اختبار استرجاع. ملف ZIP الكود ليس Backup قاعدة بيانات، والفرع/Git ليسا Backup.

## 4. كيف تم التحقق

لا شيء من جهة القاعدة. المنفّذ فعليًا:

- `python3 -m unittest tests.test_challenge_game tests.test_challenge_content tests.test_challenge_repo`: **34 اختبارًا؛ 30 نجحت، 4 أخطاء**، كلها `ModuleNotFoundError: aiogram` (3 في HandlerTests وsetUpClass لـScreenTests). السبب بيئة بلا تبعيات ولا شبكة لتثبيتها، لا خلل مؤكد في الكود. **لا تُحسب هذه الأربعة ناجحة.**
- لم تُشغَّل: مجموعة SQL (`tests/sql/run_challenge_game.mjs`) لأن PGlite غير مثبت ولا شبكة؛ ولم يُشغَّل `unittest discover` الكامل ولا smoke import.

## 5. migrations المطبقة

لا شيء. لم يُطبّق 0008 ولا 0009 ولا rollback في أي بيئة.

## 6. أين طُبّقت

لا مكان.

## 7. نتائج الاختبارات الفعلية

انظر البند 4 فقط. تقرير HANDOFF_2 (243 Python و31 SQL) يبقى نتيجة جلسته السابقة ولم أعد إنتاجه هنا.

## 8. الاختلاف عن HANDOFF_2.md

- الكود: لا اختلاف.
- لا تغيير في الحقائق السابقة؛ الفرق الوحيد أن هذه الجلسة لم تستطع الوصول إلى Supabase أصلًا (HANDOFF_2 ذكر أنه كان متاحًا في جلسته).

## 9. نقطة البداية للبرومبت التالي

الحالة: كود جاهز على `46c0837` + هذا الملف، قاعدة غير مفحوصة وبلا Backup، الميزات غير مفعلة. المطلوب قبل أي شيء:

1. (تم الفحص — القسم 2.) يبقى اختيار بيئة اختبار: إما Supabase Branch من الإنتاج (مخطط فقط، بلا بيانات، له تكلفة وتحتاج موافقة) أو ترقية `Test_telegramquizbot` ليطابق الإنتاج.
2. Backup فعلية: `pg_dump --format=custom` أو Backups من لوحة Supabase، ثم `pg_restore --list` واسترجاع إلى قاعدة اختبار، مع تسجيل الوقت والمشروع وsha256. بلا أسرار في Git.
3. تطبيق 0008 ثم 0009 على **مشروع الاختبار فقط** بعد تأكد الـBackup، ثم فحص الجداول والقيود والـRPCs والفهارس وRLS وgrants وصلاحيات service_role، وبقاء الـ flags الثلاثة `false`.
4. تثبيت `requirements.txt` وتشغيل `unittest discover` وsmoke، وتشغيل SQL عبر PGlite، واختبار تزامن بين اتصالين حقيقيين.
5. لا Real-Time/Spectator/Friend Links/Bot قبل ذلك.
