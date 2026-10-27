"""No live Telegram sends: delivery recovery and private settings behavior."""
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiogram import types
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendMessage
from services.daily_renewal import renewal_step, report_text, renewal_keyboard
from reliability_support import load_functions


class Client:
    def __init__(self, claim=None, report=None):
        self.responses = {"daily_renewal_claim": claim, "daily_renewal_report_claim": report}
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, deepcopy(params)))
        return SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(data=self.responses.get(name))))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Client(dict(day="2026-10-28", user_id=42, token="token", amount=50))
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.method = SendMessage(chat_id=42, text="test")

    def finished(self):
        return [p for name,p in self.client.calls if name=="daily_renewal_finish"]

    async def test_success_records_delivery_and_inline_opt_out(self):
        await renewal_step(self.client, self.bot, 999)
        self.assertEqual(self.finished()[0]["p_status"], "sent")
        self.assertIn("12 منتصف الليل بتوقيت دمشق",self.bot.send_message.await_args.args[1])
        button=renewal_keyboard().inline_keyboard[0][0]
        self.assertEqual(button.callback_data,"renewal:off")
        self.assertIn("الإعدادات",button.text)

    async def test_only_explicit_block_is_classified_as_block(self):
        for error, expected in [
            (TelegramForbiddenError(method=self.method,message="Forbidden: bot was blocked by the user"),"blocked"),
            (TelegramForbiddenError(method=self.method,message="Forbidden: user is deactivated"),"unavailable"),
            (TelegramBadRequest(method=self.method,message="chat not found"),"failed"),
            (TimeoutError(),"uncertain"),
        ]:
            self.client.calls.clear()
            self.bot.send_message.side_effect=error
            await renewal_step(self.client,self.bot,999)
            self.assertEqual(self.finished()[0]["p_status"],expected)

    async def test_flood_wait_requeues_and_defers_reports(self):
        self.bot.send_message.side_effect=TelegramRetryAfter(method=self.method,message="flood",retry_after=30)
        self.assertEqual(await renewal_step(self.client,self.bot,999),31)
        self.assertEqual(self.finished()[0]["p_retry"],31)
        self.assertEqual(self.finished()[0]["p_status"],"pending")
        self.assertFalse(any(n=="daily_renewal_report_claim" for n,_ in self.client.calls))

    async def test_report_is_sent_only_for_claimed_completed_run(self):
        report=dict(day="2026-10-28",sent=4,enabled=7,disabled=2,failed=2,uncertain=1,excluded=3,skipped=1,token="report")
        self.client=Client(report=report)
        await renewal_step(self.client,self.bot,999)
        self.assertEqual(self.bot.send_message.await_args.args[0],999)
        self.assertEqual(self.bot.send_message.await_args.args[1],report_text(report))
        self.assertEqual(self.client.calls[-1][1]["p_status"],"sent")

    async def test_interruption_after_claim_never_marks_sent(self):
        import asyncio
        self.bot.send_message.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await renewal_step(self.client,self.bot,999)
        self.assertEqual(self.finished(),[])


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_panel_uses_persisted_preference_and_valid_home_button(self):
        for enabled,callback in [(True,"renewal:off"),(False,"renewal:on")]:
            client=SimpleNamespace()
            client.table=lambda *_:client
            client.select=lambda *_:client
            client.eq=lambda *_:client
            client.single=lambda:client
            client.execute=AsyncMock(return_value=SimpleNamespace(data={"renewal_notifications":enabled}))
            env=load_functions("handlers/settings.py",["settings_panel"],supabase=client,types=types)
            text,kb=await env["settings_panel"](42)
            self.assertIn("توقيت دمشق",text)
            self.assertEqual(kb.inline_keyboard[0][0].callback_data,callback)
            self.assertEqual(kb.inline_keyboard[1][0].callback_data,"student_home")


if __name__=="__main__":
    unittest.main()
