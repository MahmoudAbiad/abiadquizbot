from aiogram import types
from logger import get_logger

logger = get_logger(__name__)

# ==================== لوحات التحكم والملاحة العامة ====================

def get_main_menu_keyboard(bot_username: str, user_id: int) -> types.InlineKeyboardMarkup:
    try:
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        kb = [
            [types.InlineKeyboardButton(text="💰 شحن الرصيد (نقاط إضافية)", callback_data="recharge_info")],
            [types.InlineKeyboardButton(text="⭐ قائمتي المفضلة المنظمة", callback_data="favorites_menu")],
            [types.InlineKeyboardButton(text="🔗 شارك واربح نقاط مجانية", switch_inline_query=f"\nاشترك في بوت الكويزات الرهيب عبر رابطي واربح نقاطاً: {ref_link}")]
        ]
        return types.InlineKeyboardMarkup(inline_keyboard=kb)
    except Exception as e:
        logger.error(f"Error generating main menu keyboard: {e}")
        return types.InlineKeyboardMarkup(inline_keyboard=[])

def get_quiz_result_keyboard(quiz_id: str = None, is_score_public: bool = False) -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="🔄 إعادة الاختبار", callback_data="quiz_replay")],
        [types.InlineKeyboardButton(text="🔗 مشاركة هذا الكويز", callback_data="quiz_share")],
        [types.InlineKeyboardButton(text="⭐ حفظ في المفضلة", callback_data="quiz_favorite")]
    ]
    
    if quiz_id:
        if not is_score_public:
            kb.append([types.InlineKeyboardButton(text="📢 مشاركة نتيجتي في لوحة الشرف", callback_data=f"publish_score_{quiz_id}")])
        kb.append([types.InlineKeyboardButton(text="🏆 عرض لوحة الشرف (Top 5)", callback_data=f"leaderboard_{quiz_id}")])
        
    kb.append([types.InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="quiz_home")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_quiz_start_keyboard() -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="🚀 ابدأ الاختبار الآن", callback_data="start_first_question")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_quiz_exit_confirmation_keyboard() -> types.InlineKeyboardMarkup:
    """لوحة حارسة تمنع خروج الطالب بالخطأ أثناء حل الأسئلة"""
    kb = [
        [
            types.InlineKeyboardButton(text="⏹️ نعم، إيقاف وخروج", callback_data="quiz_stop_confirmed"),
            types.InlineKeyboardButton(text="🔄 لا، إكمال الحل", callback_data="quiz_resume_flow")
        ]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_cancel_upload_keyboard() -> types.InlineKeyboardMarkup:
    """زر التراجع النظيف لإلغاء طلبات معالجة الملفات أو النصوص المباشرة المعلقة"""
    kb = [
        [types.InlineKeyboardButton(text="❌ إلغاء الطلب", callback_data="cancel_upload_request")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

# ==================== لوحات إدارة المخازن المتعددة والتقييمات ====================

def get_multiple_quizzes_keyboard(quizzes: list, cost: float, show_generate_btn: bool = True) -> types.InlineKeyboardMarkup:
    """توليد أزرار ذكية لعرض كافة الكويزات المتوفرة للملف الواحد مع إحصائيات تقييم مجتمع الطلاب"""
    kb = []
    for idx, q in enumerate(quizzes, 1):
        likes = q.get('likes', 0)
        dislikes = q.get('dislikes', 0)
        btn_text = f"📝 كويز {idx} الجاهز | 👍 {likes} | 👎 {dislikes}"
        kb.append([types.InlineKeyboardButton(text=btn_text, callback_data=f"use_multi_{q['id']}")])
    
    if show_generate_btn:
        kb.append([types.InlineKeyboardButton(text="🆕 توليد كويز جديد كلياً (تكلفة كاملة)", callback_data="cache_action_no")])
    else:
        # إشعار شفاف في حال قفل التوليد للوصول للحد الأقصى للملف بناءً على حجمه
        kb.append([types.InlineKeyboardButton(text="🔒 تم استنفاد الحد الأقصى لتنوع هذا الملف", callback_data="ignored")])
        
    kb.append([types.InlineKeyboardButton(text="❌ إلغاء الطلب والتراجع", callback_data="cancel_upload_request")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_rating_keyboard(file_quiz_id: str) -> types.InlineKeyboardMarkup:
    """لوحة التقييم التفاعلية الفورية التي تظهر للطالب بمجرد إنهاء الكويز المركزي لفرز المحتوى"""
    kb = [
        [
            types.InlineKeyboardButton(text="👍 راق لي الأسئلة", callback_data=f"rate_like_{file_quiz_id}"),
            types.InlineKeyboardButton(text="👎 سيئ / يحتوي تكرار", callback_data=f"rate_dislike_{file_quiz_id}")
        ],
        [types.InlineKeyboardButton(text="✍️ إرسال ملاحظة أو شكوى أكاديمية", callback_data=f"rate_feedback_{file_quiz_id}")],
        [types.InlineKeyboardButton(text="🏠 العودة للقائمة الرئيسية", callback_data="quiz_home")]
    ]
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
        types.InlineKeyboardButton(text="⏹️ إيقاف", callback_data="quiz_stop"),
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
        types.InlineKeyboardButton(text="⏹️ إيقاف الكويز", callback_data="quiz_stop"),
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
    kb = [
        [types.InlineKeyboardButton(text="🔍 بحث عن مستخدم", callback_data="admin_search_prompt")],
        [
            types.InlineKeyboardButton(text="📊 الإحصائيات", callback_data="admin_stats"),
            types.InlineKeyboardButton(text="📥 تصدير الطلاب", callback_data="admin_export_users")
        ],
        [types.InlineKeyboardButton(text="💬 مراجعة الملاحظات والتقييمات", callback_data="admin_view_feedbacks")],
        [types.InlineKeyboardButton(text="❌ إغلاق القائمة", callback_data="admin_cancel")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_user_actions_keyboard(user_id: int) -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="💰 شحن رصيد الطالب", callback_data=f"admin_charge_menu_{user_id}")],
        [types.InlineKeyboardButton(text="🔙 رجوع للوحة", callback_data="admin_cancel")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_charge_options_keyboard(user_id: int) -> types.InlineKeyboardMarkup:
    kb = [
        [
            types.InlineKeyboardButton(text="➕ 50 نقطة", callback_data=f"admin_charge_quick_50_{user_id}"),
            types.InlineKeyboardButton(text="➕ 100 نقطة", callback_data=f"admin_charge_quick_100_{user_id}")
        ],
        [
            types.InlineKeyboardButton(text="➕ 500 نقطة", callback_data=f"admin_charge_quick_500_{user_id}")
        ],
        [types.InlineKeyboardButton(text="✍️ إدخال كمية يدوياً", callback_data=f"admin_charge_manual_{user_id}")],
        [types.InlineKeyboardButton(text="🔙 إلغاء والرجوع", callback_data="admin_cancel")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_cancel_keyboard() -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_cancel")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)