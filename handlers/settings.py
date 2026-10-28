"""Private student settings; never clears an in-progress quiz/FSM."""
from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from helpers.supabase_helper._client import supabase
from helpers.supabase_helper.users import check_or_add_user

router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


async def settings_panel(user_id):
    result = await supabase.table("users").select("renewal_notifications").eq("user_id", user_id).single().execute()
    enabled = result.data["renewal_notifications"]
    text = ("⚙️ الإعدادات\n\n"
            "🔄 النقاط المجانية تتجدد يوميًا عند الساعة 8 مساءً بتوقيت دمشق.\n"
            f"إشعارات التجديد: {'🔔 مفعّلة' if enabled else '🔕 متوقفة'}\n\n"
            "إيقاف الإشعار لا يؤثر على تجدد نقاطك.")
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text="🔕 إيقاف إشعارات التجديد" if enabled else "🔔 تفعيل إشعارات التجديد",
            callback_data="renewal:off" if enabled else "renewal:on")],
        [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="student_home")],
    ])
    return text, keyboard


async def ensure_user(user):
    await check_or_add_user(user.id, user.username or "Unknown", user.first_name, user.last_name or "Unknown")
    await supabase.table("users").update({"bot_delivery_state": "reachable"}).eq("user_id", user.id).execute()


@router.message(Command("settings"))
async def settings_command(message: types.Message):
    await ensure_user(message.from_user)
    text, kb = await settings_panel(message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.in_({"student_settings", "renewal:on", "renewal:off"}))
async def settings_callback(call: types.CallbackQuery):
    await ensure_user(call.from_user)
    if call.data != "student_settings":
        await supabase.table("users").update({
            "renewal_notifications": call.data == "renewal:on",
        }).eq("user_id", call.from_user.id).execute()
    text, kb = await settings_panel(call.from_user.id)
    if call.data == "renewal:off":
        text = "🔕 تم إيقاف إشعارات التجديد. يمكنك إعادة تفعيلها من ⚙️ الإعدادات.\n\n" + text
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await call.answer()


@router.my_chat_member(F.chat.type == "private")
async def private_membership(update: types.ChatMemberUpdated):
    status = update.new_chat_member.status
    status = getattr(status, "value", status)
    await supabase.table("users").update({
        "bot_delivery_state": "blocked" if status == "kicked" else "reachable",
    }).eq("user_id", update.chat.id).execute()

