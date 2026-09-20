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
import json
import time
from typing import Any, Dict, Optional

from aiogram import F, Router, types
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandObject

from config import bot, redis_client
from keyboards import (
    get_group_anonymity_keyboard,
    get_group_names_privacy_keyboard,
    get_group_pacing_keyboard,
    get_group_question_control_keyboard,
    get_group_timer_keyboard,
)
from logger import get_logger, log_error, log_info
from services.group_permissions import is_group_admin
from services.group_quiz_store import (
    claim_question_slot,
    clear_session_questions_cache,
    create_session,
    get_due_sessions,
    get_leaderboard,
    get_open_session_for_chat,
    get_session,
    get_session_questions,
    record_answer,
    bump_participant,
    update_session,
)
from services.quiz_engine import send_quiz_poll
from supabase_helper import check_or_add_user, get_shared_quiz

logger = get_logger(__name__)

router = Router()
# فلتر على مستوى الـ router بدل تكرار فحص نوع المحادثة بكل معالج.
# ⚠️ ما بينطبق على `poll_answer` لأن تحديث PollAnswer ما فيه كائن chat أصلاً -
# التمييز هناك بيصير عبر محتوى سجل Redis (راجع handle_group_poll_answer).
router.message.filter(F.chat.type.in_({"group", "supergroup"}))
router.callback_query.filter(F.message.chat.type.in_({"group", "supergroup"}))

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


async def _safe_call(coro_factory, *, what: str):
    """يلفّ أي نداء إرسال لتيليجرام بمعالجة TelegramRetryAfter كخط دفاع أخير.

    `coro_factory` دالة بترجّع coroutine جديدة بكل محاولة (ما بينفع نعيد
    استخدام نفس الـ coroutine بعد ما انرمى منها استثناء).
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
            return None
    except Exception as e:
        log_error(logger, f"Failed on {what}: {e}")
        return None


# ==================== بدء الجلسة ====================
async def _begin_group_session(msg: types.Message, share_id: str) -> None:
    chat_id = msg.chat.id
    user = msg.from_user
    if not user:
        return

    if not await is_group_admin(chat_id, user.id):
        await _safe_call(
            lambda: msg.reply("⛔ فقط أدمن الغروب يقدر يشغّل كويز جماعي هون."),
            what="reply not-admin",
        )
        return

    existing = await get_open_session_for_chat(chat_id)
    if existing:
        await _safe_call(
            lambda: msg.reply("⚠️ في جلسة كويز شغّالة (أو بانتظار الإعداد) بهالغروب. خلّصها أو ألغيها أولاً."),
            what="reply session-exists",
        )
        return

    shared = await get_shared_quiz(share_id)
    questions = (shared or {}).get("quiz_data") or []
    if not shared or not questions:
        await _safe_call(
            lambda: msg.reply("❌ رابط الكويز منتهي الصلاحية أو ما عاد موجوداً."),
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
    )
    if not session:
        await _safe_call(lambda: msg.reply("❌ تعذّر إنشاء الجلسة حالياً، جرّب بعد شوي."), what="reply create-failed")
        return

    title = shared.get("source_title") or shared.get("title") or "كويز"
    text = (
        f"📚 <b>{title}</b>\n"
        f"عدد الأسئلة: {len(questions)}\n\n"
        "🎮 أول شي: شو نمط الجلسة؟\n"
        "🏆 <b>تنافسي</b>: نقاط وترتيب نهائي، بس أي عضو بالغروب يقدر يشوف \"من صوّت لمين\" على كل سؤال (قيد من تيليجرام نفسها، مش قابل للتعطيل).\n"
        "🙈 <b>مجهول بالكامل</b>: محدا (ولا حتى أنا) بيعرف مين جاوب شو - بالمقابل بلا نقاط ولا ترتيب إطلاقاً."
    )
    await _safe_call(
        lambda: msg.answer(text, parse_mode="HTML", reply_markup=get_group_anonymity_keyboard(str(session["id"]))),
        what="send anonymity screen",
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
    """فحص مشترك لأزرار الإعداد: أدمن بالغروب + جلسة موجودة وبحالة waiting."""
    if not await is_group_admin(call.message.chat.id, call.from_user.id):
        await call.answer("⛔ للأدمن فقط", show_alert=True)
        return None
    session = await get_session(session_id)
    if not session or session["status"] != "waiting" or int(session["chat_id"]) != call.message.chat.id:
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
    )
    if poll_msg is None:
        log_error(logger, f"Question {idx} burned for session {session_id} (send failed after claim)")
        return False
    await _remember_open_poll(session_id, int(session["chat_id"]), poll_msg.message_id)
    return True


async def finish_group_session(session: Dict[str, Any], manual: bool = False) -> None:
    """ينهي الجلسة وينشر الترتيب النهائي.

    الترتيب هون مقصود (نفس ترتيب الخطة): 1) `status='finished'` أولاً - هاد اللي
    بيمنع `handle_group_poll_closed` من إرسال سؤال إضافي لو التحديث وصل بنفس
    اللحظة (الفحص هناك على `status == 'active'`). 2) `bot.stop_poll` على أي
    poll لسا مفتوح - يقفل الاستفتاء نفسه بواجهة تيليجرام حتى لو حدا لسا
    بيحاول يجاوب. 3) نشر النتائج.
    """
    session_id = str(session["id"])
    if session.get("status") == "finished":
        return
    await update_session(session_id, {
        "status": "finished",
        "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "next_question_due_at": None,
    })
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
                    who = r.get("first_name") or (f"@{r['username']}" if r.get("username") else str(r["user_id"]))
                else:
                    who = f"لاعب {i + 1}"
                rank = medals[i] if i < len(medals) else f"{i + 1}."
                lines.append(f"{rank} {who} — {r.get('score', 0)} نقطة")
            text = "\n".join(lines)
        needs_manual_suffix = True
    if manual and needs_manual_suffix:
        text += "\n\n<i>(أُنهيت يدوياً من قبل الأدمن)</i>"

    await _safe_call(
        lambda: bot.send_message(chat_id=int(session["chat_id"]), text=text, parse_mode="HTML"),
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
