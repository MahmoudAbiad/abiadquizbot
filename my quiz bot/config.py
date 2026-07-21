"""
Bot configuration and FSM states initialization.
Handles bot setup, dispatcher configuration, and Finite State Machine states.
Moved set_bot_commands here to prevent circular imports between main and webhook_server.
"""

import os
from aiogram import Bot, Dispatcher, types
# إضافة مكتبات Redis
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis
from aiogram.fsm.state import State, StatesGroup
from dotenv import load_dotenv
from logger import get_logger

# شحن متغيرات البيئة
load_dotenv()
logger = get_logger(__name__)

# ==================== FSM States ====================
class QuizState(StatesGroup):
    """حالات المستخدم المخصصة لإدارة تدفق الكويز"""
    waiting_for_count = State()  # انتظار تحديد عدد الأسئلة
    answering_quiz = State()     # مرحلة الإجابة على الأسئلة الحالية
    saving_favorite_name = State()  # انتظار اسم الكويز قبل حفظه في المفضلة
    saving_favorite_section_name = State()  # انتظار اسم القسم الجديد
    searching_favorites = State()  # انتظار كلمة البحث داخل المفضلة
    waiting_for_cache_decision = State() # معالجة قرار الكاش
    waiting_for_limit_decision = State()
    waiting_for_custom_name = State()       # استقبال الاسم المخصص
    waiting_for_new_section_title = State() # استقبال اسم القسم الجديد
    waiting_for_quiz_feedback = State()     # 🆕 استقبال ملاحظات وشكاوى الطلاب بنهاية الاختبار

# ==================== Bot Initialization Helpers ====================
def _get_bot_token() -> str:
    """جلب توكن البوت من ملف البيئة"""
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise ValueError("BOT_TOKEN is not set in .env file")
    return token

def _get_admin_id() -> int:
    """جلب معرف الآدمن من ملف البيئة"""
    try:
        admin_id = os.getenv("ADMIN_ID", "0")
        return int(admin_id)
    except ValueError:
        logger.warning("Invalid ADMIN_ID in .env, defaulting to 0")
        return 0

# ==================== Initialization ====================
try:
    # إعداد الاتصال بـ Redis
    # يقوم بقراءة REDIS_URL من بيئة Railway، وإذا لم يجده يستخدم المحلي للتطوير
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    redis_client = Redis.from_url(redis_url)
    
    # إعداد التخزين الدائم (RedisStorage)
    # جعل حالة المستخدم وبياناته المؤقتة تنتهي وتُحذف تلقائياً من Redis بعد 15 ساعة من خمول المستخدم
    storage = RedisStorage(redis=redis_client, state_ttl=86400, data_ttl=86400)
    
    bot = Bot(token=_get_bot_token())
    dp = Dispatcher(storage=storage) # ربط الـ Dispatcher بـ Redis
    
    ADMIN_ID: int = _get_admin_id()
    logger.info(f"Bot initialized successfully with Redis. Admin ID: {ADMIN_ID if ADMIN_ID else 'Not set'}")
except Exception as e:
    logger.critical(f"Failed to initialize bot components: {e}")
    raise

# ==================== Shared Functions (Fixes Circular Import) ====================
async def set_bot_commands(bot_instance: Bot):
    """
    إعداد القائمة الزرقاء للأوامر (Menu) في تلغرام.
    تم نقلها هنا لكي تستدعيها ملفات main و webhook_server بأمان دون تداخل.
    """
    try:
        # أوامر الطلاب الافتراضية
        student_commands = [
            types.BotCommand(command="start", description="🔄 تشغيل البوت وعرض الرصيد"),
            types.BotCommand(command="favorites", description="⭐ قائمتي المفضلة المنظمة"),
        ]
        await bot_instance.set_my_commands(student_commands, scope=types.BotCommandScopeDefault())
        
        # أوامر الآدمن الخاصة (تظهر للآدمن فقط)
        if ADMIN_ID != 0:
            admin_commands = [
                types.BotCommand(command="start", description="🔄 تشغيل البوت وعرض الرصيد"),
                types.BotCommand(command="charge", description="💰 شحن نقاط لطالب"),
                types.BotCommand(command="dbstats", description="📊 إحصائيات قاعدة البيانات"),
                types.BotCommand(command="searchuser", description="🔍 فحص بيانات طالب"),
                types.BotCommand(command="fetchall", description="👥 استعراض جميع الطلاب")
            ]
            await bot_instance.set_my_commands(
                admin_commands,
                scope=types.BotCommandScopeChat(chat_id=ADMIN_ID)
            )
        logger.info("Bot commands menus set successfully.")
    except Exception as e:
        logger.error(f"Error setting bot commands menu: {e}")