# Group quiz setup and permission recovery

Group launches return an owner-bound, random 192-bit deep link valid for ten
minutes. The private `/start gqsetup_…` route preserves group, quiz and forum
topic without touching the private quiz FSM. Redemption checks live Telegram
administrator status and uses atomic Redis GETDEL. Other users cannot consume
or redeem the link. Configuration and manual finish also check current rights.

Locking is explicitly opt-in and defaults off. It applies only to groups with a
question timer or fixed send interval. Burst, private chats and channels cannot
activate it. The bot must have restrict-members rights. Existing closed groups
are left untouched. Voting on existing polls remains available; send-polls
permission governs creating polls, not voting.

`group_quiz_chat_locks` journals the exact original permissions before changing
Telegram. Redis serializes each group's permission operations across workers;
API calls have bounded timeouts. A completed restoration is required before the
journal is deleted. Both normal finish and manual finish restore permissions
before processing results. First-question failure also restores. The heartbeat
recovers pending restorations after restart and repeats failed attempts without
expiring the permission snapshot. An interrupted prepare/apply is resumed only
for an active session. Pending records prevent cleanup deleting their session.

Setup writes and activation are conditional on waiting status. Heartbeat arming
is conditional on active status, so a delayed start cannot resurrect a finish.
The ordinary-message group filter remains enabled and allows the new `gql:`
configuration callbacks alongside existing group quiz controls.

The schema gate caches readiness for 60 seconds. Before the migration, group
quiz setup follows the existing timer/pacing flow and hides the unavailable
lock option. Recovery also waits for the journal schema. Deploying code ahead
of the migration therefore preserves existing quizzes; applying it enables the
new option automatically within one minute.

## Deployment order

1. Verify production database identity and inspect existing session schema.
2. Apply `db/migrations/20261027110638_group_quiz_chat_lock_journal.sql`.
   It is additive and idempotent, with short DDL lock and statement timeouts.
   It enables RLS, revokes public/client access and grants only service-role CRUD.
3. Deploy the matching master revision, confirm GitHub Actions and Heroku release,
   and inspect startup/runtime logs without printing configuration values.

## Verification

`python -m unittest discover -s tests -p 'test_group*py' -v`

`node tests/sql/group_quiz_lock_test.mjs`

The Python cases cover owner-only links, replay, revoked admins, topic preservation,
normal permission lifecycle, already-closed groups, API/DB failures, interrupted
apply, restart recovery, simultaneous finish, mutex contention, configuration
races and channel/private/burst exclusions. The isolated PostgreSQL test verifies
repeat migration, existing-row default, unique journal, FK durability, RLS,
client denial and service-role lifecycle. CI runs both automatically.

## Operational limits

Telegram cannot restore permissions while its API is unavailable or after the
bot loses restrict-members rights; the journal remains and recovery retries.
Default group permissions do not restrict Telegram administrators or members
explicitly exempted by Telegram. Restoring the saved permissions replaces any
manual default-permission edits made by admins during the locked quiz.
