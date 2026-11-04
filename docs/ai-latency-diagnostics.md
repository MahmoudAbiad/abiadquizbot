# AI request latency diagnostics

Search Heroku logs / Papertrail for `AI_DIAG`. Copy the `request_id` to see the
existing `AI_REQUEST_*`, `AI_ATTEMPT_*` and new diagnostic events together.
`provider`, `model` and `credential_index` distinguish fallback attempts; the index
is only the position in the configured credential list, never an API key.

Each attempt emits stage transitions, transport milestones, a `waiting` event
every 30 seconds, and a terminal `success`, `failed`, `timeout` or `cancelled` event.
`elapsed_ms` is time since this attempt started, `stage_ms` is time since the
current SDK stage started. Stage transitions also emit `stage_done` with that
stage's duration. These are separate from whole-cascade `AI_REQUEST_*` timings.

Stages: `client_setup`, `attachment_prepare`, `attachment_read`,
`file_session_upload`, `file_upload`, `generate_wait`, `response_parse`.
Transport events distinguish DNS/connection establishment, request sending,
response headers and response body reception where the backend exposes them.
The default SDK backend is retained (aiohttp if installed, otherwise HTTPX).
The SDK-managed mTLS session is retained without detailed transport tracing.

How to interpret a slow attempt:

* `attachment_read` without `generate_wait`: local file preparation has stalled.
* `connect_start` without `connect_done`: connection setup has not completed.
* `send_body_complete` (HTTPX), or the `send_body_chunk` milestone (aiohttp),
  followed by a long wait without `response_headers_complete`: data has been
  passed to the transport but response headers have not arrived. This does not
  prove Google has accepted or started computing the request. aiohttp chunk
  events are sampled once per request, not an assertion that every chunk is sent.
* Response headers followed by delayed body reception: investigate the response
  download, SDK parsing and client cleanup separately.
* `generate_wait` without `request_start`: still inside SDK preprocessing,
  authentication or another step before the traced generation HTTP request.

`last_network` is the latest transport event; `last_progress` retains the latest
nonfailure event when a timeout/connection exception arrives. `cleanup_start`
and `cleanup_ms` identify time spent closing the client/deleting temporary remote
files. Cleanup network requests do not overwrite generation milestones.

Google SDK errors may use a string `status` (`UNAVAILABLE`) and an integer `code`
(`503`). The runtime now retains the numeric code in `AI_ATTEMPT_FAILED` rather
than discarding it as `None`.

No prompts, responses, image bytes, file paths, URLs, headers, credentials or raw
exception messages are emitted by the new diagnostics. The trace callbacks use
an allowlist of event names and numeric HTTP statuses only. Context is isolated
per attempt and removed on success, failure, timeout and cancellation.

No synthetic production AI requests are needed. Diagnostics start with the next
normal user request after deployment. These logs cannot distinguish Google's
internal queuing from internal generation time without provider-side evidence.

This change retains configured model order, credential rotation, retry counts
and request timeouts. Numeric status extraction restores existing cooldown
classification for SDK errors (including `429`); it does not add a retry policy.

Verification: `python -m unittest discover -s tests -p 'test_ai*.py' -v`.
The latency tests use a local HTTP server with the pinned Google SDK, test both
aiohttp and HTTPX when available, stall responses to exercise timeouts, verify
numeric 503 errors and check concurrent context/heartbeat cleanup and privacy.
