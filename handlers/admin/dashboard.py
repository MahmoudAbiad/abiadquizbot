# handlers/admin/dashboard.py
from aiogram import Router, types, F
from aiogram.filters import Command, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import TelegramBadRequest

from config import ADMIN_ID
from keyboards import get_admin_dashboard_keyboard
from logger import get_logger

logger = get_logger(__name__)
router = Router()


# ==================== فلتر الحماية المركزي للإدارة ====================

class IsAdminFilter(BaseFilter):
    """جدار حماية ذكي يفحص صلاحيات الآدمن للرسائل وضغطات الأزرار تلقائياً."""
    async def __call__(self, event: types.TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        if not user:
            return False
        return str(user.id) == str(ADMIN_ID)


# تطبيق الفلتر المركزي على كافة الأحداث الموجهة لهذا الراوتر
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())


# ==================== حالات الـ FSM للإدارة ====================

class AdminState(StatesGroup):
    waiting_for_search_query = State()
    waiting_for_charge_amount = State()
    waiting_for_broadcast_text = State()
    waiting_for_broadcast_confirm = State()


# ==================== الدالات المساعدة ====================

async def safe_edit_text(message: types.Message, text: str, reply_markup=None):
    """تعديل النص بشكل آمن يتفادى أخطاء التكرار في تيليجرام."""
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest:
        pass


async def render_admin_dashboard(event, state: FSMContext = None):
    """عرض لوحة التحكم الرئيسية وتصفير أي حالة معلقة."""
    if state:
        await state.clear()
        
    text = (
        "⚙️ <b>لوحة تحكم الإدارة</b>\n\n"
        "أهلاً بك، اختر الإجراء الذي تود القيام به من القائمة أدناه:"
    )
    reply_markup = get_admin_dashboard_keyboard()
    
    if isinstance(event, types.Message):
        await event.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    elif isinstance(event, types.CallbackQuery):
        await safe_edit_text(event.message, text, reply_markup=reply_markup)
        await event.answer()


# ==================== الأوامر والمستمعات الرئيسية ====================

@router.message(Command("admin"))
async def admin_cmd_dashboard(msg: types.Message, state: FSMContext):
    """استجابة أمر /admin."""
    await render_admin_dashboard(msg, state)


@router.callback_query(F.data == "admin_main_menu")
async def admin_callback_main_menu(call: types.CallbackQuery, state: FSMContext):
    """الرجوع للقائمة الرئيسية للإدارة."""
    await render_admin_dashboard(call, state)


@router.callback_query(F.data == "admin_cancel")
async def admin_cancel_action(call: types.CallbackQuery, state: FSMContext):
    """إغلاق القائمة وإلغاء العمليات."""
    await state.clear()
    await safe_edit_text(call.message, "❌ تم إغلاق لوحة الإدارة.")
    await call.answer()