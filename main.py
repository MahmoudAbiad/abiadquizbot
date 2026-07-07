"""
Main entry point for the quiz maker bot.
Supports both Webhook (Railway/Srv) and Polling modes.
"""

import sys
import os
import asyncio
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
    
    # 2. تسجيل الـ Routers
    dp.include_routers(admin_router, start_router, quiz_router)
    
    webhook_url = os.getenv("WEBHOOK_URL")
    
    if webhook_url:
        logger.info("Starting bot in WEBHOOK mode")
        print("🚀 تشغيل البوت في وضع WEBHOOK (السرفر)...")
        from webhook_server import run_webhook_server
        run_webhook_server()
    else:
        logger.info("Starting bot in POLLING mode")
        print("🚀 تشغيل البوت في وضع POLLING (الاستطلاع المحلي)...")
        
        async def polling_main():
            try:
                try:
                    await bot.delete_webhook(drop_pending_updates=True)
                except:
                    pass
                
                await set_bot_commands(bot)
                await dp.start_polling(bot)
                
            except Exception as e:
                logger.critical(f"Error in polling main: {e}")
                sys.exit(1)
        
        try:
            asyncio.run(polling_main())
        except KeyboardInterrupt:
            print("\n🛑 تم إيقاف البوت")

if __name__ == "__main__":
    main()