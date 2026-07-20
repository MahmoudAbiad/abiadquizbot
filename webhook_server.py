"""
Webhook configuration and FastAPI setup for Azure/Railway deployment.
Handles HTTP server setup safely with modern lifespan context and proper Pydantic validation.
"""

import os
import asyncio  
from fastapi import FastAPI, Request, HTTPException
from contextlib import asynccontextmanager
from aiogram.types import Update
from config import bot, dp
# نقوم باستيراد دالة الأوامر من config لتجنب الـ Circular Import مع ملف main
from config import set_bot_commands 
from logger import get_logger
from constants import WEBHOOK_PATH, WEBHOOK_PORT, TELEGRAM_WEBHOOK_SECRET
from supabase_helper import auto_cleanup_bad_quizzes  

logger = get_logger(__name__)

# ==================== Background Tasks ====================

async def scheduled_cleanup_loop():
    """
    مهمة خلفية دورية مستمرة تعمل كل 24 ساعة لفحص بنك الأسئلة المركزي،
    وحذف الاختبارات التي حصلت على ديسلايكات سلبية وتجاوزت مهلة الـ 48 ساعة.
    """
    while True:
        try:
            await auto_cleanup_bad_quizzes()
        except Exception as e:
            logger.error(f"Error inside the background scheduled cleanup task: {e}")
        # النوم والانتظار لمدة 24 ساعة كاملة (86400 ثانية)
        await asyncio.sleep(86400)

# ==================== Lifespan Context (Modern Event Handling) ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    إدارة أحداث بدء وإيقاف السيرفر بشكل آمن وبالمعايير الحديثة لـ FastAPI.
    """
    # [حدث الـ Startup]: يتم تنفيذه عند إقلاع السيرفر
    try:
        # تنظيف وجرف مجلد التحميلات بالكامل عند إقلاع السيرفر على Railway
        import shutil
        if os.path.exists("downloads"):
            shutil.rmtree("downloads")
        os.makedirs("downloads", exist_ok=True)
        
        webhook_url = os.getenv("WEBHOOK_URL")
        if webhook_url:
            full_webhook_url = f"{webhook_url.rstrip('/')}{WEBHOOK_PATH}"

            if not TELEGRAM_WEBHOOK_SECRET:
                raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is not configured")
            
            # تنظيف الـ Webhook القديم وتفعيل الجديد
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.set_webhook(
                url=full_webhook_url,
                drop_pending_updates=True,
                # إضافة poll_answer و poll لكي يرسل تلغرام أحداث الإجابات وحساب النقاط
                allowed_updates=["message", "callback_query", "poll_answer", "poll"],
                secret_token=TELEGRAM_WEBHOOK_SECRET,
            )
            print(f"✅ تم تفعيل Webhook بنجاح على: {full_webhook_url}")
            
            # تفعيل قائمة الأوامر
            await set_bot_commands(bot)
            
        # إطلاق وتثبيت مهمة التنظيف التلقائي في الخلفية فور نجاح الإقلاع
        asyncio.create_task(scheduled_cleanup_loop())
        print("🔄 تم جدولة ميزة الفلترة التلقائية وحذف الاختبارات الرديئة بنجاح كل 24 ساعة.")
            
    except Exception as e:
        print(f"❌ فشل تفعيل الـ Webhook أو المهام الدورية أثناء تشغيل السيرفر: {e}")
        logger.error(f"Failed to set webhook or tasks on startup: {e}")

    yield  # هنا يعمل السيرفر ويستقبل الطلبات...

    # [حدث الـ Shutdown]: يتم تنفيذه عند إغلاق السيرفر
    try:
        await bot.session.close()
        print("🛑 تم إغلاق جلسة البوت بنجاح.")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")


# إنشاء تطبيق FastAPI وتمرير الـ lifespan له
app = FastAPI(title="Quiz Maker Bot", version="2.0", lifespan=lifespan)

# ==================== Endpoints ====================

@app.get("/health")
async def health_check():
    return {"status": "ok", "bot": "running"}

@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    """
    استقبال التحديثات من تلغرام وتحليلها بشكل صحيح.
    """
    try:
        if TELEGRAM_WEBHOOK_SECRET:
            incoming_secret = request.headers.get("x-telegram-bot-api-secret-token")
            if incoming_secret != TELEGRAM_WEBHOOK_SECRET:
                raise HTTPException(status_code=403, detail="Forbidden")

        update_data = await request.json()
        
        # استخدام model_validate لتحليل البيانات المتداخلة بالكامل
        update = Update.model_validate(update_data)
        
        # تمرير التحديث للـ Dispatcher الخاص بـ aiogram
        await dp.feed_update(bot, update)
        
    except Exception as e:
        logger.error(f"Error processing webhook update: {e}", exc_info=True)

    # الرد دائماً بـ OK لتلغرام لمنع الـ Retry Loop
    return {"ok": True}

# ==================== Run Server Function ====================
def run_webhook_server():
    import uvicorn
    port = int(os.getenv("PORT", WEBHOOK_PORT))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")