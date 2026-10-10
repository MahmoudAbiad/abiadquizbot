# Progressive quiz experiment (test branch)

Base: `9b133cba81e4af89a56b596c8193b260f85b32c4`.

## Behavior

- Gemini/Vertex and Groq quiz generation use streaming. A question is published only after its complete JSON object passes schema and answer-index validation.
- Applies to generated private quizzes from PDF, image albums, text, Office extraction and audio transcripts. Cached/shared quizzes retain the existing full-result path. Audio transcription itself is unchanged.
- Extended/Super PDF calls stay parallel and retain context-page ownership. Delivery follows source order and the existing boundary deduplication/quotas. A later part buffers until earlier parts finish. Super image batches also keep their original order.
- Students can answer, request hints and advance while generation continues. Reaching the buffer end waits for the next question instead of completing or skipping it.
- Choices are normalized for ready-made exams and shuffled once before publication. Final persistence uses those same choices. Finalization preserves answers, score, current index and the identity of already-sent polls.
- Save/share/edit/new operations wait until generation finishes. “إيقاف الحل” pauses playback while generation continues; students can resume. If left paused, the final complete quiz is saved and the attempt is reported as stopped early.
- A failure before any complete question can use the existing provider fallback. After a question has been emitted, a failed attempt cannot silently substitute a different provider's questions. The existing receipt-based refund path is used, and partial playback is invalidated.
- A renewable producer lease protects generation while the ordinary user lock is held only for short playback/state mutations. If a worker disappears, the next message/callback after lease expiry can refund the pending receipt and clear the abandoned session. Old producers cannot replace newer session state.

## Switch and rollback

`QUIZ_STREAMING_ENABLED=1` enables the experiment (default on this test-branch change).
`QUIZ_STREAMING_ENABLED=0` restores non-streaming for newly started generations.
Admin control: الإعدادات العامة → مفاتيح التحكم → عرض الأسئلة أثناء التوليد (Streaming).
The saved `quiz_streaming_enabled` feature flag defaults on and controls new generations without a restart. Running generations keep their current mode. The environment variable remains an emergency override; the admin panel warns when it disables streaming.
No database migrations or security changes are required. Allow active generations to finish before restarting/reverting. Revert this change's commit with `git revert <streaming-commit>`; do not reset the branch to an old commit and discard unrelated work.

## Validation

- 129 focused Python tests passed: streaming, generation/refunds, PDF context splitting, session navigation, uploads, resource cleanup, queue reliability and AI audit/timeout checks.
- Import smoke test: 122 modules, all passed. Python compilation and `git diff --check` passed.
- 32 dedicated streaming tests cover incremental JSON/LaTeX, provider errors/timeouts/cancellation, ordered chunks, stable shuffling, early playback, end-of-buffer waiting, finalization, duplicate answers, pause/resume, stale-session isolation and refunds.
- Full discovery at the earlier validation checkpoint: 491 tests; 18 errors and 35 skips. Errors were in unrelated challenge/export tests requiring resources not materialized in the local code snapshot (challenge rules JSON, migration SQL, Arabic fonts). This is not a claim that the entire suite passed.
- No live Gemini/Groq or Telegram generation was performed, and no manual hosting deployment was performed. Runtime verification should use the test bot.

## Manual test

1. Generate a fresh short text quiz; first question should appear before the request finishes, and answers should count.
2. Advance faster than generation; wait notice should appear once and the next question should arrive automatically.
3. Test an 18-page/40-question PDF and a Super PDF. Verify source order, boundary context and final question count.
4. Test a mathematics quiz, translated language quiz and ready-made five-choice exam; displayed and saved correct choices must agree.
5. Pause/resume during generation; try a new upload and save/share while generation is pending.
6. Finish and replay; results, saved questions and deduplication of answers must remain correct.
7. Repeat with `QUIZ_STREAMING_ENABLED=0` to compare the original full-result behavior.

## Limits

Streaming reduces time to the first usable question, not necessarily total generation time. Initial model reasoning/uploading still takes time. Later chunks may be buffered to preserve order. Attempt analytics currently create the tracking row when the final quiz ID/count becomes available, so its recorded duration excludes the early streaming portion. A worker restart cannot reconstruct an interrupted provider connection; the pending receipt is recovered through the lease/refund flow.
