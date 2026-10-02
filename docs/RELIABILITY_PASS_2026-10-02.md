# Reliability pass — 2026-10-02

Base: `master` at `3bfd970089945700266823ddfc2d73cbc9525b7f`.
Checkpoint: `backup-master-after-first-fixes-2026-10-02` (also created on GitHub).
The old checkpoint and `main` were not changed. No deployment or production configuration changes.

## Upload and resource behavior

- Web image batches retain 50 images / 15 MiB per image, with a new 100 MiB total.
  This matches the existing web document budget and supports many small images.
  Browser, init, actual R2 sizes at complete, and cumulative bounded local download all enforce it.
- Document allowlist: PDF, DOCX, PPTX, UTF-8 TXT. Legacy DOC/PPT were advertised
  by the browser but are not supported by the actual parsers; selectors now match those parsers.
- Image allowlist: JPEG, PNG, WebP, static GIF/BMP/TIFF. Pillow verifies structure
  and rejects images over 25 million pixels or a 16,000-pixel dimension, including animated images.
- Audio extension allowlist covers MP3/M4A/AAC/WAV/OGG/OGA/Opus/FLAC/AIFF/WMA/WebM/MP4/3GP/AMR.
  The existing real-duration probe remains authoritative; unprobeable web audio is rejected.
- Existing document/audio limits (20 MiB Telegram, 100 MiB / 150 pages web PDF,
  250 MiB / 240 minutes audio) remain unchanged.
- R2 downloads stream in 64 KiB blocks and enforce actual transferred size,
  ContentLength consistency, free disk headroom, and partial-output cleanup.
- Super PDF reserves 300 MiB for its three parts plus 64 MiB free disk headroom,
  in addition to its already-present source (up to 100 MiB). Writes are bounded.
  Split failures/cancellation clean all parts, including partially written parts.
  Existing memory release and final cleanup are retained.
- Startup no longer wipes files referenced by live Redis FSM sessions. Only local
  files older than 48 hours are eligible for periodic/startup cleanup (FSM expires at 24 hours).

## Idempotency, locks, and webhook delivery

- Complete claims are atomic Redis SET NX with 24-hour TTL. Claims precede object
  lookup so retries after R2 deletion do not relaunch work. A failure before task
  creation releases the claim using its ownership token.
- Image identity is its upload session, rather than ordered object paths. Duplicate
  paths and mixed sessions are rejected. File and audio endpoints only accept
  their flat UUID object paths, preventing cross-endpoint claims of image objects.
- Per-user correctness locks use SET NX, owner-checked Lua release/renewal,
  10-minute leases, renewal every 200 seconds, and cancellation if ownership is lost.
  Intake, transcription, cancellation, and generation share the same lock.
  Album collection gets a short wait; its 1.5-second collection delay occurs before locking.
- CPU/RAM semaphores remain process-local: 2 heavy generations, 2 web preparations,
  and 15 Telegram workers per process. They are resource throttles, not distributed correctness locks.
- Telegram updates are atomically enqueued before HTTP 200. Redis failures return
  503, allowing Telegram retry. Queued updates have 120-second renewable leases.
  Interrupted leases are reclaimed; completion and retries require the owner token.
- Duplicate update IDs are ignored while queued/in flight and for seven days after
  completion. Processing retries use bounded backoff and a maximum of five attempts.
  Poison records go to a capped 1,000-record Redis dead list with seven-day expiry.
- Webhook setup now uses `drop_pending_updates=False`: clearing pending updates
  on routine startup conflicts with durability. Polling setup in main.py is unchanged.
- Error middleware propagates failures to the queue; failed processing no longer
  leaves a throttle entry that would suppress the retry as spam.

## Audio / admin audit follow-up

- The existing usage_events JSON stores input_channel separately from content_source:
  `telegram` versus `web_upload`. Documents, images, direct text, cached quizzes,
  audio transcription/summary, and quizzes made from transcripts carry this provenance.
- Audio transcription and summary events appear in the existing quiz-generation
  admin log with actual model/provider, token metadata when present, duration,
  partial-result indicator, and input channel. Generic text generation now exports
  ContextVar metadata without changing its return API.
- Historical provenance is displayed as unrecorded; it is not guessed.
- Audio full/partial refunds track the remaining debit, preventing a second refund
  after an error message fails. A saved successful quiz is not refunded because
  secondary analytics/referral delivery fails.

## Verification and limitations

Run: `python -m compileall -q .` and
`python -m unittest discover -s tests -p 'test_*.py' -v`.
CI uses Python 3.11 and a dedicated local Redis 7 service, without production secrets.
Locally the same Lua scripts run through system Lua 5.4 against a deterministic
Redis-command double. Tests isolate production functions with injected SDK dependencies;
they do not substitute production logic with copied implementations.

The existing smoke importer and Gemini equivalence harness are preserved. Full SDK
import/real service integration must also be checked in a dependency-complete environment.

Remaining architectural limits:

1. A hard process crash after financial deduction still needs a durable financial
   operation ledger/idempotent financial model. No such ledger or database migration was added.
2. Delivery is at least once. A crash after a downstream side effect but before queue
   completion can replay the update; queue dedup does not make every side effect exactly once.
3. Redis enqueue durability depends on the provider's persistence/eviction/failover
   guarantees. None of those production settings were accessed or changed.
4. Accepted web-upload background tasks are not a durable job queue. Hard crashes
   after task scheduling require a new upload; duplicate complete claims are retained
   for 24 hours once work starts to prevent duplicate charge/user flows.
5. Local pending files are not shared between separate dynos. Distributed locks
   prevent overlapping work, but do not implement portable cross-dyno file storage.
   Do not infer full multi-dyno compatibility from the new locks.
6. Budgets are application-level disk/pixel limits, not a sandbox or an OS memory quota.
   Actual peak memory/load testing of near-limit documents remains required.
7. New-user insert conflicts are handled by rereading on existing unique-constraint
   errors only; application code does not create database uniqueness/transaction guarantees.
8. No Supabase RLS, security configuration, RPC permissions, authentication settings,
   credentials, permissions, referrals redesign, or financial schema were modified.
9. Upload completion and audit events retain the project's existing best-effort
   background/scheduled cleanup and analytics behavior; failures are logged.
