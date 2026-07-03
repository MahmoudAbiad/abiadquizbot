import asyncio
import os
from aiogram import Bot
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from config import bot, dp, ADMIN_ID
from handlers import start, admin, quiz

# 1. إعداد الـ Webhook
# يتم جلب الرابط من الإعدادات في Render
WEBHOOK_URL = os.getenv("WEBHOOK_URL") 
WEBHOOK_PATH = f"/bot/{bot.token}"

async def set_bot_commands(bot: Bot):
    """إعداد قوائم الأوامر (Menu)"""
    student_commands = [types.BotCommand(command="start", description="🔄 تشغيل البوت وعرض الرصيد")]
    await bot.set_my_commands(student_commands, scope=types.BotCommandScopeDefault())
    
    if ADMIN_ID != 0:
        admin_commands = [
            types.BotCommand(command="start", description="🔄 تشغيل البوت"),
            types.BotCommand(command="charge", description="💰 شحن نقاط"),
            types.BotCommand(command="dbstats", description="📊 إحصائيات"),
            types.BotCommand(command="searchuser", description="🔍 فحص مستخدم")
        ]
        try:
            await bot.set_my_commands(admin_commands, scope=types.BotCommandScopeChat(chat_id=ADMIN_ID))
        except: pass

async def on_startup(bot: Bot):
    """إعداد البوت عند التشغيل"""
    await set_bot_commands(bot)
    await bot.set_webhook(f"{WEBHOOK_URL}{WEBHOOK_PATH}")
    print("✅ تم ربط الـ Webhook بنجاح.")

async def main():
    # دمج الـ Routers
    dp.include_routers(admin.router, start.router, quiz.router)
    
    # تحضير تطبيق الويب لاستقبال الطلبات من تلغرام
    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    
    # إعدادات التشغيل على المنصة السحابية
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    
    await on_startup(bot)
    await site.start()
    
    print(f"🚀 البوت يعمل الآن بنظام Webhook على البورت {port}...")
    await asyncio.Event().wait() # إبقاء البوت يعمل

if __name__ == "__main__":
    asyncio.run(main())
