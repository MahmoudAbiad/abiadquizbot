"""
Main entry point for the quiz maker bot.
Supports both Webhook (Azure) and Polling modes.
"""

import sys
import os
import asyncio
from logger import get_logger
from config import bot, dp
# نقلنا استيراد الـ Routers للأعلى لتكون جاهزة دائماً
from handlers import start_router, admin_router, quiz_router
from aiogram import types

logger = get_logger(__name__)

async def set_bot_commands(bot):
    """Set bot commands for the menu"""
    try:
        student_commands = [
            types.BotCommand(command="start", description="🔄 تشغيل البوت وعرض الرصيد")
        ]
        await bot.set_my_commands(student_commands, scope=types.BotCommandScopeDefault())
        print("✅ تم تفعيل قائمة أوامر الطلاب على الشاشة.")
        
        from config import ADMIN_ID
        if ADMIN_ID != 0:
            admin_commands = [
                types.BotCommand(command="start", description="🔄 تشغيل البوت وعرض الرصيد"),
                types.BotCommand(command="charge", description="💰 شحن نقاط لطالب"),
                types.BotCommand(command="dbstats", description="📊 إحصائيات قاعدة البيانات"),
                types.BotCommand(command="searchuser", description="🔍 فحص بيانات طالب")
            ]
            await bot.set_my_commands(
                admin_commands,
                scope=types.BotCommandScopeChat(chat_id=ADMIN_ID)
            )
            print("👑 تم تفعيل قائمة أوامر الآدمن السرية.")
    except Exception as e:
        logger.error(f"Error setting commands: {e}")

def main():
    """
    Main entry point - determines whether to run in webhook or polling mode.
    """
    # 🔥 خطوة الإصلاح: تسجيل الـ Routers بشكل عام لتشمل وضع الـ Webhook أيضاً
    dp.include_routers(admin_router, start_router, quiz_router)
    
    # Check if running in webhook mode
    webhook_url = os.getenv("WEBHOOK_URL")
    
    if webhook_url:
        # Webhook mode (for Azure, Heroku, JustRunMy.App, etc.)
        logger.info("Starting bot in WEBHOOK mode")
        print("🚀 تشغيل البوت في وضع WEBHOOK (السرفر)...")
        
        # تفعيل القائمة للأوامر حتى في وضع السيرفر
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(set_bot_commands(bot))
            else:
                loop.run_until_complete(set_bot_commands(bot))
        except Exception as e:
            logger.error(f"Could not set commands in webhook mode: {e}")
            
        from webhook_server import run_webhook_server
        run_webhook_server()
    else:
        # Polling mode (for local development)
        logger.info("Starting bot in POLLING mode")
        print("🚀 تشغيل البوت في وضع POLLING (الاستطلاع المحلي)...")
        
        async def polling_main():
            """Main polling loop"""
            try:
                # Delete old webhook if exists
                try:
                    await bot.delete_webhook(drop_pending_updates=True)
                    print("✅ تم حذف أي webhook قديم")
                except:
                    pass
                
                # Set commands
                await set_bot_commands(bot)
                
                print("🚀 البوت يعمل الآن في وضع الاستطلاع...")
                await dp.start_polling(bot)
                
            except Exception as e:
                logger.critical(f"Error in polling main: {e}", exception=e)
                sys.exit(1)
        
        try:
            asyncio.run(polling_main())
        except KeyboardInterrupt:
            print("\n🛑 تم إيقاف البوت")
            logger.info("Bot stopped by user")

if __name__ == "__main__":
    main()