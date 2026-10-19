import asyncio
import copy
import importlib.util
import os
import random
import unittest
from unittest.mock import patch, AsyncMock
from services.runner_core import build_questions, answer_transition, sign_launch, verify_launch, enabled
from services.quiz_timing import question_key, fallback_time

Q={'question':'ما ناتج $2+2$؟','options':['1','2','3','4'],'correct_option_id':3,'explanation':'نجمع العددين.','estimated_time_seconds':40,'is_math':True}

class CoreTests(unittest.TestCase):
    def test_options_and_immutability(self):
        original=copy.deepcopy(Q)
        lanes=set()
        for seed in range(50):
            q=build_questions([Q],rng=random.Random(seed))[0]
            self.assertEqual(len(q['options']),3)
            self.assertEqual(len({o['id'] for o in q['options']}),3)
            self.assertIn(3,[o['id'] for o in q['options']])
            for o in q['options']:self.assertEqual(o['text'],Q['options'][o['id']])
            lanes.add([o['id'] for o in q['options']].index(3))
        self.assertEqual(lanes,{0,1,2});self.assertEqual(Q,original)
    def test_bad_questions(self):
        for change in [{'correct_option_id':-1},{'correct_option_id':True},{'options':['x','y']},{'options':['','','x','y']}]:
            with self.assertRaises(ValueError):build_questions([{**Q,**change}])
    def test_manual_precedence_and_original(self):
        key=question_key(Q);cfg={'manual_times':{key:80},'uniform_seconds':60,'ai_times':{key:50}}
        self.assertEqual(build_questions([Q],cfg)[0]['seconds'],80)
        cfg['manual_times']={};self.assertEqual(build_questions([Q],cfg)[0]['seconds'],60)
        cfg['uniform_seconds']=None;self.assertEqual(build_questions([Q],cfg)[0]['seconds'],40)
        self.assertEqual(Q['estimated_time_seconds'],40)
    def test_hash_ignores_cache_but_tracks_edits(self):
        self.assertEqual(question_key(Q),question_key({**Q,'image_url':'new','estimated_time_seconds':100}))
        self.assertNotEqual(question_key(Q),question_key({**Q,'options':['a','b','c','d']}))
    def test_answer_rules(self):
        row={'questions':build_questions([Q,Q]),'cursor':0,'issued_at':100,'status':'running','score':0,'lives':1,'version':0}
        update,response=answer_transition(row,3,120)
        self.assertTrue(response['correct']);self.assertEqual(update['score'],100)
        self.assertEqual(update['cursor'],1);self.assertIsNone(update['issued_at'])
        update,response=answer_transition(row,3,145)
        self.assertFalse(response['correct']);self.assertEqual(update['status'],'caught')
        row['lives']=2;self.assertEqual(answer_transition(row,None,120)[0]['status'],'running')
        with self.assertRaises(ValueError):answer_transition(row,99,120)
    def test_signature_bound_to_user_quiz_and_expiry(self):
        token=sign_launch('q',7,'secret',100)
        self.assertTrue(verify_launch(token,'q',7,'secret',101))
        for args in [('x',7,'secret',101),('q',8,'secret',101),('q',7,'bad',101),('q',7,'secret',8000)]:
            self.assertFalse(verify_launch(token,*args))
        self.assertFalse(verify_launch('nonsense','q',7,'secret'))
    def test_environment_fails_closed(self):
        env={'RUNNER_ENABLED':'1','RUNNER_TEST_SUPABASE_REF':'testref','SUPABASE_URL':'https://testref.supabase.co','RENDER_GIT_BRANCH':'test'}
        with patch.dict(os.environ,env,clear=True):self.assertTrue(enabled())
        for change in [{'RENDER_GIT_BRANCH':'master'},{'SUPABASE_URL':'https://prod.supabase.co'},{'RUNNER_TEST_SUPABASE_REF':''},{'RUNNER_ENABLED':'0'}]:
            with patch.dict(os.environ,{**env,**change},clear=True):self.assertFalse(enabled())
    def test_legacy_schema_tolerates_missing_or_invalid_time(self):
        spec=importlib.util.spec_from_file_location('runner_schema','helpers/gemini_helper/schemas.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        for value in [None,'bad',True,-10,10000,float('inf')]:
            obj=m.QuizQuestion(**{**Q,'hint':'hint','estimated_time_seconds':value})
            self.assertGreaterEqual(obj.estimated_time_seconds,10);self.assertIsNone(obj.ai_estimated_time_seconds)
        obj=m.QuizQuestion(**{**Q,'hint':'hint'})
        self.assertEqual(obj.model_dump()['estimated_time_seconds'],40)
        self.assertEqual(obj.model_dump()['ai_estimated_time_seconds'],40)

class ImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_projection_retains_structures_and_no_options(self):
        import services.image_quiz_renderer as r
        question={**Q,'table':{'headers':['x'],'rows':[['1']]},'matrices':[{'rows':[['1','2'],['3','4']],'bracket':'bar'}]}
        original=copy.deepcopy(question)
        with patch.object(r,'render_question_image_async',new=AsyncMock(return_value=b'png')) as render:
            await r.render_game_image_async(question)
            projected=render.await_args.args[0]
            self.assertEqual(projected['options'],[]);self.assertEqual(projected['table'],question['table']);self.assertEqual(projected['matrices'],question['matrices'])
            render.return_value = r.render_question_image({'question':'4','options':[], '_game_part':True},0,1,False)
            await r.render_game_image_async(question,part='3')
            self.assertEqual(render.await_args.args[0],{'question':'4','options':[], '_game_part':True})
        self.assertEqual(question,original)
    async def test_real_math_image(self):
        from services.image_quiz_renderer import render_game_image_async
        q={**Q,'table':{'headers':['القيمة','التكرار'],'rows':[['2','3']]},'matrices':[{'rows':[['1','2'],['3','4']],'bracket':'bar','label':'A='}]}
        image=await render_game_image_async(q)
        self.assertTrue(image.startswith(b'\x89PNG'))
        from pathlib import Path
        Path('/tmp/runner-math.png').write_bytes(image)

if __name__=='__main__':unittest.main()
