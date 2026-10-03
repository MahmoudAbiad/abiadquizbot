import asyncio
import contextvars
import html
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock
from services.ai_audit import input_channel_label, build_audio_audit_metadata
from reliability_support import load_functions


class AIAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_log_displays_audio_and_per_record_input_channel(self):
        rows=[]
        for event,channel in [('audio_transcription_completed','web_upload'),('quiz_generated','telegram'),('cached_quiz_used',None),('audio_summary_completed','telegram')]:
            rows.append({'event_type':event,'user_id':1,'time_str':'now','user':{},'metadata':{
                'input_channel':channel,'ai_provider':'gemini','ai_model':'m','duration_minutes':90,
                'generation_seconds':2,'questions_generated':5,'cost':1}})
        edited=AsyncMock()
        env=load_functions('handlers/admin/ai_control.py',{'show_quiz_generation_log'},
            admin_get_quiz_generation_log=AsyncMock(return_value=rows),QUIZ_GEN_LOG_FETCH_LIMIT=200,
            QUIZ_GEN_LOG_DAYS=7,QUIZ_GEN_LOG_PAGE_SIZE=10,get_ai_model_pricing=AsyncMock(return_value={}),
            _estimate_quiz_cost=lambda *a:(None,False),input_channel_label=input_channel_label,
            html_escape=html.escape,safe_edit_text=edited,log_error=Mock(),logger=Mock(),
            types=types.SimpleNamespace(InlineKeyboardButton=lambda **kw:kw,InlineKeyboardMarkup=lambda **kw:kw))
        call=types.SimpleNamespace(data='admin_quiz_gen_log',message=Mock(),answer=AsyncMock())
        await env['show_quiz_generation_log'](call)
        edited.assert_awaited_once()
        text=edited.call_args.args[1]
        for expected in ['تفريغ صوتي','تلخيص محاضرة صوتية','90 دقيقة صوت','صفحة رفع الويب','محادثة تلغرام','غير مسجَّل']:
            self.assertIn(expected,text)
        env['log_error'].assert_not_called()

    async def test_audio_metadata_preserves_real_model_tokens_and_origin(self):
        meta=build_audio_audit_metadata({'provider':'gemini','model':'model-A','input_tokens':20,'output_tokens':30,'thoughts_tokens':4,'total_tokens':54},
            'audio_transcription','web_upload',duration_minutes=60,cost=2,truncated=True,duration_seconds=9)
        self.assertEqual(meta['ai_model'],'model-A')
        self.assertEqual(meta['total_tokens'],54)
        self.assertEqual(meta['input_channel'],'web_upload')
        self.assertEqual(meta['generation_seconds'],9)
        self.assertTrue(meta['truncated'])
        missing=build_audio_audit_metadata(None,'audio_summary',None,duration_minutes=1,cost=0)
        self.assertNotIn('total_tokens',missing)
        self.assertEqual(input_channel_label(None),'غير مسجَّل')

    async def test_text_cascade_exports_metadata_without_changing_return_api(self):
        model=contextvars.ContextVar('model',default=None)
        usage=contextvars.ContextVar('usage',default=None)
        metadata=contextvars.ContextVar('metadata',default=None)
        response=types.SimpleNamespace(text='transcript',candidates=[],usage_metadata=None)
        client=types.SimpleNamespace(aio=types.SimpleNamespace(models=types.SimpleNamespace(generate_content=AsyncMock(return_value=response))))
        def record(response):usage.set({'input_tokens':10,'output_tokens':20,'total_tokens':30});return 30
        async def cascade(attempt):
            result=await attempt(client,0,'audio-model',1,2)
            model.set({'provider':'gemini','model':'audio-model','cascade_rank':1,'cascade_total':2})
            return result
        env=load_functions('helpers/gemini_helper/generic.py',{'generate_text_with_cascade'},asyncio=asyncio,time=time,
            _last_model_used_var=model,_last_token_usage_var=usage,_last_generation_metadata_var=metadata,
            _execute_cascade=cascade,_record_token_usage=record,get_ai_request_timeout=AsyncMock(return_value=1))
        result=await env['generate_text_with_cascade'](['audio'])
        self.assertEqual(result,('transcript',30,False))
        self.assertEqual(metadata.get()['model'],'audio-model')
        self.assertEqual(metadata.get()['total_tokens'],30)
        env['_execute_cascade']=AsyncMock(return_value=None)
        self.assertIsNone(await env['generate_text_with_cascade'](['bad']))
        self.assertIsNone(metadata.get())

    async def test_admin_query_includes_audio_events(self):
        query=Mock()
        for name in ['select','in_','gte','neq','order','limit']:getattr(query,name).return_value=query
        query.execute=AsyncMock(return_value=types.SimpleNamespace(data=[]))
        env=load_functions('helpers/supabase_helper/analytics.py',{'admin_get_quiz_generation_log'},datetime=__import__('datetime'),
            supabase=types.SimpleNamespace(table=lambda *a:query),ADMIN_ID=0,log_error=Mock(),logger=Mock())
        await env['admin_get_quiz_generation_log']()
        events=query.in_.call_args.args[1]
        self.assertIn('audio_transcription_completed',events)
        self.assertIn('audio_summary_completed',events)
        self.assertIn('quiz_generated',events)
