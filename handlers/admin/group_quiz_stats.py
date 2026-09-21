# handlers/admin/group_quiz_stats.py
"""
==============================================================================
MODULE: شاشة إحصائيات الكويز الجماعي بلوحة الأدمن
==============================================================================
شاشة قراءة فقط: نظرة عامة (أعداد الجلسات حسب الحالة، جلسات آخر 7/30 يوم، حجم
البيانات المتراكمة بـ group_quiz_participants/answers) + قائمة آخر 10 جلسات.

مبنية بنفس نمط handlers/admin/analytics.py (safe_edit_text، IsAdminFilter +
فلتر chat.type الخاص). الاستعلامات نفسها بـ services/group_quiz_store.py -
هالملف عرض/تنسيق بس، بلا لمس مباشر لـ supabase.
"""

from aiogram import Router, types, F

from constants import format_syria_time
from keyboards import get_group_quiz_stats_keyboard
from logger import get_logger
from services.group_quiz_store import (
    get_group_quiz_overview_stats,
    get_recent_group_sessions,
    GROUP_SESSION_RETENTION_DAYS,
)
from .dashboard import IsAdminFilter
from .admin_utils import safe_edit_text

logger = get_logger(__name__)
router = Router()

# 🔒 حماية أمنية للراوتر - نفس نمط كل ملفات handlers/admin/ التانية
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

STATUS_LABELS = {
    "waiting": "⏳ بانتظار الإعداد",
    "active": "🟢 نشطة الآن",
    "finished": "🏁 منتهية",
    "cancelled": "❌ ملغاة",
}
CHAT_TYPE_LABELS = {
    "group": "👥 غروب",
    "supergroup": "👥 غروب",
    "channel": "📢 قناة",
}
RECENT_SESSIONS_LIMIT = 10


@router.callback_query(F.data == "admin_group_quiz_stats")
async def show_group_quiz_overview(call: types.CallbackQuery):
    try:
        stats = await get_group_quiz_overview_stats()
        if stats.get("error"):
            await call.answer("❌ تعذر تحميل الإحصائيات حالياً.", show_alert=True)
            return

        counts = stats.get("counts_by_status", {})
        status_lines = "\n".join(
            f"┣ {STATUS_LABELS.get(s, s)}: <code>{counts.get(s, 0)}</code>"
            for s in ("waiting", "active", "finished", "cancelled")
        )

        text = (
            "📊 <b>إحصائيات الكويز الجماعي (غروبات + قنوات)</b>\n\n"
            f"🗂 <b>إجمالي الجلسات:</b> <code>{stats.get('total_sessions', 0)}</code>\n"
            f"{status_lines}\n\n"
            f"📅 آخر 7 أيام: <code>{stats.get('sessions_last_7_days', 0)}</code> جلسة\n"
            f"📅 آخر 30 يوم: <code>{stats.get('sessions_last_30_days', 0)}</code> جلسة\n\n"
            f"👥 محادثات مميّزة شغّلت جلسة: <code>{stats.get('distinct_chats_used', 0)}</code>\n"
            f"📢 قنوات جاهزة للنشر حالياً: <code>{stats.get('postable_channels_now', 0)}</code>\n\n"
            f"🧮 سطور مشاركة مخزّنة: <code>{stats.get('total_participation_rows', 0)}</code>\n"
            f"🧮 إجابات مخزّنة: <code>{stats.get('total_answers', 0)}</code>\n"
            f"<i>(بيانات الجلسات المنتهية/الملغاة بتُحذف تلقائياً بعد "
            f"{GROUP_SESSION_RETENTION_DAYS} يوم - راجع سياسة التنظيف)</i>"
        )
        await safe_edit_text(call.message, text, reply_markup=get_group_quiz_stats_keyboard())
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering group quiz overview stats: {e}")
        await call.answer("❌ تعذر تحميل الإحصائيات حالياً.", show_alert=True)


@router.callback_query(F.data == "admin_group_quiz_recent")
async def show_recent_group_sessions(call: types.CallbackQuery):
    try:
        sessions = await get_recent_group_sessions(limit=RECENT_SESSIONS_LIMIT)
        if not sessions:
            text = "🕐 <b>آخر الجلسات</b>\n\nلا توجد أي جلسة كويز جماعي مسجّلة بعد."
        else:
            lines = [f"🕐 <b>آخر {len(sessions)} جلسة</b>\n"]
            for s in sessions:
                status_label = STATUS_LABELS.get(s.get("status"), s.get("status", "؟"))
                chat_type_label = CHAT_TYPE_LABELS.get(s.get("_chat_type"), "؟")
                lines.append(
                    f"┣ {chat_type_label} <b>{s['_chat_title']}</b>\n"
                    f"┃  📚 {s['_quiz_title']} — {status_label}\n"
                    f"┃  👤 {s['_participant_count']} مشارك · 🕐 {format_syria_time(s.get('created_at'))}"
                )
            text = "\n".join(lines)

        await safe_edit_text(call.message, text, reply_markup=get_group_quiz_stats_keyboard())
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering recent group quiz sessions: {e}")
        await call.answer("❌ تعذر تحميل قائمة الجلسات حالياً.", show_alert=True)
