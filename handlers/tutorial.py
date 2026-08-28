# Handlers/tutorial.py
"""
==================== الدليل التفاعلي (Onboarding / How-To-Use Guide) ====================
هذا الملف يضيف طبقة إرشاد تفاعلية جديدة بالكامل (Progressive Disclosure) لحل أكبر نقطة
ألم لدى المستخدمين الجدد: "أضغط /start لكن لا أعرف كيف أتعامل مع البوت".

المبدأ: بدل حشو رسالة الترحيب بجدار نصي طويل، نعرض خطوات قصيرة قابلة للتصفح (Next/Prev)
مع شريط تقدم بصري، وفي النهاية دعوة واضحة لاتخاذ إجراء (CTA) تُشغّل الاستخدام الفعلي فوراً.

هذا الملف إضافي بالكامل: لا يحذف ولا يعدّل أي معالج (handler) أو ميزة موجودة مسبقاً.
"""
import asyncio
from aiogram import Router, types, F
from aiogram.filters import Command

from config import bot
from keyboards import get_main_menu_keyboard
from supabase_helper import log_usage_event
from logger import get_logger, log_error

logger = get_logger(__name__)
router = Router()
tutorial_router = router

# ==================== محتوى خطوات الدليل ====================
# كل خطوة: (العنوان، الوصف، إيموجي رمزي)
TUTORIAL_STEPS = [
    {
        "emoji": "📥",
        "title": "أرسل المحتوى التعليمي",
        "body": (
            "ابدأ بإرسال أي محتوى تريد تحويله لاختبار، بأي صيغة تناسبك:\n\n"
            "📄 ملف <b>PDF / Word / PowerPoint / TXT</b>\n"
            "🖼 صورة واحدة أو <b>ألبوم صور</b> متعددة\n"
            "📝 أو <b>نص مباشر</b> تكتبه أو تلصقه هنا"
        ),
    },
    {
        "emoji": "🔢",
        "title": "حدد عدد الأسئلة",
        "body": (
            "بعد استقبال المحتوى، سيسألك البوت:\n"
            "<i>«كم سؤالاً تريد توليده؟»</i>\n\n"
            "أرسل رقماً فقط، وسيظهر لك البوت <b>تكلفة النقاط بشفافية كاملة</b> "
            "قبل أي خصم فعلي من رصيدك، مع زر لتأكيد أو إلغاء الطلب في أي لحظة."
        ),
    },
    {
        "emoji": "🤖",
        "title": "التوليد التلقائي بالذكاء الاصطناعي",
        "body": (
            "بمجرد تأكيدك، يعمل البوت خلال ثوانٍ على قراءة المحتوى وتوليد أسئلة اختيار "
            "من متعدد عالية الجودة بالاعتماد الحصري على ما أرسلته.\n\n"
            "💡 <b>نصيحة:</b> إن وُجد كويز جاهز لنفس الملف مسبقاً، سيعرضه عليك البوت "
            "بخصم 90% لتوفير نقاطك!"
        ),
    },
    {
        "emoji": "🎯",
        "title": "حل الكويز خطوة بخطوة",
        "body": (
            "أجب على كل سؤال بالضغط على الخيار المناسب مباشرة من الأزرار.\n\n"
            "💡 عالق بسؤال؟ اضغط <b>«طلب تلميح ذكي»</b>\n"
            "💾 يمكنك حفظ الكويز أو مشاركته في أي وقت أثناء الحل\n"
            "🏁 وينتهي الاختبار بعرض نتيجتك النهائية فوراً"
        ),
    },
    {
        "emoji": "🏆",
        "title": "بعد الانتهاء: شارك وتنافس",
        "body": (
            "بعد ظهور نتيجتك، لديك عدة خيارات بضغطة زر:\n\n"
            "🔄 إعادة المحاولة\n"
            "🔗 مشاركة الكويز مع زملائك\n"
            "⭐ حفظه في مفضلتك المنظمة بأقسام\n"
            "📁 تصديره ملف Word أو PDF جاهز للطباعة\n"
            "🏅 نشر نتيجتك في لوحة الشرف والتنافس على القمة"
        ),
    },
]

TOTAL_STEPS = len(TUTORIAL_STEPS)


def _progress_bar(step_index: int) -> str:
    """شريط تقدم بصري بسيط، مثال: ●●○○○"""
    filled = "●" * (step_index + 1)
    empty = "○" * (TOTAL_STEPS - step_index - 1)
    return filled + empty


def _render_step(step_index: int) -> str:
    step = TUTORIAL_STEPS[step_index]
    header = f"{step['emoji']} <b>{step['title']}</b>"
    footer = f"\n\n{_progress_bar(step_index)}  ·  الخطوة {step_index + 1} من {TOTAL_STEPS}"
    return f"{header}\n\n{step['body']}{footer}"


def _tutorial_keyboard(step_index: int) -> types.InlineKeyboardMarkup:
    nav_row = []
    if step_index > 0:
        nav_row.append(types.InlineKeyboardButton(text="◀️ السابق", callback_data=f"tut_go_{step_index - 1}"))
    if step_index < TOTAL_STEPS - 1:
        nav_row.append(types.InlineKeyboardButton(text="التالي ▶️", callback_data=f"tut_go_{step_index + 1}"))

    rows = [nav_row] if nav_row else []

    if step_index == TOTAL_STEPS - 1:
        rows.append([types.InlineKeyboardButton(text="🚀 فهمت، جاهز أبدأ الآن!", callback_data="tut_finish")])
    else:
        rows.append([types.InlineKeyboardButton(text="⏭️ تخطّي وابدأ الآن مباشرة", callback_data="tut_finish")])

    rows.append([types.InlineKeyboardButton(text="✖️ إغلاق الدليل", callback_data="tut_close")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def get_tutorial_step_content(step_index: int):
    """يُستخدم خارجياً (مثلاً في رسالة ترحيب مستخدم جديد) لدمج نص وأزرار خطوة من الدليل
    داخل رسالة واحدة بدل إرسال رسالة منفصلة بعدها مباشرة."""
    return _render_step(step_index), _tutorial_keyboard(step_index)


async def send_tutorial(message: types.Message, step_index: int = 0) -> None:
    """يرسل رسالة جديدة تحتوي خطوة من الدليل (يُستخدم عند أول ظهور له في الشات)."""
    await message.answer(
        _render_step(step_index),
        reply_markup=_tutorial_keyboard(step_index),
        parse_mode="HTML",
    )


# ==================== المعالجات ====================

@router.message(Command("help"))
async def cmd_help(message: types.Message) -> None:
    """أمر /help: يفتح الدليل التفاعلي من أول خطوة، لأي مستخدم في أي وقت."""
    await send_tutorial(message, 0)
    asyncio.create_task(log_usage_event(message.from_user.id, "tutorial_opened", {"source": "command"}))


@router.callback_query(F.data == "how_to_use")
async def open_tutorial_from_menu(call: types.CallbackQuery) -> None:
    """زر «كيف يعمل البوت؟» من القائمة الرئيسية."""
    try:
        await call.message.answer(
            _render_step(0),
            reply_markup=_tutorial_keyboard(0),
            parse_mode="HTML",
        )
        asyncio.create_task(log_usage_event(call.from_user.id, "tutorial_opened", {"source": "menu_button"}))
    except Exception as exc:
        log_error(logger, f"open_tutorial_from_menu failed: {exc}", exception=exc)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("tut_go_"))
async def navigate_tutorial(call: types.CallbackQuery) -> None:
    """التنقل بين خطوات الدليل (السابق/التالي) عبر تعديل نفس الرسالة."""
    try:
        step_index = int(call.data.replace("tut_go_", "", 1))
        step_index = max(0, min(TOTAL_STEPS - 1, step_index))
        await call.message.edit_text(
            _render_step(step_index),
            reply_markup=_tutorial_keyboard(step_index),
            parse_mode="HTML",
        )
    except Exception as exc:
        log_error(logger, f"navigate_tutorial failed: {exc}", exception=exc)
    finally:
        await call.answer()


@router.callback_query(F.data == "tut_finish")
async def finish_tutorial(call: types.CallbackQuery) -> None:
    """إنهاء الدليل بدعوة واضحة لبدء الاستخدام الفعلي فوراً."""
    try:
        await call.message.edit_text(
            "✅ <b>جاهز تماماً!</b>\n\n"
            "📥 أرسل الآن أي ملف، صورة، أو نص مباشرة في هذه المحادثة لتبدأ أول اختبار لك فوراً 🚀\n\n"
            "👇 أو استخدم القائمة الرئيسية في أي وقت:",
            parse_mode="HTML",
        )
        # 🩹 UX: بدون هذا، يبقى المستخدم بلا أي أزرار وصول (شحن رصيد/مفضلة/دعم) إلى
        # أن يكتب /start يدوياً من جديد — نعيد له القائمة الرئيسية فور إغلاق الدليل.
        bot_info = await bot.get_me()
        await call.message.answer(
            "🏠 القائمة الرئيسية",
            reply_markup=await get_main_menu_keyboard(bot_info.username, call.from_user.id)
        )
        asyncio.create_task(log_usage_event(call.from_user.id, "tutorial_completed"))
    except Exception as exc:
        log_error(logger, f"finish_tutorial failed: {exc}", exception=exc)
    finally:
        await call.answer()


@router.callback_query(F.data == "tut_close")
async def close_tutorial(call: types.CallbackQuery) -> None:
    """إغلاق الدليل دون فرض أي خطوة إضافية على المستخدم، مع إبقاء القائمة الرئيسية متاحة."""
    try:
        bot_info = await bot.get_me()
        menu_kb = await get_main_menu_keyboard(bot_info.username, call.from_user.id)
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.message.answer("🏠 القائمة الرئيسية", reply_markup=menu_kb)
    except Exception as exc:
        log_error(logger, f"close_tutorial failed: {exc}", exception=exc)
    finally:
        await call.answer()
