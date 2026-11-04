// Real isolated PostgreSQL engine; no production data or credentials.
import { PGlite } from '@electric-sql/pglite';
import fs from 'node:fs';
import assert from 'node:assert/strict';
const db = new PGlite();
try {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    CREATE TABLE group_quiz_sessions (id uuid PRIMARY KEY, chat_id bigint, status text);
    INSERT INTO group_quiz_sessions VALUES ('11111111-1111-1111-1111-111111111111',-100123,'active');`);
  const sql = fs.readFileSync('db/migrations/20261027110638_group_quiz_chat_lock_journal.sql', 'utf8');
  await db.exec(sql); await db.exec(sql);
  assert.equal((await db.query('SELECT lock_chat FROM group_quiz_sessions')).rows[0].lock_chat, false);
  const insert = `INSERT INTO group_quiz_chat_locks(chat_id,session_id,original_permissions,locked_permissions)
    VALUES(-100123,'11111111-1111-1111-1111-111111111111','{"can_send_messages":true}','{"can_send_messages":false}')`;
  await db.exec('SET ROLE service_role'); await db.exec(insert);
  assert.equal((await db.query('SELECT state FROM group_quiz_chat_locks')).rows[0].state, 'prepared');
  await assert.rejects(db.exec(insert)); // One journal per chat: never overwrite a snapshot.
  await db.exec('RESET ROLE');
  await assert.rejects(db.exec('DELETE FROM group_quiz_sessions')); // Pending restore cannot be purged.
  for (const role of ['anon','authenticated']) {
    await db.exec('SET ROLE ' + role);
    await assert.rejects(db.exec('SELECT * FROM group_quiz_chat_locks'));
    await assert.rejects(db.exec(insert));
    await assert.rejects(db.exec('DELETE FROM group_quiz_chat_locks'));
    await db.exec('RESET ROLE');
  }
  assert.equal((await db.query("SELECT relrowsecurity FROM pg_class WHERE relname='group_quiz_chat_locks'")).rows[0].relrowsecurity, true);
  await db.exec('SET ROLE service_role; DELETE FROM group_quiz_chat_locks; RESET ROLE; DELETE FROM group_quiz_sessions');
  console.log('PASS: migration twice, default-off, durable FK, unique snapshot, RLS/client denial, service-role lifecycle');
} finally { await db.close(); }
