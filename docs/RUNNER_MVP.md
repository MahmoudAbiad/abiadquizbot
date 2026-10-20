# Educational runner — test only

Implemented in branch `test`, based on `0082c005786e58320f29645073c0353138a8aded`.
The game is a lightweight Canvas 2D three-lane runner with perspective drawing,
original character art, obstacles, coins, jumps and police pursuit. Canvas was
chosen to avoid adding a game-engine download for this small scene. Objects are
bounded, DPR is capped at 2 and physics uses delta time. No frames reach the server.

## Persistent coins and permanent jetpack (2026-10-20)

Runner currency is now saved per Telegram player, separate from knowledge/bot points. The store sells a permanent unlimited-flight jetpack for 100 coins. See [RUNNER_STORE.md](RUNNER_STORE.md) for schema, retry/concurrency behavior, and verification.

## Test activation and identity (performed on 2026-10-20)

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

Game choices use two deterministically selected original wrong indices per
(user, quiz, question), plus the original correct index, shuffled per round. Private round snapshots retain their original indices. Public
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
Rounds expire after two hours. The independent Runner retention loop runs in both
webhook and polling modes, and deletes bounded batches seven days after
`coalesce(finished_at, expires_at)`. It is gated by the unchanged `enabled()` checks.

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

## V2 image cache, prewarming and retention

`/start` spawns prewarming immediately after inserting the round and does not await
rendering. Every image question and every original option is prepared in round
order. The `(quiz_id, question_key, part)` cache is shared between students; hits
require `expires_at > now()`. Storage upload already uses upsert to the stable
`runner/{quiz}/{question-key}/{part}.png` path. Cache lifetime is fixed from render,
not extended on reads.

In-flight tasks deduplicate within one process. Renewable, owner-checked Redis
locks (120s image / 900s prewarm) coordinate processes. Waiters poll the cache every
300ms up to 15 seconds; `/question` gathers four images with a 25-second bound.
Timed-out waiters cannot cancel shared renders. Prewarm yields before new work
when a player waits; its global remote cap reserves one connection when the
remote limit exceeds one. Local prewarm is globally serial; direct local/fallback work
shares a two-slot gate within the renderer's existing three-slot ceiling.

A configured Worker always gets remote-only prewarm, including while its circuit
is open: no local spillover. Failed images get three retries with 30-second
backoff. One failure does not abort the remaining quiz. On-demand requests retain
the existing worker/local fallback policy. `_game_part` uses the same rendering
implementation on the worker and locally. The worker API is unchanged. Worker lifespan now warms
its two child renderers before advertising readiness (`WORKER_WARMUP=1`), avoiding
first-request Matplotlib/font initialization on a newly deployed worker.

The `0018` migration adds cache creation/expiry, retention indexes, and a private
RLS-protected `runner_image_gc` deletion queue. It was applied and independently
verified on test project `xhlksusmczccqyvbxylo` on 2026-10-20. The queue trigger
survives quiz cascades; the handler attempts immediate prefix cleanup and the loop
retries failures. Storage objects are removed before cache rows; failed removal
retains rows. Cleanup acquires the same image lock and rechecks expiry, preventing
it from deleting a concurrently refreshed file. Work is bounded to 200 images,
500 rounds, 10 orphan prefixes per cycle. Prefix deletion is also bounded to 200
files per call and only applies to deleted quizzes.

Environment controls (also in `.env.example`):

| Variable | Default |
|---|---|
| RUNNER_IMAGE_CACHE_DAYS | 7 |
| RUNNER_PREWARM_ENABLED | 1 |
| RUNNER_PREWARM_CONCURRENCY_REMOTE | 4, capped by offload concurrency |
| RUNNER_PREWARM_CONCURRENCY_LOCAL | 1 (hard ceiling 1 on 512MB) |
| RUNNER_ROUND_RETENTION_DAYS | 7 |
| RUNNER_CLEANUP_INTERVAL_SECONDS | 3600 |
| RUNNER_SIGNING_SECRET | optional; existing bot-token fallback |

## Answer gates

RUN keeps the existing 8-second travel segment. APPROACH slows to 0.35 speed over
1.5 seconds while image loading runs; empty gate frames approach from the horizon.
DECIDE starts only after image decode, foreground/resume checks and `/ready`;
buttons use the Canvas projection at z=0.62. The player swipes or taps a lane,
then confirms. RESOLVE moves toward the gates for 0.55 seconds: the correct gate
opens/glows, wrong answers show impact and the correct path, timeout is explicit.
The existing feedback/explanation follows. Scoring/lives remain server-controlled.

Long options (>60 characters) and tall math images automatically use the original
bottom choices. Reduced-motion users skip easing/particles/shake. Coins disappear
on collection with a 220ms effect; collided barriers disappear, jumped barriers
remain. Pure rules live in `world.js`; objects/effects stay bounded.

## Verification on 2026-10-20

- Runner T1/T3/T4 baseline: 53 Python tests passed; the final UI suite has 18 passing scenarios.
- Final combined suite: 148 pytest +26 subtests passed, including 18 cache/retention
  cases, 6 real process/readiness cases and the HTTP/2 logging privacy regression.
- 112 existing standalone unittests passed: offload (45), prompt contracts (8),
  favorites (11), result UI (11), navigation (19), mistake practice (18).
- PGlite ran actual migrations 0017/0018: RLS/grants, CAS, constraints, cache expiry,
  five indexes and quiz-deletion tombstone/cascade passed.
- Chromium at 390×844 passed RUN/APPROACH/DECIDE/RESOLVE, correct/wrong answers,
  timeout, classic fallback, reduced motion and persisted sound settings. No
  JavaScript errors, horizontal overflow or question/gate overlap. Screens inspected.
- Supabase identity/schema verified live. Real API smoke passed: /start 0.64s,
  first question 3.75s, a distinct second student's cached question 0.594s,
  unchanged created_at on all five image parts, real expiry regeneration,
  idempotent ready/answer, correct/wrong/timeout and real cascade/Storage cleanup.
  Another start took 1.219s, so a strict sub-second network SLA is not guaranteed.
- Chromium against the actual Render service and Supabase passed with a signed
  test session (only the Telegram bridge was injected): 10 coins, 3 gates,
  decoded math PNGs, a correct answer and 100 points, zero JS errors/overflow.
- Final runtime commit `5dbff1b9a168bc9702066daea2c8b9198c58e1b7` is live on
  main (`dep-dbbram8u01pc73bonqa0`) and worker (`dep-dbbram8u01pc73bonq50`).
  The real browser flow was repeated after that deploy and passed. An authenticated
  direct worker PNG rendered in 2.172s; both /health endpoints returned ok.
  Sampled main memory ~323MiB,
  worker ~211MiB, within 512MiB; this is not a sustained load benchmark.
- Real testing exposed cold child-process/font-cache initialization. Worker now
  warms its two child renderers before readiness. Platform sleep/network outages
  can still require the existing retry path; no local background fallback is used.
- HTTP/2 hpack DEBUG exposed the test API key in a local diagnostic run. hpack and
  httpcore debug logging are now disabled and locally persisted logs were redacted.
  The logging regression encodes a fake API key and verifies it is not emitted.
  No key was committed. Rotation of the exposed test key is recommended and has
  not been performed. A physical phone inside Telegram remains untested.

A further live retention probe uploaded an expired PNG and inserted old/recent
rounds. `cleanup_once` removed the file and row, removed the old round, and kept
the recent round. The disposable browser quizzes were then deleted, their Storage
prefixes emptied and GC tombstones acknowledged. Runtime logs contained no errors
in the final observation window; one renderer warning extended its canvas by 16px
automatically. The mobile UI had no overflow.

Reproducible commands:

```bash
PYTHONPATH=.:tests python -m pytest -q tests/test_runner.py tests/test_runner_api.py tests/test_runner_images.py tests/test_generation_regressions.py tests/test_ready_exam_context.py tests/test_image_quiz_layout.py tests/test_ai_providers.py tests/test_render_processes.py tests/test_logging_privacy.py
PYTHONPATH=.:tests python tests/test_offload.py -q
PYTHONPATH=.:tests python -m pytest -q tests/test_render_processes.py
node --check webapp/runner/game.js
node --check webapp/runner/audio.js
node tests/runner_ui_logic.mjs
node tests/runner_world.mjs
node tests/sql/runner_test.mjs
node tests/runner_browser.cjs
```

SQL tests require PGlite; browser tests require Playwright/Chromium in the test
harness only. No runtime frontend/backend dependency was added.
