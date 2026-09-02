import io
import csv
import json
import re
from aiogram import Router, types, F
from aiogram.types import BufferedInputFile
from aiogram.exceptions import TelegramBadRequest

from constants import format_syria_time
from supabase_helper import (
    admin_get_usage_overview,
    admin_get_daily_active_users,
    admin_get_today_active_users,
    admin_get_today_quizzes,
    admin_get_user_activity,
    admin_get_all_usage_events,
    admin_get_recent_errors,
    admin_get_referral_leaderboard,
)
from keyboards import get_analytics_keyboard
from logger import get_logger
from .dashboard import IsAdminFilter
from .admin_utils import safe_edit_text, sanitize_csv_value

logger = get_logger(__name__)
router = Router()

# 🔒 حماية أمنية للراوتر
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())


QUIZZES_PAGE_SIZE = 4      # عدد الكويزات المعروضة في الصفحة الواحدة
TODAY_USERS_PAGE_SIZE = 5  # عدد الطلاب النشطين المعروضين في الصفحة الواحدة
REFERRERS_PAGE_SIZE = 10   # عدد "المُحيلين" المعروضين بصفحة قائمة الإحالات
REFERRED_PAGE_SIZE = 15    # عدد الأسماء المعروضة بالقائمة المنفردة لكل مُحيل
RECENT_ERRORS_PAGE_SIZE = 5    # 🆕 عدد الأخطاء المعروضة بالصفحة الواحدة بلوحة "آخر الأخطاء"
RECENT_ERRORS_FETCH_LIMIT = 100  # 🆕 السقف الأقصى للأخطاء التي تُجلب من قاعدة البيانات دفعة واحدة قبل تصفحها محلياً

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
    "quiz_shared": "🔗 مشاركة كويز",
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
    # 🆕 (2026-08-28) الدليل التفاعلي حُذف بالكامل من الكود - الحدثان أعلاه ما عادوا
    # يُسجَّلان بعد الآن، لكن أبقيناهما هنا فقط لتبقى التسمية العربية صحيحة لأي بيانات
    # تاريخية قديمة بقاعدة البيانات. البديل الجديد لحدث "بدء الاستخدام":
    "start_cta_acknowledged": "🚀 تأكيد بدء الاستخدام (زر أول ملف)",
    "recharge_info_viewed": "💳 عرض معلومات الشحن",
    "support_opened": "🆘 فتح الدعم الفني",
    "channel_opened": "📢 فتح القناة",
    "request_cancelled": "🚫 إلغاء طلب أثناء العملية",
    # 🆕 أحداث جديدة أضيفت بهذا التحديث
    "quiz_exported": "📄 تصدير كويز (Word/PDF)",
    "favorite_opened": "⭐ فتح كويز من المفضلة",
    "favorite_deleted": "🗑 حذف كويز من المفضلة",
    "joined_via_referral": "🎯 انضمام عبر رابط دعوة",
}

SOURCE_LABELS = {
    "file": "📄 ملف", "photo": "🖼 صورة", "album": "🖼🖼 ألبوم", "text": "📝 نص مباشر",
    "shared": "🔗 مشترك", "favorite": "⭐ مفضلة", "cached_file": "♻️ كاش", "admin_test": "🛠 تجربة إدارية",
}

def _format_event_label(event_type: str, metadata: dict) -> str:
    """🆕 يبني تسمية عربية للحدث، مع دعم أحداث تحتاج قيمة ديناميكية من الـ metadata
    (بدل تسمية ثابتة بـ EVENT_LABELS). حالياً يُغطّي فقط حدث "رصيد غير كافٍ"
    (insufficient_balance_blocked) الذي كان يُسجَّل فعلياً بقاعدة البيانات من قبل
    (راجع handlers/files.py و handlers/audio.py) لكنه كان يظهر هنا كنص الحدث الخام
    بدون تسمية عربية ولا عرض لعدد النقاط الناقصة (deficit).
    """
    if event_type == "insufficient_balance_blocked":
        meta = metadata or {}
        deficit = meta.get("deficit_points")
        deficit_str = f"{float(deficit):.2f}" if deficit is not None else "غير معروف"
        return f"🚫 لم تكفِ النقاط - عجز مطلوب شحنه: <code>{deficit_str}</code>"
    return EVENT_LABELS.get(event_type, event_type)


def _format_seconds(total_seconds: float) -> str:
    total_seconds = int(total_seconds or 0)
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes} د {seconds} ث" if minutes else f"{seconds} ث"

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
            await call.answer("📭 لم يتم توليد أي كويزات خلال آخر 24 ساعة.", show_alert=True)
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
            time_str = q.get("time_str") or format_syria_time(q.get("created_at", ""))
            
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

            kb.append([
                types.InlineKeyboardButton(text=f"🎯 تجربة #{idx}: {title[:20]}", callback_data=f"afb_try_{quiz_id}"),
                # 🆕 حذف نهائي مباشر من هنا - الراوتر مقيّد بـ IsAdminFilter أصلاً فالأدمن
                # مصرَّح له دائماً (راجع services/quiz_permissions.py/handlers/quiz_delete.py).
                types.InlineKeyboardButton(text="🗑 حذف", callback_data=f"qdel_{quiz_id}"),
            ])

        nav_row = []
        if page > 1:
            nav_row.append(types.InlineKeyboardButton(text="◀️ السابق", callback_data=f"admin_today_quizzes_p_{page - 1}"))
        if page < total_pages:
            nav_row.append(types.InlineKeyboardButton(text="التالي ▶️", callback_data=f"admin_today_quizzes_p_{page + 1}"))
        if nav_row:
            kb.append(nav_row)

        kb.append([types.InlineKeyboardButton(text="⚙️ لوحة التحكم الرئيسية", callback_data="admin_main_menu")])

        text = (
            f"🎯 <b>الكويزات المُولدة خلال آخر 24 ساعة (الصفحة {page}/{total_pages})</b>\n"
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
            f"🎓 متوسط النتائج: <code>{overview['avg_score_percentage']:.1f}%</code>\n"
            f"🐞 أخطاء واجهها الطلاب: <code>{overview.get('error_count', 0)}</code> "
            f"(<code>{overview.get('users_with_errors', 0)}</code> طالب متأثر)\n\n"
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

# 🐞 6. معالج آخر الأخطاء التي واجهها الطلاب (مُصفّح: 5 أخطاء بكل صفحة)
# 🆕 يقبل كلا الشكلين: "admin_recent_errors" (الدخول أول مرة = صفحة 1) و
# "admin_recent_errors_p_<page>" (التنقل بين الصفحات عبر زري السابق/التالي).
@router.callback_query(F.data == "admin_recent_errors")
@router.callback_query(F.data.startswith("admin_recent_errors_p_"))
async def show_recent_errors(call: types.CallbackQuery):
    try:
        page = 1
        if call.data.startswith("admin_recent_errors_p_"):
            try:
                page = int(call.data.replace("admin_recent_errors_p_", "", 1))
            except ValueError:
                page = 1

        # نجلب دفعة أكبر (حتى 100 خطأ من آخر 7 أيام) ثم نصفّحها محلياً بخمسة
        # بكل صفحة - بنفس نمط التصفح المحلي المستخدم بقائمة الإحالات أعلاه.
        errors = await admin_get_recent_errors(limit=RECENT_ERRORS_FETCH_LIMIT, days=7)
        if not errors:
            await call.answer("✅ لا توجد أي أخطاء مسجّلة خلال آخر 7 أيام.", show_alert=True)
            return

        total = len(errors)
        total_pages = max(1, -(-total // RECENT_ERRORS_PAGE_SIZE))
        page = max(1, min(page, total_pages))
        start = (page - 1) * RECENT_ERRORS_PAGE_SIZE
        page_items = errors[start:start + RECENT_ERRORS_PAGE_SIZE]

        report_lines = []
        for idx, err in enumerate(page_items, start=start + 1):
            meta = err.get("metadata") or {}
            user = err.get("user") or {}
            username_str = f"@{user['username']}" if user.get("username") and user['username'] != "Unknown" else "بدون يوزر"
            name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or "بدون اسم"
            msg = (meta.get("message") or "غير معروف")[:150]
            tag = "🔴 غير معالج" if meta.get("unhandled") else "🟡 معالج"

            report_lines.append(
                f"<b>{idx}. {name}</b> ({username_str}) — 🆔 <code>{err.get('user_id')}</code>\n"
                f" ┣ {tag} {('· ' + meta['error_type']) if meta.get('error_type') else ''}\n"
                f" ┣ 🕒 <code>{err.get('time_str')}</code>\n"
                f" ┗ 📝 <code>{msg}</code>\n"
            )

        # 🆕 زر "السابق" (نحو الأخطاء الأقدم) يظهر دائماً بعد الصفحة الأولى، وزر
        # "التالي" (نحو الأخطاء الأحدث) يظهر فقط بعد التنقل للخلف - أي عندما لا
        # نكون بآخر صفحة أصلاً.
        nav_row = []
        if page < total_pages:
            nav_row.append(types.InlineKeyboardButton(text="◀️ أخطاء أقدم", callback_data=f"admin_recent_errors_p_{page + 1}"))
        if page > 1:
            nav_row.append(types.InlineKeyboardButton(text="أخطاء أحدث ▶️", callback_data=f"admin_recent_errors_p_{page - 1}"))

        kb_rows = []
        if nav_row:
            kb_rows.append(nav_row)
        kb_rows.append([types.InlineKeyboardButton(text="📥 تصدير كل الأحداث (CSV)", callback_data="admin_export_events")])
        kb_rows.append([types.InlineKeyboardButton(text="📊 رجوع للتحليلات", callback_data="admin_analytics_7")])
        kb_rows.append([types.InlineKeyboardButton(text="⚙️ لوحة التحكم الرئيسية", callback_data="admin_main_menu")])

        text = (
            f"🐞 <b>آخر الأخطاء التي واجهها الطلاب (آخر 7 أيام)</b>\n"
            f"إجمالي الأخطاء: <code>{total}</code> | صفحة {page}/{total_pages}\n"
            f"───────────────────\n\n" +
            "\n".join(report_lines)
        )
        await safe_edit_text(call.message, text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb_rows))
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering recent errors: {e}")
        await call.answer("❌ تعذر جلب سجل الأخطاء.", show_alert=True)

# 🎯 6.5 معالج قائمة الإحالات (الطلاب الذين شاركوا رابط الدعوة، مرتبين حسب عدد الإحالات)
@router.callback_query(F.data.startswith("admin_referrals_"))
async def show_referral_leaderboard(call: types.CallbackQuery):
    try:
        page = int(call.data.replace("admin_referrals_", "", 1) or "1")
    except ValueError:
        page = 1

    try:
        leaderboard = await admin_get_referral_leaderboard(limit=200)
        if not leaderboard:
            await call.answer("لا يوجد أي طالب حالياً أحال أشخاصاً عبر رابط الدعوة.", show_alert=True)
            return

        total_pages = max(1, (len(leaderboard) + REFERRERS_PAGE_SIZE - 1) // REFERRERS_PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start = (page - 1) * REFERRERS_PAGE_SIZE
        page_items = leaderboard[start:start + REFERRERS_PAGE_SIZE]
        total_referred = sum(r["referral_count"] for r in leaderboard)

        lines = []
        kb_rows = []
        for idx, ref in enumerate(page_items, start=start + 1):
            username_str = f"@{ref['referrer_username']}" if ref.get("referrer_username") and ref["referrer_username"] != "Unknown" else "بدون يوزر"
            lines.append(
                f"<b>{idx}. {ref['referrer_name']}</b> ({username_str})\n"
                f" ┗ 🎯 عدد الإحالات: <code>{ref['referral_count']}</code> — 🆔 <code>{ref['referrer_id']}</code>"
            )
            kb_rows.append([types.InlineKeyboardButton(
                text=f"👁 عرض إحالات #{idx} ({ref['referral_count']})",
                callback_data=f"admin_ref_detail_{ref['referrer_id']}_1"
            )])

        nav_row = []
        if page > 1:
            nav_row.append(types.InlineKeyboardButton(text="◀️ السابق", callback_data=f"admin_referrals_{page - 1}"))
        if page < total_pages:
            nav_row.append(types.InlineKeyboardButton(text="التالي ▶️", callback_data=f"admin_referrals_{page + 1}"))
        if nav_row:
            kb_rows.append(nav_row)
        kb_rows.append([types.InlineKeyboardButton(text="📊 رجوع للتحليلات", callback_data="admin_analytics_7")])
        kb_rows.append([types.InlineKeyboardButton(text="⚙️ لوحة التحكم الرئيسية", callback_data="admin_main_menu")])

        text = (
            f"🎯 <b>قائمة الإحالات (مرتبة تنازلياً)</b>\n"
            f"عدد المُحيلين: <code>{len(leaderboard)}</code> | إجمالي المُحالين: <code>{total_referred}</code>\n"
            f"صفحة {page}/{total_pages}\n"
            f"───────────────────\n\n" +
            "\n\n".join(lines)
        )
        await safe_edit_text(call.message, text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb_rows))
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering referral leaderboard: {e}")
        await call.answer("❌ تعذر جلب قائمة الإحالات.", show_alert=True)


# 🎯 6.6 معالج القائمة المنفردة لمن انضم عن طريق مُحيل معيّن (لمنع ازدحام الواجهة الرئيسية)
@router.callback_query(F.data.regexp(r"^admin_ref_detail_(-?\d+)_(\d+)$"))
async def show_referrer_detail(call: types.CallbackQuery):
    match = re.match(r"^admin_ref_detail_(-?\d+)_(\d+)$", call.data)
    referrer_id = int(match.group(1))
    page = int(match.group(2))

    try:
        leaderboard = await admin_get_referral_leaderboard(limit=200)
        entry = next((r for r in leaderboard if r["referrer_id"] == referrer_id), None)
        if not entry:
            await call.answer("❌ لا توجد بيانات لهذا المُحيل حالياً.", show_alert=True)
            return

        referred = entry["referred_users"]
        total_pages = max(1, (len(referred) + REFERRED_PAGE_SIZE - 1) // REFERRED_PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start = (page - 1) * REFERRED_PAGE_SIZE
        page_items = referred[start:start + REFERRED_PAGE_SIZE]

        lines = []
        for idx, u in enumerate(page_items, start=start + 1):
            username_str = f"@{u['username']}" if u.get("username") and u["username"] != "Unknown" else "بدون يوزر"
            lines.append(f"{idx}. {u['name']} ({username_str}) — 🆔 <code>{u['user_id']}</code>")

        nav_row = []
        if page > 1:
            nav_row.append(types.InlineKeyboardButton(text="◀️ السابق", callback_data=f"admin_ref_detail_{referrer_id}_{page - 1}"))
        if page < total_pages:
            nav_row.append(types.InlineKeyboardButton(text="التالي ▶️", callback_data=f"admin_ref_detail_{referrer_id}_{page + 1}"))
        kb_rows = [nav_row] if nav_row else []
        kb_rows.append([types.InlineKeyboardButton(text="🎯 رجوع لقائمة الإحالات", callback_data="admin_referrals_1")])

        text = (
            f"👥 <b>الطلاب الذين انضموا عن طريق: {entry['referrer_name']}</b>\n"
            f"إجمالي إحالاته: <code>{entry['referral_count']}</code> | صفحة {page}/{total_pages}\n"
            f"───────────────────\n\n" +
            "\n".join(lines)
        )
        await safe_edit_text(call.message, text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb_rows))
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering referrer detail: {e}")
        await call.answer("❌ تعذر جلب تفاصيل هذا المُحيل.", show_alert=True)


# 📈 7. معالج نشاط طالب محدد
@router.callback_query(F.data.startswith("admin_user_activity_"))
async def show_user_activity(call: types.CallbackQuery):
    try:
        target_id = int(call.data.replace("admin_user_activity_", ""))
        activity = await admin_get_user_activity(target_id)

        events_lines = "\n".join(
            f"┣ {_format_event_label(e['event_type'], e.get('metadata'))} — <code>{format_syria_time(e['created_at'])}</code>"
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