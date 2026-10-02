"""
services/flow_state.py
==============================================================================
بناء FSMContext يدوياً لمستخدم/محادثة معيّنة خارج سياق رسالة تيليجرام عادية
(مثلاً من endpoint استقبال ويب في webhook_server.py، أو لاستئناف كويز من خلفية).

كانت سابقاً دالة خاصة `_build_state_for_chat` داخل handlers/audio.py، وكانت
handlers/files.py و handlers/quiz_runner.py تستوردانها من هناك - مما أنشأ دورة
استيراد (quiz_runner -> audio -> files -> quiz_runner) كانت تُخفى بـ imports داخل الدوال.
نقلها لخدمة مستقلة (لا تستورد أي handler) يكسر الدورة نهائياً بلا أي تغيير بالسلوك.

يستخدم نفس RedisStorage المُهيّأ بـ config.py (dp.storage)، فالحالة متوافقة 100%
مع بقية تدفقات البوت العادية.
==============================================================================
"""

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from config import bot, dp


def build_state_for_chat(chat_id: int, user_id: int) -> FSMContext:
    """
    يبني FSMContext يدوياً لمستخدم/محادثة معيّنة (نفس منطق الدالة الأصلية بلا أي تغيير).
    بعد ما تخلص الدالة المستدعية، أي زر يضغطه الطالب من نفس اللوحة الحالية بيشتغل
    عادي بدون أي تعديل عليه.
    """
    key = StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=user_id)
    return FSMContext(storage=dp.storage, key=key)
