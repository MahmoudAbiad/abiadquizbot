"""
Main entry point for the quiz maker bot.
Supports both Webhook (Railway/Srv) and Polling modes.
"""

import sys
import os
import asyncio
import uvicorn
import logging
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

from logger import get_logger
from config import bot, dp
# 🚀 تم تحديث الاستيراد لجلب quiz_runner_router بدلاً من execution_router القديم
from handlers import (
    start_router, 
    admin_router, 
    files_router, 
    quiz_options_router,
    quiz_runner_router, 
    favorites_router, 
    sharing_router,
    leaderboard_router,
    export_router,
    tutorial_router,
    audio_router
)
from middlewares import ThrottlingMiddleware, ErrorTrackingMiddleware

# تهيئة Sentry للتتبع
SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[FastApiIntegration()],
    )

# تقليل ضوضاء سجلات المكتبات الخارجية
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("hpack").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = get_logger(__name__)

def main():
    # 0. 🐞 تتبع الأخطاء: يغطي كل أنواع الـ Update (رسائل/أزرار/إجابات استفتاء...) ويُسجَّل
    # كل خطأ يواجهه الطالب فعلياً بلوحة تحليلات الأدمن. يجب تسجيله كـ outer middleware
    # (أول شيء يشتغل وآخر شيء يلف حوله) ليغطي أي استثناء يحدث بأي مرحلة لاحقة، بما فيها
    # ThrottlingMiddleware نفسها.
    dp.update.outer_middleware(ErrorTrackingMiddleware())

    # 1. تفعيل الحماية من التكرار (Rate Limiting)
    dp.message.middleware(ThrottlingMiddleware(limit=1.2))
    dp.callback_query.middleware(ThrottlingMiddleware(limit=0.8))
    
    # 2. تسجيل الـ Routers المحدثة بالترتيب الصحيح
    dp.include_routers(
        start_router,
        tutorial_router,  # 👈 الدليل التفاعلي الجديد (يجب أن يُسجَّل قبل files_router)
        admin_router,
        sharing_router,
        audio_router,  # 🆕 معالج المحاضرات الصوتية (يجب أن يُسجَّل قبل files_router لالتقاط
        # زر إلغاء الطلب الخاص بشاشة waiting_for_audio_action قبل المعالج العام بـ files_router)
        files_router,
        quiz_options_router,  # 🆕 شاشة اختيار نوع/صعوبة الأسئلة (يجب أن يُسجَّل بعد files_router)
        quiz_runner_router,  # 👈 الـ Router الجديد لإدارة حركة الكويز
        favorites_router,
        leaderboard_router,
        export_router,  # 👈 تصدير الكويز إلى Word/PDF
    )
    
    webhook_url = os.getenv("WEBHOOK_URL")
    
    if webhook_url:
        logger.info("Starting bot in WEBHOOK mode")
        print("🚀 تشغيل البوت في وضع WEBHOOK على السيرفر...")
        
        server_port = int(os.getenv("PORT", os.getenv("WEBHOOK_PORT", 8080)))
        uvicorn.run("webhook_server:app", host="0.0.0.0", port=server_port, loop="asyncio")
    else:
        logger.info("Starting bot in POLLING mode")
        print("🚀 تشغيل البوت في وضع POLLING (الاستطلاع المحلي)...")
        
        async def polling_main():
            try:
                try:
                    await bot.delete_webhook(drop_pending_updates=True)
                except Exception:
                    pass
                
                try:
                    from config import set_bot_commands
                    await set_bot_commands(bot)
                except Exception as e:
                    logger.error(f"Failed to set bot commands: {e}")
                    
                await dp.start_polling(bot)
                
            except Exception as e:
                logger.critical(f"Error in polling main: {e}")
                sys.exit(1)
        
        try:
            asyncio.run(polling_main())
        except KeyboardInterrupt:
            print("\n🛑 تم إيقاف البوت بنجاح.")

if __name__ == "__main__":
    main()