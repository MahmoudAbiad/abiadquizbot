# handlers/admin/group_quiz_stats.py
"""
==============================================================================
MODULE: شاشة إحصائيات الكويز الجماعي بلوحة الأدمن
==============================================================================
- نظرة عامة: أعداد الجلسات حسب الحالة، آخر 7/30 يوم، حجم البيانات المخزّنة.
- 🟢 الكويزات النشطة الآن (waiting + active) بالغروبات/القنوات.
- 🕐 سجل الجلسات (صفحات) + تفاصيل كل جلسة: الأدمن المشغّل (اسم/يوزر/ID)،
  الغروب/القناة (اسم/نوع/ID + يوزر وعدد الأعضاء لايف من تيليجرام)، الكويز المستخدم،
  التوقيت الدقيق (إنشاء/بدء/انتهاء بالثواني)، وأعداد المشاركين والإجابات.
- 👤 زر "المشاركون" بالتفاصيل: شاشة منفصلة (بصفحات) فيها المشاركون المسجّلون بالجلسة بس.
- ▶️ زر "شغّل الكويز عندي": تشغيل كويز الجلسة للأدمن بخاصه.
- 🧹 تفريغ يدوي (بتأكيد) للجلسات المنتهية/الملغاة مع مشاركيها وإجاباتها.

مبنية بنفس نمط handlers/admin/analytics.py (safe_edit_text، IsAdminFilter +
فلتر chat.type الخاص). الاستعلامات والحذف كلها بـ services/group_quiz_store.py -
هالملف عرض/تنسيق بس، بلا لمس مباشر لـ supabase.
"""

import asyncio
import html
import uuid

from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext

from config import bot
from helpers.supabase_helper import admin_get_quiz_by_id
from handlers.quiz_runner import start_loaded_quiz, ACTIVE_QUIZ_STATES
from constants import format_syria_time
from keyboards import (
    get_group_quiz_stats_keyboard,
    get_group_quiz_sessions_keyboard,
    get_group_quiz_session_detail_keyboard,
    get_group_quiz_participants_keyboard,
    get_group_quiz_purge_confirm_keyboard,
)
from logger import get_logger
from services.group_quiz_store import (
    get_group_quiz_overview_stats,
    get_group_sessions_page,
    get_group_session_details,
    get_group_session_participants,
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
PARTICIPANTS_PAGE_SIZE = 8
MAX_NAME_CHARS = 40  # قصّ الأسماء الطويلة بالعرض فقط - حتى تضل الرسالة تحت حد تيليجرام
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
    parts = [html.escape(name[:MAX_NAME_CHARS]) if name else "بلا اسم"]
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
        data = await get_group_sessions_page(statuses=["active", "waiting"], page=page, page_size=SESSIONS_PAGE_SIZE)
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
        header = f"{title}\n<i>الإجمالي: {total} · صفحة {page + 1}/{pages}</i>"
        body = "\n\n".join(_session_list_line(i, s) for i, s in enumerate(sessions, start=1))
        text = f"{header}\n\n{body}\n\n<i>اضغط رقم الجلسة لعرض تفاصيلها 👇</i>"
        has_next = (page + 1) * SESSIONS_PAGE_SIZE < total

    await safe_edit_text(
        call.message, text,
        reply_markup=get_group_quiz_sessions_keyboard(sessions, mode, page, has_next),
    )
    await call.answer()


@router.callback_query(F.data.startswith("admin_gq_active"))
async def show_active_group_sessions(call: types.CallbackQuery):
    try:
        try:
            page = int(call.data.split(":", 1)[1]) if ":" in call.data else 0
        except ValueError:
            page = 0
        await _render_sessions_list(call, "a", page)
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

    # --- المشاركون (القائمة الكاملة بشاشة منفصلة عبر زر "المشاركون") ---
    total_participants = d.get("participants_total", 0)
    lines.append(f"👤 <b>المشاركون المسجّلون:</b> <code>{total_participants}</code> · إجابات: <code>{d.get('total_answers', 0)}</code>")
    if not total_participants:
        lines.append("┣ <i>لا توجد بيانات مشاركين (الوضع المجهول بالكامل أو لم يجاوب أحد).</i>")

    lines.append(f"\n🆔 الجلسة: <code>{s['id']}</code>")
    return "\n".join(lines)


def _parse_session_callback(data: str, extra_int: bool = False):
    """`<prefix>:<uuid>:<mode><page>[:<n>]` -> (session_id, mode, page, n) أو None لو غير صالح.
    `n` بس لشاشة المشاركين (رقم الصفحة)، والتفاصيل بتتجاهل أي جزء رابع (رسائل قديمة)."""
    parts = data.split(":")
    if len(parts) not in (3, 4) or parts[2][:1] not in ("a", "r"):
        return None
    try:
        session_id = str(uuid.UUID(parts[1]))
        mode, page = parts[2][0], int(parts[2][1:] or 0)
        n = int(parts[3]) if (extra_int and len(parts) == 4) else 0
    except ValueError:
        return None
    return session_id, mode, page, n


@router.callback_query(F.data.startswith("admin_gq_s:"))
async def show_group_session_details(call: types.CallbackQuery):
    try:
        parsed = _parse_session_callback(call.data)
        if not parsed:
            await call.answer("❌ طلب غير صالح.", show_alert=True)
            return
        session_id, mode, page, _ = parsed

        details = await get_group_session_details(session_id)
        if not details:
            await call.answer("⚠️ الجلسة غير موجودة (ربما انحذفت بالتفريغ).", show_alert=True)
            return

        live = await _fetch_live_chat_info(int(details["session"]["chat_id"]))
        await safe_edit_text(
            call.message, _build_session_details_text(details, live),
            reply_markup=get_group_quiz_session_detail_keyboard(
                session_id, mode, page,
                participants_total=details["participants_total"],
                quiz_id=str(details["quiz"]["id"]) if details.get("quiz") else None,
            ),
        )
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering group quiz session details: {e}")
        await call.answer("❌ تعذر تحميل تفاصيل الجلسة حالياً.", show_alert=True)


def _build_participants_text(data: dict) -> str:
    s, participants = data["session"], data["participants"]
    total, page = data["total"], data["page"]
    pages = max(1, -(-total // PARTICIPANTS_PAGE_SIZE))
    lines = [
        f"👤 <b>مشاركو الجلسة</b> ({total})" + (f" · صفحة {page + 1}/{pages}" if total > PARTICIPANTS_PAGE_SIZE else ""),
        f"📚 {html.escape(s['_quiz_title'])} · {CHAT_TYPE_LABELS.get(s.get('_chat_type'), '؟')} {html.escape(s['_chat_title'])}\n",
    ]
    text_so_far = "\n".join(lines)
    for i, p in enumerate(participants):
        rank = page * PARTICIPANTS_PAGE_SIZE + i + 1
        answers = p.get("_answers_count", 0)
        avg = (p.get("total_answer_time_ms", 0) / answers / 1000) if answers else 0
        entry = (
            f"<b>{rank}.</b> {_user_label(p)}\n"
            f"┃  🏆 {p.get('score', 0)} · ✅ {p.get('correct_count', 0)}/{answers} · ⏳ متوسط {avg:.1f}ث\n"
            f"┃  🕐 أول مشاركة: {format_syria_time(p.get('joined_at'), EXACT_TIME_FMT)}"
        )
        if len(text_so_far) + len(entry) + 80 > MAX_MESSAGE_CHARS:
            lines.append("<i>… باقي مشاركي هالصفحة غير معروضين (ضيق مساحة الرسالة)</i>")
            break
        lines.append(entry)
        text_so_far += "\n" + entry
    return "\n".join(lines)


@router.callback_query(F.data.startswith("admin_gq_p:"))
async def show_group_session_participants(call: types.CallbackQuery):
    try:
        parsed = _parse_session_callback(call.data, extra_int=True)
        if not parsed:
            await call.answer("❌ طلب غير صالح.", show_alert=True)
            return
        session_id, mode, page, ppage = parsed

        data = await get_group_session_participants(session_id, page=ppage, page_size=PARTICIPANTS_PAGE_SIZE)
        if not data:
            await call.answer("⚠️ الجلسة غير موجودة (ربما انحذفت بالتفريغ).", show_alert=True)
            return
        if not data["participants"]:
            await call.answer("لا يوجد مشاركون مسجّلون بهالجلسة.", show_alert=True)
            return

        ppage = data["page"]
        await safe_edit_text(
            call.message, _build_participants_text(data),
            reply_markup=get_group_quiz_participants_keyboard(
                session_id, mode, page, ppage,
                has_prev=ppage > 0,
                has_next=(ppage + 1) * PARTICIPANTS_PAGE_SIZE < data["total"],
            ),
        )
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering group quiz session participants: {e}")
        await call.answer("❌ تعذر تحميل قائمة المشاركين حالياً.", show_alert=True)


@router.callback_query(F.data.startswith("admin_gq_try:"))
async def try_group_session_quiz(call: types.CallbackQuery, state: FSMContext):
    """يشغّل كويز الجلسة للأدمن نفسه بخاص البوت (نفس مسار 'تجربة الكويز' بملاحظات الطلاب:
    origin=admin_test، ومحاولات الأدمن أصلاً ما بتنسجّل بجدول المحاولات)."""
    try:
        try:
            quiz_id = str(uuid.UUID(call.data.split(":", 1)[1]))
        except ValueError:
            await call.answer("❌ طلب غير صالح.", show_alert=True)
            return

        # تفادي دعس كويز الأدمن الجاري (start_loaded_quiz بيستبدل الحالة بلا سؤال)
        if await state.get_state() in {st.state for st in ACTIVE_QUIZ_STATES}:
            await call.answer("⚠️ عندك كويز شغّال حالياً - أنهِه أولاً (أو أوقفه) قبل تشغيل كويز جديد.", show_alert=True)
            return

        quiz = await admin_get_quiz_by_id(quiz_id)
        if not quiz or not quiz.get("quiz_data"):
            await call.answer("❌ تعذّر تحميل الكويز، قد يكون محذوفاً.", show_alert=True)
            return

        await call.answer("🎯 جاري تشغيل الكويز عندك...")
        await start_loaded_quiz(
            call, state, quiz["quiz_data"],
            quiz.get("source_title") or "كويز (تجربة إدارية)",
            origin="admin_test",
            quiz_id=quiz_id,
        )
    except Exception as e:
        logger.error(f"Error starting group session quiz for admin: {e}")
        await call.answer("❌ تعذر تشغيل الكويز.", show_alert=True)


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
