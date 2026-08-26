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
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError
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
    # 🆕 waiting_for_generation_confirm أُزيلت: شاشة التأكيد صارت مدمجة داخل waiting_for_count
    # نفسها (خطوة واحدة بدل خطوتين - راجع handlers/files.py get_question_count_keyboard).
    waiting_for_translation_choice = State()  # 🆕 اختيار "مترجمة/بدون ترجمة" عند اكتشاف محتوى إنجليزي
    waiting_for_quiz_options = State()        # 🆕 شاشة اختيار نوع الأسئلة + الصعوبة (رسالة واحدة، تحديثات متتالية)
    waiting_for_custom_question_type = State()  # 🆕 استقبال تفضيل نوع الأسئلة النصي الحر من الطالب
    waiting_for_audio_confirm = State()     # 🆕 انتظار تأكيد الطالب (إقرار الحقوق + المدة والتكلفة) قبل خصم أي نقاط أو بدء التفريغ الفعلي
    waiting_for_audio_action = State()      # 🆕 انتظار قرار الطالب بعد تفريغ المحاضرة الصوتية (تلخيص/تصدير/كويز/إرسال النص)
    processing_audio = State()              # 🆕 قفل مؤقت أثناء تحميل/تفريغ محاضرة صوتية قائمة، لمنع معالجة مضاعفة لو وصل مقطع صوتي ثانٍ قبل انتهاء الأول
    processing_web_file = State()           # 🆕 نظير processing_audio لملف/ألبوم صور مرفوع عبر صفحة الويب قيد التحميل/الفحص
    processing_file_quiz = State()          # 🆕 قفل مؤقت أثناء تنفيذ execute_quiz_generation_workflow الفعلي (بعد ضغط "ابدأ التوليد") -
                                             # يمنع أي رسالة/ملف جديد وصل بهالأثناء من حذف ملفات الطلب الجاري معالجته حالياً عند Gemini
                                             # (كانت هاي الحالة تضل waiting_for_count طول التوليد، وهي جوا PENDING_REQUEST_STATES، فأي
                                             # إرسال ثانٍ للملف كان يُفسَّر كـ"استبدال طلب معلّق" ويحذف الملف وهو لسا قيد الرفع لـ Gemini).
    
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
    redis_kwargs = {"ssl_cert_reqs": None} if redis_url.startswith("rediss://") else {}
    # 🩹 إصلاح خلل حقيقي: مزوّد Redis المُستخدَم فعلياً (Upstash عبر Heroku) بيقفل أي
    # اتصال خامل (idle) من جهته بعد فترة قصيرة دون أي إشعار للعميل - فأول عملية Redis
    # بعد فترة هدوء (مثلاً مستخدم ما تفاعل مع البوت لدقائق) كانت ترمي مباشرة
    # ConnectionError('Connection lost') لأن الـ connection pool كان يعيد استخدام اتصال
    # ميت دون علم. هيدا كان يُسقط أي تحديث تيليجرام بالكامل (dp.feed_update بيفشل، والبوت
    # أصلاً رد على تيليجرام بـ 200 OK فوراً قبل المعالجة، فتيليجرام ما بيعيد إرسالها إطلاقاً -
    # يعني رسالة المستخدم بتضيع نهائياً بصمت من غير أي رد أو خطأ ظاهر له).
    # الحل: health_check_interval بيخلي المكتبة تفحص كل اتصال دورياً وتستبدله لو ميت *قبل*
    # الاستخدام الفعلي، وsocket_keepalive بيقلل احتمال انقطاعه بالأساس، وretry_on_error
    # بيعطي محاولة تلقائية إضافية فوراً (بنفس الاتصال الجديد من الـ pool) لو انقطع رغم كل هيك.
    redis_client = Redis.from_url(
        redis_url,
        socket_keepalive=True,
        health_check_interval=30,
        retry_on_error=[RedisConnectionError, RedisTimeoutError, ConnectionResetError],
        retry=Retry(ExponentialBackoff(base=0.1, cap=1), retries=2),
        **redis_kwargs,
    )
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
            types.BotCommand(command="help", description="🎬 كيف يعمل البوت؟ (دليل سريع)"),
            types.BotCommand(command="favorites", description="⭐ قائمتي المفضلة المنظمة"),
            types.BotCommand(command="channel", description="📢 قناة التحديثات والأخبار"),
            types.BotCommand(command="support", description="💬 التواصل مع الدعم الفني"),
        ]
        await bot_instance.set_my_commands(student_commands, scope=types.BotCommandScopeDefault())
        
        # أوامر الآدمن الخاصة (تظهر للآدمن فقط)
        if ADMIN_ID != 0:
            admin_commands = [
                types.BotCommand(command="start", description="🔄 تشغيل البوت وعرض الرصيد"),
                types.BotCommand(command="help", description="🎬 كيف يعمل البوت؟ (دليل سريع)"),
                types.BotCommand(command="favorites", description="⭐ قائمتي المفضلة المنظمة"),
                types.BotCommand(command="admin", description="⚙️ لوحة تحكم الإدارة"),
                types.BotCommand(command="charge", description="💰 شحن نقاط لطالب"),
            ]
            await bot_instance.set_my_commands(
                admin_commands,
                scope=types.BotCommandScopeChat(chat_id=ADMIN_ID)
            )
        logger.info("Bot commands menus set successfully.")
    except Exception as e:
        logger.error(f"Error setting bot commands menu: {e}")