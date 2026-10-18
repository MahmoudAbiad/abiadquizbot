# Admin gift management

This change belongs to `test`. It does not deploy production or send an actual
gift campaign. New administrative actions require the additive migration
`db/migrations/20261018084726_gift_admin_campaigns.sql` on the test database.
The old code list, creation and enable/disable controls remain usable before the
migration; new features are hidden until its schema is available.

## Code actions

- **Disable** prevents new redemptions, without changing already granted gifts.
- **Archive** also disables redemption and moves the code to a separate paginated
  archive. It preserves beneficiaries, gifts and balances. Restore moves it back
  while leaving it disabled; enabling is a separate explicit action.
- **Delete** is offered for unused codes. The database rechecks under the code
  lock that no redemption or gift exists. A concurrently redeemed code cannot
  be deleted. Used codes must be archived instead.
- **Revoke all gifts from this code** previews affected beneficiaries and their
  currently spendable remaining gift points. Confirmation expires after five
  minutes and is tied to the latest admin action. The database disables the code
  and marks only this code's gift ledger entries revoked, retaining all amounts
  and history. Spent points and other free/paid/gift balances are not subtracted.
  The marker also covers spent entries so later refunds cannot restore their
  spendability. A revoked code cannot be re-enabled; issue a new code instead.

Redemption and revocation take the same per-code advisory transaction lock before
user locks. Bulk revocation locks affected users in ascending order, then the
code and gifts. Existing debit/refund logic remains unchanged and serialized on
the user row. Repeated revocation has no further effect.

## Direct gift campaigns

The admin supplies a title, points per user (0.01–100000, at most two decimals),
validity in days (0 means permanent), and a plain-text message (1–2000 characters).
The preview snapshots all currently registered positive user IDs, and displays
their count, per-user amount, total points, message and validity. No points are
granted and no messages are sent at preview time. Confirmation expires after
30 minutes; new registrations after the preview are not included.

After confirmation, a durable PostgreSQL recipient ledger drives bounded work in
both polling and webhook modes. Campaign/user gift uniqueness and transactional
progress prevent duplicate grants across repeated callbacks, multiple workers,
failures or restarts. Deleted accounts are skipped. Each user's validity starts
when their gift is granted. Notification failure never rolls back a gift.

Telegram notices include the admin's escaped plain text, points, remaining
validity and create-quiz/wallet buttons. Known flood waits are retried with a
persisted delay, up to five claims. Blocked/rejected notices are reported.
Timeouts and interrupted in-flight sends are marked **uncertain**, rather than
blindly resent: Telegram has no send idempotency key. In-flight claims older than
two minutes are recovered as uncertain. The gift itself is still granted once.

The reports list separates granted, skipped, delivered, blocked, failed, pending
and uncertain counts. Completion sends at most one admin report notice; it never
overwrites a menu the admin may have opened meanwhile. The report is durable and
remains accessible if that admin notice cannot be delivered. No student FSM is
cleared or changed by campaign delivery.

The additive tables have RLS enabled and no anon/authenticated privileges. All
new RPCs are SECURITY INVOKER, with EXECUTE granted only to the server's existing
service_role. Telegram entry points retain the private-chat IsAdminFilter.

## Validation and test activation

- `python -m unittest discover -s tests -p 'test_admin_gift_campaigns.py' -v`
- `node tests/sql/gift_admin_test.mjs`: isolated PGlite database, no credentials.
- CI additionally runs the SQL tests against an isolated PostgreSQL service and
  verifies concurrent redemption/revocation and concurrent grant workers.
- Apply the migration only to the verified test project, then verify new
  columns/tables/functions and privileges. Merely installing the migration does
  not grant/revoke points or send any campaign.
- On the test bot, exercise archive/restore and stale confirmation buttons using
  a test code. Confirm a small campaign only with the intended test accounts;
  verify the preview audience before the economic action. A running campaign
  continues after navigating away or restarting the bot.

Production migrations and production deployment are outside this task.
