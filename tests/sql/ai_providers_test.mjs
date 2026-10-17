// Isolated PostgreSQL tests, no remote project or credentials.
// Run from repository root: node tests/sql/ai_providers_test.mjs
import { PGlite } from '@electric-sql/pglite';
import fs from 'node:fs';
import assert from 'node:assert/strict';
const db = new PGlite();
try {
  await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;');
  const baseline = fs.readFileSync('db/migrations/0001_baseline.sql', 'utf8');
  await db.exec(baseline.match(/CREATE TABLE public\.ai_model_slots \([\s\S]*?\n\);/)[0]);
  await db.exec(baseline.match(/CREATE TABLE public\.ai_model_pricing \([\s\S]*?\n\);/)[0]);
  await db.exec(`INSERT INTO ai_model_slots(slot,provider,model_name,display_order)
                VALUES ('cascade','gemini','original',1),('groq_fast','groq','original-fast',1);`);
  const migration = fs.readdirSync('db/migrations').find(name => name.endsWith('_ai_provider_registry.sql'));
  await db.exec(fs.readFileSync('db/migrations/' + migration, 'utf8'));
  await db.exec('GRANT SELECT, INSERT ON ai_model_pricing TO service_role; SET ROLE service_role');
  assert.equal((await db.query('SELECT * FROM ai_providers')).rows.length, 4);
  const original = (await db.query("SELECT * FROM ai_model_slots WHERE model_name='original'")).rows[0];
  assert.deepEqual(original.capabilities, ['text','image','pdf','audio']);
  await db.exec(`INSERT INTO ai_providers(id,label,transport,base_url,api_keys_env)
                VALUES('custom','Custom','openai_compatible','https://example.com/v1','CUSTOM_KEYS');`);
  await db.exec(`SELECT * FROM ai_model_add('cascade','custom','vendor/model-a:free');
                SELECT * FROM ai_model_add('groq_fast','custom','fast-a');
                SELECT * FROM ai_model_add('detection','custom','vision-a');`);
  let model = (await db.query("SELECT * FROM ai_model_slots WHERE model_name='vendor/model-a:free'")).rows[0];
  assert.deepEqual(model.capabilities, ['text']);
  await db.query('SELECT * FROM ai_model_update($1,$2,$3)', [model.id, 'model-b', ['text','image']]);
  await assert.rejects(db.query('SELECT * FROM ai_model_update($1,$2,$3)', [model.id, 'bad', ['text','pdf']]));
  await assert.rejects(db.exec("SELECT * FROM ai_model_add('cascade','missing','x')"));
  await assert.rejects(db.exec("SELECT * FROM ai_model_add('cascade','custom','bad name')"));
  await db.exec("UPDATE ai_providers SET label='Renamed',base_url='https://new.example/v1',is_enabled=false WHERE id='custom'");
  assert.equal((await db.query("SELECT is_enabled FROM ai_providers WHERE id='custom'")).rows[0].is_enabled, false);
  // Historical prices and quizzes are deliberately independent of provider FK.
  await db.exec("INSERT INTO ai_model_pricing(provider,model_name,input_price_per_million,output_price_per_million) VALUES('custom','model-b',1,2)");
  await db.exec("DELETE FROM ai_providers WHERE id='custom'");
  assert.equal((await db.query("SELECT * FROM ai_model_slots WHERE provider='custom'")).rows.length, 0);
  assert.equal((await db.query("SELECT * FROM ai_model_pricing WHERE provider='custom'")).rows.length, 1);
  for (const role of ['anon', 'authenticated']) {
    await db.exec('RESET ROLE; SET ROLE ' + role);
    await assert.rejects(db.exec('SELECT * FROM ai_providers'));
    await assert.rejects(db.exec("SELECT * FROM ai_model_add('cascade','gemini','unauthorized')"));
    await assert.rejects(db.exec("SELECT * FROM ai_model_update(1,'unauthorized',ARRAY['text'])"));
  }
  await db.exec('RESET ROLE');
  assert.equal((await db.query(`SELECT relrowsecurity FROM pg_class WHERE oid='public.ai_providers'::regclass`)).rows[0].relrowsecurity, true);
  console.log('PASS provider SQL: migration, preservation, CRUD, slots, capabilities, cascade deletion, historical prices, RLS and RPC grants');
} finally {
  await db.close();
}
