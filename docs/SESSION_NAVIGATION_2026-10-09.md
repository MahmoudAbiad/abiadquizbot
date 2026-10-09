# Quiz session navigation — test branch

Base: fbbc362e6f5e60a4027a8a3cc604f14d9b438017.

## Behavior

- Telegram document/photo/album, text and audio intake ask before replacing an unfinished quiz. The shared quiz loader applies the same choice to favorites and shared links; replay is guarded too.
- The prompt offers resume, end-and-continue and cancel. Resume renders the current question without resetting the score or question index. Confirmation is bound to a user, chat, session and random request token; replaced, expired and already-used prompts cannot stop a newer quiz.
- Telegram web-upload entry and oversize-file redirect buttons first call the bot. A successful check reveals the Web App button.
- All three authenticated upload init endpoints check before creating presigned upload targets. Existing upload pages already display HTTP error details before uploading bytes; old open pages are therefore covered too.
- If a quiz starts while upload is in flight, background intake retains the upload and offers the same choice. It expires after 30 minutes. Redis stores the pending action and a cleanup deadline; a per-minute sweeper runs in both webhook and polling modes. Cancel/replacement/expiry deletes temporary objects; confirmed intake retains the existing per-user lock and web-preparation semaphore.
- Album messages are collected into one pending action. Confirmed dispatch restores the original student identity from the saved Telegram message.
- The live challenge's active-land-attack warning has direct resume and withdrawal buttons. Withdrawal still uses the existing confirmation and no-reward rules. This change does not introduce a shared lock between the independent PvP/land game and ordinary quiz systems.
- Private poll metadata includes the attempt/session ID. Answers from another attempt, another question or duplicate deliveries cannot increase the score twice after a question is re-rendered. Polls created by older code do not carry the new metadata; resume or advance displays a newly bound question.

## Validation

123 passing tests across session navigation (19), live challenge UI (66), upload reliability (14), generation regressions (18), resource cleanup (6).

```
PYTHONPATH=tests:. python -m unittest test_session_navigation test_challenge_live_ui test_upload_reliability test_generation_regressions test_resource_cleanup -q
python -m compileall -q handlers services webhook_server.py main.py keyboards.py
```

Tests use local fakes for Telegram, Redis and persistence; no production services or database schema were changed. No live Telegram end-to-end run or deployment was performed.

## Manual acceptance on the test bot

1. During a quiz, try audio, a PDF/photo album, a long text, a favorite and a shared link. Resume should preserve progress; confirm should process the chosen request.
2. Click web upload during a quiz; the choice must appear before the page-opening button. Repeat from a previously opened upload page: init must block before file transfer.
3. Begin a quiz while a web upload is in flight; verify the completed upload is offered for continuation and is cleaned on cancel/expiry.
4. Reuse an older confirmation after starting a newer quiz; it must not stop it. Answer a re-rendered question twice; score must increase at most once.
5. Start a live search while a land attack is active; use the direct resume/withdrawal actions.
