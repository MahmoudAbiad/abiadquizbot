"""
handlers/payments.py
==============================================================================
واجهة الشحن الذاتي عبر شام كاش + حاسبة النقاط والباقات المخصصة:
  • الطالب: شاشة الباقات (مع زر «🧮 حاسبة النقاط» أسفل الخيارات السعرية) -> اختيار وسيلة الدفع
            -> بطاقة الطلب (QR + كود التحويل + رمز الملاحظة) -> «✅ دفعت» / «❌ إلغاء».
  • حاسبة النقاط (calc:*):
      - الشق الأول: معرفة سعر السؤال والصفحة وآلية الحساب بشكل مبسط.
      - الشق الثاني: تحديد حجم الاستهلاك يدوياً (بأزرار سريعة أو إدخال نصي) أو تلقائياً من
        سجل الاستخدام الفعلي (يظهر خيار الحساب التلقائي فقط للمستخدم النشط خلال آخر 14 يوماً)،
        واقتراح باقة مخصصة مع حساب خصم الكمية وتوضيح التوفير، والمتابعة السلسة لخطوة الشحن.
  • الأدمن (بمجموعة الدفع): أزرار pay:ok / pay:no / pay:ig. الشحن الفعلي دائماً عبر
    services.payment_service.credit_order (دالة ذرية)، والأزرار محصورة بمعرّفات الأدمن.
==============================================================================
"""

import html
import os
from typing import Optional
from urllib.parse import urlencode

from aiogram import Router, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from constants import (
    ADMIN_CONTACT, PAYMENT_ADMIN_GROUP_ID, PAYMENT_ORDER_TTL_MINUTES, PAYMENT_WEBHOOK_SECRET,
    SHAMCASH_QR_PATH, SHAMCASH_TRANSFER_CODE,
)
from handlers.admin.users import send_points_notification
from helpers.supabase_helper import is_feature_enabled, log_usage_event, set_feature_flag
from logger import get_logger
from services import i18n
from services import payment_service as ps
from services import points_calculator_service as pcs
from task_utils import spawn

logger = get_logger(__name__)
router = Router()


class CalculatorState(StatesGroup):
    """حالة استقبال إدخال نصي مخصص لحجم الاستهلاك أو عدد النقاط داخل حاسبة النقاط."""
    waiting_for_custom_input = State()


# ==================== جانب الطالب ====================

async def payments_available() -> bool:
    if not (PAYMENT_ADMIN_GROUP_ID and PAYMENT_WEBHOOK_SECRET):
        return False
    return await is_feature_enabled("payments_enabled", default=False)


def _support_row() -> list:
    return [types.InlineKeyboardButton(text="💬 تواصل مع الدعم", url=f"https://t.me/{ADMIN_CONTACT.lstrip('@')}")]


def _calculator_button_row() -> list:
    return [types.InlineKeyboardButton(text="🧮 حاسبة النقاط", callback_data="calc:menu")]


async def _edit_or_send(call: types.CallbackQuery, text: str, reply_markup: types.InlineKeyboardMarkup) -> None:
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
    except Exception:
        pass
    await call.message.answer(text, parse_mode="HTML", reply_markup=reply_markup)


async def try_show_payment_packages(
    call: types.CallbackQuery,
    edit: bool = False,
    currency_override: Optional[str] = None,
    selected_package: Optional[str] = None,
) -> bool:
    """يُستدعى من شاشة الشحن (recharge_info). يعرض الباقات حسب لغة المستخدم وعملته (SYP / USD)."""
    try:
        user_id = call.from_user.id
        lang = await i18n.get_user_language(user_id, fallback_user_or_code=call.from_user)
        non_syrian_phone = i18n.is_non_syrian_phone(call)
        currency = i18n.resolve_recharge_currency(
            lang,
            currency_override=currency_override,
            has_non_syrian_phone=non_syrian_phone,
        )

        # إذا كانت لغة المستخدم عربية والعملة المختارة هي الليرة السورية (SYP)، نفحص توفر الشحن الذاتي (شام كاش)
        if lang == "ar" and currency == "SYP" and selected_package is None and await payments_available():
            packages = await ps.get_active_packages()
            if packages:
                open_order = await ps.get_open_order_for_user(user_id)
                if open_order:
                    await _show_open_order(call, open_order)
                    return True

                rows = [[types.InlineKeyboardButton(
                    text=f"⭐ {p['points']} نقطة — {ps.format_money(p['price'], p['currency'])}" + (f" ({p['label']})" if p.get("label") else ""),
                    callback_data=f"pay:pkg:{p['id']}",
                )] for p in packages]
                rows.append([types.InlineKeyboardButton(
                    text=i18n.CURRENCY_TOGGLE_TO_USD_TEXT,
                    callback_data="recharge_currency:USD",
                )])
                rows.append(_calculator_button_row())
                rows.append(_support_row())
                text = (
                    "🔋 <b>شحن النقاط</b>\n\nاختر الباقة المناسبة، أو استخدم «🧮 حاسبة النقاط» "
                    "لمعرفة آلية الحساب واقتراح باقة مخصصة لحجم استهلاكك.\n"
                    "يُضاف الرصيد لحسابك فور تأكيد الدفعة. 🚀"
                )
                markup = types.InlineKeyboardMarkup(inline_keyboard=rows)
                if edit:
                    await _edit_or_send(call, text, markup)
                else:
                    await call.message.answer(text, parse_mode="HTML", reply_markup=markup)
                spawn(log_usage_event(user_id, "recharge_info_viewed"))
                return True

        # عرض شاشة الباقات (العربية بالليرة/الدولار مع زر التبديل، أو غير العربية بالدولار حصراً مع خصم الكمية وتلوين الأوفر)
        text, markup = i18n.build_recharge_view(
            lang_code=lang,
            currency_override=currency,
            selected_package=selected_package,
            has_non_syrian_phone=non_syrian_phone,
            admin_contact=ADMIN_CONTACT,
        )
        if edit:
            await _edit_or_send(call, text, markup)
        else:
            await call.message.answer(text, parse_mode="HTML", reply_markup=markup)
        spawn(log_usage_event(user_id, "recharge_info_viewed"))
        return True
    except Exception as exc:
        logger.error(f"try_show_payment_packages failed, falling back to support screen: {exc}", exc_info=True)
        return False


@router.callback_query(F.data.startswith("recharge_currency:"))
async def toggle_recharge_currency(call: types.CallbackQuery):
    """التبديل بين عرض الأسعار بالليرة السورية (SYP) والدولار الأمريكي (USD) لمستخدمي اللغة العربية."""
    try:
        target_currency = call.data.split(":", 1)[1].strip().upper()
        if target_currency not in ("SYP", "USD"):
            target_currency = "SYP"
        await try_show_payment_packages(call, edit=True, currency_override=target_currency)
    except Exception as exc:
        logger.error(f"toggle_recharge_currency failed: {exc}", exc_info=True)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("recharge_package:"))
async def select_Localized_recharge_package(call: types.CallbackQuery):
    """اختيار باقة شحن (بالليرة أو الدولار للعربية، أو باقات الدولار 400/900/1600 للغات الأخرى) وتجهيز مراسلة الدعم."""
    try:
        parts = call.data.split(":")
        if len(parts) == 3 and parts[1] == "ar_usd":
            currency_override = "USD"
            package = parts[2]
        elif len(parts) == 3 and parts[1] == "intl":
            currency_override = "USD"
            package = parts[2]
        else:
            currency_override = "SYP"
            package = parts[-1]

        await try_show_payment_packages(
            call,
            edit=True,
            currency_override=currency_override,
            selected_package=package,
        )
    except Exception as exc:
        logger.error(f"select_Localized_recharge_package failed: {exc}", exc_info=True)
    finally:
        await call.answer()


@router.callback_query(F.data == "recharge_choose_package")
async def remind_to_choose_recharge_package(call: types.CallbackQuery):
    lang = await i18n.get_user_language(call.from_user.id, fallback_user_or_code=call.from_user)
    await call.answer(i18n.get_choose_package_alert(lang), show_alert=True)


def _order_text(order: dict, auto: bool = False) -> str:
    lines = [
        "💳 <b>الدفع عبر شام كاش</b>",
        "",
        f"📦 <b>{order['points']}</b> نقطة",
        f"💵 المبلغ المطلوب: <b>{ps.format_money(order['amount'], order['currency'])}</b> (بالضبط)",
        "",
        "1️⃣ حوّل المبلغ إلى حسابنا بمسح الـ QR أعلاه، أو بكود التحويل:",
    ]
    if SHAMCASH_TRANSFER_CODE:
        lines.append(f"<code>{html.escape(SHAMCASH_TRANSFER_CODE)}</code>")
    lines += [
        "",
        "2️⃣ <b>مهم جداً:</b> الصق هذا الرمز في خانة <b>الملاحظة</b> عند التحويل (اضغط عليه لنسخه):",
        f"<code>{order['code']}</code>",
        "⚠️ <b>الملاحظة ضرورية جداً لتمييز حوالتك.</b> عدم وضع الرمز كما هو قد يؤدي إلى تعذر مطابقة الحوالة، والبوت غير مسؤول عن ضياع أي حوالة بسبب عدم التقيد بالتعليمات.",
        "",
        "3️⃣ بعد التحويل اضغط «✅ دفعت».",
        "",
        f"⏳ الطلب صالح {PAYMENT_ORDER_TTL_MINUTES} دقيقة. بدون الرمز في الملاحظة يتأخر شحنك لحين المراجعة اليدوية.",
    ]
    if auto:
        lines.append("⚡ عند تحويل المبلغ بالضبط مع الرمز بالملاحظة يُضاف رصيدك عادةً تلقائياً خلال دقائق. "
                     "وإن لم يتحقق ذلك تُراجَع الدفعة يدوياً وقد يستغرق الأمر من ساعة إلى 48 ساعة.")
    else:
        lines.append("⏱️ بعد تأكيد الدفع، تستغرق معالجة وإتمام عملية الشحن عادةً من ساعة إلى 48 ساعة.")
    return "\n".join(lines)


def _order_keyboard(order: dict) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ دفعت", callback_data=f"pay:paid:{order['id']}")],
        [types.InlineKeyboardButton(text="❌ إلغاء الطلب", callback_data=f"pay:cancel:{order['id']}")],
    ])


async def _send_order_screen(chat_id: int, order: dict, bot_obj) -> None:
    text = _order_text(order, auto=await ps.auto_confirm_active())
    markup = _order_keyboard(order)
    if SHAMCASH_QR_PATH and os.path.exists(SHAMCASH_QR_PATH):
        try:
            await bot_obj.send_photo(chat_id, types.FSInputFile(SHAMCASH_QR_PATH), caption=text,
                                     parse_mode="HTML", reply_markup=markup)
            return
        except Exception as exc:
            logger.warning(f"could not send Sham Cash QR photo, sending text only: {exc}")
    await bot_obj.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


async def _show_open_order(call: types.CallbackQuery, order: dict) -> None:
    if order["status"] == "pending":
        await _send_order_screen(call.message.chat.id, order, call.bot)
    else:
        await call.message.answer(
            f"⏳ طلبك <code>{order['code']}</code> ({order['points']} نقطة) قيد التحقق من الدفعة.\n"
            "سيصلك إشعار فور تأكيده. إن تأخر الأمر تواصل مع الدعم.",
            parse_mode="HTML",
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[_support_row()]),
        )


# ==================== 🧮 حاسبة النقاط والباقات المخصصة ====================

def _build_calculator_menu_keyboard(is_active_user: bool) -> types.InlineKeyboardMarkup:
    rows = [
        [types.InlineKeyboardButton(
            text="💡 سعر السؤال والصفحة وآلية الحساب",
            callback_data="calc:explain",
        )],
        [types.InlineKeyboardButton(
            text="🛠️ حساب الاستهلاك يدوياً واقتراح باقة",
            callback_data="calc:m:15:20:10",
        )],
    ]
    # الشرط الإلزامي: لا يظهر خيار الحساب التلقائي إلا إذا كان المستخدم نشطاً ولديه سجل استخدام
    if is_active_user:
        rows.append([types.InlineKeyboardButton(
            text="⚡ حساب تلقائي من سجل استخدامي",
            callback_data="calc:auto:14",
        )])
    rows.append([types.InlineKeyboardButton(text="⬅️ رجوع لباقات الشحن", callback_data="calc:back_recharge")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _build_calculator_menu_text(activity: dict) -> str:
    lines = [
        "🧮 <b>حاسبة النقاط والباقات المخصصة</b>",
        "",
        "اختر ما تريده من الخيارات أدناه:",
        "",
        "1️⃣ <b>سعر السؤال والصفحة وآلية الحساب:</b>",
        "تعرّف بشكل مبسط وواضح على تكلفة كل صفحة وسؤال وكيف تُحسب نقاط الكويز.",
        "",
        "2️⃣ <b>تحديد حجم استهلاكك واقتراح باقة مخصصة:</b>",
        "احسب احتياجك الفعلي واحصل على باقة مفصّلة على مقاسك مع <b>خصم كمية</b> يوضّح لك كم توفّر بالليرة السورية، وتابع الشحن بضغطة واحدة.",
    ]
    if activity.get("is_active"):
        lines.extend([
            "",
            f"✨ <b>متاح لك الحساب التلقائي:</b> رصدنا نشاطك خلال آخر {activity['window_days']} يوماً "
            f"(<code>{activity['valid_ops']}</code> عمليات باستهلاك <code>{activity['total_spent']:g}</code> نقطة) — "
            "يمكنك اختيار «⚡ حساب تلقائي من سجل استخدامي» لاقتراح الباقة الأنسب لنمط دراستك فوراً!",
        ])
    return "\n".join(lines)


@router.callback_query(F.data == "calc:menu")
async def show_calculator_menu(call: types.CallbackQuery, state: Optional[FSMContext] = None):
    try:
        if state is not None and await state.get_state() == CalculatorState.waiting_for_custom_input.state:
            await state.set_state(None)
        activity = await pcs.get_user_activity_summary(call.from_user.id)
        text = _build_calculator_menu_text(activity)
        markup = _build_calculator_menu_keyboard(bool(activity.get("is_active")))
        await _edit_or_send(call, text, markup)
    except Exception as exc:
        logger.error(f"show_calculator_menu failed: {exc}", exc_info=True)
    finally:
        await call.answer()


@router.callback_query(F.data == "calc:back_recharge")
async def back_to_recharge_packages(call: types.CallbackQuery, state: Optional[FSMContext] = None):
    try:
        if state is not None and await state.get_state() == CalculatorState.waiting_for_custom_input.state:
            await state.set_state(None)
        if await try_show_payment_packages(call, edit=True):
            return
        from handlers.start import get_recharge_keyboard
        recharge_text = (
            "🔋 <b>شحن النقاط</b>\n\n"
            "اختر الباقة المطلوبة، ثم اضغط «تواصل للشحن» لمعرفة طريقة الدفع، "
            "أو استخدم «🧮 حاسبة النقاط» لتصميم باقة مخصصة.\n\n"
            "💵 الأسعار بالليرة السورية."
        )
        await _edit_or_send(call, recharge_text, get_recharge_keyboard())
    except Exception as exc:
        logger.error(f"back_to_recharge_packages failed: {exc}", exc_info=True)
    finally:
        await call.answer()


@router.callback_query(F.data == "calc:explain")
async def show_pricing_explanation(call: types.CallbackQuery):
    try:
        activity = await pcs.get_user_activity_summary(call.from_user.id)
        text = await pcs.build_pricing_explanation_text()
        rows = [
            [types.InlineKeyboardButton(
                text="🛠️ احسب استهلاكي يدوياً واقترح باقة",
                callback_data="calc:m:15:20:10",
            )],
        ]
        if activity.get("is_active"):
            rows.append([types.InlineKeyboardButton(
                text="⚡ حساب تلقائي من سجل استخدامي",
                callback_data="calc:auto:14",
            )])
        rows.append([
            types.InlineKeyboardButton(text="🔙 حاسبة النقاط", callback_data="calc:menu"),
            types.InlineKeyboardButton(text="⬅️ باقات الشحن", callback_data="calc:back_recharge"),
        ])
        await _edit_or_send(call, text, types.InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception as exc:
        logger.error(f"show_pricing_explanation failed: {exc}", exc_info=True)
    finally:
        await call.answer()


def _charge_action_rows(quote: dict) -> list:
    """أزرار المتابعة السلسة لخطوة الشحن للباقة المخصصة المقترحة (+ زر الترقية للشريحة الأعلى إن وُجدت)."""
    pts = quote["points"]
    price = quote["price"]
    discount_pct = quote["discount_pct"]
    saved = quote["saved"]
    unit = "ل.س" if quote.get("currency", "SYP") == "SYP" else "$"

    btn_label = f"💳 متابعة شحن {pts} نقطة ({price} {unit})"
    if discount_pct > 0 and saved > 0:
        btn_label = f"💳 شحن {pts} نقطة بـ {price} {unit} (توفير {saved} {unit})"

    rows = [[types.InlineKeyboardButton(text=btn_label, callback_data=f"pay:custom:{pts}")]]
    hint = quote.get("next_tier_hint")
    if hint:
        rows.append([types.InlineKeyboardButton(
            text=f"🔥 شحن {hint['points']} نقطة بـ {hint['price']} {unit} (خصم {hint['discount_pct']}% · توفير {hint['saved']} {unit})",
            callback_data=f"pay:custom:{hint['points']}",
        )])
    return rows


@router.callback_query(F.data.startswith("calc:auto:"))
async def show_auto_calculation(call: types.CallbackQuery):
    try:
        parts = call.data.split(":")
        target_days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 14
        if target_days not in (7, 14, 30):
            target_days = 14

        activity = await pcs.get_user_activity_summary(call.from_user.id)
        if not activity.get("is_active"):
            await call.answer(
                "الحساب التلقائي يظهر فقط عند وجود سجل نشاط كافٍ في آخر 14 يوماً. يمكنك استخدام الحساب اليدوي الآن.",
                show_alert=True,
            )
            return await _render_manual_calculator(call, 15, 20, 10)

        rec = pcs.build_auto_recommendation(activity, target_days=target_days)
        quote = rec["quote"]

        period_labels = {7: "أسبوع (7 أيام)", 14: "أسبوعين (14 يوماً)", 30: "شهر (30 يوماً)"}
        lines = [
            "⚡ <b>الحساب التلقائي الذكي (بناءً على سجل استخدامك)</b>",
            "",
            f"📊 <b>ملخص نشاطك الفعلي خلال آخر {activity['window_days']} يوماً:</b>",
            f"• عدد العمليات المنفذة: <b>{activity['valid_ops']}</b> عملية (في <b>{activity['active_days']}</b> أيام دراسة نشطة)",
            f"• إجمالي النقاط المستهلكة: <code>{activity['total_spent']:g}</code> نقطة",
            f"• متوسط استهلاكك باليوم النشط: <code>~{activity['avg_per_active_day']:g}</code> نقطة",
            "",
            f"🎯 <b>استهلاكك المتوقع لمدة {period_labels.get(target_days, f'{target_days} يوم')}:</b> "
            f"<code>~{rec['projected_points']}</code> نقطة",
            "",
            *pcs.format_quote_block(quote),
            "",
            "👇 يمكنك تغيير مدة التغطية أو المتابعة مباشرة لخطوة الشحن:",
        ]

        period_row = [
            types.InlineKeyboardButton(
                text=f"{'✅ ' if d == target_days else ''}{lbl}",
                callback_data=f"calc:auto:{d}",
            )
            for d, lbl in ((7, "7 أيام"), (14, "14 يوماً"), (30, "30 يوماً"))
        ]
        rows = [
            period_row,
            *_charge_action_rows(quote),
            [types.InlineKeyboardButton(text="🛠️ التبديل للحساب اليدوي", callback_data="calc:m:15:20:10")],
            [
                types.InlineKeyboardButton(text="🔙 حاسبة النقاط", callback_data="calc:menu"),
                types.InlineKeyboardButton(text="⬅️ باقات الشحن", callback_data="calc:back_recharge"),
            ],
        ]
        await _edit_or_send(call, "\n".join(lines), types.InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception as exc:
        logger.error(f"show_auto_calculation failed: {exc}", exc_info=True)
    finally:
        await call.answer()


def _build_manual_calculator_keyboard(
    pages: int, questions: int, quizzes: int, quote: dict, is_active_user: bool = False
) -> types.InlineKeyboardMarkup:
    page_presets = [5, 15, 30, 50]
    question_presets = [10, 20, 30, 50]
    quiz_presets = [5, 10, 20, 30]

    row_pages = [
        types.InlineKeyboardButton(
            text=f"{'✅ ' if p == pages else ''}📄 {p}ص",
            callback_data=f"calc:m:{p}:{questions}:{quizzes}",
        )
        for p in page_presets
    ]
    row_questions = [
        types.InlineKeyboardButton(
            text=f"{'✅ ' if q == questions else ''}❓ {q}س",
            callback_data=f"calc:m:{pages}:{q}:{quizzes}",
        )
        for q in question_presets
    ]
    row_quizzes = [
        types.InlineKeyboardButton(
            text=f"{'✅ ' if k == quizzes else ''}🔁 {k}",
            callback_data=f"calc:m:{pages}:{questions}:{k}",
        )
        for k in quiz_presets
    ]

    rows = [
        row_pages,
        row_questions,
        row_quizzes,
        [types.InlineKeyboardButton(
            text="✍️ إدخال أرقام أو كمية نقاط مخصصة",
            callback_data=f"calc:inp:{pages}:{questions}:{quizzes}",
        )],
        *_charge_action_rows(quote),
    ]
    if is_active_user:
        rows.append([types.InlineKeyboardButton(
            text="⚡ حساب تلقائي من سجل استخدامي",
            callback_data="calc:auto:14",
        )])
    rows.append([
        types.InlineKeyboardButton(text="🔙 حاسبة النقاط", callback_data="calc:menu"),
        types.InlineKeyboardButton(text="⬅️ باقات الشحن", callback_data="calc:back_recharge"),
    ])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_manual_calculator(
    call: types.CallbackQuery, pages: int, questions: int, quizzes: int
) -> None:
    rec = await pcs.build_manual_recommendation(pages, questions, quizzes)
    activity = await pcs.get_user_activity_summary(call.from_user.id)
    quote = rec["quote"]
    lines = [
        "🛠️ <b>حاسبة الاستهلاك اليدوية</b>",
        "",
        "حدّد متوسط استخدامك بالأزرار أدناه (أو اضغط «✍️ إدخال أرقام أو كمية نقاط مخصصة»):",
        f"• 📄 حجم الملف الواحد: <b>{rec['pages']} صفحة</b>",
        f"• ❓ الأسئلة في كل اختبار: <b>{rec['questions']} سؤالاً</b>",
        f"• 🔁 عدد الاختبارات المطلوبة: <b>{rec['quizzes_count']} اختبار</b>",
        "",
        f"🧮 تكلفة الاختبار الواحد: <code>{rec['cost_per_quiz']:g}</code> نقطة",
        f"📊 إجمالي النقاط اللازمة لاستهلاكك: <code>{rec['raw_total_points']:g}</code> نقطة",
        "",
        *pcs.format_quote_block(quote),
        "",
        "👇 عدّل الأرقام من الأزرار أو تابع مباشرة إلى خطوة الشحن:",
    ]
    markup = _build_manual_calculator_keyboard(
        rec["pages"], rec["questions"], rec["quizzes_count"], quote,
        is_active_user=bool(activity.get("is_active")),
    )
    await _edit_or_send(call, "\n".join(lines), markup)


@router.callback_query(F.data.startswith("calc:m:"))
async def handle_manual_calculator(call: types.CallbackQuery, state: Optional[FSMContext] = None):
    try:
        if state is not None and await state.get_state() == CalculatorState.waiting_for_custom_input.state:
            await state.set_state(None)
        parts = call.data.split(":")
        pages = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 15
        questions = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 20
        quizzes = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 10
        await _render_manual_calculator(call, pages, questions, quizzes)
    except Exception as exc:
        logger.error(f"handle_manual_calculator failed: {exc}", exc_info=True)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("calc:inp:"))
async def prompt_custom_calculator_input(call: types.CallbackQuery, state: FSMContext):
    try:
        current = await state.get_state()
        if current and current != CalculatorState.waiting_for_custom_input.state:
            await call.answer(
                "لديك عملية جارية حالياً (اختبار أو رفع ملف). يمكنك استخدام أزرار التعديل السريعة أعلاه مباشرة دون مقاطعة عمليتك!",
                show_alert=True,
            )
            return
        parts = call.data.split(":")
        pages = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 15
        questions = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 20
        quizzes = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 10
        await state.set_state(CalculatorState.waiting_for_custom_input)
        text = (
            "✍️ <b>إدخال مخصص لحجم الاستهلاك</b>\n\n"
            "أرسل الآن برسالة واحدة أحد الخيارين:\n"
            "1️⃣ <b>عدد النقاط مباشرة</b> (بين 50 و 10000 نقطة) — مثال:\n"
            "<code>450</code>\n\n"
            "2️⃣ أو <b>(عدد الصفحات، عدد الأسئلة، عدد الاختبارات)</b> مفصولة بمسافة — مثال:\n"
            "<code>12 25 8</code> (يعني 12 صفحة و25 سؤالاً لـ 8 اختبارات)"
        )
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(
                text="🔙 رجوع للحاسبة اليدوية",
                callback_data=f"calc:m:{pages}:{questions}:{quizzes}",
            )],
        ])
        await _edit_or_send(call, text, kb)
    except Exception as exc:
        logger.error(f"prompt_custom_calculator_input failed: {exc}", exc_info=True)
    finally:
        await call.answer()


@router.message(CalculatorState.waiting_for_custom_input, F.text, ~F.text.startswith("/"))
async def process_custom_calculator_input(message: types.Message, state: FSMContext):
    parsed = pcs.parse_custom_manual_input(message.text or "")
    if not parsed:
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔙 رجوع للحاسبة اليدوية", callback_data="calc:m:15:20:10")],
        ])
        await message.answer(
            "❌ صيغة غير واضحة.\n"
            "أرسل عدد النقاط المطلوب مباشرة (من <code>50</code> إلى <code>10000</code>، مثل <code>450</code>)، "
            "أو أرسل 3 أرقام تمثل <code>صفحات أسئلة اختبارات</code> (مثل <code>10 20 8</code>).",
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    await state.set_state(None)
    if parsed["mode"] == "direct_points":
        pts = pcs.normalize_custom_points(parsed["points"])
        quote = pcs.build_custom_package_quote(pts)
        lines = [
            "🎯 <b>باقة مخصصة حسب الكمية المطلوبة</b>",
            "",
            f"• النقاط التي طلبتها: <code>{parsed['points']}</code> نقطة",
            "",
            *pcs.format_quote_block(quote),
            "",
            "👇 يمكنك المتابعة مباشرة إلى خطوة الشحن بضغطة واحدة:",
        ]
        rows = [
            *_charge_action_rows(quote),
            [types.InlineKeyboardButton(text="🛠️ تعديل بالحاسبة اليدوية", callback_data="calc:m:15:20:10")],
            [types.InlineKeyboardButton(text="⬅️ باقات الشحن", callback_data="calc:back_recharge")],
        ]
        await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows))
        return

    rec = await pcs.build_manual_recommendation(
        parsed["pages"], parsed["questions"], parsed["quizzes_count"]
    )
    activity = await pcs.get_user_activity_summary(message.from_user.id)
    quote = rec["quote"]
    lines = [
        "🛠️ <b>نتيجة حساب استهلاكك المخصص</b>",
        "",
        f"• 📄 حجم الملف الواحد: <b>{rec['pages']} صفحة</b>",
        f"• ❓ الأسئلة في كل اختبار: <b>{rec['questions']} سؤالاً</b>",
        f"• 🔁 عدد الاختبارات المطلوبة: <b>{rec['quizzes_count']} اختبار</b>",
        "",
        f"🧮 تكلفة الاختبار الواحد: <code>{rec['cost_per_quiz']:g}</code> نقطة",
        f"📊 إجمالي النقاط اللازمة لاستهلاكك: <code>{rec['raw_total_points']:g}</code> نقطة",
        "",
        *pcs.format_quote_block(quote),
        "",
        "👇 تابع مباشرة إلى خطوة الشحن أو عدّل الأرقام:",
    ]
    markup = _build_manual_calculator_keyboard(
        rec["pages"], rec["questions"], rec["quizzes_count"], quote,
        is_active_user=bool(activity.get("is_active")),
    )
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=markup)


# ==================== متابعة الشحن السلسة للباقة المخصصة ====================

def _build_support_custom_charge_url(quote: dict) -> str:
    pts = quote["points"]
    price = quote["price"]
    discount_pct = quote["discount_pct"]
    saved = quote["saved"]
    base_price = quote["base_price"]
    if discount_pct > 0 and saved > 0:
        draft = (
            f"مرحباً، أرغب بشحن باقة مخصصة ({pts} نقطة) بسعر {price} ل.س "
            f"(خصم كمية {discount_pct}% — توفير {saved} ل.س بدلاً من {base_price} ل.س). ما طريقة الدفع؟"
        )
    else:
        draft = f"مرحباً، أرغب بشحن باقة مخصصة ({pts} نقطة) بسعر {price} ل.س. ما طريقة الدفع؟"
    return f"https://t.me/{ADMIN_CONTACT.lstrip('@')}?{urlencode({'text': draft})}"


@router.callback_query(F.data.startswith("pay:custom:"))
async def proceed_custom_package(call: types.CallbackQuery):
    """المتابعة السلسة من الحاسبة إلى خطوة الشحن (سواء عبر شام كاش الذاتي أو عبر التواصل المباشر مع الدعم)."""
    try:
        raw_pts = call.data.split(":")[2]
        if not raw_pts.isdigit():
            await call.answer("باقة غير صالحة.", show_alert=True)
            return
        quote = pcs.build_custom_package_quote(int(raw_pts))
        pts = quote["points"]
        price_str = ps.format_money(quote["price"], quote["currency"])

        if await payments_available():
            open_order = await ps.get_open_order_for_user(call.from_user.id)
            if open_order:
                await _show_open_order(call, open_order)
                return
            savings_line = (
                f"\n✨ خصم الكمية المطبق: <b>{quote['discount_pct']}%</b> "
                f"(وفّرت <b>{ps.format_money(quote['saved'], quote['currency'])}</b> بدلاً من {ps.format_money(quote['base_price'], quote['currency'])})"
                if quote["discount_pct"] > 0 and quote["saved"] > 0
                else ""
            )
            markup = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="💳 شام كاش", callback_data=f"pay:cm:{pts}:sc")],
                [types.InlineKeyboardButton(text="🧮 تعديل في حاسبة النقاط", callback_data="calc:menu")],
                [types.InlineKeyboardButton(text="⬅️ رجوع لباقات الشحن", callback_data="calc:back_recharge")],
            ])
            await _edit_or_send(
                call,
                f"📦 <b>باقة مخصصة: {pts} نقطة</b> — <b>{price_str}</b>{savings_line}\n\nاختر وسيلة الدفع للمتابعة:",
                markup,
            )
            return

        # عندما يكون الشحن الذاتي غير مفعّل، نوجّه الطالب بسلاسة لخطوة الشحن عبر الدعم برسالة جاهزة مملوءة تلقائياً
        support_url = _build_support_custom_charge_url(quote)
        savings_details = (
            f"🏷️ السعر قبل الخصم: <s>{quote['base_price']} ل.س</s>\n"
            f"✨ السعر بعد خصم الكمية ({quote['discount_pct']}%): <b>{quote['price']} ل.س</b>\n"
            f"💰 <b>توفيرك في هذه الباقة: {quote['saved']} ل.س</b>\n\n"
            if quote["discount_pct"] > 0 and quote["saved"] > 0
            else f"💵 المبلغ المطلوب: <b>{quote['price']} ل.س</b>\n\n"
        )
        text = (
            f"🔋 <b>تأكيد شحن الباقة المخصصة</b>\n\n"
            f"📦 الباقة المختارة: <b>{pts} نقطة</b>\n"
            f"{savings_details}"
            "اضغط على زر «💬 إرسال طلب الشحن الآن» أدناه لفتح المحادثة مع الدعم بتفاصيل باقتك وسعرها المخفض جاهزة للإرسال فوراً: 🚀"
        )
        markup = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(
                text=f"💬 إرسال طلب الشحن الآن ({pts} نقطة — {quote['price']} ل.س)",
                url=support_url,
            )],
            [types.InlineKeyboardButton(text="🧮 تعديل الباقة في الحاسبة", callback_data="calc:menu")],
            [types.InlineKeyboardButton(text="⬅️ عرض باقات الشحن", callback_data="calc:back_recharge")],
        ])
        await _edit_or_send(call, text, markup)
    except Exception as exc:
        logger.error(f"proceed_custom_package failed: {exc}", exc_info=True)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("pay:cm:"))
async def create_custom_order_for_method(call: types.CallbackQuery):
    """إنشاء طلب شحن فعلي عبر شام كاش للباقة المخصصة المقترحة من الحاسبة."""
    try:
        _, _, raw_pts, method = call.data.split(":")
        if method != "sc" or not raw_pts.isdigit() or not await payments_available():
            await call.answer("طريقة الدفع غير متاحة حالياً.", show_alert=True)
            return
        quote = pcs.build_custom_package_quote(int(raw_pts))
        try:
            order = await ps.create_order(call.from_user.id, quote)
        except ps.OpenOrderExists:
            existing = await ps.get_open_order_for_user(call.from_user.id)
            if existing:
                await _show_open_order(call, existing)
            return
        await _send_order_screen(call.message.chat.id, order, call.bot)
        spawn(log_usage_event(call.from_user.id, "payment_order_created", {
            "order_id": order["id"],
            "points": order["points"],
            "custom_package": True,
            "discount_pct": quote["discount_pct"],
        }))
    except Exception as exc:
        logger.error(f"create_custom_order_for_method failed: {exc}", exc_info=True)
        await call.message.answer("⚠️ تعذّر إنشاء طلب الشحن الآن، حاول بعد قليل أو تواصل مع الدعم.")
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("pay:pkg:"))
async def choose_method(call: types.CallbackQuery):
    try:
        package_id = int(call.data.split(":")[2])
        if not await payments_available():
            await call.answer("الشحن الذاتي غير متاح حالياً، تواصل مع الدعم.", show_alert=True)
            return
        package = await ps.get_package(package_id)
        if not package:
            await call.answer("هذه الباقة لم تعد متاحة.", show_alert=True)
            return
        markup = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="💳 شام كاش", callback_data=f"pay:m:{package_id}:sc")],
            [types.InlineKeyboardButton(text="⬅️ رجوع", callback_data="recharge_info")],
        ])
        await call.message.answer(
            f"📦 <b>{package['points']} نقطة</b> — {ps.format_money(package['price'], package['currency'])}\n\nاختر وسيلة الدفع:",
            parse_mode="HTML", reply_markup=markup,
        )
    except Exception as exc:
        logger.error(f"choose_method failed: {exc}", exc_info=True)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("pay:m:"))
async def create_order_for_method(call: types.CallbackQuery):
    try:
        _, _, package_id, method = call.data.split(":")
        if method != "sc" or not await payments_available():
            await call.answer("طريقة الدفع غير متاحة حالياً.", show_alert=True)
            return
        package = await ps.get_package(int(package_id))
        if not package:
            await call.answer("هذه الباقة لم تعد متاحة.", show_alert=True)
            return
        try:
            order = await ps.create_order(call.from_user.id, package)
        except ps.OpenOrderExists:
            existing = await ps.get_open_order_for_user(call.from_user.id)
            if existing:
                await _show_open_order(call, existing)
            return
        await _send_order_screen(call.message.chat.id, order, call.bot)
        spawn(log_usage_event(call.from_user.id, "payment_order_created",
                              {"order_id": order["id"], "points": order["points"]}))
    except Exception as exc:
        logger.error(f"create_order_for_method failed: {exc}", exc_info=True)
        await call.message.answer("⚠️ تعذّر إنشاء طلب الشحن الآن، حاول بعد قليل أو تواصل مع الدعم.")
    finally:
        await call.answer()


async def _already_paid_for_user(call: types.CallbackQuery, order_id: int) -> bool:
    """الشحن التلقائي قد يشحن الطلب قبل ضغط الطالب «دفعت/إلغاء»: نخبره بالحقيقة ونزيل الأزرار الباقية."""
    order = await ps.get_order(order_id)
    if not order or order.get("user_id") != call.from_user.id or order.get("status") != "paid":
        return False
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await call.answer("✅ تم شحن رصيدك بالفعل.", show_alert=True)
    return True


@router.callback_query(F.data.startswith("pay:paid:"))
async def user_claims_paid(call: types.CallbackQuery):
    try:
        order_id = int(call.data.split(":")[2])
        order = await ps.claim_order(order_id, call.from_user.id)
        if not order:
            if await _already_paid_for_user(call, order_id):
                return
            await call.answer("لا يمكن تأكيد هذا الطلب (منتهٍ أو مؤكد مسبقاً).", show_alert=True)
            return
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await call.message.answer(
            "✅ <b>استلمنا تأكيدك</b>\nنتحقق من دفعتك الآن، وسيصلك إشعار فور إضافة النقاط. 🙏",
            parse_mode="HTML",
        )
        await ps.notify_claim(order)
    except Exception as exc:
        logger.error(f"user_claims_paid failed: {exc}", exc_info=True)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("pay:cancel:"))
async def user_cancels(call: types.CallbackQuery):
    try:
        order_id = int(call.data.split(":")[2])
        if await ps.cancel_order(order_id, call.from_user.id):
            try:
                await call.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
            await call.message.answer("تم إلغاء الطلب. يمكنك بدء طلب جديد بأي وقت من «شحن الرصيد».")
        else:
            if await _already_paid_for_user(call, order_id):
                return
            await call.answer("لا يمكن إلغاء هذا الطلب (مؤكد مسبقاً أو منتهٍ).", show_alert=True)
            return
    except Exception as exc:
        logger.error(f"user_cancels failed: {exc}", exc_info=True)
    finally:
        await call.answer()


# ==================== جانب الأدمن (مجموعة الدفع) ====================

async def _finish_group_message(call: types.CallbackQuery, suffix: str) -> None:
    try:
        await call.message.edit_text(f"{call.message.html_text}\n\n{suffix}", parse_mode="HTML", reply_markup=None)
    except TelegramBadRequest:
        pass


async def _strip_order_message_buttons(order: Optional[dict], clicked: types.Message) -> None:
    """يشيل أزرار بطاقة 'طالب أكد الدفع' الأصلية لو كانت رسالة مختلفة عن اللي انضغطت."""
    if not order or not order.get("group_message_id") or not order.get("group_chat_id"):
        return
    if order["group_message_id"] == clicked.message_id and order["group_chat_id"] == clicked.chat.id:
        return
    try:
        await clicked.bot.edit_message_reply_markup(
            chat_id=order["group_chat_id"], message_id=order["group_message_id"], reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("pay:ok:"))
async def admin_confirm(call: types.CallbackQuery):
    if not ps.is_payment_admin(call.from_user.id):
        await call.answer("غير مصرّح لك.", show_alert=True)
        return
    try:
        _, _, order_id, receipt_id = call.data.split(":")
        order_id, receipt_id = int(order_id), int(receipt_id) or None
        actor = f"tg:{call.from_user.id}"
        result = await ps.credit_order(order_id, receipt_id, actor)
        status = result.get("result")
        who = html.escape(call.from_user.full_name or str(call.from_user.id))

        if status == "ok":
            user_id = int(result["order_user_id"])
            points = int(result["credited_points"])
            total = int(result.get("new_total_points") or 0)
            await _finish_group_message(call, f"✅ <b>تم شحن {points} نقطة</b> بواسطة {who}")
            await _strip_order_message_buttons(await ps.get_order(order_id), call.message)
            await send_points_notification(user_id, points, total)
            spawn(log_usage_event(user_id, "payment_credited", {"order_id": order_id, "points": points, "by": actor}))
            await call.answer("تم الشحن ✅")
            return

        messages = {
            "already_paid": "هذا الطلب مشحون مسبقاً — لم يُضف شيء.",
            "not_creditable": "الطلب مرفوض أو ملغى — لا يمكن شحنه.",
            "receipt_used": "هذا الإيصال استُخدم بالفعل لشحن طلب آخر.",
            "receipt_not_creditable": "هذا الإيصال معلَّم كتجاهل/خط أساس — لا يمكن استخدامه للشحن.",
            "receipt_not_found": "الإيصال غير موجود.",
            "user_not_found": "حساب الطالب غير موجود.",
            "not_found": "الطلب غير موجود.",
        }
        await call.answer(messages.get(status, "تعذّر تنفيذ الشحن، راجع السجلات."), show_alert=True)
        if status in ("already_paid", "not_creditable", "receipt_used", "receipt_not_creditable"):
            await _finish_group_message(call, f"ℹ️ {messages[status]}")
    except Exception as exc:
        logger.error(f"admin_confirm failed: {exc}", exc_info=True)
        await call.answer("حدث خطأ غير متوقع، لم يُضف شيء. راجع السجلات.", show_alert=True)


@router.callback_query(F.data.startswith("pay:no:"))
async def admin_reject(call: types.CallbackQuery):
    if not ps.is_payment_admin(call.from_user.id):
        await call.answer("غير مصرّح لك.", show_alert=True)
        return
    try:
        order_id = int(call.data.split(":")[2])
        order = await ps.reject_order(order_id, f"tg:{call.from_user.id}")
        if not order:
            await call.answer("لا يمكن رفض هذا الطلب (مشحون/مرفوض/ملغى مسبقاً).", show_alert=True)
            return
        who = html.escape(call.from_user.full_name or str(call.from_user.id))
        await _finish_group_message(call, f"❌ <b>تم رفض الطلب #{order_id}</b> بواسطة {who}")
        await _strip_order_message_buttons(order, call.message)
        try:
            await call.bot.send_message(
                order["user_id"],
                f"⚠️ لم نتمكن من تأكيد دفعتك للطلب <code>{order['code']}</code>.\n"
                f"إن كنت قد حوّلت المبلغ فتواصل مع الدعم: {ADMIN_CONTACT}",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await call.answer("تم الرفض")
    except Exception as exc:
        logger.error(f"admin_reject failed: {exc}", exc_info=True)
        await call.answer("حدث خطأ غير متوقع.", show_alert=True)


@router.callback_query(F.data.startswith("pay:ig:"))
async def admin_ignore(call: types.CallbackQuery):
    if not ps.is_payment_admin(call.from_user.id):
        await call.answer("غير مصرّح لك.", show_alert=True)
        return
    try:
        await ps.dismiss_receipt(int(call.data.split(":")[2]))
        who = html.escape(call.from_user.full_name or str(call.from_user.id))
        await _finish_group_message(call, f"🚫 <b>تم تجاهل الحوالة</b> بواسطة {who}")
        await call.answer("تم التجاهل")
    except Exception as exc:
        logger.error(f"admin_ignore failed: {exc}", exc_info=True)
        await call.answer("حدث خطأ غير متوقع.", show_alert=True)


@router.callback_query(F.data == "pay:autooff")
async def admin_auto_off(call: types.CallbackQuery):
    """زر الطوارئ تحت رسالة «شحن تلقائي»: يوقف المفتاح الفرعي فوراً (يعود كل شيء للتأكيد اليدوي)."""
    if not ps.is_payment_admin(call.from_user.id):
        await call.answer("غير مصرّح لك.", show_alert=True)
        return
    try:
        if not await set_feature_flag("payments_auto_confirm", False):
            await call.answer("تعذّر حفظ الإيقاف، أوقفه من لوحة الأدمن.", show_alert=True)
            return
        who = html.escape(call.from_user.full_name or str(call.from_user.id))
        await _finish_group_message(call, f"🛑 <b>أُوقف الشحن التلقائي</b> بواسطة {who}")
        await call.answer("أُوقف الشحن التلقائي")
    except Exception as exc:
        logger.error(f"admin_auto_off failed: {exc}", exc_info=True)
        await call.answer("حدث خطأ غير متوقع.", show_alert=True)
