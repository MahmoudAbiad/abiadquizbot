# Runner integration and later production activation

Integration keeps Runner off on Heroku. The live app is `abiad-quiz-maker-bot`, auto-deploying `master`, using Supabase project `pqawoanhbgvdtbdhnbod` and `https://bot.abiad.me`.

No Runner schema or environment activation is performed by merging this PR. Startup, cleanup and new game admission remain closed unless all environment checks pass. The admin flag alone cannot bypass the deployment guard.

Before activating: confirm a restorable backup; audit existing schema; apply the four Runner forward SQL files in dependency order (0017, 0018, wallet, knowledge_car), never rollback files; verify RLS, RPC grants, bucket access and real PostgreSQL concurrency. Protect feature_flags as described in db/review/runner_feature_flag_hardening.sql. The script is repeat-safe for the flag seed and preserves other flags.

After those checks, explicitly configure APP_BRANCH=master, RUNNER_PRODUCTION_SUPABASE_REF=pqawoanhbgvdtbdhnbod, RUNNER_PRODUCTION_READY=1 and RUNNER_ENABLED=1. Leave runner_enabled=false in feature_flags until the admin intentionally enables it. No environment variables are changed by the code or migration files themselves.

Use the private admin feature-control panel to disable new rounds while existing owned, unexpired rounds finish. Revoking the environment guard instead is a full shutdown and also blocks round continuation. An active admission request can finish after an off-toggle; existing shared caches take up to 30 seconds normally (up to roughly 60 seconds if Redis publication fails).

To revert this integration, revert the merge commit through a normal reviewed PR. It does not require rolling back database schema because this integration does not install production Runner tables. Never delete historical rounds or wallet data to roll back a later activation.
