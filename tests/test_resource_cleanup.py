import asyncio
import os
from pathlib import Path
import tempfile
import types
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

from reliability_support import load_functions
from test_generation_regressions import State


class ResourcesTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_cleanup_preserves_live_session_files(self):
        import time
        from services.upload_validation import cleanup_stale_downloads
        with tempfile.TemporaryDirectory() as d:
            Path(d+'/live').write_bytes(b'live');Path(d+'/stale').write_bytes(b'stale')
            old=time.time()-49*3600;os.utime(d+'/stale',(old,old))
            cleanup_stale_downloads(d)
            self.assertEqual(os.listdir(d),['live'])

    async def test_local_image_total_guard_and_per_file_limit(self):
        with tempfile.TemporaryDirectory() as d:
            state=State();state.status=None
            downloaded=[]
            async def download(obj,dest,max_size_bytes):
                downloaded.append(max_size_bytes)
                if max_size_bytes < 15:return False
                Path(dest).write_bytes(b'x'*15);return True
            env=load_functions('handlers/files.py',{'process_web_uploaded_images'},
                asyncio=asyncio,os=os,uuid=uuid,build_state_for_chat=lambda *a:state,
                QuizState=types.SimpleNamespace(answering_quiz='answering',processing_web_file='web',processing_file_quiz='quiz'),
                PENDING_REQUEST_STATES=[],bot=types.SimpleNamespace(send_message=AsyncMock(return_value=types.SimpleNamespace(delete=AsyncMock()))),
                ensure_directory_exists=lambda *a:None,DOWNLOADS_DIR=d,MAX_IMAGE_WEB_UPLOAD_COUNT=50,MAX_IMAGE_WEB_UPLOAD_TOTAL_SIZE=500,MAX_IMAGE_WEB_UPLOAD_SIZE_PER_IMAGE=15,
                download_file_temp_to_file=download,validate_image=lambda p:None,delete_file_temp_batch=AsyncMock(),
                safe_file_cleanup=lambda p:os.remove(p) if os.path.exists(p) else None,
                _reject_web_upload=AsyncMock(),logger=Mock(),log_error=Mock())
            await env['process_web_uploaded_images'](1,1,[f'1/{i}.jpg' for i in range(34)])
            self.assertEqual(downloaded,[15]*33+[5])
            self.assertEqual(os.listdir(d),[])
            env['_reject_web_upload'].assert_awaited_once()

    async def test_pdf_parts_cleaned_on_split_failure_and_budget(self):
        import fitz
        env=load_functions('helpers/gemini_helper/media.py',{'get_pdf_page_count_sync','split_pdf_into_three_sync','split_pdf_without_orphans'},
            asyncio=asyncio,Path=Path,os=os,uuid=uuid,fitz=fitz)
        with tempfile.TemporaryDirectory() as d:
            source=fitz.open()
            for _ in range(9):source.new_page()
            source.save(d+'/source.pdf');source.close()
            normal=env['split_pdf_into_three_sync'](d+'/source.pdf')
            self.assertEqual([len(fitz.open(p)) for p in normal],[3,3,3])
            for p in normal:os.remove(p)
            real_open=fitz.open;calls=0
            def failing_open(*args,**kwargs):
                nonlocal calls
                calls+=1
                if calls==4:raise ValueError('second chunk failed')
                return real_open(*args,**kwargs)
            with patch.object(fitz,'open',side_effect=failing_open):
                with self.assertRaises(ValueError):env['split_pdf_into_three_sync'](d+'/source.pdf')
            self.assertEqual(os.listdir(d),['source.pdf'])
            with patch('services.upload_validation.MAX_PDF_PARTS_BYTES',1):
                with self.assertRaises(Exception):env['split_pdf_into_three_sync'](d+'/source.pdf')
            self.assertEqual(os.listdir(d),['source.pdf'])

    async def test_cancelled_split_waits_for_thread_and_removes_parts(self):
        import threading
        started=threading.Event();finish=threading.Event()
        with tempfile.TemporaryDirectory() as d:
            source=d+'/source.pdf';Path(source).write_bytes(b'source')
            def split(path):
                started.set();finish.wait(timeout=5)
                part=d+'/part.pdf';Path(part).write_bytes(b'part');return [part]
            env=load_functions('helpers/gemini_helper/media.py',{'split_pdf_without_orphans'},asyncio=asyncio,os=os,split_pdf_into_three_sync=split)
            task=asyncio.create_task(env['split_pdf_without_orphans'](source))
            await asyncio.to_thread(started.wait,5);task.cancel();finish.set()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual(os.listdir(d),['source.pdf'])

    async def test_cancelled_download_waits_for_thread_cleanup(self):
        import threading
        started=threading.Event();finish=threading.Event()
        with tempfile.TemporaryDirectory() as d:
            def download(*args):
                started.set();finish.wait(timeout=5);Path(args[2]).write_bytes(b'partial');return True
            env=load_functions('helpers/r2_helper.py',{'_download_without_orphan'},asyncio=asyncio,os=os,_download_object_sync=download)
            task=asyncio.create_task(env['_download_without_orphan']('b','key',d+'/out',100))
            await asyncio.to_thread(started.wait,5);task.cancel();finish.set()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual(os.listdir(d),[])

    async def test_existing_user_on_unique_insert_race_preserves_balance(self):
        class UniqueError(Exception):code='23505'
        row={'user_id':1,'free_points':3,'paid_points':4}
        query=Mock();query.insert.return_value=query;query.select.return_value=query;query.eq.return_value=query
        query.execute=AsyncMock(side_effect=[UniqueError(),types.SimpleNamespace(data=[row])])
        env=load_functions('helpers/supabase_helper/users.py',{'_add_new_user','_balance_payload'},
            supabase=types.SimpleNamespace(table=lambda *a:query,rpc=lambda *a:query),get_setting=AsyncMock(return_value=2),
            _check_daily_renewal=AsyncMock(return_value={'points':7}),logger=Mock(),log_error=Mock())
        result=await env['_add_new_user'](1,'name','first','last',None,'2026-10-02')
        self.assertEqual(result,{'points':7})
        env['_check_daily_renewal'].assert_awaited_once_with(1,row,'2026-10-02')

