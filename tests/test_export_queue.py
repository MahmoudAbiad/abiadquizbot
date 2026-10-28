import ast
import asyncio
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


def load_functions():
    tree = ast.parse(Path('handlers/export.py').read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name in ('_generate_and_send', '_build_export')]
    # Annotations use app types; strip them for this isolated handler test.
    for node in nodes:
        node.returns = None
        for arg in node.args.args:
            arg.annotation = None
    ns = dict(offload=SimpleNamespace(ENABLED=False, remote_build_quiz=AsyncMock(return_value=None)), asyncio=asyncio, _PENDING_EXPORT_USERS=set(), EXPORT_SLOT=asyncio.Lock(),
              HEAVY_PROCESSING_SEMAPHORE=asyncio.Semaphore(2), RENDER_SEMAPHORE=asyncio.Semaphore(3),
              MAX_PENDING_EXPORTS=8, _safe_answer=AsyncMock(), STYLE_CODE_TO_NAME={'s': 'simple'},
              MSG_NO_QUESTIONS='empty', MSG_GENERATING='generating', MSG_FAILED='failed',
              release_memory_to_os=lambda: None, ExportError=ValueError, TelegramBadRequest=RuntimeError,
              BufferedInputFile=lambda data, filename: data, build_export_filename=lambda title, fmt: title,
              spawn=lambda coro: coro.close(), log_usage_event=AsyncMock(),
              log_error=lambda *a, **kw: None, logger=None)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'handlers/export.py', 'exec'), ns)
    return ns


def call(user):
    status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
    return SimpleNamespace(from_user=SimpleNamespace(id=user), message=SimpleNamespace(
        answer=AsyncMock(return_value=status), answer_document=AsyncMock()))


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_excel_stays_local_and_preserves_student_variant(self):
        ns = load_functions()
        ns['offload'].ENABLED = True
        builder = ns['build_quiz_xlsx'] = Mock(return_value=b'excel')
        a = call(1)
        await ns['_generate_and_send'](a, [{'question': 'sample'}], 'title', 'xlsx', 's', False)
        builder.assert_called_once_with('title', [{'question': 'sample'}], 'simple', False)
        ns['offload'].remote_build_quiz.assert_not_awaited()
        self.assertEqual(a.message.answer_document.call_args.kwargs['document'], b'excel')
        self.assertFalse(ns['_PENDING_EXPORT_USERS'])

    async def test_remote_export_preserves_student_variant_without_local_work(self):
        ns = load_functions()
        ns['offload'].ENABLED = True
        ns['offload'].remote_build_quiz.return_value = b'remote-docx'
        heavy = ns['HEAVY_PROCESSING_SEMAPHORE']
        await heavy.acquire(); await heavy.acquire()
        a = call(1)
        try:
            await asyncio.wait_for(ns['_generate_and_send'](
                a, [{'question': 'sample'}], 'title', 'docx', 's', include_answers=False), 2)
        finally:
            heavy.release(); heavy.release()
        ns['offload'].remote_build_quiz.assert_awaited_once_with(
            'docx', 'title', [{'question': 'sample'}], 'simple', False)
        self.assertEqual(a.message.answer_document.call_args.kwargs['document'], b'remote-docx')
        self.assertFalse(ns['_PENDING_EXPORT_USERS'])

    async def test_buttons_roundtrip_and_legacy(self):
        import re
        from aiogram import types
        tree = ast.parse(Path('keyboards.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'get_export_format_keyboard')
        ns = {'types': types}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'keyboards.py', 'exec'), ns)
        handler = ast.parse(Path('handlers/export.py').read_text())
        patterns = {}
        for n in handler.body:
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id in ('_FORMAT_RE', '_FAV_FORMAT_RE'):
                patterns[n.targets[0].id] = re.compile(ast.literal_eval(n.value.args[0]))
        for favorite in ('', '12345678-1234-1234-1234-123456789012'):
            kb = ns['get_export_format_keyboard']('a', favorite)
            regex = patterns['_FAV_FORMAT_RE' if favorite else '_FORMAT_RE']
            for row in kb.inline_keyboard:
                for button in row:
                    self.assertLessEqual(len(button.callback_data.encode()), 64)
                    if button.callback_data == 'export_back' or button.callback_data.startswith('fav_export_back_'):
                        continue
                    self.assertIsNotNone(regex.fullmatch(button.callback_data))
        self.assertIsNotNone(patterns['_FORMAT_RE'].fullmatch('export_pdf_s'))
        self.assertIsNotNone(patterns['_FAV_FORMAT_RE'].fullmatch('fav_export_docx_a_old-id'))

    async def test_shared_gate_and_duplicate(self):
        ns = load_functions()
        heavy = ns['HEAVY_PROCESSING_SEMAPHORE']
        await heavy.acquire(); await heavy.acquire()
        built = []
        ns['build_quiz_pdf'] = lambda *args: built.append(1) or b'pdf'
        a = call(1)
        first = asyncio.create_task(ns['_generate_and_send'](a, [{}], 'x', 'pdf', 's'))
        await asyncio.sleep(.03)
        self.assertFalse(built)
        await ns['_generate_and_send'](a, [{}], 'x', 'pdf', 's')
        self.assertEqual(a.message.answer.await_count, 1)
        heavy.release()
        await first
        heavy.release()
        self.assertEqual(built, [1])
        self.assertFalse(ns['_PENDING_EXPORT_USERS'])

    async def test_one_export_including_upload(self):
        ns = load_functions()
        uploaded = asyncio.Event()
        built = []
        ns['build_quiz_pdf'] = lambda *args: built.append(1) or b'pdf'
        a, b = call(1), call(2)
        async def upload(**kw):
            await uploaded.wait()
        a.message.answer_document.side_effect = upload
        first = asyncio.create_task(ns['_generate_and_send'](a, [{}], 'x', 'pdf', 's'))
        while not a.message.answer_document.await_count:
            await asyncio.sleep(.01)
        second = asyncio.create_task(ns['_generate_and_send'](b, [{}], 'x', 'pdf', 's'))
        await asyncio.sleep(.03)
        self.assertEqual(len(built), 1)
        uploaded.set()
        await asyncio.gather(first, second)
        self.assertEqual(len(built), 2)

    async def test_cancellation_waits_for_thread(self):
        ns = load_functions()
        started, finish = threading.Event(), threading.Event()
        def build(*args):
            started.set()
            finish.wait(3)
            return b'pdf'
        ns['build_quiz_pdf'] = build
        task = asyncio.create_task(ns['_generate_and_send'](call(1), [{}], 'x', 'pdf', 's'))
        while not started.is_set():
            await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(.03)
        self.assertTrue(ns['EXPORT_SLOT'].locked())
        task.cancel()
        await asyncio.sleep(.02)
        self.assertTrue(ns['EXPORT_SLOT'].locked())
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(ns['EXPORT_SLOT'].locked())
        self.assertFalse(ns['_PENDING_EXPORT_USERS'])

    async def test_full_queue_and_failure_cleanup(self):
        ns = load_functions()
        ns['_PENDING_EXPORT_USERS'].update(range(8))
        a = call(99)
        await ns['_generate_and_send'](a, [{}], 'x', 'pdf', 's')
        a.message.answer.assert_not_awaited()
        ns['_PENDING_EXPORT_USERS'].clear()
        def fail(*args):
            raise ValueError('bad formula')
        ns['build_quiz_pdf'] = fail
        await ns['_generate_and_send'](a, [{}], 'x', 'pdf', 's')
        self.assertFalse(ns['_PENDING_EXPORT_USERS'])
        self.assertFalse(ns['EXPORT_SLOT'].locked())

if __name__ == '__main__':
    unittest.main()
