import asyncio
import io
import csv
import json
from aiogram import Router, types, F
from aiogram.types import BufferedInputFile
from aiogram.exceptions import TelegramBadRequest

from supabase_helper import (
    admin_get_usage_overview,
    admin_get_daily_active_users,
    admin_get_today_active_users,
    admin_get_today_quizzes,
    admin_get_user_activity,
    admin_get_all_usage_events
)
from keyboards import get_analytics_keyboard, get_admin_dashboard_keyboard
from logger import get_logger
from .dashboard import IsAdminFilter

logger = get_logger(__name__)
router = Router()

# 🔒 حماية أمنية للراوتر
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

QUIZZES_PAGE_SIZE = 4      # عدد الكويزات المعروضة في الصفحة الواحدة
TODAY_USERS_PAGE_SIZE = 5  # عدد الطلاب النشطين المعروضين في الصفحة الواحدة

EVENT_LABELS = {
    "bot_start": "▶️ تشغيل البوت",
    "content_uploaded": "📤 رفع محتوى/ملف",
    "quiz_generation_requested": "🧮 طلب توليد كويز",
    "quiz_generated": "🆕 توليد كويز جديد",
    "cached_quiz_used": "♻️ استخدام كويز مخزن",
    "quiz_started": "🚀 بدء كويز",
    "quiz_completed": "🏁 إكمال كويز",
    "quiz_stopped": "⏹ إيقاف كويز مبكراً",
    "quiz_shared": "🔗 مشاركة كويز",
    "share_link_created": "🔗 إنشاء رابط مشاركة",
    "shared_link_opened": "📬 فتح رابط مشترك",
    "quiz_saved_favorite": "⭐ حفظ بالمفضلة",
    "quiz_rated": "👍👎 تقييم كويز",
    "feedback_submitted": "✍️ إرسال ملاحظة",
    "score_published": "🏆 نشر نتيجة",
    "leaderboard_viewed": "📋 عرض لوحة الشرف",
}

SOURCE_LABELS = {
    "file": "📄 ملف", "photo": "🖼 صورة", "album": "🖼🖼 ألبوم", "text": "📝 نص مباشر",
    "shared": "🔗 مشترك", "favorite": "⭐ مفضلة", "cached_file": "♻️ كاش", "admin_test": "🛠 تجربة إدارية",
}

def _format_seconds(total_seconds: float) -> str:
    total_seconds = int(total_seconds or 0)
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes} د {seconds} ث" if minutes else f"{seconds} ث"

def sanitize_csv_value(val) -> str:
    val_str = str(val) if val is not None else ""
    return f"'{val_str}" if val_str.startswith(('=', '+', '-', '@')) else val_str

async def safe_edit_text(message: types.Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest:
        pass

# ⚡ 1. معالج عرض الطلاب النشطين خلال الـ 24 ساعة الأخيرة مصفحاً (بتوقيت سوريا)
@router.callback_query(F.data.startswith("admin_analytics_today"))
async def show_today_active_users_handler(call: types.CallbackQuery):
    try:
        page = 1
        if "_p_" in call.data:
            page = int(call.data.split("_p_")[1])

        active_users = await admin_get_today_active_users()
        if not active_users:
            await call.answer("📭 لا يوجد أي نشاط للطلاب خلال الـ 24 ساعة الأخيرة.", show_alert=True)
            return

        total = len(active_users)
        total_pages = max(1, -(-total // TODAY_USERS_PAGE_SIZE))
        page = max(1, min(page, total_pages))

        start_idx = (page - 1) * TODAY_USERS_PAGE_SIZE
        end_idx = start_idx + TODAY_USERS_PAGE_SIZE
        page_users = active_users[start_idx:end_idx]

        report_lines = []
        for idx, u in enumerate(page_users, start=start_idx + 1):
            username_str = f"@{u['username']}" if u.get("username") and u['username'] != "Unknown" else "بدون يوزر"
            name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or "بدون اسم"
            event_desc = EVENT_LABELS.get(u['last_event'], u['last_event'])
            
            report_lines.append(
                f"<b>{idx}. {name}</b> ({username_str})\n"
                f" └ 🆔 <code>{u['user_id']}</code>\n"
                f" └ 🕒 <code>{u['time_str']}</code>\n"
                f" └ 📝 آخر نشاط: <b>{event_desc}</b>\n"
            )

        kb = []
        nav_row = []
        if page > 1:
            nav_row.append(types.InlineKeyboardButton(text="◀️ السابق", callback_data=f"admin_analytics_today_p_{page - 1}"))
        if page < total_pages:
            nav_row.append(types.InlineKeyboardButton(text="التالي ▶️", callback_data=f"admin_analytics_today_p_{page + 1}"))
        if nav_row:
            kb.append(nav_row)

        kb.append([types.InlineKeyboardButton(text="📊 رجوع للتحليلات", callback_data="admin_analytics_7")])
        kb.append([types.InlineKeyboardButton(text="⚙️ لوحة التحكم الرئيسية", callback_data="admin_main_menu")])

        text = (
            f"⚡ <b>الطلاب النشطون (خلال الـ 24 ساعة الماضية - توقيت سوريا)</b>\n"
            f"📄 الصفحة {page} من أصل {total_pages} (الإجمالي: <code>{total}</code> طالب)\n"
            f"───────────────────\n\n" +
            "\n".join(report_lines)
        )
        await safe_edit_text(call.message, text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb))
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering 24h active users: {e}")
        await call.answer("❌ تعذر جلب قائمة النشطين.", show_alert=True)

# 🎯 2. معالج عرض الكويزات التي تم توليدها اليوم حصراً مع خيار التجربة والتصفح
@router.callback_query(F.data.startswith("admin_today_quizzes_p_"))
async def show_today_quizzes_handler(call: types.CallbackQuery):
    try:
        page = int(call.data.replace("admin_today_quizzes_p_", "", 1))
        quizzes = await admin_get_today_quizzes()
        
        if not quizzes:
            await call.answer("📭 لم يتم توليد أي كويزات جديدة اليوم حتى الآن.", show_alert=True)
            return

        total = len(quizzes)
        total_pages = max(1, -(-total // QUIZZES_PAGE_SIZE))
        page = max(1, min(page, total_pages))

        start_idx = (page - 1) * QUIZZES_PAGE_SIZE
        end_idx = start_idx + QUIZZES_PAGE_SIZE
        page_quizzes = quizzes[start_idx:end_idx]

        report_lines = []
        kb = []

        for idx, q in enumerate(page_quizzes, start=start_idx + 1):
            quiz_id = q["id"]
            title = q.get("source_title") or "كويز بدون عنوان"
            time_str = str(q.get("created_at", ""))[:16].replace("T", " ")
            
            student = q.get("users") or {}
            user_id = q.get("creator_id") or student.get("user_id", "غير معروف")
            username = f"@{student['username']}" if student.get("username") and student['username'] != "Unknown" else "بدون يوزر"
            name = f"{student.get('first_name', '')} {student.get('last_name', '')}".strip() or "بدون اسم"

            report_lines.append(
                f"<b>{idx}. {title}</b>\n"
                f" ┣ 👤 الطالب: <b>{name}</b> ({username})\n"
                f" ┣ 🆔 الآيدي: <code>{user_id}</code>\n"
                f" ┗ 🕒 الوقت: <code>{time_str}</code>\n"
                f"───────────────────"
            )

            kb.append([types.InlineKeyboardButton(
                text=f"🎯 تجربة #{idx}: {title[:25]}",
                callback_data=f"afb_try_{quiz_id}"
            )])

        nav_row = []
        if page > 1:
            nav_row.append(types.InlineKeyboardButton(text="◀️ السابق", callback_data=f"admin_today_quizzes_p_{page - 1}"))
        if page < total_pages:
            nav_row.append(types.InlineKeyboardButton(text="التالي ▶️", callback_data=f"admin_today_quizzes_p_{page + 1}"))
        if nav_row:
            kb.append(nav_row)

        kb.append([types.InlineKeyboardButton(text="⚙️ لوحة التحكم الرئيسية", callback_data="admin_main_menu")])

        text = (
            f"🎯 <b>الكويزات المُولدة اليوم (الصفحة {page}/{total_pages})</b>\n"
            f"👥 الإجمالي: <code>{total}</code> كويز\n"
            f"───────────────────\n\n" +
            "\n".join(report_lines)
        )

        await safe_edit_text(call.message, text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb))
        await call.answer()

    except Exception as e:
        logger.error(f"Error rendering today quizzes: {e}")
        await call.answer("❌ تعذر جلب قائمة كويزات اليوم.", show_alert=True)

# 📈 3. معالج تحليلات الاستخدام (7، 30، 90 يوماً)
@router.callback_query(F.data.regexp(r"^admin_analytics_(7|30|90)$"))
async def show_usage_analytics(call: types.CallbackQuery):
    days = int(call.data.replace("admin_analytics_", ""))
    try:
        overview = await admin_get_usage_overview(days=days)
        top_events = sorted(overview["event_counts"].items(), key=lambda x: x[1], reverse=True)[:8]
        events_lines = "\n".join(f"┣ {EVENT_LABELS.get(ev, ev)}: <code>{count}</code>" for ev, count in top_events) or "┣ لا توجد أحداث."
        source_lines = "\n".join(f"┣ {SOURCE_LABELS.get(src, src)}: <code>{count}</code>" for src, count in sorted(overview["source_breakdown"].items(), key=lambda x: x[1], reverse=True)) or "┣ لا توجد بيانات."

        text = (
            f"📈 <b>تحليلات الاستخدام — آخر {days} يوم</b>\n\n"
            f"👥 مستخدمون نشطون: <code>{overview['active_users']}</code>\n"
            f"🎯 محاولات كويز: <code>{overview['total_attempts']}</code> (مكتمل: <code>{overview['completed_attempts']}</code>)\n"
            f"✅ معدل الإكمال: <code>{overview['completion_rate']:.1f}%</code>\n"
            f"⏱ متوسط مدة الحل: <code>{_format_seconds(overview['avg_duration_seconds'])}</code>\n"
            f"🎓 متوسط النتائج: <code>{overview['avg_score_percentage']:.1f}%</code>\n\n"
            f"📊 <b>الأحداث الأكثر تكراراً:</b>\n{events_lines}\n\n"
            f"🗂 <b>مصدر الكويزات:</b>\n{source_lines}"
        )
        await safe_edit_text(call.message, text, reply_markup=get_analytics_keyboard(days))
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering analytics: {e}")
        await call.answer("❌ تعذر تحميل بيانات التحليلات.", show_alert=True)

# 📅 4. معالج النشاط اليومي (آخر 14 يوم)
@router.callback_query(F.data == "admin_analytics_daily")
async def show_daily_active_users(call: types.CallbackQuery):
    try:
        daily = await admin_get_daily_active_users(days=14)
        if not daily:
            await call.answer("📭 لا توجد بيانات كافية.", show_alert=True)
            return
        max_active = max(d["active_users"] for d in daily) or 1
        lines = [f"<code>{row['day']}</code> {'█' * max(1, round((row['active_users'] / max_active) * 12))} {row['active_users']}" for row in daily]
        text = "📅 <b>المستخدمون النشطون يومياً (آخر 14 يوم):</b>\n\n" + "\n".join(lines)
        await safe_edit_text(call.message, text, reply_markup=get_analytics_keyboard(7))
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering daily active: {e}")
        await call.answer("❌ تعذر تحميل النشاط اليومي.", show_alert=True)

# 📥 5. معالج تصدير الأحداث كـ CSV
@router.callback_query(F.data == "admin_export_events")
async def export_usage_events(call: types.CallbackQuery):
    await safe_edit_text(call.message, "⏳ جاري استخراج سجل الأحداث CSV...")
    try:
        events = await admin_get_all_usage_events(limit=5000)
        if not events:
            return await safe_edit_text(call.message, "📭 لا توجد أحداث لتصديرها.", reply_markup=get_analytics_keyboard(7))

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["User ID", "Event Type", "Metadata", "Created At"])
        for e in events:
            meta = json.dumps(e.get("metadata", {}), ensure_ascii=False) if isinstance(e.get("metadata"), dict) else str(e.get("metadata", ""))
            writer.writerow([
                sanitize_csv_value(e.get("user_id", "")),
                sanitize_csv_value(e.get("event_type", "")),
                sanitize_csv_value(meta),
                sanitize_csv_value(e.get("created_at", "")),
            ])

        file = BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename="usage_events.csv")
        try: 
            await call.message.delete()
        except TelegramBadRequest: 
            pass
        await call.message.answer_document(document=file, caption="📥 <b>تم استخراج ملف سجل الأحداث!</b>", reply_markup=get_analytics_keyboard(7), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error exporting events: {e}")
        await safe_edit_text(call.message, "❌ حدث خطأ أثناء استخراج الملف.", reply_markup=get_analytics_keyboard(7))

# 📈 6. معالج نشاط طالب محدد
@router.callback_query(F.data.startswith("admin_user_activity_"))
async def show_user_activity(call: types.CallbackQuery):
    try:
        target_id = int(call.data.replace("admin_user_activity_", ""))
        activity = await admin_get_user_activity(target_id)

        events_lines = "\n".join(
            f"┣ {EVENT_LABELS.get(e['event_type'], e['event_type'])} — <code>{str(e['created_at'])[:16].replace('T', ' ')}</code>"
            for e in activity["recent_events"][:10]
        ) or "┣ لا يوجد نشاط مسجل بعد."

        attempts_lines = "\n".join(
            f"┣ {SOURCE_LABELS.get(a.get('source_type'), a.get('source_type'))} — "
            f"{'✅' if a.get('is_completed') else '⏹'} {a.get('score', 0)}/{a.get('total_questions', 0)}"
            for a in activity["recent_attempts"]
        ) or "┣ لم يخض أي كويز بعد."

        text = (
            f"📈 <b>نشاط الطالب</b> <code>{target_id}</code>\n\n"
            f"🎯 إجمالي المحاولات: <code>{activity['total_attempts']}</code> "
            f"(مكتملة: <code>{activity['completed_attempts']}</code>)\n"
            f"🎓 متوسط النتائج: <code>{activity['avg_score_percentage']:.1f}%</code>\n\n"
            f"📝 <b>آخر المحاولات:</b>\n{attempts_lines}\n\n"
            f"🕒 <b>آخر الأحداث:</b>\n{events_lines}"
        )
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔙 رجوع لبيانات الطالب", callback_data="admin_main_menu")]
        ])
        await safe_edit_text(call.message, text, reply_markup=kb)
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering user activity: {e}")
        await call.answer("❌ تعذر تحميل نشاط هذا الطالب.", show_alert=True)