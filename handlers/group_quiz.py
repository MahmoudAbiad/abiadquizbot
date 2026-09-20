# handlers/group_quiz.py
"""
==============================================================================
MODULE: تشغيل الكويز ضمن الغروبات
==============================================================================
يغطي:
- بدء الجلسة من داخل الغروب (`/start gq_<share_id>` أو `/groupquiz <share_id>`).
- شاشات الإعداد: نمط الأنونيميتي، إخفاء الأسماء، المؤقّت الداخلي، إيقاع الانتقال.
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
import time
from typing import Any, Callable, Dict, Optional

from aiogram import F, Router, types
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandObject

from config import bot, redis_client
from keyboards import (
    get_group_anonymity_keyboard,
    get_group_channel_picker_keyboard,
    get_group_names_privacy_keyboard,
    get_group_pacing_keyboard,
    get_group_question_control_keyboard,
    get_group_timer_keyboard,
)
from logger import get_logger, log_error, log_info
from services.group_permissions import invalidate_group_admin_cache, is_group_admin
from services.group_quiz_store import (
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
    record_answer,
    bump_participant,
    update_session,
    upsert_chat_membership,
)
from services.quiz_engine import send_quiz_poll
from supabase_helper import check_or_add_user, get_shared_quiz

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
    if not await is_group_admin(chat_id, user.id):
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

    # ⚠️ `source_title`/`title` نص حر من المستخدم ومنحطّه جوا parse_mode=HTML -
    # لازم escape (نفس عائلة بق أسماء الطلاب بـ finish_group_session).
    title = html.escape(shared.get("source_title") or shared.get("title") or "كويز")
    dest_line = f"📢 القناة: <b>{html.escape(dest_title)}</b>\n" if (is_channel and dest_title) else ""
    if is_channel:
        # بلا أي ادّعاء عن مين بيشوف \"من صوّت لمين\" بالقنوات (ما تحقّقنا منه) - بس
        # الحقيقة الثابتة: الاستفتاء غير المجهول = تيليجرام بتربط كل إجابة بصاحبها.
        competitive_line = (
            "🏆 <b>تنافسي</b>: نقاط وترتيب نهائي، بس الاستفتاء بيكون غير مجهول "
            "(تيليجرام بتربط كل إجابة بصاحبها - قيد من تيليجرام نفسها، مش قابل للتعطيل)."
        )
    else:
        competitive_line = (
            "🏆 <b>تنافسي</b>: نقاط وترتيب نهائي، بس أي عضو بالغروب يقدر يشوف \"من صوّت لمين\" "
            "على كل سؤال (قيد من تيليجرام نفسها، مش قابل للتعطيل)."
        )
    text = (
        f"📚 <b>{title}</b>\n"
        f"{dest_line}"
        f"عدد الأسئلة: {len(questions)}\n\n"
        "🎮 أول شي: شو نمط الجلسة؟\n"
        f"{competitive_line}\n"
        "🙈 <b>مجهول بالكامل</b>: محدا (ولا حتى أنا) بيعرف مين جاوب شو - بالمقابل بلا نقاط ولا ترتيب إطلاقاً."
    )
    await _safe_call(
        lambda: ui_msg.answer(text, parse_mode="HTML", reply_markup=get_group_anonymity_keyboard(str(session["id"]))),
        what="send anonymity screen",
    )


async def _begin_group_session(msg: types.Message, share_id: str) -> None:
    """مسار الغروب: الرسالة نفسها جوا الغروب الهدف (`/start gq_<id>` أو `/groupquiz`)."""
    user = msg.from_user
    if not user:
        return
    await _start_session_setup(
        ui_msg=msg,
        chat_id=msg.chat.id,
        user=user,
        share_id=share_id,
        message_thread_id=msg.message_thread_id,
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
        if not await is_group_admin(int(session["chat_id"]), call.from_user.id):
            await call.answer("⛔ للأدمن فقط", show_alert=True)
            return None
        return session

    if not await is_group_admin(chat.id, call.from_user.id):
        await call.answer("⛔ للأدمن فقط", show_alert=True)
        return None
    session = await get_session(session_id)
    if not session or session["status"] != "waiting" or int(session["chat_id"]) != chat.id:
        await call.answer("⚠️ هالجلسة ما عادت متاحة", show_alert=True)
        return None
    return session


@router.callback_query(F.data.startswith("gqa:"))
async def choose_group_anonymity(call: types.CallbackQuery):
    try:
        _, session_id, mode_code = call.data.split(":", 2)
        session = await _guard_config_callback(call, session_id)
        if not session:
            return

        is_anonymous = mode_code == "a"
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
            f"{hint}\n\n⏱ وهلق: بدك مؤقّت داخلي لكل سؤال؟ (الاستفتاء بيسكّر لحاله لما يخلص الوقت)",
            reply_markup=get_group_timer_keyboard(session_id),
        )
    except Exception as e:
        log_error(logger, f"Error in choose_group_names_privacy: {e}", exception=e)
    finally:
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
        timer = seconds if seconds > 0 else None
        await update_session(session_id, {"question_timer_seconds": timer})

        hint = f"⏱ المؤقّت: {timer} ثانية لكل سؤال" if timer else "🚫 بلا مؤقّت داخلي"
        await call.message.edit_text(
            f"{hint}\n\n⏳ وهلق: إيمتى يُرسل السؤال التالي؟",
            reply_markup=get_group_pacing_keyboard(session_id, timer_enabled=bool(timer)),
        )
    except Exception as e:
        log_error(logger, f"Error in choose_group_timer: {e}", exception=e)
    finally:
        try:
            await call.answer()
        except Exception:
            pass


@router.callback_query(F.data.startswith("gqp:"))
async def choose_group_pacing(call: types.CallbackQuery):
    try:
        _, session_id, mode_code, raw_value = call.data.split(":", 3)
        session = await _guard_config_callback(call, session_id)
        if not session:
            return

        if mode_code == "c":
            if not session.get("question_timer_seconds"):
                # حماية ضد callback_data مزوّر: chain_to_timer بلا مؤقّت = جلسة
                # بتعلّق للأبد (ما في حدث إغلاق poll يشغّل السؤال التالي).
                await call.answer("⚠️ هالوضع بيحتاج مؤقّت داخلي مفعّل", show_alert=True)
                return
            pacing_mode, interval = "chain_to_timer", None
        else:
            pacing_mode = "fixed_interval"
            interval = max(MIN_QUESTION_INTERVAL_SECONDS, int(raw_value))

        updated = await update_session(session_id, {
            "pacing_mode": pacing_mode,
            "question_interval_seconds": interval,
            "status": "active",
            "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        if not updated:
            await call.answer("❌ تعذّر بدء الجلسة", show_alert=True)
            return

        label = "فور انتهاء مؤقّت كل سؤال" if pacing_mode == "chain_to_timer" else f"كل {interval} ثانية"
        await call.message.edit_text(f"🚀 انطلقت الجلسة! الأسئلة بتوصل {label}.\nبالتوفيق للجميع 🎯")
        log_info(logger, f"Group quiz session {session_id} started in chat {updated['chat_id']} ({pacing_mode})")
        await send_next_group_question(updated)
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


@router.callback_query(F.data.startswith("gqe:"))
async def end_group_session_now(call: types.CallbackQuery):
    """زر "🏁 إنهاء الجلسة الآن" - يظهر تحت كل سؤال طول الجلسة. أي أدمن بالغروب
    يقدر يضغطه (مش لازم نفس الأدمن اللي بلش الجلسة - قرار عملي: أي أدمن قادر
    يتدخّل لو صاحب الجلسة غاب)."""
    try:
        session_id = call.data.split(":", 1)[1]
        if not await is_group_admin(call.message.chat.id, call.from_user.id):
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
    if chat.type != "channel" or new_status != "administrator" or not user:
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


@router.callback_query(F.data.startswith("gqg:"))
async def show_group_share_steps(call: types.CallbackQuery):
    """زر \"👥 شغّل الكويز ضمن غروب\": خطوات صريحة + زر اختيار الغروب (`?startgroup=`)."""
    try:
        if call.message.chat.type != "private":
            await call.answer("افتح البوت بالخاص.", show_alert=True)
            return
        share_id = call.data.split(":", 1)[1]
        username = await _get_bot_username()
        text = (
            "👥 <b>تشغيل الكويز بغروب</b>\n\n"
            "1️⃣ اضغط «➕ اختر الغروب» تحت وحدّد الغروب من قائمة تيليجرام.\n"
            "2️⃣ أكّد إضافة البوت (إذا مش موجود أصلاً بالغروب).\n"
            "3️⃣ بعد الإضافة البوت بيبدأ شاشة الإعداد جوا الغروب - <b>لازم تكون أدمن</b> بالغروب.\n"
            "4️⃣ اختر نمط الجلسة والمؤقّت واضغط ابدأ.\n\n"
            "📌 البوت موجود أصلاً بالغروب، أو الغروب فيه توبيكات وبدك توبيك معيّن؟ "
            "اكتب داخل الغروب (أو داخل التوبيك المطلوب):\n"
            f"<code>/groupquiz {html.escape(share_id)}</code>\n\n"
            "⚠️ إذا مش أدمن بالغروب، ابعت الأمر السابق لأدمن الغروب."
        )
        kb = types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text="➕ اختر الغروب", url=f"https://t.me/{username}?startgroup=gq_{share_id}")
        ]])
        await call.message.answer(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        log_error(logger, f"Error in show_group_share_steps: {e}", exception=e)
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
        if (not membership
                or membership.get("chat_type") != "channel"
                or membership.get("status") != "administrator"):
            await call.answer("⚠️ البوت مش أدمن بهالقناة حالياً.", show_alert=True)
            return
        if membership.get("can_post_messages") is not True:
            await call.answer("⚠️ البوت ما عنده صلاحية «نشر الرسائل» بهالقناة.", show_alert=True)
            return
        if not await is_group_admin(chat_id, user.id):
            await call.answer("⛔ لازم تكون أدمن بهالقناة.", show_alert=True)
            return

        title = membership.get("chat_title") or "القناة"
        try:
            # يمنع ضغطتين متتاليتين (الثانية كانت رح تنرفض بـ session-exists على أي حال)
            await call.message.edit_text(f"📢 القناة المختارة: <b>{html.escape(title)}</b>", parse_mode="HTML")
        except Exception:
            pass
        await _start_session_setup(
            ui_msg=call.message,
            chat_id=chat_id,
            user=user,
            share_id=share_id,
            message_thread_id=None,
            is_channel=True,
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
    sent = await _safe_call(
        lambda: bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML",
            message_thread_id=session.get("message_thread_id"),
        ),
        what=f"notify group of cancelled session {session_id}",
    )
    if sent is None:
        await _safe_call(
            lambda: bot.send_message(chat_id=started_by, text=text, parse_mode="HTML"),
            what=f"notify admin (DM fallback) of cancelled session {session_id}",
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
    await clear_session_questions_cache(session_id)

    open_poll = await _pop_open_poll(session_id)
    if open_poll:
        try:
            await bot.stop_poll(chat_id=open_poll["chat_id"], message_id=open_poll["message_id"])
        except Exception as e:
            # متوقّع لو السؤال أصلاً سكّر لحاله (خلص open_period) قبل ما توصل هون -
            # aiogram بترمي BadRequest بهالحالة، مش خطأ حقيقي.
            log_error(logger, f"stop_poll failed for session {session_id} (likely already closed): {e}")

    if session.get("poll_is_anonymous"):
        # ما في ولا صف بـ group_quiz_participants أصلاً بهالوضع (تيليجرام ما
        # بيبعت poll_answer للاستفتاءات المجهولة) - رسالة صريحة بدل ما توحي
        # "لا يوجد أي إجابة مسجّلة" إنه محدا جاوب، بينما هو قرار مقصود بالإعداد.
        text = "🏁 خلصت الجلسة (نمط مجهول بالكامل - بلا نقاط ولا ترتيب فردي حسب الإعداد المُختار)."
        needs_manual_suffix = True
    elif not session.get("question_timer_seconds"):
        # جلسة بدون مؤقّت: الاستفتاءات مصممة تضل مفتوحة للأبد، وما في أي
        # تتبّع/تسجيل إجابات لهالنوع أصلاً (قرار: صفر tracking - الاعتماد
        # الكامل على تيليجرام نفسها). فمفيش ترتيب نهشره، بس لازم إشعار واضح
        # إنه ما في أسئلة جديدة جايّة (بدل سكوت تام يوحي بخلل). الصياغة نفسها
        # بتفرّق بين "خلصت كل الأسئلة طبيعياً" و"الأدمن قطعها يدوياً قبل
        # ما تخلص" - القطع اليدوي مش بالضرورة يعني كل الأسئلة انبعثت.
        if manual:
            text = "⏹️ تم إيقاف إرسال الأسئلة يدوياً من قبل الأدمن. الاستفتاءات المرسلة ضلّت مفتوحة، أي حدا لسا يقدر يجاوب عليها."
        else:
            text = "🏁 انتهت الأسئلة. الاستفتاءات المرسلة ضلّت مفتوحة، أي حدا لسا يقدر يجاوب عليها."
        needs_manual_suffix = False
    else:
        rows = await get_leaderboard(session_id)
        show_names = session.get("show_names", True)  # True لو العمود لسا مش مضاف (fail-open نفس نمط باقي الإعدادات بالمشروع)
        if not rows:
            text = "🏁 خلصت الجلسة - ما في أي إجابة مسجّلة."
        else:
            medals = ["🥇", "🥈", "🥉"]
            lines = ["🏁 <b>انتهت الجلسة! الترتيب النهائي:</b>", ""]
            for i, r in enumerate(rows):
                if show_names:
                    raw_who = r.get("first_name") or (f"@{r['username']}" if r.get("username") else str(r["user_id"]))
                    who = html.escape(raw_who)
                else:
                    who = f"لاعب {i + 1}"
                rank = medals[i] if i < len(medals) else f"{i + 1}."
                lines.append(f"{rank} {who} — {r.get('score', 0)} نقطة")
            text = "\n".join(lines)
        needs_manual_suffix = True
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


# ==================== إجابات الأعضاء ====================
@router.poll_answer()
async def handle_group_poll_answer(poll_answer: types.PollAnswer):
    """نسخة الغروب من معالج الإجابات.

    ما في فلتر نوع محادثة على `poll_answer` (التحديث بلا chat)، فمنميّز عبر
    وجود `session_id` بسجل Redis. أي استفتاء من النمط الفردي بينزل لمعالج
    `handlers/quiz_runner.py` عبر SkipHandler - سلوكه ما بيتغيّر إطلاقاً.

    بخلاف النمط الفردي، ما في فحص `user.id != quiz_info["user_id"]` هون -
    أي عضو بالغروب إجابته محسوبة.
    """
    try:
        raw = await redis_client.get(f"poll:{poll_answer.poll_id}")
    except Exception as e:
        log_error(logger, f"Redis error reading poll record: {e}")
        raise SkipHandler()

    if not raw:
        raise SkipHandler()
    try:
        info = json.loads(raw)
    except Exception:
        raise SkipHandler()
    if not info.get("session_id"):
        raise SkipHandler()

    if not info.get("has_timer"):
        # جلسة بدون مؤقّت - بالتصميم ما منسجّل ولا منتتبّع (قرار: الاعتماد
        # الكامل على واجهة تيليجرام نفسها لعرض الجواب الصح لكل طالب لحاله،
        # بدون تخزين هوية أي حدا). هاد بيطبّق كمان على أي جواب متأخر يوصل
        # حتى بعد أيام - نفس السلوك بالضبط، بلا حاجة لأي منطق تنظيف إضافي.
        return

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
        if inserted:
            await bump_participant(
                session_id=info["session_id"],
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                is_correct=is_correct,
                answer_time_ms=answer_time_ms,
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
    sessions = await get_due_sessions()
    for session in sessions:
        try:
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


group_quiz_router = router
