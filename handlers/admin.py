import asyncio
from aiogram import Router, types
from aiogram.filters import Command, CommandObject
from config import bot, ADMIN_ID
from supabase_helper import admin_add_points, admin_get_global_stats, admin_search_user

router = Router()

@router.message(Command("charge"))
async def admin_charge_points(msg: types.Message, command: CommandObject):
    """أمر شحن النقاط لمستخدم: /charge [user_id] [amount]"""
    if msg.from_user.id != ADMIN_ID: return
    
    try:
        args = command.args.split()
        target_id = int(args[0])
        amount = int(args[1])
        
        # تم إزالة asyncio.to_thread لأن الدالة أصبحت غير متزامنة
        new_balance = await admin_add_points(target_id, amount)
        if new_balance is not None:
            await msg.answer(f"✅ تم بنجاح شحن {amount} نقطة للمستخدم `{target_id}`.\n💰 رصيده الجديد أصبح: {new_balance} نقطة.")
            try:
                await bot.send_message(target_id, f"🎉 بشرى سارة! قامت الإدارة بشحن حسابك بـ **{amount}** نقطة إضافية.\n💰 رصيدك الإجمالي الحالي: {new_balance} نقطة. استمتع بالاختبارات!")
            except Exception:
                pass
        else:
            await msg.answer("❌ فشل الشحن، تأكد من أن رقم الآيدي (User ID) صحيح ومسجل في قاعدة البيانات.")
    except Exception:
        await msg.answer("⚠️ طريقة الاستخدام الخاطئة!\nالرجاء كتابة الأمر كالتالي:\n`/charge آيدي_المستخدم كمية_النقاط`")

@router.message(Command("dbstats"))
async def admin_db_stats(msg: types.Message):
    """أمر رؤية إحصائيات سوبابيس العامة: /dbstats"""
    if msg.from_user.id != ADMIN_ID: return
    
    # تم التعديل
    stats = await admin_get_global_stats()
    report = (
        "📊 **إحصائيات قاعدة البيانات (Supabase) الحالية:**\n\n"
        f"👥 إجمالي الطلاب المسجلين: {stats['total_users']} طالب.\n"
        f"📝 إجمالي الأسئلة المولدة بالذكاء الاصطناعي: {stats['total_questions']} سؤال."
    )
    await msg.answer(report)

@router.message(Command("searchuser"))
async def admin_get_user(msg: types.Message, command: CommandObject):
    """أمر فحص مستخدم معين في سوبابيس: /searchuser [user_id أو username]"""
    if msg.from_user.id != ADMIN_ID: return
    if not command.args:
        await msg.answer("الرجاء إدخال اليوزرنيم أو الآيدي بعد الأمر. مثال:\n`/searchuser Mahmoud`")
        return
        
    # تم التعديل
    user_data = await admin_search_user(command.args.strip())
    if user_data:
        info = (
            "🔍 **بيانات الطالب المستخرجة:**\n\n"
            f"🆔 الآيدي الرقمي: `{user_data['user_id']}`\n"
            f"👤 اسم المستخدم: @{user_data['username']}\n"
            f"💰 النقاط المتاحة: {user_data['points']} نقطة\n"
            f"📈 الأسئلة المستهلكة: {user_data['total_questions']} سؤال\n"
            f"🔗 مستدعى بواسطة: `{user_data.get('referred_by') or 'لا يوجد'}`\n"
            f"📅 تاريخ آخر تجديد: {user_data.get('last_renewal')}"
        )
        await msg.answer(info)
    else:
        await msg.answer("❌ لم يتم العثور على أي بيانات مطابقة لهذا الطالب في قاعدة البيانات.")
