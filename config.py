import os
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from dotenv import load_dotenv

load_dotenv()

# إعداد البوت والـ Dispatcher المركزي
bot = Bot(token=os.getenv("BOT_TOKEN"), request_timeout=300)
dp = Dispatcher(storage=MemoryStorage())

# جلب آيدي الآدمن من ملف البيئة
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

# المتغيرات الثابتة للملفات والصور
MAX_DOC_SIZE = 20 * 1024 * 1024  # 20MB
MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10MB
MAX_PDF_PAGES = 30  

# حالات الـ FSM الخاصة بالاختبارات
class QuizState(StatesGroup):
    waiting_for_count = State()
    answering_quiz = State()