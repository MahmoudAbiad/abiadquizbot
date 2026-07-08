from aiogram import types
from logger import get_logger

logger = get_logger(__name__)

# ==================== User Keyboards ====================

def get_main_menu_keyboard(bot_username: str, user_id: int) -> types.InlineKeyboardMarkup:
    try:
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        kb = [
            [types.InlineKeyboardButton(text="💰 شحن الرصيد (نقاط إضافية)", callback_data="recharge_info")],
            [types.InlineKeyboardButton(text="⭐ قائمتي المفضلة", callback_data="favorites_menu")],
            [types.InlineKeyboardButton(text="🔗 شارك واربح نقاط مجانية", switch_inline_query=f"\nاشترك في بوت الكويزات الرهيب عبر رابطي واربح نقاطاً: {ref_link}")]
        ]
        return types.InlineKeyboardMarkup(inline_keyboard=kb)
    except Exception as e:
        logger.error(f"Error generating main menu keyboard: {e}")
        return types.InlineKeyboardMarkup(inline_keyboard=[])

def get_quiz_result_keyboard() -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="🔄 إعادة الاختبار", callback_data="quiz_replay")],
        [types.InlineKeyboardButton(text="🔗 مشاركة الكويز", callback_data="quiz_share")],
        [types.InlineKeyboardButton(text="⭐ حفظ في المفضلة", callback_data="quiz_favorite")],
        [types.InlineKeyboardButton(text="🏠 القائمة الرئيسية", callback_data="quiz_home")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_quiz_start_keyboard() -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="🚀 ابدأ الاختبار", callback_data="start_first_question")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

# 🆕 تم التحديث: إضافة نظام الصفحات وإزالة أزرار الحذف المكررة
def get_favorites_list_keyboard(favorites: list, current_page: int = 1, page_size: int = 5, sort_mode: str = "latest", search_query: str = "") -> types.InlineKeyboardMarkup:
    kb = []
    
    # أزرار الفلترة والبحث العلوية
    sort_latest_label = "✅ الأحدث" if sort_mode == "latest" else "⬇️ حسب الأحدث"
    sort_section_label = "✅ حسب القسم" if sort_mode == "section" else "📁 حسب القسم"
    
    kb.append([
        types.InlineKeyboardButton(text="🔍 بحث", callback_data="favorites_search"),
        types.InlineKeyboardButton(text=sort_latest_label, callback_data="favorites_sort_latest"),
        types.InlineKeyboardButton(text=sort_section_label, callback_data="favorites_sort_section"),
    ])
    kb.append([types.InlineKeyboardButton(text="📁 تصفح الأقسام", callback_data="sections_menu")])

    if search_query:
        kb.append([
            types.InlineKeyboardButton(text=f"🔎 نتيجة البحث: {search_query}", callback_data="ignored"),
            types.InlineKeyboardButton(text="🧹 مسح البحث", callback_data="favorites_clear_search")
        ])

    # منطق تقسيم الصفحات (Pagination)
    start_idx = (current_page - 1) * page_size
    end_idx = start_idx + page_size
    page_items = favorites[start_idx:end_idx]

    # إضافة أزرار الكويزات (زر واحد لكل كويز يفتح التفاصيل)
    for item in page_items:
        title = item.get("title") or "كويز محفوظ"
        favorite_id = item.get("favorite_id") or item.get("created_at")
        section_title = item.get("section_title") or "عام"
        
        label = f"📚 {title}"
        if section_title and sort_mode != "section":
            label = f"📚 {title} • {section_title}"
            
        kb.append([types.InlineKeyboardButton(text=label, callback_data=f"fav_details_{favorite_id}")])

    # أزرار التقليب بين الصفحات
    pagination_row = []
    if current_page > 1:
        pagination_row.append(types.InlineKeyboardButton(text="⬅️ السابق", callback_data=f"fav_page_{current_page-1}"))
    if end_idx < len(favorites):
        pagination_row.append(types.InlineKeyboardButton(text="التالي ➡️", callback_data=f"fav_page_{current_page+1}"))
    if pagination_row:
        kb.append(pagination_row)

    kb.append([types.InlineKeyboardButton(text="🏠 العودة للقائمة الرئيسية", callback_data="favorites_back")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

# 🆕 إضافة جديدة: لوحة تفاصيل الكويز (تظهر عند اختيار كويز من القائمة)
def get_favorite_details_keyboard(favorite_id: str) -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="▶️ بدء الاختبار الآن", callback_data=f"fav_open_{favorite_id}")],
        [types.InlineKeyboardButton(text="🗑 حذف الكويز", callback_data=f"fav_del_{favorite_id}")],
        [types.InlineKeyboardButton(text="🔙 العودة للمفضلة", callback_data="favorites_menu")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

# 🆕 تم التحديث: جعل الأقسام قابلة للنقر لفلترة الكويزات بناءً عليها
def get_sections_list_keyboard(sections: list) -> types.InlineKeyboardMarkup:
    kb = []
    for section in sections:
        section_id = section.get("section_id")
        title = section.get("title") or "قسم عام"
        # يمكن لاحقاً إضافة Handler لالتقاط fav_sec_view_ لعرض كويزات هذا القسم فقط
        kb.append([types.InlineKeyboardButton(text=f"📁 {title}", callback_data=f"fav_sec_view_{section_id}")])
        
    kb.append([types.InlineKeyboardButton(text="⭐ عرض كل الكويزات المحفوظة", callback_data="favorites_menu")])
    kb.append([types.InlineKeyboardButton(text="🏠 العودة للقائمة الرئيسية", callback_data="favorites_back")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_cache_choice_keyboard(points_cost: int) -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text=f"🎁 كويز جاهز بـ {points_cost} نقطة (خصم 80%)", callback_data="cache_accept")],
        [types.InlineKeyboardButton(text="🆕 توليد كويز جديد (تكلفة كاملة)", callback_data="cache_reject")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_quiz_question_keyboard(options: list, show_hint: bool = True) -> types.InlineKeyboardMarkup:
    kb = []
    for index, option in enumerate(options):
        kb.append([types.InlineKeyboardButton(text=option, callback_data=f"ans_{index}")])
    
    control_buttons = []
    if show_hint:
        control_buttons.append(types.InlineKeyboardButton(text="💡 تلميح", callback_data="get_hint"))
    control_buttons.append(types.InlineKeyboardButton(text="💾 حفظ الكويز", callback_data="save_quiz"))
    
    kb.append(control_buttons)
    kb.append([types.InlineKeyboardButton(text="التالي ➡️", callback_data="next_question")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_quiz_answered_keyboard(options: list, correct_opt: int, selected_opt: int) -> types.InlineKeyboardMarkup:
    kb = []
    for i, opt in enumerate(options):
        prefix = "🟢 " if i == correct_opt else "🔴 " if i == selected_opt else ""
        kb.append([types.InlineKeyboardButton(text=f"{prefix}{opt}", callback_data="ignored")])
    kb.append([types.InlineKeyboardButton(text="➡️ السؤال التالي", callback_data="next_question")])
    kb.append([types.InlineKeyboardButton(text="⏹ إيقاف الكويز", callback_data="quiz_stop")])
    kb.append([types.InlineKeyboardButton(text="⏹ مشاركة الكويز", callback_data="quiz_share")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_favorite_section_keyboard(sections: list, allow_new: bool = True, allow_default: bool = True) -> types.InlineKeyboardMarkup:
    """هذه اللوحة تستخدم عند (حفظ) كويز جديد لاختيار أين سيتم حفظه"""
    kb = []
    for section in sections:
        section_id = section.get("section_id")
        title = section.get("title") or "قسم"
        kb.append([types.InlineKeyboardButton(text=f"📁 {title}", callback_data=f"fav_section_{section_id}")])

    if allow_new:
        kb.append([types.InlineKeyboardButton(text="➕ إنشاء قسم جديد", callback_data="fav_section_new")])
    if allow_default:
        kb.append([types.InlineKeyboardButton(text="⏭ بدون قسم (حفظ في عام)", callback_data="fav_section_default")])

    kb.append([types.InlineKeyboardButton(text="🏠 العودة للقائمة الرئيسية", callback_data="favorites_back")])
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

# تم الاحتفاظ بها في حال كنت تستخدمها في مكان آخر (رغم أننا دمجنا Pagination في لوحة المفضلة مباشرة)
def get_pagination_keyboard(current_page: int, total_pages: int, query: str) -> types.InlineKeyboardMarkup:
    buttons = []
    if current_page > 1:
        buttons.append(types.InlineKeyboardButton(text="⬅️ السابق", callback_data=f"page_{query}_{current_page-1}"))
    if current_page < total_pages:
        buttons.append(types.InlineKeyboardButton(text="التالي ➡️", callback_data=f"page_{query}_{current_page+1}"))
    return types.InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None

# ==================== Admin Keyboards ====================

def get_admin_dashboard_keyboard() -> types.InlineKeyboardMarkup:
    """لوحة التحكم الرئيسية للإدارة"""
    kb = [
        [types.InlineKeyboardButton(text="🔍 بحث عن مستخدم", callback_data="admin_search_prompt")],
        [
            types.InlineKeyboardButton(text="📊 الإحصائيات", callback_data="admin_stats"),
            types.InlineKeyboardButton(text="📥 تصدير الطلاب", callback_data="admin_export_users")
        ],
        [types.InlineKeyboardButton(text="❌ إغلاق القائمة", callback_data="admin_cancel")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_user_actions_keyboard(user_id: int) -> types.InlineKeyboardMarkup:
    """أزرار التحكم بعد العثور على مستخدم"""
    kb = [
        [types.InlineKeyboardButton(text="💰 شحن رصيد", callback_data=f"admin_charge_menu_{user_id}")],
        [types.InlineKeyboardButton(text="🔙 رجوع للوحة", callback_data="admin_cancel")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_charge_options_keyboard(user_id: int) -> types.InlineKeyboardMarkup:
    """خيارات الشحن السريع واليدوي"""
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
    """زر خروج عام من أي حالة FSM"""
    kb = [
        [types.InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_cancel")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)