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
    # تسجيل الـ Routers بشكل عام لتشمل وضعي التشغيل
    dp.include_routers(admin_router, start_router, quiz_router)
    
    # التحقق من وجود رابط النشر الخارجي
    webhook_url = os.getenv("WEBHOOK_URL")
    
    if webhook_url:
        logger.info("Starting bot in WEBHOOK mode")
        print("🚀 تشغيل البوت في وضع WEBHOOK (السرفر)...")
        
        # أصبح الـ main هنا نظيفاً ومجرد مستدعي للسيرفر، لمنع مشاكل الـ Event Loop
        from webhook_server import run_webhook_server
        run_webhook_server()
    else:
        # وضع الاستطلاع المحلي (Polling) للتطوير المحلي
        logger.info("Starting bot in POLLING mode")
        print("🚀 تشغيل البوت في وضع POLLING (الاستطلاع المحلي)...")
        
        async def polling_main():
            """Main polling loop"""
            try:
                try:
                    await bot.delete_webhook(drop_pending_updates=True)
                    print("✅ تم حذف أي webhook قديم وتصفير البيانات")
                except:
                    pass
                
                # إعداد الأوامر للـ Polling هنا طبيعي لأننا داخل نفس الـ loop
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