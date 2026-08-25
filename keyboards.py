from aiogram import types
from constants import (
    OFFICIAL_CHANNEL_URL, SUPPORT_BOT_URL, BTN_CANCEL_REQUEST,
    BTN_TRANSLATE_YES, BTN_TRANSLATE_NO,
    QUESTION_TYPE_OPTIONS, QUESTION_TYPE_GENERAL, QUESTION_TYPE_CUSTOM,
    DIFFICULTY_EASY, DIFFICULTY_MEDIUM, DIFFICULTY_ADVANCED, DIFFICULTY_PROGRESSIVE, DIFFICULTY_LABELS_AR,
    BTN_QUESTION_TYPE_GENERAL, BTN_QUESTION_TYPE_CUSTOM, BTN_BACK_TO_TYPE_SCREEN,
    SUBJECT_MATH, SUBJECT_ENGLISH,
    WEBAPP_PUBLIC_BASE_URL,
    BTN_AUDIO_CONFIRM_START,
    BTN_OPEN_UPLOAD_PAGE,
)
from logger import get_logger
from services.export_service import STYLE_CODE_TO_NAME, STYLE_LABELS_AR

logger = get_logger(__name__)

# ==================== لوحات التحكم والملاحة العامة ====================

def get_main_menu_keyboard(bot_username: str, user_id: int) -> types.InlineKeyboardMarkup:
    try:
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        kb = [
            [types.InlineKeyboardButton(text="🎬 كيف يعمل البوت؟ (دليل سريع)", callback_data="how_to_use")],
            [types.InlineKeyboardButton(text="💰 شحن الرصيد (نقاط إضافية)", callback_data="recharge_info")],
            [types.InlineKeyboardButton(text="⭐ قائمتي المفضلة المنظمة", callback_data="favorites_menu")],
            [
                types.InlineKeyboardButton(text="📢 قناة الأخبار", url=OFFICIAL_CHANNEL_URL),
                types.InlineKeyboardButton(text="💬 الدعم الفني", url=SUPPORT_BOT_URL)
            ],
            [types.InlineKeyboardButton(text="🔗 شارك واربح نقاط مجانية", switch_inline_query=f"\nاشترك في بوت الكويزات الرهيب عبر رابطي واربح نقاطاً: {ref_link}")]
        ]
        # 🆕 زر رفع محاضرة صوتية كبيرة (حتى 250MB) عبر Mini App - يُخفى تلقائياً لو
        # WEBAPP_PUBLIC_BASE_URL فاضي (مثلاً بوضع polling محلي بدون WEBHOOK_URL)
        # حتى ما نعرض زر مكسور بيفتح رابط فاضي.
        if WEBAPP_PUBLIC_BASE_URL:
            kb.append([
                types.InlineKeyboardButton(
                    text="🎙️ تفريغ ملف صوتي كبير (حتى 250MB)",
                    web_app=types.WebAppInfo(url=f"{WEBAPP_PUBLIC_BASE_URL}/webapp/audio_upload.html"),
                )
            ])
            # 🆕 نفس فكرة زر الصوت أعلاه، لكن لمستندات كبيرة (حتى 150 صفحة/100MB)
            # أو ألبومات صور كبيرة (حتى 50 صورة سوا) - كلاهما عبر webapp/file_upload.html
            # بباراميتر type يفرّق النوعين (راجع الملف نفسه لتفاصيل الواجهة).
            kb.append([
                types.InlineKeyboardButton(
                    text="📄 رفع ملف كبير (حتى 150 صفحة/100MB)",
                    web_app=types.WebAppInfo(url=f"{WEBAPP_PUBLIC_BASE_URL}/webapp/file_upload.html?type=document"),
                )
            ])
            kb.append([
                types.InlineKeyboardButton(
                    text="🖼️ رفع ألبوم صور كبير (حتى 50 صورة)",
                    web_app=types.WebAppInfo(url=f"{WEBAPP_PUBLIC_BASE_URL}/webapp/file_upload.html?type=images"),
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
    upload_type: "audio" (صفحة audio_upload.html الحالية) أو "document"/"images"
    (صفحة file_upload.html؟type=... الموحّدة). تُرجع لوحة فاضية لو WEBAPP_PUBLIC_BASE_URL
    غير مُهيّأ (بدل زر مكسور برابط فاضي).
    """
    if not WEBAPP_PUBLIC_BASE_URL:
        return types.InlineKeyboardMarkup(inline_keyboard=[])
    if upload_type == "audio":
        url = f"{WEBAPP_PUBLIC_BASE_URL}/webapp/audio_upload.html"
    else:
        url = f"{WEBAPP_PUBLIC_BASE_URL}/webapp/file_upload.html?type={upload_type}"
    return types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text=BTN_OPEN_UPLOAD_PAGE, web_app=types.WebAppInfo(url=url)),
    ]])

def get_export_style_keyboard(favorite_id: str = "") -> types.InlineKeyboardMarkup:
    """لوحة اختيار شكل تنسيق الملف (بسيط / عصري / أكاديمي) - أول خطوة بمسار التصدير."""
    suffix = f"_{favorite_id}" if favorite_id else ""
    prefix = "fav_export_style_" if favorite_id else "export_style_"
    kb = [
        [types.InlineKeyboardButton(text=f"⚪ {STYLE_LABELS_AR[name]}", callback_data=f"{prefix}{code}{suffix}")]
        for code, name in STYLE_CODE_TO_NAME.items()
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_export_format_keyboard(style_code: str = "s", favorite_id: str = "") -> types.InlineKeyboardMarkup:
    """لوحة اختيار صيغة تحميل الكويز (Word أو PDF)، مع الحفاظ على الستايل المختار،
    تدعم التصدير من الجلسة الحالية أو من كويز محفوظ بالمفضلة"""
    if favorite_id:
        docx_cb = f"fav_export_docx_{style_code}_{favorite_id}"
        pdf_cb = f"fav_export_pdf_{style_code}_{favorite_id}"
    else:
        docx_cb, pdf_cb = f"export_docx_{style_code}", f"export_pdf_{style_code}"
    kb = [
        [
            types.InlineKeyboardButton(text="📄 Word (docx)", callback_data=docx_cb),
            types.InlineKeyboardButton(text="📕 PDF", callback_data=pdf_cb)
        ]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_quiz_result_keyboard(quiz_id: str = None) -> types.InlineKeyboardMarkup:
    # 🆕 إعادة تنظيم بصري: تجميع الإجراءات المترابطة بصف واحد (2 بجنب بعض)
    # بدل صف مستقل لكل زر - يقلل عدد الصفوف من 9 إلى 6 ويخلي الشاشة أخف.
    kb = [
        [
            types.InlineKeyboardButton(text="🔄 إعادة المحاولة", callback_data="quiz_replay"),
            types.InlineKeyboardButton(text="🔗 مشاركة الكويز", callback_data="quiz_share"),
        ],
        [
            types.InlineKeyboardButton(text="⭐ حفظ بالمفضلة", callback_data="quiz_favorite"),
            types.InlineKeyboardButton(text="📁pdf تحميل الكويز", callback_data="export_menu"),
        ],
    ]

    if quiz_id:
        # 🆕 ما في ولا زر متعلق بالنشر/الإخفاء هون - القرار كله صار تحت لوحة
        # الشرف نفسها (راجع get_leaderboard_keyboard)، هون بس زر الدخول إلها.
        kb.append([types.InlineKeyboardButton(text="🏆 عرض لوحة الشرف", callback_data=f"leaderboard_{quiz_id}")])

    kb.append([types.InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="quiz_home")])
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
    kb.append([types.InlineKeyboardButton(text="🚀 ابدأ التوليد", callback_data="qcount_start")])
    kb.append([types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_quiz_type_keyboard(subject_type: str, suggested_types: list, selected_type: str) -> types.InlineKeyboardMarkup:
    """
    🆕 المرحلة الأولى من شاشة الخيارات (نوع الأسئلة فقط - بدون صعوبة بنفس الشاشة لتقليل
    الازدحام البصري). اختيار أي نوع (أو "متنوع") ينقل تلقائياً لمرحلة الصعوبة - لا حاجة
    لزر "متابعة" منفصل.
    """
    kb = []

    if subject_type in (SUBJECT_MATH, SUBJECT_ENGLISH):
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
    # 🆕 زر "عام" العمومي يُخفى لمادة الإنجليزي تحديداً لأن "🎯 اختبار عام" (general_test)
    # من القائمة الجاهزة أعلاه يؤدي نفس الغرض بالضبط - عرض الاثنين معاً كان يبدو
    # كخيارين متطابقين مربكين بصرياً. باقي المواد (رياضيات/أخرى) لا تملك خياراً مكافئاً
    # بنفس المعنى ضمن قوائمها، فيبقى الزر العمومي ضرورياً لها.
    if subject_type != SUBJECT_ENGLISH:
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


def get_translation_choice_keyboard() -> types.InlineKeyboardMarkup:
    """🆕 لوحة اختيار نمط الأسئلة عند اكتشاف محتوى إنجليزي: مترجمة للعربية أو إنجليزية فقط."""
    kb = [
        [types.InlineKeyboardButton(text=BTN_TRANSLATE_YES, callback_data="translate_choice_yes")],
        [types.InlineKeyboardButton(text=BTN_TRANSLATE_NO, callback_data="translate_choice_no")],
        [types.InlineKeyboardButton(text=BTN_CANCEL_REQUEST, callback_data="cancel_upload_request")],
    ]
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

def get_multiple_quizzes_keyboard(
    all_quizzes: list, filtered_quizzes: list, items: int, is_album: bool, show_generate_btn: bool = True,
    filter_type: str = "all", filter_difficulty: str = "all",
) -> types.InlineKeyboardMarkup:
    """
    توليد أزرار ذكية لعرض الكويزات المتوفرة للملف الواحد مع إحصائيات تقييم مجتمع الطلاب.

    🆕 all_quizzes: كل الكويزات المخزّنة (تُستخدم فقط لاستخراج القيم المتاحة للفلترة).
    🆕 filtered_quizzes: الكويزات بعد تطبيق الفلتر الحالي (هي فقط اللي تُعرض كأزرار تشغيل).
    🆕 صفوف الفلترة (نوع/صعوبة) تُعرض فقط لو فيه أكثر من قيمة واحدة فعلياً موجودة بين
    الكويزات المخزّنة - لتفادي ازدحام الأزرار لما تكون كلها بنفس التركيبة.

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
            distinct_types.append((qt, q.get("question_type_label") or "🔀 متنوع"))
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
        type_label = q.get("question_type_label") or "🔀 متنوع"
        diff_label = DIFFICULTY_LABELS_AR.get(q.get("difficulty") or "medium", "🟡 متوسط")
        # 🩹 سعر هذا الكويز تحديداً (وليس سعراً عاماً موحّداً) بناءً على عدد أسئلته الفعلي
        quiz_cost = calculate_cached_points_cost(items, q_count, is_album)
        btn_text = f"{icon} {type_label} | {diff_label} | {q_count} سؤال ({quiz_cost:.2f}💎) | 👍{likes} 👎{dislikes}"
        kb.append([types.InlineKeyboardButton(text=btn_text, callback_data=f"use_multi_{q['id']}")])
    
    if show_generate_btn:
        kb.append([types.InlineKeyboardButton(text="🆕 توليد كويز جديد كلياً (تكلفة كاملة)", callback_data="cache_action_no")])
    else:
        kb.append([types.InlineKeyboardButton(text="🔒 تم استنفاد الحد الأقصى لتنوع هذا الملف", callback_data="ignored")])
        
    kb.append([types.InlineKeyboardButton(text="❌ إلغاء الطلب والتراجع", callback_data="cancel_upload_request")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_rating_keyboard(file_quiz_id: str, quiz_id: str = None) -> types.InlineKeyboardMarkup:
    """لوحة نتيجة الاختبار الكاملة + صف تقييم الكويز المركزي مضاف فوقها.
    🆕 صف التقييم مدمج (لايك/دِسلايك جنب بعض) وزر الملاحظة تحته مباشرة،
    فتصير 6 صفوف إجمالاً بدل 9 - أخف بصرياً وأسهل مسحاً بالعين."""
    base_kb = get_quiz_result_keyboard(quiz_id=quiz_id if quiz_id is not None else file_quiz_id)
    kb = list(base_kb.inline_keyboard)
    kb.insert(0, [
        types.InlineKeyboardButton(text="👍 عجبتني الأسئلة", callback_data=f"rate_like_{file_quiz_id}"),
        types.InlineKeyboardButton(text="👎 فيها خلل", callback_data=f"rate_dislike_{file_quiz_id}")
    ])
    kb.insert(1, [types.InlineKeyboardButton(text="✍️ ملاحظة أو شكوى", callback_data=f"rate_feedback_{file_quiz_id}")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

# ==================== لوحات إدارة المفضلة والأقسام ====================

def get_favorites_list_keyboard(favorites: list, current_page: int = 1, page_size: int = 5, sort_mode: str = "latest", search_query: str = "") -> types.InlineKeyboardMarkup:
    kb = []
    
    sort_latest_label = "✅ الأحدث" if sort_mode == "latest" else "⬇️ حسب الأحدث"
    sort_section_label = "✅ حسب القسم" if sort_mode == "section" else "📁 حسب القسم"
    
    kb.append([
        types.InlineKeyboardButton(text="🔍 بحث", callback_data="favorites_search"),
        types.InlineKeyboardButton(text=sort_latest_label, callback_data="favorites_sort_latest"),
        types.InlineKeyboardButton(text=sort_section_label, callback_data="favorites_sort_section"),
    ])
    kb.append([types.InlineKeyboardButton(text="📁 تصفح الأقسام الأكاديمية", callback_data="sections_menu")])

    if search_query:
        kb.append([
            types.InlineKeyboardButton(text=f"🔎 نتيجة البحث: {search_query}", callback_data="ignored"),
            types.InlineKeyboardButton(text="🧹 مسح البحث", callback_data="favorites_clear_search")
        ])

    start_idx = (current_page - 1) * page_size
    end_idx = start_idx + page_size
    page_items = favorites[start_idx:end_idx]

    for item in page_items:
        title = item.get("title") or item.get("source_title") or "كويز محفوظ"
        favorite_id = item.get("id") or item.get("favorite_id") or item.get("created_at")
        section_title = item.get("section_title") or "عام"
        
        label = f"📚 {title}"
        if section_title and sort_mode != "section":
            label = f"📚 {title} • {section_title}"
            
        kb.append([types.InlineKeyboardButton(text=label, callback_data=f"fav_details_{favorite_id}")])

    pagination_row = []
    if current_page > 1:
        pagination_row.append(types.InlineKeyboardButton(text="⬅️ السابق", callback_data=f"fav_page_{current_page-1}"))
    if end_idx < len(favorites):
        pagination_row.append(types.InlineKeyboardButton(text="التالي ➡️", callback_data=f"fav_page_{current_page+1}"))
    if pagination_row:
        kb.append(pagination_row)

    kb.append([types.InlineKeyboardButton(text="🏠 العودة للقائمة الرئيسية", callback_data="favorites_back")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_favorite_details_keyboard(favorite_id: str, section_id: str = None) -> types.InlineKeyboardMarkup:
    """تعتمد زر رجوع ذكي يعيد الطالب لنفس القسم الدراسي بدلاً من تشتيته"""
    back_target = f"fav_sec_view_{section_id}" if section_id else "favorites_menu"
    kb = [
        [types.InlineKeyboardButton(text="▶️ بدء الاختبار الآن", callback_data=f"fav_open_{favorite_id}")],
        [types.InlineKeyboardButton(text="📁 تحميل الكويز (Word/PDF)", callback_data=f"fav_export_menu_{favorite_id}")],
        [types.InlineKeyboardButton(text="🗑️ حذف الكويز", callback_data=f"fav_del_{favorite_id}")],
        [
            types.InlineKeyboardButton(text="🔙 رجوع", callback_data=back_target),
            types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="favorites_back")
        ]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_sections_list_keyboard(sections: list) -> types.InlineKeyboardMarkup:
    kb = []
    for section in sections:
        section_id = section.get("section_id")
        title = section.get("title") or "قسم عام"
        kb.append([types.InlineKeyboardButton(text=f"📁 {title}", callback_data=f"fav_sec_view_{section_id}")])
        
    kb.append([types.InlineKeyboardButton(text="⭐ عرض كل الكويزات المحفوظة", callback_data="favorites_menu")])
    kb.append([types.InlineKeyboardButton(text="🏠 العودة للقائمة الرئيسية", callback_data="favorites_back")])
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
    kb.append([types.InlineKeyboardButton(text="التالي ➡️", callback_data="next_question")])
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
    """لوحة تحكم الإدارة الرئيسية محدثة مع زر الإرسال الجماعي واستعراض الطلاب والكويزات والنشطين"""
    kb = [
        [types.InlineKeyboardButton(text="📢 إرسال رسالة جماعية", callback_data="admin_broadcast_prompt")],
        [types.InlineKeyboardButton(text="🔍 البحث عن مستخدم", callback_data="admin_search_prompt")],
        [types.InlineKeyboardButton(text="👥 استعراض الطلاب (مصفّح)", callback_data="admin_users_page_1")],
        [
            types.InlineKeyboardButton(text="⚡ الطلاب النشطون اليوم", callback_data="admin_analytics_today"),
            types.InlineKeyboardButton(text="🎯 كويزات اليوم", callback_data="admin_today_quizzes_p_1")
        ],
        [
            types.InlineKeyboardButton(text="📊 الإحصائيات", callback_data="admin_stats"),
            types.InlineKeyboardButton(text="📥 تصدير CSV", callback_data="admin_export_users")
        ],
        [types.InlineKeyboardButton(text="📈 تحليلات الاستخدام", callback_data="admin_analytics_7")],
        [types.InlineKeyboardButton(text="📋 تصفح ملاحظات الكويزات", callback_data="admin_view_feedbacks")],
        [types.InlineKeyboardButton(text="❌ إغلاق القائمة", callback_data="admin_cancel")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_user_actions_keyboard(user_id: int) -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="💰 شحن رصيد الطالب", callback_data=f"admin_charge_menu_{user_id}")],
        [
            types.InlineKeyboardButton(text="📈 نشاط هذا الطالب", callback_data=f"admin_user_activity_{user_id}"),
            types.InlineKeyboardButton(text="🎯 كويزات هذا الطالب", callback_data=f"admin_user_quizzes_{user_id}_p_1")
        ],
        [types.InlineKeyboardButton(text="⚙️ لوحة التحكم", callback_data="admin_main_menu")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_charge_options_keyboard(user_id: int) -> types.InlineKeyboardMarkup:
    kb = [
        [
            types.InlineKeyboardButton(text="➕ 10 نقاط", callback_data=f"admin_charge_quick_10_{user_id}"),
            types.InlineKeyboardButton(text="➕ 50 نقطة", callback_data=f"admin_charge_quick_50_{user_id}"),
            types.InlineKeyboardButton(text="➕ 100 نقطة", callback_data=f"admin_charge_quick_100_{user_id}")
        ],
        [types.InlineKeyboardButton(text="✍️ إدخال كمية يدوياً", callback_data=f"admin_charge_manual_{user_id}")],
        [types.InlineKeyboardButton(text="⚙️ لوحة التحكم", callback_data="admin_main_menu")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_cancel_keyboard() -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="⚙️ لوحة التحكم", callback_data="admin_main_menu")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_analytics_keyboard(days: int) -> types.InlineKeyboardMarkup:
    period_row = []
    for d, label in [(7, "7 أيام"), (30, "30 يوم"), (90, "90 يوم")]:
        text = f"✅ {label}" if d == days else label
        period_row.append(types.InlineKeyboardButton(text=text, callback_data=f"admin_analytics_{d}"))
    
    kb = [
        period_row,
        [types.InlineKeyboardButton(text="⚡ النشطون اليوم حصراً", callback_data="admin_analytics_today")],
        [types.InlineKeyboardButton(text="📅 النشاط اليومي (آخر 14 يوم)", callback_data="admin_analytics_daily")],
        [types.InlineKeyboardButton(text="📥 تصدير سجل الأحداث CSV", callback_data="admin_export_events")],
        [types.InlineKeyboardButton(text="⚙️ لوحة التحكم", callback_data="admin_main_menu")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)