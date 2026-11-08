from typing import Optional
from aiogram import types
from constants import (
    OFFICIAL_CHANNEL_URL, SUPPORT_BOT_URL, BTN_CANCEL_REQUEST,
    BTN_TRANSLATE_YES, BTN_TRANSLATE_NO,
    BTN_TRANSLATE_YES_FRENCH, BTN_TRANSLATE_NO_FRENCH,
    BTN_QUIZ_EXTRACT_AS_IS, BTN_QUIZ_EXTRACT_AI_SOLVE,
    QUESTION_TYPE_OPTIONS, QUESTION_TYPE_GENERAL, QUESTION_TYPE_CUSTOM,
    QUESTION_TYPE_SIMILAR_MATH, BTN_SIMILAR_MATH,
    DIFFICULTY_EASY, DIFFICULTY_MEDIUM, DIFFICULTY_ADVANCED, DIFFICULTY_PROGRESSIVE, DIFFICULTY_LABELS_AR,
    BTN_QUESTION_TYPE_GENERAL, BTN_QUESTION_TYPE_CUSTOM, BTN_BACK_TO_TYPE_SCREEN,
    SUBJECT_MATH, SUBJECT_ENGLISH, SUBJECT_FRENCH,
    WEBAPP_PUBLIC_BASE_URL,
    BTN_AUDIO_CONFIRM_START,
    BTN_OPEN_UPLOAD_PAGE,
    BTN_OPEN_QUESTION_EDIT_PAGE,
    BTN_CLASSIFICATION_VOTE_YES, BTN_CLASSIFICATION_VOTE_NO,
    CLASSIFICATION_SUBJECT_CODES,
)
from helpers.settings_helper import get_setting
from logger import get_logger
from services.export_service import STYLE_CODE_TO_NAME, STYLE_LABELS_AR
from services.export_options import (
    ANSWERS_MODE_LABELS_AR, COLUMN_LABELS_AR, DRAFT_SIZE_LABELS_AR, DRAFT_STYLE_LABELS_AR, FIELD_CODES,
    MAX_PER_PAGE, PLACEMENT_LABELS_AR, PLACEMENTS, TEXT_FIELDS,
)
from services.quiz_permissions import can_delete_quiz
from utils import hash_to_short_token

logger = get_logger(__name__)


def get_delete_quiz_button_row(quiz_id: str, viewer_id=None, creator_id=None) -> list:
    """🆕 صف زر "🗑 حذف الكويز نهائياً" - يُستخدم من أي شاشة يظهر فيها كويز.
    يُرجع صفاً فارغاً [] (بدل الزر) إذا لم يكن viewer_id مصرَّحاً له (ليس الأدمن
    ولا مالك الكويز) - راجع services/quiz_permissions.py لمنطق الصلاحية الكامل.
    التحقق هنا يتحكم فقط بـ"ظهور" الزر؛ نفس الصلاحية تُعاد فحصها من جديد عند
    الضغط الفعلي (handlers/quiz_delete.py) لأن أي مستخدم يقدر تقنياً يرسل أي
    callback_data حتى لو لم يظهر له الزر أصلاً."""
    if not quiz_id or not can_delete_quiz(viewer_id, creator_id):
        return []
    return [[types.InlineKeyboardButton(text="🗑 حذف الكويز نهائياً", callback_data=f"qdel_{quiz_id}")]]

# ==================== لوحات التحكم والملاحة العامة ====================

async def get_main_menu_keyboard(bot_username: str, user_id: int) -> types.InlineKeyboardMarkup:
    try:
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        # 🆕 مكافأة الإحالة تُقرأ من app_settings كي يبقى نص الزر متطابقاً مع القيمة
        # الفعلية القابلة للتعديل من لوحة الإدارة، بدل رقم ثابت في الكود.
        referral_bonus_points = await get_setting("referral_bonus_points")
        kb = [
            [types.InlineKeyboardButton(text="❓ كيف أستخدم البوت؟", callback_data="student_help")],
            [types.InlineKeyboardButton(text="⭐ اختباراتي المحفوظة", callback_data="favorites_menu", style="primary")],
            [types.InlineKeyboardButton(text="💰 رصيدي وهداياي", callback_data="gift:wallet")],
            [types.InlineKeyboardButton(text="⚙️ الإعدادات", callback_data="student_settings")],
            [
                types.InlineKeyboardButton(text="📢 قناة الأخبار", url=OFFICIAL_CHANNEL_URL),
                types.InlineKeyboardButton(text="💬 الدعم الفني", url=SUPPORT_BOT_URL)
            ],
            [types.InlineKeyboardButton(text=f"🎁 دعوة صديق · {referral_bonus_points:g} نقاط", switch_inline_query=f"\nحوّل محتواك الدراسي إلى كويز عبر البوت: {ref_link}")]
        ]
        from helpers.supabase_helper.feature_flags import is_feature_enabled
        if await is_feature_enabled("challenge_system_enabled", default=False):
            kb.append([types.InlineKeyboardButton(text="🏕️ أرضي والتحديات", callback_data="cg:home:0")])
        # 🆕 زر رفع محاضرة صوتية كبيرة (حتى 250MB) عبر Mini App - يُخفى تلقائياً لو
        # WEBAPP_PUBLIC_BASE_URL فاضي (مثلاً بوضع polling محلي بدون WEBHOOK_URL)
        # حتى ما نعرض زر مكسور بيفتح رابط فاضي.
        # 🆕 زر واحد موحّد بدل 3 أزرار متفرقة - يفتح صفحة اختيار (Hub) فيها 3 بطاقات
        # (صوت/ملف/صور) الاختيار يصير داخل الصفحة نفسها بلمسة وحدة، بدل تشتيت قائمة
        # البوت بـ 3 أزرار WebApp منفصلة. يُخفى تلقائياً لو WEBAPP_PUBLIC_BASE_URL فاضي.
        if WEBAPP_PUBLIC_BASE_URL:
            kb.insert(2, [
                types.InlineKeyboardButton(
                    text="📚 ملفك أو محاضرتك حجمها كبير؟ اضغط هون",
                    callback_data="upload_open:all",
                )
            ])
        return types.InlineKeyboardMarkup(inline_keyboard=kb)
    except Exception as e:
        logger.error(f"Error generating main menu keyboard: {e}")
        return types.InlineKeyboardMarkup(inline_keyboard=[])


def get_web_upload_redirect_keyboard(upload_type: str = "document") -> types.InlineKeyboardMarkup:
    """
    🆕 لوحة برفقة رسالة MSG_REDIRECT_TO_WEB_UPLOAD - تُعرض لما يفشل استقبال ملف/صوت
    مباشرة عبر تيليجرام بسبب تجاوز حد Bot API (20MB)، كبديل فوري بدل رفض جاف بلا حل.
    upload_type: "audio"/"document"/"images" - فحص الجلسة أولاً ثم زر صفحة الرفع
    الموحّدة webapp/upload_hub.html؟type=... يتخطى شاشة الاختيار مباشرة (النوع معروف
    مسبقاً بهالحالة، فلا داعي يختار الطالب من جديد). تُرجع لوحة فاضية لو
    WEBAPP_PUBLIC_BASE_URL غير مُهيّأ (بدل زر مكسور برابط فاضي).
    """
    if not WEBAPP_PUBLIC_BASE_URL:
        return types.InlineKeyboardMarkup(inline_keyboard=[])
    return types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text=BTN_OPEN_UPLOAD_PAGE, callback_data=f"upload_open:{upload_type}"),
    ]])

def get_export_style_keyboard(favorite_id: str = "") -> types.InlineKeyboardMarkup:
    """لوحة اختيار شكل تنسيق الملف (بسيط / عصري / أكاديمي) - أول خطوة بمسار التصدير."""
    suffix = f"_{favorite_id}" if favorite_id else ""
    prefix = "fav_export_style_" if favorite_id else "export_style_"
    kb = [
        [types.InlineKeyboardButton(text=f"⚪ {STYLE_LABELS_AR[name]}", callback_data=f"{prefix}{code}{suffix}")]
        for code, name in STYLE_CODE_TO_NAME.items()
    ]
    kb.append([types.InlineKeyboardButton(text="🔙 تفاصيل الكويز" if favorite_id else "إغلاق خيارات التحميل",
        callback_data=f"fav_details_{favorite_id}" if favorite_id else "export_close")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_export_format_keyboard(style_code: str = "s", favorite_id: str = "") -> types.InlineKeyboardMarkup:
    """لوحة اختيار صيغة تحميل الكويز (Word أو PDF أو Excel)، مع الحفاظ على الستايل المختار،
    تدعم التصدير من الجلسة الحالية أو من كويز محفوظ بالمفضلة"""
    prefix = "fav_export_" if favorite_id else "export_"
    suffix = f"_{favorite_id}" if favorite_id else ""
    kb = []
    for variant, label in (("s", "نسخة الطالب"), ("t", "نسخة الأستاذ")):
        kb.append([
            types.InlineKeyboardButton(text=f"📕 PDF — {label}", callback_data=f"{prefix}pdf{variant}_{style_code}{suffix}"),
            types.InlineKeyboardButton(text=f"📄 Word — {label}", callback_data=f"{prefix}docx{variant}_{style_code}{suffix}"),
        ])
    kb.append([
        types.InlineKeyboardButton(text=f"📊 Excel — {label}", callback_data=f"{prefix}xlsx{variant}_{style_code}{suffix}")
        for variant, label in (("s", "نسخة الطالب"), ("t", "نسخة الأستاذ"))
    ])
    # إعدادات ورقة الاختبار الاختيارية (عدد الأسئلة بالصفحة، اسم الأستاذ، ...). قبل زر الرجوع.
    kb.append([types.InlineKeyboardButton(text="⚙️ إعدادات إضافية",
        callback_data=f"xo|open||{style_code}|{favorite_id}")])
    kb.append([types.InlineKeyboardButton(text="🔙 تغيير التنسيق",
        callback_data=f"fav_export_back_{favorite_id}" if favorite_id else "export_back")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def _xo(action: str, arg: str, style_code: str, favorite_id: str) -> str:
    """callback_data لإعدادات التصدير: xo|الإجراء|الوسيط|التنسيق|معرّف المفضلة (≤ 64 بايت)."""
    return f"xo|{action}|{arg}|{style_code}|{favorite_id}"


def get_export_settings_keyboard(options: dict, style_code: str = "s", favorite_id: str = "") -> types.InlineKeyboardMarkup:
    """شاشة «إعدادات إضافية». options = ExportOptions.to_dict() (الحقول غير الافتراضية فقط)."""
    def mark(name):
        return "✅ " if options.get(name) else ""
    per_page = options.get("per_page")
    kb = [[types.InlineKeyboardButton(
        text=f"🔢 عدد الأسئلة بالصفحة: {per_page if per_page else 'تلقائي'}",
        callback_data=_xo("pp", "", style_code, favorite_id))]]
    labels = {"exam_title": "📝 عنوان الاختبار", "subject": "📚 المادة", "grade": "🎓 الصف",
              "exam_date": "📅 التاريخ", "duration": "⏱ المدة", "total_marks": "💯 العلامة الكلية"}
    codes = {name: code for code, name in FIELD_CODES.items()}
    pairs = list(labels.items())
    for i in range(0, len(pairs), 2):
        kb.append([types.InlineKeyboardButton(text=f"{mark(name)}{label}",
                   callback_data=_xo("ed", codes[name], style_code, favorite_id)) for name, label in pairs[i:i + 2]])
    kb.append([types.InlineKeyboardButton(text=f"{mark('teacher_name')}👨‍🏫 اسم الأستاذ",
               callback_data=_xo("ed", "n", style_code, favorite_id))])
    if options.get("teacher_name"):
        place = PLACEMENT_LABELS_AR[options.get("teacher_placement", "first")]
        kb.append([types.InlineKeyboardButton(text=f"📍 مكان الاسم: {place}",
                   callback_data=_xo("pl", "", style_code, favorite_id))])
    kb.append([
        types.InlineKeyboardButton(
            text="🧾 بيانات الطالب: " + ("مخفية" if options.get("student_fields") is False else "ظاهرة"),
            callback_data=_xo("sf", "", style_code, favorite_id)),
        types.InlineKeyboardButton(
            text="🔑 " + ANSWERS_MODE_LABELS_AR[options.get("answers_mode", "full")],
            callback_data=_xo("am", "", style_code, favorite_id)),
    ])
    numbers = options.get("page_numbers")
    kb.append([types.InlineKeyboardButton(
        text="🔢 ترقيم الصفحات: " + ("افتراضي" if numbers is None else "ظاهر" if numbers else "مخفي"),
        callback_data=_xo("pn", "", style_code, favorite_id))])
    # تقسيم الصفحة (PDF وWord): عمود واحد أو عمودان بخط فاصل اختياري
    columns = options.get("columns", 1)
    row = [types.InlineKeyboardButton(text="📰 تقسيم الصفحة: " + COLUMN_LABELS_AR[columns],
                                      callback_data=_xo("cl", "", style_code, favorite_id))]
    if columns == 2:
        row.append(types.InlineKeyboardButton(
            text="➖ خط فاصل: " + ("ظاهر" if options.get("column_divider", True) else "مخفي"),
            callback_data=_xo("dv", "", style_code, favorite_id)))
    kb.append(row)
    # مساحة مسودة تحت كل سؤال (PDF وWord): الحجم ثم الشكل
    draft_size = options.get("draft_size", "none")
    row = [types.InlineKeyboardButton(text="✍️ مسودة تحت كل سؤال: " + DRAFT_SIZE_LABELS_AR[draft_size],
                                      callback_data=_xo("ds", "", style_code, favorite_id))]
    if draft_size != "none":
        row.append(types.InlineKeyboardButton(
            text="📐 الشكل: " + DRAFT_STYLE_LABELS_AR[options.get("draft_style", "auto")],
            callback_data=_xo("dt", "", style_code, favorite_id)))
    kb.append(row)
    kb.append([types.InlineKeyboardButton(text="♻️ إعادة الضبط", callback_data=_xo("rs", "", style_code, favorite_id)),
               types.InlineKeyboardButton(text="✅ تم — اختيار الصيغة",
                                          callback_data=_xo("done", "", style_code, favorite_id))])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_export_per_page_keyboard(max_allowed: int, current=None, style_code: str = "s",
                                 favorite_id: str = "") -> types.InlineKeyboardMarkup:
    """أزرار الأرقام الصالحة فقط (1..الحد الأعلى لهذا الكويز) + تلقائي + رجوع."""
    max_allowed = max(1, min(MAX_PER_PAGE, int(max_allowed)))
    numbers = [types.InlineKeyboardButton(text=("✅ " if current == n else "") + str(n),
               callback_data=_xo("ppv", str(n), style_code, favorite_id)) for n in range(1, max_allowed + 1)]
    kb = [numbers[i:i + 5] for i in range(0, len(numbers), 5)]
    kb.append([types.InlineKeyboardButton(text=("✅ " if not current else "") + "تلقائي (كل ما يتسع)",
               callback_data=_xo("ppv", "auto", style_code, favorite_id))])
    kb.append([types.InlineKeyboardButton(text="🔙 رجوع", callback_data=_xo("open", "", style_code, favorite_id))])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_export_placement_keyboard(current: str = "first", style_code: str = "s",
                                  favorite_id: str = "") -> types.InlineKeyboardMarkup:
    kb = [[types.InlineKeyboardButton(text=("✅ " if current == place else "") + PLACEMENT_LABELS_AR[place],
           callback_data=_xo("plv", place, style_code, favorite_id))] for place in PLACEMENTS]
    kb.append([types.InlineKeyboardButton(text="🔙 رجوع", callback_data=_xo("open", "", style_code, favorite_id))])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_export_input_keyboard(field_code: str, has_value: bool, style_code: str = "s",
                              favorite_id: str = "") -> types.InlineKeyboardMarkup:
    """تحت رسالة «أرسل القيمة»: مسح القيمة الحالية (إن وُجدت) أو إلغاء."""
    row = []
    if has_value:
        row.append(types.InlineKeyboardButton(text="🗑 مسح القيمة",
                   callback_data=_xo("clr", field_code, style_code, favorite_id)))
    row.append(types.InlineKeyboardButton(text="❌ إلغاء", callback_data=_xo("open", "", style_code, favorite_id)))
    return types.InlineKeyboardMarkup(inline_keyboard=[row])


def get_quiz_result_keyboard(quiz_id: str = None, viewer_id=None, creator_id=None) -> types.InlineKeyboardMarkup:
    # 🆕 إعادة تنظيم بصري: تجميع الإجراءات المترابطة بصف واحد (2 بجنب بعض)
    # بدل صف مستقل لكل زر - يقلل عدد الصفوف من 9 إلى 6 ويخلي الشاشة أخف.
    kb = [
        [
            types.InlineKeyboardButton(text="🔄 إعادة المحاولة", callback_data="quiz_replay"),
            types.InlineKeyboardButton(text="🔗 مشاركة الكويز", callback_data="quiz_share"),
        ],
        [
            types.InlineKeyboardButton(text="⭐ حفظ بالمفضلة", callback_data="quiz_favorite"),
            types.InlineKeyboardButton(text="📄 تحميل الكويز", callback_data="export_menu"),
        ],
    ]

    from services.runner_core import enabled as runner_enabled
    if runner_enabled():
        kb.append([types.InlineKeyboardButton(text="🏃 العب رحلة أبيض", callback_data="runner:current")])

    if quiz_id:
        # 🆕 ما في ولا زر متعلق بالنشر/الإخفاء هون - القرار كله صار تحت لوحة
        # الشرف نفسها (راجع get_leaderboard_keyboard)، هون بس زر الدخول إلها.
        kb.append([types.InlineKeyboardButton(text="🏆 عرض لوحة الشرف", callback_data=f"leaderboard_{quiz_id}")])

    # 🆕 زر "حذف الكويز نهائياً" - يظهر فقط للأدمن أو لمالك الكويز الفعلي.
    kb.extend(get_delete_quiz_button_row(quiz_id, viewer_id, creator_id))

    kb.append([types.InlineKeyboardButton(text="📝 كويز جديد", callback_data="student_create_quiz"),
               types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="student_home")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_leaderboard_keyboard(quiz_id: str, is_public: bool = None) -> types.InlineKeyboardMarkup:
    """🆕 زر إدارة رؤية النتيجة تحت لوحة الشرف نفسها (بدل شاشة نتيجة الكويز) -
    ما بيظهر إلا لطالب أخد هالكويز فعلياً وعنده قرار مسجّل (is_public ليس None)."""
    if is_public is None:
        return None
    toggle_btn = (
        types.InlineKeyboardButton(text="🙈 إخفاء نتيجتي من لوحة الشرف", callback_data=f"hide_score_{quiz_id}_lb")
        if is_public else
        types.InlineKeyboardButton(text="📢 انشر نتيجتي في لوحة الشرف", callback_data=f"publish_score_{quiz_id}_lb")
    )
    return types.InlineKeyboardMarkup(inline_keyboard=[[toggle_btn]])

def get_quiz_start_keyboard() -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="🚀 ابدأ الاختبار الآن", callback_data="start_first_question")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_quiz_exit_confirmation_keyboard() -> types.InlineKeyboardMarkup:
    """لوحة حارسة تؤكد رغبة الطالب في إنهاء الكويز وعرض النتيجة"""
    kb = [
        [
            types.InlineKeyboardButton(text="🏁 نعم، إنهاء وعرض النتيجة", callback_data="quiz_stop_confirmed"),
            types.InlineKeyboardButton(text="🔄 لا، إكمال الحل", callback_data="quiz_resume_flow")
        ]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

# 🆕 زر تراجع موحّد لكل شاشات تعديل سؤال أثناء حل الكويز - يعيد المستخدم لحالة
# answering_quiz ويتابع الاختبار بشكل طبيعي بدل تجمّد الحالة لو رد بالنقطة بالخطأ
# أو غيّر رأيه ولا يريد التعديل فعلياً (راجع cancel_question_edit في quiz_runner.py).
BTN_CANCEL_QUESTION_EDIT = "🔙 تراجع ومتابعة الاختبار"

def get_cancel_edit_keyboard() -> types.InlineKeyboardMarkup:
    """كيبورد تراجع مستقل - يُستخدم برسائل انتظار النص الحر (نص سؤال/إجابة معدّلة)
    التي لم يكن لها أي زر سابقاً."""
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=BTN_CANCEL_QUESTION_EDIT, callback_data="cancel_question_edit")],
    ])

def get_question_edit_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✏️ تعديل نص السؤال", callback_data="edit_question_text")],
        [types.InlineKeyboardButton(text="📝 تعديل أحد الإجابات", callback_data="edit_question_answer")],
        [types.InlineKeyboardButton(text=BTN_CANCEL_QUESTION_EDIT, callback_data="cancel_question_edit")],
    ])

def get_answer_edit_keyboard(options: list) -> types.InlineKeyboardMarkup:
    rows = [
        [types.InlineKeyboardButton(text=f"{index + 1}. {str(option)[:45]}", callback_data=f"edit_answer_{index}")]
        for index, option in enumerate(options)
    ]
    rows.append([types.InlineKeyboardButton(text=BTN_CANCEL_QUESTION_EDIT, callback_data="cancel_question_edit")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)

def get_math_question_edit_keyboard(url: str) -> types.InlineKeyboardMarkup:
    """🆕 زر WebApp يفتح محرر أسئلة الرياضيات الكامل (webapp/question_edit.html) -
    نص + إجابات + جدول + مصفوفات بمعاينة LaTeX حية، بدل التعديل النصي المجزّأ.
    + زر تراجع بجانبه لمن رد بالنقطة بالخطأ ولا يريد فتح المحرر أصلاً."""
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=BTN_OPEN_QUESTION_EDIT_PAGE, web_app=types.WebAppInfo(url=url))],
        [types.InlineKeyboardButton(text=BTN_CANCEL_QUESTION_EDIT, callback_data="cancel_question_edit")],
    ])

def get_question_count_quick_keyboard(suggestions: list = None) -> types.InlineKeyboardMarkup:
    """نسخة احتياطية بسيطة (بدون أسعار) - تُستخدم فقط كـ reply_markup لرسائل الخطأ
    (مثال: 'أدخل رقماً صحيحاً') حيث لا داعي لإعادة حساب التكلفة الكاملة."""
    suggestions = suggestions or [5, 10, 15, 20]
    row = [types.InlineKeyboardButton(text=f"{n}", callback_data=f"qcount_pick_{n}") for n in suggestions]
    kb = [row, [types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")]]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_question_count_keyboard(
    items: int, is_album: bool, selected_count: int, suggestions: list = None
) -> types.InlineKeyboardMarkup:
    """
    🆕 شاشة عدد الأسئلة المدمجة - خطوة واحدة بدل خطوتين منفصلتين (اختيار عدد ← ثم شاشة
    تأكيد وتكلفة مستقلة). السعر لا يظهر على الأزرار (تفادي الازدحام البصري) - مصدر
    وحيد للسعر هو سطر "تكلفة العملية" بنص الرسالة فوق الكيبورد (build_transparency_text
    بـ handlers/files.py._render_question_count_screen)، ويتحدّث تلقائياً مع كل تغيير
    بالعدد المختار لأن الرسالة كلها تُعاد بناؤها بكل تعديل (edit_message_text).
    """
    kb = []
    row = []
    for n in (suggestions or [5, 10, 15, 20]):
        label = f"{n} سؤال"
        text = f"✅ {label}" if selected_count == n else label
        row.append(types.InlineKeyboardButton(text=text, callback_data=f"qcount_pick_{n}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)

    kb.append([types.InlineKeyboardButton(text="✏️ عدد مخصص (اكتبه مباشرة)", callback_data="qcount_custom")])
    kb.append([types.InlineKeyboardButton(text=f"🚀 توليد {selected_count} أسئلة", callback_data="qcount_start")])
    kb.append([types.InlineKeyboardButton(text="🔙 تعديل الصعوبة", callback_data="qback_to_difficulty")])
    kb.append([types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_quiz_detected_count_keyboard(selected_count: int) -> types.InlineKeyboardMarkup:
    """Review the detected or corrected count; require a count before extraction."""
    kb = [
        [types.InlineKeyboardButton(text="✏️ تعديل عدد الأسئلة", callback_data="qcount_custom")],
        [types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")],
    ]
    if selected_count > 0:
        kb.insert(0, [types.InlineKeyboardButton(
            text=f"🚀 ابدأ استخراج {selected_count} سؤال", callback_data="qcount_start",
        )])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_quiz_type_keyboard(subject_type: str, suggested_types: list, selected_type: str) -> types.InlineKeyboardMarkup:
    """
    🆕 المرحلة الأولى من شاشة الخيارات (نوع الأسئلة فقط - بدون صعوبة بنفس الشاشة لتقليل
    الازدحام البصري). اختيار أي نوع (أو الشمول) ينقل تلقائياً لمرحلة الصعوبة - لا حاجة
    لزر "متابعة" منفصل.
    """
    kb = []

    if suggested_types:
        type_options = [(f"other_{i}", label) for i, label in enumerate(suggested_types[:4])]
        if subject_type == SUBJECT_MATH:
            type_options.append((QUESTION_TYPE_SIMILAR_MATH, BTN_SIMILAR_MATH))
    elif subject_type in (SUBJECT_MATH, SUBJECT_ENGLISH, SUBJECT_FRENCH):
        type_options = list(QUESTION_TYPE_OPTIONS.get(subject_type, []))
    else:
        # 🆕 اقتراحات AI الديناميكية للمواد غير المصنّفة
        type_options = [(f"other_{i}", label) for i, label in enumerate(suggested_types[:4])]

    row = []
    for value, label in type_options:
        text = f"✅ {label}" if selected_type == value else label
        row.append(types.InlineKeyboardButton(text=text, callback_data=f"qtype_{value}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)

    general_text = f"✅ {BTN_QUESTION_TYPE_GENERAL}" if selected_type == QUESTION_TYPE_GENERAL else BTN_QUESTION_TYPE_GENERAL
    custom_text = f"✅ {BTN_QUESTION_TYPE_CUSTOM}" if selected_type == QUESTION_TYPE_CUSTOM else BTN_QUESTION_TYPE_CUSTOM
    # 🆕 زر "عام" العمومي يُخفى لمادتي الإنجليزي والفرنسي تحديداً لأن "🎯 اختبار عام"
    # (general_test) من القائمة الجاهزة أعلاه يؤدي نفس الغرض بالضبط - عرض الاثنين معاً
    # كان يبدو كخيارين متطابقين مربكين بصرياً. باقي المواد (رياضيات/أخرى) لا تملك خياراً
    # مكافئاً بنفس المعنى ضمن قوائمها، فيبقى الزر العمومي ضرورياً لها.
    if suggested_types or subject_type not in (SUBJECT_ENGLISH, SUBJECT_FRENCH):
        kb.append([types.InlineKeyboardButton(text=general_text, callback_data="qtype_general")])
    kb.append([types.InlineKeyboardButton(text=custom_text, callback_data="qtype_custom")])
    kb.append([types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_quiz_difficulty_keyboard(selected_difficulty: str) -> types.InlineKeyboardMarkup:
    """
    🆕 المرحلة الثانية والأخيرة (الصعوبة فقط). اختيار أي مستوى يُكمل التدفق مباشرة
    لشاشة عدد الأسئلة - لا حاجة لزر "متابعة" منفصل هنا أيضاً.
    """
    diff_row = []
    for value in (DIFFICULTY_EASY, DIFFICULTY_MEDIUM, DIFFICULTY_ADVANCED):
        label = DIFFICULTY_LABELS_AR[value]
        text = f"✅ {label}" if selected_difficulty == value else label
        diff_row.append(types.InlineKeyboardButton(text=text, callback_data=f"qdiff_{value}"))
    # 🆕 "متدرج" بصف خاص لوحده (بدل ازدحامه مع الأزرار الثلاثة الأخرى بصف واحد) لأن
    # نصه أطول (يشمل توضيح "سهل ← صعب") فقد يبدو مضغوطاً بجانب الثلاثة الباقين.
    progressive_label = DIFFICULTY_LABELS_AR[DIFFICULTY_PROGRESSIVE]
    progressive_text = (
        f"✅ {progressive_label}" if selected_difficulty == DIFFICULTY_PROGRESSIVE else progressive_label
    )
    kb = [
        diff_row,
        [types.InlineKeyboardButton(text=progressive_text, callback_data=f"qdiff_{DIFFICULTY_PROGRESSIVE}")],
        [types.InlineKeyboardButton(text=BTN_BACK_TO_TYPE_SCREEN, callback_data="qback_to_type")],
        [types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_translation_choice_keyboard(subject_type: str = SUBJECT_ENGLISH) -> types.InlineKeyboardMarkup:
    """🆕 لوحة اختيار نمط الأسئلة عند اكتشاف محتوى إنجليزي أو فرنسي: مترجمة للعربية أو
    باللغة الأصلية فقط. subject_type يحدد نص الأزرار (إنجليزي/فرنسي) - السلوك والـ
    callback_data نفسهما لكلتا المادتين (راجع handlers/files.py)."""
    if subject_type == SUBJECT_FRENCH:
        yes_text, no_text = BTN_TRANSLATE_YES_FRENCH, BTN_TRANSLATE_NO_FRENCH
    else:
        yes_text, no_text = BTN_TRANSLATE_YES, BTN_TRANSLATE_NO
    kb = [
        [types.InlineKeyboardButton(text=yes_text, callback_data="translate_choice_yes")],
        [types.InlineKeyboardButton(text=no_text, callback_data="translate_choice_no")],
        [types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

# 🆕 لوحة "هل التصنيف صحيح؟" (التحقق المجتمعي - راجع services/subject_classifier.py +
# migration_classification_votes.sql). subject يُشفَّر بكود قصير وfile_hash يُضغط عبر
# utils.hash_to_short_token لأن callback_data محدود بـ 64 بايت من تيليجرام والهاش
# الأصلي (SHA-256 hex) وحده 64 حرفاً. الطالب لا يحتاج معرفة أي من هذا - قيمتا subject/
# file_hash تُستخرجان لاحقاً عبر handlers/files.py._decode_classification_vote_callback.
def get_classification_vote_keyboard(file_hash: str, subject: str) -> types.InlineKeyboardMarkup:
    token = hash_to_short_token(file_hash)
    subj_code = CLASSIFICATION_SUBJECT_CODES.get(subject, "o")
    kb = [[
        types.InlineKeyboardButton(text=BTN_CLASSIFICATION_VOTE_YES, callback_data=f"cv_y_{subj_code}_{token}"),
        types.InlineKeyboardButton(text=BTN_CLASSIFICATION_VOTE_NO, callback_data=f"cv_n_{subj_code}_{token}"),
    ]]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


# 🆕 لوحة اختيار طريقة استخراج "اختبار محلول" (subject=quiz_solved): اعتماد الأجوبة
# المدوّنة بالمستند كما هي، أو تجاهلها وحلّ الاختبار بالذكاء الاصطناعي بدلاً منها.
# لا تُعرض إطلاقاً لـ quiz_unsolved (لا يوجد أجوبة أصلاً لاعتمادها - راجع handlers/files.py).
def get_quiz_extraction_choice_keyboard(is_math: bool = False, solved: bool = True) -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text=BTN_QUIZ_EXTRACT_AS_IS, callback_data="quiz_extract_as_is")],
        [types.InlineKeyboardButton(text=BTN_QUIZ_EXTRACT_AI_SOLVE, callback_data="quiz_extract_ai_solve")],
        [types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")],
    ]
    kb.insert(-1, [types.InlineKeyboardButton(text="✨ توليد أسئلة جديدة من المحتوى", callback_data="quiz_generate_new")])
    if not solved:
        kb = [row for row in kb if row[0].callback_data != "quiz_extract_as_is"]
    if is_math:
        from constants import BTN_SIMILAR_MATH
        kb.insert(-1, [types.InlineKeyboardButton(text=BTN_SIMILAR_MATH, callback_data="quiz_generate_similar_math")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_cancel_upload_keyboard() -> types.InlineKeyboardMarkup:
    """زر التراجع النظيف لإلغاء طلبات معالجة الملفات أو النصوص المباشرة المعلقة"""
    kb = [
        [types.InlineKeyboardButton(text="❌ إلغاء الطلب", callback_data="cancel_upload_request")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

# ==================== 🆕 لوحات معالجة المحاضرات الصوتية ====================

def get_audio_confirm_keyboard() -> types.InlineKeyboardMarkup:
    """🆕 لوحة تأكيد ما قبل التفريغ: تُعرض مرة واحدة بعد معرفة مدة الملف الفعلية،
    وتحتوي إقرار الحقوق + المدة/التكلفة ضمن نص الرسالة (MSG_AUDIO_CONFIRM_TEMPLATE)
    فوق هذه الأزرار مباشرة. لا خصم نقاط قبل ضغط الطالب على زر التأكيد صراحةً."""
    kb = [
        [types.InlineKeyboardButton(text=BTN_AUDIO_CONFIRM_START, callback_data="audio_confirm_start")],
        [types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_audio_action_keyboard() -> types.InlineKeyboardMarkup:
    """🆕 لوحة الإجراءات المتاحة بعد تفريغ محاضرة صوتية (voice/audio) بنجاح إلى نص -
    تُعرض مباشرة عند دخول القسم QuizState.waiting_for_audio_action (راجع handlers/audio.py)."""
    kb = [
        [types.InlineKeyboardButton(text="📄 تحميل Word / PDF", callback_data="audio_act_export")],
        [types.InlineKeyboardButton(text="✨ تلخيص وصياغة أكاديمية", callback_data="audio_act_summarize")],
        [types.InlineKeyboardButton(text="🎯 إنشاء كويز من المحاضرة", callback_data="audio_act_quiz")],
        [types.InlineKeyboardButton(text="📋 إرسال النص المفرغ", callback_data="audio_act_send_text")],
        [types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_document_export_keyboard() -> types.InlineKeyboardMarkup:
    """🆕 لوحة اختيار صيغة الملف (Word أو PDF) عند تصدير النص المفرغ/الملخص الأكاديمي
    للمحاضرة الصوتية - أبسط من get_export_format_keyboard (بدون اختيار ستايل تنسيق
    مسبق، لأن المحتوى هنا نص متصل وليس كويز أسئلة)."""
    kb = [
        [
            types.InlineKeyboardButton(text="📄 Word (docx)", callback_data="audio_export_docx"),
            types.InlineKeyboardButton(text="📕 PDF", callback_data="audio_export_pdf"),
        ]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

# ==================== لوحات إدارة المخازن المتعددة والتقييمات ====================

async def get_multiple_quizzes_keyboard(
    all_quizzes: list, filtered_quizzes: list, items: int, is_album: bool, show_generate_btn: bool = True,
    filter_type: str = "all", filter_difficulty: str = "all", viewer_id=None,
) -> types.InlineKeyboardMarkup:
    """
    توليد أزرار ذكية لعرض الكويزات المتوفرة للملف الواحد مع إحصائيات تقييم مجتمع الطلاب.

    🆕 all_quizzes: كل الكويزات المخزّنة (تُستخدم فقط لاستخراج القيم المتاحة للفلترة).
    🆕 filtered_quizzes: الكويزات بعد تطبيق الفلتر الحالي (هي فقط اللي تُعرض كأزرار تشغيل).
    🆕 صفوف الفلترة (نوع/صعوبة) تُعرض فقط لو فيه أكثر من قيمة واحدة فعلياً موجودة بين
    الكويزات المخزّنة - لتفادي ازدحام الأزرار لما تكون كلها بنفس التركيبة.

    🆕 viewer_id: معرّف الطالب الحالي - يُستخدم لإظهار زر "🗑 حذف نهائياً" بجانب كل
    كويز مخزّن فقط للأدمن أو لمالك ذلك الكويز تحديداً (creator_id لكل كويز على حدة).

    🩹 items/is_album: يُحسب سعر كل كويز مخزّن **حسب عدد أسئلته الفعلي الخاص فيه** (بدل
    سعر واحد ثابت مأخوذ من أول كويز بالقائمة وتطبيقه على الجميع - كان يسبب حجب طلاب
    برصيد كافٍ لكويز أرخص فعلياً بالقائمة، لمجرد إن كويزاً آخر أغلى صادف إنه الأول).
    """
    from helpers.points_calculator import calculate_cached_points_cost  # تفادي استيراد دائري

    kb = []

    # 🆕 صف فلترة النوع - أكثر 3 أنواع تكراراً بين الكويزات المخزّنة (تفادي ازدحام الصف)
    seen_types, distinct_types = set(), []
    for q in all_quizzes:
        qt = q.get("question_type") or "general"
        if qt not in seen_types:
            seen_types.add(qt)
            distinct_types.append((qt, q.get("question_type_label") or "📚 أسئلة شاملة عن الدرس"))
    if len(distinct_types) > 1:
        row = [types.InlineKeyboardButton(
            text=("✅ الكل" if filter_type == "all" else "الكل"), callback_data="cachefilter_type_all"
        )]
        for qt, label in distinct_types[:3]:
            short_label = label if len(label) <= 18 else (label[:16] + "…")
            text = f"✅ {short_label}" if filter_type == qt else short_label
            row.append(types.InlineKeyboardButton(text=text, callback_data=f"cachefilter_type_{qt}"))
        kb.append(row)

    # 🆕 صف فلترة الصعوبة
    distinct_difficulties = sorted({q.get("difficulty") or "medium" for q in all_quizzes})
    if len(distinct_difficulties) > 1:
        row = [types.InlineKeyboardButton(
            text=("✅ الكل" if filter_difficulty == "all" else "الكل"), callback_data="cachefilter_diff_all"
        )]
        for d in distinct_difficulties:
            label = DIFFICULTY_LABELS_AR.get(d, d)
            text = f"✅ {label}" if filter_difficulty == d else label
            row.append(types.InlineKeyboardButton(text=text, callback_data=f"cachefilter_diff_{d}"))
        kb.append(row)

    if not filtered_quizzes:
        kb.append([types.InlineKeyboardButton(text="🔍 لا يوجد كويزات مطابقة لهذا الفلتر", callback_data="ignored")])

    for idx, q in enumerate(filtered_quizzes, 1):
        likes = q.get('likes', 0)
        dislikes = q.get('dislikes', 0)
        # 🩹 UX: إضافة عدد الأسئلة لكل كويز جاهز، فالطالب كان يختار بين "كويز 1" و"كويز 2"
        # دون معرفة عدد أسئلة أي منهما قبل الدفع.
        quiz_data = q.get('quiz_data') or []
        q_count = len(quiz_data)
        # 🆕 تمييز الكويزات المصوّرة (LaTeX) بأيقونة مختلفة لأن تجربتها مختلفة
        # (صورة لكل سؤال + Poll بحروف الإجابة فقط) عن الكويز النصي العادي
        is_math = bool(q.get('is_math_quiz')) or (q_count > 0 and bool(quiz_data[0].get('is_math')))
        icon = "📐" if is_math else "📝"
        # 🆕 تفاصيل النوع + الصعوبة بجانب كل كويز مخزّن (بدل عرضها كوحدة متجانسة كسابقاً)
        type_label = q.get("question_type_label") or "📚 أسئلة شاملة عن الدرس"
        diff_label = DIFFICULTY_LABELS_AR.get(q.get("difficulty") or "medium", "🟡 متوسط")
        # 🩹 سعر هذا الكويز تحديداً (وليس سعراً عاماً موحّداً) بناءً على عدد أسئلته الفعلي
        # 🆕 calculate_cached_points_cost صارت async (تقرأ معدّلي التسعير من إعدادات
        # الأدمن الديناميكية - راجع helpers/points_calculator.py وsettings_helper.py)،
        # فصارت هذه الدالة نفسها async تبعاً لذلك (مستدعيها الوحيد _render_cache_decision_screen
        # بـ handlers/files.py أصلاً async وتُستدعى الآن بـ await).
        quiz_cost = await calculate_cached_points_cost(items, q_count, is_album)
        btn_text = f"{icon} {type_label} | {diff_label} | {q_count} سؤال ({quiz_cost:.2f}💎) | 👍{likes} 👎{dislikes}"
        kb.append([types.InlineKeyboardButton(text=btn_text, callback_data=f"use_multi_{q['id']}")])
        # 🆕 زر حذف مستقل تحت كل كويز على حدة (بيظهر بس للأدمن أو لمالك هذا الكويز
        # تحديداً - creator_id يوصل الآن ضمن كل صف من get_file_quizzes).
        kb.extend(get_delete_quiz_button_row(q['id'], viewer_id, q.get('creator_id')))
    
    if show_generate_btn:
        kb.append([types.InlineKeyboardButton(text="🆕 توليد كويز جديد كلياً (تكلفة كاملة)", callback_data="cache_action_no")])
    else:
        kb.append([types.InlineKeyboardButton(text="🔒 تم استنفاد الحد الأقصى لتنوع هذا الملف", callback_data="ignored")])
        
    kb.append([types.InlineKeyboardButton(text="❌ إلغاء الطلب والتراجع", callback_data="cancel_upload_request")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_rating_keyboard(file_quiz_id: str, quiz_id: str = None, viewer_id=None, creator_id=None) -> types.InlineKeyboardMarkup:
    """لوحة نتيجة الاختبار الكاملة + صف تقييم الكويز المركزي مضاف فوقها.
    🆕 صف التقييم مدمج (لايك/دِسلايك جنب بعض) وزر الملاحظة تحته مباشرة،
    فتصير 6 صفوف إجمالاً بدل 9 - أخف بصرياً وأسهل مسحاً بالعين."""
    base_kb = get_quiz_result_keyboard(quiz_id=quiz_id if quiz_id is not None else file_quiz_id, viewer_id=viewer_id, creator_id=creator_id)
    kb = list(base_kb.inline_keyboard)
    kb.insert(-1, [types.InlineKeyboardButton(
        text="💬 عندك ملاحظات؟", callback_data=f"quiz_feedback_{file_quiz_id}"
    )])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

# ==================== لوحات إدارة المفضلة والأقسام ====================

def get_favorites_list_keyboard(favorites: list, current_page: int = 1, page_size: int = 5, sort_mode: str = "latest", search_query: str = "", section_filter: str = "") -> types.InlineKeyboardMarkup:
    """Keep saved tests primary; filters and navigation follow the results."""
    page_size = max(1, page_size)
    pages = max(1, (len(favorites) + page_size - 1) // page_size)
    current_page = max(1, min(current_page, pages))
    start = (current_page - 1) * page_size
    kb = []
    for number, item in enumerate(favorites[start:start + page_size], start + 1):
        title = " ".join((item.get("title") or item.get("source_title") or "اختبار محفوظ").split())
        title = title if len(title) <= 36 else title[:35] + "…"
        fid = item.get("favorite_id") or item.get("id")
        count = len(item.get("quiz_data") or [])
        kb.append([types.InlineKeyboardButton(
            text=f"{number}. {title} · {count} سؤال", callback_data=f"fav_details_{fid}", style="primary")])
    if not favorites and not search_query and not section_filter:
        kb.append([types.InlineKeyboardButton(text="📝 إنشاء أول كويز", callback_data="student_create_quiz", style="success")])
    navigation = []
    if current_page > 1:
        navigation.append(types.InlineKeyboardButton(text="⬅️ السابق", callback_data=f"fav_page_{current_page - 1}"))
    if current_page < pages:
        navigation.append(types.InlineKeyboardButton(text="التالي ➡️", callback_data=f"fav_page_{current_page + 1}"))
    if navigation:
        kb.append(navigation)
    kb.append([
        types.InlineKeyboardButton(text="🔎 بحث عن اختبار", callback_data="favorites_search"),
        types.InlineKeyboardButton(text="📁 الأقسام", callback_data="sections_menu"),
    ])
    if search_query:
        kb.append([types.InlineKeyboardButton(text="✕ مسح البحث", callback_data="favorites_clear_search")])
    if section_filter:
        kb.append([types.InlineKeyboardButton(text="📚 كل اختباراتي", callback_data="favorites_all")])
    elif favorites:
        kb.append([types.InlineKeyboardButton(
            text="↕️ الترتيب: الأحدث أولاً" if sort_mode == "latest" else "↕️ الترتيب: حسب القسم",
            callback_data="favorites_sort_section" if sort_mode == "latest" else "favorites_sort_latest")])
    kb.append([types.InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="student_home")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_favorite_details_keyboard(favorite_id: str, section_id: str = None, quiz_id=None, viewer_id=None, creator_id=None) -> types.InlineKeyboardMarkup:
    from services.runner_core import enabled as runner_enabled
    game_rows = [[types.InlineKeyboardButton(text="🏃 العب رحلة أبيض", callback_data=f"runner:{quiz_id}")]] if quiz_id and runner_enabled() else []
    return types.InlineKeyboardMarkup(inline_keyboard=game_rows + [
        [types.InlineKeyboardButton(text="▶️ ابدأ الاختبار", callback_data=f"fav_open_{favorite_id}", style="success")],
        [types.InlineKeyboardButton(text="📄 تحميل PDF أو Word", callback_data=f"fav_export_menu_{favorite_id}", style="primary")],
        [types.InlineKeyboardButton(text="⚙️ إدارة الاختبار", callback_data=f"fav_manage_{favorite_id}")],
        [types.InlineKeyboardButton(text="🔙 رجوع لاختباراتي", callback_data="favorites_resume"),
         types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="student_home")],
    ])


def get_sections_list_keyboard(sections: list, quiz_counts: dict = None, current_page: int = 1) -> types.InlineKeyboardMarkup:
    quiz_counts = quiz_counts or {}
    page_size = 6
    pages = max(1, (len(sections) + page_size - 1) // page_size)
    current_page = max(1, min(current_page, pages))
    start = (current_page - 1) * page_size
    kb = []
    for section in sections[start:start + page_size]:
        sid = str(section.get("section_id"))
        title = " ".join((section.get("title") or "عام").split())
        title = title if len(title) <= 35 else title[:34] + "…"
        kb.append([types.InlineKeyboardButton(
            text=f"📁 {title} · {quiz_counts.get(sid, 0)} اختبار",
            callback_data=f"fav_sec_view_{sid}", style="primary")])
    navigation = []
    if current_page > 1:
        navigation.append(types.InlineKeyboardButton(text="⬅️ السابق", callback_data=f"fav_sections_{current_page - 1}"))
    if current_page < pages:
        navigation.append(types.InlineKeyboardButton(text="التالي ➡️", callback_data=f"fav_sections_{current_page + 1}"))
    if navigation:
        kb.append(navigation)
    kb.append([types.InlineKeyboardButton(text="📚 كل اختباراتي", callback_data="favorites_all")])
    kb.append([types.InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="student_home")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_cache_choice_keyboard(points_cost: int) -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text=f"🎁 كويز جاهز بـ {points_cost} نقطة (خصم 90%)", callback_data="cache_accept")],
        [types.InlineKeyboardButton(text="🆕 توليد كويز جديد (تكلفة كاملة)", callback_data="cache_reject")],
        [types.InlineKeyboardButton(text="❌ إلغاء الطلب وتراجع", callback_data="cancel_upload_request")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

# ==================== واجهات إدارة الكويز النشط ====================

def get_quiz_question_keyboard(options: list, show_hint: bool = True) -> types.InlineKeyboardMarkup:
    kb = []
    for index, option in enumerate(options):
        kb.append([types.InlineKeyboardButton(text=option, callback_data=f"ans_{index}")])
    
    if show_hint:
        kb.append([types.InlineKeyboardButton(text="💡 طلب تلميح ذكي", callback_data="get_hint")])
    
    control_buttons = [
        types.InlineKeyboardButton(text="🏁 إنهاء", callback_data="quiz_stop"),
        types.InlineKeyboardButton(text="🔗 مشاركة", callback_data="quiz_share"),
        types.InlineKeyboardButton(text="💾 حفظ", callback_data="save_quiz")
    ]
    kb.append(control_buttons)
    kb.append([types.InlineKeyboardButton(text="➡️ السؤال التالي", callback_data="next_question")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_quiz_answered_keyboard(options: list, correct_opt: int, selected_opt: int) -> types.InlineKeyboardMarkup:
    kb = []
    for i, opt in enumerate(options):
        prefix = "🟢 " if i == correct_opt else "🔴 " if i == selected_opt else ""
        kb.append([types.InlineKeyboardButton(text=f"{prefix}{opt}", callback_data="ignored")])
    
    kb.append([types.InlineKeyboardButton(text="➡️ السؤال التالي", callback_data="next_question")])
    kb.append([
        types.InlineKeyboardButton(text="🏁 إنهاء الكويز", callback_data="quiz_stop"),
        types.InlineKeyboardButton(text="🔗 مشاركة الكويز", callback_data="quiz_share")
    ])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_favorite_section_keyboard(sections: list, allow_new: bool = True, allow_default: bool = True) -> types.InlineKeyboardMarkup:
    kb = []
    for section in sections:
        section_id = section.get("section_id")
        title = section.get("title") or "قسم"
        kb.append([types.InlineKeyboardButton(text=f"📁 {title}", callback_data=f"fav_section_{section_id}")])

    if allow_new:
        kb.append([types.InlineKeyboardButton(text="➕ إنشاء قسم جديد", callback_data="fav_section_new")])
    if allow_default:
        kb.append([types.InlineKeyboardButton(text="⏭️ بدون قسم (حفظ في عام)", callback_data="fav_section_default")])

    kb.append([types.InlineKeyboardButton(text="🏠 العودة للقائمة الرئيسية", callback_data="favorites_back")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_pagination_keyboard(current_page: int, total_pages: int, query: str) -> types.InlineKeyboardMarkup:
    buttons = []
    if current_page > 1:
        buttons.append(types.InlineKeyboardButton(text="⬅️ السابق", callback_data=f"page_{query}_{current_page-1}"))
    if current_page < total_pages:
        buttons.append(types.InlineKeyboardButton(text="التالي ➡️", callback_data=f"page_{query}_{current_page+1}"))
    return types.InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None

# ==================== Admin Keyboards ====================

def get_admin_dashboard_keyboard() -> types.InlineKeyboardMarkup:
    """لوحة الإدارة الرئيسية للأقسام التشغيلية. الإعدادات التقنية والإدارية
    (الذكاء الاصطناعي والتحديات وهجوم الأرض) مجمعة تحت "الإعدادات العامة"."""
    kb = [
        [types.InlineKeyboardButton(text="💬 التواصل مع الطلاب", callback_data="admin_communication_menu")],
        [types.InlineKeyboardButton(text="👥 إدارة المستخدمين", callback_data="admin_users_menu")],
        [types.InlineKeyboardButton(text="📊 التحليلات والإحصائيات", callback_data="admin_stats")],
        [types.InlineKeyboardButton(text="👥 إحصائيات الكويز الجماعي", callback_data="admin_group_quiz_stats")],
        [types.InlineKeyboardButton(text="📋 تصفح ملاحظات الكويزات", callback_data="admin_view_feedbacks")],
        [types.InlineKeyboardButton(text="⚙️ الإعدادات العامة", callback_data="admin_settings_general")],
        [types.InlineKeyboardButton(text="🛠 وضع الصيانة", callback_data="admin_maintenance")],
        [types.InlineKeyboardButton(text="❌ إغلاق لوحة الإدارة", callback_data="admin_cancel")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_group_quiz_stats_keyboard() -> types.InlineKeyboardMarkup:
    """كيبورد شاشة إحصائيات الكويز الجماعي: الكويزات النشطة الآن، سجل الجلسات
    (بتفاصيل كل جلسة)، التفريغ اليدوي للمنتهية/الملغاة، + تحديث/رجوع."""
    kb = [
        [types.InlineKeyboardButton(text="🟢 الكويزات النشطة الآن", callback_data="admin_gq_active")],
        [types.InlineKeyboardButton(text="🕐 سجل الجلسات (تفاصيل كل جلسة)", callback_data="admin_gq_recent:0")],
        [types.InlineKeyboardButton(text="🧹 تفريغ المنتهية والملغاة", callback_data="admin_gq_purge")],
        [types.InlineKeyboardButton(text="🔄 نظرة عامة", callback_data="admin_group_quiz_stats")],
        [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_group_quiz_sessions_keyboard(sessions, mode: str, page: int, has_next: bool) -> types.InlineKeyboardMarkup:
    """كيبورد قائمة جلسات الكويز الجماعي: أزرار أرقام (1..n) بتفتح تفاصيل الجلسة
    المقابلة بنص الرسالة، + تنقّل بين الصفحات + رجوع.

    `mode`: `a` = النشطة الآن (waiting+active)، `r` = سجل كل الجلسات - الاتنين بصفحات.
    بيتحمّل بـ callback_data للتفاصيل (`admin_gq_s:<uuid>:<mode><page>`) حتى زر الرجوع
    من التفاصيل يرجع لنفس القائمة ونفس الصفحة."""
    kb = []
    row = []
    for i, s in enumerate(sessions, start=1):
        row.append(types.InlineKeyboardButton(
            text=str(i), callback_data=f"admin_gq_s:{s['id']}:{mode}{page}"))
        if len(row) == 4:
            kb.append(row)
            row = []
    if row:
        kb.append(row)

    list_prefix = "admin_gq_active" if mode == "a" else "admin_gq_recent"
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton(text="◀️ السابق", callback_data=f"{list_prefix}:{page - 1}"))
    if has_next:
        nav.append(types.InlineKeyboardButton(text="التالي ▶️", callback_data=f"{list_prefix}:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([types.InlineKeyboardButton(text="🔄 تحديث", callback_data=f"{list_prefix}:{page}")])
    kb.append([types.InlineKeyboardButton(text="🔙 إحصائيات الكويز الجماعي", callback_data="admin_group_quiz_stats")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_group_quiz_session_detail_keyboard(
    session_id: str, mode: str, page: int,
    participants_total: int = 0, quiz_id: str | None = None,
) -> types.InlineKeyboardMarkup:
    """كيبورد تفاصيل جلسة: زر المشاركين (لو في مشاركون مسجّلون)، تشغيل الكويز عند الأدمن
    (لو الكويز لسا موجود)، تحديث، ورجوع للقائمة اللي جاء منها الأدمن (نفس الصفحة)."""
    base = f"admin_gq_s:{session_id}:{mode}{page}"
    kb = []
    if participants_total > 0:
        kb.append([types.InlineKeyboardButton(
            text=f"👤 المشاركون ({participants_total})", callback_data=f"admin_gq_p:{session_id}:{mode}{page}:0")])
    if quiz_id:
        kb.append([types.InlineKeyboardButton(text="▶️ شغّل الكويز عندي", callback_data=f"admin_gq_try:{quiz_id}")])
    kb.append([types.InlineKeyboardButton(text="🔄 تحديث", callback_data=base)])
    back_cb = f"admin_gq_active:{page}" if mode == "a" else f"admin_gq_recent:{page}"
    kb.append([types.InlineKeyboardButton(text="🔙 رجوع للقائمة", callback_data=back_cb)])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_group_quiz_participants_keyboard(
    session_id: str, mode: str, page: int, participants_page: int, has_prev: bool, has_next: bool,
) -> types.InlineKeyboardMarkup:
    """كيبورد قائمة مشاركي جلسة: تنقّل بين الصفحات، تحديث، ورجوع لتفاصيل الجلسة
    (`admin_gq_p:<uuid>:<mode><page>:<participants_page>` أقصاه ~58 بايت من حد 64)."""
    base = f"admin_gq_p:{session_id}:{mode}{page}"
    kb = []
    nav = []
    if has_prev:
        nav.append(types.InlineKeyboardButton(text="◀️ السابق", callback_data=f"{base}:{participants_page - 1}"))
    if has_next:
        nav.append(types.InlineKeyboardButton(text="التالي ▶️", callback_data=f"{base}:{participants_page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([types.InlineKeyboardButton(text="🔄 تحديث", callback_data=f"{base}:{participants_page}")])
    kb.append([types.InlineKeyboardButton(text="🔙 تفاصيل الجلسة", callback_data=f"admin_gq_s:{session_id}:{mode}{page}")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_group_quiz_purge_confirm_keyboard() -> types.InlineKeyboardMarkup:
    """تأكيد التفريغ اليدوي (حذف نهائي للجلسات المنتهية/الملغاة)."""
    kb = [
        [types.InlineKeyboardButton(text="✅ نعم، احذف نهائياً", callback_data="admin_gq_purge_yes")],
        [types.InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_group_quiz_stats")],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_admin_communication_keyboard() -> types.InlineKeyboardMarkup:
    """قسم التواصل: رسالة مخصصة لطالب واحد، أو رسالة جماعية لكل الطلاب."""
    kb = [
        [types.InlineKeyboardButton(text="✉️ رسالة مخصصة", callback_data="admin_direct_message_prompt")],
        [types.InlineKeyboardButton(text="📢 رسالة جماعية", callback_data="admin_broadcast_prompt")],
        [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_admin_users_menu_keyboard() -> types.InlineKeyboardMarkup:
    """قسم إدارة المستخدمين: بحث عن طالب، استعراض السجل الكامل مصفحاً، تصدير CSV."""
    kb = [
        [types.InlineKeyboardButton(text="🔍 البحث عن مستخدم", callback_data="admin_search_prompt")],
        [types.InlineKeyboardButton(text="👥 استعراض الطلاب", callback_data="admin_users_page_1")],
        [types.InlineKeyboardButton(text="📭 تعذر وصول إشعار التجديد", callback_data="admin_renewal_failures:all:0")],
        [types.InlineKeyboardButton(text="🚫 حظروا البوت", callback_data="admin_renewal_failures:blocked:0")],
        [types.InlineKeyboardButton(text="📊 آخر تقرير تجديد", callback_data="admin_renewal_report")],
        [types.InlineKeyboardButton(text="📥 تصدير الكل كـ CSV", callback_data="admin_export_users")],
        [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_admin_settings_general_keyboard() -> types.InlineKeyboardMarkup:
    """قسم الإعدادات العامة: كل إعدادات النظام والإدارة التقنية بمكان واحد."""
    kb = [
        [types.InlineKeyboardButton(text="🎯 إعدادات النقاط", callback_data="admin_settings_menu")],
        [types.InlineKeyboardButton(text="🧩 مفاتيح التحكم", callback_data="admin_flags_menu")],
        [types.InlineKeyboardButton(text="🤖 التحكم بالذكاء الاصطناعي", callback_data="admin_ai_menu")],
        [types.InlineKeyboardButton(text="🎟 أكواد الهدايا", callback_data="ag:home:0")],
        [types.InlineKeyboardButton(text="🏆 إدارة التحديات", callback_data="admin_challenges")],
        [types.InlineKeyboardButton(text="🏰 إعدادات هجوم الأرض", callback_data="admin_challenge_cooldown")],
        [types.InlineKeyboardButton(text="📜 نسخ الشروط والخصوصية", callback_data="admin_legal_documents")],
        [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_admin_settings_keyboard(settings: dict, labels: dict) -> types.InlineKeyboardMarkup:
    """لوحة إعدادات النقاط: زر تعديل لكل إعداد + رجوع (قسم فرعي ضمن الإعدادات العامة -
    راجع get_admin_settings_general_keyboard). labels المتوقّع هنا هو
    settings_helper.POINTS_SETTING_LABELS (إعدادات النقاط فقط، بدون أي إعداد خاص
    بميزة معيّنة - راجع settings_helper.SETTING_TO_FEATURE_FLAG)."""
    from helpers.settings_helper import format_setting_value
    kb = []
    kb.append([types.InlineKeyboardButton(text="🎟 أكواد الهدايا", callback_data="ag:home:0")])
    for key, label in labels.items():
        value_text = format_setting_value(settings.get(key))
        kb.append([types.InlineKeyboardButton(
            text=f"{label}: {value_text}",
            callback_data=f"admin_setting_edit_{key}"
        )])
    kb.append([types.InlineKeyboardButton(text="🔙 رجوع للإعدادات العامة", callback_data="admin_settings_general")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)





def get_admin_legal_documents_keyboard(versions: dict) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text=f"📈 رفع نسخة شروط الاستخدام · {versions['terms']}",
            callback_data="admin_legal_bump:terms",
        )],
        [types.InlineKeyboardButton(
            text=f"📈 رفع نسخة سياسة الخصوصية · {versions['privacy']}",
            callback_data="admin_legal_bump:privacy",
        )],
        [types.InlineKeyboardButton(text="🔙 رجوع للإعدادات العامة", callback_data="admin_settings_general")],
    ])


def get_admin_legal_bump_confirm_keyboard(document_type: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text="✅ تأكيد رفع النسخة",
            callback_data=f"admin_legal_bump_confirm:{document_type}",
        )],
        [types.InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_legal_documents")],
    ])

# ==================== 🆕 AI Models Control Panel Keyboards ====================

def get_ai_control_keyboard(request_timeout: float = None, vertex_parallel_enabled: bool = False,
                            detection_thinking: str = "medium", generation_thinking: str = "auto") -> types.InlineKeyboardMarkup:
    """اللوحة الرئيسية للتحكم بالذكاء الاصطناعي: نقطة الدخول لكل الأقسام الفرعية
    (سلسلة التوليد، موديل الفحص السريع، موديل Groq السريع، سجل التوليد).
    ملاحظة: إعدادات النقاط ما عادت هون - انتقلت لقسم "الإعدادات العامة" المستقل
    (راجع get_admin_settings_general_keyboard) لأنها مالها علاقة بموديلات الذكاء الاصطناعي."""
    kb = [
        [types.InlineKeyboardButton(text="🔌 إدارة المزوّدين", callback_data="aip:list")],
        [types.InlineKeyboardButton(text="🔎 كتالوج نماذج Google (اختبار وتفعيل)", callback_data="gmc:start")],
        [types.InlineKeyboardButton(text="🧠 سلسلة توليد الأسئلة (Cascade)", callback_data="admin_ai_slot_cascade")],
        [types.InlineKeyboardButton(text="🔍 موديل فحص المحتوى السريع", callback_data="admin_ai_slot_detection")],
        [types.InlineKeyboardButton(text="⚡ المسار النصي السريع", callback_data="admin_ai_slot_groq_fast")],
        [types.InlineKeyboardButton(
            text=("✅ Vertex في المعالجة متعددة الأجزاء: مفعّل" if vertex_parallel_enabled
                  else "🚫 Vertex في المعالجة متعددة الأجزاء: معطّل"),
            callback_data="admin_ai_vertex_parallel_toggle",
        )],
        [types.InlineKeyboardButton(text="📊 سجل توليد الكويزات (الوقت + الموديل)", callback_data="admin_quiz_gen_log")],
    ]
    from helpers.settings_helper import AI_THINKING_LABELS
    kb.extend([
        [types.InlineKeyboardButton(
            text=f"🔍 تفكير الفحص: {AI_THINKING_LABELS[detection_thinking]}", callback_data="ait:open:detection")],
        [types.InlineKeyboardButton(
            text=f"🧠 تفكير التوليد: {AI_THINKING_LABELS[generation_thinking]}", callback_data="ait:open:generation")],
    ])
    if request_timeout is not None:
        from helpers.settings_helper import format_setting_value
        kb.append([types.InlineKeyboardButton(
            text=f"⏱ مهلة طلب الذكاء الاصطناعي: {format_setting_value(request_timeout)} ث",
            callback_data="admin_setting_edit_ai_request_timeout",
        )])
    kb.append([types.InlineKeyboardButton(text="🔙 الإعدادات العامة", callback_data="admin_settings_general")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_ai_thinking_keyboard(task: str, current: str) -> types.InlineKeyboardMarkup:
    from helpers.settings_helper import AI_THINKING_LABELS
    rows = [[types.InlineKeyboardButton(
        text=f"{'✅ ' if level == current else ''}{label}",
        callback_data=f"ait:set:{task}:{level}",
    )] for level, label in AI_THINKING_LABELS.items()]
    rows.append([types.InlineKeyboardButton(text="🔙 التحكم بالذكاء الاصطناعي", callback_data="admin_ai_menu")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def get_ai_slot_keyboard(slot: str, models: list) -> types.InlineKeyboardMarkup:
    """قائمة موديلات slot معيّن بالترتيب: كل صف = زر تفاصيل/إدارة لموديل واحد
    (▲/▼ لإعادة الترتيب، ✅/🚫 للتفعيل، 🗑 للحذف)، بالإضافة لزر "➕ إضافة موديل" ورجوع."""
    kb = []
    total = len(models)
    for index, model in enumerate(models):
        status_icon = "✅" if model.get("is_enabled") else "🚫"
        provider = model.get("provider", "?")
        label = f"{status_icon} #{index + 1} [{provider}] {model.get('model_name')}"
        kb.append([types.InlineKeyboardButton(text=label, callback_data=f"admin_ai_model_{model['id']}")])
        nav_row = []
        if index > 0:
            nav_row.append(types.InlineKeyboardButton(text="⬆️", callback_data=f"admin_ai_move_up_{model['id']}_{slot}"))
        if index < total - 1:
            nav_row.append(types.InlineKeyboardButton(text="⬇️", callback_data=f"admin_ai_move_down_{model['id']}_{slot}"))
        toggle_text = "🚫 تعطيل" if model.get("is_enabled") else "✅ تفعيل"
        nav_row.append(types.InlineKeyboardButton(text=toggle_text, callback_data=f"admin_ai_toggle_{model['id']}_{slot}"))
        nav_row.append(types.InlineKeyboardButton(text="✏️ تعديل", callback_data=f"aim:edit:{slot}:{model['id']}"))
        nav_row.append(types.InlineKeyboardButton(text="🗑 حذف", callback_data=f"admin_ai_delete_{model['id']}_{slot}"))
        kb.append(nav_row)
    kb.append([types.InlineKeyboardButton(text="➕ إضافة موديل جديد", callback_data=f"admin_ai_add_{slot}")])
    if slot == "detection":
        kb.append([types.InlineKeyboardButton(
            text="🔎 اكتشف نماذج Google واختبرها", callback_data="gmc:start")])
    kb.append([types.InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_ai_menu")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_ai_provider_choice_keyboard(slot: str, providers: dict) -> types.InlineKeyboardMarkup:
    rows = [[types.InlineKeyboardButton(text=p["label"], callback_data=f"admin_ai_provider_{p['id']}_{slot}")]
            for p in providers.values() if p["is_enabled"]]
    rows.append([types.InlineKeyboardButton(text="إدارة المزوّدين", callback_data="aip:list")])
    rows.append([types.InlineKeyboardButton(text="إلغاء", callback_data=f"admin_ai_slot_{slot}")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def get_ai_cancel_keyboard(slot: str) -> types.InlineKeyboardMarkup:
    kb = [[types.InlineKeyboardButton(text="🔙 إلغاء", callback_data=f"admin_ai_slot_{slot}")]]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def build_contact_button(user_id: int, username: Optional[str] = None, text: str = "💬 تواصل مع الطالب") -> types.InlineKeyboardButton:
    """
    زر تواصل مع مستخدم — بدون الاعتماد على tg://user?id= الذي قد يُرفض
    بخطأ BUTTON_USER_PRIVACY_RESTRICTED حسب إعدادات خصوصية المستخدم.
    - لو عنده يوزر عام: رابط https://t.me/username عادي (لا يخضع لقيود الخصوصية دي).
    - لو مفيش يوزر: زر يوجّه لتدفق "رسالة مباشرة عبر البوت" (admin_direct_start_<id>)
      وهو دايماً شغال طالما المستخدم لم يحظر البوت.
    """
    if username and username != "Unknown":
        clean = username.lstrip("@")
        return types.InlineKeyboardButton(text=text, url=f"https://t.me/{clean}")
    return types.InlineKeyboardButton(text=text, callback_data=f"admin_direct_start_{user_id}")


def get_admin_user_actions_keyboard(user_id: int, username: Optional[str] = None) -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="🎁 هدايا الطالب وصلاحيتها", callback_data=f"ag:gifts:{user_id}:0")],
        [types.InlineKeyboardButton(text="💰 شحن رصيد الطالب", callback_data=f"admin_charge_menu_{user_id}")],
        [
            types.InlineKeyboardButton(text="📈 نشاط هذا الطالب", callback_data=f"admin_user_activity_{user_id}"),
            types.InlineKeyboardButton(text="🎯 كويزات هذا الطالب", callback_data=f"admin_user_quizzes_{user_id}_p_1")
        ],
        [build_contact_button(user_id, username)],
        [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_charge_options_keyboard(user_id: int) -> types.InlineKeyboardMarkup:
    """أزرار شحن الرصيد. الرجوع يوديك لبروفايل نفس الطالب (admin_user_profile_<id>)
    مش للقائمة الرئيسية، حتى ما يضيع سياق الطالب اللي كنت شغال عليه."""
    kb = [
        [
            types.InlineKeyboardButton(text="➕ 10 نقاط", callback_data=f"admin_charge_quick_10_{user_id}"),
            types.InlineKeyboardButton(text="➕ 50 نقطة", callback_data=f"admin_charge_quick_50_{user_id}"),
            types.InlineKeyboardButton(text="➕ 100 نقطة", callback_data=f"admin_charge_quick_100_{user_id}")
        ],
        [types.InlineKeyboardButton(text="✍️ إدخال كمية يدوياً", callback_data=f"admin_charge_manual_{user_id}")],
        [types.InlineKeyboardButton(text="🔙 رجوع لبيانات الطالب", callback_data=f"admin_user_profile_{user_id}")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_cancel_keyboard() -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_direct_message_charge_keyboard(user_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🎁 شحن مجاني", callback_data=f"admin_direct_charge_free_{user_id}")],
        [types.InlineKeyboardButton(text="💳 شحن مدفوع", callback_data=f"admin_direct_charge_paid_{user_id}")],
        [types.InlineKeyboardButton(text="⏭️ بدون شحن", callback_data=f"admin_direct_charge_none_{user_id}")],
        [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")],
    ])

def get_admin_direct_message_mode_keyboard() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✉️ رسالة عادية فقط", callback_data="admin_direct_mode_message")],
        [types.InlineKeyboardButton(text="💰 رسالة مع شحن رصيد", callback_data="admin_direct_mode_charge")],
        [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")],
    ])

def get_analytics_keyboard(days: int) -> types.InlineKeyboardMarkup:
    period_row = []
    for d, label in [(7, "7 أيام"), (30, "30 يوم"), (90, "90 يوم")]:
        text = f"✅ {label}" if d == days else label
        period_row.append(types.InlineKeyboardButton(text=text, callback_data=f"admin_analytics_{d}"))
    
    kb = [
        period_row,
        [types.InlineKeyboardButton(text="📊 جميع أنواع الأحداث", callback_data=f"admin_events_{days}_1")],
        [
            types.InlineKeyboardButton(text="⚡ النشطون اليوم", callback_data="admin_analytics_today"),
            types.InlineKeyboardButton(text="🎯 كويزات اليوم", callback_data="admin_today_quizzes_p_1"),
        ],
        [types.InlineKeyboardButton(text="📅 النشاط اليومي (آخر 14 يوم)", callback_data="admin_analytics_daily")],
        [types.InlineKeyboardButton(text="🐞 آخر الأخطاء", callback_data="admin_recent_errors")],
        # 🆕 «قائمة الإحالات» انتقلت إلى داخل لوحة «طرق الانضمام» (handlers/admin/join_sources.py)
        [types.InlineKeyboardButton(text="📥 طرق الانضمام إلى البوت", callback_data="admin_joinsrc_30")],
        [types.InlineKeyboardButton(text="📥 تصدير سجل الأحداث CSV", callback_data="admin_export_events")],
        [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

# ==================== 🆕 مفاتيح التحكم (Feature Flags) ====================

def get_feature_flags_keyboard(flags: dict, registry: dict) -> types.InlineKeyboardMarkup:
    """لوحة "مفاتيح التحكم": زر واحد لكل ميزة يعرض حالتها الحالية، لكنه لا يبدّلها
    مباشرة (كما كان سابقاً) بل يفتح لوحة تفاصيل الميزة (get_feature_detail_keyboard)
    التي فيها زر التشغيل/الإيقاف نفسه بالإضافة لأي إعدادات أخرى خاصة بتلك الميزة
    (مثلاً عتبة تثبيت التصنيف لميزة classification_vote_prompt - كانت هذه العتبة
    ضائعة سابقاً ضمن "إعدادات النقاط" رغم أنها لا علاقة لها بالنقاط). flags: {key:
    enabled} (من supabase_helper.get_all_feature_flags), registry: {key: label}
    (من constants.FEATURE_FLAGS_REGISTRY)."""
    kb = []
    for key, label in registry.items():
        enabled = flags.get(key, True)
        status_icon = "✅" if enabled else "🚫"
        kb.append([types.InlineKeyboardButton(
            text=f"{status_icon} {label}",
            callback_data=f"admin_flag_detail_{key}"
        )])
    kb.append([types.InlineKeyboardButton(text="🔙 رجوع للإعدادات العامة", callback_data="admin_settings_general")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def get_feature_detail_keyboard(flag_key: str, enabled: bool, extra_settings: list,
                                sub_flags: list = None) -> types.InlineKeyboardMarkup:
    """لوحة تفاصيل ميزة واحدة ضمن "مفاتيح التحكم": زر تشغيل/إيقاف الميزة نفسها +
    زر تعديل لكل إعداد إضافي خاص بها (extra_settings)، بدل الاكتفاء بالتشغيل/الإيقاف
    فقط كما كانت get_feature_flags_keyboard قديماً. extra_settings: قائمة عناصر
    (setting_key, label, value_text) جاهزة للعرض - راجع
    handlers/admin/feature_flags.py (FEATURE_EXTRA_SETTINGS + render_feature_detail)
    لمصدرها. sub_flags: قائمة (key, label, enabled) لمفاتيح فرعية
    (constants.FEATURE_SUBFLAGS_REGISTRY) تظهر كأزرار تبديل تحت زر الميزة نفسها."""
    kb = []
    toggle_text = "🚫 إيقاف الميزة" if enabled else "✅ تشغيل الميزة"
    kb.append([types.InlineKeyboardButton(text=toggle_text, callback_data=f"admin_flag_toggle_{flag_key}")])
    for sub_key, sub_label, sub_enabled in (sub_flags or []):
        kb.append([types.InlineKeyboardButton(
            text=f"{sub_label}: {'✅ مفعّل' if sub_enabled else '🚫 موقوف'}",
            callback_data=f"admin_flag_toggle_{sub_key}"
        )])
    for setting_key, label, value_text in extra_settings:
        kb.append([types.InlineKeyboardButton(
            text=f"{label}: {value_text}",
            callback_data=f"admin_setting_edit_{setting_key}"
        )])
    kb.append([types.InlineKeyboardButton(text="🔙 رجوع لمفاتيح التحكم", callback_data="admin_flags_menu")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


# ==================== 🆕 كيبوردات الكويز الجماعي (ضمن الغروبات) ====================
# راجع services/group_quiz_store.py و handlers/group_quiz.py.
# صيغة callback_data مختصرة عمداً (gqt/gqp) لأن معرّف الجلسة UUID بطول 36 حرفاً
# وسقف تيليجرام لـ callback_data هو 64 بايت.

# خيارات المؤقّت الداخلي لكل سؤال (open_period). 0 = بلا مؤقّت.
GROUP_TIMER_PRESETS = [0, 15, 30, 45, 60]
# خيارات الفاصل الزمني الثابت بين سؤال وسؤال (بالثواني).
GROUP_INTERVAL_PRESETS = [30, 60, 120, 300]


def get_group_anonymity_keyboard(session_id: str) -> types.InlineKeyboardMarkup:
    """الشاشة الأولى فعلياً (قبل كل شي): تنافسي (نقاط/ترتيب) أم مجهول بالكامل؟

    هاد فعلياً is_anonymous بنداء bot.send_poll، مو ميزة عرض بس - تيليجرام ما
    بيبعت poll_answer إطلاقاً للاستفتاءات المجهولة، يعني الوضع المجهول بيلغي
    التتبّع من جذوره (بلا نقاط ولا ترتيب)، مقابل إنه محدا - ولا حتى البوت -
    بيعرف مين جاوب شو. راجع migration_group_quiz_anonymous_mode.sql."""
    rows = [
        [types.InlineKeyboardButton(text="🏆 تنافسي (نقاط + ترتيب نهائي)", callback_data=f"gqa:{session_id}:c")],
        [types.InlineKeyboardButton(text="🙈 مجهول بالكامل (بلا نقاط ولا ترتيب)", callback_data=f"gqa:{session_id}:a")],
        [types.InlineKeyboardButton(text="❌ إلغاء", callback_data=f"gqc:{session_id}")],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def get_group_names_privacy_keyboard(session_id: str) -> types.InlineKeyboardMarkup:
    """الشاشة الأولى فعلياً (قبل المؤقّت): هل تظهر أسماء المشاركين برسائل البوت
    (الترتيب النهائي/الدوري)؟ ⚠️ لا علاقة لهالإعداد بزر "من صوّت لمين" على
    الاستفتاء نفسه - هاد مفروض من تيليجرام بمجرد is_anonymous=False (إجباري
    للتصحيح) وما بينطفي من هون. راجع migration_group_quiz_show_names.sql."""
    rows = [
        [types.InlineKeyboardButton(text="👤 اعرض الأسماء بالترتيب", callback_data=f"gqn:{session_id}:1")],
        [types.InlineKeyboardButton(text="🙈 إخفِ الأسماء (لاعب ١، لاعب ٢...)", callback_data=f"gqn:{session_id}:0")],
        [types.InlineKeyboardButton(text="❌ إلغاء", callback_data=f"gqc:{session_id}")],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


GROUP_TOP_N_MIN, GROUP_TOP_N_MAX, GROUP_TOP_N_DEFAULT = 1, 20, 10
GROUP_TOP_N_PRESETS = [3, 5, 10, 15, 20]


def get_group_top_n_keyboard(session_id: str, n: int = GROUP_TOP_N_DEFAULT) -> types.InlineKeyboardMarkup:
    """شاشة اختيار عدد الأوائل بالترتيب النهائي (1-20): أزرار جاهزة + ➖/➕ + تأكيد.

    التنقّل (`gqk:<id>:n:<رقم>`) بيعيد رسم الأزرار بس بلا أي كتابة بقاعدة البيانات؛ الحفظ
    الفعلي بالتأكيد (`gqk:<id>:ok:<رقم>`)."""
    n = max(GROUP_TOP_N_MIN, min(GROUP_TOP_N_MAX, int(n)))
    base = f"gqk:{session_id}"
    presets = [
        types.InlineKeyboardButton(text=(f"• {v} •" if v == n else str(v)), callback_data=f"{base}:n:{v}")
        for v in GROUP_TOP_N_PRESETS
    ]
    stepper = [
        types.InlineKeyboardButton(text="➖", callback_data=f"{base}:n:{max(GROUP_TOP_N_MIN, n - 1)}"),
        types.InlineKeyboardButton(text=f"🏅 {n}", callback_data=f"{base}:noop"),
        types.InlineKeyboardButton(text="➕", callback_data=f"{base}:n:{min(GROUP_TOP_N_MAX, n + 1)}"),
    ]
    rows = [
        presets,
        stepper,
        [types.InlineKeyboardButton(text=f"✅ تأكيد (أول {n})", callback_data=f"{base}:ok:{n}")],
        [types.InlineKeyboardButton(text="❌ إلغاء", callback_data=f"gqc:{session_id}")],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def get_group_timer_keyboard(session_id: str) -> types.InlineKeyboardMarkup:
    """الشاشة الأولى: هل في مؤقّت داخلي لكل سؤال؟ وكم؟"""
    rows = []
    row = []
    for seconds in GROUP_TIMER_PRESETS:
        label = "🚫 بدون مؤقّت" if seconds == 0 else f"⏱ {seconds} ثانية"
        row.append(types.InlineKeyboardButton(text=label, callback_data=f"gqt:{session_id}:{seconds}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([types.InlineKeyboardButton(text="❌ إلغاء", callback_data=f"gqc:{session_id}")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def get_group_pacing_keyboard(session_id: str, timer_enabled: bool) -> types.InlineKeyboardMarkup:
    """الشاشة الثانية: متى يُرسل السؤال التالي؟

    خيار "فور انتهاء المؤقّت" (chain_to_timer) بيظهر **فقط** لو المعلّم فعّل
    المؤقّت الداخلي بالشاشة السابقة - بلا مؤقّت ما في حدث إغلاق نعتمد عليه.
    """
    rows = []
    if timer_enabled:
        rows.append([types.InlineKeyboardButton(
            text="⚡️ فور انتهاء مؤقّت السؤال",
            callback_data=f"gqp:{session_id}:c:0",
        )])
    else:
        # 🚀 كل الأسئلة دفعة واحدة (burst): بفاصل قصير ثابت بين سؤال وسؤال لتفادي حدود
        # تيليجرام. بلا مؤقّت فقط - مع مؤقّت داخلي كانت الاستفتاءات الأولى رح تسكّر
        # قبل ما الطلاب يوصلولها.
        rows.append([types.InlineKeyboardButton(
            text="🚀 كل الأسئلة دفعة واحدة",
            callback_data=f"gqp:{session_id}:b:0",
        )])
    row = []
    for seconds in GROUP_INTERVAL_PRESETS:
        label = f"⏳ كل {seconds} ثانية" if seconds < 60 else f"⏳ كل {seconds // 60} دقيقة"
        row.append(types.InlineKeyboardButton(text=label, callback_data=f"gqp:{session_id}:f:{seconds}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([types.InlineKeyboardButton(text="❌ إلغاء", callback_data=f"gqc:{session_id}")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


# ==================== شاشة ملخّص إعدادات الكويز الجماعي (gqs:) ====================
# بديل السلسلة القديمة gqa -> gqn -> gqk -> gqt -> gql -> gqp: شاشة ملخّص وحدة + قائمة
# إعدادات. كل ضغطة إعداد بتنحفظ فوراً وبتنعاد نفس الشاشة. القواعد والتعارضات كلها
# بـ handlers/group_quiz.py::_compute_setup_update (هون عرض أزرار بس).
SETUP_DEFAULT_TOP_N = 5
SETUP_DEFAULT_INTERVAL_SECONDS = 30


def _setup_btn(text: str, session_id: str, tail: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text=text, callback_data=f"gqs:{session_id}:{tail}")


def _mark(label: str, selected: bool) -> str:
    return f"✅ {label}" if selected else label


def get_group_setup_summary_keyboard(session_id: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [_setup_btn("🚀 ابدأ الاختبار", session_id, "go")],
        [_setup_btn("⚙️ إعدادات الاختبار", session_id, "m")],
        [types.InlineKeyboardButton(text="❌ إلغاء", callback_data=f"gqc:{session_id}")],
    ])


def get_group_setup_menu_keyboard(
    session_id: str, *, is_channel: bool = False, anonymous: bool = False, lock_available: bool = True,
    cert_available: bool = False, ceremony_available: bool = False,
) -> types.InlineKeyboardMarkup:
    """قائمة الإعدادات. الأزرار الما إلها معنى بتنخفى: القنوات بلا نمط/قفل، والمجهول بلا أسماء/أوائل."""
    rows = []
    if not is_channel:
        rows.append([_setup_btn("🎮 نمط الجلسة", session_id, "o:mode")])
    if not anonymous:
        rows.append([_setup_btn("👤 ظهور الأسماء", session_id, "o:names")])
        rows.append([_setup_btn("🏅 عدد الأوائل", session_id, "o:top")])
    rows.append([_setup_btn("⏱ المؤقّت الداخلي", session_id, "o:timer")])
    rows.append([_setup_btn("⏳ إيقاع إرسال الأسئلة", session_id, "o:pace")])
    if not is_channel and lock_available:
        rows.append([_setup_btn("🔒 قفل رسائل المجموعة", session_id, "o:lock")])
    if cert_available and not is_channel:
        rows.append([_setup_btn("🎓 شهادات التقدير", session_id, "o:cert")])
    if ceremony_available and not is_channel:
        rows.append([_setup_btn("🏆 حفل التكريم", session_id, "o:cer")])
    rows.append([_setup_btn("🔙 رجوع للملخّص", session_id, "b")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _back_row(session_id: str) -> list:
    return [_setup_btn("🔙 رجوع", session_id, "m")]


def get_group_setup_mode_keyboard(session_id: str, *, anonymous: bool) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [_setup_btn(_mark("🏆 تنافسي (نقاط + ترتيب نهائي)", not anonymous), session_id, "s:mode:c")],
        [_setup_btn(_mark("🙈 مجهول بالكامل (بلا نقاط ولا ترتيب)", anonymous), session_id, "s:mode:a")],
        _back_row(session_id),
    ])


def get_group_setup_names_keyboard(session_id: str, *, show_names: bool) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [_setup_btn(_mark("👤 اعرض الأسماء بالترتيب", show_names), session_id, "s:names:1")],
        [_setup_btn(_mark("🙈 إخفِ الأسماء (لاعب ١، لاعب ٢...)", not show_names), session_id, "s:names:0")],
        _back_row(session_id),
    ])


def get_group_setup_top_keyboard(session_id: str, top_n: int) -> types.InlineKeyboardMarkup:
    n = max(GROUP_TOP_N_MIN, min(GROUP_TOP_N_MAX, int(top_n)))
    presets = [_setup_btn(_mark(str(v), v == n), session_id, f"s:top:{v}") for v in GROUP_TOP_N_PRESETS]
    stepper = [
        _setup_btn("➖", session_id, f"s:top:{max(GROUP_TOP_N_MIN, n - 1)}"),
        _setup_btn(f"🏅 {n}", session_id, "noop"),
        _setup_btn("➕", session_id, f"s:top:{min(GROUP_TOP_N_MAX, n + 1)}"),
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=[presets, stepper, _back_row(session_id)])


def get_group_setup_timer_keyboard(session_id: str, timer_seconds: int) -> types.InlineKeyboardMarkup:
    rows, row = [], []
    for seconds in GROUP_TIMER_PRESETS:
        label = "🚫 بدون مؤقّت" if seconds == 0 else f"⏱ {seconds} ثانية"
        row.append(_setup_btn(_mark(label, seconds == int(timer_seconds or 0)), session_id, f"s:timer:{seconds}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(_back_row(session_id))
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def get_group_setup_pace_keyboard(
    session_id: str, *, pace: str, timer_enabled: bool, lock: bool = False,
) -> types.InlineKeyboardMarkup:
    """`pace` ∈ c | b | f<ثواني>. «فور انتهاء المؤقّت» بس مع مؤقّت، و«دفعة واحدة» بس بلا مؤقّت وبلا قفل."""
    rows = []
    if timer_enabled:
        rows.append([_setup_btn(_mark("⚡️ فور انتهاء مؤقّت السؤال", pace == "c"), session_id, "s:pace:c")])
    elif not lock:
        rows.append([_setup_btn(_mark("🚀 كل الأسئلة دفعة واحدة", pace == "b"), session_id, "s:pace:b")])
    row = []
    for seconds in GROUP_INTERVAL_PRESETS:
        label = f"⏳ كل {seconds} ثانية" if seconds < 60 else f"⏳ كل {seconds // 60} دقيقة"
        row.append(_setup_btn(_mark(label, pace == f"f{seconds}"), session_id, f"s:pace:f{seconds}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(_back_row(session_id))
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def get_group_setup_lock_keyboard(session_id: str, *, lock: bool) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [_setup_btn(_mark("🔓 بدون قفل (الافتراضي)", not lock), session_id, "s:lock:0")],
        [_setup_btn(_mark("🔒 تفعيل القفل", lock), session_id, "s:lock:1")],
        _back_row(session_id),
    ])


def get_group_question_control_keyboard(session_id: str) -> types.InlineKeyboardMarkup:
    """كيبورد كل سؤال جماعي - زر واحد بس (مش لكل سؤال أزرار تنقّل، الإرسال تلقائي
    بالكامل حسب القرار المعماري رقم 3 بالخطة). يظهر تحت كل poll طول الجلسة."""
    return types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="🏁 إنهاء الجلسة الآن", callback_data=f"gqe:{session_id}")
    ]])


def get_group_channel_picker_keyboard(
    share_id: str,
    channels: list,
    add_url: Optional[str] = None,
    *,
    refresh_prefix: str = "gqchr",
    add_text: str = "➕ أضف البوت لقناة",
) -> types.InlineKeyboardMarkup:
    """شاشة \"📢 شارك مع قناة\": زر لكل قناة جاهزة (البوت أدمن فيها + عنده صلاحية نشر +
    المستخدم نفسه أدمن فيها - الفلترة الأخيرة بيعملها المستدعي لايف)، وتحتها:

    - \"➕ أضف البوت لقناة\" (`add_url`): رابط `?startchannel&admin=post_messages`
      الرسمي - بيفتح لائحة تيليجرام الأصلية لاختيار القناة وبيضيف البوت كأدمن
      بصلاحية النشر بخطوة وحدة (موثّق بـ core.telegram.org/api/links). `None` = بلا زر.
    - \"🔄 تحديث\" (`gqchr:`): إعادة بناء الشاشة بعد ما يرجع المستخدم من الإضافة.

    `callback_data` = `gqchp:<share_id>:<chat_id>` (12 + ~14 حرف، تحت حد الـ 64 بايت).
    `chat_title` جاي من تيليجرام - بيُقصّ لطول معقول لأنه نص زر (مو HTML، فما
    في داعي escape هون).
    """
    rows = []
    for ch in channels:
        title = (ch.get("chat_title") or "قناة بلا اسم").strip()
        if len(title) > 40:
            title = title[:39] + "…"
        icon = "📢" if ch.get("chat_type") == "channel" else "👥"
        rows.append([types.InlineKeyboardButton(
            text=f"{icon} {title}",
            callback_data=f"gqchp:{share_id}:{int(ch['chat_id'])}",
        )])
    if add_url:
        rows.append([types.InlineKeyboardButton(text=add_text, url=add_url)])
    rows.append([types.InlineKeyboardButton(text="🔄 تحديث", callback_data=f"{refresh_prefix}:{share_id}")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


_CERT_THEME_BUTTONS = (
    ("blue", "🔵 أزرق"), ("green", "🟢 أخضر"), ("gold", "🟡 ذهبي"),
    ("purple", "🟣 بنفسجي"), ("red", "🔴 أحمر"), ("slate", "⚫ رمادي"),
)
_CERT_COUNT_CHOICES = (1, 3, 5, 10)


def get_group_setup_cert_keyboard(session_id: str, cfg: dict, *, enabled: bool) -> types.InlineKeyboardMarkup:
    """شاشة إعداد الشهادات. `cfg` إعدادات مُطبَّعة (services.group_certificate.normalize_config).
    الـ callbacks كلها gqs:<sid>:s:cert:<value> (ما في بادئة جديدة): 1/0 · th<لون> · d1/d0 · r1/r0 · sc1/sc0 · n<عدد>
    + pv (معاينة) · te (تعديل اسم الاختبار) · ie (تعديل الجهة/المعلّم)."""
    rows = [
        [_setup_btn("✅ مفعّلة (اضغط للتعطيل)" if enabled else "☑️ تفعيل الشهادات", session_id, "s:cert:0" if enabled else "s:cert:1")],
        [_setup_btn("✏️ اسم الاختبار", session_id, "s:cert:te"), _setup_btn("✍️ الجهة / المعلّم", session_id, "s:cert:ie")],
    ]
    themes = [_setup_btn(_mark(label, cfg.get("theme") == key), session_id, f"s:cert:th{key}") for key, label in _CERT_THEME_BUTTONS]
    rows += [themes[:3], themes[3:]]
    rows.append([
        _setup_btn("📅 التاريخ: " + ("يظهر" if cfg.get("show_date") else "مخفي"), session_id, "s:cert:d0" if cfg.get("show_date") else "s:cert:d1"),
        _setup_btn("🏅 المركز: " + ("يظهر" if cfg.get("show_rank") else "عام"), session_id, "s:cert:r0" if cfg.get("show_rank") else "s:cert:r1"),
        _setup_btn("🔢 الدرجة: " + ("تظهر" if cfg.get("show_score") else "مخفية"), session_id, "s:cert:sc0" if cfg.get("show_score") else "s:cert:sc1"),
    ])
    rows.append([_setup_btn(_mark(f"أول {n}", cfg.get("count") == n), session_id, f"s:cert:n{n}") for n in _CERT_COUNT_CHOICES])
    rows.append([_setup_btn("👁 معاينة الشهادة", session_id, "s:cert:pv")])
    rows.append(_back_row(session_id))
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


_CEREMONY_COUNT_CHOICES = (3, 5, 10)


def get_group_setup_ceremony_keyboard(session_id: str, cfg: dict, *, enabled: bool) -> types.InlineKeyboardMarkup:
    """شاشة إعداد حفل التكريم. `cfg` إعدادات مُطبَّعة (services.group_ceremony.normalize_config).
    الـ callbacks كلها gqs:<sid>:s:cer:<value>: 1/0 · fv/fi (فيديو/صورة) · m1/m0 (الأغنية) · n<عدد>
    + pv (معاينة) · te (تعديل عنوان الحفل)."""
    is_video = cfg.get("format") != "image"
    rows = [
        [_setup_btn("✅ مفعّل (اضغط للتعطيل)" if enabled else "☑️ تفعيل حفل التكريم", session_id, "s:cer:0" if enabled else "s:cer:1")],
        [_setup_btn("✏️ عنوان الحفل", session_id, "s:cer:te")],
        [_setup_btn(_mark("🎬 فيديو (لهب متحرّك)", is_video), session_id, "s:cer:fv"),
         _setup_btn(_mark("🖼 صورة ثابتة", not is_video), session_id, "s:cer:fi")],
        [_setup_btn("🎵 الأغنية: " + ("تشتغل" if cfg.get("music", True) else "مطفية"), session_id,
                    "s:cer:m0" if cfg.get("music", True) else "s:cer:m1")],
        [_setup_btn(_mark(f"أول {n}", cfg.get("count") == n), session_id, f"s:cer:n{n}") for n in _CEREMONY_COUNT_CHOICES],
        [_setup_btn("👁 معاينة الحفل", session_id, "s:cer:pv")],
        _back_row(session_id),
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=rows)
