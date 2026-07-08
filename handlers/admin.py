"""
Admin-specific command handlers.
Updated with robust TelegramBadRequest handling, Pagination, and CSV Exports.
"""

import asyncio
import os
import csv
import io
import html
from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import BufferedInputFile
from aiogram.exceptions import TelegramBadRequest
from supabase import create_client

from config import bot, ADMIN_ID
from supabase_helper import admin_add_points, admin_get_global_stats, admin_search_user
from logger import get_logger

logger = get_logger(__name__)
router = Router()

# ==================== FSM States ====================
class AdminState(StatesGroup):
    waiting_for_amount = State()
    waiting_for_search_query = State()

def _is_admin(user_id: int) -> bool:
    return str(user_id) == str(ADMIN_ID)

# ==================== Safe Edit Function (The Fix) ====================
async def safe_edit_text(message: types.Message, text: str, reply_markup=None, parse_mode="HTML"):
    """دالة ذكية لتعديل الرسائل وتجنب أخطاء تليجرام عند التعامل مع الملفات أو النصوص المتطابقة"""
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        err_str = str(e).lower()
        if "message is not modified" in err_str:
            pass # تجاهل إذا كان النص مطابقاً
        elif "there is no text in the message to edit" in err_str:
            # إذا كانت الرسالة ملفاً (مثل CSV) ولا يمكن تعديل نصها، نحذفها ونرسل رسالة جديدة
            try:
                await message.delete()
            except:
                pass
            await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            logger.error(f"TelegramBadRequest in safe_edit_text: {e}")

# ==================== Keyboards ====================
def get_admin_dashboard_keyboard() -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="🔍 البحث عن مستخدم", callback_data="admin_search_prompt")],
        [types.InlineKeyboardButton(text="👥 استعراض الطلاب (مصفّح)", callback_data="admin_page_1")],
        [types.InlineKeyboardButton(text="📊 الإحصائيات", callback_data="admin_stats"),
         types.InlineKeyboardButton(text="📥 تصدير CSV", callback_data="admin_export_csv")],
        [types.InlineKeyboardButton(text="❌ إغلاق القائمة", callback_data="admin_cancel")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_users_keyboard(current_page: int, total_pages: int) -> types.InlineKeyboardMarkup:
    buttons = []
    if current_page > 1:
        buttons.append(types.InlineKeyboardButton(text="⬅️ السابق", callback_data=f"admin_page_{current_page-1}"))
    
    buttons.append(types.InlineKeyboardButton(text=f"📄 {current_page} / {total_pages}", callback_data="admin_page_noop"))
    
    if current_page < total_pages:
        buttons.append(types.InlineKeyboardButton(text="التالي ➡️", callback_data=f"admin_page_{current_page+1}"))
    
    kb = [
        buttons,
        [types.InlineKeyboardButton(text="📥 تصدير السجل بالكامل (CSV)", callback_data="admin_export_csv")],
        [types.InlineKeyboardButton(text="⚙️ العودة للوحة", callback_data="admin_main_menu")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_user_actions_keyboard(target_id: int) -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="💰 شحن نقاط", callback_data=f"admin_charge_menu_{target_id}")],
        [types.InlineKeyboardButton(text="⚙️ العودة للوحة", callback_data="admin_main_menu")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_charge_options_keyboard(target_id: int) -> types.InlineKeyboardMarkup:
    kb = [
        [
            types.InlineKeyboardButton(text="+10", callback_data=f"admin_charge_quick_10_{target_id}"),
            types.InlineKeyboardButton(text="+50", callback_data=f"admin_charge_quick_50_{target_id}"),
            types.InlineKeyboardButton(text="+100", callback_data=f"admin_charge_quick_100_{target_id}")
        ],
        [types.InlineKeyboardButton(text="✍️ إدخال كمية مخصصة (يدوي)", callback_data=f"admin_charge_manual_{target_id}")],
        [types.InlineKeyboardButton(text="🔙 إلغاء والعودة", callback_data="admin_main_menu")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_cancel_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="❌ إلغاء العملية", callback_data="admin_main_menu")]
    ])

def build_users_page_text(users_slice: list, start_idx: int, total_users: int) -> str:
    report = f"👥 <b>سجل الطلاب المسجلين (الإجمالي: {total_users}):</b>\n\n"
    for idx, u in enumerate(users_slice, start_idx):
        username = u.get('username')
        username_str = f"@{html.escape(username)}" if username and username != "Unknown" else "بلا يوزر"
        user_id = u.get('user_id')
        points = u.get('points', 0)
        report += f"<b>{idx}.</b> 🆔 <code>{user_id}</code> | 👤 {username_str} | 💰 <b>{points}</b> ن\n"
    return report

# ==================== Core Handlers ====================

@router.message(Command("admin"))
async def admin_cmd_dashboard(msg: types.Message, state: FSMContext):
    if not _is_admin(msg.from_user.id): return
    await state.clear()
    text = "⚙️ <b>لوحة تحكم الإدارة</b>\n\nأهلاً بك، اختر الإجراء الذي تود القيام به من القائمة أدناه:"
    await msg.answer(text, reply_markup=get_admin_dashboard_keyboard(), parse_mode="HTML")

@router.callback_query(F.data == "admin_main_menu")
async def admin_callback_main_menu(call: types.CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id): return
    await state.clear()
    text = "⚙️ <b>لوحة تحكم الإدارة</b>\n\nأهلاً بك، اختر الإجراء الذي تود القيام به من القائمة أدناه:"
    await safe_edit_text(call.message, text, reply_markup=get_admin_dashboard_keyboard())
    await call.answer()

@router.callback_query(F.data == "admin_cancel")
async def admin_cancel_action(call: types.CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id): return
    await state.clear()
    await safe_edit_text(call.message, "❌ تم إغلاق لوحة الإدارة.")
    await call.answer()

# --- Search Flow ---

@router.message(Command("searchuser"))
async def cmd_search_user(msg: types.Message, command: CommandObject):
    if not _is_admin(msg.from_user.id): return
    if not command.args:
        return await msg.answer("🔍 الرجاء إدخال اليوزر أو الآيدي للبحث. مثال: `/searchuser ahmad`")
    
    await execute_search(msg, command.args.strip())

@router.callback_query(F.data == "admin_search_prompt")
async def prompt_search_user(call: types.CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id): return
    await state.set_state(AdminState.waiting_for_search_query)
    await safe_edit_text(
        call.message,
        "🔍 <b>بحث عن مستخدم</b>\n\nأرسل الآن (الآيدي ID) أو (معرف المستخدم @Username):",
        reply_markup=get_cancel_keyboard()
    )
    await call.answer()

@router.message(AdminState.waiting_for_search_query)
async def process_search_user(msg: types.Message, state: FSMContext):
    if not _is_admin(msg.from_user.id): return
    await state.clear()
    await execute_search(msg, msg.text.strip())

async def execute_search(message: types.Message, query: str):
    users_data = await asyncio.to_thread(admin_search_user, query)
    if users_data:
        u = users_data[0]
        username = u.get('username')
        username_str = f"@{html.escape(username)}" if username and username != "Unknown" else "بدون يوزر"
        report = (
            "👤 <b>معلومات المستخدم:</b>\n"
            f"┣ 🆔 الآيدي: <code>{u['user_id']}</code>\n"
            f"┣ 👤 اليوزر: {username_str}\n"
            f"┣ 💰 النقاط الحالية: <code>{u['points']}</code>\n"
            f"┗ 📊 إجمالي الأسئلة المُولدة: <code>{u.get('total_questions', 0)}</code>"
        )
        await message.answer(report, reply_markup=get_admin_user_actions_keyboard(u['user_id']), parse_mode="HTML")
    else:
        await message.answer("❌ لم يتم العثور على أي مستخدم بهذا البحث.", reply_markup=get_cancel_keyboard())

# --- Charge Flow ---

@router.callback_query(F.data.startswith("admin_charge_menu_"))
async def show_charge_menu(call: types.CallbackQuery):
    if not _is_admin(call.from_user.id): return
    target_id = call.data.split("_")[3]
    await safe_edit_text(
        call.message,
        f"💰 <b>شحن رصيد للمستخدم</b> <code>{target_id}</code>\n\nاختر كمية الشحن السريعة أو اختر إدخالاً يدوياً:",
        reply_markup=get_admin_charge_options_keyboard(target_id)
    )
    await call.answer()

@router.callback_query(F.data.startswith("admin_charge_quick_"))
async def process_quick_charge(call: types.CallbackQuery):
    if not _is_admin(call.from_user.id): return
    parts = call.data.split("_")
    amount = int(parts[3])
    target_id = int(parts[4])
    
    new_balance = await asyncio.to_thread(admin_add_points, target_id, amount)
    if new_balance is not None:
        await safe_edit_text(
            call.message,
            f"✅ <b>تم الشحن بنجاح!</b>\n\nالمستخدم: <code>{target_id}</code>\nالكمية المضافة: <code>+{amount}</code> 🟢\nالرصيد الجديد: <code>{new_balance}</code> 💰",
            reply_markup=get_admin_dashboard_keyboard()
        )
    else:
        await call.answer("❌ حدث خطأ أثناء الشحن.", show_alert=True)

@router.callback_query(F.data.startswith("admin_charge_manual_"))
async def prompt_manual_charge(call: types.CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id): return
    target_id = call.data.split("_")[3]
    await state.update_data(target_id=target_id)
    await state.set_state(AdminState.waiting_for_amount)
    
    await safe_edit_text(
        call.message,
        f"✍️ <b>شحن يدوي</b>\n\nأرسل عدد النقاط المراد إضافتها للمستخدم <code>{target_id}</code> (أرقام فقط):",
        reply_markup=get_cancel_keyboard()
    )
    await call.answer()

@router.message(AdminState.waiting_for_amount)
async def process_manual_charge(msg: types.Message, state: FSMContext):
    if not _is_admin(msg.from_user.id): return
    if not msg.text.isdigit():
        return await msg.answer("❌ يرجى إرسال أرقام صحيحة فقط.", reply_markup=get_cancel_keyboard())
    
    amount = int(msg.text)
    data = await state.get_data()
    target_id = int(data.get('target_id'))
    
    new_balance = await asyncio.to_thread(admin_add_points, target_id, amount)
    if new_balance is not None:
        await msg.answer(
            f"✅ <b>تم الشحن بنجاح!</b>\n\nالمستخدم: <code>{target_id}</code>\nالكمية المضافة: <code>+{amount}</code> 🟢\nالرصيد الجديد: <code>{new_balance}</code> 💰",
            reply_markup=get_admin_dashboard_keyboard(),
            parse_mode="HTML"
        )
    else:
        await msg.answer("❌ حدث خطأ أثناء الشحن. حاول مجدداً.", reply_markup=get_cancel_keyboard())
    await state.clear()

# --- Stats & Exports Flow ---

@router.message(Command("dbstats"))
async def cmd_db_stats(msg: types.Message):
    if not _is_admin(msg.from_user.id): return
    stats = await asyncio.to_thread(admin_get_global_stats)
    text = f"📊 <b>إحصائيات النظام الحية:</b>\n\n👥 إجمالي الطلاب: <code>{stats['total_users']}</code>\n📝 إجمالي الأسئلة: <code>{stats['total_questions']}</code>"
    await msg.answer(text, reply_markup=get_admin_dashboard_keyboard(), parse_mode="HTML")

@router.callback_query(F.data == "admin_stats")
async def callback_db_stats(call: types.CallbackQuery):
    if not _is_admin(call.from_user.id): return
    stats = await asyncio.to_thread(admin_get_global_stats)
    text = f"📊 <b>إحصائيات النظام الحية:</b>\n\n👥 إجمالي الطلاب: <code>{stats['total_users']}</code>\n📝 إجمالي الأسئلة: <code>{stats['total_questions']}</code>"
    await safe_edit_text(call.message, text, reply_markup=get_admin_dashboard_keyboard())
    await call.answer()

@router.message(Command("fetchall"))
async def cmd_fetch_all(msg: types.Message):
    if not _is_admin(msg.from_user.id): return
    await render_users_page_logic(msg, 1)

@router.callback_query(F.data.startswith("admin_page_"))
async def callback_fetch_all_pages(call: types.CallbackQuery):
    if not _is_admin(call.from_user.id): return
    page_str = call.data.split("_")[2]
    if page_str == "noop":
        return await call.answer()
    await render_users_page_logic(call, int(page_str))
    await call.answer()

async def render_users_page_logic(event, page: int):
    try:
        supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
        res = supabase.table("users").select("*").order("joined_at", desc=True).execute()
        users = res.data
        
        if not users:
            text = "📭 لا يوجد أي طلاب مسجلين."
            if isinstance(event, types.Message):
                return await event.answer(text, reply_markup=get_admin_dashboard_keyboard(), parse_mode="HTML")
            else:
                return await safe_edit_text(event.message, text, reply_markup=get_admin_dashboard_keyboard())

        PER_PAGE = 5
        total_pages = (len(users) + PER_PAGE - 1) // PER_PAGE
        
        if page < 1: page = 1
        if page > total_pages: page = total_pages
            
        start_idx = (page - 1) * PER_PAGE
        end_idx = start_idx + PER_PAGE
        users_slice = users[start_idx:end_idx]
        
        report = build_users_page_text(users_slice, start_idx + 1, len(users))
        kb = get_admin_users_keyboard(page, total_pages)
        
        if isinstance(event, types.Message):
            await event.answer(text=report, reply_markup=kb, parse_mode="HTML")
        else:
            await safe_edit_text(event.message, report, reply_markup=kb)
            
    except Exception as e:
        logger.error(f"Error in render_users_page_logic: {e}")

@router.callback_query(F.data == "admin_export_csv")
async def admin_export_users_csv(call: types.CallbackQuery):
    if not _is_admin(call.from_user.id): return
    await call.answer("⏳ جاري تحضير ملف CSV...")
    
    try:
        supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
        res = supabase.table("users").select("*").order("joined_at", desc=True).execute()
        users = res.data
        
        if not users:
            return await safe_edit_text(call.message, "📭 لا يوجد طلاب مسجلين لتصديرهم.", reply_markup=get_admin_dashboard_keyboard())
        
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Index", "User ID", "Username", "Points", "Joined At"])
        
        for idx, u in enumerate(users, 1):
            writer.writerow([
                idx,
                u.get("user_id"),
                u.get("username", "Unknown"),
                u.get("points", 0),
                u.get("joined_at", "")
            ])
            
        csv_data = output.getvalue().encode('utf-8-sig') # لدعم اللغة العربية في اكسيل
        output.close()
        
        input_file = BufferedInputFile(csv_data, filename="students_report.csv")
        
        # نقوم بمسح الرسالة الحالية (إذا لم تكن ملفاً سابقاً)
        try:
            await call.message.delete()
        except:
            pass
            
        await call.message.answer_document(
            document=input_file,
            caption="📊 <b>تم تصدير سجل الطلاب بنجاح.</b>",
            reply_markup=get_admin_dashboard_keyboard(),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error exporting CSV: {e}")
        try:
            await safe_edit_text(call.message, "❌ حدث خطأ داخلي أثناء استخراج الملف.", reply_markup=get_admin_dashboard_keyboard())
        except:
            await call.message.answer("❌ حدث خطأ داخلي أثناء استخراج الملف.", reply_markup=get_admin_dashboard_keyboard())