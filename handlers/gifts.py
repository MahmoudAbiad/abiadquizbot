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


async def _resolve_lang(user_or_id):
    try:
        from services.i18n import get_user_language, normalize_language_code
        if isinstance(user_or_id, int):
            return normalize_language_code(await get_user_language(user_or_id))
        uid = getattr(user_or_id, 'id', None)
        if uid is not None:
            return normalize_language_code(await get_user_language(uid, fallback_user_or_code=user_or_id))
    except Exception:
        pass
    return 'ar'


def _w_strings(lang='ar'):
    if lang and lang != 'ar':
        try:
            from services.i18n import get_wallet_strings
            return get_wallet_strings(lang)
        except Exception:
            pass
    return None


def wallet_buttons(lang: str = 'ar'):
    ws = _w_strings(lang)
    if ws:
        return types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=ws['btn_recharge'], callback_data='recharge_info')],
            [types.InlineKeyboardButton(text=ws['btn_gifts'], callback_data='gift:list:0')],
            [types.InlineKeyboardButton(text=ws['btn_redeem'], callback_data='gift:redeem')],
            [types.InlineKeyboardButton(text=ws['btn_home'], callback_data='student_home')],
        ])
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
    lang = await _resolve_lang(event.from_user)
    ws = _w_strings(lang)
    message = event.message if isinstance(event, types.CallbackQuery) else event
    if user.get('status') == 'error':
        await message.answer(ws['fetch_error'] if ws else 'تعذر جلب الرصيد، حاول مجددًا.')
        return
    if ws:
        text = (
            f"{ws['balance_header'].format(points=user['points'])}\n\n"
            f"{ws['free_line'].format(free_points=user['free_points'])}\n"
            f"{ws['gift_line'].format(gift_points=user['gift_points'])}\n"
            f"{ws['paid_line'].format(paid_points=user['paid_points'])}\n\n"
            f"{ws['consumption_note']}"
        )
        kb = wallet_buttons(lang)
    else:
        text = (f"💰 <b>رصيدك: {user['points']:.2f} نقطة</b>\n\n"
                f"🔄 مجانية: {user['free_points']:.2f}\n"
                f"🎁 هدايا: {user['gift_points']:.2f}\n"
                f"💳 مدفوعة: {user['paid_points']:.2f}\n\n"
                'تُستهلك المجانية أولًا، ثم الهدايا الأقرب انتهاءً، ثم المدفوعة.\n'
                'الهدايا المنتهية أو الملغاة لا تدخل في الرصيد المتاح.')
        kb = wallet_buttons()
    if isinstance(event, types.CallbackQuery):
        await _show_code_screen(event, text, kb, parse_mode='HTML')
        await event.answer()
    else:
        await message.answer(text, parse_mode='HTML', reply_markup=kb)


@router.callback_query(F.data.startswith('gift:list:'))
async def show_gifts(call):
    lang = await _resolve_lang(call.from_user)
    ws = _w_strings(lang)
    raw_page = call.data.split(':')[-1]
    if not re.fullmatch(r'[0-9]{1,8}', raw_page):
        await call.answer(ws['page_unavailable'] if ws else 'صفحة غير متاحة. أعد فتح قائمة الهدايا.', show_alert=True)
        return
    page = int(raw_page)
    rows = await list_gifts(call.from_user.id, page, page_size=3)
    if page and not rows:
        # A gift may have been removed since this navigation button was sent.
        page = 0
        rows = await list_gifts(call.from_user.id, page, page_size=3)
    if ws:
        header = ws['gifts_header'].format(page=page + 1)
        empty_txt = ws['no_gifts']
        prev_lbl = ws['btn_prev']
        next_lbl = ws['btn_next']
        back_lbl = ws['btn_back_wallet']
    else:
        header = f'🎁 <b>هداياي وصلاحيتها · صفحة {page + 1}</b>'
        empty_txt = 'لا توجد هدايا في حسابك بعد.'
        prev_lbl = '⬅️ السابق'
        next_lbl = 'التالي ➡️'
        back_lbl = '🔙 رصيدي وهداياي'
    text = f'{header}\n\n' + ('\n\n'.join(gift_text(g, compact=True) for g in rows[:3]) or empty_txt)
    buttons = []
    if page:
        buttons.append(types.InlineKeyboardButton(text=prev_lbl, callback_data=f'gift:list:{page-1}'))
    if len(rows)>3:
        buttons.append(types.InlineKeyboardButton(text=next_lbl, callback_data=f'gift:list:{page+1}'))
    kb = ([buttons] if buttons else []) + [[types.InlineKeyboardButton(text=back_lbl, callback_data='gift:wallet')]]
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
    lang = await _resolve_lang(call.from_user)
    ws = _w_strings(lang)
    current = await state.get_state()
    if current and current != GiftState.code.state:
        await call.answer(ws['busy_alert'] if ws else 'أنهِ العملية الحالية أولًا أو ألغِها، ثم أدخل الكود.', show_alert=True)
        return
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=ws['btn_cancel'] if ws else 'إلغاء', callback_data='gift:cancel')]])
    await _show_code_screen(call, ws['prompt_code'] if ws else '🎟 أرسل كود الهدية:', keyboard)
    await state.set_state(GiftState.code)
    await call.answer()


@router.callback_query(F.data == 'gift:cancel')
async def cancel_code(call, state: FSMContext):
    lang = await _resolve_lang(call.from_user)
    ws = _w_strings(lang)
    if await state.get_state() != GiftState.code.state:
        await call.answer(ws['cancel_inactive'] if ws else 'طلب إدخال الكود لم يعد نشطاً.', show_alert=True)
        return
    await state.set_state(None)
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=ws['btn_return_home'] if ws else '🏠 العودة للرئيسية', callback_data='student_home')]])
    await _show_code_screen(call, ws['cancel_done'] if ws else '✅ تم إلغاء إدخال كود الهدية.', keyboard)
    await call.answer(ws['cancel_toast'] if ws else 'تم الإلغاء')


@router.message(GiftState.code, F.text, ~F.text.startswith('/'))
async def redeem_message(message, state: FSMContext):
    from services.usage_tracking import track_usage
    lang = await _resolve_lang(message.from_user)
    ws = _w_strings(lang)
    code = message.text.strip().upper()
    if not re.fullmatch(r'[A-Z0-9-]{12,64}', code):
        await message.answer(ws['invalid_format'] if ws else '❌ صيغة الكود غير صحيحة. انسخه كاملًا ثم أعد المحاولة.')
        return
    user = await ensure_user(message.from_user)
    if user.get('status') == 'error':
        await message.answer(ws['account_error'] if ws else 'تعذر الوصول للحساب. حاول مجددًا.')
        return
    result = await redeem(message.from_user.id, code)
    errors = ws['redeem_errors'] if ws else {
        'invalid':'الكود غير موجود.', 'expired':'انتهت مهلة شحن هذا الكود.',
        'disabled':'هذا الكود موقوف.', 'exhausted':'وصل الكود إلى العدد الأقصى للمستفيدين.',
        'already_used':'سبق لك استخدام هذا الكود. يمكن لكل طالب استخدامه مرة واحدة فقط.',
        'missing_user':'شغّل البوت باستخدام /start أولًا.'
    }
    default_err = errors.get('default', 'تعذر الشحن. حاول مجددًا.') if ws else 'تعذر الشحن. حاول مجددًا.'
    if result['status'] != 'ok':
        track_usage(message.from_user.id, "gift_code_rejected", {"reason": result['status']})
        await message.answer('❌ '+errors.get(result['status'], default_err))
        return
    await state.set_state(None)
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=ws['btn_create_quiz'] if ws else '📝 إنشاء كويز', callback_data='student_create_quiz')],
        [types.InlineKeyboardButton(text=ws['btn_back_wallet'] if ws else '🔙 رصيدي وهداياي', callback_data='gift:wallet')],
    ])
    track_usage(message.from_user.id, "gift_code_redeemed", {"points": result['points']})
    if ws:
        msg_txt = ws['redeem_success'].format(
            points=result['points'],
            remaining=remaining_expiry_text(result.get('expires_at')),
            expiry=expiry_text(result.get('expires_at')),
        )
    else:
        msg_txt = (f"✅ أُضيفت <b>{result['points']:g} نقطة هدية</b>.\n"
                   f"⏳ {remaining_expiry_text(result.get('expires_at'))}\n"
                   f"📅 انتهاء الهدية: {expiry_text(result.get('expires_at'))}")
    await message.answer(msg_txt, parse_mode='HTML', reply_markup=keyboard)
