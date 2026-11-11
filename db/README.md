# `db/` — دليل هيكل قاعدة البيانات والترحيلات (Migrations)

يحتوي هذا المجلد على المخطط الكامل لقاعدة البيانات (PostgreSQL / Supabase)، وجميع الترحيلات التراكمية (Migrations)، والعمليات التشغيلية، وسكربتات مراجعة الصلاحيات.

## 1. خريطة المجلدات

| المسار | الوصف |
|---|---|
| `db/migrations/` | الترحيلات المعتمدة لبناء وتحديث قاعدة البيانات (تُطبَّق بالترتيب الموضّح أدناه). |
| `db/operations/` | إجراءات تشغيلية ذرية تُنفَّذ لمرة واحدة على الإنتاج (مثل `renewal_2000_transition.sql`). |
| `db/review/` | سكربتات مراجعة وتقوية صلاحيات الجداول والمفاتيح (`runner_feature_flag_hardening.sql`). |
| `db/archive/` | ملفات `migration_*.sql` التاريخية القديمة التي دُمجت بالكامل داخل `0001_baseline.sql` (محفوظة للمرجع التاريخي فقط ولا تُشغَّل). |

## 2. جدول الترحيلات الكامل (`db/migrations/`)

تُصنَّف الترحيلات إلى سلسلتين متتابعتين: السلسلة المرقّمة (`0001` إلى `0018`) تليها السلسلة المؤرخة بالتوقيت (`YYYYMMDDHHMMSS_*`).

⚠️ **تنبيه مهم:** الملفات المنتهية بـ `*_rollback.sql` هي ملفات تراجع طارئة فقط؛ **لا تشغّلها أبداً** أثناء البناء أو التحديث التصاعدي.

### أولاً: التأسيس والكويز الجماعي والمدفوعات (`0001` – `0007`)

| الملف | الوظيفة والمحتوى |
|---|---|
| `0001_baseline.sql` | المخطط التأسيسي الكامل (23 جدولاً، 2 Views، 17 دالة RPC، الفهارس، القيود، وإعدادات RLS). |
| `0002_seed_config.sql` | البيانات المرجعية الأولية (`app_settings`, `feature_flags`, `ai_model_slots`, `ai_model_pricing`) دون بيانات مستخدمين. |
| `0003_group_quiz_burst_pacing.sql` | إضافة وضع `burst` (إرسال كل الأسئلة دفعة واحدة) إلى قيد `pacing_mode` في جدول `group_quiz_sessions`. |
| `0004_group_quiz_top_n.sql` | إضافة عمود `top_n` (1–20، الافتراضي 20) لتحديد عدد الأوائل في `group_quiz_sessions`. |
| `0005_ai_request_timeout_setting.sql` | إضافة إعداد `ai_request_timeout` (الافتراضي 120 ثانية) إلى جدول `app_settings`. |
| `0006_payments.sql` | نظام الشحن الذاتي: جداول `payment_packages`, `payment_orders`, `payment_events`, `payment_receipts` + دالة `payment_credit_order` + مفتاح `payments_enabled`. |
| `0007_payment_credit_hardening.sql` | تقوية دالة `payment_credit_order` بمعامل `p_require_match` للشحن التلقائي الآمن ورفض إيصالات `dismissed/baseline`. |

### ثانياً: نظام التحديات ولعبة Runner (`0008` – `0018`)

| الملف | الوظيفة والمحتوى | ملف التراجع (للطوارئ فقط) |
|---|---|---|
| `0008_challenge_content.sql` | بنك محتوى التحديات (`challenge_branches`, `subjects`, `units`, `tests`, `questions`) + دالة الاستيراد الذري + `challenge_playable_units` + مفتاح `challenge_system_enabled`. | `0008_challenge_content_rollback.sql` |
| `0009_challenge_game.sql` | نظام الأراضي والأبراج والهجمات غير المتزامنة (`challenge_profiles`, `towers`, `matches`, `match_players`, `match_answers`, `notifications`) ودوال التسوية الذرية. | `0009_challenge_game_rollback.sql` |
| `0010_challenge_live.sql` | المواجهات المباشرة الحية: طابور البحث `challenge_live_queue`، دوال المطابقة، الإجابة، والتسوية الذرية للمباريات الحية. | `0010_challenge_live_rollback.sql` |
| `0011_challenge_cooldown_admin.sql` | إعداد مهلة إعادة مهاجمة نفس الأرض (`challenge_target_cooldown_seconds`) وتصفير المهل من لوحة الأدمن. | — |
| `0012_challenge_daily_attack_limit.sql` | إعداد الحد اليومي لهجمات الأرض لكل طالب (`challenge_daily_attack_limit`). | — |
| `0013_challenge_attack_notification_cooldown.sql` | إعداد مهلة تكرار إشعار «تتعرض أرضك للهجوم» (`challenge_attack_notification_cooldown_seconds`). | — |
| `0014_challenge_completion_notification_cooldown.sql` | إعداد مهلة تكرار إشعار «نتيجة الدفاع» (`challenge_completion_notification_cooldown_seconds`). | — |
| `0015_challenge_spectator.sql` | وضع المشاهدة الحية للمدافع: دالتا القراءة الآمنة `challenge_spectator_state` و`challenge_spectator_incoming` وفهرس الهجمات الجارية. | `0015_challenge_spectator_rollback.sql` |
| `0016_challenge_branch_archive.sql` | دالة الحذف النهائي الآمن للفروع المؤرشفة غير المرتبطة بمباريات (`challenge_delete_archived_branch`). | `0016_challenge_branch_archive_rollback.sql` |
| `0017_educational_runner.sql` | الجداول الأساسية للعبة «رحلة أبيض» التعليمية (`runner_settings`, `runner_images`, `runner_rounds`). | — |
| `0018_runner_image_cache.sql` | كاش صور لعبة Runner لمدة 7 أيام، فهارس الاحتفاظ، وجدول ودالة تنظيف الصور اليتيمة (`runner_image_gc`). | `0018_runner_image_cache_rollback.sql` |

### ثالثاً: الترحيلات المؤرخة (`20261008` – `20261108`)

| الملف | الوظيفة والمحتوى |
|---|---|
| `20261008141907_gift_wallet_codes.sql` | نظام المحفظة الذرية وأكواد الهدايا: جداول `gift_codes`, `point_gifts`, `gift_code_redemptions`, `wallet_debits` ودوال الخصم والاسترجاع واسترداد الهدايا. |
| `20261017220038_ai_provider_registry.sql` | سجل مزوّدي الذكاء الاصطناعي الديناميكي (`ai_providers`) ودوال إدارة المزوّدين والنماذج. |
| `20261017223736_workers_ai_model_names.sql` | توسيع نمط أسماء النماذج المسموحة ليدعم معرفات Cloudflare Workers AI وHuggingFace (`@cf/...`, `@hf/...`). |
| `20261018084726_gift_admin_campaigns.sql` | أرشفة وسحب هدايا الأكواد، وحملات الإهداء الجماعية المباشرة (`gift_campaigns`, `gift_campaign_recipients`) ودوالها الذرية. |
| `20261018093843_wallet_debit_numeric_arithmetic.sql` | ضبط الدقة الحسابية (`::numeric`) في دالة `wallet_debit` لمنع أخطاء الفاصلة العائمة عند الحدود الصفرية. |
| `20261020191247_runner_wallet.sql` | محفظة العملات وحقيبة الطيران الدائمة في لعبة Runner (`runner_wallets`, `runner_tracks`, `runner_buy_jetpack`). |
| `20261020193546_runner_knowledge_car.sql` | رصيد المعرفة، إتقان الأسئلة (`runner_mastery`)، الهدف اليومي (`runner_daily_study`)، وسيارة تيسلا بـ 3 مستويات في لعبة Runner. |
| `20261026235246_daily_renewal_notifications.sql` | نظام التجديد اليومي التلقائي عبر `pg_cron` وطابور إشعارات التجديد (`daily_renewal_config`, `daily_renewal_runs`, `daily_renewal_deliveries`). |
| `20261027001007_daily_renewal_delivery_revision.sql` | إضافة عداد المراجعة `bot_delivery_revision` لمنع تعارض تحديثات حالة حظر/إلغاء حظر البوت المتزامنة. |
| `20261027110638_group_quiz_chat_lock_journal.sql` | دفتر يومية أقفال المجموعات (`group_quiz_chat_locks`) وعمود `lock_chat` لاستعادة صلاحيات المجموعة بأمان. |
| `20261101034215_legal_consent_v1.sql` | نظام الموافقات القانونية: جداول `legal_documents`, `user_consents`, `user_consent_events` ودوال التحقق والتسجيل. |
| `20261101034802_legal_consent_admin_controls.sql` | دالة `bump_legal_document_version` لرفع إصدار شروط الاستخدام أو سياسة الخصوصية ذرياً من لوحة الأدمن. |
| `20261101040113_user_delete_cascade_cleanup.sql` | تحديث قيود المفاتيح الأجنبية (`ON DELETE CASCADE`) لضمان تنظيف الجلسات والاستجابات المرتبطة عند حذف حساب مستخدم. |
| `20261105120000_group_quiz_certificates.sql` | إضافة `cert_config` إلى `group_quiz_sessions` و`last_name` إلى `group_quiz_participants` لميزة شهادات التقدير. |
| `20261106120000_group_quiz_ceremony.sql` | إضافة `ceremony_config` إلى `group_quiz_sessions` لميزة حفل التكريم المصوّر/الفيديو. |
| `20261108120000_export_profiles.sql` | جدول `export_profiles` لحفظ إعدادات التصدير الدائمة لكل أستاذ/مستخدم. |

## 3. الإجراءات التشغيلية وسكربتات المراجعة

- **`db/operations/renewal_2000_transition.sql`:** يُشغَّل بعد تطبيق ترحيلات التجديد اليومي لضبط دورة التجديد اليومي عند الساعة **20:00 بتوقيت دمشق** عبر `daily_renewal_cycle_day()`.
- **`db/review/runner_feature_flag_hardening.sql`:** يضبط مفتاح `runner_enabled` (مطفأ افتراضياً) ويقوّي حماية جدول `feature_flags` (تفعيل RLS وحصر التعديل بـ `service_role`).

## 4. خطوات بناء قاعدة بيانات جديدة بالكامل

1. أنشئ مشروع Supabase جديداً (PostgreSQL 15+).
2. شغّل `db/migrations/0001_baseline.sql` ثم `db/migrations/0002_seed_config.sql`.
3. شغّل بقية الترحيلات التصاعدية من `0003` إلى `0018` (باستثناء أي ملف ينتهي بـ `_rollback.sql`).
4. شغّل الترحيلات المؤرخة من `20261008141907_gift_wallet_codes.sql` حتى `20261108120000_export_profiles.sql` بالترتيب الزمني لأسمائها.
5. شغّل `db/operations/renewal_2000_transition.sql` و`db/review/runner_feature_flag_hardening.sql`.
6. اضبط `SUPABASE_URL` و`SUPABASE_KEY` (مفتاح `service_role`) في ملف `.env`.

## 5. اختبارات SQL المحلية (`tests/sql/`)

يحتوي المشروع على حزمة اختبارات SQL معزولة تعمل عبر محرك PostgreSQL داخل الذاكرة (`@electric-sql/pglite`) دون الحاجة لاتصال بقاعدة سحابية:

```bash
npm ci --prefix tests/sql --ignore-scripts
node tests/sql/gift_wallet_test.mjs
node tests/sql/gift_admin_test.mjs
node tests/sql/ai_providers_test.mjs
node tests/sql/daily_renewal_test.mjs
node tests/sql/renewal_2000_test.mjs
node tests/sql/group_quiz_lock_test.mjs
node tests/sql/runner_test.mjs
node tests/sql/runner_wallet_test.mjs
node tests/sql/runner_knowledge_test.mjs
node tests/sql/run_challenge_game.mjs
node tests/sql/run_challenge_live.mjs
node tests/sql/run_challenge_spectator.mjs
node tests/sql/run_challenge_branch_archive.mjs
```
