# handlers/admin/direct_gift_charge.py
"""مسار شحن النقاط والهدايا (الافتراضية والمخصصة) المرفق مع الرسائل المباشرة للأدمن."""
import html
import uuid
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramForbiddenError

from config import bot
import keyboards as _kb_module
from keyboards import (
    get_admin_dashboard_keyboard,
    get_admin_direct_message_mode_keyboard,
    get_cancel_keyboard,
)
from helpers.supabase_helper import admin_add_points, admin_grant_direct_gift
from logger import get_logger
from services.gift_presentation import expiry_text, remaining_expiry_text
from .dashboard import AdminState, IsAdminFilter, safe_edit_text

logger = get_logger(__name__)
router = Router()

router.message.filter(IsAdminFilter(), F.chat.type == "private")
router.callback_query.filter(IsAdminFilter(), F.message.chat.type == "private")

DIRECT_GIFT_PRESET_TITLES = {
    "admin": "هدية من الإدارة",
    "special": "هدية خاصة",
    "reward": "مكافأة تميز",
    "welcome": "هدية مستخدم جديد",
}


def get_admin_direct_message_charge_keyboard(user_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔄 شحن مجاني", callback_data=f"admin_direct_charge_free_{user_id}")],
        [types.InlineKeyboardButton(text="🎁 شحن هدايا", callback_data=f"admin_direct_charge_gift_{user_id}")],
        [types.InlineKeyboardButton(text="💳 شحن مدفوع", callback_data=f"admin_direct_charge_paid_{user_id}")],
        [types.InlineKeyboardButton(text="⏭️ بدون شحن", callback_data=f"admin_direct_charge_none_{user_id}")],
        [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")],
    ])


def get_admin_direct_gift_mode_keyboard(user_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text="🎁 هدية مستخدم جديد - 150 نقطة صالحة لمدة شهر",
            callback_data="admin_direct_gift_preset_welcome",
        )],
        [types.InlineKeyboardButton(
            text="⚙️ خيار مخصص",
            callback_data="admin_direct_gift_preset_custom",
        )],
        [types.InlineKeyboardButton(
            text="🔙 رجوع لأنواع الشحن",
            callback_data="admin_direct_gift_back_types",
        )],
        [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")],
    ])


def get_admin_direct_gift_title_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="🎁 هدية من الإدارة", callback_data="admin_direct_gift_title_admin"),
            types.InlineKeyboardButton(text="🌟 هدية خاصة", callback_data="admin_direct_gift_title_special"),
        ],
        [
            types.InlineKeyboardButton(text="🏆 مكافأة تميز", callback_data="admin_direct_gift_title_reward"),
            types.InlineKeyboardButton(text="🎉 هدية مستخدم جديد", callback_data="admin_direct_gift_title_welcome"),
        ],
        [types.InlineKeyboardButton(text="🔙 رجوع لخيارات الهدية", callback_data="admin_direct_gift_back_mode")],
        [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")],
    ])


def get_admin_direct_gift_amount_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="➕ 25", callback_data="admin_direct_gift_amt_25"),
            types.InlineKeyboardButton(text="➕ 50", callback_data="admin_direct_gift_amt_50"),
            types.InlineKeyboardButton(text="➕ 100", callback_data="admin_direct_gift_amt_100"),
        ],
        [
            types.InlineKeyboardButton(text="➕ 150", callback_data="admin_direct_gift_amt_150"),
            types.InlineKeyboardButton(text="➕ 250", callback_data="admin_direct_gift_amt_250"),
            types.InlineKeyboardButton(text="➕ 500", callback_data="admin_direct_gift_amt_500"),
        ],
        [types.InlineKeyboardButton(text="✍️ كمية مخصصة", callback_data="admin_direct_gift_amt_custom")],
        [types.InlineKeyboardButton(text="🔙 رجوع لاسم الهدية", callback_data="admin_direct_gift_back_title")],
        [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")],
    ])


def get_admin_direct_gift_days_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="📅 يوم واحد", callback_data="admin_direct_gift_days_1"),
            types.InlineKeyboardButton(text="📅 3 أيام", callback_data="admin_direct_gift_days_3"),
            types.InlineKeyboardButton(text="📅 أسبوع (7 أيام)", callback_data="admin_direct_gift_days_7"),
        ],
        [
            types.InlineKeyboardButton(text="📅 أسبوعان (14 يوماً)", callback_data="admin_direct_gift_days_14"),
            types.InlineKeyboardButton(text="📅 شهر (30 يوماً)", callback_data="admin_direct_gift_days_30"),
        ],
        [types.InlineKeyboardButton(text="♾️ بدون انتهاء", callback_data="admin_direct_gift_days_0")],
        [types.InlineKeyboardButton(text="✍️ مدة مخصصة", callback_data="admin_direct_gift_days_custom")],
        [types.InlineKeyboardButton(text="🔙 رجوع للكمية", callback_data="admin_direct_gift_back_amount")],
        [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")],
    ])


# تصدير أزرار الشحن والهدايا لوحدة keyboards أيضاً لضمان توافق أي استدعاء مباشر
_kb_module.DIRECT_GIFT_PRESET_TITLES = DIRECT_GIFT_PRESET_TITLES
_kb_module.get_admin_direct_message_charge_keyboard = get_admin_direct_message_charge_keyboard
_kb_module.get_admin_direct_gift_mode_keyboard = get_admin_direct_gift_mode_keyboard
_kb_module.get_admin_direct_gift_title_keyboard = get_admin_direct_gift_title_keyboard
_kb_module.get_admin_direct_gift_amount_keyboard = get_admin_direct_gift_amount_keyboard
_kb_module.get_admin_direct_gift_days_keyboard = get_admin_direct_gift_days_keyboard


def _format_gift_validity_days(days: int) -> str:
    if not days or int(days) <= 0:
        return "بدون انتهاء"
    d = int(days)
    if d == 1:
        return "يوم واحد"
    if d == 2:
        return "يومان"
    if d == 7:
        return "أسبوع (7 أيام)"
    if d == 14:
        return "أسبوعان (14 يوماً)"
    if d == 30:
        return "شهر (30 يوماً)"
    return f"{d} يوم"


@router.message(AdminState.waiting_for_direct_message_text)
async def process_direct_message_text(msg: types.Message, state: FSMContext):
    message_text = msg.text or ""
    if not message_text.strip():
        return await msg.answer("⚠️ يرجى إرسال نص للرسالة.", reply_markup=get_cancel_keyboard())
    if len(message_text) > 4000:
        return await msg.answer("❌ الرسالة طويلة جداً. الحد الأقصى هو 4000 حرفاً لتوفير مساحة لتفاصيل الرصيد.", reply_markup=get_cancel_keyboard())

    await state.update_data(direct_message_text=message_text, direct_message_charge_amount=None, direct_message_charge_type=None)
    data = await state.get_data()
    if data.get("direct_message_mode") == "message":
        await state.update_data(direct_message_charge_amount=0, direct_message_charge_type="none")
        return await show_direct_message_confirmation(msg, state)

    await state.set_state(AdminState.waiting_for_direct_message_charge_type)
    await msg.answer(
        f"💰 اختر نوع الشحن المرفق مع الرسالة للمستخدم <code>{data['direct_message_target_id']}</code>:",
        reply_markup=get_admin_direct_message_charge_keyboard(data["direct_message_target_id"]),
        parse_mode="HTML",
    )


@router.message(AdminState.waiting_for_direct_message_charge_amount)
async def process_direct_message_charge_amount(msg: types.Message, state: FSMContext):
    amount_text = (msg.text or "").strip()
    if not amount_text.isdigit():
        return await msg.answer("❌ أرسل عدد نقاط صحيحاً (صفر أو أكثر).", reply_markup=get_cancel_keyboard())

    amount = int(amount_text)
    if amount > 1000000:
        return await msg.answer("❌ العدد كبير جداً. الحد الأقصى هو 1,000,000 نقطة.", reply_markup=get_cancel_keyboard())

    data = await state.get_data()
    await state.update_data(direct_message_charge_amount=amount)
    if amount == 0:
        await state.update_data(direct_message_charge_type="none")
        return await show_direct_message_confirmation(msg, state)

    if data.get("direct_message_charge_type") in ("free", "paid"):
        return await show_direct_message_confirmation(msg, state)

    await state.set_state(AdminState.waiting_for_direct_message_charge_type)
    await msg.answer(
        f"اختر وجهة <code>{amount}</code> نقطة للمستخدم <code>{data['direct_message_target_id']}</code>:",
        reply_markup=get_admin_direct_message_charge_keyboard(data["direct_message_target_id"]),
        parse_mode="HTML",
    )


async def show_direct_message_confirmation(message: types.Message, state: FSMContext):
    data = await state.get_data()
    charge_type = data.get("direct_message_charge_type")
    amount = int(data.get("direct_message_charge_amount") or 0)
    balance_before = float(data.get("direct_message_balance_before") or 0)
    include_balance = bool(data.get("direct_message_include_balance"))
    charge_label = {
        "free": "مجاني",
        "gift": "شحن هدايا",
        "paid": "مدفوع",
        "none": "بدون شحن",
    }.get(charge_type, "غير محدد")
    gift_lines = ""
    if charge_type == "gift" and amount > 0:
        gift_title = data.get("direct_message_gift_title") or "هدية مستخدم جديد"
        gift_days = int(data.get("direct_message_gift_days") or 0)
        gift_lines = (
            f"اسم الهدية: <b>{html.escape(str(gift_title))}</b>\n"
            f"صلاحية الهدية: <code>{html.escape(_format_gift_validity_days(gift_days))}</code>\n"
        )
    preview = (
        "🔍 <b>معاينة العملية</b>\n"
        f"المستخدم: <code>{html.escape(str(data['direct_message_target_id']))}</code>\n"
        f"الشحن: <code>{amount}</code> نقطة ({charge_label})\n"
        f"{gift_lines}"
        f"الرصيد قبل الشحن: <code>{balance_before:.2f}</code> نقطة\n"
        f"إرفاق الرصيد بالرسالة: {'☑️ نعم' if include_balance else '☐ لا'}\n"
        "───────────────────\n"
        f"{html.escape(data['direct_message_text'])}\n"
        "───────────────────\n\n"
        "هل تريد تأكيد إرسال الرسالة وتنفيذ الشحن؟"
    )
    toggle_label = (
        ("☑️ إرفاق تفاصيل الهدية والرصيد" if include_balance else "☐ إرفاق تفاصيل الهدية والرصيد")
        if charge_type == "gift"
        else ("☑️ إرفاق الرصيد قبل/بعد الشحن" if include_balance else "☐ إرفاق الرصيد قبل/بعد الشحن")
    )
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="✅ تأكيد العملية", callback_data="admin_confirm_direct_message"),
        types.InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_main_menu"),
    ], [
        types.InlineKeyboardButton(
            text=toggle_label,
            callback_data="admin_direct_balance_toggle",
        ),
    ]])
    await message.answer(preview, reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(AdminState.waiting_for_direct_message_confirm)


@router.callback_query(F.data.startswith("admin_direct_charge_"), AdminState.waiting_for_direct_message_charge_type)
async def process_direct_message_charge_type(call: types.CallbackQuery, state: FSMContext):
    charge_type = call.data.split("_")[3]
    data = await state.get_data()
    target_id = data.get("direct_message_target_id")
    await call.answer()

    if charge_type == "none":
        await state.update_data(direct_message_charge_type="none", direct_message_charge_amount=0)
        return await show_direct_message_confirmation(call.message, state)

    if charge_type == "gift":
        await state.update_data(direct_message_charge_type="gift")
        await state.set_state(AdminState.waiting_for_direct_gift_mode)
        return await safe_edit_text(
            call.message,
            f"🎁 <b>شحن هدايا للمستخدم</b> <code>{target_id}</code>\n\n"
            "اختر الهدية الافتراضية الجاهزة أو قم بتخصيص الكمية والمدة:",
            reply_markup=get_admin_direct_gift_mode_keyboard(target_id),
        )

    if charge_type in ("free", "paid"):
        await state.update_data(direct_message_charge_type=charge_type)
        existing_amount = data.get("direct_message_charge_amount")
        if isinstance(existing_amount, int) and existing_amount > 0:
            return await show_direct_message_confirmation(call.message, state)
        type_label = "شحن مجاني" if charge_type == "free" else "شحن مدفوع"
        await state.set_state(AdminState.waiting_for_direct_message_charge_amount)
        return await safe_edit_text(
            call.message,
            f"💰 <b>{type_label}</b>\n\n"
            "أرسل عدد النقاط المراد شحنها مع الرسالة.\n"
            "أرسل <code>0</code> إذا كنت لا تريد شحن رصيد.",
            reply_markup=get_cancel_keyboard(),
        )


@router.callback_query(F.data == "admin_direct_gift_back_types")
async def process_direct_gift_back_types(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("direct_message_target_id")
    await state.set_state(AdminState.waiting_for_direct_message_charge_type)
    await call.answer()
    await safe_edit_text(
        call.message,
        f"💰 اختر نوع الشحن المرفق مع الرسالة للمستخدم <code>{target_id}</code>:",
        reply_markup=get_admin_direct_message_charge_keyboard(target_id),
    )


@router.callback_query(F.data.startswith("admin_direct_gift_preset_"), AdminState.waiting_for_direct_gift_mode)
async def process_direct_gift_preset(call: types.CallbackQuery, state: FSMContext):
    preset = call.data.replace("admin_direct_gift_preset_", "", 1)
    data = await state.get_data()
    await call.answer()

    if preset == "welcome":
        updates = {
            "direct_message_charge_type": "gift",
            "direct_message_charge_amount": 150,
            "direct_message_gift_days": 30,
            "direct_message_gift_title": "هدية مستخدم جديد",
            "direct_message_gift_source_key": f"direct:{uuid.uuid4()}",
        }
        if "direct_message_include_balance" not in data:
            updates["direct_message_include_balance"] = True
        await state.update_data(**updates)
        return await show_direct_message_confirmation(call.message, state)

    if preset == "custom":
        await state.set_state(AdminState.waiting_for_direct_gift_title)
        return await safe_edit_text(
            call.message,
            "🏷️ <b>اسم الهدية المخصصة</b>\n\n"
            "اختر اسماً جاهزاً من الأزرار أدناه، أو أرسل الآن اسم الهدية الذي سيظهر للطالب في محفظته (حتى 100 حرف):",
            reply_markup=get_admin_direct_gift_title_keyboard(),
        )


@router.callback_query(F.data == "admin_direct_gift_back_mode")
async def process_direct_gift_back_mode(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("direct_message_target_id")
    await state.set_state(AdminState.waiting_for_direct_gift_mode)
    await call.answer()
    await safe_edit_text(
        call.message,
        f"🎁 <b>شحن هدايا للمستخدم</b> <code>{target_id}</code>\n\n"
        "اختر الهدية الافتراضية الجاهزة أو قم بتخصيص الكمية والمدة:",
        reply_markup=get_admin_direct_gift_mode_keyboard(target_id),
    )


@router.callback_query(F.data.startswith("admin_direct_gift_title_"), AdminState.waiting_for_direct_gift_title)
async def process_direct_gift_title_button(call: types.CallbackQuery, state: FSMContext):
    key = call.data.replace("admin_direct_gift_title_", "", 1)
    title = DIRECT_GIFT_PRESET_TITLES.get(key, "هدية من الإدارة")
    await state.update_data(direct_message_gift_title=title)
    await state.set_state(AdminState.waiting_for_direct_gift_amount)
    await call.answer()
    await safe_edit_text(
        call.message,
        f"💰 <b>كمية نقاط الهدية</b>\n"
        f"اسم الهدية: <b>{html.escape(title)}</b>\n\n"
        "اختر الكمية من الأزرار الجاهزة أو اضغط «كمية مخصصة» لإدخال الرقم يدوياً:",
        reply_markup=get_admin_direct_gift_amount_keyboard(),
    )


@router.message(AdminState.waiting_for_direct_gift_title)
async def process_direct_gift_title_text(msg: types.Message, state: FSMContext):
    title = (msg.text or "").strip()
    if not 1 <= len(title) <= 100:
        return await msg.answer(
            "⚠️ يرجى إرسال اسم للهدية يتراوح بين حرف واحد و100 حرف، أو اختيار اسم جاهز:",
            reply_markup=get_admin_direct_gift_title_keyboard(),
        )
    await state.update_data(direct_message_gift_title=title)
    await state.set_state(AdminState.waiting_for_direct_gift_amount)
    await msg.answer(
        f"💰 <b>كمية نقاط الهدية</b>\n"
        f"اسم الهدية: <b>{html.escape(title)}</b>\n\n"
        "اختر الكمية من الأزرار الجاهزة أو اضغط «كمية مخصصة» لإدخال الرقم يدوياً:",
        reply_markup=get_admin_direct_gift_amount_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_direct_gift_back_title")
async def process_direct_gift_back_title(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_direct_gift_title)
    await call.answer()
    await safe_edit_text(
        call.message,
        "🏷️ <b>اسم الهدية المخصصة</b>\n\n"
        "اختر اسماً جاهزاً من الأزرار أدناه، أو أرسل الآن اسم الهدية الذي سيظهر للطالب في محفظته (حتى 100 حرف):",
        reply_markup=get_admin_direct_gift_title_keyboard(),
    )


@router.callback_query(F.data.startswith("admin_direct_gift_amt_"), AdminState.waiting_for_direct_gift_amount)
async def process_direct_gift_amount_button(call: types.CallbackQuery, state: FSMContext):
    raw = call.data.replace("admin_direct_gift_amt_", "", 1)
    await call.answer()

    if raw == "custom":
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔙 رجوع لأزرار الكمية", callback_data="admin_direct_gift_back_amount")],
            [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")],
        ])
        return await safe_edit_text(
            call.message,
            "✍️ <b>كمية مخصصة للهدية</b>\n\n"
            "أرسل الآن عدد النقاط المراد منحها كهدية (من <code>1</code> إلى <code>1,000,000</code>):",
            reply_markup=kb,
        )

    if not raw.isdigit():
        return
    amount = int(raw)
    await state.update_data(direct_message_charge_amount=amount)
    await state.set_state(AdminState.waiting_for_direct_gift_days)
    await safe_edit_text(
        call.message,
        f"⏳ <b>مدة صلاحية الهدية</b>\n"
        f"الكمية المختارة: <code>{amount}</code> نقطة\n\n"
        "اختر مدة الصلاحية من الأزرار الجاهزة أو اضغط «مدة مخصصة»:",
        reply_markup=get_admin_direct_gift_days_keyboard(),
    )


@router.message(AdminState.waiting_for_direct_gift_amount)
async def process_direct_gift_amount_text(msg: types.Message, state: FSMContext):
    amount_text = (msg.text or "").strip()
    if not amount_text.isdigit():
        return await msg.answer("❌ أرسل عدد نقاط صحيحاً (من 1 إلى 1,000,000).", reply_markup=get_admin_direct_gift_amount_keyboard())
    amount = int(amount_text)
    if not 1 <= amount <= 1000000:
        return await msg.answer("❌ يجب أن تكون كمية الهدية بين 1 و 1,000,000 نقطة.", reply_markup=get_admin_direct_gift_amount_keyboard())

    await state.update_data(direct_message_charge_amount=amount)
    await state.set_state(AdminState.waiting_for_direct_gift_days)
    await msg.answer(
        f"⏳ <b>مدة صلاحية الهدية</b>\n"
        f"الكمية المختارة: <code>{amount}</code> نقطة\n\n"
        "اختر مدة الصلاحية من الأزرار الجاهزة أو اضغط «مدة مخصصة»:",
        reply_markup=get_admin_direct_gift_days_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_direct_gift_back_amount")
async def process_direct_gift_back_amount(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    title = data.get("direct_message_gift_title") or "هدية من الإدارة"
    await state.set_state(AdminState.waiting_for_direct_gift_amount)
    await call.answer()
    await safe_edit_text(
        call.message,
        f"💰 <b>كمية نقاط الهدية</b>\n"
        f"اسم الهدية: <b>{html.escape(title)}</b>\n\n"
        "اختر الكمية من الأزرار الجاهزة أو اضغط «كمية مخصصة» لإدخال الرقم يدوياً:",
        reply_markup=get_admin_direct_gift_amount_keyboard(),
    )


@router.callback_query(F.data.startswith("admin_direct_gift_days_"), AdminState.waiting_for_direct_gift_days)
async def process_direct_gift_days_button(call: types.CallbackQuery, state: FSMContext):
    raw = call.data.replace("admin_direct_gift_days_", "", 1)
    await call.answer()

    if raw == "custom":
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔙 رجوع لأزرار المدة", callback_data="admin_direct_gift_back_days")],
            [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")],
        ])
        return await safe_edit_text(
            call.message,
            "✍️ <b>مدة صلاحية مخصصة</b>\n\n"
            "أرسل عدد أيام صلاحية الهدية (من <code>1</code> إلى <code>3650</code> يوماً، أو أرسل <code>0</code> لهدية بدون انتهاء):",
            reply_markup=kb,
        )

    if not raw.isdigit():
        return
    days = int(raw)
    data = await state.get_data()
    updates = {
        "direct_message_charge_type": "gift",
        "direct_message_gift_days": days,
        "direct_message_gift_source_key": f"direct:{uuid.uuid4()}",
    }
    if "direct_message_include_balance" not in data:
        updates["direct_message_include_balance"] = True
    await state.update_data(**updates)
    await show_direct_message_confirmation(call.message, state)


@router.message(AdminState.waiting_for_direct_gift_days)
async def process_direct_gift_days_text(msg: types.Message, state: FSMContext):
    days_text = (msg.text or "").strip()
    if not days_text.isdigit():
        return await msg.answer("❌ أرسل عدد أيام صحيحاً (من 0 إلى 3650).", reply_markup=get_admin_direct_gift_days_keyboard())
    days = int(days_text)
    if not 0 <= days <= 3650:
        return await msg.answer("❌ يجب أن تكون المدة بين 0 (بدون انتهاء) و 3650 يوماً.", reply_markup=get_admin_direct_gift_days_keyboard())

    data = await state.get_data()
    updates = {
        "direct_message_charge_type": "gift",
        "direct_message_gift_days": days,
        "direct_message_gift_source_key": f"direct:{uuid.uuid4()}",
    }
    if "direct_message_include_balance" not in data:
        updates["direct_message_include_balance"] = True
    await state.update_data(**updates)
    await show_direct_message_confirmation(msg, state)


@router.callback_query(F.data == "admin_direct_gift_back_days")
async def process_direct_gift_back_days(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    amount = int(data.get("direct_message_charge_amount") or 0)
    await state.set_state(AdminState.waiting_for_direct_gift_days)
    await call.answer()
    await safe_edit_text(
        call.message,
        f"⏳ <b>مدة صلاحية الهدية</b>\n"
        f"الكمية المختارة: <code>{amount}</code> نقطة\n\n"
        "اختر مدة الصلاحية من الأزرار الجاهزة أو اضغط «مدة مخصصة»:",
        reply_markup=get_admin_direct_gift_days_keyboard(),
    )


@router.callback_query(F.data == "admin_direct_balance_toggle", AdminState.waiting_for_direct_message_confirm)
async def toggle_direct_message_balance(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(direct_message_include_balance=not bool(data.get("direct_message_include_balance")))
    await call.answer()
    await show_direct_message_confirmation(call.message, state)


@router.callback_query(
    F.data == "admin_confirm_direct_message",
    AdminState.waiting_for_direct_message_confirm,
)
async def process_confirm_direct_message(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("direct_message_target_id")
    message_text = data.get("direct_message_text")
    charge_amount = int(data.get("direct_message_charge_amount") or 0)
    charge_type = data.get("direct_message_charge_type", "none")
    balance_before = float(data.get("direct_message_balance_before") or 0)
    include_balance = bool(data.get("direct_message_include_balance"))
    gift_title = data.get("direct_message_gift_title") or "هدية مستخدم جديد"
    gift_days = int(data.get("direct_message_gift_days") or 30)
    gift_source_key = data.get("direct_message_gift_source_key") or f"direct:{uuid.uuid4()}"
    await state.clear()
    await call.answer()

    if not target_id or not message_text:
        return await safe_edit_text(call.message, "❌ انتهت جلسة الرسالة، يرجى بدء العملية مجدداً.", reply_markup=get_admin_dashboard_keyboard())

    gift_expires_at = None
    if charge_amount and charge_type in ("free", "paid"):
        new_balance = await admin_add_points(target_id, charge_amount, balance_type=charge_type)
        if new_balance is None:
            return await safe_edit_text(
                call.message,
                "❌ تعذر شحن الرصيد، لذلك لم يتم إرسال الرسالة.",
                reply_markup=get_admin_dashboard_keyboard(),
            )
    elif charge_amount and charge_type == "gift":
        gift_result = await admin_grant_direct_gift(
            target_id=target_id,
            amount=charge_amount,
            valid_days=gift_days,
            title=gift_title,
            source_key=gift_source_key,
        )
        if not gift_result or gift_result.get("status") != "ok":
            return await safe_edit_text(
                call.message,
                "❌ تعذر شحن الهدية، لذلك لم يتم إرسال الرسالة.",
                reply_markup=get_admin_dashboard_keyboard(),
            )
        new_balance = float(gift_result["total_points"])
        gift_expires_at = gift_result.get("expires_at")
    else:
        new_balance = balance_before

    outgoing_message = html.escape(message_text) if include_balance else message_text
    if include_balance:
        if charge_type == "gift" and charge_amount > 0:
            outgoing_message += (
                "\n\n🎁 <b>تفاصيل الهدية والرصيد</b>\n"
                f"┣ الهدية: <b>{html.escape(str(gift_title))}</b>\n"
                f"┣ النقاط المضافة: <code>+{charge_amount}</code> نقطة هدية\n"
                f"┣ انتهاء الهدية: {expiry_text(gift_expires_at)}\n"
                f"┣ ⏳ {remaining_expiry_text(gift_expires_at)}\n"
                f"┣ الرصيد قبل الشحن: <code>{balance_before:.2f}</code> نقطة\n"
                f"┗ الرصيد بعد الشحن: <code>{new_balance:.2f}</code> نقطة"
            )
        else:
            outgoing_message += (
                "\n\n📊 <b>تفاصيل الرصيد</b>\n"
                f"┣ قبل الشحن: <code>{balance_before:.2f}</code> نقطة\n"
                f"┗ بعد الشحن: <code>{new_balance:.2f}</code> نقطة"
            )

    try:
        await bot.send_message(chat_id=int(target_id), text=outgoing_message, parse_mode="HTML" if include_balance else None)
    except TelegramForbiddenError:
        return await safe_edit_text(
            call.message,
            f"🚫 تعذر إرسال الرسالة. المستخدم <code>{target_id}</code> حظر البوت أو لم يبدأ المحادثة.",
            reply_markup=get_admin_dashboard_keyboard(),
        )
    except Exception as e:
        logger.error(f"Error sending direct message to user {target_id}: {e}")
        return await safe_edit_text(
            call.message,
            "❌ حدث خطأ أثناء إرسال الرسالة. حاول مجدداً.",
            reply_markup=get_admin_dashboard_keyboard(),
        )

    if charge_type == "gift" and charge_amount > 0:
        charge_summary = (
            f"<code>{charge_amount}</code> نقطة (شحن هدايا — {html.escape(str(gift_title))}، "
            f"الصلاحية: {html.escape(_format_gift_validity_days(gift_days))})"
        )
    else:
        charge_summary = (
            f"<code>{charge_amount}</code> نقطة "
            f"({'مجاني' if charge_type == 'free' else 'مدفوع' if charge_type == 'paid' else 'بدون شحن'})"
        )
    await safe_edit_text(
        call.message,
        f"✅ تم إرسال الرسالة بنجاح إلى المستخدم <code>{target_id}</code>\n"
        f"💰 الشحن: {charge_summary}",
        reply_markup=get_admin_dashboard_keyboard(),
    )
