# Optional rendering/export service on Render

The existing test bot and a separate HTTP renderer share the `test` branch.
The bot keeps its database, Telegram, Redis and AI configuration. The renderer
needs only a shared `OFFLOAD_WORKER_SECRET` and bounded execution settings.

Worker: Python runtime; `pip install -r requirements.worker.txt`; start with
`python render_worker.py`. Same region as the bot (Frankfurt). Health: `/health`.
Use one web instance. Free instances receive requests over their public HTTPS URL.
Set `APP_ROLE=render_worker`, `WORKER_CONCURRENCY=2`, `WORKER_MAX_QUEUE=8`,
`MALLOC_ARENA_MAX=2`, and `MALLOC_TRIM_THRESHOLD_=131072` on the worker.

Bot: set `OFFLOAD_ENABLED=1`, `OFFLOAD_WORKER_URL` to the worker URL (no trailing
slash), and the exact same shared secret. Start with `OFFLOAD_REMOTE_CONCURRENCY=4`
and `OFFLOAD_UPDATE_CONCURRENCY=15`. `OFFLOAD_FALLBACK_LOCAL=1` preserves availability
by doing local work when the renderer is unavailable. Setting it to `0` forbids local
fallback while offloading is active, including an open circuit or oversized payload.
Keep secrets in Render configuration only, never in source control or logs.

`OFFLOAD_ENABLED=0` returns to the existing local drawing/export implementation,
including its semaphore, Matplotlib lock, export admission, and cancellation behavior.
Student/teacher `include_answers` is transported explicitly, so student exports do
not accidentally receive teacher solutions. Export admission remains held through
file upload to bound the bot's result memory; remote builds do not take local CPU slots.

The renderer uses two spawned processes with independent Matplotlib locks. It
holds active slots until jobs really finish, even after HTTP cancellation. It
recovers its process pool if a child exits. No bot/database imports happen in the
HTTP parent. Cleanup happens in the child that performed the rendering.

The client rechecks the circuit after waiting for a connection slot. Recovery is
limited to one probe even when that request exceeds the cooldown. Older request
results cannot close a newly opened circuit. Invalid user content (422) is surfaced
without rebuilding locally. Set `OFFLOAD_RENDER_READ_TIMEOUT=90` on Render Free to
allow cold starts. Export timeout defaults to 150 seconds.

Free is useful for verification, not a promise of faster rendering: its CPU is
shared, both child processes share the same 512MB allocation, and it sleeps when
idle. The workspace's Free hour allowance is shared across services. Do not run
periodic HTTP pings just to keep both services awake. Monitor deploy states and
runtime errors without waking idle instances; measure speed and peak memory under
representative files before increasing limits or selecting paid compute.

Validation: run test_offload, test_render_processes, test_export_queue and image
layout tests. Live verification uses synthetic math PNG, student and teacher DOCX,
and PDF requests against the worker, followed by deployment/log checks on the bot.
These synthetic checks do not send Telegram messages or spend AI credits.

References: https://render.com/docs/compute-plans and https://render.com/docs/free
