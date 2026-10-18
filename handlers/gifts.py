"""Private student wallet, gift details and redemption flow."""
import re
from aiogram import Router, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from helpers.supabase_helper import check_or_add_user
from helpers.supabase_helper.gifts import list_gifts, redeem
from services.gift_presentation import gift_text, expiry_text, remaining_expiry_text

router = Router()
router.message.filter(F.chat.type == 'private')
router.callback_query.filter(F.message.chat.type == 'private')

class GiftState(StatesGroup):
    code = State()


def wallet_buttons():
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text='💳 شحن الرصيد', callback_data='recharge_info')],
        [types.InlineKeyboardButton(text='🎁 هداياي وصلاحيتها', callback_data='gift:list:0')],
        [types.InlineKeyboardButton(text='🎟 شحن كود هدية', callback_data='gift:redeem')],
        [types.InlineKeyboardButton(text='🔙 رجوع للقائمة الرئيسية', callback_data='student_home')],
    ])


async def ensure_user(user):
    return await check_or_add_user(user.id, user.username or 'Unknown', user.first_name, user.last_name or '')


@router.message(Command('wallet'))
@router.callback_query(F.data == 'gift:wallet')
async def wallet(event):
    user = await ensure_user(event.from_user)
    message = event.message if isinstance(event, types.CallbackQuery) else event
    if user.get('status') == 'error':
        await message.answer('تعذر جلب الرصيد، حاول مجددًا.')
        return
    text = (f"💰 <b>رصيدك: {user['points']:.2f} نقطة</b>\n\n"
            f"🔄 مجانية: {user['free_points']:.2f}\n"
            f"🎁 هدايا: {user['gift_points']:.2f}\n"
            f"💳 مدفوعة: {user['paid_points']:.2f}\n\n"
            'تُستهلك المجانية أولًا، ثم الهدايا الأقرب انتهاءً، ثم المدفوعة.\n'
            'الهدايا المنتهية أو الملغاة لا تدخل في الرصيد المتاح.')
    if isinstance(event, types.CallbackQuery):
        await _show_code_screen(event, text, wallet_buttons(), parse_mode='HTML')
        await event.answer()
    else:
        await message.answer(text, parse_mode='HTML', reply_markup=wallet_buttons())


@router.callback_query(F.data.startswith('gift:list:'))
async def show_gifts(call):
    raw_page = call.data.split(':')[-1]
    if not re.fullmatch(r'[0-9]{1,8}', raw_page):
        await call.answer('صفحة غير متاحة. أعد فتح قائمة الهدايا.', show_alert=True)
        return
    page = int(raw_page)
    rows = await list_gifts(call.from_user.id, page, page_size=3)
    if page and not rows:
        # A gift may have been removed since this navigation button was sent.
        page = 0
        rows = await list_gifts(call.from_user.id, page, page_size=3)
    text = f'🎁 <b>هداياي وصلاحيتها · صفحة {page + 1}</b>\n\n' + (
        '\n\n'.join(gift_text(g, compact=True) for g in rows[:3]) or 'لا توجد هدايا في حسابك بعد.')
    buttons = []
    if page:
        buttons.append(types.InlineKeyboardButton(text='⬅️ السابق', callback_data=f'gift:list:{page-1}'))
    if len(rows)>3:
        buttons.append(types.InlineKeyboardButton(text='التالي ➡️', callback_data=f'gift:list:{page+1}'))
    kb = ([buttons] if buttons else []) + [[types.InlineKeyboardButton(text='🔙 رصيدي وهداياي', callback_data='gift:wallet')]]
    await _show_code_screen(call, text, types.InlineKeyboardMarkup(inline_keyboard=kb), parse_mode='HTML')
    await call.answer()


async def _show_code_screen(call, text, keyboard, *, parse_mode=None):
    """Keep gift navigation inside the same message, with a fallback for old ones."""
    kwargs = {'reply_markup': keyboard}
    if parse_mode:
        kwargs['parse_mode'] = parse_mode
    try:
        await call.message.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        if 'message is not modified' in str(exc).lower():
            return
        await call.message.answer(text, **kwargs)
    except (AttributeError, TypeError):
        # Older callbacks might not expose an editable message.
        await call.message.answer(text, **kwargs)


@router.callback_query(F.data == 'gift:redeem')
async def prompt_code(call, state: FSMContext):
    current = await state.get_state()
    if current and current != GiftState.code.state:
        await call.answer('أنهِ العملية الحالية أولًا أو ألغِها، ثم أدخل الكود.', show_alert=True)
        return
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text='إلغاء', callback_data='gift:cancel')]])
    await _show_code_screen(call, '🎟 أرسل كود الهدية:', keyboard)
    await state.set_state(GiftState.code)
    await call.answer()


@router.callback_query(F.data == 'gift:cancel')
async def cancel_code(call, state: FSMContext):
    if await state.get_state() != GiftState.code.state:
        await call.answer('طلب إدخال الكود لم يعد نشطاً.', show_alert=True)
        return
    await state.set_state(None)
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text='🏠 العودة للرئيسية', callback_data='student_home')]])
    await _show_code_screen(call, '✅ تم إلغاء إدخال كود الهدية.', keyboard)
    await call.answer('تم الإلغاء')


@router.message(GiftState.code, F.text, ~F.text.startswith('/'))
async def redeem_message(message, state: FSMContext):
    code = message.text.strip().upper()
    if not re.fullmatch(r'[A-Z0-9-]{12,64}', code):
        await message.answer('❌ صيغة الكود غير صحيحة. انسخه كاملًا ثم أعد المحاولة.')
        return
    user = await ensure_user(message.from_user)
    if user.get('status') == 'error':
        await message.answer('تعذر الوصول للحساب. حاول مجددًا.')
        return
    result = await redeem(message.from_user.id, code)
    errors = {'invalid':'الكود غير موجود.', 'expired':'انتهت مهلة شحن هذا الكود.',
              'disabled':'هذا الكود موقوف.', 'exhausted':'وصل الكود إلى العدد الأقصى للمستفيدين.',
              'already_used':'سبق لك استخدام هذا الكود. يمكن لكل طالب استخدامه مرة واحدة فقط.',
              'missing_user':'شغّل البوت باستخدام /start أولًا.'}
    if result['status'] != 'ok':
        await message.answer('❌ '+errors.get(result['status'], 'تعذر الشحن. حاول مجددًا.'))
        return
    await state.set_state(None)
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text='📝 إنشاء كويز', callback_data='student_create_quiz')],
        [types.InlineKeyboardButton(text='🔙 رصيدي وهداياي', callback_data='gift:wallet')],
    ])
    await message.answer(f"✅ أُضيفت <b>{result['points']:g} نقطة هدية</b>.\n"
                         f"⏳ {remaining_expiry_text(result.get('expires_at'))}\n"
                         f"📅 انتهاء الهدية: {expiry_text(result.get('expires_at'))}",
                         parse_mode='HTML', reply_markup=keyboard)
