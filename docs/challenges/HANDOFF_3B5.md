# HANDOFF 3B.5 — التحقق من Backend المواجهات المباشرة على PostgreSQL حقيقي

التاريخ: 2026-10-05. الفرع: `feature/challenges`. **commit البداية: `5d4719c`** (نهاية 3B). commit هذه المرحلة: انظر `git log -1` (الرسالة `test(challenges): validate live backend on postgres`).

> **الخلاصة:** `0010` تعمل فعليًا على PostgreSQL حقيقي، ومجموعة SQL الحية 34/34، والتزامن متعدد الاتصالات 10/10 بلا Deadlocks ولا ازدواج. **لم يُعدَّل أي migration ولا كود منتج**؛ كل الإصلاحات في ملفي الاختبار فقط. **لم يُختبر على Supabase** (لا وصول) ولا على مخطط Production الكامل (انظر §17).

## 1. بيئة الاختبار (ما هي وما ليست)
- **لا وصول إلى Supabase في هذه الجلسة.** لم يُلمس `Test_telegramquizbot` ولا Production. لم يُشغَّل أي flag.
- استُخدم **PostgreSQL 16.15 حقيقي مثبَّت محليًا** (مثيل مؤقت على منفذ 5433، قاعدة `abiad_test`)، يُعاد بناؤها من الصفر في كل تشغيل. مجموعة PGlite (PostgreSQL 17.5 WASM) استُخدمت لـ`run_challenge_live.mjs` و`run_challenge_game.mjs` كما صُمّمتا.
- **المخطط**: لم تُطبَّق 0001–0007. وُضعت أدوار `anon/authenticated/service_role` وجدولان بديلان (`feature_flags` و`users(user_id,points)`) كما تفعل مجموعات الاختبار نفسها، ثم 0008→0009→0010. أي اعتماد لـ0008–0010 على كائنات من 0001–0007 غير الجدولين أعلاه **غير مغطى**.
- **Backup:** لا يوجد؛ لم تُعدَّل قاعدة موجودة (قاعدة مؤقتة تُبنى وتُحذف). لا أدّعي نسخة احتياطية.

## 2. Migrations المطبَّقة فعليًا
0008 ثم 0009 ثم 0010 على PostgreSQL 16 (كل مرة من قاعدة جديدة). **0010 نجحت من أول مرة** (NOTICE واحد فقط: حذف trigger غير موجود عند أول تطبيق). إعادة تطبيق 0010 ناجحة (idempotent). rollback ثم reapply ناجحان: بعد rollback لا دوال `challenge_live*` ولا جدول الطابور، وتبقى النسخ المحروسة من دوال 0009؛ بعد reapply تعود 15 دالة.
Flags: `live_matches_enabled=false` و`challenge_system_enabled=false` افتراضيًا بعد التطبيق.
الصلاحيات (بأدوار حقيقية): `anon` مرفوض على الجدول وعلى RPC، `authenticated` مرفوض على RPC، `service_role` مسموح، RLS مفعّل على `challenge_live_queue`.

## 3. Bugs SQL المكتشفة
**لا Bugs في `0010` ولا في كود المنتج.** كل الأعطال كانت في الاختبارات (النوع B) وأُصلحت:

| الملف | العطل | الإصلاح |
|---|---|---|
| `run_challenge_live.mjs` | فرع «أدبي» بلا محتوى → `challenge_touch_profile` (0009) يرفض `no_content` | إضافة مادة/وحدة/اختبار للفرع قبل إنشاء الملف |
| `run_challenge_live.mjs` | عدّ مباريات live يشمل مباراة ملغاة من اختبار سابق (مرتان) | استثناء `cancelled` |
| `run_challenge_live.mjs` | اختبار `invalid_option` على لاعب أجاب مسبقًا (الدالة تعيد حالة idempotent قبل التحقق من الخيار — تصميم سليم) | التحقق على اللاعب الذي لم يُجب |
| `run_challenge_live.mjs` | افتراض أن رفض `no_content` يحدث وقت المطابقة فقط؛ `join` يرفض فورًا عند غياب اختبار فعّال | السيناريو يغطي المسارين |
| `live_concurrency.mjs` | 20 لاعبًا بنفس الكؤوس لا يتطابقون كلهم بجولة واحدة (12/20) | إعادة جولات heartbeat (حتى 30) وطباعة العدد؛ **السلوك سليم** (انظر §12) |
| `live_concurrency.mjs` | لاعبان بقيا waiting بعد سيناريو cancel/match وأفسدا اللاحق | cancel تنظيفي |
| `live_concurrency.mjs` | `pg` يعيد bigint كنص | cast إلى int |
| `live_concurrency.mjs` | مقارنة deadline بـJS Date تقصّ الميكروثانية (فشل متقطع 1 من 7 تشغيلات) | المقارنة داخل PostgreSQL |

## 4. `tests/sql/run_challenge_live.mjs`: **34 / 34 ناجحة** (PGlite). تشمل: queue، range، cancel، duplicate، one-active، same questions، disabled test، answers، deadline، timeout، settlement once، XP once، anti-farming، caps، forfeit، expiry، rollback، RLS، EXPLAIN، وعزل النقاط المدفوعة.

## 5. `tests/sql/live_concurrency.mjs`: **10 / 10** على PostgreSQL 16.15 الحقيقي
أُضيفت 7 سيناريوهات للـ3 الأصلية. شُغِّلت **7 مرات متتالية من قاعدة نظيفة**: 6 نجحت مباشرة؛ واحدة فشلت بسبب عطل الاختبار أعلاه (دقة الوقت)، وبعد إصلاحه نجحت 6/6 (إضافةً لتشغيل التأكيد النهائي).
- **الاتصالات:** Pool بحد أقصى 40 اتصالًا مستقلًا؛ أعلى عدد backends مرصود ≈ 21.
- **Race Conditions المختبَرة:**
  1. 20 لاعبًا يطابَقون بالتزامن: كل لاعب في مباراة واحدة فقط، لا ربط بخصمين، الطابور consistent.
  2. نفس الزوج، 8 استدعاءات مطابقة متزامنة: مباراة واحدة و2 لاعبين و`match_id` واحد.
  3. cancel مقابل matchmaking: إما cancelled بلا مباراة أو matched بمباراة، لا Zombie ولا مباراة نصف منشأة.
  4. إجابتان متزامنتان + نفس `update_key` من اتصالين: صف إجابة واحد لكل لاعب/سؤال.
  5. التسوية: 6 استدعاءات متسابقة (إجابات/ticks/تكرار keys) لكل سؤال × 10 أسئلة: `match_completed` مرة واحدة، 20 صف إجابة بالضبط، XP=100 وكؤوس=8 وتحديات=1 للفائز، 0/0/1 للخاسر، ولا تتغير شيء بعد إعادة المحاولات و`leave` المتأخر.
  6. إجابة مقابل deadline (12 جولة × 3 ticker + اتصال إجابة): نتيجة حتمية بحسب وقت الخادم؛ صف واحد؛ الإجابة المقبولة دائمًا قبل الـdeadline؛ `question_resolved` مرة واحدة. نُفِّذت النتيجتان فعليًا (قُبلت/مهلة في تشغيلات: 7/5، 5/7، 6/6، 4/8، 8/4، 8/4، 9/3، 6/6).
  7. leave مقابل answer مقابل tick (10 جولات بترتيبين): حدث نهائي واحد، `abandoned`، لا XP لأحد، المنسحب لا يفوز.
  8. anti-farming: انظر §10.
- **Deadlocks: 0** (عدّاد `pg_stat_database` ضمن الاختبار، وسجل PostgreSQL بلا أي سطر deadlock).

## 6. حماية async ضد live
اختبار 30 في المجموعة الحية: دوال async لا ترى ولا تحرّك `kind='live'` وتاريخ live لا يظهر في سجل async. و`run_challenge_game.mjs` يعمل كاملًا مع 0010 (انظر §14). فلاتر `kind` في Python مغطاة باختبارات `test_challenge_live.py` (52 اختبارًا ناجحًا للتحديات).

## 7. Python Regression
بعد `pip install -r requirements.txt --break-system-packages --ignore-installed PyJWT` (**`requirements.txt` لم يُعدَّل**؛ كان لازمًا تجاوز PyJWT المثبَّت من debian):
- `python -m unittest discover -s tests`: **257 اختبارًا، OK: failures=0، errors=0، skipped=0** (في 3B كانت 7 أخطاء `aiogram/fitz` لعدم تثبيتها؛ الآن مثبَّتة فتعمل كلها).
- `unittest tests.test_challenge_content tests.test_challenge_repo tests.test_challenge_game tests.test_challenge_live`: **52 اختبارًا OK**.
- `tests/smoke_import_test.py`: `imported OK: 102 — ALL GOOD`.

## 8. عزل النقاط المدفوعة
فعليًا: `users.points` (القيم 1234 و4321) لم تتغير بعد 34 سيناريو ولا بعد مجموعة التزامن (فحص قبل/بعد). XP والكؤوس تُكتب في `challenge_profiles` فقط. لا اختبار على جداول الدفع الحقيقية (غير موجودة في البيئة).

## 9. Feature Flags
`live_matches_enabled` = false افتراضيًا (مُتحقَّق على PostgreSQL حقيقي)؛ لم يُشغَّل أي flag على Production؛ الاختبارات تشغّلها فقط داخل القواعد المؤقتة.

## 10. Anti-Farming (فعليًا)
- نفس الزوج 5 مرات (نافذة التكرار معطّلة): (XP، كؤوس، تحديات) بعد كل مباراة = (100,8,1) (200,16,2) (300,24,3) (300,24,4) (300,24,5). توقفت الكؤوس عند 24 = `3 × live_win_trophies` (`live_pair_daily_limit=3`)، وXP عند 300 = `daily_xp_limit`، والتحديات المسجّلة 5.
- نفس المحتوى داخل `repeat_content_seconds` (قواعد افتراضية): المباراة الثانية تكتمل بلا XP ولا كؤوس إضافية.
- لا مكافآت مكررة بسبب retry (ticks/إجابات/leave متأخرة بعد الاكتمال لا تغيّر شيئًا).

## 11. EXPLAIN / الأداء (استعلام peek الفعلي، من داخل `challenge_live_try_match` عبر auto_explain)
بيانات اصطناعية: 30 ألف waiting + 20 ألف صف cancelled/expired موزّعة على 3 وحدات (~10 آلاف/وحدة).
- الخطة: **Bitmap Heap Scan على `challenge_live_queue_pool`** (`Recheck Cond: unit_id = … AND trophies >= … AND trophies <= … AND status='waiting'`) ثم `Sort top-N heapsort` ثم `Limit 1`. لا `ORDER BY RANDOM`، لا scan كامل للجدول.
- الزمن: ≈ **31 ms** متوسطًا (≈75 ms أول تشغيل بـauto_explain) عند ~10 آلاف منتظر في الوحدة الواحدة؛ ≈ **1.16 ms** عند 300 منتظر.
- ملاحظة: استعلام مكافئ كتبته يدويًا بصيغة CTE اختار `challenge_live_queue_expiry` (غير ممثّل للدالة الفعلية)، فاعتمدت خطة الدالة نفسها. الكلفة تتناسب مع حجم نافذة ±500 كؤوس في الوحدة، وهي مقبولة للإطلاق؛ لم أضف فهارس. لا Load Test كبير.

## 12. Technical Debt المتبقي
1. **المطابقة تحت تزاحم كؤوس متطابقة:** كل المتصلين يلتقطون المرشح الأقرب نفسه؛ التقدّم مضمون والسلامة سليمة، لكن 20 لاعبًا متطابقين احتاجوا ≈ 8–9 جولات heartbeat متزامنة. لا يؤثر على الصحة؛ قابل للتحسين لاحقًا (مثلاً `FOR UPDATE SKIP LOCKED` في peek) إن ظهرت حاجة.
2. تكلفة peek ≈ 31 ms في الحالة القصوى (10 آلاف في وحدة واحدة) — راقبها عند الإطلاق.
3. **تداخل السقف اليومي async↔live** كما في HANDOFF_3B: async لا يرى مكافآت live عند حساب سقفه؛ لم يُختبر ولم يُغيَّر.
4. اختبارات SQL تعمل على بدائل (`feature_flags`, `users`) لا على Schema الإنتاج.

## 13. ما لم يُختبر
- أي تشغيل على **Supabase** (فرع اختبار أو `Test_telegramquizbot`): PostgREST، صلاحيات المشروع، مخطط 0001–0007 الحقيقي.
- تزامن trigger `challenge_block_async_while_live` مع بدء هجوم async بالتوازي مع الدخول للطابور (اختُبرت منطقيًا فقط).
- حمل كبير/تحمّل زمني، أخطاء الشبكة بين Python وSupabase، Telegram UI.
- تداخل السقوف async↔live (§12.3).

## 14. SQL regression للمراحل السابقة
- `tests/sql/run_challenge_game.mjs` (0008+0009): **31/31** بدون 0010. ومع 0010 مطبّقة فوق 0009 (نسخة مؤقتة من السكربت تضيف 0010 وrollbackها): **31/31**. كلاهما على PGlite.
- `tests/sql/challenge_content_test.sql` (0008) على PostgreSQL 16 الحقيقي: **rc=0**.

## 15. Git
تغييرات هذه المرحلة: `tests/sql/run_challenge_live.mjs` و`tests/sql/live_concurrency.mjs` و`docs/challenges/HANDOFF_3B5.md` فقط. لا push (لا remote قابل للكتابة). لا merge إلى master.

## 16. كيف تُعاد التجربة
```
# PostgreSQL حقيقي مؤقت (ليس إنتاجًا) + أدوار anon/authenticated/service_role + جدولا feature_flags وusers البديلان
psql -d TEST -f db/migrations/0008_challenge_content.sql -f ...0009_challenge_game.sql -f ...0010_challenge_live.sql
DATABASE_URL=postgres://.../TEST node tests/sql/live_concurrency.mjs /path/to/repo      # pg
node tests/sql/run_challenge_live.mjs /path/to/repo                                      # @electric-sql/pglite@0.3.10
```

## 17. هل Live Backend جاهز لبناء Telegram UI في 3C؟
**نعم، بشرط واحد صريح.** الأدلة: 0010 تعمل على PostgreSQL 16 حقيقي من أول مرة وتُعاد وتُرجَع وتُعاد؛ 34/34؛ 10/10 بـ~21 اتصالًا حقيقيًا بلا Deadlock ولا مباراة/إجابة/تسوية/XP/كؤوس مكررة؛ async لم ينكسر (31/31)؛ Python 257 OK؛ الأعلام مطفأة؛ النقاط المدفوعة لم تتغير. **الشرط:** لم يُجرَّب على Supabase الحقيقي ولا على مخطط 0001–0007 الفعلي، لذا يجب **قبل أي تشغيل لمستخدمين** تطبيق 0008→0010 على قاعدة Supabase اختبار (بإذن المالك) وتكرار `live_concurrency.mjs` عليها. لا شيء يمنع بدء تصميم/بناء واجهة 3C فوق الـAPIs الحالية بالتوازي.
