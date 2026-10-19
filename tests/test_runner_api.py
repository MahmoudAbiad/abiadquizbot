"""API access, optimistic concurrency and isolation tests with an in-memory DB double."""
import copy
import sys
import types
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from services import runner_api as api
from services.runner_core import build_questions, sign_launch
from uuid import uuid4

class Query:
    def __init__(self,db,table):self.db=db;self.table=table;self.filters=[];self.operation='select';self.values=None
    def select(self,*a):return self
    def eq(self,k,v):self.filters.append((k,v));return self
    def is_(self,k,v):self.filters.append((k,None));return self
    def limit(self,*a):return self
    def update(self,v):self.operation='update';self.values=v;return self
    def insert(self,v):self.operation='insert';self.values=v;return self
    def upsert(self,v,**kw):self.operation='upsert';self.values=v;return self
    async def execute(self):
        rows=self.db.rows.setdefault(self.table,[])
        if self.operation in ('insert','upsert'):
            if not any(r.get('quiz_id')==self.values.get('quiz_id') for r in rows):rows.append(copy.deepcopy(self.values))
            return types.SimpleNamespace(data=[copy.deepcopy(self.values)])
        selected=[r for r in rows if all(r.get(k)==v for k,v in self.filters)]
        if self.operation=='update':
            if self.db.conflict:return types.SimpleNamespace(data=[])
            for row in selected:row.update(copy.deepcopy(self.values))
        return types.SimpleNamespace(data=copy.deepcopy(selected))
class DB:
    def __init__(self,rows):self.rows=rows;self.conflict=False
    def table(self,name):return Query(self,name)

class APITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.qid=str(uuid4());self.rid=str(uuid4())
        q={'question':'Q','options':['a','b','c','d'],'correct_option_id':1,'estimated_time_seconds':30}
        self.quiz={'id':self.qid,'creator_id':7,'quiz_data':[q],'source_title':'Quiz'}
        self.row={'id':self.rid,'quiz_id':self.qid,'user_id':7,'questions':build_questions([q]),'status':'running','cursor':0,'lives':1,'score':0,'version':0,'issued_at':100,'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}
        self.db=DB({'quizzes':[self.quiz],'runner_rounds':[self.row],'runner_settings':[{'quiz_id':self.qid,'version':0,'manual_times':{},'ai_times':{},'lives':1,'ai_status':'new','uniform_seconds':None}]})
        self.patches=[patch.object(api,'db',return_value=self.db),patch.object(api,'identity',new=AsyncMock(return_value=7)),patch.dict(sys.modules,{'config':types.SimpleNamespace(bot=types.SimpleNamespace(token='secret'),ADMIN_ID=99)})]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
    def request(self,**kwargs):return api.RoundRequest(init_data='valid',quiz_id=self.qid,round_id=self.rid,**kwargs)
    async def test_owner_and_shared_token_access(self):
        req=self.request()
        self.assertTrue((await api.access(req,7))[1])
        with self.assertRaises(HTTPException) as err:await api.access(req,8)
        self.assertEqual(err.exception.status_code,403)
        req.launch=sign_launch(self.qid,8,'secret')
        self.assertFalse((await api.access(req,8))[1])
        with self.assertRaises(HTTPException):await api.access(req,8,manage=True)
    async def test_favorite_access_is_user_scoped(self):
        self.db.rows['favorite_quizzes']=[{'quiz_id':self.qid,'user_id':8,'favorite_id':'f'}]
        await api.access(self.request(),8)
        with self.assertRaises(HTTPException):await api.access(self.request(),9)
    async def test_round_cannot_be_read_by_other_user(self):
        with self.assertRaises(HTTPException):await api.round_row(self.request(),8)
    async def test_expired_round(self):
        self.row['expires_at']='2000-01-01T00:00:00+00:00'
        with self.assertRaises(HTTPException) as err:await api.round_row(self.request(),7)
        self.assertEqual(err.exception.status_code,410)
    async def test_no_answers_or_explanations_in_question(self):
        response=await api.question(self.request())
        self.assertNotIn('correct_id',response);self.assertNotIn('source',response)
        self.assertNotIn('explanation',response)
        self.assertEqual(len(response['options']),3)
    async def test_answer_idempotence_and_server_score(self):
        with patch('services.runner_core.time.time',return_value=110):
            first=await api.answer(self.request(option_id=1));second=await api.answer(self.request(option_id=1))
        self.assertEqual(first,second);self.assertEqual(first['score'],100);self.assertEqual(self.row['version'],1)
    async def test_concurrent_answer_rejected(self):
        self.db.conflict=True
        with self.assertRaises(HTTPException) as err:
            await api.answer(self.request(option_id=1))
        self.assertEqual(err.exception.status_code,409)
    async def test_old_version_rejected(self):
        with self.assertRaises(HTTPException):await api.answer(self.request(version=7,option_id=1))
    async def test_manual_settings_dont_touch_ai_or_quiz(self):
        before=copy.deepcopy(self.quiz)
        self.db.rows['runner_settings'][0]['ai_times']={'old':40}
        payload=api.SettingsRequest(init_data='valid',quiz_id=self.qid,version=0,uniform_seconds=80,lives=2)
        await api.save_settings(payload)
        self.assertEqual(self.db.rows['runner_settings'][0]['ai_times'],{'old':40});self.assertEqual(self.quiz,before)
        with self.assertRaises(HTTPException):await api.save_settings(payload)
    async def test_stale_question_key_rejected(self):
        payload=api.SettingsRequest(init_data='valid',quiz_id=self.qid,version=0,manual_times={'invalid':40})
        with self.assertRaises(HTTPException):await api.save_settings(payload)
    async def test_ai_done_does_not_repeat_request(self):
        self.db.rows['runner_settings'][0]['ai_status']='done'
        self.assertEqual(await api.estimate(self.request()),{'saved':True})
    async def test_ai_running_does_not_repeat_request(self):
        self.db.rows['runner_settings'][0]['ai_status']='running'
        with self.assertRaises(HTTPException) as err:await api.estimate(self.request())
        self.assertEqual(err.exception.status_code,409)

if __name__=='__main__':unittest.main()
