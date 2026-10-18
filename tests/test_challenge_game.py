"""Pure rules + Telegram boundary regression tests; no credentials/network required."""
import datetime as dt
import json
import sys
import unittest
from pathlib import Path
from uuid import UUID, uuid4
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reliability_support import load_functions
from services.challenge_game import load_rules, question_score, stars_for, tower_progress
from services.challenge_notifications import notification_text
from services.challenge_presentation import PhotoText, source_image, image_option_label


class RuleTests(unittest.TestCase):
    def setUp(self): self.r=load_rules()
    def test_star_boundaries(self):
        for correct,stars in [(0,0),(5,0),(6,1),(7,1),(8,2),(9,2),(10,3)]:
            self.assertEqual(stars_for(correct,10,self.r),stars)
        self.assertEqual(stars_for(0,0,self.r),0)
    def test_time_boundary_and_wrong(self):
        for elapsed in [-1,30,31]: self.assertEqual(question_score(True,elapsed,self.r),0)
        self.assertEqual(question_score(False,0,self.r),0)
        self.assertEqual(question_score(True,0,self.r),100)
        self.assertEqual(question_score(True,29.999,self.r),95)
    def test_accuracy_beats_speed(self):
        for c in range(1,11):
            self.assertGreater(c*question_score(True,29.9,self.r),(c-1)*question_score(True,0,self.r))
    def test_empty_actions_never_level_up(self):
        for xp in [0,100,100000]: self.assertEqual(tower_progress(xp,0,0,self.r)["level"],1)
    def test_mastery_cap(self):
        self.assertEqual(tower_progress(100000,60,100,self.r)["level"],12)
        self.assertEqual(tower_progress(100000,100,100,self.r)["level"],20)
        self.assertEqual(tower_progress(100,10,10,self.r)["level"],2)
    def test_notification_escaping(self):
        m={"labels":{"attacker":"<b>x</b>","subject":"A&B"}}
        self.assertIn("&lt;b&gt;", notification_text("attack_started",m))


class ScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aiogram import types
        cls.f=load_functions("handlers/challenge_game.py",["button","attack_screen"],UUID=UUID,dt=dt,types=types, PhotoText=PhotoText, source_image=source_image, image_option_label=image_option_label)
    def state(self):
        return {"id":str(uuid4()),"attacker_id":1,"status":"in_progress","index":0,"total":10,
                "question":{"text":"س"*2000,"options":["خ"*500 for _ in range(10)]},
                "deadline":"2026-10-04T20:00:30Z","server_now":"2026-10-04T20:00:00Z"}
    def test_long_questions_and_options_fit_and_are_complete(self):
        state=self.state()
        for page in range(5):
            text, rows=self.f["attack_screen"](state,page)
            self.assertLessEqual(len(text),4096)
            self.assertIn(state["question"]["text"],text)
            for row in rows:
                for b in row:
                    if b.callback_data: self.assertLessEqual(len(b.callback_data.encode()),64)
            self.assertIn(f"{page*2+1}) "+"خ"*500,text)
    def test_no_answer_key_in_screen(self):
        s=self.state();s["question"]["correct_option_id"]=8
        text,rows=self.f["attack_screen"](s)
        self.assertNotIn("correct_option",text)
        self.assertEqual(rows[0][0].text,"الإجابة 1")
    def test_callback_max_user_ids(self):
        self.f["button"]("x", f"cg:v:{2**63-1}:{uuid4().hex}:99")
    def test_completed_screen(self):
        s=self.state();s.update(status="completed",labels={"subject":"فيزياء","unit":"حركة"},result={"stars":3,"correct":10,"score":995,"xp":100,"trophies":15})
        text,rows=self.f["attack_screen"](s)
        self.assertIn("⭐⭐⭐",text);self.assertIn("+100 XP",text)


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_actor_is_from_telegram_not_payload(self):
        from aiogram import types
        repo=Mock();repo.advance=AsyncMock(return_value={"id":"m","index":1,"status":"in_progress"})
        show=AsyncMock();c=Mock();c.answer=AsyncMock();c.from_user.id=77
        mid=uuid4();c.data=f"cg:a:{mid.hex}:0:1";c.id="telegram-retry-identifier"
        f=load_functions("handlers/challenge_game.py",["game_callback"],UUID=UUID,types=types,
                         repo=repo,show_attack=show,logger=Mock(),ERRORS={})
        await f["game_callback"](c)
        repo.advance.assert_awaited_once_with(str(mid),77,"answer",0,1,c.id)
    async def test_edit_not_modified_does_not_send_duplicate(self):
        from aiogram.exceptions import TelegramBadRequest
        m=Mock();m.edit_text=AsyncMock(side_effect=TelegramBadRequest(method=Mock(),message="message is not modified"));m.answer=AsyncMock()
        f=load_functions("handlers/challenge_game.py",["render"],TelegramBadRequest=TelegramBadRequest,keyboard=lambda r:r)
        self.assertIs(await f["render"](m,"text",[]),m)
        m.answer.assert_not_awaited()
    async def test_old_message_fallback(self):
        from aiogram.exceptions import TelegramBadRequest
        m=Mock();m.edit_text=AsyncMock(side_effect=TelegramBadRequest(method=Mock(),message="message can't be edited"));m.answer=AsyncMock()
        f=load_functions("handlers/challenge_game.py",["render"],TelegramBadRequest=TelegramBadRequest,keyboard=lambda r:r)
        await f["render"](m,"text",[])
        m.answer.assert_awaited_once()
