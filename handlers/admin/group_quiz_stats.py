# handlers/admin/group_quiz_stats.py
"""
==============================================================================
MODULE: شاشة إحصائيات الكويز الجماعي بلوحة الأدمن
==============================================================================
- نظرة عامة: أعداد الجلسات حسب الحالة، آخر 7/30 يوم، حجم البيانات المخزّنة.
- 🟢 الكويزات النشطة الآن (waiting + active) بالغروبات/القنوات.
- 🕐 سجل الجلسات (صفحات) + تفاصيل كل جلسة: الأدمن المشغّل (اسم/يوزر/ID)،
  الغروب/القناة (اسم/نوع/ID + يوزر وعدد الأعضاء لايف من تيليجرام)، الكويز المستخدم،
  التوقيت الدقيق (إنشاء/بدء/انتهاء بالثواني)، وبيانات المشاركين المسجّلة.
- 🧹 تفريغ يدوي (بتأكيد) للجلسات المنتهية/الملغاة مع مشاركيها وإجاباتها.

مبنية بنفس نمط handlers/admin/analytics.py (safe_edit_text، IsAdminFilter +
فلتر chat.type الخاص). الاستعلامات والحذف كلها بـ services/group_quiz_store.py -
هالملف عرض/تنسيق بس، بلا لمس مباشر لـ supabase.
"""

import asyncio
import html
import uuid

from aiogram import Router, types, F

from config import bot
from constants import format_syria_time
from keyboards import (
    get_group_quiz_stats_keyboard,
    get_group_quiz_sessions_keyboard,
    get_group_quiz_session_detail_keyboard,
    get_group_quiz_purge_confirm_keyboard,
)
from logger import get_logger
from services.group_quiz_store import (
    get_group_quiz_overview_stats,
    get_group_sessions_page,
    get_group_session_details,
    count_ended_group_sessions,
    purge_ended_group_sessions,
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
SESSIONS_PAGE_SIZE = 8
EXACT_TIME_FMT = "%Y-%m-%d %I:%M:%S %p"  # توقيت سوريا بالثواني
PACING_LABELS = {
    "fixed_interval": "فاصل ثابت",
    "chain_to_timer": "سؤال بعد انتهاء مؤقت السابق",
    "burst": "دفعة واحدة",
}
MAX_MESSAGE_CHARS = 3900  # حد تيليجرام 4096 - هامش أمان
MAX_PARTICIPANTS_SHOWN = 15
LIVE_CHAT_TIMEOUT_SECONDS = 5


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


def _user_label(user: dict | None, user_id=None) -> str:
    """اسم + @يوزر + ID لمستخدم (من جدول users أو participants). الـ ID رابط
    `tg://user?id=` قابل للضغط. القيمة الافتراضية 'Unknown' (بتنكتب بـ check_or_add_user
    لما ما بكون في يوزر) ما بتنعرض كأنها يوزر حقيقي."""
    user = user or {}
    uid = user.get("user_id", user_id)
    name = " ".join(
        p for p in (user.get("first_name"), user.get("last_name")) if p and p != "Unknown"
    ).strip()
    username = user.get("username")
    parts = [html.escape(name) if name else "بلا اسم"]
    if username and username != "Unknown":
        parts.append(f"@{html.escape(username)}")
    label = " · ".join(parts)
    if uid is not None:
        label += f' · <a href="tg://user?id={uid}"><code>{uid}</code></a>'
    return label


def _session_list_line(index: int, s: dict) -> str:
    status_label = STATUS_LABELS.get(s.get("status"), s.get("status", "؟"))
    chat_type_label = CHAT_TYPE_LABELS.get(s.get("_chat_type"), "؟")
    starter = s.get("_starter") or {}
    starter_name = starter.get("first_name") or starter.get("username") or str(s.get("started_by"))
    return (
        f"<b>{index}.</b> {chat_type_label} <b>{html.escape(s['_chat_title'])}</b>\n"
        f"┃  📚 {html.escape(s['_quiz_title'])} — {status_label}\n"
        f"┃  🧑‍🏫 {html.escape(str(starter_name))} · 👤 {s['_participant_count']} مشارك\n"
        f"┃  🕐 {format_syria_time(s.get('created_at'), EXACT_TIME_FMT)}"
    )


async def _render_sessions_list(call: types.CallbackQuery, mode: str, page: int) -> None:
    """يعرض قائمة جلسات: `mode='a'` النشطة الآن (waiting+active)، `'r'` السجل الكامل."""
    if mode == "a":
        data = await get_group_sessions_page(statuses=["active", "waiting"], page=0, page_size=SESSIONS_PAGE_SIZE)
        title = "🟢 <b>الكويزات النشطة الآن</b>"
        empty = "لا يوجد أي كويز جماعي نشط أو بانتظار الإعداد حالياً."
    else:
        data = await get_group_sessions_page(statuses=None, page=page, page_size=SESSIONS_PAGE_SIZE)
        title = "🕐 <b>سجل جلسات الكويز الجماعي</b>"
        empty = "لا توجد أي جلسة كويز جماعي مسجّلة."
    if data["error"]:
        await call.answer("❌ تعذر تحميل قائمة الجلسات حالياً.", show_alert=True)
        return

    sessions, total, page = data["sessions"], data["total"], data["page"]
    if not sessions:
        text = f"{title}\n\n{empty}"
        has_next = False
    else:
        pages = max(1, -(-total // SESSIONS_PAGE_SIZE))
        header = f"{title}\n<i>الإجمالي: {total}" + (f" · صفحة {page + 1}/{pages}" if mode == "r" else "") + "</i>"
        if mode == "a" and total > len(sessions):
            header += f"\n<i>(يُعرض أحدث {len(sessions)} فقط)</i>"
        body = "\n\n".join(_session_list_line(i, s) for i, s in enumerate(sessions, start=1))
        text = f"{header}\n\n{body}\n\n<i>اضغط رقم الجلسة لعرض تفاصيلها 👇</i>"
        has_next = mode == "r" and (page + 1) * SESSIONS_PAGE_SIZE < total

    await safe_edit_text(
        call.message, text,
        reply_markup=get_group_quiz_sessions_keyboard(sessions, mode, page, has_next),
    )
    await call.answer()


@router.callback_query(F.data == "admin_gq_active")
async def show_active_group_sessions(call: types.CallbackQuery):
    try:
        await _render_sessions_list(call, "a", 0)
    except Exception as e:
        logger.error(f"Error rendering active group quiz sessions: {e}")
        await call.answer("❌ تعذر تحميل الكويزات النشطة حالياً.", show_alert=True)


@router.callback_query(F.data.startswith("admin_gq_recent:"))
async def show_recent_group_sessions(call: types.CallbackQuery):
    try:
        try:
            page = int(call.data.split(":", 1)[1])
        except ValueError:
            page = 0
        await _render_sessions_list(call, "r", page)
    except Exception as e:
        logger.error(f"Error rendering recent group quiz sessions: {e}")
        await call.answer("❌ تعذر تحميل قائمة الجلسات حالياً.", show_alert=True)


@router.callback_query(F.data == "admin_group_quiz_recent")
async def show_recent_group_sessions_legacy(call: types.CallbackQuery):
    """زر قديم (رسائل لوحة أدمن مفتوحة قبل التحديث) - نفس سجل الجلسات صفحة 1."""
    try:
        await _render_sessions_list(call, "r", 0)
    except Exception as e:
        logger.error(f"Error rendering recent group quiz sessions (legacy): {e}")
        await call.answer("❌ تعذر تحميل قائمة الجلسات حالياً.", show_alert=True)


async def _fetch_live_chat_info(chat_id: int) -> dict:
    """معلومات حيّة من تيليجرام (يوزر المحادثة + عدد الأعضاء) - غير مخزّنة بقاعدة
    البيانات. فشلها (البوت انطرد/انحذفت القناة/مهلة) ما بيكسر الشاشة - بترجّع {}."""
    info: dict = {}
    try:
        chat = await asyncio.wait_for(bot.get_chat(chat_id), timeout=LIVE_CHAT_TIMEOUT_SECONDS)
        info["username"] = getattr(chat, "username", None)
        info["title"] = getattr(chat, "title", None)
        info["is_forum"] = bool(getattr(chat, "is_forum", False))
    except Exception as e:
        logger.info(f"Live chat info unavailable for {chat_id}: {e}")
        return {}
    try:
        info["members"] = await asyncio.wait_for(bot.get_chat_member_count(chat_id), timeout=LIVE_CHAT_TIMEOUT_SECONDS)
    except Exception as e:
        logger.info(f"Live member count unavailable for {chat_id}: {e}")
    return info


def _build_session_details_text(d: dict, live: dict) -> str:
    s = d["session"]
    membership = d.get("membership") or {}
    quiz = d.get("quiz") or {}
    chat_type = membership.get("chat_type")
    chat_type_label = CHAT_TYPE_LABELS.get(chat_type, "؟")
    status_label = STATUS_LABELS.get(s.get("status"), s.get("status", "؟"))
    chat_title = membership.get("chat_title") or live.get("title") or "غير معروف"

    # --- الأدمن المشغّل ---
    lines = [
        f"🔎 <b>تفاصيل جلسة كويز جماعي</b>  {status_label}\n",
        "🧑‍🏫 <b>الأدمن المشغّل</b>",
        f"┣ {_user_label(d.get('starter'), s.get('started_by'))}\n",
    ]

    # --- الغروب / القناة ---
    chat_lines = [
        "┣ " + f"{chat_type_label} <b>{html.escape(chat_title)}</b>",
        f"┣ 🆔 <code>{s['chat_id']}</code>",
    ]
    if live.get("username"):
        chat_lines.append(f"┣ 🔗 @{html.escape(live['username'])}")
    if live.get("members") is not None:
        chat_lines.append(f"┣ 👥 الأعضاء: <code>{live['members']}</code> <i>(لايف)</i>")
    if not live:
        chat_lines.append("┣ ⚠️ <i>تعذّر جلب المعلومات الحيّة (البوت خارج المحادثة أو انحذفت)</i>")
    if s.get("message_thread_id") is not None:
        chat_lines.append(f"┣ 🧵 توبيك: <code>{s['message_thread_id']}</code>")
    if membership:
        chat_lines.append(f"┣ 🤖 حالة البوت فيها: <code>{html.escape(str(membership.get('status')))}</code>")
        if membership.get("added_by") is not None:
            chat_lines.append(f"┣ ➕ رقّى البوت: {_user_label(d.get('added_by_user'), membership['added_by'])}")
    lines += ["📍 <b>المحادثة</b>", *chat_lines, ""]

    # --- الكويز ---
    quiz_title = quiz.get("source_title") or "كويز"
    lines += [
        "📚 <b>الكويز المستخدم</b>",
        f"┣ {html.escape(quiz_title)}",
        f"┣ 🆔 <code>{s.get('quiz_id')}</code>",
    ]
    if quiz.get("creator_id") is not None:
        lines.append(f"┣ ✍️ منشئ الكويز: <code>{quiz['creator_id']}</code>")
    lines.append(f"┣ ❓ الأسئلة: <code>{s.get('questions_sent', 0)}/{s.get('total_questions', 0)}</code> مُرسَل\n")

    # --- التوقيت ---
    lines += [
        "⏱ <b>التوقيت (توقيت سوريا)</b>",
        f"┣ إنشاء الجلسة: {format_syria_time(s.get('created_at'), EXACT_TIME_FMT)}",
        f"┣ بدء الأسئلة: {format_syria_time(s.get('started_at'), EXACT_TIME_FMT) if s.get('started_at') else '—'}",
        f"┣ الانتهاء/الإلغاء: {format_syria_time(s.get('finished_at'), EXACT_TIME_FMT) if s.get('finished_at') else '—'}\n",
    ]

    # --- الإعدادات ---
    pacing = PACING_LABELS.get(s.get("pacing_mode"), s.get("pacing_mode") or "—")
    settings_line = f"┣ النمط: {pacing}"
    if s.get("question_interval_seconds"):
        settings_line += f" · فاصل {s['question_interval_seconds']}ث"
    if s.get("question_timer_seconds"):
        settings_line += f" · مؤقت {s['question_timer_seconds']}ث"
    lines += [
        "⚙️ <b>الإعدادات</b>",
        settings_line,
        f"┣ إخفاء هوية المصوّتين (استفتاء مجهول): {'نعم' if s.get('poll_is_anonymous') else 'لا'}",
        f"┣ عرض الأسماء بالنتيجة: {'نعم' if s.get('show_names') else 'لا'}\n",
    ]

    # --- المشاركون ---
    participants = d.get("participants") or []
    header = f"👤 <b>المشاركون المسجّلون</b> ({len(participants)}) · إجابات: <code>{d.get('total_answers', 0)}</code>"
    lines.append(header)
    if not participants:
        lines.append("┣ <i>لا توجد بيانات مشاركين (الوضع المجهول بالكامل أو لم يجاوب أحد).</i>")
    else:
        text_so_far = "\n".join(lines)
        shown = 0
        for rank, p in enumerate(participants[:MAX_PARTICIPANTS_SHOWN], start=1):
            answers = p.get("_answers_count", 0)
            avg = (p.get("total_answer_time_ms", 0) / answers / 1000) if answers else 0
            entry = (
                f"┣ <b>{rank}.</b> {_user_label(p)}\n"
                f"┃   🏆 {p.get('score', 0)} · ✅ {p.get('correct_count', 0)}/{answers} · ⏳ متوسط {avg:.1f}ث\n"
                f"┃   🕐 أول مشاركة: {format_syria_time(p.get('joined_at'), EXACT_TIME_FMT)}"
            )
            if len(text_so_far) + len(entry) + 80 > MAX_MESSAGE_CHARS:
                break
            lines.append(entry)
            text_so_far += "\n" + entry
            shown += 1
        if len(participants) > shown:
            lines.append(f"┗ <i>… و{len(participants) - shown} مشارك إضافي غير معروض (ضيق مساحة الرسالة)</i>")

    lines.append(f"\n🆔 الجلسة: <code>{s['id']}</code>")
    return "\n".join(lines)


@router.callback_query(F.data.startswith("admin_gq_s:"))
async def show_group_session_details(call: types.CallbackQuery):
    try:
        parts = call.data.split(":")
        if len(parts) != 3 or parts[2][:1] not in ("a", "r"):
            await call.answer("❌ طلب غير صالح.", show_alert=True)
            return
        try:
            session_id = str(uuid.UUID(parts[1]))
            mode, page = parts[2][0], int(parts[2][1:] or 0)
        except ValueError:
            await call.answer("❌ طلب غير صالح.", show_alert=True)
            return

        details = await get_group_session_details(session_id)
        if not details:
            await call.answer("⚠️ الجلسة غير موجودة (ربما انحذفت بالتفريغ).", show_alert=True)
            return

        live = await _fetch_live_chat_info(int(details["session"]["chat_id"]))
        text = _build_session_details_text(details, live)
        await safe_edit_text(
            call.message, text,
            reply_markup=get_group_quiz_session_detail_keyboard(session_id, mode, page),
        )
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering group quiz session details: {e}")
        await call.answer("❌ تعذر تحميل تفاصيل الجلسة حالياً.", show_alert=True)


@router.callback_query(F.data == "admin_gq_purge")
async def confirm_purge_ended_sessions(call: types.CallbackQuery):
    try:
        counts = await count_ended_group_sessions()
        if counts.get("error"):
            await call.answer("❌ تعذر قراءة أعداد الجلسات حالياً.", show_alert=True)
            return
        total = counts["finished"] + counts["cancelled"]
        if total == 0:
            await call.answer("لا توجد جلسات منتهية أو ملغاة لتفريغها.", show_alert=True)
            return
        text = (
            "🧹 <b>تفريغ بيانات الجلسات المنتهية والملغاة</b>\n\n"
            f"🏁 منتهية: <code>{counts['finished']}</code>\n"
            f"❌ ملغاة: <code>{counts['cancelled']}</code>\n\n"
            "⚠️ سيتم حذف هالجلسات <b>نهائياً</b> مع كل مشاركيها وإجاباتهم (بغض النظر عن عمرها) - "
            "ولا يمكن التراجع، وتفاصيلها بسجل الجلسات ستختفي.\n"
            "✅ الجلسات النشطة وبانتظار الإعداد <b>لا تُمَس إطلاقاً</b>."
        )
        await safe_edit_text(call.message, text, reply_markup=get_group_quiz_purge_confirm_keyboard())
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering purge confirmation: {e}")
        await call.answer("❌ تعذر تحضير شاشة التفريغ حالياً.", show_alert=True)


@router.callback_query(F.data == "admin_gq_purge_yes")
async def purge_ended_sessions(call: types.CallbackQuery):
    try:
        await call.answer("⏳ جاري الحذف...")
        result = await purge_ended_group_sessions()
        if result["error"]:
            text = (
                "⚠️ <b>توقّف التفريغ بنص الطريق بسبب خطأ</b>\n\n"
                f"المحذوف قبل الخطأ: جلسات <code>{result['sessions']}</code> · "
                f"مشاركون <code>{result['participants']}</code> · إجابات <code>{result['answers']}</code>\n"
                "جرّب الضغط مرة تانية لإكمال الباقي (راجع اللوج للسبب)."
            )
        else:
            text = (
                "✅ <b>تم تفريغ الجلسات المنتهية والملغاة</b>\n\n"
                f"🗂 جلسات محذوفة: <code>{result['sessions']}</code>\n"
                f"👤 سطور مشاركين: <code>{result['participants']}</code>\n"
                f"🧮 إجابات: <code>{result['answers']}</code>"
            )
        await safe_edit_text(call.message, text, reply_markup=get_group_quiz_stats_keyboard())
    except Exception as e:
        logger.error(f"Error purging ended group quiz sessions: {e}")
        await safe_edit_text(
            call.message, "❌ حصل خطأ أثناء التفريغ - راجع اللوج.", reply_markup=get_group_quiz_stats_keyboard()
        )
