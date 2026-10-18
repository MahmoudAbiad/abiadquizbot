# Practice your mistakes

The private quiz result has two separate actions when there are recorded wrong
answers: **📚 مراجعة الأخطاء** and **🎯 تدرب على أخطائك (N)**. Rich-message buttons
are preferred; a normal inline keyboard provides the same actions if unsupported.

Practice uses the existing question sender, poll-answer recorder, next/stop
handlers, and error review. It copies the full original question payload (including
options, explanation, hint, math tables/matrices, and cached image URL). There is
no question generation or AI call. Correct and unanswered questions are excluded.
Each completed round offers another round containing only that round's mistakes.
Skipping a practice question is disclosed and prevents claiming mastery of the
entire series, including in later rounds. Skips in the original quiz do not count
as mistakes and do not prevent mastering its recorded errors.

## Isolation and retention

- A practice session has its own random navigation identity, score, answers,
  progress, and round number in the existing Redis FSM. It has no database attempt
  ID and no source quiz ID. Thus practice cannot update the original score,
  leaderboard, attempt analytics, or question image at a remapped subset index.
- Practice never enters the generation, charging, or reward paths. No schema,
  point balance, stored quiz, or existing result is changed. Practice scores are
  temporary; they are not inserted in `quiz_attempts` or `quiz_scores`.
- Review snapshots retain their existing format and seven-day TTL. Separate
  full-question practice snapshots use `quiz:mistake_practice:<user>:<token>` with
  the same TTL. The FSM retains its existing one-day expiry.
- A practice start must match the current completed result and its session
  identity, in a private chat with no other active FSM flow. An older result
  cannot replace a newer quiz, restart an already-started practice, or reopen
  errors removed in a later round. Old reviews remain readable independently.
- Next, hint, and stop controls carry a fresh question token; a stop confirmation
  cannot act on another question or another session. Legacy unscoped controls are
  accepted only for pre-upgrade sessions without a question token.
- Historic save/share/replay/feedback-wizard callbacks cannot mutate a practice
  subset. Start the full quiz again from its saved/shared source to replay it.
- Missing/expired snapshots and Redis loading failures produce an explicit alert.
  An expired FSM also prevents an old training button from starting a new run;
  reopen and solve the original quiz instead.

## Automated verification

`python -m unittest discover -s tests -p 'test_mistake_practice.py' -v` covers the
40 → 8 → 3 → 1 → 0 chain, unchanged source results/review, independent identities,
early stops, skips, zero mistakes, math image reuse, deep copies, answer
deduplication, stale/foreign/expired buttons, Redis failures, and rich fallback.
The regular reliability workflow discovers these tests automatically.

## Telegram checks on the test bot

1. Finish a text quiz with several wrong answers. Open review and toggle its
   explanation, then start practice from the separate training button. Confirm
   counts shrink each round and the final all-correct round congratulates you.
2. Repeat with math images, tables, and matrices. Confirm cached images, letter
   choices, hints, and explanations appear correctly in Telegram clients.
3. Stop the original early; only explicitly wrong answers should enter practice.
   Stop/skip during practice and verify skipped questions are not called errors
   and the bot does not claim that every original mistake has been mastered.
4. Test all-correct and all-unanswered runs: neither offers mistake practice.
5. Double-tap training/next; tap an old training button, old poll controls, an old
   stop confirmation, and old save/share buttons while a new session is active.
   Confirm the current score/progress/session is unaffected.
6. Check rich controls and the ordinary inline-keyboard fallback on supported
   clients. Confirm the original result and leaderboard remain unchanged.
7. On an isolated test Redis, expire the practice snapshot or FSM and verify the
   explanatory alert. Do not clear or modify shared/production Redis.

No production deployment is part of this change.
