"""Student-facing copy and lightweight UI helpers for private chat."""
from aiogram import types
from config import OFFICIAL_CHANNEL_URL, SUPPORT_BOT_URL

HOME_TEXT = (
    "أهلاً بك في <b>أبيض - منشئ الكويزات الذكي</b> ✨\n\n"
    "📤 أرسل ملفك (PDF أو Word أو PowerPoint) أو صورة واضحة أو نصاً (100 حرف على الأقل)؛ "
    "ليحوّله البوت فوراً إلى اختبار تفاعلي ذكي!"
)

UPLOAD_TEXT = (
    "📤 <b>أرسل مادتك الدراسية الآن!</b>\n\n"
    "• 📄 <b>ملف:</b> PDF أو Word (.docx) أو PowerPoint (.pptx)\n"
    "• 🖼 <b>صورة:</b> صورة واضحة لصفحة أو ملخص\n"
    "• ✍️ <b>نص:</b> الصق نص المحاضرة مباشرة (100 حرف فأكثر)\n"
    "• 🌐 <b>ملفك كبير؟</b> استخدم زر الرفع عبر الويب بالأسفل."
)

HELP_TEXT = (
    "❓ <b>كيف تستخدم بوت أبيض؟</b>\n\n"
    "1️⃣ <b>أرسل المحتوى:</b> ملف PDF/Word/PowerPoint أو صورة أو نص مباشر.\n"
    "2️⃣ <b>خصّص الكويز:</b> اختر عدد الأسئلة، مستوى الصعوبة، ونمط العرض (كويز تفاعلي، اختبار متسلسل، أو ملف جاهز للطباعة).\n"
    "3️⃣ <b>راجع واحفظ:</b> تدرّب على أخطائك واحفظ اختباراتك في <b>⭐ اختباراتي المحفوظة</b> للرجوع إليها في أي وقت."
)


def welcome_text(
    points: float,
    free_points: float = 0,
    gift_points: float = 0,
    paid_points: float = 0,
    renewed: bool = False,
    lang: str = "ar",
) -> str:
    if lang and lang != "ar":
        try:
            from services.i18n import get_ui_strings
            ui = get_ui_strings(lang)
            if ui:
                prefix = ui["welcome_renewed"] if renewed else ""
                return prefix + ui["welcome_body"].format(
                    points=float(points),
                    free_points=float(free_points),
                    gift_points=float(gift_points),
                    paid_points=float(paid_points),
                )
        except Exception:
            pass

    prefix = "🎉 تم تجديد رصيدك اليومي!\n\n" if renewed else ""
    return (
        f"{prefix}أهلاً بك في <b>أبيض - منشئ الكويزات الذكي</b> ✨\n\n"
        f"💰 <b>رصيدك: {points:.2f} نقطة</b>\n"
        f"🔄 مجانية: {free_points:.2f} · 🎁 هدايا: {gift_points:.2f} · 💳 مدفوعة: {paid_points:.2f}\n\n"
        "📤 <b>أرسل محتواك الدراسي الآن لإنشاء كويز:</b>\n"
        "• 📄 ملف (PDF أو Word أو PowerPoint)\n"
        "• 🖼 صورة واضحة لصفحة أو سلايد\n"
        "• ✍️ نص مباشر (100 حرف على الأقل)\n\n"
        "اختر من الأزرار بالأسفل لإدارة اختباراتك أو رصيدك أو إعداداتك:"
    )


def get_home_text(lang: str = "ar") -> str:
    if lang and lang != "ar":
        try:
            from services.i18n import get_ui_strings
            ui = get_ui_strings(lang)
            if ui:
                return ui["home_text"]
        except Exception:
            pass
    return HOME_TEXT


def get_upload_text(lang: str = "ar") -> str:
    if lang and lang != "ar":
        try:
            from services.i18n import get_ui_strings
            ui = get_ui_strings(lang)
            if ui:
                return ui["upload_text"]
        except Exception:
            pass
    return UPLOAD_TEXT


def get_help_text(lang: str = "ar") -> str:
    if lang and lang != "ar":
        try:
            from services.i18n import get_ui_strings
            ui = get_ui_strings(lang)
            if ui:
                return ui["help_text"]
        except Exception:
            pass
    return HELP_TEXT


def generation_wait_text(num_questions: int, source_kind: str = "file") -> str:
    return (
        f"⏳ جاري تحليل المحتوى وتوليد <b>{num_questions} سؤال</b>...\n"
        "قد يستغرق ذلك بضع ثوانٍ حسب حجم المادة."
    )


def low_points_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="💳 شحن الرصيد", callback_data="recharge_info")],
            [types.InlineKeyboardButton(text="💰 رصيدي وهداياي", callback_data="gift:wallet")],
            [types.InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="student_home")],
        ]
    )


def support_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="💬 تواصل مع الدعم الفني", url=SUPPORT_BOT_URL)],
            [types.InlineKeyboardButton(text="📢 قناة الأخبار", url=OFFICIAL_CHANNEL_URL)],
            [types.InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="student_home")],
        ]
    )


try:
    from services.i18n import install_ui_localization_hooks as _install_hooks
    _install_hooks()
except Exception:
    pass
