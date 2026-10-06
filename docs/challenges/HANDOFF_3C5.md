# HANDOFF 3C.5 — إعادة بناء `challenge_*` على مشروع الاختبار والتحقق

> **تنبيه صدق:** هذه المهمة بدأت في حساب Claude آخر ونفدت أرصدته قبل التسليم. هذه الجلسة (الثانية) **لم تجد** مخرجات الجلسة الأولى (HANDOFF/DDL/commit/zip) ولا سجل نتائجها، فتحققتُ من **الحالة الفعلية للقاعدة** وأعدتُ ما أمكن بنفسي، وأبلغ بدقة عمّا نُفِّذ هنا وما لم يُنفَّذ.

## 1. الأمان
- كل الكتابة/القراءة على `Test_telegramquizbot` (`xhlksusmczccqyvbxylo`) فقط، بعد مطابقة الاسم والمعرّف عبر `list_projects`. لم يُلمس `telegramquizbot` (الإنتاج) ولا أي مشروع آخر.
- لم تُعدَّل migrations ولا كود المنتج. لم تُنفَّذ أي كتابة على القاعدة في هذه الجلسة (قراءة + اختبار داخل DO block يُلغى تلقائيًا بـEXCEPTION).

## 2. الحالة الفعلية للقاعدة (تحقق في هذه الجلسة، 2026-10-06)
- سجل الـmigrations: 0001a…fix_payment_credit_order_comment (baseline) ثم `challenge_content_0008`, `challenge_game_0009`, `challenge_live_0010` (القديمة) ثم `reset_challenge_objects_for_repo_rebuild` ثم `challenge_content_0008_repo`, `challenge_game_0009_repo`, `challenge_live_0010_repo`.
- **مطابقة الملفات:** md5 للـDDL المسجل في الـ3 migrations «_repo» = md5 ملفات `db/migrations/0008/0009/0010` بالضبط (a6351f55…, ace5cf50…, 90262181…) ⇒ طُبّقت بلا تعديل.
- 15 دالة `challenge_live*` (المتوقع من 3B.5)، 25 دالة `challenge*` إجمالًا، 14 جدول `challenge_*`، العمودان `challenge_matches.countdown_until` و`challenge_match_players.message_id` والجدول `challenge_live_queue` موجودة. لا بقايا للجدولين القديمين `challenge_queue`/`challenge_active_slots`.
- الأعلام: `challenge_system_enabled=false`، `live_matches_enabled=false`.
- كل جداول `challenge_*` فارغة (count=0)، `users` = 0 صف، `pg_stat_database.deadlocks`=0.

## 3. الصلاحيات و RLS (مفحوصة فعليًا)
- RLS مفعّل على كل الـ14 جدول `challenge_*`، ولا policies عليها.
- `anon` و`authenticated`: مرفوضان (`has_table_privilege`/`has_function_privilege`) على كل جداول ودوال `challenge*` (25 دالة)؛ `service_role`: مسموح.
- **PostgREST (نفّذه المالك يدويًا بـcurl، بمفتاح anon العام لمشروع الاختبار):** `GET /rest/v1/challenge_matches?select=id&limit=1` ⇒ **HTTP 401**، `Proxy-Status: PostgREST; error=42501`، الرسالة `permission denied for table challenge_matches` ⇒ الرفض فعلي عبر REST. لم يُرسل مفتاح service_role ولا authenticated، وكذلك `POST /rest/v1/rpc/challenge_live_queue_state` بمفتاح anon ⇒ **HTTP 401**، `error=42501`، `permission denied for function challenge_live_queue_state` ⇒ رفض RPC فعلي عبر REST.

## 4. Advisors (بعد الـDDL)
- أمان: ما يخص التحديات = INFO `rls_enabled_no_policy` على 14 جدول (مقصود: وصول service_role فقط). باقي الملاحظات **من الـbaseline 0001 وليست من التحديات** ولم تُصلَح (خارج النطاق): `rls_disabled_in_public` على 15 جدولًا منها `feature_flags` (مقروء لـanon)، ودوال `SECURITY DEFINER` منها `admin_add_points_atomic`, `deduct_user_points_atomic`, `refund_*`, `update_app_setting_atomic`… قابلة للتنفيذ من `anon`/`authenticated`، و`favorite_quizzes` بـpolicy بلا RLS، و2 security-definer views، و6 دوال بـsearch_path متغير.
  - **تنبيه مهم:** إن كان الإنتاج على نفس الـbaseline فهذه ثغرة حقيقية تمس النقاط المدفوعة. لم أفحص الإنتاج (ممنوع). يُنصح بفحصه بالطرق الرسمية.
- أداء: INFO فقط — 10 FKs بلا فهرس مغطٍّ في جداول التحديات (`challenge_live_queue` ×3: branch/match/subject، `challenge_matches` ×3، `challenge_match_answers.question_id`، `challenge_notifications.user_id`، `challenge_towers.subject_id`، `challenge_unit_stats.unit_id`)، وفهارس «غير مستخدمة» لأن الجداول فارغة. لا إجراء الآن؛ يُراجع عند وجود حمل حقيقي.

## 5. الاختبارات
| الاختبار | النتيجة |
|---|---|
| `tests/sql/challenge_content_test.sql` (0008، 12 سيناريو) على المخطط الحقيقي | **نجح** (نُفِّذ كـDO block مع EXCEPTION ختامي للتراجع؛ لم يُترك أي صف) |
| `tests/sql/live_concurrency.mjs` (10/10، 3 مرات) | **لم يُنفَّذ** — لا `DATABASE_URL` ولا شبكة من بيئتي |
| `run_challenge_live.mjs` / `run_challenge_game.mjs` على Supabase | **لم يُنفَّذ** (نفس السبب) |
| فحص `users.points` لم تتغير على صفوف حقيقية | **لم يُنفَّذ** (users فارغ ولم تُشغَّل اختبارات التزامن) |
| EXPLAIN لـpeek على `challenge_live_queue_pool` | **لم يُنفَّذ بشكل مفيد** — الجدول فارغ فالخطة لا تدل على شيء. تعريف الفهرس: `(unit_id, trophies, user_id) WHERE status='waiting'` |
| rollback ثم reapply لـ0010 | **لم يُعَد في هذه الجلسة.** وصف الجلسة السابقة يقول إنه نُفِّذ، ولا أستطيع إثباته؛ الحالة النهائية الحالية (15 دالة، عمودان، جدول الطابور) تتسق مع «0010 مطبّقة». ملف الرجوع راجعته نصيًا وهو يسقط دوال/جدول/فهارس/أعمدة 0010 ويُبقي نسخ 0009 |
| PostgREST | **anon على جدول وعلى RPC: نجح الرفض (401/42501) في الحالتين**. غير منفَّذ: service_role (قبول)، authenticated |

## 6. تحليل النسخة القديمة المنشورة (لا تُعتمد)
اعتمدت على جدولين: `challenge_queue` (صف واحد لكل مستخدم بحالة waiting/matched/cancelled/expired وفهرسين جزئيين) و`challenge_active_slots` (PK على user_id لمنع تعدد المباريات النشطة لنفس اللاعب بغض النظر عن النوع، كُتب لها backfill من الهجمات غير المتزامنة). تصميم الـrepo يحقق نفس منع التعدد بفهرس فريد جزئي (`challenge_one_active_live_defender`) وتريجر `challenge_block_async_while_live`، ويضيف `countdown_until` و`message_id` وكل دوال tick/answer/leave/due/progress/apply/range التي تعتمد عليها 3B.5 و3C. فكرة `active_slots` مفيدة نظريًا (قفل أبسط على صف واحد) لكن لا تُعتمد. التفاصيل والبصمات في `PUBLISHED_TEST_DDL_2026-10-04.sql` (جزئي، والنص الكامل محفوظ في سجل القاعدة).

## 7. هل بنود 3B.5 §18 تحققت؟
تحقق جزئي: **تطبيق 0008→0010 على المخطط الحقيقي (0001–0007) نجح بلا خطأ، والاعتماديات على `users`/`feature_flags` لم تُكسَر**، وتحقق RLS/الصلاحيات. **لم يتحقق** بعد: التزامن على Supabase الحقيقي، وسلامة `users.points`، وPostgREST.

## 8. الدين التقني / المطلوب من المالك
1. تشغيل `live_concurrency.mjs` 3 مرات بـ`DATABASE_URL` (pooler/مباشر)، بعد قراءة كيفية إنشائه `users` البديل — جدول `users` الحقيقي أعمدته مختلفة، فيلزم تكييف ملفات الاختبار فقط (صفوف `user_id>=7_000_000` مع تنظيفها).
2. إكمال PostgREST: service_role (متوقع قبول) فقط، وauthenticated اختياري.
3. EXPLAIN بعد زرع بيانات صغيرة.
4. إعادة اختبار rollback→reapply لـ0010 وتوثيقه.
5. فحص ثغرات الـbaseline (دوال النقاط executable من anon، `feature_flags` بلا RLS) على الإنتاج بطرقك.

## 9. الانتقال لاختبار Telegram اليدوي بحسابين (HANDOFF_3C §19)
**لا — ليس بعد.** البنية الأساسية سليمة (مخطط الـrepo مطبّق، RLS/صلاحيات صحيحة، 12/12 اختبار محتوى)، لكن التزامن الفعلي وسلامة `users.points` لم يُثبتا على Supabase الحقيقي. نفّذ البندين 1 و2 أولًا (أو اقبل المخاطرة كاختبار يدوي في مشروع الاختبار فقط، مع إبقاء الأعلام false في الإنتاج).
