# handlers/group_quiz.py
"""
==============================================================================
MODULE: تشغيل الكويز ضمن الغروبات
==============================================================================
يغطي:
- بدء الجلسة من داخل الغروب (`/start gq_<share_id>` أو `/groupquiz <share_id>`).
- شاشة إعداد واحدة (`gqs:`): ملخّص بالإعدادات الحالية + زر «ابدأ» + قائمة إعدادات (نمط الجلسة،
  ظهور الأسماء، عدد الأوائل، المؤقّت، إيقاع الإرسال، القفل). بديل السلسلة القديمة
  `gqa/gqn/gqk/gqt/gql/gqp` (معالجاتها ضلّت للرسائل القديمة المعلّقة فقط). القواعد بـ `_compute_setup_update`.
- `send_next_group_question()` - نقطة الإرسال الموحّدة (بدء الجلسة، النبضة
  الدورية، ومعالج إغلاق الـ poll كمسار تسريع اختياري - راجع الملاحظة تحت).
- `@router.poll_answer()` نسخة الغروب + `@router.poll()` (اختياري، انظر تحت).
- `group_quiz_heartbeat_loop()` - النبضة الدورية، الضامن الوحيد للتقدّم بكلا
  وضعي `fixed_interval` و`chain_to_timer` (مسجَّلة بـ webhook_server.py
  و main.py، لازم بالاثنين).
- زر "🏁 إنهاء الجلسة الآن" اليدوي + إنهاء تلقائي بعد آخر سؤال + نشر الترتيب.
- 📢 دعم القنوات (بند 3 بـ group_quiz_decisions.md): تيليجرام ما عندها Deep Link
  للقنوات، فالأدمن بيبلّش من **الخاص** مع البوت (زر "📢 شارك مع قناة" بـ
  handlers/sharing.py) وبيختار القناة من لائحة بتتغذّى من تحديثات `my_chat_member`
  (`track_bot_chat_membership` -> جدول `bot_chat_memberships`). شاشات الإعداد
  كلها بتشتغل بالخاص بنفس الأزرار (`gqa:/gqn:/gqt:/gqp:/gqc:`)؛ الفرق الوحيد بـ
  `_guard_config_callback` (بتتحقق من أدمن القناة **الهدف** مش من محادثة الرسالة).
  الجلسة نفسها (إرسال/نبضة/إنهاء) بلا أي تغيير - `chat_id` القناة هو نفسه بكل مكان.

⚠️ **تصحيح مهم على `chain_to_timer` (بعد تجربة فعلية):** الخطة الأصلية افترضت
إنه `@router.poll()` بيوصله تحديث لما الاستفتاء يسكّر لحاله بانتهاء
`open_period`. هاد غير صحيح - تيليجرام موثّق رسمياً إنه بيبعت تحديث `poll`
بس للإغلاق **اليدوي** (`bot.stop_poll`)، مش التلقائي. فـ `chain_to_timer`
هلق بيعتمد بالكامل على النبضة الدورية (نفس آلية `fixed_interval`، بس
بمصدر توقيت مختلف: `question_timer_seconds` بدل `question_interval_seconds`)
- راجع `send_next_group_question` و`services/group_quiz_store.py::get_due_sessions`.
معالج `@router.poll()` ضل موجود كمسار تسريع اختياري بلا ضرر (idempotent).

⚠️ ترتيب تسجيل الـ router: لازم **قبل** `start_router` (حتى نلتقط `/start gq_`
بالغروب قبل معالج البدء العام) و**قبل** `quiz_runner_router` (لأن معالج
`poll_answer` الموجود هناك بيلتقط أي إجابة استفتاء وبيوقف الانتشار).
"""

import asyncio
import datetime
import html
import json
import secrets
import time
from typing import Any, Callable, Dict, Optional, Tuple

from aiogram import F, Router, types
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandObject

from config import bot, redis_client
from keyboards import (
    get_group_channel_picker_keyboard,
    get_group_names_privacy_keyboard,
    get_group_pacing_keyboard,
    get_group_question_control_keyboard,
    get_group_timer_keyboard,
    get_group_top_n_keyboard,
    get_group_setup_cert_keyboard,
    get_group_setup_lock_keyboard,
    get_group_setup_menu_keyboard,
    get_group_setup_mode_keyboard,
    get_group_setup_names_keyboard,
    get_group_setup_pace_keyboard,
    get_group_setup_summary_keyboard,
    get_group_setup_timer_keyboard,
    get_group_setup_top_keyboard,
    GROUP_TIMER_PRESETS,
    GROUP_INTERVAL_PRESETS,
    GROUP_TOP_N_MIN,
    GROUP_TOP_N_MAX,
    GROUP_TOP_N_DEFAULT,
    SETUP_DEFAULT_TOP_N,
    SETUP_DEFAULT_INTERVAL_SECONDS,
)
from logger import get_logger, log_error, log_info
from services.group_certificate import (
    DEFAULT_TITLE as CERT_DEFAULT_TITLE,
    ISSUER_MAX_CHARS as CERT_ISSUER_MAX_CHARS,
    THEMES as CERT_THEMES,
    TITLE_MAX_CHARS as CERT_TITLE_MAX_CHARS,
    apply_config_change as cert_apply_change,
    build_payload as cert_build_payload,
    clean_text as cert_clean_text,
    display_name as cert_display_name,
    eligibility as cert_eligibility,
    format_date as cert_format_date,
    normalize_config as cert_normalize_config,
    rank_label as cert_rank_label,
    select_winners as cert_select_winners,
)
from services.group_permissions import invalidate_group_admin_cache, is_group_admin
from services.group_quiz_store import (
    cert_schema_ready as group_cert_schema_ready,
    start_waiting_session,
    activate_session_due,
    claim_question_slot,
    claim_session_cancel,
    claim_session_finish,
    clear_session_questions_cache,
    create_session,
    get_chat_membership,
    get_due_sessions,
    get_leaderboard,
    get_open_session_for_chat,
    get_session,
    get_session_questions,
    list_postable_channels,
    list_postable_groups,
    record_answer,
    bump_participant,
    update_session,
    upsert_chat_membership,
)
from services.group_quiz_lock import acquire_quiz_lock, restore_quiz_lock, recover_quiz_locks, quiz_lock_schema_ready
from services.quiz_engine import send_quiz_poll
from task_utils import spawn
from helpers.supabase_helper import check_or_add_user, get_shared_quiz

logger = get_logger(__name__)

router = Router()
# فلتر على مستوى الـ router بدل تكرار فحص نوع المحادثة بكل معالج.
# ⚠️ ما بينطبق على `poll_answer` لأن تحديث PollAnswer ما فيه كائن chat أصلاً -
# التمييز هناك بيصير عبر محتوى سجل Redis (راجع handle_group_poll_answer).
router.message.filter(F.chat.type.in_({"group", "supergroup"}))
# ⚠️ وُسّع ليشمل `private` (شاشات إعداد جلسة قناة بتنعرض بخاص الأدمن) و`channel`
# (زر "🏁 إنهاء الجلسة الآن" تحت استفتاءات القناة). آمن: كل معالجات callback بهالملف
# بتفلتر على prefix خاص (`gq*:`) فما بتلتقط أي callback تاني، وأي معالج ما بيطابق
# بينزل للـ routers اللي بعده عادي. `router.message` لسا محصور بالغروبات بس.
router.callback_query.filter(F.message.chat.type.in_({"group", "supergroup", "private", "channel"}))

# ⚠️ تصحيح (كان في تناقض هون): الحد الفعلي بين سؤالين مو بس هالرقم - النبضة
# الدورية (`group_quiz_heartbeat_loop`) نفسها بتفحص كل `HEARTBEAT_INTERVAL_SECONDS`
# ثانية بس، فحتى لو `question_interval_seconds` انضبط لقيمة أصغر (تزوير
# callback_data مثلاً)، ما في طريقة فعلية يوصل سؤالين أسرع من تكة نبضة وحدة.
# فالحد الأدنى الحقيقي = أكبر قيمة بين الاثنين، مش رقم هالثابت لحاله.
#
# سبب وجود رقم منفصل أصلاً (بدل الاكتفاء بـ HEARTBEAT_INTERVAL_SECONDS مباشرة):
# سقف تيليجرام الفعلي 20 رسالة/دقيقة لنفس الغروب (≈3 ثواني/رسالة)، وبعض الأنماط
# بترسل **رسالتين** لكل سؤال (نمط الرياضيات: صورة + poll، ومسار الـ fallback
# النصي للأسئلة الطويلة: نص + poll) - فهاد الرقم محسوب على أساس رسالتين مع هامش
# أمان مستقل عن توقيت النبضة. أي تغيير مستقبلي لـ HEARTBEAT_INTERVAL_SECONDS
# (تسريع النبضة لغرض تاني مثلاً) ما بيكسر هالضمان، لأنه الـ max() تحت بياخد
# أعلى قيمة تلقائياً.
HEARTBEAT_INTERVAL_SECONDS = 12  # 👈 نفس الرقم يُستخدم كـ default لـ group_quiz_heartbeat_loop تحت - مصدر حقيقة واحد
MIN_QUESTION_INTERVAL_SECONDS = max(8, HEARTBEAT_INTERVAL_SECONDS)

# 🚀 وضع burst ("كل الأسئلة دفعة واحدة"): فاصل ثابت بين سؤال وسؤال. سقف تيليجرام ≈20
# رسالة/دقيقة لنفس المحادثة، وسؤال الرياضيات = رسالتين (صورة + poll) -> 6 ثواني تبقينا
# تحت السقف حتى بأسوأ حالة (و`_safe_call` بيعالج TelegramRetryAfter كخط دفاع أخير).
BURST_GAP_SECONDS = 6
# `next_question_due_at` بوضع burst بتنحجز لهالمدة بعد كل سؤال: المنفّذ (`run_burst_session`)
# بيسبقها دايماً، فالنبضة ما بتتدخّل إلا لو العملية انقطعت (إعادة تشغيل) - وقتها بتعيد
# تشغيل المنفّذ ويكمّل من `questions_sent`.
BURST_RECOVERY_SECONDS = 45
_BURST_RUNNING: set = set()

# علامة "الترتيب النهائي اننشر" لكل جلسة (قيمتها وقت النشر) - بتمنع الإجابات المتأخرة من تغيير
# الترتيب المنشور وتلويث إحصائيات الأدمن. TTL = عمر سجل `poll:{id}` بـ Redis.
# ⚠️ لازم تكون معرّفة هون على مستوى الملف: غيابها كان بيرمي NameError ببلع صامت
# (try/except) فتتعطّل الحماية بلا ما ينتبه حدا.
_FINISHED_SESSION_PREFIX = "gq:finished:"
_FINISHED_SESSION_TTL = 7200

# آخر poll مفتوح لكل جلسة (chat_id + message_id) - `bot.stop_poll` بحاجة
# message_id مش poll_id، وهاد مش مخزَّن بجدول group_quiz_sessions أصلاً (ولا
# داعي لعمود دائم إله، عمره الفعلي = عمر السؤال المفتوح). TTL أطول شوي من
# أطول open_period ممكن (600 ثانية) زائد هامش.
_LAST_POLL_MSG_PREFIX = "gq:last_poll_msg:"
_LAST_POLL_MSG_TTL = 900


async def _remember_open_poll(session_id: str, chat_id: int, message_id: int) -> None:
    try:
        await redis_client.set(
            f"{_LAST_POLL_MSG_PREFIX}{session_id}",
            json.dumps({"chat_id": chat_id, "message_id": message_id}),
            ex=_LAST_POLL_MSG_TTL,
        )
    except Exception as e:
        log_error(logger, f"Redis error remembering open poll for session {session_id}: {e}")


async def _pop_open_poll(session_id: str) -> Optional[Dict[str, Any]]:
    key = f"{_LAST_POLL_MSG_PREFIX}{session_id}"
    try:
        raw = await redis_client.get(key)
        if not raw:
            return None
        await redis_client.delete(key)
        return json.loads(raw)
    except Exception as e:
        log_error(logger, f"Redis error popping open poll for session {session_id}: {e}")
        return None


def _utc_iso(seconds_from_now: int) -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=seconds_from_now)).isoformat()


async def _safe_call(coro_factory, *, what: str, on_error: Optional[Callable[[Exception], None]] = None):
    """يلفّ أي نداء إرسال لتيليجرام بمعالجة TelegramRetryAfter كخط دفاع أخير.

    `coro_factory` دالة بترجّع coroutine جديدة بكل محاولة (ما بينفع نعيد
    استخدام نفس الـ coroutine بعد ما انرمى منها استثناء).

    `on_error` (اختياري): بيتنادى بالاستثناء **النهائي** بس (بعد أي إعادة محاولة)
    قبل ما ترجع `None` - للمستدعي اللي بدو يعرف *ليش* فشل الإرسال (مثلاً
    `send_next_group_question` بيعرضه للأدمن). الافتراضي `None` = سلوك مطابق للسابق.
    """
    try:
        return await coro_factory()
    except TelegramRetryAfter as e:
        log_error(logger, f"Rate limited on {what}, sleeping {e.retry_after}s")
        await asyncio.sleep(e.retry_after + 1)
        try:
            return await coro_factory()
        except Exception as e2:
            log_error(logger, f"Retry failed on {what}: {e2}")
            if on_error:
                on_error(e2)
            return None
    except Exception as e:
        log_error(logger, f"Failed on {what}: {e}")
        if on_error:
            on_error(e)
        return None


# ==================== بدء الجلسة ====================
async def _start_session_setup(
    *,
    ui_msg: types.Message,
    chat_id: int,
    user: types.User,
    share_id: str,
    message_thread_id: Optional[int] = None,
    is_channel: bool = False,
    dest_title: Optional[str] = None,
) -> None:
    """المنطق المشترك لبدء إعداد جلسة (غروب أو قناة).

    - `chat_id`: **المحادثة الهدف** (اللي رح ينبعت فيها الكويز) - مش بالضرورة
      محادثة `ui_msg`. بمسار الغروب هي نفسها، بمسار القناة الهدف هي القناة وشاشات
      الإعداد بتنعرض بخاص الأدمن (`ui_msg` = رسالة بالخاص).
    - `user`: الأدمن اللي عم يبلّش الجلسة (منفصل عن `ui_msg.from_user` لأنه بمسار
      القناة `ui_msg` هي رسالة البوت نفسه).
    - `message_thread_id`: توبيك الغروب - دايماً `None` للقنوات (ما في توبيكس).
    - `dest_title`: اسم القناة لعرضه بشاشة الإعداد (مسار القناة بس).
    """
    if not await is_group_admin(chat_id, user.id, fresh=True):
        not_admin_txt = ("⛔ فقط أدمن القناة يقدر يشغّل كويز جماعي عليها." if is_channel
                         else "⛔ فقط أدمن الغروب يقدر يشغّل كويز جماعي هون.")
        await _safe_call(lambda: ui_msg.reply(not_admin_txt), what="reply not-admin")
        return

    existing = await get_open_session_for_chat(chat_id)
    if existing:
        exists_txt = ("⚠️ في جلسة كويز شغّالة (أو بانتظار الإعداد) بهالقناة. خلّصها أو ألغيها أولاً." if is_channel
                      else "⚠️ في جلسة كويز شغّالة (أو بانتظار الإعداد) بهالغروب. خلّصها أو ألغيها أولاً.")
        await _safe_call(lambda: ui_msg.reply(exists_txt), what="reply session-exists")
        return

    shared = await get_shared_quiz(share_id)
    questions = (shared or {}).get("quiz_data") or []
    if not shared or not questions:
        await _safe_call(
            lambda: ui_msg.reply("❌ رابط الكويز منتهي الصلاحية أو ما عاد موجوداً."),
            what="reply quiz-missing",
        )
        return

    # `group_quiz_sessions.started_by` عندها FK على `users.user_id` - لازم نضمن
    # وجود المعلّم بجدول المستخدمين حتى لو أول تفاعل إله مع البوت كان هون.
    await check_or_add_user(user.id, user.username or "Unknown", user.first_name, user.last_name or "Unknown", None)

    session = await create_session(
        quiz_id=str(shared["id"]),
        chat_id=chat_id,
        started_by=user.id,
        total_questions=len(questions),
        message_thread_id=message_thread_id,
    )
    if not session:
        await _safe_call(lambda: ui_msg.reply("❌ تعذّر إنشاء الجلسة حالياً، جرّب بعد شوي."), what="reply create-failed")
        return

    # قيم الشاشة الافتراضية (الملخّص بيعرضها والأدمن بيعدّلها أو بيبدأ مباشرة).
    # `top_n` لازم يتكتب صراحةً: افتراضي الـ schema 20 مش 5.
    # القنوات مجهولة دايماً (قيد تيليجرام) فبنكتبها مباشرة.
    sid = str(session["id"])
    defaults: Dict[str, Any] = {
        "poll_is_anonymous": bool(is_channel),
        "show_names": True,
        "top_n": SETUP_DEFAULT_TOP_N,
        "question_timer_seconds": None,
        "pacing_mode": "fixed_interval",
        "question_interval_seconds": SETUP_DEFAULT_INTERVAL_SECONDS,
    }
    if not await update_session(sid, defaults):
        await claim_session_cancel(sid)
        await _safe_call(lambda: ui_msg.reply("❌ تعذّر تجهيز إعدادات الجلسة حالياً، جرّب بعد شوي."), what="reply defaults-failed")
        return

    title_raw = shared.get("source_title") or shared.get("title") or "كويز"
    await _remember_setup_header(sid, title_raw, dest_title, is_channel)
    state = _setup_state({**session, **defaults}, is_channel=is_channel)
    text = _setup_summary_text(
        state, title=title_raw, dest_title=dest_title, total=len(questions),
        is_channel=is_channel, lock_available=await quiz_lock_schema_ready(),
    )
    await _safe_call(
        lambda: ui_msg.answer(text, parse_mode="HTML", reply_markup=get_group_setup_summary_keyboard(sid)),
        what="send setup summary screen",
    )


async def _begin_group_session(msg: types.Message, share_id: str) -> None:
    """مسار الغروب (`/start gq_<id>` أو `/groupquiz`): **الإعداد بالكامل بخاص البوت**.

    الأمر جوا الغروب بس بيحدّد الغروب الهدف؛ شاشات الإعداد بتنبعت لخاص الأدمن (نفس مسار
    القنوات). لو الأدمن ما فتح البوت بالخاص أبداً (تيليجرام بتمنع البوت يبدأ محادثة)
    بيفتح رابط متابعة مؤقت مرتبط به، بلا إعادة الأمر.
    """
    user = msg.from_user
    if not user:
        return
    chat = msg.chat
    if not await is_group_admin(chat.id, user.id):
        await _safe_call(lambda: msg.reply("⛔ فقط أدمن الغروب يقدر يشغّل كويز جماعي هون."), what="reply not-admin")
        return

    # البوت موجود بالغروب أكيد (وصله الأمر)، بس ممكن ما يكون مسجَّل بـ bot_chat_memberships
    # (انضاف قبل تفعيل my_chat_member) - منسجّله هون حتى يظهر بلائحة الغروبات لاحقاً.
    membership = await get_chat_membership(chat.id)
    if not membership or membership.get("status") not in ("member", "administrator"):
        await upsert_chat_membership(
            chat_id=chat.id, chat_type=chat.type, chat_title=chat.title,
            status="member", can_post_messages=None,
        )

    # A random, short-lived capability. Never embed chat/quiz IDs or credentials
    # in the URL. Redemption is additionally bound to the initiating admin.
    token = secrets.token_urlsafe(24)
    await redis_client.set(
        f"gq:setup:{token}",
        json.dumps({"user_id": user.id, "chat_id": chat.id, "share_id": share_id,
                    "message_thread_id": msg.message_thread_id, "title": chat.title}),
        ex=600,
    )
    username = await _get_bot_username()
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="📩 إكمال إعداد الاختبار في الخاص",
                                   url=f"https://t.me/{username}?start=gqsetup_{token}")
    ]])
    await _safe_call(
        lambda: msg.reply("📩 تابع إعداد هذا الاختبار في الخاص. الرابط صالح لعشر دقائق وللمشرف الذي بدأه فقط.", reply_markup=kb),
        what="reply private setup link",
    )


async def resume_group_setup(msg: types.Message, payload: str) -> None:
    """Private /start entry point; no dependency on an existing user or FSM."""
    token = payload.removeprefix("gqsetup_")
    if msg.chat.type != "private" or len(token) != 32 or not all(c.isalnum() or c in "_-" for c in token):
        await msg.answer("❌ رابط إعداد غير صالح أو منتهي الصلاحية.")
        return
    key = f"gq:setup:{token}"
    raw = await redis_client.get(key)
    if not raw:
        await msg.answer("⌛ انتهت صلاحية الرابط أو استُخدم بالفعل. ابدأ الاختبار مجدداً من المجموعة.")
        return
    data = json.loads(raw)
    if data["user_id"] != msg.from_user.id or not await is_group_admin(data["chat_id"], msg.from_user.id, fresh=True):
        await msg.answer("⛔ هذا الرابط للمشرف الذي بدأ الاختبار، ويشترط بقاء صلاحياته في المجموعة.")
        return
    # GETDEL is atomic across webhook workers. Unauthorized attempts never consume it.
    claimed = await redis_client.getdel(key)
    if not claimed:
        await msg.answer("ℹ️ تم فتح هذا الرابط بالفعل.")
        return
    await _start_session_setup(
        ui_msg=msg, chat_id=data["chat_id"], user=msg.from_user,
        share_id=data["share_id"], message_thread_id=data.get("message_thread_id"),
        dest_title=data.get("title"),
    )


@router.message(Command("start"))
async def group_start_deeplink(msg: types.Message, command: CommandObject):
    """يلتقط `/start gq_<share_id>` الواصل للغروب عبر زر `?startgroup=`.

    أي `/start` تاني بالغروب (بلا payload أو بـ payload مختلف) بينزل للمعالج
    العام بـ `handlers/start.py` عبر SkipHandler - ما منغيّر سلوكه إطلاقاً.
    """
    payload = (command.args or "").strip()
    if not payload.startswith("gq_"):
        raise SkipHandler()
    await _begin_group_session(msg, payload[3:])


@router.message(Command("groupquiz"))
async def group_quiz_command(msg: types.Message, command: CommandObject):
    """مسار احتياطي يدوي: `/groupquiz <share_id>`.

    ضروري لأن رابط `?startgroup=` بيتصرّف بشكل مختلف بين نسخ تيليجرام لو البوت
    أصلاً عضو بالغروب - فبيضل في طريق مضمون لبدء الجلسة بلا إعادة إضافة البوت.
    """
    share_id = (command.args or "").strip()
    if not share_id:
        await _safe_call(lambda: msg.reply("استخدم: <code>/groupquiz &lt;كود المشاركة&gt;</code>", parse_mode="HTML"),
                         what="reply usage")
        return
    await _begin_group_session(msg, share_id.replace("gq_", "", 1))


# ==================== شاشتا الإعداد ====================
async def _guard_config_callback(call: types.CallbackQuery, session_id: str) -> Optional[Dict[str, Any]]:
    """فحص مشترك لأزرار الإعداد: أدمن + جلسة موجودة وبحالة waiting.

    مساران (بيتحدّدوا بنوع محادثة الرسالة اللي عليها الزر):

    1. **غروب** (المسار الأصلي بلا أي تغيير): الشاشة جوا الغروب نفسه - الأدمن
       بيتفحص على `call.message.chat.id` والجلسة لازم تكون لنفس المحادثة.
    2. **خاص** (مسار القنوات): الشاشة بخاص الأدمن مع البوت، والقناة الهدف هي
       `session["chat_id"]` (مش محادثة الرسالة). ثلاث شروط لازم تتحقق سوا:
       الجلسة `waiting`، وصاحب الجلسة (`started_by`) هو نفسه الضاغط وهاي محادثته
       الخاصة، و**أدمن لايف بالقناة الهدف** (مش بس وقت البدء - ممكن انسحبت صلاحيته
       بين الشاشات).
    """
    chat = call.message.chat
    if chat.type == "private":
        session = await get_session(session_id)
        if (not session
                or session["status"] != "waiting"
                or int(session["started_by"]) != call.from_user.id
                or chat.id != call.from_user.id):
            await call.answer("⚠️ هالجلسة ما عادت متاحة", show_alert=True)
            return None
        if not await is_group_admin(int(session["chat_id"]), call.from_user.id, fresh=True):
            await call.answer("⛔ للأدمن فقط", show_alert=True)
            return None
        return session

    if not await is_group_admin(chat.id, call.from_user.id, fresh=True):
        await call.answer("⛔ للأدمن فقط", show_alert=True)
        return None
    session = await get_session(session_id)
    if not session or session["status"] != "waiting" or int(session["chat_id"]) != chat.id:
        await call.answer("⚠️ هالجلسة ما عادت متاحة", show_alert=True)
        return None
    return session


async def _is_channel_chat(chat_id: int) -> bool:
    """القنوات استفتاءاتها مجهولة دايماً بتيليجرام - المرجع `bot_chat_memberships.chat_type`
    (chat_id وحده ما بيفرّق بين القناة والسوبرغروب - الاتنين -100...)."""
    membership = await get_chat_membership(chat_id)
    return bool(membership) and membership.get("chat_type") == "channel"


@router.callback_query(F.data.startswith("gqa:"))
async def choose_group_anonymity(call: types.CallbackQuery):
    try:
        _, session_id, mode_code = call.data.split(":", 2)
        session = await _guard_config_callback(call, session_id)
        if not session:
            return

        is_anonymous = mode_code == "a"
        if not is_anonymous and await _is_channel_chat(int(session["chat_id"])):
            # حماية ضد callback_data قديم/مزوّر: القنوات مجهولة فقط (قيد تيليجرام).
            await call.answer("⚠️ القنوات بتدعم النمط المجهول فقط (قيد من تيليجرام)", show_alert=True)
            return
        await update_session(session_id, {"poll_is_anonymous": is_anonymous})

        if is_anonymous:
            # شاشة إخفاء الأسماء بلا معنى بوضع مجهول بالكامل - ما في ترتيب
            # أصلاً نعرض فيه أسماء. منتخطّاها مباشرة لشاشة المؤقّت.
            await call.message.edit_text(
                "🙈 وضع مجهول بالكامل - بلا نقاط ولا ترتيب نهائي.\n\n"
                "⏱ بدك مؤقّت داخلي لكل سؤال؟ (الاستفتاء بيسكّر لحاله لما يخلص الوقت)",
                reply_markup=get_group_timer_keyboard(session_id),
            )
        else:
            await call.message.edit_text(
                "🏆 وضع تنافسي - نقاط وترتيب نهائي.\n\n"
                "👥 بتحب أسماء الطلاب تظهر برسائل الترتيب اللي بينشرها البوت؟\n"
                "<i>(زر \"من صوّت لمين\" على الاستفتاء نفسه رح يضل ظاهر مهما اخترت - مفروض من تيليجرام)</i>",
                parse_mode="HTML",
                reply_markup=get_group_names_privacy_keyboard(session_id),
            )
    except Exception as e:
        log_error(logger, f"Error in choose_group_anonymity: {e}", exception=e)
    finally:
        try:
            await call.answer()
        except Exception:
            pass


@router.callback_query(F.data.startswith("gqn:"))
async def choose_group_names_privacy(call: types.CallbackQuery):
    try:
        _, session_id, raw_flag = call.data.split(":", 2)
        session = await _guard_config_callback(call, session_id)
        if not session:
            return

        show_names = raw_flag == "1"
        await update_session(session_id, {"show_names": show_names})

        hint = "👤 الأسماء رح تظهر بالترتيب" if show_names else "🙈 الأسماء رح تنخفى بالترتيب (لاعب ١، لاعب ٢...)"
        await call.message.edit_text(
            f"{hint}\n\n{_TOP_N_PROMPT}",
            reply_markup=get_group_top_n_keyboard(session_id, GROUP_TOP_N_DEFAULT),
        )
    except Exception as e:
        log_error(logger, f"Error in choose_group_names_privacy: {e}", exception=e)
    finally:
        try:
            await call.answer()
        except Exception:
            pass


_TOP_N_PROMPT = (
    f"🏅 كم متسابق بدك يظهر بالترتيب النهائي؟ ({GROUP_TOP_N_MIN}-{GROUP_TOP_N_MAX})\n"
    "🥇 الأول ذهبي · 🥈 الثاني فضي · 🥉 الثالث برونزي"
)


@router.callback_query(F.data.startswith("gqk:"))
async def choose_group_top_n(call: types.CallbackQuery):
    """`gqk:<id>:n:<رقم>` تنقّل (إعادة رسم بلا حفظ) · `gqk:<id>:ok:<رقم>` تأكيد وحفظ · `gqk:<id>:noop`."""
    try:
        parts = call.data.split(":")
        if len(parts) < 3:
            await call.answer("❌ طلب غير صالح.", show_alert=True)
            return
        session_id, action = parts[1], parts[2]
        if action == "noop":
            await call.answer()
            return
        try:
            n = int(parts[3])
        except (IndexError, ValueError):
            await call.answer("❌ طلب غير صالح.", show_alert=True)
            return
        if action not in ("n", "ok"):
            await call.answer("❌ طلب غير صالح.", show_alert=True)
            return
        session = await _guard_config_callback(call, session_id)
        if not session:
            return
        n = max(GROUP_TOP_N_MIN, min(GROUP_TOP_N_MAX, n))

        if action == "n":
            await call.message.edit_reply_markup(reply_markup=get_group_top_n_keyboard(session_id, n))
            await call.answer()
            return

        await update_session(session_id, {"top_n": n})
        await call.message.edit_text(
            f"🏅 الترتيب النهائي بيعرض أول <b>{n}</b>.\n\n"
            "⏱ وهلق: بدك مؤقّت داخلي لكل سؤال؟ (الاستفتاء بيسكّر لحاله لما يخلص الوقت)",
            parse_mode="HTML",
            reply_markup=get_group_timer_keyboard(session_id),
        )
        await call.answer()
    except Exception as e:
        log_error(logger, f"Error in choose_group_top_n: {e}", exception=e)
        try:
            await call.answer()
        except Exception:
            pass


@router.callback_query(F.data.startswith("gqt:"))
async def choose_group_timer(call: types.CallbackQuery):
    try:
        _, session_id, raw_seconds = call.data.split(":", 2)
        session = await _guard_config_callback(call, session_id)
        if not session:
            return

        seconds = int(raw_seconds)
        if seconds not in {0, 15, 30, 45, 60, 90, 120, 300, 600}:
            await call.answer("❌ مدة غير صالحة", show_alert=True)
            return
        timer = seconds if seconds > 0 else None
        lock_available = await quiz_lock_schema_ready()
        timer_fields = {"question_timer_seconds": timer}
        if lock_available:
            timer_fields["lock_chat"] = False
        if not await update_session(session_id, timer_fields):
            await call.answer("❌ تعذر حفظ الإعدادات", show_alert=True)
            return
        if lock_available and not await _is_channel_chat(int(session["chat_id"])):
            await call.message.edit_text(
                "🔒 قفل رسائل المجموعة أثناء الاختبار؟ التصويت يبقى متاحاً.\n"
                "اختياري ومطفأ افتراضياً، ويتطلب مؤقّتاً للأسئلة أو فاصلاً زمنياً محدداً.",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="🔓 متابعة دون قفل (الافتراضي)", callback_data=f"gql:{session_id}:0")],
                    [types.InlineKeyboardButton(text="🔒 تفعيل القفل", callback_data=f"gql:{session_id}:1")],
                    [types.InlineKeyboardButton(text="❌ إلغاء", callback_data=f"gqc:{session_id}")],
                ]),
            )
        else:
            await call.message.edit_text("⏳ إيمتى يُرسل السؤال التالي؟",
                reply_markup=get_group_pacing_keyboard(session_id, timer_enabled=bool(timer)))
    except Exception as e:
        log_error(logger, f"Error in choose_group_timer: {e}", exception=e)
    finally:
        try:
            await call.answer()
        except Exception:
            pass


async def _ensure_group_lock_permission(
    call: types.CallbackQuery,
    session: Dict[str, Any],
    retry_data: Optional[str] = None,
    skip_data: Optional[str] = None,
) -> bool:
    """Check Telegram live and keep the waiting session intact while granting rights.

    `retry_data`/`skip_data` (اختياريان): callback_data لزرّي «تحقق مجدداً» و«متابعة دون قفل».
    الافتراضي = السلسلة القديمة (`gql:`)؛ شاشة الملخّص (`gqs:`) بتمرّر قيمها حتى ما يرجع
    الأدمن للسلسلة القديمة.
    """
    try:
        member = await bot.get_chat_member(int(session["chat_id"]), bot.id, request_timeout=15)
        if _member_status(member) == "administrator" and getattr(member, "can_restrict_members", False):
            return True
        heading = "🔑 لتفعيل القفل، يحتاج البوت صلاحية «تقييد الأعضاء»."
    except Exception:
        heading = "⚠️ تعذر التحقق من صلاحيات البوت حالياً. جرّب «تحقق مجدداً» بعد قليل."
    username = await _get_bot_username()
    sid = str(session["id"])
    retry_cb = retry_data or f"gql:{sid}:1"
    skip_cb = skip_data or f"gql:{sid}:0"
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔑 منح الصلاحية",
            url=f"https://t.me/{username}?startgroup&admin=restrict_members")],
        [types.InlineKeyboardButton(text="🔄 تحقق مجدداً", callback_data=retry_cb)],
        [types.InlineKeyboardButton(text="🔓 متابعة دون قفل", callback_data=skip_cb)],
        [types.InlineKeyboardButton(text="❌ إلغاء", callback_data=f"gqc:{sid}")],
    ])
    await _safe_call(lambda: call.message.edit_text(
        heading + "\n\n"
        "1️⃣ اضغط «منح الصلاحية» واختر نفس مجموعة الاختبار.\n"
        "2️⃣ أكّد صلاحية «حظر المستخدمين / تقييد الأعضاء»، ثم ارجع واضغط «تحقق مجدداً».\n\n"
        "يلزم أن تكون مالك المجموعة أو مشرفاً يستطيع تعديل صلاحيات المشرفين.\n"
        "إذا لم تظهر المجموعة: افتح إعداداتها ← المشرفون ← بوت أبيض، وفعّل الصلاحية.\n\n"
        "✅ إعدادات الاختبار محفوظة، ولم تبدأ الجلسة بعد.", reply_markup=keyboard),
        what="show group lock permission help")
    await call.answer("إعداداتك محفوظة. امنح الصلاحية ثم تحقق مجدداً.")
    return False


@router.callback_query(F.data.startswith("gql:"))
async def choose_group_lock(call: types.CallbackQuery):
    _, session_id, value = call.data.split(":", 2)
    session = await _guard_config_callback(call, session_id)
    if not session:
        return
    if (not await quiz_lock_schema_ready() or value not in {"0", "1"}
            or await _is_channel_chat(int(session["chat_id"]))):
        await call.answer("❌ إعداد غير صالح", show_alert=True)
        return
    if value == "1" and not await _ensure_group_lock_permission(call, session):
        return
    if not await update_session(session_id, {"lock_chat": value == "1"}):
        await call.answer("❌ تعذر حفظ الإعدادات", show_alert=True)
        return
    await call.message.edit_text(
        "⏳ إيمتى يُرسل السؤال التالي؟" + ("\n🔒 القفل مفعّل، ويُعاد فتح المجموعة تلقائياً عند النهاية." if value == "1" else "\n🔓 القفل معطّل."),
        reply_markup=get_group_pacing_keyboard(session_id, timer_enabled=bool(session.get("question_timer_seconds"))),
    )
    await call.answer()


async def _launch_group_session(
    call: types.CallbackQuery,
    session: Dict[str, Any],
    session_id: str,
    pacing_mode: str,
    interval: Optional[int],
) -> None:
    """مسار الإطلاق المشترك (waiting -> active ثم أول سؤال) - بتستدعيه شاشة الملخّص
    (`gqs:...:go`) والمسار القديم (`gqp:`). القفل لازم يكون انفحص قبل هون.
    بلا `call.answer()` - المستدعي مسؤول عنه."""
    start_fields = {
        "pacing_mode": pacing_mode,
        "question_interval_seconds": interval,
        "status": "active",
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "next_question_due_at": _utc_iso(120),
    }
    if not session.get("poll_is_anonymous") and await _is_channel_chat(int(session["chat_id"])):
        # شبكة أمان أخيرة (جلسة قناة قديمة غير مجهولة): تيليجرام ما بتسمح بغير المجهول بالقنوات.
        start_fields["poll_is_anonymous"] = True
    updated = await start_waiting_session(session_id, start_fields)
    if not updated:
        await call.answer("❌ تعذّر بدء الجلسة", show_alert=True)
        return

    if updated.get("lock_chat"):
        try:
            await acquire_quiz_lock(updated)
        except Exception:
            await claim_session_cancel(session_id)
            await restore_quiz_lock(session_id, int(updated["chat_id"]))
            await call.message.edit_text("❌ تعذر تفعيل قفل المجموعة بأمان. تحقق أن البوت مشرف ولديه صلاحية تقييد الأعضاء، ثم أعد الإعداد.")
            return
    # Never overwrite a concurrent finish. Heartbeat resumes after a crash.
    updated = await activate_session_due(session_id)
    if not updated:
        await restore_quiz_lock(session_id, int(session["chat_id"]))
        return
    if pacing_mode == "burst":
        total = int(updated.get("total_questions") or 0)
        minutes = max(1, -(-(total * BURST_GAP_SECONDS) // 60))
        await call.message.edit_text(
            f"🚀 انطلقت الجلسة! كل الأسئلة ({total}) رح تنبعت دفعة وحدة، بفاصل {BURST_GAP_SECONDS} ثواني "
            f"بين كل سؤال والتاني (حوالي {minutes} دقيقة) لتفادي حدود تيليجرام.\nبالتوفيق للجميع 🎯"
        )
        log_info(logger, f"Group quiz session {session_id} started in chat {updated['chat_id']} (burst)")
        spawn(run_burst_session(session_id), name=f"gq_burst_{session_id}")
        return
    label = "فور انتهاء مؤقّت كل سؤال" if pacing_mode == "chain_to_timer" else f"كل {interval} ثانية"
    await call.message.edit_text(f"🚀 انطلقت الجلسة! الأسئلة بتوصل {label}.\nبالتوفيق للجميع 🎯")
    log_info(logger, f"Group quiz session {session_id} started in chat {updated['chat_id']} ({pacing_mode})")
    await send_next_group_question(updated)


@router.callback_query(F.data.startswith("gqp:"))
async def choose_group_pacing(call: types.CallbackQuery):
    try:
        _, session_id, mode_code, raw_value = call.data.split(":", 3)
        session = await _guard_config_callback(call, session_id)
        if not session:
            return

        if session.get("lock_chat") and mode_code == "b":
            await call.answer("⚠️ القفل يتطلب اختباراً بمؤقّت أو فاصلاً زمنياً، ولا يعمل مع الإرسال دفعة واحدة", show_alert=True)
            return
        if mode_code not in {"b", "c", "f"}:
            await call.answer("❌ إعداد غير صالح", show_alert=True)
            return
        if mode_code == "b":
            if session.get("question_timer_seconds"):
                # حماية ضد callback_data مزوّر: مع مؤقّت داخلي الاستفتاءات الأولى بتسكّر
                # قبل ما الطلاب يوصلولها.
                await call.answer("⚠️ هالوضع بلا مؤقّت داخلي فقط", show_alert=True)
                return
            pacing_mode, interval = "burst", BURST_GAP_SECONDS
        elif mode_code == "c":
            if not session.get("question_timer_seconds"):
                # حماية ضد callback_data مزوّر: chain_to_timer بلا مؤقّت = جلسة
                # بتعلّق للأبد (ما في حدث إغلاق poll يشغّل السؤال التالي).
                await call.answer("⚠️ هالوضع بيحتاج مؤقّت داخلي مفعّل", show_alert=True)
                return
            pacing_mode, interval = "chain_to_timer", None
        else:
            pacing_mode = "fixed_interval"
            if int(raw_value) not in {30, 60, 120, 300}:
                await call.answer("❌ فاصل زمني غير صالح", show_alert=True)
                return
            interval = max(MIN_QUESTION_INTERVAL_SECONDS, int(raw_value))

        if session.get("lock_chat") and not await _ensure_group_lock_permission(call, session):
            return

        await _launch_group_session(call, session, session_id, pacing_mode, interval)
    except Exception as e:
        log_error(logger, f"Error in choose_group_pacing: {e}", exception=e)
    finally:
        try:
            await call.answer()
        except Exception:
            pass


@router.callback_query(F.data.startswith("gqc:"))
async def cancel_group_setup(call: types.CallbackQuery):
    try:
        session_id = call.data.split(":", 1)[1]
        session = await _guard_config_callback(call, session_id)
        if not session:
            return
        await update_session(session_id, {"status": "cancelled", "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
        await clear_session_questions_cache(session_id)
        await call.message.edit_text("❌ تم إلغاء إعداد الجلسة.")
    except Exception as e:
        log_error(logger, f"Error in cancel_group_setup: {e}", exception=e)
    finally:
        try:
            await call.answer()
        except Exception:
            pass


# ==================== شاشة ملخّص الإعدادات (gqs:) ====================
# بديل السلسلة القديمة gqa -> gqn -> gqk -> gqt -> gql -> gqp (آخر ضغطة فيها كانت تبدأ الجلسة).
# هلق: ملخّص واحد + قائمة إعدادات؛ كل ضغطة إعداد بتنحفظ فوراً وبتنعاد نفس الشاشة.
# المعالجات القديمة (gqa/gqn/gqk/gqt/gql/gqp) ضلّت موجودة بلا تغيير لرسائل قديمة معلّقة،
# ولا شي بالكود الجديد بيوصل إلها.
#
# صيغة callback_data (≤ 64 بايت):
#   gqs:<sid>:go | b | m | noop
#   gqs:<sid>:o:<field>              فتح شاشة إعداد
#   gqs:<sid>:s:<field>:<value>      حفظ قيمة ثم إعادة رسم نفس الشاشة
#   field ∈ mode(c|a) · names(1|0) · top(1-20) · timer(0,15,30,45,60) · pace(c|b|f30|f60|f120|f300) · lock(1|0)
#         · cert(1|0|th<لون>|d1|d0|r1|r0|sc1|sc0|n1|n3|n5|n10|pv|te|ie)  ← شهادات التقدير (المرحلة ب)
_SETUP_HEADER_PREFIX = "gq:setup_hdr:"
_SETUP_HEADER_TTL = 3600
_SETUP_FIELDS = ("mode", "names", "top", "timer", "pace", "lock", "cert")
_SETUP_PACE_LABELS = {30: "كل 30 ثانية", 60: "كل دقيقة", 120: "كل دقيقتين", 300: "كل 5 دقائق"}


async def _remember_setup_header(session_id: str, title: str, dest_title: Optional[str], is_channel: bool) -> None:
    """صف الجلسة ما فيه عنوان الكويز/اسم الوجهة، فمنحفظهم بـ Redis لإعادة رسم الملخّص.
    فقدانهم غير خطير: بيظهر «كويز» بلا اسم وجهة."""
    try:
        await redis_client.set(
            f"{_SETUP_HEADER_PREFIX}{session_id}",
            json.dumps({"title": title, "dest_title": dest_title, "is_channel": bool(is_channel)}),
            ex=_SETUP_HEADER_TTL,
        )
    except Exception as e:
        log_error(logger, f"Redis error saving setup header for session {session_id}: {e}")


async def _load_setup_header(session_id: str) -> Dict[str, Any]:
    try:
        raw = await redis_client.get(f"{_SETUP_HEADER_PREFIX}{session_id}")
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
    except Exception as e:
        log_error(logger, f"Redis error loading setup header for session {session_id}: {e}")
    return {}


def _pace_to_mode(pace: str) -> Tuple[str, Optional[int]]:
    """رمز الإيقاع -> (pacing_mode, question_interval_seconds)."""
    if pace == "b":
        return "burst", BURST_GAP_SECONDS
    if pace == "c":
        return "chain_to_timer", None
    return "fixed_interval", max(MIN_QUESTION_INTERVAL_SECONDS, int(pace[1:]))


def _setup_state(session: Dict[str, Any], *, is_channel: bool) -> Dict[str, Any]:
    """الإعدادات الفعلية من صف الجلسة بعد تطبيع أي تعارض (مصدر الحقيقة لكل العرض)."""
    anonymous = True if is_channel else bool(session.get("poll_is_anonymous"))
    names = session.get("show_names")
    names = True if names is None else bool(names)
    try:
        top = int(session.get("top_n") or SETUP_DEFAULT_TOP_N)
    except (TypeError, ValueError):
        top = SETUP_DEFAULT_TOP_N
    top = max(GROUP_TOP_N_MIN, min(GROUP_TOP_N_MAX, top))
    try:
        timer = int(session.get("question_timer_seconds") or 0)
    except (TypeError, ValueError):
        timer = 0

    mode = session.get("pacing_mode")
    try:
        interval = int(session.get("question_interval_seconds") or SETUP_DEFAULT_INTERVAL_SECONDS)
    except (TypeError, ValueError):
        interval = SETUP_DEFAULT_INTERVAL_SECONDS
    if mode == "burst" and timer == 0:
        pace = "b"
    elif mode == "chain_to_timer" and timer > 0:
        pace = "c"
    else:
        pace = f"f{interval if interval in GROUP_INTERVAL_PRESETS else SETUP_DEFAULT_INTERVAL_SECONDS}"
    lock = bool(session.get("lock_chat")) and not is_channel and pace != "b"
    cert_cfg = cert_normalize_config(session.get("cert_config"))
    cert_ok, cert_reason = cert_eligibility(anonymous=anonymous, names=names, pace=pace, is_channel=is_channel)
    return {
        "mode": "a" if anonymous else "c", "anonymous": anonymous, "names": names, "top": top,
        "timer": timer, "pace": pace, "lock": lock, "is_channel": bool(is_channel),
        "cert": bool(cert_cfg["enabled"] and cert_ok), "cert_cfg": cert_cfg, "cert_reason": cert_reason,
    }


def _compute_setup_update_core(
    session: Dict[str, Any], field: str, value: str, *, is_channel: bool, lock_available: bool = True,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """دالة صرفة (بلا I/O) بكل قواعد التعارض (الحقول الستة الأصلية؛ الشهادات بالغلاف `_compute_setup_update`). بترجّع (updates, None) أو (None, رسالة_خطأ).

    - burst بلا مؤقّت فقط · chain_to_timer بمؤقّت فقط · القفل لا يعمل مع burst.
    - تفعيل مؤقّت أثناء burst (أو إلغاؤه أثناء chain_to_timer) يرجّع الإيقاع تلقائياً إلى fixed_interval/30.
    - القنوات: مجهولة فقط وبلا قفل. المجهول: الأسماء والأوائل بلا معنى.
    """
    state = _setup_state(session, is_channel=is_channel)
    invalid = "❌ إعداد غير صالح"
    reset_pace = {
        "pacing_mode": "fixed_interval",
        "question_interval_seconds": max(MIN_QUESTION_INTERVAL_SECONDS, SETUP_DEFAULT_INTERVAL_SECONDS),
    }

    if field == "mode":
        if value not in ("c", "a"):
            return None, invalid
        if value == "c" and is_channel:
            return None, "⚠️ القنوات بتدعم النمط المجهول فقط (قيد من تيليجرام)"
        return {"poll_is_anonymous": value == "a"}, None

    if field == "names":
        if value not in ("1", "0"):
            return None, invalid
        if state["anonymous"]:
            return None, "⚠️ النمط المجهول بلا ترتيب، فالأسماء بلا معنى"
        return {"show_names": value == "1"}, None

    if field == "top":
        try:
            n = int(value)
        except (TypeError, ValueError):
            return None, invalid
        if not GROUP_TOP_N_MIN <= n <= GROUP_TOP_N_MAX:
            return None, invalid
        if state["anonymous"]:
            return None, "⚠️ النمط المجهول بلا ترتيب نهائي"
        return {"top_n": n}, None

    if field == "timer":
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            return None, invalid
        if seconds not in GROUP_TIMER_PRESETS:
            return None, "❌ مدة غير صالحة"
        updates: Dict[str, Any] = {"question_timer_seconds": seconds or None}
        if (seconds > 0 and state["pace"] == "b") or (seconds == 0 and state["pace"] == "c"):
            updates.update(reset_pace)
        return updates, None

    if field == "pace":
        if value == "b":
            if state["timer"]:
                return None, "⚠️ هالوضع بلا مؤقّت داخلي فقط"
            if state["lock"]:
                return None, "⚠️ القفل لا يعمل مع الإرسال دفعة واحدة"
        elif value == "c":
            if not state["timer"]:
                return None, "⚠️ هالوضع بيحتاج مؤقّت داخلي مفعّل"
        elif value.startswith("f"):
            try:
                seconds = int(value[1:])
            except ValueError:
                return None, invalid
            if seconds not in GROUP_INTERVAL_PRESETS:
                return None, "❌ فاصل زمني غير صالح"
        else:
            return None, invalid
        mode, interval = _pace_to_mode(value)
        return {"pacing_mode": mode, "question_interval_seconds": interval}, None

    if field == "lock":
        if value not in ("1", "0"):
            return None, invalid
        if is_channel or not lock_available:
            return None, "❌ إعداد غير صالح"
        if value == "1" and state["pace"] == "b":
            return None, "⚠️ القفل لا يعمل مع الإرسال دفعة واحدة"
        return {"lock_chat": value == "1"}, None

    return None, invalid


_CERT_AUTO_DISABLED_NOTE = "ℹ️ عطّلت شهادات التقدير لأنها بتحتاج النمط التنافسي مع إظهار الأسماء وترتيب نهائي منشور."


def _compute_setup_update(
    session: Dict[str, Any], field: str, value: str, *, is_channel: bool, lock_available: bool = True,
    cert_available: bool = False, default_title: str = CERT_DEFAULT_TITLE,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """دالة صرفة (بلا I/O). غلاف حول القواعد الأصلية يضيف: حقل `cert` (الشهادات) وتعطيلها تلقائياً
    لو تغيّر إعداد آخر بحيث ما عادت ممكنة (مجهول / أسماء مخفية / إرسال دفعة واحدة)."""
    if field == "cert":
        if is_channel or not cert_available:
            return None, "❌ إعداد غير صالح"
        state = _setup_state(session, is_channel=is_channel)
        cfg = cert_normalize_config(session.get("cert_config"), default_title=default_title)
        new_cfg, error = cert_apply_change(cfg, value)
        if error:
            return None, error
        if value == "1":
            ok, reason = cert_eligibility(anonymous=state["anonymous"], names=state["names"], pace=state["pace"], is_channel=is_channel)
            if not ok:
                return None, f"⚠️ {reason}"
        return {"cert_config": new_cfg}, None

    updates, error = _compute_setup_update_core(session, field, value, is_channel=is_channel, lock_available=lock_available)
    if error or not updates:
        return updates, error
    cfg = cert_normalize_config(session.get("cert_config"))
    if cfg["enabled"]:
        after = _setup_state({**session, **updates}, is_channel=is_channel)
        if not after["cert"]:
            updates = {**updates, "cert_config": {**cfg, "enabled": False}}
    return updates, error


def _setup_summary_text(
    state: Dict[str, Any], *, title: str, dest_title: Optional[str], total: int,
    is_channel: bool, lock_available: bool = True, cert_available: bool = False,
) -> str:
    # `title`/`dest_title` نص حر من المستخدم ومنحطّه جوا parse_mode=HTML - لازم escape.
    pace = state["pace"]
    if pace == "b":
        pace_label = "دفعة واحدة"
    elif pace == "c":
        pace_label = "فور انتهاء المؤقّت"
    else:
        pace_label = _SETUP_PACE_LABELS.get(int(pace[1:]), f"كل {pace[1:]} ثانية")
    lines = [f"📚 <b>{html.escape(title or 'كويز')}</b>"]
    if dest_title:
        dest_label = "📢 القناة" if is_channel else "👥 الغروب"
        lines.append(f"{dest_label}: <b>{html.escape(dest_title)}</b>")
    lines += [f"عدد الأسئلة: {total}", "", "⚙️ <b>الإعدادات الحالية</b>"]
    lines.append("🎮 النمط: " + ("مجهول" if state["anonymous"] else "تنافسي"))
    if not state["anonymous"]:
        lines.append("👤 الأسماء بالترتيب: " + ("ظاهرة" if state["names"] else "مخفية"))
        if pace == "b":
            # هاد سلوك finish_group_session الفعلي: burst بلا مؤقّت ما بينشر ترتيب نهائي.
            lines.append("🏅 الترتيب النهائي: ما بينشر مع الإرسال دفعة واحدة")
        else:
            lines.append(f"🏅 الترتيب النهائي: أول {state['top']}")
    lines.append("⏱ المؤقّت الداخلي: " + (f"{state['timer']} ثانية" if state["timer"] else "بدون"))
    lines.append(f"⏳ إرسال الأسئلة: {pace_label}")
    if not is_channel and lock_available:
        lines.append("🔒 قفل رسائل المجموعة: " + ("مفعّل" if state["lock"] else "معطّل"))
    if cert_available and not is_channel:
        if state["cert"]:
            lines.append(f"🎓 الشهادات: مفعّلة (أول {min(state['cert_cfg']['count'], state['top'])})")
        else:
            lines.append("🎓 الشهادات: غير مفعّلة")
    if state["anonymous"]:
        lines += ["", "🙈 <i>بلا نقاط ولا ترتيب نهائي" + (" (القنوات بتدعم المجهول فقط - قيد من تيليجرام)" if is_channel else "") + ".</i>"]
    return "\n".join(lines)


def _cert_screen_text(cfg: Dict[str, Any], *, enabled: bool, reason: Optional[str], top: int) -> str:
    """نص شاشة الشهادات. اسم الاختبار/الجهة نص حر -> html.escape (داخل parse_mode=HTML)."""
    lines = [
        "🎓 <b>شهادات التقدير</b>",
        "الحالة: " + ("✅ مفعّلة" if enabled else "غير مفعّلة"),
        f"📝 اسم الاختبار: <b>{html.escape(cfg['title'])}</b>",
        "✍️ الجهة / المعلّم: " + (f"<b>{html.escape(cfg['issuer'])}</b>" if cfg["issuer"] else "بدون"),
        f"🎨 اللون: {CERT_THEMES[cfg['theme']]['label']}",
        "📅 التاريخ: " + ("يظهر" if cfg["show_date"] else "مخفي"),
        "🏅 المركز: " + ("يظهر (المركز الأول...)" if cfg["show_rank"] else "عبارة عامة (متميّز)"),
        "🔢 الدرجة: " + ("تظهر" if cfg["show_score"] else "مخفية"),
        f"👥 عدد الشهادات: أول {min(cfg['count'], top)} (فقط لمن جاب نقطة أو أكتر)",
        "",
        "<i>بتنرسل كصور بعد الترتيب النهائي، وبتحمل اسم الطالب (الأول والأخير من حسابه) واسم المجموعة. "
        "هي شهادة تقدير غير رسمية، والعبارة هاي ثابتة. اضغط «معاينة» لتشوف شكلها التقريبي.</i>",
    ]
    if reason:
        lines += ["", f"⚠️ {html.escape(reason)}"]
    return "\n".join(lines)


async def _setup_screen_view(session: Dict[str, Any], screen: str, *, is_channel: bool, note: str = "") -> Tuple[str, Any]:
    """(نص، كيبورد) لشاشة: b ملخّص · m قائمة · أو اسم إعداد."""
    sid = str(session["id"])
    state = _setup_state(session, is_channel=is_channel)
    lock_available = await quiz_lock_schema_ready()
    cert_available = (not is_channel) and await group_cert_schema_ready()
    prefix = f"{note}\n\n" if note else ""
    if screen == "b":
        header = await _load_setup_header(sid)
        text = _setup_summary_text(
            state, title=header.get("title") or "كويز", dest_title=header.get("dest_title"),
            total=int(session.get("total_questions") or 0), is_channel=is_channel, lock_available=lock_available,
            cert_available=cert_available,
        )
        return prefix + text, get_group_setup_summary_keyboard(sid)
    if screen == "m":
        return (prefix + "⚙️ <b>إعدادات الاختبار</b>\nاختار الإعداد اللي بدك تعدّله:",
                get_group_setup_menu_keyboard(sid, is_channel=is_channel, anonymous=state["anonymous"],
                                              lock_available=lock_available, cert_available=cert_available))
    if screen == "cert":
        header = await _load_setup_header(sid)
        cfg = cert_normalize_config(session.get("cert_config"), default_title=header.get("title") or CERT_DEFAULT_TITLE)
        return (prefix + _cert_screen_text(cfg, enabled=state["cert"], reason=state["cert_reason"], top=state["top"]),
                get_group_setup_cert_keyboard(sid, cfg, enabled=state["cert"]))
    if screen == "mode":
        return (prefix + "🎮 <b>نمط الجلسة</b>\n🏆 تنافسي: نقاط وترتيب نهائي (زر \"من صوّت لمين\" بيضل ظاهر - قيد من تيليجرام).\n"
                "🙈 مجهول: محدا بيعرف مين جاوب شو، بلا نقاط ولا ترتيب.",
                get_group_setup_mode_keyboard(sid, anonymous=state["anonymous"]))
    if screen == "names":
        return (prefix + "👤 <b>ظهور الأسماء</b>\nبتحب أسماء الطلاب تظهر برسائل الترتيب اللي بينشرها البوت؟",
                get_group_setup_names_keyboard(sid, show_names=state["names"]))
    if screen == "top":
        return (prefix + _TOP_N_PROMPT, get_group_setup_top_keyboard(sid, state["top"]))
    if screen == "timer":
        return (prefix + "⏱ <b>المؤقّت الداخلي</b>\nالاستفتاء بيسكّر لحاله لما يخلص الوقت.",
                get_group_setup_timer_keyboard(sid, state["timer"]))
    if screen == "pace":
        return (prefix + "⏳ <b>إيمتى يُرسل السؤال التالي؟</b>",
                get_group_setup_pace_keyboard(sid, pace=state["pace"], timer_enabled=bool(state["timer"]), lock=state["lock"]))
    # lock
    return (prefix + "🔒 <b>قفل رسائل المجموعة أثناء الاختبار</b>\nالتصويت يبقى متاحاً، ويُعاد فتح المجموعة تلقائياً عند النهاية.\n"
            "ما بيشتغل مع الإرسال دفعة واحدة.",
            get_group_setup_lock_keyboard(sid, lock=state["lock"]))


async def _edit_setup_screen(call: types.CallbackQuery, text: str, keyboard: Any) -> None:
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        # ضغط على خيار محدّد أصلاً -> "message is not modified": مقصود نبلعه.
        if "message is not modified" not in str(e):
            raise


@router.callback_query(F.data.startswith("gqs:"))
async def setup_summary_callback(call: types.CallbackQuery):
    try:
        parts = call.data.split(":")
        if len(parts) < 3 or not parts[1]:
            await call.answer("❌ طلب غير صالح.", show_alert=True)
            return
        session_id, action = parts[1], parts[2]
        if action == "noop":
            return
        if action not in {"go", "b", "m", "o", "s"} or (action in {"o"} and len(parts) != 4) or (action == "s" and len(parts) != 5):
            await call.answer("❌ طلب غير صالح.", show_alert=True)
            return
        field = parts[3] if len(parts) > 3 else ""
        if action in {"o", "s"} and field not in _SETUP_FIELDS:
            await call.answer("❌ طلب غير صالح.", show_alert=True)
            return
        session = await _guard_config_callback(call, session_id)
        if not session:
            return
        is_channel = await _is_channel_chat(int(session["chat_id"]))
        state = _setup_state(session, is_channel=is_channel)

        if action in {"b", "m"}:
            text, kb = await _setup_screen_view(session, action, is_channel=is_channel)
            await _edit_setup_screen(call, text, kb)
            return

        if action == "o":
            hidden = ((is_channel and field in {"mode", "lock", "cert"})
                      or (state["anonymous"] and field in {"names", "top"})
                      or (field == "lock" and not await quiz_lock_schema_ready())
                      or (field == "cert" and not await group_cert_schema_ready()))
            if hidden:
                await call.answer("⚠️ هالإعداد غير متاح هون", show_alert=True)
                return
            if field == "cert" and not state["cert"] and state["cert_reason"]:
                await call.answer(f"⚠️ {state['cert_reason']}", show_alert=True)
                return
            text, kb = await _setup_screen_view(session, field, is_channel=is_channel)
            await _edit_setup_screen(call, text, kb)
            return

        if action == "s":
            value = parts[4]
            if field == "cert" and value in {"pv", "te", "ie"}:
                await _handle_cert_special(call, session, session_id, value, state, is_channel=is_channel)
                return
            cert_title = CERT_DEFAULT_TITLE
            if field == "cert":
                cert_title = (await _load_setup_header(session_id)).get("title") or CERT_DEFAULT_TITLE
            updates, error = _compute_setup_update(
                session, field, value, is_channel=is_channel, lock_available=await quiz_lock_schema_ready(),
                cert_available=(not is_channel) and await group_cert_schema_ready(), default_title=cert_title)
            if error:
                await call.answer(error, show_alert=True)
                return
            if field == "lock" and value == "1" and not await _ensure_group_lock_permission(
                    call, session, retry_data=f"gqs:{session_id}:s:lock:1", skip_data=f"gqs:{session_id}:s:lock:0"):
                return
            saved = await update_session(session_id, updates)
            if not saved:
                await call.answer("❌ تعذر حفظ الإعدادات", show_alert=True)
                return
            session = {**session, **updates}
            note = ""
            if field == "timer" and "pacing_mode" in updates:
                note = "ℹ️ رجّعت إيقاع الإرسال لكل 30 ثانية لأنه ما بيتوافق مع هالمؤقّت."
            if "cert_config" in updates and field != "cert":
                note = (note + "\n" if note else "") + _CERT_AUTO_DISABLED_NOTE
            text, kb = await _setup_screen_view(session, field, is_channel=is_channel, note=note)
            await _edit_setup_screen(call, text, kb)
            return

        # action == "go": ابدأ الاختبار بالإعدادات الحالية
        fixes: Dict[str, Any] = {}
        if bool(session.get("lock_chat")) != state["lock"]:
            fixes["lock_chat"] = state["lock"]
        if is_channel and not session.get("poll_is_anonymous"):
            fixes["poll_is_anonymous"] = True
        if state["cert_cfg"]["enabled"] and not state["cert"]:
            fixes["cert_config"] = {**state["cert_cfg"], "enabled": False}
        if fixes:
            if not await update_session(session_id, fixes):
                await call.answer("❌ تعذر حفظ الإعدادات", show_alert=True)
                return
            session = {**session, **fixes}
        if state["lock"] and not await _ensure_group_lock_permission(
                call, session, retry_data=f"gqs:{session_id}:go", skip_data=f"gqs:{session_id}:s:lock:0"):
            return
        pacing_mode, interval = _pace_to_mode(state["pace"])
        await _launch_group_session(call, session, session_id, pacing_mode, interval)
    except Exception as e:
        log_error(logger, f"Error in setup_summary_callback: {e}", exception=e)
    finally:
        try:
            await call.answer()
        except Exception:
            pass


@router.callback_query(F.data.startswith("gqe:"))
async def end_group_session_now(call: types.CallbackQuery):
    """زر "🏁 إنهاء الجلسة الآن" - يظهر تحت كل سؤال طول الجلسة. أي أدمن بالغروب
    يقدر يضغطه (مش لازم نفس الأدمن اللي بلش الجلسة - قرار عملي: أي أدمن قادر
    يتدخّل لو صاحب الجلسة غاب)."""
    try:
        session_id = call.data.split(":", 1)[1]
        if not await is_group_admin(call.message.chat.id, call.from_user.id, fresh=True):
            await call.answer("⛔ للأدمن فقط", show_alert=True)
            return

        session = await get_session(session_id)
        if not session or int(session["chat_id"]) != call.message.chat.id:
            await call.answer("⚠️ هالجلسة غير موجودة", show_alert=True)
            return
        if session["status"] not in ("waiting", "active"):
            await call.answer("ℹ️ الجلسة خلصت أصلاً", show_alert=True)
            return

        await call.answer("🏁 عم تُنهى الجلسة...")
        await finish_group_session(session, manual=True)
        log_info(logger, f"Group quiz session {session_id} ended manually by {call.from_user.id}")
    except Exception as e:
        log_error(logger, f"Error in end_group_session_now: {e}", exception=e)
        try:
            await call.answer("❌ حدث خطأ", show_alert=True)
        except Exception:
            pass


# ==================== دعم القنوات ====================
def _member_status(member: Any) -> str:
    """حالة العضوية كنص عادي - aiogram بترجّعها `str` عادي حالياً (use_enum_values)
    بس منوحّد لو انرجعت Enum بنسخة تانية."""
    status = getattr(member, "status", "")
    return str(getattr(status, "value", status))


@router.my_chat_member()
async def track_bot_chat_membership(update: types.ChatMemberUpdated):
    """كل ما تتغيّر حالة عضوية **البوت نفسه** بأي محادثة -> upsert بـ `bot_chat_memberships`.

    بلا فلتر نوع محادثة (بعكس باقي الـ router) - لازم يلتقط channel/supergroup/group.
    منتجاهل `private` بس: هناك `my_chat_member` بيوصل لما مستخدم يحظر/يفكّ حظر
    البوت، مش عضوية بمحادثة نقدر ننشر فيها.

    ⚠️ تحديثات `my_chat_member` بتوصل **من لحظة تفعيل allowed_updates وطالع بس**.
    قنوات كان البوت أدمن فيها قبل هالنشر ما رح تنسجّل لحد ما تتغيّر حالة البوت
    فيها (تعديل أي صلاحية أو إعادة إضافته).
    """
    try:
        chat = update.chat
        if chat.type not in ("channel", "supergroup", "group"):
            return

        new_status = _member_status(update.new_chat_member)
        old_status = _member_status(update.old_chat_member)
        # `can_post_messages` موجود بـ ChatMemberAdministrator بس (للقنوات) - غير هيك None.
        can_post = getattr(update.new_chat_member, "can_post_messages", None)
        if new_status != "administrator":
            can_post = None

        # added_by: بس لحظة الترقية الفعلية (مش أدمن -> أدمن) - راجع upsert_chat_membership.
        promoted_now = new_status == "administrator" and old_status != "administrator"
        added_by = update.from_user.id if (promoted_now and update.from_user) else None

        await upsert_chat_membership(
            chat_id=chat.id,
            chat_type=chat.type,
            chat_title=chat.title,
            status=new_status,
            can_post_messages=can_post,
            added_by=added_by,
        )
        log_info(logger, f"Bot membership in {chat.type} {chat.id}: {old_status} -> {new_status} (can_post={can_post})")
    except Exception as e:
        log_error(logger, f"Error in track_bot_chat_membership: {e}", exception=e)
        return

    # بعد التسجيل (وبمعزل عن أي فشل فيه): لو المستخدم اللي غيّر حالة البوت كان عم يجهّز
    # قناة لكويز - نبعتله زر البدء تلقائياً.
    try:
        await _notify_channel_ready(update, new_status=new_status, old_status=old_status, can_post=can_post)
    except Exception as e:
        log_error(logger, f"Error in _notify_channel_ready: {e}", exception=e)


async def _notify_channel_ready(update: types.ChatMemberUpdated, *, new_status: str, old_status: str, can_post: Optional[bool]) -> None:
    """يبعت للمستخدم (بالخاص) زر \"🚀 ابدأ\" لما البوت ينضاف أدمن بقناة **وعنده صلاحية نشر**،
    بشرط إنه المستخدم نفسه (`update.from_user`) كان فاتح شاشة القنوات (نيّة معلّقة بـ Redis).

    - إذا انضاف أدمن بس **بدون** صلاحية نشر (المستخدم شال التفعيل بشاشة تيليجرام) -> رسالة
      قصيرة بتشرح كيف يفعّلها، والنيّة بتضل معلّقة: لما يفعّلها بيوصل `my_chat_member` جديد
      وبتنبعت رسالة البدء.
    - بس على **الانتقال** (مش أدمن -> أدمن، أو ما كان عنده نشر -> صار عنده) عشان ما تتكرر
      الرسالة مع كل تحديث لاحق.
    """
    chat = update.chat
    user = update.from_user
    if not user:
        return

    # 👥 غروب: أي انضمام فعلي للبوت (مش عضو -> عضو/أدمن) من المستخدم اللي فاتح شاشة الغروبات.
    if chat.type in ("group", "supergroup"):
        active = ("member", "administrator")
        if new_status not in active or old_status in active:
            return
        share_id = await _pop_pending_channel_intent(user.id, consume=True)
        if not share_id:
            return
        await invalidate_group_admin_cache(chat.id, user.id)
        kb = types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text="🚀 ابدأ على هالغروب", callback_data=f"gqchp:{share_id}:{chat.id}")
        ]])
        await _safe_call(
            lambda: bot.send_message(
                chat_id=user.id,
                text=f"✅ تمام! البوت انضاف لغروب <b>{html.escape(chat.title or 'الغروب')}</b>.\nبدك تشغّل الكويز عليه هلق؟",
                parse_mode="HTML",
                reply_markup=kb,
            ),
            what=f"notify user {user.id} group {chat.id} ready",
        )
        return

    if chat.type != "channel" or new_status != "administrator":
        return

    old_can_post = getattr(update.old_chat_member, "can_post_messages", None) if old_status == "administrator" else None
    gained_post = can_post is True and old_can_post is not True
    just_added = old_status != "administrator"
    if not (gained_post or just_added):
        return

    share_id = await _pop_pending_channel_intent(user.id, consume=False)
    if not share_id:
        return  # المستخدم ما كان عم يجهّز قناة لكويز - ما منزعجه برسالة

    title = html.escape(chat.title or "القناة")
    if can_post is True:
        await _pop_pending_channel_intent(user.id, consume=True)
        # الكاش القديم (لو انسأل عن هالقناة قبل) ممكن يكون "مش أدمن" - منمسحه
        await invalidate_group_admin_cache(chat.id, user.id)
        kb = types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text="🚀 ابدأ على هالقناة", callback_data=f"gqchp:{share_id}:{chat.id}")
        ]])
        await _safe_call(
            lambda: bot.send_message(
                chat_id=user.id,
                text=f"✅ تمام! البوت صار أدمن بقناة <b>{title}</b> وعنده صلاحية النشر.\nبدك تشغّل الكويز عليها هلق؟",
                parse_mode="HTML",
                reply_markup=kb,
            ),
            what=f"notify user {user.id} channel {chat.id} ready",
        )
    elif just_added:
        await _safe_call(
            lambda: bot.send_message(
                chat_id=user.id,
                text=(
                    f"⚠️ البوت انضاف لقناة <b>{title}</b> بس صلاحية «نشر الرسائل» مطفية.\n"
                    "فعّلها: إعدادات القناة ← المشرفون ← البوت ← «نشر الرسائل» ← حفظ.\n"
                    "أول ما تحفظ برسلك زر البدء تلقائياً."
                ),
                parse_mode="HTML",
            ),
            what=f"notify user {user.id} channel {chat.id} missing post permission",
        )


async def _filter_channels_user_admins(channels: list, user_id: int, concurrency: int = 10) -> list:
    """يفلتر لائحة القنوات لللي `user_id` أدمن فيها **لايف** (`is_group_admin` - كاش Redis
    5 دقايق، fail-closed). بترتيب الإدخال نفسه. `Semaphore` بس لتفادي دفعة
    `get_chat_member` كبيرة سوا لو اللائحة طويلة."""
    sem = asyncio.Semaphore(concurrency)

    async def _check(ch: Dict[str, Any]) -> bool:
        async with sem:
            return await is_group_admin(int(ch["chat_id"]), user_id)

    flags = await asyncio.gather(*(_check(ch) for ch in channels))
    return [ch for ch, ok in zip(channels, flags) if ok]


# ==== شاشات الخطوات (غروب + قناة) ====
# نية "المستخدم عم يجهّز قناة لهالكويز" - بتنحفظ لحظة فتح شاشة القنوات، وبتُستخدم لما
# يوصل `my_chat_member` (البوت انضاف أدمن بقناة) عشان نبعت للمستخدم زر "🚀 ابدأ"
# تلقائياً. **ضرورية** لأنه رابط `?startchannel` ما بيحمل payload (موثّق: "absent in
# channel links") - فما في طريقة تانية نربط القناة الجديدة بالكويز اللي كان عم يشاركه.
_PENDING_CHANNEL_PREFIX = "gq:pending_channel:"
_PENDING_CHANNEL_TTL = 1800  # 30 دقيقة - وقت كافي لإضافة البوت من تيليجرام والرجوع

_bot_username_cache: Optional[str] = None


async def _get_bot_username() -> str:
    global _bot_username_cache
    if not _bot_username_cache:
        _bot_username_cache = (await bot.get_me()).username
    return _bot_username_cache


async def _set_pending_channel_intent(user_id: int, share_id: str) -> None:
    try:
        await redis_client.set(f"{_PENDING_CHANNEL_PREFIX}{user_id}", share_id, ex=_PENDING_CHANNEL_TTL)
    except Exception as e:
        log_error(logger, f"Redis error saving pending channel intent for {user_id}: {e}")


async def _pop_pending_channel_intent(user_id: int, *, consume: bool = True) -> Optional[str]:
    key = f"{_PENDING_CHANNEL_PREFIX}{user_id}"
    try:
        raw = await redis_client.get(key)
        if not raw:
            return None
        if consume:
            await redis_client.delete(key)
        return raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
    except Exception as e:
        log_error(logger, f"Redis error reading pending channel intent for {user_id}: {e}")
        return None


async def _build_channel_screen(share_id: str, user_id: int):
    """نص + كيبورد شاشة القنوات: قنوات جاهزة (البوت أدمن + نشر + المستخدم أدمن لايف)
    + زر إضافة البوت لقناة جديدة + خطوات صريحة."""
    candidates = await list_postable_channels()
    mine = await _filter_channels_user_admins(candidates, user_id)
    username = await _get_bot_username()
    add_url = f"https://t.me/{username}?startchannel&admin=post_messages"
    ready = "✅ <b>قنواتك الجاهزة</b> (البوت أدمن فيها): اختر وحدة من الأزرار تحت.\n\n" if mine else ""
    text = (
        "📢 <b>تشغيل الكويز بقناة</b>\n\n"
        f"{ready}"
        "➕ <b>قناة جديدة؟</b>\n"
        "1️⃣ اضغط «➕ أضف البوت لقناة» تحت.\n"
        "2️⃣ اختر القناة من قائمة تيليجرام (لازم تكون مالك القناة أو مشرف بصلاحية إضافة مشرفين).\n"
        "3️⃣ خلّي صلاحية «نشر الرسائل» مفعّلة وأكّد.\n"
        "4️⃣ ارجع لهون - رح يوصلك زر «🚀 ابدأ» تلقائياً (أو اضغط «🔄 تحديث»)."
    )
    return text, get_group_channel_picker_keyboard(share_id, mine, add_url=add_url)


async def _build_group_screen(share_id: str, user_id: int):
    """نص + كيبورد شاشة الغروبات: غروباتك (البوت فيها + أنت أدمن لايف) + زر إضافة البوت لغروب."""
    candidates = await list_postable_groups()
    mine = await _filter_channels_user_admins(candidates, user_id)
    username = await _get_bot_username()
    add_url = f"https://t.me/{username}?startgroup=gqadd&admin=restrict_members"
    ready = "✅ <b>غروباتك الجاهزة</b> (البوت فيها): اختر وحدة من الأزرار تحت.\n\n" if mine else ""
    text = (
        "👥 <b>تشغيل الكويز بغروب</b>\n\n"
        f"{ready}"
        "الإعداد كله بيصير <b>هون بخاص البوت</b> - ما بتحتاج تفتح الغروب.\n\n"
        "➕ <b>غروب جديد؟</b>\n"
        "1️⃣ اضغط «➕ أضف البوت لغروب» تحت واختر الغروب.\n"
        "2️⃣ أكّد إضافة البوت كمشرف مع صلاحية «تقييد الأعضاء» اللازمة لقفل المجموعة أثناء الاختبار.\n"
        "3️⃣ ارجع لهون - رح يوصلك زر «🚀 ابدأ» تلقائياً (أو اضغط «🔄 تحديث»).\n\n"
        "⚠️ لإضافة البوت كمشرف، لازم تكون مالك الغروب أو مشرف عندك صلاحية إضافة مشرفين.\n"
        "🔒 البوت موجود من قبل؟ فعّل له «تقييد الأعضاء» من إعدادات مشرفي الغروب إذا بدك تستخدم القفل.\n"
        "📌 البوت موجود أصلاً بالغروب ومش ظاهر باللائحة؟ اكتب داخل الغروب مرة وحدة:\n"
        f"<code>/groupquiz {html.escape(share_id)}</code>"
    )
    kb = get_group_channel_picker_keyboard(
        share_id, mine, add_url=add_url, refresh_prefix="gqgr", add_text="➕ أضف البوت لغروب",
    )
    return text, kb


@router.callback_query(F.data.startswith("gqg:"))
async def show_group_share_steps(call: types.CallbackQuery):
    """زر \"👥 شغّل الكويز ضمن غروب\": لائحة غروباتك الجاهزة + إضافة البوت لغروب جديد.
    بعدها اختيار الغروب (`gqchp:`) بيبدأ الإعداد هون بالخاص (نفس مسار القنوات)."""
    try:
        if call.message.chat.type != "private":
            await call.answer("افتح البوت بالخاص.", show_alert=True)
            return
        share_id = call.data.split(":", 1)[1]
        await _set_pending_channel_intent(call.from_user.id, share_id)
        text, kb = await _build_group_screen(share_id, call.from_user.id)
        await call.message.answer(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        log_error(logger, f"Error in show_group_share_steps: {e}", exception=e)
    finally:
        try:
            await call.answer()
        except Exception:
            pass


@router.callback_query(F.data.startswith("gqgr:"))
async def refresh_groups_screen(call: types.CallbackQuery):
    """زر \"🔄 تحديث\" بشاشة الغروبات - بتتعدّل بمكانها."""
    try:
        if call.message.chat.type != "private":
            await call.answer("افتح البوت بالخاص.", show_alert=True)
            return
        share_id = call.data.split(":", 1)[1]
        await _set_pending_channel_intent(call.from_user.id, share_id)
        text, kb = await _build_group_screen(share_id, call.from_user.id)
        try:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            await call.answer("ما في جديد بعد.", show_alert=False)
            log_info(logger, f"refresh_groups_screen: unchanged ({e})")
            return
    except Exception as e:
        log_error(logger, f"Error in refresh_groups_screen: {e}", exception=e)
    finally:
        try:
            await call.answer()
        except Exception:
            pass


@router.callback_query(F.data.startswith("gqchl:"))
async def list_channels_for_share(call: types.CallbackQuery):
    """زر \"📢 شارك مع قناة\" (من handlers/sharing.py): شاشة القنوات الجاهزة + خطوات إضافة
    البوت لقناة جديدة. بيسجّل نيّة المستخدم (`_set_pending_channel_intent`) حتى يوصله
    زر البدء تلقائياً لما البوت ينضاف أدمن (`track_bot_chat_membership`)."""
    try:
        if call.message.chat.type != "private":
            await call.answer("افتح البوت بالخاص لاختيار القناة.", show_alert=True)
            return
        share_id = call.data.split(":", 1)[1]
        await _set_pending_channel_intent(call.from_user.id, share_id)
        text, kb = await _build_channel_screen(share_id, call.from_user.id)
        await call.message.answer(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        log_error(logger, f"Error in list_channels_for_share: {e}", exception=e)
    finally:
        try:
            await call.answer()
        except Exception:
            pass


@router.callback_query(F.data.startswith("gqchr:"))
async def refresh_channels_screen(call: types.CallbackQuery):
    """زر \"🔄 تحديث\": نفس الشاشة، بتنعدّل بمكانها (مش رسالة جديدة كل ضغطة)."""
    try:
        if call.message.chat.type != "private":
            await call.answer("افتح البوت بالخاص.", show_alert=True)
            return
        share_id = call.data.split(":", 1)[1]
        await _set_pending_channel_intent(call.from_user.id, share_id)
        text, kb = await _build_channel_screen(share_id, call.from_user.id)
        try:
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            # "message is not modified" = ما في قناة جديدة - مش خطأ حقيقي
            await call.answer("ما في جديد بعد.", show_alert=False)
            log_info(logger, f"refresh_channels_screen: unchanged ({e})")
            return
    except Exception as e:
        log_error(logger, f"Error in refresh_channels_screen: {e}", exception=e)
    finally:
        try:
            await call.answer()
        except Exception:
            pass


@router.callback_query(F.data.startswith("gqchp:"))
async def pick_channel_for_share(call: types.CallbackQuery):
    """اختيار قناة من اللائحة -> نفس منطق بدء الجلسة بالضبط (`_start_session_setup`)
    بس بـ `chat_id` القناة، وشاشات الإعداد بتنعرض هون بالخاص."""
    try:
        if call.message.chat.type != "private":
            await call.answer("افتح البوت بالخاص لاختيار القناة.", show_alert=True)
            return
        _, share_id, raw_chat_id = call.data.split(":", 2)
        chat_id = int(raw_chat_id)
        user = call.from_user

        # فحص وقائي (بند 3.3): صلاحية البوت **قبل** ما نبلّش الجلسة أصلاً، بدل ما
        # نكتشف الفشل بعد إرسال أول سؤال. ما بيغطّي كل الحالات (السجل ممكن يكون
        # قديم لو فاتنا تحديث) - فشل الإرسال الفعلي = بند 5 (لسا ما انكتب).
        membership = await get_chat_membership(chat_id)
        chat_type = (membership or {}).get("chat_type")
        is_channel = chat_type == "channel"
        if is_channel:
            if membership.get("status") != "administrator":
                await call.answer("⚠️ البوت مش أدمن بهالقناة حالياً.", show_alert=True)
                return
            if membership.get("can_post_messages") is not True:
                await call.answer("⚠️ البوت ما عنده صلاحية «نشر الرسائل» بهالقناة.", show_alert=True)
                return
        elif chat_type in ("group", "supergroup"):
            if membership.get("status") not in ("member", "administrator"):
                await call.answer("⚠️ البوت مش عضو بهالغروب حالياً.", show_alert=True)
                return
        else:
            await call.answer("⚠️ هالمحادثة غير معروفة للبوت.", show_alert=True)
            return
        if not await is_group_admin(chat_id, user.id):
            await call.answer("⛔ لازم تكون أدمن بهالمحادثة.", show_alert=True)
            return

        title = membership.get("chat_title") or ("القناة" if is_channel else "الغروب")
        try:
            # يمنع ضغطتين متتاليتين (الثانية كانت رح تنرفض بـ session-exists على أي حال)
            picked_label = "📢 القناة المختارة" if is_channel else "👥 الغروب المختار"
            await call.message.edit_text(f"{picked_label}: <b>{html.escape(title)}</b>", parse_mode="HTML")
        except Exception:
            pass
        await _start_session_setup(
            ui_msg=call.message,
            chat_id=chat_id,
            user=user,
            share_id=share_id,
            message_thread_id=None,
            is_channel=is_channel,
            dest_title=title,
        )
    except Exception as e:
        log_error(logger, f"Error in pick_channel_for_share: {e}", exception=e)
    finally:
        try:
            await call.answer()
        except Exception:
            pass


async def _cancel_session_on_first_send_failure(session: Dict[str, Any], error: Optional[Exception]) -> None:
    """يلغي الجلسة (`cancelled`) ويشرح للأدمن ليش - نداء لما يفشل إرسال **أول** سؤال.

    - الإلغاء شرطي بقاعدة البيانات (`claim_session_cancel`): لو جلسة خلصت/انلغت
      أصلاً بنفس اللحظة (مثلاً زر "إنهاء الآن") بننسحب بصمت بلا رسالة زايدة.
    - **وين منبعت التنبيه؟** بالقناة: خاص الأدمن (`started_by`) بس - القناة نفسها
      غالباً هي سبب الفشل (البوت ما بيقدر ينشر فيها) وما بدنا نضجّ مشتركيها
      برسالة خطأ إدارية. بالغروب: بالغروب نفسه (مع التوبيك) لأنه الأدمن ممكن ما
      يكون فتح البوت بالخاص أبداً، وإذا فشل هاد كمان بنجرّب الخاص.
    - القناة بتنعرف من `bot_chat_memberships.chat_type` (جلسات القنوات بتنبدأ
      حصراً من لائحة القنوات المبنية على هالجدول).
    """
    session_id = str(session["id"])
    if not await claim_session_cancel(session_id):
        return
    await restore_quiz_lock(session_id, int(session["chat_id"]))
    await clear_session_questions_cache(session_id)
    log_error(logger, f"Session {session_id} cancelled: first question could not be sent ({error})")

    chat_id = int(session["chat_id"])
    started_by = int(session["started_by"])
    membership = await get_chat_membership(chat_id)
    is_channel = bool(membership) and membership.get("chat_type") == "channel"

    reason = html.escape(str(error))[:300] if error else ""
    reason_line = f"\n\n<i>رد تيليجرام:</i> <code>{reason}</code>" if reason else ""

    if is_channel:
        title = html.escape((membership or {}).get("chat_title") or "القناة")
        text = (
            f"❌ تعذّر إرسال أول سؤال بالقناة <b>{title}</b>، فانلغت الجلسة.\n"
            "تأكد إنه البوت أدمن بالقناة مع صلاحية «نشر الرسائل»، وبعدها ابدأ الجلسة من جديد."
            f"{reason_line}"
        )
        await _safe_call(
            lambda: bot.send_message(chat_id=started_by, text=text, parse_mode="HTML"),
            what=f"notify admin of cancelled channel session {session_id}",
        )
        return

    text = (
        "❌ تعذّر إرسال أول سؤال، فانلغت الجلسة.\n"
        "تأكد إنه البوت عنده صلاحية إرسال الاستفتاءات (Send Polls) بالغروب، وبعدها ابدأ الجلسة من جديد."
        f"{reason_line}"
    )
    # الإعداد صار بخاص البوت: منبلّغ الأدمن بالخاص أولاً، وإذا فشل (ما فتح البوت) منبلّغ الغروب.
    sent = await _safe_call(
        lambda: bot.send_message(chat_id=started_by, text=text, parse_mode="HTML"),
        what=f"notify admin of cancelled session {session_id}",
    )
    if sent is None:
        await _safe_call(
            lambda: bot.send_message(
                chat_id=chat_id, text=text, parse_mode="HTML",
                message_thread_id=session.get("message_thread_id"),
            ),
            what=f"notify group (fallback) of cancelled session {session_id}",
        )


# ==================== الإرسال الموحّد ====================
async def send_next_group_question(session: Dict[str, Any]) -> bool:
    """يرسل السؤال التالي بالجلسة. نقطة الدخول الوحيدة للإرسال.

    بيرجّع True لو انبعت سؤال فعلاً، وFalse لو ما في شي ينبعت (خلصت الأسئلة،
    الجلسة مش active، أو منادٍ تانٍ سبقنا للسؤال نفسه).
    """
    session_id = str(session["id"])
    if session.get("status") != "active":
        return False

    if session.get("lock_chat"):
        try:
            await acquire_quiz_lock(session)
        except Exception:
            await _cancel_session_on_first_send_failure(session, None)
            return False

    questions = await get_session_questions(session)
    idx = int(session.get("questions_sent") or 0)
    total = int(session.get("total_questions") or len(questions))

    if idx >= min(total, len(questions)):
        await finish_group_session(session)
        return False

    # الحجز أولاً: لو النبضة الدورية ومعالج إغلاق الـ poll اشتغلوا سوا، واحد بس
    # بينجح - راجع claim_question_slot.
    #
    # ⚠️ next_due محسوبة لكلا الوضعين هلق، مو fixed_interval بس. تيليجرام ما
    # بيبعت تحديث poll تلقائياً عند إغلاق الاستفتاء بانتهاء open_period (بيبعت
    # بس عند bot.stop_poll اليدوي - موثّق رسمياً)، فـ chain_to_timer ما ممكن
    # يعتمد على @router.poll() لوحده (كان هاد سبب توقّفه عند أول سؤال فعلياً).
    # النبضة الدورية هلق هي الضامن الوحيد للتقدّم بكلا الوضعين؛ @router.poll()
    # ضل موجود كمسار تسريع اختياري بس (idempotent، بلا ضرر لو ما اشتغل).
    pacing_mode = session.get("pacing_mode")
    if pacing_mode == "fixed_interval":
        wait_seconds = session.get("question_interval_seconds")
    elif pacing_mode == "chain_to_timer":
        wait_seconds = session.get("question_timer_seconds")
    elif pacing_mode == "burst":
        wait_seconds = BURST_RECOVERY_SECONDS
    else:
        wait_seconds = None
    next_due = _utc_iso(int(wait_seconds)) if wait_seconds else None
    if not await claim_question_slot(session_id, idx, next_due):
        return False

    send_errors: list = []
    poll_msg = await _safe_call(
        lambda: send_quiz_poll(
            chat_id=int(session["chat_id"]),
            user_id=int(session["started_by"]),
            q=questions[idx],
            idx=idx,
            total=total,
            control_kb=get_group_question_control_keyboard(session_id),
            quiz_id=str(session["quiz_id"]),
            open_period=session.get("question_timer_seconds"),
            is_anonymous=bool(session.get("poll_is_anonymous", False)),
            message_thread_id=session.get("message_thread_id"),
            poll_meta={
                "session_id": session_id,
                "is_group": True,
                "sent_at_ms": int(time.time() * 1000),
                # قرار: صفر تتبّع/تسجيل إجابات للجلسات بدون مؤقّت (الاستفتاءات
                # مصممة تضل مفتوحة للأبد، وتيليجرام نفسها كافية لعرض الجواب
                # الصح لكل طالب لحاله - ما في داعي نخزّن هوية حدا). مخزّنة هون
                # (مش عبر استعلام قاعدة بيانات إضافي بمعالج الإجابة) تفادياً
                # لقراءة DB على كل جواب واصل.
                "has_timer": bool(session.get("question_timer_seconds")),
                "track_answers": not bool(session.get("poll_is_anonymous")),
            },
        ),
        what=f"send group question {idx} (session {session_id})",
        on_error=send_errors.append,
    )
    if poll_msg is None:
        log_error(logger, f"Question {idx} burned for session {session_id} (send failed after claim)")
        if idx == 0:
            # بند 5: فشل أول سؤال (غالباً البوت ناقص صلاحية إرسال استفتاءات) =
            # مشكلة ثابتة مش عابرة - بدل ما النبضة تحرق باقي الأسئلة سؤال كل دورة
            # وتوهم الأدمن إنه الجلسة شغّالة، منلغيها فوراً ومنشرح الأدمن ليش.
            # (فشل سؤال بمنتصف الجلسة بعد ما انبعت غيره = غالباً عابر - بيضل
            # السلوك القديم: يتحرق سؤال وحدة وبتكمل الجلسة.)
            await _cancel_session_on_first_send_failure(session, send_errors[-1] if send_errors else None)
        return False
    await _remember_open_poll(session_id, int(session["chat_id"]), poll_msg.message_id)
    return True


async def finish_group_session(session: Dict[str, Any], manual: bool = False) -> None:
    """ينهي الجلسة وينشر الترتيب النهائي.

    الترتيب هون مقصود (نفس ترتيب الخطة): 1) `claim_session_finish` أولاً - هاد
    اللي بيمنع `handle_group_poll_closed` من إرسال سؤال إضافي لو التحديث وصل
    بنفس اللحظة (الفحص هناك على `status == 'active'`)، **وبيضمن كمان إنه نداء
    واحد بس من بين عدة نداءات متزامنة محتملة (نهاية طبيعية + زر يدوي من أكتر
    من أدمن + مسار تسريع @router.poll()) هو يلي بينشر رسالة النهاية** - تحديث
    مشروط بقاعدة البيانات (`status IN ('waiting','active')` لحظة التنفيذ)، مش
    فحص نسخة محلية (stale) من `session` زي قبل. 2) `bot.stop_poll` على أي
    poll لسا مفتوح - يقفل الاستفتاء نفسه بواجهة تيليجرام حتى لو حدا لسا
    بيحاول يجاوب. 3) نشر النتائج.
    """
    session_id = str(session["id"])
    if not await claim_session_finish(session_id):
        # جلسة تانية (سباق) سبقتنا بالإنهاء، أو الجلسة خلصت أصلاً - انسحب بصمت
        # بلا ما ننشر رسالة نهاية مكرّرة.
        return
    await restore_quiz_lock(session_id, int(session["chat_id"]))
    await clear_session_questions_cache(session_id)

    open_poll = await _pop_open_poll(session_id)
    # وضع burst: كل الاستفتاءات لازم تضل مفتوحة (آخر سؤال كمان) - بلا stop_poll.
    if open_poll and session.get("pacing_mode") != "burst":
        try:
            await bot.stop_poll(chat_id=open_poll["chat_id"], message_id=open_poll["message_id"])
        except Exception as e:
            # متوقّع لو السؤال أصلاً سكّر لحاله (خلص open_period) قبل ما توصل هون -
            # aiogram بترمي BadRequest بهالحالة، مش خطأ حقيقي.
            log_error(logger, f"stop_poll failed for session {session_id} (likely already closed): {e}")

    cert_rows: Optional[list] = None
    cert_top_n = 0
    if session.get("poll_is_anonymous"):
        # ما في ولا صف بـ group_quiz_participants أصلاً بهالوضع (تيليجرام ما
        # بيبعت poll_answer للاستفتاءات المجهولة) - رسالة صريحة بدل ما توحي
        # "لا يوجد أي إجابة مسجّلة" إنه محدا جاوب، بينما هو قرار مقصود بالإعداد.
        text = "🏁 خلصت الجلسة (نمط مجهول بالكامل - بلا نقاط ولا ترتيب فردي حسب الإعداد المُختار)."
        needs_manual_suffix = True
    elif session.get("pacing_mode") == "burst" and not session.get("question_timer_seconds"):
        # burst بلا مؤقّت: الجلسة بتنتهي لحظة إرسال آخر سؤال (الطلاب لسا ما جاوبوا)،
        # فنشر ترتيب هون بيكون مبكّر ومضلّل - منكتفي برسالة إنهاء الإرسال. الإجابات
        # بتضل منتتبّعة (track_answers) وبتظهر بلوحة الأدمن.
        if manual:
            text = "⏹️ تم إيقاف إرسال الأسئلة يدوياً من قبل الأدمن. الاستفتاءات المرسلة ضلّت مفتوحة، أي حدا لسا يقدر يجاوب عليها."
        else:
            text = "🏁 انتهت الأسئلة. الاستفتاءات المرسلة ضلّت مفتوحة، أي حدا لسا يقدر يجاوب عليها."
        needs_manual_suffix = False
    else:
        top_n = max(GROUP_TOP_N_MIN, min(GROUP_TOP_N_MAX, int(session.get("top_n") or GROUP_TOP_N_MAX)))
        rows = await get_leaderboard(session_id, limit=top_n)
        show_names = session.get("show_names", True)  # True لو العمود لسا مش مضاف (fail-open نفس نمط باقي الإعدادات بالمشروع)
        if not rows:
            text = "🏁 خلصت الجلسة - ما في أي إجابة مسجّلة."
        else:
            medals = ["🥇", "🥈", "🥉"]
            lines = [f"🏁 <b>انتهت الجلسة! الترتيب النهائي (أول {top_n}):</b>", ""]
            for i, r in enumerate(rows):
                if show_names:
                    raw_who = r.get("first_name") or (f"@{r['username']}" if r.get("username") else str(r["user_id"]))
                    who = html.escape(raw_who)
                else:
                    who = f"لاعب {i + 1}"
                rank = medals[i] if i < len(medals) else f"{i + 1}."
                lines.append(f"{rank} {who} — {r.get('score', 0)} نقطة")
            if not session.get("question_timer_seconds"):
                lines += ["", "<i>الاستفتاءات ضلّت مفتوحة، بس الإجابات بعد هالترتيب ما بتنحسب.</i>"]
            text = "\n".join(lines)
            cert_rows, cert_top_n = rows, top_n
        needs_manual_suffix = True
        try:
            await redis_client.set(f"{_FINISHED_SESSION_PREFIX}{session_id}", "1", ex=_FINISHED_SESSION_TTL)
        except Exception as e:
            log_error(logger, f"Redis error setting finished marker for session {session_id}: {e}")
    if manual and needs_manual_suffix:
        text += "\n\n<i>(أُنهيت يدوياً من قبل الأدمن)</i>"

    await _safe_call(
        lambda: bot.send_message(
            chat_id=int(session["chat_id"]),
            text=text,
            parse_mode="HTML",
            message_thread_id=session.get("message_thread_id"),
        ),
        what=f"send leaderboard (session {session_id})",
    )
    if cert_rows:
        # 🎓 بعد الترتيب النصي وبمهمة منفصلة: فشل/بطء الرسم ما بيعطّل الإنهاء ولا النبضة.
        spawn(_send_certificates(session, cert_rows, cert_top_n))


# ==================== إجابات الأعضاء ====================
@router.poll_answer()
async def handle_group_poll_answer(poll_answer: types.PollAnswer):
    """نسخة الغروب من معالج الإجابات.

    ما في فلتر نوع محادثة على `poll_answer` (التحديث بلا chat)، فمنميّز عبر
    وجود العلم الصريح `is_group=True` بسجل Redis. الاختبارات الفردية تستخدم
    `session_id` أيضاً، لذلك لا يصلح كمعيار للتمييز بينها وبين الجماعية.
    أي استفتاء فردي ينتقل لمعالج `handlers/quiz_runner.py` عبر SkipHandler.

    بخلاف النمط الفردي، ما في فحص `user.id != quiz_info["user_id"]` هون -
    أي عضو بالغروب إجابته محسوبة.
    """
    try:
        raw = await redis_client.get(f"poll:{poll_answer.poll_id}")
    except Exception as e:
        log_error(logger, f"Redis error reading poll record: {e}")
        raise SkipHandler()

    if not raw:
        # تشخيص مؤقّت (GQ_ANS): غياب سجل Redis إما إجابة فردية قديمة أو سجل جماعي انتهى/ضاع.
        logger.info("GQ_ANS no_redis_record poll=%s user=%s", poll_answer.poll_id, getattr(poll_answer.user, "id", None))
        raise SkipHandler()
    try:
        info = json.loads(raw)
    except Exception:
        raise SkipHandler()
    # A session_id also exists on private quizzes. Only group polls carry is_group.
    if info.get("is_group") is not True or not info.get("session_id"):
        raise SkipHandler()

    # تشخيص مؤقّت (GQ_ANS): وصل صوت على استفتاء جماعي (يعني التحديث وصل لمعالجنا فعلاً).
    logger.info(
        "GQ_ANS received session=%s q=%s user=%s options=%s has_timer=%s track=%s",
        info.get("session_id"), info.get("question_index"), getattr(poll_answer.user, "id", None),
        list(poll_answer.option_ids or []), info.get("has_timer"), info.get("track_answers"),
    )

    if not (info.get("has_timer") or info.get("track_answers")):
        logger.info("GQ_ANS skipped reason=no_tracking_flag session=%s", info.get("session_id"))
        # سجلات استفتاءات قديمة (قبل إضافة track_answers) بلا مؤقّت: نفس السلوك
        # السابق (بلا تتبّع). الجلسات التنافسية الجديدة بتحمل track_answers=True
        # فبتنتتبّع حتى بدون مؤقّت.
        return

    try:
        if await redis_client.exists(f"{_FINISHED_SESSION_PREFIX}{info['session_id']}"):
            logger.info("GQ_ANS skipped reason=after_final_ranking session=%s", info.get("session_id"))
            return
    except Exception as e:
        log_error(logger, f"Redis error checking finished marker: {e}")

    # من هون وطالع: هاي إجابة على استفتاء جلسة جماعية عندها مؤقّت - مسؤوليتنا نحنا.
    try:
        if not poll_answer.option_ids:
            return  # سحب الصوت (retract) - منتجاهله، الإجابة الأولى هي المعتمدة
        user = poll_answer.user
        if not user:
            return

        selected = int(poll_answer.option_ids[0])
        is_correct = selected == int(info["correct_option_id"])
        question_index = int(info.get("question_index", 0))
        answer_time_ms = max(0, int(time.time() * 1000) - int(info.get("sent_at_ms") or 0))

        inserted = await record_answer(
            session_id=info["session_id"],
            user_id=user.id,
            question_index=question_index,
            poll_id=poll_answer.poll_id,
            selected_option=selected,
            is_correct=is_correct,
        )
        # `record_answer` بترجع False لو الصف موجود أصلاً (قيد الـ unique) - يعني
        # هاي محاولة تانية لنفس السؤال، فما منحدّث النقاط مرتين.
        logger.info("GQ_ANS recorded=%s session=%s q=%s user=%s", inserted, info["session_id"], question_index, user.id)
        if inserted:
            await bump_participant(
                session_id=info["session_id"],
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                is_correct=is_correct,
                answer_time_ms=answer_time_ms,
                last_name=getattr(user, "last_name", None),
            )
    except Exception as e:
        log_error(logger, f"Error in handle_group_poll_answer: {e}", exception=e)


# ==================== مسار تسريع اختياري: إغلاق يدوي للاستفتاء ====================
@router.poll()
async def handle_group_poll_closed(poll: types.Poll):
    """تحديث `Poll` بيوصل لأي تغيّر بحالة الاستفتاء - نحنا مهتمّين بلحظة
    `is_closed=True` بس.

    ⚠️ **هاد مسار تسريع اختياري، مش الآلية الأساسية لوضع `chain_to_timer`.**
    تيليجرام ما بيبعت هالتحديث عند الإغلاق التلقائي بانتهاء `open_period` -
    موثّق رسمياً: "Bots receive only updates about **manually** stopped
    polls" - يعني بس لما ينادى `bot.stop_poll` صراحة. تأكّدنا من هاد عملياً:
    بكل جلسات `chain_to_timer` المجرّبة، `questions_sent` علقت على ١ وما
    تحرّكت. النبضة الدورية (`group_quiz_heartbeat_loop`) هلق هي الضامن
    الوحيد للتقدّم بكلا الوضعين (راجع `send_next_group_question`). هالمعالج
    تركناه بلا ضرر - لو تيليجرام بعت التحديث بأي ظرف (مثلاً لو حدا نادى
    `bot.stop_poll` يدوياً بمكان تاني)، بينفّذ فوراً بدل انتظار النبضة، وبما
    إنه `claim_question_slot` idempotent فما في خطر إرسال مزدوج.

    هاد التحديث بلا `chat` كمان (نفس `poll_answer`)، فالتمييز عبر سجل Redis.
    ما منستخدم `router.message.filter` (ما بينطبق على `poll` أصلاً) ولا داعي
    لـ SkipHandler هون - ما في معالج `poll` تاني بالمشروع كله يتعارض معه.
    """
    if not poll.is_closed:
        return
    try:
        raw = await redis_client.get(f"poll:{poll.id}")
    except Exception as e:
        log_error(logger, f"Redis error reading closed-poll record {poll.id}: {e}")
        return
    if not raw:
        return

    try:
        info = json.loads(raw)
    except Exception:
        return
    session_id = info.get("session_id")
    if not session_id:
        return  # استفتاء النمط الفردي - مش شغلنا هون

    session = await get_session(session_id)
    # ⚠️ لازم نتأكد إن الجلسة لسا active قبل الإرسال - لو انضغط زر "إنهاء الجلسة
    # الآن" بين لحظة إغلاق آخر poll ولحظة وصول هالتحديث، ما بدنا نبعت سؤال زيادة
    # بعد ما الجلسة خلصت رسمياً (راجع نقطة 6 بالخطة).
    if not session or session.get("status") != "active" or session.get("pacing_mode") != "chain_to_timer":
        return

    await send_next_group_question(session)


# ==================== وضع burst: كل الأسئلة دفعة واحدة ====================
async def run_burst_session(session_id: str) -> None:
    """يرسل كل أسئلة الجلسة تباعاً بفاصل `BURST_GAP_SECONDS` بين السؤال والتاني.

    - الحجز الذرّي (`claim_question_slot`) جوا `send_next_group_question` بيضمن ما في إرسال
      مزدوج حتى لو اشتغل منفّذان بالغلط.
    - بيوقف لما الجلسة تخلص/تنلغى (زر "إنهاء الآن" أو فشل أول سؤال)، أو لما ما يصير تقدّم؛
      بهالحالة الأخيرة النبضة بتعيد تشغيله لاحقاً (`group_quiz_heartbeat_tick`).
    - بعد آخر سؤال بياخد استدعاء إضافي بينهي الجلسة وبينشر رسالة النهاية (استفتاءات مفتوحة).
    """
    if session_id in _BURST_RUNNING:
        return
    _BURST_RUNNING.add(session_id)
    try:
        while True:
            session = await get_session(session_id)
            if not session or session.get("status") != "active" or session.get("pacing_mode") != "burst":
                return
            before = int(session.get("questions_sent") or 0)
            sent = await send_next_group_question(session)
            if not sent:
                fresh = await get_session(session_id)
                # خلصت/انلغت، أو ما صار أي تقدّم -> نوقف (النبضة بتتولّى الاستئناف لو لزم)
                if not fresh or fresh.get("status") != "active" or int(fresh.get("questions_sent") or 0) <= before:
                    return
            await asyncio.sleep(BURST_GAP_SECONDS)
    except Exception as e:
        log_error(logger, f"Burst runner failed for session {session_id}: {e}", exception=e)
    finally:
        _BURST_RUNNING.discard(session_id)


# ==================== النبضة الدورية: وضع fixed_interval ====================
async def group_quiz_heartbeat_tick() -> None:
    """دورة واحدة: يجلب كل الجلسات اللي حان دورها (بكلا الوضعين `fixed_interval`
    و`chain_to_timer` - راجع الملاحظة بأعلى الملف) ويبعت سؤالها التالي.

    مستوى التطبيق كله (استعلام واحد لكل الجلسات المستحقة) وليس
    `asyncio.create_task` طويل العمر لكل جلسة على حدة - البوت بيتنقّل بين
    منصات استضافة وأي إعادة تشغيل بمنتصف جلسة كانت رح تفقد أي حالة بالذاكرة.
    `next_question_due_at` نفسه مخزَّن بقاعدة البيانات، فالاستئناف بعد إعادة
    تشغيل تلقائي بلا أي كود إضافي.
    """
    await recover_quiz_locks()
    sessions = await get_due_sessions()
    for session in sessions:
        try:
            if session.get("pacing_mode") == "burst":
                # منفّذ burst انقطع (إعادة تشغيل مثلاً) - نعيد تشغيله ويكمّل من questions_sent.
                sid = str(session["id"])
                if sid not in _BURST_RUNNING:
                    spawn(run_burst_session(sid), name=f"gq_burst_{sid}")
                continue
            await send_next_group_question(session)
        except Exception as e:
            log_error(logger, f"Heartbeat failed for session {session.get('id')}: {e}", exception=e)


async def group_quiz_heartbeat_loop(interval_seconds: int = HEARTBEAT_INTERVAL_SECONDS) -> None:
    """حلقة خلفية دائمة - تُطلق مرة واحدة عند إقلاع السيرفر (نفس نمط
    `scheduled_analytics_batch_loop`/`scheduled_cleanup_loop` بـ webhook_server.py).

    ⚠️ `HEARTBEAT_INTERVAL_SECONDS` (معرَّف بأعلى الملف) هو نفسه المستخدم بحساب
    `MIN_QUESTION_INTERVAL_SECONDS` - مصدر حقيقة واحد بدل رقمين منفصلين ممكن
    ينحرفوا عن بعض. لو غيّرت هالقيمة هون، غيّرها هناك كمان (أو مرّر
    `interval_seconds` صراحة هون فقط لغرض اختبار - بدون ما تلمس الثابت الأساسي).
    """
    while True:
        try:
            await group_quiz_heartbeat_tick()
        except Exception as e:
            log_error(logger, f"Error inside group quiz heartbeat loop: {e}", exception=e)
        await asyncio.sleep(interval_seconds)



# ==================== 🎓 شهادات التقدير (المرحلة ب) ====================
# تُفعَّل من شاشة الإعداد (gqs:<sid>:o:cert) وتُرسل كصور بعد الترتيب النهائي مباشرة.
# الرسم بالسيرفر الثاني (أو محلياً كاحتياط) عبر services/certificate_renderer.py، وأي فشل هناك
# ما بيأثّر على إنهاء الجلسة ولا على الترتيب النصي (مهمة منفصلة + try/except شامل).
CERT_RENDER_TIMEOUT_SECONDS = 60
_CERT_TEXT_WAIT_PREFIX = "gq:cert_text_wait:"
_CERT_TEXT_WAIT_TTL = 300
_CERT_PREVIEW_COOLDOWN_PREFIX = "gq:cert_preview:"
_CERT_PREVIEW_COOLDOWN_SECONDS = 8
_CERT_CANCEL_WORDS = {"إلغاء", "الغاء", "cancel"}

# رسائل الأدمن النصية (اسم الاختبار/الجهة) بتوصل للخاص، و`router` الأساسي محصور بالغروبات،
# فلها router مستقل محصور بالخاص (مسجّل بـ main.py قبل payments/start).
group_quiz_cert_router = Router()
group_quiz_cert_router.message.filter(F.chat.type == "private")


async def _render_cert(payload: Dict[str, Any]) -> bytes:
    # استيراد كسول: PIL ما بيتحمّل ببداية البوت إلا لو استُخدمت الميزة فعلاً (ذاكرة 512MB).
    from services.certificate_renderer import render_certificate_async
    return await render_certificate_async(payload)


async def _handle_cert_special(
    call: types.CallbackQuery, session: Dict[str, Any], session_id: str, value: str, state: Dict[str, Any], *, is_channel: bool,
) -> None:
    """أزرار الشهادات اللي ما بتحفظ إعداداً: pv معاينة · te تعديل اسم الاختبار · ie تعديل الجهة."""
    if is_channel or not await group_cert_schema_ready():
        await call.answer("⚠️ هالإعداد غير متاح هون", show_alert=True)
        return
    if value == "pv":
        await _send_cert_preview(call, session, session_id)
        return
    field = "title" if value == "te" else "issuer"
    try:
        await redis_client.set(
            f"{_CERT_TEXT_WAIT_PREFIX}{call.from_user.id}",
            json.dumps({"sid": session_id, "field": field, "message_id": call.message.message_id}),
            ex=_CERT_TEXT_WAIT_TTL,
        )
    except Exception as e:
        log_error(logger, f"Redis error arming cert text wait: {e}")
        await call.answer("❌ تعذّر، جرّب بعد شوي", show_alert=True)
        return
    if field == "title":
        prompt = (f"✏️ ابعتلي <b>اسم الاختبار</b> اللي بدك ينكتب على الشهادة (حتى {CERT_TITLE_MAX_CHARS} حرف).\n"
                  "الاسم إجباري. لإلغاء التعديل ابعت «إلغاء».")
    else:
        prompt = (f"✍️ ابعتلي اسم <b>الجهة أو المعلّم</b> (حتى {CERT_ISSUER_MAX_CHARS} حرف) ليظهر أسفل الشهادة.\n"
                  "ابعت «-» لحذفه، أو «إلغاء» لتتراجع.")
    await _safe_call(lambda: call.message.answer(prompt, parse_mode="HTML"), what="cert text prompt")


async def _send_cert_preview(call: types.CallbackQuery, session: Dict[str, Any], session_id: str) -> None:
    """معاينة تقريبية بإعدادات الشهادة الحالية، بخاص الأدمن فقط (ما بتنرسل للغروب). بتستخدم اسم الأدمن نفسه كعيّنة."""
    try:
        acquired = await redis_client.set(
            f"{_CERT_PREVIEW_COOLDOWN_PREFIX}{call.from_user.id}", "1", nx=True, ex=_CERT_PREVIEW_COOLDOWN_SECONDS)
    except Exception as e:
        log_error(logger, f"Redis error on cert preview cooldown: {e}")
        acquired = True
    if not acquired:
        await call.answer("⏳ استنى شوي قبل معاينة جديدة", show_alert=True)
        return
    await call.answer("⏳ عم جهّز المعاينة...")
    try:
        header = await _load_setup_header(session_id)
        cfg = cert_normalize_config(session.get("cert_config"), default_title=header.get("title") or CERT_DEFAULT_TITLE)
        total = max(1, int(session.get("total_questions") or 10))
        payload = cert_build_payload(
            cfg,
            name=cert_display_name(call.from_user.first_name, getattr(call.from_user, "last_name", None), call.from_user.username, call.from_user.id),
            group_name=header.get("dest_title") or "", rank=1, score=total, total=total,
            date_text=cert_format_date(datetime.datetime.now(datetime.timezone.utc)), preview=True,
        )
        await _safe_call(lambda: bot.send_chat_action(call.message.chat.id, "upload_photo"), what="cert preview action")
        png = await asyncio.wait_for(_render_cert(payload), timeout=CERT_RENDER_TIMEOUT_SECONDS)
        await bot.send_photo(
            chat_id=call.message.chat.id,
            photo=types.BufferedInputFile(png, filename="certificate_preview.png"),
            caption="👁 معاينة تقريبية للشهادة (الاسم والمركز والدرجة هون للتجربة بس).",
        )
    except Exception as e:
        log_error(logger, f"cert preview failed for session {session_id}: {e}", exception=e)
        await _safe_call(lambda: call.message.answer("❌ تعذّر رسم المعاينة هلق، جرّب بعد شوي."), what="cert preview failed notice")


async def _awaiting_cert_text(msg: types.Message) -> Any:
    """فلتر: الأدمن ضغط «اسم الاختبار/الجهة» وما أرسل النص بعد. الأوامر (/...) ما بتنلتقط."""
    text = getattr(msg, "text", None)
    if not text or text.startswith("/") or not getattr(msg, "from_user", None):
        return False
    try:
        raw = await redis_client.get(f"{_CERT_TEXT_WAIT_PREFIX}{msg.from_user.id}")
        data = json.loads(raw) if raw else None
    except Exception as e:
        log_error(logger, f"Redis error reading cert text wait: {e}")
        return False
    if not isinstance(data, dict) or data.get("field") not in {"title", "issuer"} or not data.get("sid"):
        return False
    return {"cert_wait": data}


@group_quiz_cert_router.message(F.text, _awaiting_cert_text)
async def receive_cert_text(msg: types.Message, cert_wait: Dict[str, Any]):
    user_id = msg.from_user.id
    key = f"{_CERT_TEXT_WAIT_PREFIX}{user_id}"
    try:
        raw_text = (msg.text or "").strip()
        if raw_text.lower() in _CERT_CANCEL_WORDS:
            await redis_client.delete(key)
            await msg.answer("تمام، ما غيّرت شي.")
            return
        session_id = str(cert_wait["sid"])
        session = await get_session(session_id)
        if (not session or session["status"] != "waiting" or int(session["started_by"]) != user_id
                or not await is_group_admin(int(session["chat_id"]), user_id, fresh=True)):
            await redis_client.delete(key)
            await msg.answer("⚠️ هالجلسة ما عادت متاحة.")
            return
        field = cert_wait["field"]
        if field == "title":
            value = cert_clean_text(raw_text, CERT_TITLE_MAX_CHARS)
            if not value:
                await msg.answer("❌ اسم الاختبار إجباري، وما بيقبل إيموجي أو رموز فقط. جرّب اسم تاني، أو ابعت «إلغاء».")
                return
        else:
            value = "" if raw_text in {"-", "—"} else cert_clean_text(raw_text, CERT_ISSUER_MAX_CHARS)
        header = await _load_setup_header(session_id)
        cfg = cert_normalize_config(session.get("cert_config"), default_title=header.get("title") or CERT_DEFAULT_TITLE)
        cfg[field] = value
        if not await update_session(session_id, {"cert_config": cfg}):
            await msg.answer("❌ تعذر حفظ الإعدادات، جرّب بعد شوي.")
            return
        await redis_client.delete(key)
        session = {**session, "cert_config": cfg}
        success_label = "اسم الاختبار" if field == "title" else "الجهة / المعلّم"
        success_text = f"✅ تم حفظ {success_label} بنجاح."
        try:
            await msg.answer(success_text)
        except Exception as e:
            # نجاح الإشعار ليس شرطاً لعرض شاشة الإعدادات المحدّثة؛ الحفظ تم فعلاً.
            log_error(logger, f"Error sending certificate save confirmation: {e}")
        text, kb = await _setup_screen_view(session, "cert", is_channel=False)
        try:
            await bot.edit_message_text(chat_id=msg.chat.id, message_id=int(cert_wait["message_id"]), text=text,
                                        parse_mode="HTML", reply_markup=kb)
        except Exception:
            await msg.answer(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        log_error(logger, f"Error in receive_cert_text: {e}", exception=e)


async def _send_certificates(session: Dict[str, Any], rows: list, top_n: int) -> None:
    """يرسم ويرسل شهادات الأوائل كألبوم (أو صورة وحدة). لا يرمي أبداً: كل فشل بيتسجّل وبينتهي بصمت."""
    session_id = str(session.get("id"))
    try:
        cfg = cert_normalize_config(session.get("cert_config"))
        if not cfg["enabled"] or session.get("poll_is_anonymous") or session.get("show_names") is False:
            return
        winners = cert_select_winners(rows, cfg, top_n)
        if not winners:
            return
        chat_id = int(session["chat_id"])
        group_name = ""
        try:
            group_name = getattr(await bot.get_chat(chat_id), "title", None) or ""
        except Exception as e:
            log_error(logger, f"cert: get_chat failed for session {session_id}: {e}")
        total = max(1, int(session.get("total_questions") or 0))
        date_text = cert_format_date(datetime.datetime.now(datetime.timezone.utc))
        images = []
        for winner in winners:
            score = int(winner.get("score") or 0)
            payload = cert_build_payload(
                cfg,
                name=cert_display_name(winner.get("first_name"), winner.get("last_name"), winner.get("username"), winner.get("user_id")),
                group_name=group_name, rank=int(winner["rank"]), score=score, total=max(total, score), date_text=date_text,
            )
            try:
                images.append(await asyncio.wait_for(_render_cert(payload), timeout=CERT_RENDER_TIMEOUT_SECONDS))
            except Exception as e:
                log_error(logger, f"cert: render failed for session {session_id} rank {winner['rank']}: {e}")
        if not images:
            return
        caption = "🎓 <b>شهادات التقدير للأوائل</b>\n<i>شهادة تقدير غير رسمية من البوت.</i>"
        thread_id = session.get("message_thread_id")
        if len(images) == 1:
            await _safe_call(
                lambda: bot.send_photo(chat_id=chat_id, photo=types.BufferedInputFile(images[0], filename="certificate_1.png"),
                                       caption=caption, parse_mode="HTML", message_thread_id=thread_id),
                what=f"send certificate (session {session_id})")
        else:
            media = [
                types.InputMediaPhoto(
                    media=types.BufferedInputFile(img, filename=f"certificate_{i}.png"),
                    caption=caption if i == 1 else None, parse_mode="HTML" if i == 1 else None)
                for i, img in enumerate(images, start=1)
            ]
            await _safe_call(
                lambda: bot.send_media_group(chat_id=chat_id, media=media, message_thread_id=thread_id),
                what=f"send certificates (session {session_id})")
    except Exception as e:
        log_error(logger, f"cert: unexpected failure for session {session_id}: {e}", exception=e)


group_quiz_router = router
