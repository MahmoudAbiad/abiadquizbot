# HANDOFF 3B — Backend + Database للمواجهة المباشرة (Real-Time)

التاريخ: 2026-10-05. الفرع: `feature/challenges`. بُني فوق `33301c8` (HANDOFF_3A).

> **أهم ما يجب معرفته قبل أي شيء**
> 1. **migration `0010` لم تُشغَّل على أي PostgreSQL في هذه الجلسة.** لا يوجد محرك PostgreSQL/PGlite محلي ولا شبكة، ولم أطبّقها على Supabase لأن البرومبت لم يأذن بتغيير أي قاعدة. كل SQL جديد **مراجَع يدويًا فقط وغير منفَّذ**، وسكربتا الاختبار الجديدان (`run_challenge_live.mjs`, `live_concurrency.mjs`) **كُتبا ولم يُشغَّلا**.
> 2. لم يُنفَّذ أي شيء على Supabase، ولا Backup، ولا Push (الـremote الوحيد `origin` هو ملف الـbundle المرفوع للقراءة فقط). لا Telegram UI، ولا Spectator/Friend Links/Bot.
> 3. المُنفَّذ فعليًا: اختبارات Python الجديدة (14) والقديمة للتحديات (34 منها 30 ناجحة و4 أخطاء `aiogram` غير مثبت، كما في 3A).

## 1. Schema الجديد (إضافي فقط)

| الكائن | الوصف |
|---|---|
| `challenge_live_queue` | **صف واحد لكل لاعب** (PK=`user_id`): `status` = `waiting/matched/cancelled/expired/done`، `branch/subject/unit_id`، `trophies` وقت الدخول، `request_key`، `match_id`، `joined_at`، `expires_at`. الـPK وحده يمنع دخول Queue مرتين أو الارتباط بمباراتين. RLS مفعّل، `service_role` فقط. |
| `challenge_matches.countdown_until` | عمود جديد؛ المباراة المباشرة بحالة `created` = عدّ تنازلي حتى هذا الوقت. |
| `challenge_matches.status` | وُسّع الـCHECK بقيمة `cancelled` فقط. |
| `challenge_match_players.message_id` | رقم رسالة Telegram لكل لاعب (للـUI لاحقًا). |
| فهارس | `challenge_live_queue_pool(unit_id,trophies,user_id) WHERE waiting` (بحث المطابقة)، `challenge_live_queue_expiry`، `challenge_one_active_live_defender` (فريد جزئي، دفاع إضافي)، `challenge_live_countdown_due`، `challenge_match_players_user(user_id,match_id)` (سقوف اليوم وتكرار المحتوى). لا فهارس بلا استعلام يستخدمها. |
| flag | `live_matches_enabled` = **false**، وسُجّل في `constants.FEATURE_FLAGS_REGISTRY` و`_GAME_FLAGS`. |

أُعيد استخدام `challenge_matches/match_players/match_answers/events` كما هي: `kind='live'`، اللاعبان بدور `player`، و`attacker_id < defender_id` دائمًا (ترتيب قانوني = ترتيب أقفال حتمي + فهرس الزوج الموجود).

## 2. Migration
`db/migrations/0010_challenge_live.sql` + `0010_challenge_live_rollback.sql`. Backward-compatible: لا حذف/تغيير أعمدة، وإعادة التطبيق idempotent (مصممة هكذا، لم تُجرَّب).
تعديلان على كائنات 0009 (ضرورة، ليسا توسعة): إعادة تعريف `challenge_attack_state` و`challenge_advance_attack` بإضافة `AND kind='async_attack'` فقط، كي **لا تستطيع دوال async قراءة/تحريك مباراة live** (عامل async القائم كان سيلتقط مباريات live المستحقة). والـrollback يبقي النسختين المحروستين (متوافقتان). وفي مستودع Python أُضيف فلتر `kind` إلى `active_attack/history/due_attacks/has_active_attacks`.
الـrollback يحذف تاريخ live فقط؛ **إحصاءات الملفات (xp/wins…) المضافة من مباريات live تبقى في `challenge_profiles`**.

## 3. تصميم الـQueue
- `join(user, unit, request_key)`: يقفل ملف اللاعب ثم صف الطابور. **نفس `request_key` = نفس الحالة الحالية** (حتى بعد cancel/expire/done) فلا يعيد retry أو تحديث Telegram مكرر بحثًا ملغيًا. مفتاح مختلف أثناء `waiting` → `already_queued`، أثناء مباراة نشطة → `in_live_match`، وأثناء هجوم async كمهاجم → `active_attack`.
- الوحدة المختارة ثابتة؛ لا انتقال لمادة/وحدة أخرى أبدًا. يشترط وحدة نشطة بها اختبار active، وفرع اللاعب = فرع الوحدة.
- `cancel(user, request_key?)`: idempotent؛ يلغي `waiting` فقط (وبمفتاح البحث نفسه إن أُرسل، فلا يقتل cancel قديم بحثًا جديدًا). إن كانت المباراة قد أُنشئت يعيد `matched` ولا يمسها.
- `expire_queue`: جملة واحدة `FOR UPDATE SKIP LOCKED` على صفوف الطابور فقط (لا خطر أقفال). مهلة البحث 120 ث.
- trigger `challenge_block_async_while_live`: يمنع بدء هجوم async لمن هو في الطابور/مباراة live.

## 4. خوارزمية المطابقة (`challenge_live_try_match`)
1. **Peek بلا أقفال**: أقرب لاعب `waiting` في نفس `unit_id` ضمن `±matchmaking_range` (فهرس الـpool، نطاق محدود)، مرتبًا بـ`abs(diff), joined_at, user_id`. بلا `ORDER BY RANDOM`، بلا scan كامل، بلا N+1.
2. النطاق يتوسع بالانتظار: `100 + 100×floor(waited/10s)` حتى 500 (قيم في `challenge_rules.json`: `live_range_*`). شرط الزوج: الفرق ≤ **الأوسع** من نطاقيهما (متماثل).
3. **قفل الزوج بترتيب ثابت**: ملفاتهما `user_id ASC` ثم صفا الطابور `ASC`، ثم إعادة قراءة والتحقق من كل شيء تحت الأقفال (waiting، لم تنتهِ المهلة، نفس الوحدة، النطاق، لا هجوم async جارٍ). عند الفشل يرجع `retry=true` ويعيد Python حتى 3 مرات.
4. اختيار الاختبار **Server-Side، مرة واحدة**: اختبار `active` بالوحدة يفضَّل ما لم يلعبه أي منهما خلال نافذة التكرار، ثم `FOR SHARE` على test→unit→subject→branch (نفس ترتيب 0009). لا يوجد AI fallback؛ إن لم يوجد → يبقيان في الانتظار (`reason=no_content`).
5. الأسئلة: حتى `live_questions` (10) عينة حتمية من UUID المباراة، مرتبة بترتيبها الأصلي، تُثبَّت في `snapshot` مع `rules` وأسماء العرض. اللاعبان يقرآن نفس الـsnapshot والترتيب والمواعيد. تعطيل الاختبار لاحقًا لا يؤثر.

## 5. State machine للمباراة
```
created (countdown) ──بعد countdown_until──▶ in_progress ──▶ completed
   │                                              ├──▶ abandoned  (انسحاب: leave)
   └── leave ▶ cancelled                          └──▶ expired    (تجاوز server_deadline)
```
داخل `in_progress` السؤال الحالي يُحلّ **مرة واحدة** عندما: يجيب الطرفان أو `now >= question_deadline` (يُدرج صف مهلة لمن لم يجب)، ثم يُفتح التالي أو تُجرى التسوية. الكل في `challenge_live_progress` الداخلية (idempotent) تستدعيها: `answer`, `tick`, `leave`.
- `server_deadline = created + countdown + n×question_seconds + live_grace_seconds(60)`؛ تجاوزه = `expired`: **بلا مكافآت ولا إحصاءات** (فشل بنية، لا ذنب لاعب).
- أحداث `challenge_events`: `match_created, match_started, answer_received, question_resolved, question_opened, match_completed, match_abandoned, match_timeout, match_cancelled`.
- **PostgreSQL مصدر الحقيقة.** لا Redis في هذه الطبقة أصلًا. بعد restart: `sweep()` يقرأ المستحق من القاعدة (`challenge_live_due`) ويعمل `tick` لكل مباراة؛ يمكن لأي عدد من العمال تشغيله.
- *تحفظ مقصود:* إن توقف العمال طويلًا، أول tick يحل السؤال المتأخر (مهلة للطرفين) ثم يفتح التالي من لحظته، وقد ينتهي بـ`expired`. لا نمدد وقت أحد.

## 6. الإجابة والتوقيت (قاعدة واحدة = قاعدة async)
الوقت `clock_timestamp()` **بعد** أخذ الأقفال، لا وقت من العميل.
| لحظة الوصول | النتيجة |
|---|---|
| قبل `question_deadline` (صارمًا) | تُسجَّل مرة واحدة وتُحسب بالصيغة المركزية `challenge_question_score` (دقة 95 + سرعة حتى 5) |
| **عند** الـdeadline بالضبط | مهلة: 0 نقطة، الخيار يُهمل |
| بعده | يُحلّ السؤال مهلةً لمن لم يجب؛ هذه الإجابة تُهمل ولا تُدرج |
- `PK(match,user,question_index)` + `UNIQUE(user,update_key)` + إعادة فحص تحت القفل: ضغط مزدوج، callback مكرر، update مكرر، retry — كلها لا تسجل ثانيةً ولا تتقدم سؤالًا. فهرس قديم يُتجاهل. خيار خارج النطاق → `invalid_option`.
- **لا تسريب**: `challenge_live_state` لا يعيد snapshot؛ السؤال المفتوح نص/خيارات فقط؛ حقل `opponent.answered` منطقي فقط؛ `correct_option_id` وخيارا الطرفين لأسئلة **محلولة** فقط (`previous`)؛ نتيجة الخصم الجارية تُحسب من الأسئلة المحلولة فقط. لا يُسجَّل أي جواب صحيح في Logs.
- إجابتان "متزامنتان": الأقفال ترتب التنفيذ، والوقت يؤخذ بعد القفل؛ لا تعارض، والنتيجة لا تعتمد على من وصل أولًا إلا بفرق زمن السرعة.

## 7. المكافآت وحمايتها
- XP/كؤوس التحديات فقط (`challenge_profiles/towers/unit_stats`). **لا لمس لـ`users.points`** (اختبار نصي يمنع ظهور `users.points`/`UPDATE users` في الـmigration، واختبار SQL يتأكد من ثباتها، غير مشغَّل).
- فوز: `+live_win_trophies`(8)، تعادل (نفس النقاط): `+live_draw_trophies`(2) للطرفين، خسارة `live_loss_trophies`(0 افتراضيًا). XP = `xp_per_correct × الصحيح` **لمباراة مكتملة فقط**؛ الانسحاب لا يعطي XP لأحد. انسحاب: المنسحب خاسر والخصم يربح الكؤوس فقط (وفق الحدود).
- **Anti-farming** داخل التسوية تحت أقفال الزوج: (1) حد مكافآت لنفس الزوج كل 24 س (`live_pair_daily_limit`=3)، (2) محتوى تكرر للاعب خلال `repeat_content_seconds` لا يكافأ، (3) السقوف اليومية `daily_xp_limit/daily_trophy_limit` محسوبة من `match_players.xp_delta/trophies_delta` (مشتركة مع async). *حد معروف:* async لا يرى مكافآت live عند حساب سقفه (التراكب ممكن حتى مرتين السقف)؛ لم أستبدل `challenge_finish_attack` لتجنب تعديل واسع.
- التسوية **مرة واحدة**: `challenge_live_finish` يقفل صف المباراة ويخرج إن كانت المباراة منتهية؛ كل شيء (إجابة أخيرة، إحصاءات، مكافآت، تحرير الطابور) في معاملة واحدة.

## 8. Locking / Idempotency
ترتيب الأقفال في **كل** الدوال الجديدة: ملفات اللاعبين `ASC` (`FOR NO KEY UPDATE`، لا يتعارض مع key-share الخاص بالـFK) ← صفوف الطابور `ASC` ← محتوى `FOR SHARE` (test→unit→subject→branch) ← صف المباراة. استخدمت: constraints فريدة (PK الطابور، PK الإجابات، `update_key`، فهرسا المباراة النشطة)، مفاتيح idempotency (`request_key`, `update_key`)، وقت الخادم، وإعادة التحقق بعد القفل. `expire_queue` يلمس الطابور فقط مع `SKIP LOCKED`؛ `due()` للقراءة فقط ثم `tick` لكل مباراة بمعاملته (لا يُمسك قفل مباراة ثم يُطلب قفل لاعب أبدًا).
تغطية السيناريوهات المطلوبة: عاملان يطابقان نفس اللاعبين (قفل زوج + إعادة قراءة)، مباراتان لنفس اللاعب (PK الطابور + الفهارس)، ربط بخصمين، إجابتان، ضغطتان، webhook مكرر، timer مقابل إجابة (قفل المباراة)، restart (`sweep`)، DB retry (rollback كامل + idempotent)، تسويتان (قفل + حالة نهائية).

## 9. الاختبارات التي شُغِّلت فعليًا ونتائجها
| الأمر | النتيجة |
|---|---|
| `python3 -m unittest tests.test_challenge_live` | **14 اختبارًا، OK**: نطاق المطابقة والتوسع والتماثل، نتيجة التعادل، التحقق من قواعد live، سلوك الـRepository بعميل مزيّف (join→match، عدم المطابقة بعد cancel، retry المحدود، تمرير request/update key، أسماء أحداث السجل وعدم تسجيل محتوى الإجابات، `sweep` من القاعدة)، وفحوص نصية للـmigration |
| `python3 -m unittest tests.test_challenge_game tests.test_challenge_content tests.test_challenge_repo tests.test_challenge_live` | 48 اختبارًا: 44 ناجحًا + **4 أخطاء `ModuleNotFoundError: aiogram`** (نفس 3A؛ بيئة بلا تبعيات). لا تُحسب ناجحة |
| `python3 -m unittest discover -s tests` | 169 اختبارًا، `errors=7`، `skipped=4`: كلها `aiogram`/`fitz` غير مثبتين (4 تحديات + دفع + pdf×2). لا فشل منطقي، لكنها **لم تعمل**. `smoke_import_test.py` لم يُشغَّل |
| `py_compile` للملفات المعدَّلة، `node --check` للسكربتين | ناجح |
**لم يُشغَّل إطلاقًا:** أي SQL من 0010، `tests/sql/run_challenge_live.mjs`، `tests/sql/live_concurrency.mjs`، مجموعة SQL القديمة (PGlite غير مثبت ولا شبكة).

## 10. ما لم يُختبر فعليًا (وهو الأخطر)
- **صحة SQL نفسه**: تركيب plpgsql، الأنواع، أسماء الأعمدة والمتغيرات، سلوك `ORDER BY … FOR UPDATE`، `ON CONFLICT`، grants. أي خطأ بسيط ممكن ولم يُكتشف بعد.
- `run_challenge_live.mjs` (34 سيناريو: queue/range/cancel/duplicate/one-active/same-questions/disabled test/answers/deadline/timeout/settlement once/XP once/anti-farming/caps/forfeit/expiry/rollback/RLS/EXPLAIN): مكتوب بدقة لكن **لم يُجرَّب**؛ قد يحتاج إصلاح اختبارات نفسها.
- **التزامن الحقيقي متعدد الاتصالات:** PGlite اتصال واحد ولن يثبته. `live_concurrency.mjs` (20 لاعبًا متزامنين، cancel مقابل match، إجابات/leave متزامنة، تكرار update key) يحتاج PostgreSQL حقيقي وقاعدة اختبار تتبع 0008–0010. تغطية deadlocks مبنية على ترتيب الأقفال نظريًا فقط.
- أداء الطابور تحت حمل، EXPLAIN الحقيقي على بيانات كبيرة، PostgREST/صلاحيات المشروع الحقيقية، وUI/Telegram.
- لم يُراجَع `Test_telegramquizbot` (مخططه كان قد أُعيد بناؤه إلى 0007 وفق 3A) ولم يُطبَّق عليه 0008–0010.

## 11. APIs لمرحلة Telegram UI (`helpers/supabase_helper/challenge_live.py`)
| الدالة (RPC) | الاستخدام |
|---|---|
| `join_queue(user, unit_id, request_key)` (`challenge_live_join` ثم `try_match`) | دخول البحث + محاولة مطابقة فورية. أخطاء: `already_queued, in_live_match, active_attack, game_disabled, no_content, branch_mismatch, profile_missing, invalid_request` |
| `try_match(user)` / `pair_waiting(ids)` / `waiting_users()` | إعادة المحاولة من الـheartbeat كلما اتسع النطاق |
| `queue_state(user)` | `idle/waiting/matched/cancelled/expired/done` + `match_id` + `waited_seconds` |
| `cancel_search(user, request_key)` | إلغاء البحث |
| `match_state(match_id, user)` | **الإسقاط الوحيد الآمن للاعب** (`me/opponent/question/deadline/countdown_until/previous/result/server_now`) |
| `answer(match_id, user, index, option, update_key)` | `update_key` = معرّف update/callback من Telegram |
| `leave(match_id, user)` | انسحاب/إلغاء أثناء العدّ |
| `tick(match_id)` / `due_matches()` / `expire_queue()` / `sweep()` | قلب العامل الخلفي؛ يعيد `expired_users` لإشعارهم |
| `attach_message(match_id, user, message_id)` | حفظ رسالة كل لاعب |
| `has_live_work()` | لتخفيف الـpolling |
ملاحظات للـUI: اعرض `server_now`/`deadline` فقط؛ لا تعتمد على ساعة العميل. الفلاجات: `challenge_system_enabled` + `live_matches_enabled` تُفحص داخل `join` فقط (المباريات الجارية تكتمل حتى لو أُطفئت). الهوية من `from_user.id` لا من callback_data.

## 12. قرارات واعية وحدود
- أضفت `cancelled` فقط (انسحاب أثناء العدّ)؛ لا `countdown/question_active` كحالات منفصلة (تُشتق من `created`+`countdown_until` و`question_deadline`).
- فرع/مادة غير مسموح تغييرها تلقائيًا؛ إن لم يوجد خصم تنتهي المهلة (`expired` للطابور) ولا تُوسَّع الوحدة.
- لا إشعارات outbox للـlive (جدول 0009 مقيّد بنوعي async)؛ الـUI يستخدم `sweep()`/polling.
- الأسماء المعروضة من `display_name` فقط.

## 13. الخطوة التالية المقترحة
1. بيئة PostgreSQL حقيقية (فرع Supabase اختبار أو ترقية `Test_telegramquizbot` لمطابقة الإنتاج) ثم تطبيق 0008→0009→0010 **عليها فقط** بإذن المالك.
2. تشغيل `run_challenge_live.mjs` عبر PGlite لإصلاح أخطاء SQL/الاختبارات الأولى، ثم `live_concurrency.mjs` على الاتصالات الحقيقية.
3. بعد نجاحها: بناء Telegram Live UI فوق الـAPIs أعلاه.
