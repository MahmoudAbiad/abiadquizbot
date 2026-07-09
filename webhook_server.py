"""
Webhook configuration and FastAPI setup for Azure/Railway deployment.
Handles HTTP server setup safely with modern lifespan context and proper Pydantic validation.
"""

import os
from fastapi import FastAPI, Request, HTTPException
from contextlib import asynccontextmanager
from aiogram.types import Update
from config import bot, dp
# نقوم باستيراد دالة الأوامر من config لتجنب الـ Circular Import مع ملف main
from config import set_bot_commands 
from logger import get_logger
from constants import WEBHOOK_PATH, WEBHOOK_PORT, TELEGRAM_WEBHOOK_SECRET

logger = get_logger(__name__)

# ==================== Lifespan Context (Modern Event Handling) ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    إدارة أحداث بدء وإيقاف السيرفر بشكل آمن وبالمعايير الحديثة لـ FastAPI.
    """
    # [حدث الـ Startup]: يتم تنفيذه عند إقلاع السيرفر
    try:
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
                allowed_updates=["message", "callback_query", "poll_answer", "poll"],
                secret_token=TELEGRAM_WEBHOOK_SECRET,
            )
            print(f"✅ تم تفعيل Webhook بنجاح على: {full_webhook_url}")
            
            # تفعيل قائمة الأوامر
            await set_bot_commands(bot)
            
    except Exception as e:
        print(f"❌ فشل تفعيل الـ Webhook أثناء تشغيل السيرفر: {e}")
        logger.error(f"Failed to set webhook on startup: {e}")

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
        
        # [إصلاح الخطأ الحرج]: استخدام model_validate لتحليل البيانات المتداخلة بالكامل
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