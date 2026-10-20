# Educational runner — test only

Implemented in branch `test`, based on `0082c005786e58320f29645073c0353138a8aded`.
The game is a lightweight Canvas 2D three-lane runner with perspective drawing,
original character art, obstacles, coins, jumps and police pursuit. Canvas was
chosen to avoid adding a game-engine download for this small scene. Objects are
bounded, DPR is capped at 2 and physics uses delta time. No frames reach the server.

## Activation (not performed during implementation)

1. Confirm the **test** Render service repository/branch and its `SUPABASE_URL`.
2. Apply `db/migrations/0017_educational_runner.sql` only to that confirmed test DB.
3. Ensure the existing `quiz-images` bucket works with the server service role.
4. Set `RUNNER_ENABLED=1`, `RUNNER_TEST_SUPABASE_REF=<confirmed-test-project-ref>`.
   Render must expose `RENDER_GIT_BRANCH=test` (or local `APP_BRANCH=test`).
5. Keep `WEBAPP_PUBLIC_BASE_URL` on the test HTTPS service and use the **test bot**.
6. Deploy bot and existing render worker from the same test commit. No production
   deploy or production migration is needed or authorized.
7. Open an eligible quiz result or favorite, or a shared link. Tap the runner
   button, then Start. Confirm on a real phone before calling the feature live.

The feature fails closed unless all three environment checks match. Missing
migration does not affect existing quizzes while the game is disabled. AI timing
schema changes are backward compatible and require no new DB columns.

## Model and storage

The original generation request asks for `estimated_time_seconds` (10–300).
Malformed/missing values use a reading-length/math-aware fallback; the original
AI estimate is separately serialized as `ai_estimated_time_seconds` (null when
fallback was used). Existing Streaming chunks use the same schema/runtime.
No options are removed from generated or central quizzes.

Game choices are two random original wrong indices plus the original correct
index, shuffled. Private round snapshots retain their original indices. Public
question responses expose only those three choices, never the answer key.

`runner_settings` stores lives (1–5), a uniform override, per-content manual
overrides and separate AI suggestions. Only owner/admin may edit. Manual edits
use optimistic versions; AI writes touch only AI columns. Legacy quizzes can
use deterministic fallback immediately, manual settings, or one successful AI
batch. A failed batch can be retried. A process killed during AI initialization
leaves `ai_status=running`; after checking no request remains, an operator may
reset that row to `failed`. Automatic recovery is not implemented in this MVP.

Content hashes exclude image/timing metadata and include question/options/key,
tables and matrices. Edits invalidate relevant game settings/images naturally.
`runner_images` stores `game_image_url`, separate from Telegram image URLs.
The renderer projects a copy with no options, supports tables/RTL/LaTeX/matrices,
and uses the existing bounded renderer/offload worker. Three option images are
rendered separately for math. No game operation overwrites `quiz_data`.
Legacy text quizzes without UUID are centralized on launch under the user's lock.

`runner_rounds` stores private snapshots and server-computed knowledge scores.
Answers use versioned compare-and-set and retries return the committed result.
Rounds expire after two hours. Old round snapshots currently have no scheduled
retention cleanup: configure retention before scaling usage (e.g. 30 days).

## Access and scoring

All endpoints require verified Telegram initData and a shared Redis rate limit.
Access requires owner/admin, a user-owned favorite, or a short-lived user/quiz-bound
signed launch capability issued after an authorized bot flow. Tables have RLS
and no anon/authenticated privileges; only the server service role uses them.
No Supabase keys are sent to the Mini App.

Knowledge points (100 per correct answer) are computed on the server, including
timeout and chances. Coins, distance, obstacle collisions and animation are local
and cosmetic, not monetized or used as trusted leaderboard/reward values. Two
obstacle collisions end the local run and mark its server record abandoned.
This is not a fully server-simulated anti-cheat engine. Server score submission
accepts no numeric score from the browser.

## Question timer protocol (`/ready`)

`/question` retrieves the current question without starting its timer: it responds with
`started:false` and the full `remaining_seconds` when `issued_at` is null.
On a reload after the timer has started it responds with `started:true` and the
actual remaining time.

The Mini App builds the question while its question/choice panels are hidden.
All essential question and answer images must **successfully decode** before
`POST /api/runner/ready` can be sent. Image decoding has an 8-second bound:
failures and timeouts show a retry message without activating the timer.
If the tab is hidden, a pagehide event occurred, or the game is paused, the
client awaits a foreground return and explicit Resume before calling `/ready`.
Stale in-flight loads are discarded when a new load supersedes them.

The server uses compare-and-set on `(round version, issued_at IS NULL)` so
`/ready` is idempotent and never resets a previously started timer.
The client deadline is computed from the moment it sent `/ready` plus the
returned remaining time, so network delays are not added back as extra time.
If the app is hidden **after** `/ready` was sent, the server may already have
started the timer; the question stays hidden until the user resumes, but
elapsed server time is not paused or refunded. The client cannot perfectly
guarantee visibility over an in-flight network request.

`/answer` still rejects a question whose timer was not started. Knowledge
scoring, grace period and lives are unchanged. No database migration needed.

Accepted anti-cheat tradeoff: question text and image URLs arrive before
`/ready`, so a technical user could read them while the timer is inactive.
Text is already available from `/config`; reassess this design if rewards
or leaderboard rankings become associated with the game.

## Sound

Two original 12.8-second MP3 loops and seven SFX (~180 KiB together), generated by
`scripts/build_runner_audio.py`, CC0 license next to assets. Audio starts only after
Start, crossfades over 650 ms, has independent persistent music/SFX switches, is
suspended while hidden, and stops/disconnects on completion or page exit.
If audio loading fails the game remains playable. No external audio CDN.

## Verification

- 92 pytest tests passed (+26 subtests): new runner/core/API tests, generation,
  context chunking, image layout, AI provider regressions.
- 112 existing standalone unittest tests passed: prompt contracts (8), offload
  (45), favorites (11), result UI (11), navigation (19), mistake practice (18).
- PGlite migration test passed: actual SQL, grants/RLS, constraints, optimistic
  conflict, preservation of original quiz options and AI timing, cascade deletion.
- JavaScript syntax checks passed; actual math PNG rendered and inspected.
- Chromium at 390×844 passed a mocked-API playable flow: start, run, question,
  correct answer, next question, wrong answer, police catch and persisted music
  toggle; no JavaScript errors or horizontal overflow. Screens were visually
  inspected. This does not substitute for live Telegram/device performance testing.
- The existing two-process renderer test is blocked by this host's socket
  restrictions (PermissionError); the other four tests in its file passed.
- Live test Supabase/Telegram/Render verification remains blocked: connected
  Supabase accounts show SkyFocus and an inactive unrelated project, not Abiad test.
  Render workspace selection also needs confirmation.

Reproducible commands:

```bash
PYTHONPATH=.:tests python -m pytest -q tests/test_runner.py tests/test_runner_api.py tests/test_generation_regressions.py tests/test_ready_exam_context.py tests/test_image_quiz_layout.py tests/test_ai_providers.py
PYTHONPATH=.:tests python tests/test_offload.py -q
node --check webapp/runner/game.js
node --check webapp/runner/audio.js
node tests/sql/runner_test.mjs
```
