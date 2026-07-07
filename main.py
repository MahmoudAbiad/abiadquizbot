"""
Main entry point for the quiz maker bot.
Supports both Webhook (Railway/Srv) and Polling modes.
"""

import sys
import os
import asyncio
import uvicorn
from logger import get_logger
from config import bot, dp
from handlers import start_router, admin_router, quiz_router
from middlewares import ThrottlingMiddleware
from aiogram import types

logger = get_logger(__name__)

def main():
    # 1. تفعيل الحماية (Rate Limiting) على الرسائل والأزرار (4 ثوانٍ كحد أقصى)
    dp.message.middleware(ThrottlingMiddleware(limit=4))
    dp.callback_query.middleware(ThrottlingMiddleware(limit=2))
    
    # 2. تسجيل الـ Routers الأساسية للبوت
    dp.include_routers(admin_router, start_router, quiz_router)
    
    webhook_url = os.getenv("WEBHOOK_URL")
    
    if webhook_url:
        logger.info("Starting bot in WEBHOOK mode")
        print("🚀 تشغيل البوت في وضع WEBHOOK على السيرفر...")
        
        # قراءة المنفذ ديناميكياً من بيئة Railway الافتراضية
        server_port = int(os.getenv("PORT", os.getenv("WEBHOOK_PORT", 8080)))
        
        # تشغيل السيرفر الفعلي لـ FastAPI المروّس في ملف webhook_server
        uvicorn.run("webhook_server:app", host="0.0.0.0", port=server_port, loop="asyncio")
    else:
        logger.info("Starting bot in POLLING mode")
        print("🚀 تشغيل البوت في وضع POLLING (الاستطلاع المحلي)...")
        
        async def polling_main():
            try:
                try:
                    await bot.delete_webhook(drop_pending_updates=True)
                except:
                    pass
                
                # إعداد قائمة أوامر البوت الاختيارية إذا كانت مدعومة في مشروعك
                try:
                    from handlers.start import set_bot_commands
                    await set_bot_commands(bot)
                except:
                    pass
                    
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