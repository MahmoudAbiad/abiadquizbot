# Branch archive management — 2026-10-08

Starting SHA: `64ac03d3de4ba6d689b782986edbce9dbc9d9e6a` (GitHub `test`).

## Behavior

Admin Settings → Challenge management → 🗄 الفروع المؤرشفة.
Paginated archived branches open a read-only hierarchy of subjects, units, tests,
and existing admin question previews. Descendants of all statuses are included.
Only branch detail exposes restore, rename and permanent delete.

Restore changes the branch status only, preserving IDs, data and independently
archived descendants. Existing active-name uniqueness prevents conflicts; the
admin can rename the archived branch and retry. The active list's archive icon
and confirmation now explicitly say archive rather than permanent deletion.

Permanent deletion requires an archived branch and a fresh FSM confirmation
matching its ID. Navigating away clears confirmation. All handlers retain the
router's IsAdminFilter and private-chat filter; actor identity is from Telegram.
No real user branch was deleted during implementation.

## Database

0016_challenge_branch_archive.sql introduces one SECURITY INVOKER RPC,
challenge_delete_archived_branch(uuid), executable only by service_role (and
owner). No grants to anon/authenticated, no changes to RLS/Auth or gameplay RPCs.
It locks the branch and content parents, then deletes its questions, tests,
units, subjects and branch in one transaction. Existing foreign keys reject
branches/content referenced by profiles, towers, queues, matches or answers.
A late FK failure rolls back every earlier delete: no partial content loss.
No cascade into gameplay/history, users, balances, XP, trophies or source files.
No storage object deletion: source images may be shared by other quizzes.

Applied only to verified Test_telegramquizbot / xhlksusmczccqyvbxylo as
challenge_branch_archive_0016. Backup of columns, constraints, migration list,
challenge flags/settings: BACKUP_BEFORE_BRANCH_ARCHIVE_TEST_2026-10-08.json.
This is a schema/settings record, not a complete row-data backup.
Rollback SQL removes only the RPC; it cannot undo a user-confirmed deletion.

## Validation

- Challenge suite: 200 passed, 0 failures/errors/skipped.
- Full suite: 409 passed, 0 failures/errors/skipped.
- Smoke imports: 113 successful.
- SQL regression executed locally in PGlite with migrations 0008–0016;
  rollback/reapply also executed locally.
- Same transactional SQL regression executed successfully on Supabase Test;
  its fixtures were rolled back. Covers active-branch rejection, late FK failure
  with children retained, unchanged XP/trophies, restore name conflict, successful
  restore, complete deletion, repeated deletion, role grants.
- Test metadata confirmed prosecdef=false, anon/authenticated execution=false,
  service_role execution=true.
- First Python runs failed due missing aiogram/dependencies after environment
  reset. Installed declared requirements in an isolated environment and reran
  successfully with the results above. First remote regression request returned
  expired requestState; one retry succeeded.

Files: handlers/admin/challenges.py, helpers/supabase_helper/challenges.py,
0016 and rollback, tests/test_challenge_admin_branches.py,
tests/sql/challenge_branch_archive_test.sql,
tests/sql/run_challenge_branch_archive.mjs, backup record and this document.

## Manual test remaining

Deploy latest test to Render Test; use Telegram Test Bot as admin.
Create a disposable branch with a subject/unit/test, archive it, open the archive
and inspect all levels. Restore it and confirm IDs/content remain. Archive again,
open permanent-delete confirmation, cancel, then retry and confirm on this
unused disposable branch. For an existing linked branch, expect a clear rejection
and retained content. Test duplicate active name then rename/retry restoration.
Non-admin accounts must not access these routes. No Production changes or deploy.

Limitations: no automatic removal/reassignment of student profiles or game history;
linked branches remain archived. Previews reuse the existing shortened admin
question preview. Row data backup/export and restore of individual independently
archived descendants are outside this change. Telegram live test and multi-client
concurrency stress were not performed; transactions/FKs enforce deletion atomicity.
