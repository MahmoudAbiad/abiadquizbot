import asyncio
from aiogram import Bot, types
from config import bot, dp, ADMIN_ID
from handlers import start, admin, quiz

async def set_bot_commands(bot: Bot):
    # 1. أوامر الطلاب العامة على الشاشة
    student_commands = [
        types.BotCommand(command="start", description="🔄 تشغيل البوت وعرض الرصيد")
    ]
    await bot.set_my_commands(student_commands, scope=types.BotCommandScopeDefault())
    print("✅ تم تفعيل قائمة أوامر الطلاب على الشاشة.")
    
    # 2. أوامر الآدمن السرية والحصرية (تظهر لحسابك أنت فقط)
    if ADMIN_ID != 0:
        admin_commands = [
            types.BotCommand(command="start", description="🔄 تشغيل البوت وعرض الرصيد"),
            types.BotCommand(command="charge", description="💰 شحن نقاط لطالب (/charge آيدي كمية)"),
            types.BotCommand(command="dbstats", description="📊 إحصائيات سوبابيس العامة"),
            types.BotCommand(command="searchuser", description="🔍 فحص بيانات طالب محدد")
        ]
        try:
            await bot.set_my_commands(admin_commands, scope=types.BotCommandScopeChat(chat_id=ADMIN_ID))
            print("👑 تم تفعيل قائمة أوامر الآدمن السرية لحسابك بنجاح.")
        except Exception as e:
            print(f"⚠️ تنبيه: لم يتم تفعيل قائمة الآدمن على الشاشة. السبب: {e}")

async def main():
    # 🌟 خطوة الدمج السحرية: نربط الـ Routers بالترتيب الصحيح داخل الـ Dispatcher
    dp.include_routers(
        admin.router,
        start.router,
        quiz.router
    )
    
    # تهيئة قوائم الأزرار الزرقاء (Menu) للشاشات
    await set_bot_commands(bot)
    
    print("🚀 البوت يعمل الآن بكفاءة وبنظام الوحدات المقسمة الاحترافي (Modular System)...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())