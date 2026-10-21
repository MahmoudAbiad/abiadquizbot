"""Cache/lock/worker/retention races, without external network or credentials."""
import asyncio
import copy
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from services import runner_images as images, runner_cleanup as cleanup, runner_api as api
from services.quiz_timing import question_key


class Query:
    def __init__(self, db, table):
        self.db, self.table, self.filters, self.op, self.cap = db, table, [], 'select', 10000
    def select(self, *a): return self
    def eq(self, k, v): self.filters.append(lambda r: r.get(k) == v); return self
    def lt(self, k, v): self.filters.append(lambda r: r.get(k, '') < v); return self
    def in_(self, k, values): self.filters.append(lambda r: r.get(k) in values); return self
    def order(self, *a): return self
    def limit(self, n): self.cap = n; return self
    def or_(self, expr):
        cutoff = expr.split('finished_at.lt.')[1].split(',')[0]
        self.filters.append(lambda r: (r.get('finished_at') or r['expires_at']) < cutoff); return self
    def delete(self): self.op = 'delete'; return self
    def upsert(self, row): self.op, self.row = 'upsert', row; return self
    async def execute(self):
        rows = self.db.rows.setdefault(self.table, [])
        selected = [r for r in rows if all(f(r) for f in self.filters)][:self.cap]
        if self.op == 'upsert':
            keys = ('quiz_id', 'question_key', 'part')
            rows[:] = [r for r in rows if any(r.get(k) != self.row.get(k) for k in keys)]
            rows.append(copy.deepcopy(self.row)); selected = [self.row]
        if self.op == 'delete':
            self.db.events.append('delete:' + self.table)
            rows[:] = [r for r in rows if r not in selected]
        return types.SimpleNamespace(data=copy.deepcopy(selected))


class DB:
    def __init__(self): self.rows, self.events = {}, []
    def table(self, t): return Query(self, t)


class Redis:
    def __init__(self): self.values = {}; self.acquired = []
    async def set(self, key, token, **kw):
        if key in self.values: return False
        self.values[key] = token; self.acquired.append(key); return True
    async def eval(self, script, n, key, token, *a):
        if self.values.get(key) != token: return 0
        if 'release_owned' in script: del self.values[key]
        return 1


class ImageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db, self.redis = DB(), Redis()
        self.q = {'question': '$x+1$', 'options': ['$1$', '$2$', '$3$', '$4$'], 'correct_option_id': 0}
        self.qid = '11111111-1111-1111-1111-111111111111'
        self.db.rows['quizzes'] = [{'id': self.qid}]
        self.render = AsyncMock(return_value=b'png')
        self.upload = AsyncMock(return_value='https://test/image.png')
        self.patches = [patch.object(images, 'db', return_value=self.db), patch.object(cleanup, 'db', return_value=self.db),
            patch.object(cleanup, 'enabled', return_value=True),
            patch.object(images.offload, 'ENABLED', False), patch.object(images.offload, 'is_active', return_value=False),
            patch.dict(sys.modules, {'config': types.SimpleNamespace(redis_client=self.redis),
                'services.image_quiz_renderer': types.SimpleNamespace(render_game_image_async=self.render),
                'helpers.supabase_helper.quiz_images': types.SimpleNamespace(upload_quiz_question_image=self.upload)})]
        for p in self.patches: p.start()
    def tearDown(self):
        for p in reversed(self.patches): p.stop()
    def row(self, days=-1):
        return {'quiz_id': self.qid, 'question_key': question_key(self.q), 'part': 'question',
                'game_image_url': 'cached', 'expires_at': (datetime.now(timezone.utc)+timedelta(days=days)).isoformat()}
    async def test_fresh_hit_shared_and_expired_regenerates(self):
        self.db.rows['runner_images'] = [self.row(7)]
        self.assertEqual(await images.get_or_render_image(self.qid, self.q, 'question'), 'cached')
        self.render.assert_not_awaited()
        self.db.rows['runner_images'] = [self.row(-1)]
        await images.get_or_render_image(self.qid, self.q, 'question')
        self.render.assert_awaited_once(); self.assertTrue(images.fresh(self.db.rows['runner_images'][0]))
        self.assertEqual(self.upload.call_args.args[1], images.image_path(self.qid, question_key(self.q), 'question'))
    async def test_concurrent_students_render_once_and_cancel_does_not_cancel_owner(self):
        gate = asyncio.Event()
        async def render(*a, **kw): await gate.wait(); return b'png'
        self.render.side_effect = render
        first = asyncio.create_task(images.get_or_render_image(self.qid, self.q, 'question'))
        second = asyncio.create_task(images.get_or_render_image(self.qid, self.q, 'question'))
        for _ in range(10): await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError): await first
        gate.set(); self.assertEqual(await second, 'https://test/image.png')
        self.render.assert_awaited_once()
    async def test_foreign_redis_owner_publishes_cache_without_duplicate_render(self):
        key = images.image_lock(self.qid, question_key(self.q), 'question').key
        self.redis.values[key] = 'another-process'
        async def publish():
            await asyncio.sleep(.01); self.db.rows['runner_images'] = [self.row(7)]
        task = asyncio.create_task(publish())
        self.assertEqual(await images.get_or_render_image(self.qid, self.q, 'question'), 'cached')
        await task; self.render.assert_not_awaited(); self.assertEqual(self.redis.values[key], 'another-process')
    async def test_prewarm_renders_every_original_option_once_local_serial(self):
        active = peak = 0
        async def render(*a, **kw):
            nonlocal active, peak
            active += 1; peak = max(peak, active); await asyncio.sleep(.001); active -= 1; return b'png'
        self.render.side_effect = render
        await asyncio.gather(images.prewarm_quiz_images(self.qid, [self.q]), images.prewarm_quiz_images(self.qid, [self.q]))
        self.assertEqual(self.render.await_count, 5); self.assertEqual(peak, 1)
        await images.prewarm_quiz_images(self.qid, [self.q]); self.assertEqual(self.render.await_count, 5)
        self.assertEqual({r['part'] for r in self.db.rows['runner_images']}, {'question', '0', '1', '2', '3'})
    async def test_remote_background_never_falls_back_even_when_breaker_opens(self):
        with patch.object(images.offload, 'ENABLED', True), patch.object(images.offload, 'is_active', return_value=True):
            await images.get_or_render_image(self.qid, self.q, 'question', background=True)
        self.assertFalse(self.render.call_args.kwargs['allow_local_fallback'])
        self.render.reset_mock()
        with patch.object(images.offload, 'ENABLED', True):
            with self.assertRaises(images.offload.OffloadUnavailable):
                await images.get_or_render_image(self.qid, self.q, '0', background=True)
        self.render.assert_not_awaited()
        await images.get_or_render_image(self.qid, self.q, '0')
        self.assertTrue(self.render.call_args.kwargs['allow_local_fallback'])
    async def test_prewarm_yields_until_foreground_finishes(self):
        with patch.object(images,'_foreground',1):
            task=asyncio.create_task(images.prewarm_quiz_images(self.qid,[self.q]))
            await asyncio.sleep(.02);self.render.assert_not_awaited()
            images._foreground=0;await task
        self.assertEqual(self.render.await_count,5)
    async def test_global_remote_background_cap_across_quizzes(self):
        other='22222222-2222-2222-2222-222222222222'
        self.db.rows['quizzes'].append({'id':other})
        active=peak=0
        async def render(*a,**kw):
            nonlocal active,peak
            active+=1;peak=max(peak,active);await asyncio.sleep(.005);active-=1;return b'png'
        self.render.side_effect=render
        with patch.object(images.offload,'ENABLED',True),patch.object(images.offload,'is_active',return_value=True),patch.object(images,'_background_remote',asyncio.Semaphore(2)):
            await asyncio.gather(images.prewarm_quiz_images(self.qid,[self.q]),images.prewarm_quiz_images(other,[self.q]))
        self.assertEqual(peak,2);self.assertEqual(self.render.await_count,10)
    async def test_remote_backoff_three_retries_and_continues(self):
        with patch.object(images.offload, 'ENABLED', True), patch.object(images,'get_or_render_image',AsyncMock(side_effect=images.offload.OffloadUnavailable('down'))) as get, patch.object(images,'retry_pause',AsyncMock()) as pause:
            await images.prewarm_quiz_images(self.qid,[{**self.q,'options':[]}])
        self.assertEqual(get.await_count,4);self.assertEqual(pause.await_count,3)
    async def test_one_bad_image_does_not_abort_local_prewarm(self):
        async def render(*a,part,**kw):
            if part=='question': raise ValueError('bad image')
            return b'png'
        self.render.side_effect=render
        await images.prewarm_quiz_images(self.qid,[self.q])
        self.assertEqual(self.render.await_count,5);self.assertEqual(len(self.db.rows['runner_images']),4)
    async def test_real_offload_transport_enforces_per_request_fallback(self):
        o=images.offload
        with patch.object(o,'ENABLED',True),patch.object(o,'is_active',return_value=True),patch.object(o,'FALLBACK_LOCAL',True),patch.object(o,'_breaker',o._Breaker()),patch.object(o,'_post',AsyncMock(return_value=(503,b'busy'))):
            with self.assertRaises(o.OffloadUnavailable):await o.remote_render_image(self.q,0,1,True,allow_local_fallback=False)
            self.assertIsNone(await o.remote_render_image(self.q,0,1,True))
    async def test_disabled_prewarm(self):
        with patch.dict('os.environ', {'RUNNER_PREWARM_ENABLED': '0'}):
            await images.prewarm_quiz_images(self.qid, [self.q])
        self.render.assert_not_awaited()
    async def test_deleted_quiz_not_uploaded(self):
        self.db.rows['quizzes'] = []
        with self.assertRaises(ValueError): await images.get_or_render_image(self.qid, self.q, 'question')
        self.upload.assert_not_awaited()
    async def test_question_images_run_in_parallel(self):
        from services.runner_core import build_questions
        row = {'status':'running','questions':build_questions([self.q]),'cursor':0,'version':0,'issued_at':None}
        active = peak = 0
        async def get(*a, **kw):
            nonlocal active, peak
            active += 1; peak = max(peak,active); await asyncio.sleep(.01); active -= 1; return 'url'
        with patch.object(api,'identity',AsyncMock(return_value=7)),patch.object(api,'access',AsyncMock()),patch.object(api,'round_row',AsyncMock(return_value=row)),patch.object(api,'get_or_render_image',get):
            req=api.RoundRequest(init_data='x',quiz_id=self.qid,round_id=self.qid)
            result=await api.question(req)
        self.assertEqual(peak,4);self.assertFalse(result['started']);self.assertEqual(len(result['options']),3)
    async def test_cleanup_storage_first_failure_retains_metadata_and_round_cutoff(self):
        self.db.rows['runner_images'] = [self.row(-1)]
        now=datetime.now(timezone.utc)
        self.db.rows['runner_rounds']=[{'id':'old','finished_at':None,'expires_at':(now-timedelta(days=8)).isoformat()},
            {'id':'new','finished_at':now.isoformat(),'expires_at':(now-timedelta(days=8)).isoformat()}]
        remove=AsyncMock(side_effect=RuntimeError('storage down'))
        with patch.object(cleanup,'remove_files',remove): await cleanup.cleanup_once(now)
        self.assertEqual(len(self.db.rows['runner_images']),1)
        self.assertEqual([r['id'] for r in self.db.rows['runner_rounds']],['new'])
        async def success(paths): self.db.events.append('storage')
        with patch.object(cleanup,'remove_files',success): await cleanup.cleanup_once(now)
        self.assertFalse(self.db.rows['runner_images'])
        self.assertLess(self.db.events.index('storage'),self.db.events.index('delete:runner_images'))
    async def test_cleanup_skips_locked_or_refreshed_images(self):
        self.db.rows['runner_images']=[self.row(-1)]
        key=images.image_lock(self.qid,question_key(self.q),'question').key
        self.redis.values[key]='rendering'
        with patch.object(cleanup,'remove_files',AsyncMock()) as remove:
            await cleanup.cleanup_once();remove.assert_not_awaited()
        self.assertEqual(len(self.db.rows['runner_images']),1)
    async def test_orphan_storage_failure_retains_durable_queue(self):
        self.db.rows['quizzes']=[];self.db.rows['runner_image_gc']=[{'quiz_id':self.qid}]
        store=types.SimpleNamespace(list=AsyncMock(side_effect=[[{'name':'hash'}],[{'name':'question.png'}]]),remove=AsyncMock(side_effect=RuntimeError('storage down')))
        with patch.object(cleanup,'bucket',return_value=store):
            with self.assertRaises(RuntimeError):await cleanup.delete_runner_prefix(self.qid)
        self.assertEqual(len(self.db.rows['runner_image_gc']),1)
    async def test_delete_handler_attempts_prefix_cleanup_after_success(self):
        import ast,__future__
        from pathlib import Path
        source=ast.parse((Path(__file__).parents[1]/'handlers/quiz_delete.py').read_text(encoding='utf-8'))
        source.body=[n for n in source.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='handle_quiz_delete_execute']
        source.body[0].decorator_list=[]
        call=types.SimpleNamespace(data='qdelok_'+self.qid,from_user=types.SimpleNamespace(id=7),answer=AsyncMock(),message=types.SimpleNamespace(edit_text=AsyncMock(),delete=AsyncMock()))
        env={'admin_get_quiz_by_id':AsyncMock(return_value={'creator_id':7}),'can_delete_quiz':lambda *a:True,'admin_delete_quiz':AsyncMock(return_value=True),'logger':types.SimpleNamespace(warning=lambda *a,**kw:None),'log_error':lambda *a:None}
        exec(compile(source,'quiz_delete.py','exec',flags=__future__.annotations.compiler_flag),env)
        with patch.object(cleanup,'delete_runner_prefix',AsyncMock()) as remove:await env['handle_quiz_delete_execute'](call)
        remove.assert_awaited_once_with(self.qid)
    async def test_orphan_prefix_only_after_quiz_deletion_and_failure_retries(self):
        store=types.SimpleNamespace(list=AsyncMock(side_effect=[[{'name':'hash'}],[{'name':'question.png'}],[]]),remove=AsyncMock())
        with patch.object(cleanup,'bucket',return_value=store):
            self.assertFalse(await cleanup.delete_runner_prefix(self.qid));store.list.assert_not_awaited()
            self.db.rows['quizzes']=[];self.db.rows['runner_image_gc']=[{'quiz_id':self.qid}]
            self.assertTrue(await cleanup.delete_runner_prefix(self.qid))
        store.remove.assert_awaited_once_with([f'runner/{self.qid}/hash/question.png'])
        self.assertFalse(self.db.rows['runner_image_gc'])


if __name__ == '__main__': unittest.main()
