# TASK 3C.5 — إعادة بناء `challenge_*` على Supabase الاختبار من الـrepo والتحقق منها

**لم يُنفَّذ أي جزء من هذه المهمة.** كتبها Claude السابق بعد قراءة فقط (لا كتابة) على المشروع. المالك وافق صراحة على الخطة أدناه، وطلب أن ينفّذها Claude آخر.
الفرع: `feature/challenges`. HEAD الكود: انظر `git log -1`. اقرأ أولًا: `HANDOFF_3B5.md` و`HANDOFF_3C.md` (خاصة §18 و§19).

## 0. القواعد غير القابلة للكسر
1. **المشروع الوحيد المسموح كتابته:** `Test_telegramquizbot` — `project_id = xhlksusmczccqyvbxylo` (eu-west-1). تأكد من `list_projects` أن الاسم والمعرّف متطابقان قبل **كل** كتابة.
2. **ممنوع لمس** `telegramquizbot` (`pqawoanhbgvdtbdhnbod`، الإنتاج) بأي استعلام، قراءة أو كتابة. ولا أي مشروع آخر في الحساب (akarat, Tasbeeh, mannequin-bot…).
3. الأعلام `challenge_system_enabled` و`live_matches_enabled` تبقى **false** في النهاية. يجوز تشغيلها مؤقتًا على مشروع الاختبار فقط أثناء الاختبار ثم إعادتها false وتوثيق ذلك.
4. لا تعدّل migrations الـrepo ولا كود المنتج. إن ظهر Bug فعلي في 0010 على المخطط الحقيقي: وثّقه بدقة (الخطأ، السطر، الدليل) وأصلحه بأصغر تعديل فقط مع إعادة اختبارات SQL، وسجّله كاستثناء صريح.
5. لا Merge إلى master، ولا Push إلا لـ`feature/challenges` إن وُجد remote قابل للكتابة (الحالي ملف bundle للقراءة فقط).
6. لا تدّعِ نتيجة لم تُنفَّذ فعليًا. أي خطوة تعذّرت (شبكة، بيانات اعتماد) اكتبها «لم تُنفَّذ» مع السبب.

## 1. ما وُجد (قراءة فقط، 2026-10-06)
- `Test_telegramquizbot` فيها مخطط 0001–0007 الحقيقي (users, feature_flags, payments…). الأعلام الاثنان false.
- سجل الـmigrations: `challenge_content_0008`, `challenge_game_0009`, `challenge_live_0010` (طُبّقت 2026-10-04) — **ليست نسخة الـrepo**:
  - موجود: جدولا `challenge_queue` و`challenge_active_slots`، ودوال `challenge_live_poll/_advance/_sweep/_create/_current/_resolve`، و`challenge_live_cancel(p_user)` و`challenge_live_finish(p_match,p_status)` بتوقيعات مختلفة.
  - غير موجود: `challenge_live_queue`، العمودان `challenge_matches.countdown_until` و`challenge_match_players.message_id`، ودوال `challenge_live_tick/answer/leave/due/expire_queue/attach_message/progress/apply/range` — وهي ما تعتمد عليه 3B.5 و3C.
  - أطوال DDL المسجّلة: 0008=7530، 0009=24070، 0010=41185 حرفًا (الـrepo: 9309 / 25559 / 42091 بايت).
- **كل** جداول `challenge_*` فارغة، و`users` فارغ (0 صفوف).
- لا يُطبَّق 0010 الـrepo فوقها: دوال بنفس الاسم وتوقيعات مختلفة ستتعايش كـoverloads وتجعل استدعاءات مثل `challenge_live_finish(m,'x')` غامضة.

## 2. القرار المعتمد من المالك
حذف كل كائنات `challenge_*` من مشروع الاختبار فقط، ثم تطبيق 0008 ← 0009 ← 0010 من الـrepo، مع تحقق بعد كل واحدة. السجل `supabase_migrations.schema_migrations` لا يُمسّ (يحفظ DDL النسخة القديمة).

## 3. الخطوات

### 3.1 قبل الحذف (قراءة)
- `list_projects` وتحقق من المعرّف. `list_migrations` و`list_tables(verbose)`.
- صدّر DDL النسخة المنشورة للمرجع: `select name, array_to_string(statements, E';\n') from supabase_migrations.schema_migrations where name like 'challenge%'` واحفظه في `docs/challenges/PUBLISHED_TEST_DDL_2026-10-04.sql` (يُضاف للـrepo).
- حلّل باختصار الفرق التصميمي بين النسخة المنشورة والـrepo (هل فيها فكرة مفيدة؟ لماذا `challenge_active_slots`؟) في فقرة واحدة بالـHANDOFF. لا تعتمدها.
- تأكد أن كل جداول `challenge_*` ما زالت فارغة (`count(*)`)، وإلا **توقف واسأل المالك**.
- اسرد الكائنات التي ستُحذف (dry-run SELECT) واعرضها في الـHANDOFF قبل الحذف:
```sql
select 'table' k, tablename::text n from pg_tables where schemaname='public' and tablename like 'challenge%'
union all select 'view', viewname from pg_views where schemaname='public' and viewname like 'challenge%'
union all select 'fn', oid::regprocedure::text from pg_proc where pronamespace='public'::regnamespace and proname like 'challenge%'
union all select 'trigger', tgname||' on '||tgrelid::regclass::text from pg_trigger where not tgisinternal and tgname like 'challenge%'
union all select 'policy', polname||' on '||polrelid::regclass::text from pg_policy where polrelid::regclass::text like '%challenge%';
```
- تحقق أن لا شيء خارج `challenge_*` يعتمد عليها (`pg_depend`/FK من جداول المنتج). المتوقع: لا شيء. وتأكد أن صف `feature_flags` للعلمين **يبقى** (يُعاد `live_matches_enabled` عبر 0010).

### 3.2 الحذف (على مشروع الاختبار فقط، عبر `apply_migration` باسم `reset_challenge_objects_for_repo_rebuild`)
اكتب سكربتًا مبنيًا على نتيجة الـdry-run (راجعه يدويًا؛ هذا القالب غير مُجرَّب):
```sql
BEGIN;
DROP VIEW IF EXISTS public.challenge_playable_units CASCADE;
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT oid::regprocedure AS sig FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname LIKE 'challenge%' LOOP
    EXECUTE 'DROP FUNCTION IF EXISTS '||r.sig||' CASCADE'; END LOOP;
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'challenge%' LOOP
    EXECUTE format('DROP TABLE IF EXISTS public.%I CASCADE', r.tablename); END LOOP;
END $$;
COMMIT;
```
ثم تأكد: صفر كائنات `challenge%`، و`feature_flags` سليم، و`users` والجداول الأخرى لم تتأثر (قارن `list_tables` قبل/بعد).

### 3.3 التطبيق من الـrepo، واحدة واحدة
بهذا الترتيب وبـ`apply_migration` (أسماء مثل `challenge_content_0008_repo`, `challenge_game_0009_repo`, `challenge_live_0010_repo`) مع محتوى الملف **كما هو بلا تعديل**:
`db/migrations/0008_challenge_content.sql` ← `0009_challenge_game.sql` ← `0010_challenge_live.sql`.
بعد **كل** واحدة: `list_tables`، عدّ الدوال (`challenge_live%` المتوقع 15 دالة بعد 0010 بحسب HANDOFF_3B5)، وتحقق من وجود `countdown_until` و`message_id` و`challenge_live_queue`. إن فشلت إحداها: لا تُصلحها بتعديل الملف؛ سجّل الخطأ الحرفي وتوقف.
اعتماد 0008–0010 على كائنات 0001–0007 الحقيقية هو **بالضبط** ما لم يُغطَّ في 3B.5: سجّل أي اعتماد كُسر (مثل `users`, `feature_flags` بأعمدتها الفعلية).

### 3.4 فحوص الأمان والصلاحيات (على المخطط الحقيقي)
- RLS مفعّل على `challenge_live_queue` وكل جداول التحديات؛ لا policies لـanon/authenticated.
- `has_function_privilege` و`has_table_privilege` لكل من `anon`, `authenticated`, `service_role` على دوال RPC الحية وجدول الطابور: المتوقع anon/authenticated **مرفوض**، service_role مسموح.
- `get_advisors(security)` و`get_advisors(performance)` بعد الـDDL، وسجّل ما يخص جداول التحديات (لا تُصلح خارج النطاق).
- الأعلام: `challenge_system_enabled=false` و`live_matches_enabled=false` بعد التطبيق.
- اختبار rollback ثم reapply لـ0010 (`0010_challenge_live_rollback.sql`) على مشروع الاختبار: بعد rollback لا دوال `challenge_live*` ولا جدول الطابور (تبقى نسخ 0009 المحروسة)، وبعد reapply تعود. ثم أعد الحالة النهائية = 0010 مطبّقة.

### 3.5 اختبارات الأحمال والتزامن على Supabase الحقيقي
- بيانات الاعتماد (`DATABASE_URL` المباشر أو pooler من لوحة Supabase) **يوفّرها المالك**؛ لا تحاول استخراجها من الـrepo أو من أي مشروع. إن لم تتوفر ولا الشبكة من البيئة: نفّذ ما أمكن عبر `execute_sql` (3.3–3.4 و3.6) وسجّل `live_concurrency.mjs` كـ«لم يُنفَّذ + السبب».
- **قبل التشغيل اقرأ** `tests/sql/live_concurrency.mjs` و`run_challenge_live.mjs`: كيف يُنشئ `users`/`feature_flags`/الأدوار؟ في 3B.5 بُنيت جداول **بديلة** `users(user_id,points)`. على Supabase الحقيقي `users` له أعمدة أخرى (قد تكون NOT NULL). لا تُنشئ ولا تُعدّل جدول `users` الحقيقي بأي شكل يكسره؛ تكيّف مع الاختبار بأصغر تعديل (ملفات الاختبار فقط) أو استخدم صفوفًا مُنظَّفة بعد الاختبار. فحص «`users.points` لم تتغير» يجب أن يتم على صف/صفوف حقيقية بالمخطط الفعلي.
- شغّل `live_concurrency.mjs` (المتوقع 10/10، لا deadlocks، `pg_stat_database.deadlocks` قبل/بعد) 3 مرات على الأقل مع تنظيف بين المرات. ثم تأكد أن الأعلام عادت false وأن لا بقايا في الجداول (أو نظّفها بحذف صفوف الاختبار فقط، لا DDL).
- `challenge_content_test.sql` (0008) على المخطط الحقيقي.
- EXPLAIN لاستعلام peek (`challenge_live_try_match`) على الفهرس `challenge_live_queue_pool` بحجم صغير حقيقي؛ لا حاجة لحمل 30 ألف صف هنا.

### 3.6 اختبار PostgREST (إن أمكن)
استدعاء RPC عبر REST بمفتاح `anon` ثم `authenticated` ثم `service_role` للتأكد من الرفض/القبول فعليًا (هذا ما لا يغطيه psql). المفاتيح من المالك فقط.

## 4. المخرجات المطلوبة
1. `docs/challenges/HANDOFF_3C5.md` بنفس أسلوب HANDOFF_3B5: ما نُفِّذ فعلًا بالأرقام (ما طُبّق، الأخطاء الحرفية، نتائج التزامن، الـadvisors، الصلاحيات)، وما لم يُنفَّذ وسببه، وهل المطلوب من 3B.5 §18 تحقق، والـtechnical debt.
2. `docs/challenges/PUBLISHED_TEST_DDL_2026-10-04.sql` (DDL النسخة القديمة للمرجع).
3. commit واضح مثل `test(challenges): rebuild test project from repo and validate on supabase`.
4. نسخة Self-Contained كاملة: `abiadquizbot_after_3C5.zip` (مع `.git`، وليس incremental).
5. في نهاية الرد: اسم ملف المشروع، اسم الـHANDOFF، HEAD النهائي، وهل يمكن الانتقال إلى الاختبار اليدوي بحسابين Telegram (§19 في HANDOFF_3C) نعم/لا ولماذا.

## 5. معيار النجاح
- مشروع الاختبار يحمل كائنات الـrepo فقط (لا بقايا النسخة القديمة)، الأعلام false، الإنتاج لم يُلمس.
- 0008→0010 مطبّقة بلا تعديل على الملفات، صلاحيات anon/authenticated مرفوضة، RLS مفعّل.
- `live_concurrency.mjs` 10/10 على Supabase الحقيقي بلا deadlocks (أو توثيق صريح أنه لم يُنفَّذ).
- `users.points` لم تتغير على المخطط الفعلي.

## 6. بعدها (ليست ضمن هذه المهمة)
اختبار Telegram اليدوي بحسابين وفق `HANDOFF_3C.md` §19 بعد ربط البوت بمشروع الاختبار (أعلام مفعّلة هناك فقط).
