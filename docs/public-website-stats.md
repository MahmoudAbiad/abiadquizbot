# Public website statistics

Purpose: show the registered-account count and the sum of the bot's existing
`users.total_questions` counter on abiad.me, using the same source as the admin
dashboard. This is a cumulative application counter, not independently audited
unique questions or attempts; failed/refunded requests and historical deletions
may affect its meaning according to existing accounting behavior.

## Boundary

- Opt in only on the production bot with `PUBLIC_WEBSITE_STATS_ENABLED=true`.
- `GET /api/public-stats` returns exactly `students`, `questions`, `updated_at`.
- No names, account identifiers, balances, question content, files, tokens,
  database URLs, filters, user lookup, writes or HTTP-triggered refreshes.
- The browser gets no database credential. Database RLS, grants and other bot
  endpoints are unchanged. No schema migration is required.
- The request handler reads an in-process snapshot only. It does not query
  Supabase or Redis. The existing server client refreshes every 15 minutes,
  selecting only the numeric question counter in bounded pages, with a 45-second
  total timeout. This also avoids adding per-visitor Upstash commands.
- Queries are ordered by the existing indexed account key and limited to accounts
  registered at the refresh cutoff. Concurrent updates/deletions can still cause
  eventual consistency; the timestamp describes the refresh, not a transactional
  database snapshot. Pagination continues under a smaller configured row limit.
- Only a complete, validated scan replaces the cache. On failure the previous
  snapshot remains valid for at most 24 hours. On startup or after expiry, return
  a generic 503 and Retry-After; never manufacture zero values.
- All origins can request these intentionally public aggregates outside browser
  CORS enforcement. Only `https://abiad.me` and `https://www.abiad.me` receive
  browser CORS permission. No credentialed CORS. This policy is not authentication.
- Responses can be cached for 60 seconds by clients / 300 seconds at the edge.
  Raw traffic still reaches the hosting platform if its proxy does not cache;
  provider-level request limits remain appropriate for availability protection.
- Each web process/dyno has its own cache and refresh task. Shutdown cancels it.
  Disabled environments return 404 and start no aggregate task.

## Deployment and checks

1. Merge the reviewed bot change and deploy that commit on the production Heroku
   app serving `https://bot.abiad.me`; do not change a test app or its database.
2. Set `PUBLIC_WEBSITE_STATS_ENABLED=true` on that exact app. The configuration
   change restarts its dynos; the endpoint is unavailable until the first scan.
3. Confirm `/health` and the existing bot behavior still work. Request the public
   endpoint and verify the exact three-field payload, fresh UTC timestamp and
   counts against the production database/admin source.
4. Check an allowed Origin and an unrelated Origin, query rejection and POST 405.
5. Publish the website's `data-stats-url` to the endpoint after it is healthy.
   Until successful refresh, the website keeps its explicitly dated fallback.
6. To disable publication, set the flag false. No credential rotation or database
   privilege change is needed. Reverting the website does not affect the bot.

Tests: `python -m unittest discover -s tests -p test_public_website_stats.py -v`.
Cases cover more than 1,000 accounts, reduced row limits, bad/null counters,
partial failure, response projection, CORS boundaries, stale cache, disabled
state, query/write rejection and repeated HTTP requests causing no DB refresh.
