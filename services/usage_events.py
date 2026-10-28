"""Stable analytics taxonomy. Callback events measure intent, not successful outcomes."""
import re

EVENT_LABELS = {
    "bot_start": "▶️ تشغيل البوت",
    "content_uploaded": "📤 رفع محتوى/ملف",
    "quiz_generation_requested": "🧮 طلب توليد كويز",
    "quiz_generated": "🆕 توليد كويز جديد",
    "cached_quiz_used": "♻️ استخدام كويز مخزن",
    "quiz_started": "🚀 بدء كويز",
    "quiz_completed": "🏁 إكمال كويز",
    "quiz_stopped": "⏹ إيقاف كويز مبكراً",
    "quiz_question_edited": "✏️ تعديل سؤال أثناء الكويز",
    "quiz_shared": "🔗 مشاركة كويز (حدث قديم)",
    "share_link_created": "🔗 إنشاء رابط مشاركة",
    "shared_link_opened": "📬 فتح رابط مشترك",
    "quiz_saved_favorite": "⭐ حفظ بالمفضلة",
    "quiz_rated": "👍👎 تقييم كويز",
    "feedback_submitted": "✍️ إرسال ملاحظة",
    "score_published": "🏆 نشر نتيجة",
    "leaderboard_viewed": "📋 عرض لوحة الشرف",
    "error_occurred": "🐞 خطأ واجهه الطالب",
    # 🆕 كانت هذه الأحداث تُسجَّل فعلياً بقاعدة البيانات من قبل، لكن بدون تسمية عربية هنا
    "score_hidden": "🙈 إخفاء نتيجة منشورة",
    "audio_uploaded": "🎙 رفع محاضرة صوتية",
    "audio_transcription_confirmed": "🎙 تأكيد تفريغ الصوت",
    "audio_transcription_completed": "🎙 اكتمال تفريغ الصوت",
    "audio_transcription_truncated": "🎙 تفريغ صوت مقطوع (طويل)",
    "tutorial_opened": "📘 فتح الشرح التعريفي",
    "tutorial_completed": "📘 إنهاء الشرح التعريفي",
    # The interactive tutorial is active again; retain historical event IDs.
    "start_cta_acknowledged": "🚀 تأكيد بدء الاستخدام (زر أول ملف)",
    "recharge_info_viewed": "💳 عرض معلومات الشحن",
    "support_opened": "🆘 فتح الدعم الفني",
    "channel_opened": "📢 فتح القناة",
    "request_cancelled": "🚫 إلغاء طلب أثناء العملية",
    # 🆕 أحداث جديدة أضيفت بهذا التحديث
    "quiz_exported": "📄 تصدير كويز (Word/PDF/Excel)",
    "favorite_opened": "⭐ فتح كويز من المفضلة",
    "favorite_deleted": "🗑 حذف كويز من المفضلة",
    "joined_via_referral": "🎯 انضمام عبر رابط دعوة",
}

# No active emitter remains for this legacy name. Never delete historical records.
LEGACY_EVENTS = {"quiz_shared"}

# Only uncovered actions: existing semantic events must not be emitted a second time.
CALLBACK_EVENTS = {
    "student_help": ("help_clicked", "❓ ضغط كيف أستخدم البوت"),
    "student_create_quiz": ("create_quiz_clicked", "📝 ضغط إنشاء اختبار"),
    "student_home": ("home_clicked", "🏠 ضغط الرئيسية"),
    "gift:wallet": ("wallet_clicked", "💰 ضغط رصيدي وهداياي"),
    "gift:redeem": ("gift_code_clicked", "🎟 ضغط إدخال كود هدية"),
    "gift:cancel": ("gift_cancel_clicked", "🎟 ضغط إلغاء إدخال الكود"),
    "quiz_generate_new": ("new_questions_clicked", "✨ ضغط توليد أسئلة جديدة من المحتوى"),
    "quiz_generate_similar_math": ("similar_math_clicked", "🧩 ضغط مسائل رياضيات مشابهة"),
    "quiz_extract_as_is": ("extract_as_is_clicked", "📋 ضغط استخراج الاختبار كما هو"),
    "quiz_extract_ai_solve": ("extract_solve_clicked", "🧠 ضغط حل الاختبار بالذكاء الاصطناعي"),
    "favorites_menu": ("favorites_clicked", "⭐ ضغط اختباراتي المحفوظة"),
    "favorites_search": ("favorites_search_clicked", "🔎 ضغط البحث في المحفوظات"),
    "export_menu": ("export_menu_clicked", "📄 ضغط خيارات التصدير"),
    "tut_close": ("tutorial_close_clicked", "📘 ضغط إغلاق الدليل"),
    "audio_act_summarize": ("audio_summary_clicked", "✨ ضغط تلخيص المحاضرة"),
    "audio_act_quiz": ("audio_quiz_clicked", "🎯 ضغط اختبار من المحاضرة"),
    "audio_act_export": ("audio_export_clicked", "📄 ضغط تصدير المحاضرة"),
    "audio_act_send_text": ("audio_text_clicked", "📋 ضغط عرض نص المحاضرة"),
}
# Capture only action categories; never store callback tokens, gift codes or payloads.
CALLBACK_PATTERNS = [
    (r"gift:list:[0-9]{1,8}", "gifts_clicked", "🎁 ضغط عرض الهدايا"),
    (r"qpractice:[a-f0-9]{16}", "mistake_practice_clicked", "🎯 ضغط التدريب على الأخطاء"),
    (r"qrevstart:[a-f0-9]{16}", "error_review_clicked", "📚 ضغط مراجعة الأخطاء"),
    (r"qrev:[a-f0-9]{16}:[0-9]{1,4}", "error_review_clicked", "📚 ضغط مراجعة الأخطاء"),
    (r"qrevpage:[a-f0-9]{16}:[0-9]{1,4}", "review_page_clicked", "📚 تنقل بين أسئلة المراجعة"),
    (r"qrevexp:[a-f0-9]{16}:[0-9]{1,4}", "review_explanation_clicked", "💡 ضغط إظهار شرح الخطأ"),
    (r"qrevback:[a-f0-9]{16}:[0-9]{1,4}", "review_explanation_hide_clicked", "🙈 ضغط إخفاء شرح الخطأ"),
    (r"qrev_close:[a-f0-9]{16}", "review_close_clicked", "📚 ضغط إغلاق المراجعة"),
    (r"upload_open:(?:all|audio|document|images)", "upload_page_clicked", "📤 ضغط فتح صفحة الرفع"),
    (r"cg:home:[0-9]+", "land_clicked", "🏕️ ضغط أرضي والتحديات"),
    (r"cg:b:[0-9]+:[a-f0-9]{32}", "land_attack_clicked", "⚔️ ضغط بدء هجوم"),
    (r"cg:z:[a-f0-9]{32}", "land_leave_clicked", "🏳️ ضغط تأكيد الانسحاب من هجوم"),
    (r"cg:h:(?:a|d):[0-9]+", "land_history_clicked", "📜 ضغط سجل الهجمات"),
    (r"cg:s:[0-9]+", "land_stats_clicked", "📊 ضغط إحصاءات الأرض"),
    (r"cg:n:[01]", "land_notifications_clicked", "🔔 ضغط تغيير إشعارات الأرض"),
    (r"lv:m(?::[0-9]+)?", "live_menu_clicked", "⚔️ ضغط التحديات المباشرة"),
    (r"lv:j:[a-f0-9]{32}", "live_search_clicked", "🔎 ضغط البحث عن خصم"),
    (r"lv:c(?::[A-Za-z0-9_-]+)?", "live_cancel_clicked", "✖️ ضغط إلغاء البحث عن خصم"),
    (r"lv:z:[a-f0-9]{32}", "live_leave_clicked", "🏳️ ضغط الانسحاب من مواجهة"),
    (r"sp:o:[a-f0-9]{32}", "spectator_open_clicked", "👁 ضغط مشاهدة هجوم"),
    (r"sp:x:[a-f0-9]{32}", "spectator_close_clicked", "👁 ضغط إيقاف المشاهدة"),
]
EVENT_LABELS.update({event: label for event, label in CALLBACK_EVENTS.values()})
EVENT_LABELS.update({event: label for _, event, label in CALLBACK_PATTERNS})
EVENT_LABELS.update({
    "audio_summary_completed": "✨ اكتمال تلخيص المحاضرة",
    "payment_order_created": "💳 إنشاء طلب شحن",
    "payment_credited": "💳 اعتماد شحن الرصيد",
    "mistake_practice_started": "🎯 بدء تدريب الأخطاء",
    "mistake_practice_completed": "🏁 إكمال تدريب الأخطاء",
    "mistake_practice_stopped": "⏹ إيقاف تدريب الأخطاء",
    "gift_code_redeemed": "🎁 نجاح شحن كود هدية",
    "gift_code_rejected": "🚫 رفض كود هدية",
    "insufficient_balance_blocked": "🚫 الرصيد غير كافٍ",
})


def callback_event(data):
    if not isinstance(data, str):
        return None
    if data in CALLBACK_EVENTS:
        return CALLBACK_EVENTS[data][0]
    for pattern, event, _ in CALLBACK_PATTERNS:
        if re.fullmatch(pattern, data):
            return event
    return None


def event_page(counts, requested_page, page_size=8):
    """Deterministic paging of observed event types, including unknown/legacy names."""
    items = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    pages = max(1, (len(items) + page_size - 1) // page_size)
    page = min(max(1, int(requested_page)), pages)
    return items[(page-1)*page_size:page*page_size], page, pages, len(items)
