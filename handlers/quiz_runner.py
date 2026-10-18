# Handlers/quiz_runner.py
import asyncio
from task_utils import spawn
import html
import json
import re
import secrets
from typing import Union, Optional, List, Dict, Any, Tuple

from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.exceptions import TelegramBadRequest

from config import bot, QuizState, redis_client
from constants import (
    MSG_FEEDBACK_PROMPT, MSG_FEEDBACK_SAVED,
    WEBAPP_PUBLIC_BASE_URL,
)
from keyboards import (
    get_main_menu_keyboard,
    get_quiz_result_keyboard,
    get_quiz_exit_confirmation_keyboard,
    get_rating_keyboard,
    get_question_edit_keyboard,
    get_answer_edit_keyboard,
    get_math_question_edit_keyboard,
    get_cancel_edit_keyboard,
)
from logger import get_logger, log_error, log_warning
from services.latex_text import latex_to_plain
from services.quiz_permissions import can_edit_quiz
from services.redis_reliability import user_locked
from services.flow_state import build_state_for_chat  # 🆕 نفس بناء FSMContext اليدوي المستخدم لاستئناف الكويز من خلفية (محرر أسئلة الرياضيات عبر الويب)
from helpers.supabase_helper import (
    list_favorite_quizzes,
    save_favorite_quiz,
    list_favorite_sections,
    create_favorite_section,
    get_or_update_high_score,
    submit_quiz_vote,
    save_quiz_feedback,
    log_usage_event,
    start_quiz_attempt,
    complete_quiz_attempt,
    mark_quiz_attempt_stopped,
    update_quiz_question,
    get_quiz_creator_id,
    _is_valid_uuid
)
from services.quiz_engine import send_quiz_poll
from services.quiz_result_ui import (
    build_wrong_answer_review, review_page_text, review_inline_button,
    review_question_rich_html, review_explanation_rich_html,
    review_question_fallback_text, review_explanation_fallback_text,
)

logger = get_logger(__name__)
router = Router()

ACTIVE_QUIZ_STATES = (
    QuizState.answering_quiz, 
    QuizState.waiting_for_custom_name, 
    QuizState.waiting_for_new_section_title,
    QuizState.waiting_for_quiz_feedback
)
QUIZ_EDIT_STATES = (
    QuizState.waiting_for_question_edit_choice,
    QuizState.waiting_for_question_edit_text,
    QuizState.waiting_for_answer_edit_choice,
    QuizState.waiting_for_answer_edit_text,
)

_STALE_QUESTION_CACHE_KEYS = ("image_url", "rendered_image_url", "cached_image_url")


def _sanitize_question_for_render(question: Optional[dict]) -> dict:
    """يزيل أي بيانات صورة/كاش قديمة عن سؤال رياضي بحيث يعاد رسمه من البيانات الجديدة.
    تُستخدم فقط بمسارات التعديل الفعلية (سؤال تغيّر محتواه فعلاً) - راجع
    _prepare_question_for_send تحت لمسار العرض العادي الذي لا يجب أن يُبطل الكاش."""
    if not isinstance(question, dict):
        return {}
    cleaned = dict(question)
    for key in _STALE_QUESTION_CACHE_KEYS:
        cleaned.pop(key, None)
    return cleaned


# 🩹 FIX (كان يُبطل كاش الصورة على كل عرض سؤال، بلا داعٍ): send_question_by_ids كانت
# تمرر كل سؤال عبر _sanitize_question_for_render قبل الإرسال مباشرة - تلك الدالة
# مصممة أصلاً لمسار التعديل فقط (سؤال تغيّر محتواه فعلياً، فيجب إعادة رسمه). تطبيقها
# هنا بشكل عام كان يحذف image_url من كل سؤال رياضي في كل مرة يُعرض (بما فيها "التالي"،
# إعادة تشغيل نفس الكويز quiz_replay، وأخطر من ذلك: كل كويز رياضي مشترك/مفضّل يفتحه
# طالب جديد عبر رابط عميق - shared["quiz_data"] القادمة من قاعدة البيانات تحمل بالفعل
# image_url صالحاً محسوباً مسبقاً، لكن كانت تُحذف فوراً هنا فيُعاد الرسم والرفع لـ
# Supabase من الصفر لكل طالب يفتح نفس الرابط - بالضبط الحالة التي بُني الكاش أصلاً
# لتفاديها). هذه دالة خفيفة بديلة: نسخة آمنة (تتعامل مع بيانات غير متوقعة) بلا أي
# حذف - إبطال الكاش الفعلي يبقى محصوراً بمسارات التعديل الصريحة أعلاه، التي تستدعي
# _sanitize_question_for_render بنفسها قبل حتى كتابة السؤال المعدَّل إلى state.
def _prepare_question_for_send(question: Optional[dict]) -> dict:
    return dict(question) if isinstance(question, dict) else {}

# 🩹 نفس حالات الكويز النشط + None: تُستخدم حصراً لهاندلرات ويزارد "حفظ في المفضلة"
# لأن state يُصفَّر إلى None عند اكتمال الكويز (_handle_quiz_completion) قبل أن
# يصل المستخدم لصفحة النتيجة، وزر "⭐ حفظ في المفضلة" يظهر هناك تحديداً.
# لا تُستخدم لهاندلرات الكويز النشط الأخرى (next_question, get_hint, quiz_stop...)
# التي يجب أن تبقى مقيدة بحالة كويز فعلياً جارٍ.
SAVE_WIZARD_STATES = ACTIVE_QUIZ_STATES + (None,)

async def send_main_menu(call_or_message: Union[types.Message, types.CallbackQuery], user_id: int) -> None:
    bot_info = await bot.get_me()
    menu = await get_main_menu_keyboard(bot_info.username, user_id)
    from helpers.student_ui import HOME_TEXT
    text = HOME_TEXT
    if isinstance(call_or_message, types.CallbackQuery):
        await call_or_message.message.answer(text, reply_markup=menu, parse_mode="HTML")
    else:
        await call_or_message.answer(text, reply_markup=menu, parse_mode="HTML")

@router.callback_query(F.data == "show_menu_after_first_quiz")
async def show_menu_after_first_quiz(call: types.CallbackQuery) -> None:
    """🆕 زر "📋 عرض القائمة" برسالة تهنئة أول اختبار مكتمل (راجع _handle_quiz_completion
    تحت) - يُظهر القائمة الرئيسية عند الطلب فقط بدل إرفاقها تلقائياً، تخفيفاً للازدحام
    البصري. يزيل الزر نفسه من الرسالة بعد الضغط لتفادي ضغطه أكثر من مرة بلا داعٍ."""
    try:
        await send_main_menu(call, call.from_user.id)
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    except Exception as e:
        log_error(logger, f"Error in show_menu_after_first_quiz for {call.from_user.id}: {e}", exception=e)
    finally:
        await call.answer()

@user_locked()
async def start_loaded_quiz(msg_or_call: Union[types.Message, types.CallbackQuery], state: FSMContext, quiz_data: list, source_title: str, origin: str = "shared", quiz_id: Optional[str] = "") -> None:
    user_id = msg_or_call.from_user.id
    from handlers.session_navigation import offer_transition
    chat_id = msg_or_call.message.chat.id if isinstance(msg_or_call, types.CallbackQuery) else msg_or_call.chat.id
    if await offer_transition(state, user_id, chat_id, {"kind": "loaded", "kwargs": {
            "quiz_data": quiz_data, "source_title": source_title, "origin": origin, "quiz_id": quiz_id}}):
        return
    attempt_id = start_quiz_attempt(user_id, quiz_id or None, origin, len(quiz_data))
    spawn(log_usage_event(user_id, "quiz_started", {
        "origin": origin, "quiz_id": quiz_id, "questions": len(quiz_data),
    }))

    await state.update_data(
        questions=quiz_data, current_index=0, score=0, answered_questions={}, navigation_session_id=None,
        total_count=len(quiz_data), source_title=source_title,
        quiz_origin=origin, quiz_completed=False, quiz_id=quiz_id,
        is_saved_in_session=False, is_switching_question=False,
        attempt_id=attempt_id, stopped_early=False,
        share_id=None
    )
    await state.set_state(QuizState.answering_quiz)
    if isinstance(msg_or_call, types.CallbackQuery):
        try:
            await msg_or_call.message.delete()
        except Exception:
            pass
    await send_question(msg_or_call, state)

async def send_question(msg_or_call: Union[types.Message, types.CallbackQuery], state: FSMContext) -> None:
    chat_id = msg_or_call.chat.id if isinstance(msg_or_call, types.Message) else msg_or_call.message.chat.id
    user_id = msg_or_call.from_user.id
    await send_question_by_ids(chat_id, user_id, state)


async def send_question_by_ids(chat_id: int, user_id: int, state: FSMContext) -> None:
    """
    🆕 نفس منطق send_question بالضبط لكن تاخذ chat_id/user_id مباشرة بدل Message/
    CallbackQuery - ضرورية لاستئناف الكويز من خلفية (background task) مش من داخل
    handler عادي، متل بعد الحفظ من محرر أسئلة الرياضيات عبر صفحة الويب (راجع
    save_question_edit_from_web أدناه وwebhook_server.py). send_question نفسها
    صارت غلاف رقيق فوقها لتفادي تكرار أي منطق.
    """
    try:
        data = await state.get_data()
        questions = data['questions']
        idx = data['current_index']

        # 1. حالة انتهاء الاختبار
        if idx >= len(questions):
            await _handle_quiz_completion(chat_id, user_id, state, data)
            return

        # 2. تجهيز وإرسال السؤال الحالي (بلا إبطال كاش الصورة - راجع تعليق
        #    _prepare_question_for_send أعلاه لسبب استبدال _sanitize_question_for_render هنا)
        q = _prepare_question_for_send(questions[idx])
        questions[idx] = q
        await state.update_data(questions=questions)
        control_kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="💡 طلب تلميح", callback_data="get_hint")],
            [
                types.InlineKeyboardButton(text="⏹ إنهاء", callback_data="quiz_stop"),
                types.InlineKeyboardButton(text="🔗 مشاركة", callback_data="quiz_share"),
                types.InlineKeyboardButton(text="💾 حفظ", callback_data="save_quiz")
            ],
            [types.InlineKeyboardButton(text="🏁 عرض النتيجة" if idx + 1 == len(questions) else "➡️ السؤال التالي", callback_data="next_question")]
        ])

        from handlers.session_navigation import session_id
        identity = await session_id(state)
        await send_quiz_poll(chat_id, user_id, q, idx, len(questions), control_kb,
                             quiz_id=data.get('quiz_id'), poll_meta={"session_id": identity})
        # 🆕 send_quiz_poll (لسؤال رياضي) قد يملأ q["image_url"] بعد الرسم/الرفع الفعلي -
        # q هو نفس الكائن المرجعي questions[idx] أعلاه، فنعيد كتابة questions لـ state هنا
        # لضمان تخزين الرابط الجديد فعلياً (وليس فقط بالذاكرة المحلية لهذه الدالة)، حتى
        # تستفيد منه إعادة تشغيل هذا الكويز بنفس الجلسة (quiz_replay) بلا إعادة رسم لاحقة.
        if q.get("image_url"):
            await state.update_data(questions=questions)
        await state.update_data(is_switching_question=False)
    except Exception as e:
        log_error(logger, f"Error in send_question_by_ids: {e}", exception=e)
        await state.update_data(is_switching_question=False)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ **نعتذر منك، واجه النظام مشكلة تقنية في عرض السؤال رقم ({idx + 1}).**\n\n⏩ يمكنك تخطيه والانتقال للتالي.",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="تخطي هذا السؤال والانتقال للتالي ➡️", callback_data="next_question")]
                ])
            )
        except Exception:
            pass

_REVIEW_TTL_SECONDS = 7 * 24 * 3600


def _quiz_review_key(user_id: int, token: str) -> str:
    return f"quiz:error_review:{user_id}:{token}"


async def _store_quiz_error_review(user_id: int, data: dict):
    """Keep a user-scoped snapshot independent of later FSM sessions."""
    entries = build_wrong_answer_review(data.get("questions"), data.get("answered_questions"))
    answers = data.get("answered_questions") or {}
    logger.info("QUIZ_REVIEW_SNAPSHOT questions=%s recorded_answers=%s errors=%s",
                len(data.get("questions") or []), len(answers), len(entries))
    if not entries:
        return None, 0
    token = secrets.token_hex(8)
    try:
        await redis_client.set(
            _quiz_review_key(user_id, token),
            json.dumps(entries, ensure_ascii=False),
            ex=_REVIEW_TTL_SECONDS,
        )
    except Exception as exc:
        log_warning(logger, f"Could not save error review for {user_id}: {exc}")
        return None, 0
    return token, len(entries)


async def _send_quiz_completion(chat_id: int, result_text: str, keyboard, review_token, wrong_count: int):
    """Prefer a rich in-message review CTA; normal keyboard stays as a fallback."""
    if review_token:
        button = review_inline_button(review_token, wrong_count)
        # Keep the error count close to the grade, but place the actual
        # interactive button after the whole result body for a cleaner layout.
        paragraphs = []
        inserted = False
        for line in result_text.split("\n"):
            if not line.strip():
                continue
            paragraphs.append(f"<p>{line}</p>")
            if not inserted and "📊 النسبة المئوية:" in line:
                paragraphs.append(f"<p>❌ الإجابات الخاطئة: {wrong_count}</p>")
                inserted = True
        if not inserted:
            paragraphs.append(f"<p>❌ الإجابات الخاطئة: {wrong_count}</p>")
        paragraphs.append(button)
        rich_html = "".join(paragraphs)
        logger.info("QUIZ_RESULT_RICH_ATTEMPT review_errors=%s", wrong_count)
        try:
            sent = await bot.send_rich_message(
                chat_id=chat_id, rich_message=types.InputRichMessage(html=rich_html, is_rtl=True),
                reply_markup=keyboard,
            )
            logger.info("QUIZ_RESULT_RICH_SENT review_errors=%s", wrong_count)
            return sent
        except Exception as exc:
            log_warning(logger, f"QUIZ_RESULT_RICH_FAILED reason={type(exc).__name__}: {exc}; using legacy review button")
        fallback = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(
                text=f"❌ مراجعة الأخطاء ({wrong_count})", callback_data=f"qrev:{review_token}:0")],
            *keyboard.inline_keyboard,
        ])
        return await bot.send_message(chat_id, result_text, reply_markup=fallback, parse_mode="HTML")
    return await bot.send_message(chat_id, result_text, reply_markup=keyboard, parse_mode="HTML")


async def _handle_quiz_completion(chat_id: int, user_id: int, state: FSMContext, data: dict):
    score, total = data['score'], data['total_count']
    quiz_id = data.get('quiz_id')
    # ⚠️ current_index يكون دائماً >= total هنا (send_question لا تستدعي هذه الدالة
    # إلا في هذه الحالة تحديداً)، لذلك لا يصلح كمعيار للتفريق بين "أكمل الطالب كل
    # الأسئلة" و"أوقف الاختبار مبكراً". نعتمد بدلاً منه على العلم الصريح stopped_early
    # الذي تضبطه stop_quiz_confirmed عند الإيقاف اليدوي.
    stopped_early = bool(data.get('stopped_early'))
    percentage = (score / total * 100) if total > 0 else 0
    previous_score_text = ""

    if quiz_id and str(quiz_id).strip():
        score_data = await get_or_update_high_score(user_id, quiz_id, score, total)
        # 🆕 previous_score يكون None فقط أول مرة يخلّص فيها الطالب هالكويز.
        # قرار نشر/إخفاء النتيجة بلوحة الشرف صار كله تحت لوحة الشرف نفسها
        # (زر "عرض لوحة الشرف" هون بس - راجع handlers/leaderboard.py).
        is_new_attempt = score_data["previous_score"] is None
        if not is_new_attempt:
            prev_score, highest = score_data["previous_score"], score_data["highest_score"]
            previous_score_text = f"\n🕒 نتيجتك السابقة: <b>{prev_score}</b>"
            if score > prev_score:
                previous_score_text += "\n🎉 <b>رقم قياسي جديد لك!</b>"
            previous_score_text += f"\n🏆 أعلى نتيجة مسجلة لك: <b>{highest}</b> من <b>{total}</b>\n"

    result_text = (
        f"{'⏹ <b>تم إيقاف الاختبار.</b>' if stopped_early else '🏁 <b>اكتمل الاختبار بنجاح!</b>'}\n\n"
        f"🎯 نتيجتك الحالية: <b>{score}</b> من <b>{total}</b>\n"
        f"📊 النسبة المئوية: <b>{percentage:.1f}%</b>\n"
        f"{previous_score_text}\n"
        f"{'🏆 ممتاز!' if percentage >= 80 else '👍 جيد!' if percentage >= 60 else '📚 استمر في الممارسة!'}"
    )

    from helpers.supabase_helper import has_completed_any_quiz_before  # استيراد محلي لتفادي دورة استيراد غير ضرورية

    # 🆕 UX (2026-08-28): تأجيل ظهور القائمة الرئيسية لمستخدم جديد إلى ما بعد أول اختبار
    # كامل ينهيه فعلياً - بدل إظهارها فوراً بعد ضغط زر "أرسل أول ملف الآن" بـ /start (كانت
    # تُعرض هناك سابقاً عبر handlers/start.py::ack_new_user_start، أُزيلت من هناك الآن).
    # الفحص يتم *قبل* منطق stopped_early/quiz_completed أدناه، وفقط لو الاختبار اكتمل
    # فعلياً (وليس أُوقف مبكراً) نعرض القائمة - حتى لا نكافئ إيقافاً مبكراً بنفس تجربة
    # الإكمال الفعلي. is_first_completion يُحسب دائماً (حتى لو stopped_early=True) لكن
    # يُستخدم فقط بالفرع else تحت.
    is_first_completion = not stopped_early and not await has_completed_any_quiz_before(user_id)

    # التحقق من صحة المعرف لإظهار لوحة التقييم بشكل آمن
    if quiz_id and _is_valid_uuid(quiz_id):
        # 🆕 جلب creator_id لهذا الكويز لإظهار زر "حذف الكويز نهائياً" فقط للأدمن أو
        # لمالكه الفعلي على شاشة النتيجة (المكان الأكثر ظهوراً للكويز للطالب).
        from helpers.supabase_helper import admin_get_quiz_by_id  # استيراد محلي لتفادي دورة استيراد
        quiz_row = await admin_get_quiz_by_id(quiz_id)
        creator_id = quiz_row.get("creator_id") if quiz_row else None
        keyboard = get_rating_keyboard(quiz_id, quiz_id=quiz_id, viewer_id=user_id, creator_id=creator_id)
        result_text += "\n\nاحفظ الكويز للمراجعة أو حمّله من الأزرار أدناه.\n💬 عندك ملاحظات عن الأسئلة؟ شاركنا رأيك من الزر أدناه."
    else:
        keyboard = get_quiz_result_keyboard(quiz_id=quiz_id)

    review_token, wrong_count = await _store_quiz_error_review(user_id, data)
    await _send_quiz_completion(chat_id, result_text, keyboard, review_token, wrong_count)

    # 🆕 أول اختبار كامل لمستخدم جديد: نعرض رسالة تهنئة قصيرة بعد النتيجة مباشرة، لكن
    # بزر واحد فقط "📋 عرض القائمة" بدل إرفاق القائمة الرئيسية كاملة مباشرة - تخفيفاً
    # للازدحام البصري (طلب صريح من المستخدم 2026-08-28). الضغط على الزر هو ما يستدعي
    # فعلياً القائمة الرئيسية عبر المعالج الجديد show_main_menu_after_first_quiz تحت.
    if is_first_completion:
        try:
            await bot.send_message(
                chat_id,
                "🎉 <b>أحسنت في أول اختبار لك!</b>\n\n"
                "تقدر ترجع للكويز من المفضلة إذا حفظته، أو تبدأ كويزاً جديداً بإرسال محتواك.\n"
                "رصيدك وخيارات المساعدة موجودة في القائمة الرئيسية.",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="📋 عرض القائمة", callback_data="show_menu_after_first_quiz")]
                ]),
                parse_mode="HTML",
            )
        except Exception as e:
            log_error(logger, f"Error sending first-completion congrats to {user_id}: {e}", exception=e)

    # التمييز الدقيق في التتبع بين الكويز المكتمل والتوقف المبكر
    if stopped_early:
        spawn(mark_quiz_attempt_stopped(data.get("attempt_id")))
        spawn(log_usage_event(user_id, "quiz_stopped", {
            "quiz_id": quiz_id, "score": score, "total": total, "percentage": round(percentage, 1),
        }))
    else:
        spawn(complete_quiz_attempt(data.get("attempt_id"), score))
        spawn(log_usage_event(user_id, "quiz_completed", {
            "quiz_id": quiz_id, "score": score, "total": total, "percentage": round(percentage, 1),
        }))
    await state.update_data(quiz_completed=True, is_switching_question=False)
    await state.set_state(None)


async def _review_resolve_image(user_id: int, token: str, page: int, items: list):
    """Reuse the question's existing image. Regenerate only when no URL survived."""
    entry = items[page]
    if not entry.get("is_math"):
        return None
    image_url = entry.get("image_url") or ""
    if image_url.startswith(("https://", "http://")):
        return image_url

    from services.image_quiz_renderer import render_question_image_async, looks_arabic
    from helpers.supabase_helper import upload_quiz_question_image

    question = entry.get("math_question") or {}
    raw = await render_question_image_async(
        question, int(entry.get("index", entry["number"] - 1)), len(items),
        looks_arabic(str(question.get("question") or "")),
    )
    # The original upload path may have failed during the quiz, so try again
    # rather than losing tables, matrices, and LaTeX formatting on review.
    try:
        new_url = await upload_quiz_question_image(raw, f"tmp/review-{token}-{page}.png")
        if new_url and str(new_url).startswith(("https://", "http://")):
            entry["image_url"] = str(new_url)
            key = _quiz_review_key(user_id, token)
            ttl = await redis_client.ttl(key)
            if ttl > 0:
                await redis_client.set(key, json.dumps(items, ensure_ascii=False), ex=ttl)
            return str(new_url)
    except Exception as exc:
        log_warning(logger, f"Could not cache math review image: {exc}")
    return types.BufferedInputFile(raw, filename="math-review.png")


def _review_pager(token: str, page: int, count: int, *, explanation: bool = False):
    navigation = []
    if not explanation:
        if page > 0:
            navigation.append(types.InlineKeyboardButton(
                text="⬅️ السابق", callback_data=f"qrevpage:{token}:{page - 1}"))
        if page + 1 < count:
            navigation.append(types.InlineKeyboardButton(
                text="التالي ➡️", callback_data=f"qrevpage:{token}:{page + 1}"))
    rows = [navigation] if navigation else []
    rows.append([types.InlineKeyboardButton(
        text="✖️ إغلاق المراجعة", callback_data=f"qrev_close:{token}")])
    if explanation:
        # The normal-keyboard fallback needs a back button too.
        rows.insert(0, [types.InlineKeyboardButton(
            text="↩️ العودة للسؤال", callback_data=f"qrevback:{token}:{page}")])
    else:
        rows.insert(0, [types.InlineKeyboardButton(
            text="💡 عرض الشرح", callback_data=f"qrevexp:{token}:{page}")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def _review_show(call: types.CallbackQuery, token: str, page: int, *,
                       explanation: bool = False, new_message: bool = False):
    """Rich image/text and in-message explanation toggle; never touch quiz FSM."""
    key = _quiz_review_key(call.from_user.id, token)
    raw = await redis_client.get(key)
    if not raw:
        await call.answer("انتهت صلاحية مراجعة الأخطاء. يمكنك إعادة المحاولة.", show_alert=True)
        return
    items = json.loads(raw)
    if not isinstance(items, list) or not 0 <= page < len(items):
        await call.answer("صفحة مراجعة غير متاحة.", show_alert=True)
        return

    entry = items[page]
    media = None
    if not explanation and entry.get("is_math"):
        try:
            media = await _review_resolve_image(call.from_user.id, token, page, items)
        except Exception as exc:
            log_warning(logger, f"Could not render math review image: {exc}")

    image_url = media if isinstance(media, str) else ""
    if explanation:
        rich_html = review_explanation_rich_html(entry, page, len(items), token)
        plain = review_explanation_fallback_text(entry)
    else:
        rich_html = review_question_rich_html(entry, page, len(items), token, image_url)
        plain = review_question_fallback_text(entry, page, len(items))
    keyboard = _review_pager(token, page, len(items), explanation=explanation)
    rich = types.InputRichMessage(html=rich_html, is_rtl=True)

    # New review starts below the original result. All subsequent navigation
    # and explanation/back taps edit only the new review message.
    try:
        if new_message:
            await bot.send_rich_message(
                chat_id=call.message.chat.id, rich_message=rich, reply_markup=keyboard)
        elif getattr(call.message, "photo", None):
            # A previous rich send failed and this review is a photo fallback.
            await call.message.edit_caption(
                caption=plain[:1000], parse_mode="HTML", reply_markup=keyboard)
        else:
            await bot.edit_message_text(
                chat_id=call.message.chat.id, message_id=call.message.message_id,
                rich_message=rich, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            await call.answer()
            return
        log_warning(logger, f"Rich review unavailable, using fallback: {exc}")
        await _review_fallback(call, plain, keyboard, media, new_message)
    except (AttributeError, TypeError) as exc:
        log_warning(logger, f"Rich review unsupported, using fallback: {exc}")
        await _review_fallback(call, plain, keyboard, media, new_message)
    await call.answer()


async def _review_fallback(call, body, keyboard, media, new_message):
    """On legacy clients, keep the math image and caption editable in place."""
    message = call.message
    if new_message:
        if media:
            await message.answer_photo(
                photo=media, caption=body[:1000], parse_mode="HTML", reply_markup=keyboard)
        else:
            await message.answer(body, parse_mode="HTML", reply_markup=keyboard)
    elif getattr(message, "photo", None):
        await message.edit_caption(
            caption=body[:1000], parse_mode="HTML", reply_markup=keyboard)
    else:
        await message.edit_text(body, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.regexp(r"^qrevstart:[a-f0-9]{16}$"))
async def start_quiz_error_review(call: types.CallbackQuery):
    try:
        await _review_show(call, call.data.split(":")[1], 0, new_message=True)
    except Exception as exc:
        log_warning(logger, f"Error opening error review: {exc}")
        await call.answer("تعذر فتح المراجعة حالياً.", show_alert=True)


@router.callback_query(F.data.regexp(r"^(qrev|qrevpage|qrevexp|qrevback):[a-f0-9]{16}:[0-9]{1,4}$"))
async def show_quiz_error_review(call: types.CallbackQuery):
    try:
        action, token, page_raw = call.data.split(":")
        # Older result messages used qrev:<token>:0; retain their navigation.
        legacy_new = action == "qrev" and not (
            (getattr(call.message, "text", "") or "").startswith("📚 مراجعة الأخطاء"))
        await _review_show(
            call, token, int(page_raw),
            explanation=(action == "qrevexp"),
            new_message=legacy_new)
    except Exception as exc:
        log_warning(logger, f"Error reviewing wrong quiz answers: {exc}")
        await call.answer("تعذر فتح المراجعة حالياً.", show_alert=True)


@router.callback_query(F.data.regexp(r"^qrev_close:[a-f0-9]{16}$"))
async def close_quiz_error_review(call: types.CallbackQuery):
    token = call.data.split(":")[-1]
    if not await redis_client.get(_quiz_review_key(call.from_user.id, token)):
        await call.answer("انتهت صلاحية المراجعة.", show_alert=True)
        return
    try:
        await call.message.delete()
    except TelegramBadRequest:
        await call.message.edit_text("✅ تم إغلاق مراجعة الأخطاء.")
    await call.answer()


@router.callback_query(F.data.startswith("quiz_feedback_"))
async def open_quiz_feedback_panel(call: types.CallbackQuery):
    """Keep leaderboard visible on the result; place ratings behind one CTA."""
    quiz_id = call.data.removeprefix("quiz_feedback_")
    if not _is_valid_uuid(quiz_id):
        await call.answer("هذا الاختبار غير متاح للتقييم.", show_alert=True)
        return
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="👍 عجبتني الأسئلة", callback_data=f"rate_like_{quiz_id}"),
            types.InlineKeyboardButton(text="👎 فيها خلل", callback_data=f"rate_dislike_{quiz_id}"),
        ],
        [types.InlineKeyboardButton(text="✍️ كتابة ملاحظة", callback_data=f"rate_feedback_{quiz_id}")],
        [types.InlineKeyboardButton(text="✖️ إغلاق", callback_data="feedback_hub_close")],
    ])
    await call.message.answer(
        "💬 <b>عندك ملاحظات؟</b>\n\n"
        "شاركنا تقييمك لجودة الأسئلة، أو اكتب ملاحظة تفصيلية لتحسين الكويز.",
        parse_mode="HTML", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "feedback_hub_close")
async def close_quiz_feedback_panel(call: types.CallbackQuery):
    try:
        await call.message.delete()
    except TelegramBadRequest:
        await call.message.edit_text("✅ أُغلقت خيارات التقييم.")
    await call.answer()


# ==================== معالجات حركة الكويز والتحكم ====================

@router.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer, state: FSMContext):
    try:
        poll_id = poll_answer.poll_id
        data_json = await redis_client.get(f"poll:{poll_id}")
        if not data_json or not poll_answer.option_ids:
            return

        quiz_info = json.loads(data_json)
        correct_opt = int(quiz_info["correct_option_id"])
        
        if poll_answer.user.id != quiz_info["user_id"]:
            return
        
        from handlers.session_navigation import active_quiz, session_id
        from services.redis_reliability import UserLock
        lock = UserLock(redis_client, poll_answer.user.id)
        if not await lock.acquire():
            raise RuntimeError("Quiz session busy; retry poll answer")
        try:
            if not await active_quiz(state):
                return
            current_data = await state.get_data()
            identity = await session_id(state)
            if quiz_info.get("session_id") != identity:
                return
            index = quiz_info.get("question_index")
            if index != current_data.get("current_index"):
                return
            answered = dict(current_data.get("answered_questions") or {})
            if str(index) in answered:
                return
            selected_opt = int(poll_answer.option_ids[0])
            answered[str(index)] = selected_opt
            await state.update_data(answered_questions=answered,
                score=current_data.get('score', 0) + int(selected_opt == correct_opt))
        finally:
            await lock.release()
    except Exception as e:
        log_error(logger, f"Error in handle_poll_answer: {e}", exception=e)
        raise


@router.message(QuizState.answering_quiz, F.text == ".")
async def request_question_edit(msg: types.Message, state: FSMContext):
    """تبدأ التعديل فقط عند الرد على Poll السؤال الجاري بالنقطة."""
    reply = msg.reply_to_message
    if not reply or not reply.poll:
        return
    try:
        poll_data = await redis_client.get(f"poll:{reply.poll.id}")
        if not poll_data:
            return
        poll_info = json.loads(poll_data)
        data = await state.get_data()
        question_index = int(poll_info.get("question_index", -1))
        if (poll_info.get("user_id") != msg.from_user.id or
                question_index != data.get("current_index")):
            return
        questions = data.get("questions", [])
        if not 0 <= question_index < len(questions):
            return

        quiz_id = data.get("quiz_id")
        # 🩹 فحص الصلاحية (مالك الكويز أو الأدمن) الآن مبكراً هون، *قبل* عرض أي واجهة
        # تعديل (نصية أو محرر الرياضيات) - قبلاً كانت تُعرض واجهة التعديل لأي طالب
        # يحل الكويز بغض النظر عن ملكيته، ويُكتشف الرفض فقط لاحقاً عند الحفظ الفعلي
        # (رسالة "⛔ لا تملك صلاحية..." من apply_question_edit_and_resume أو
        # fetch_question_for_edit_web) - بعد خطوة أو خطوتين إضافيتين (فتح صفحة ويب
        # حتى بالنسبة لأسئلة الرياضيات). الفحص هنا لا يلغي الفحص اللاحق بتلك الدوال
        # (يبقى كخط دفاع ثانٍ ضروري لأي مسار لا يمر من هنا، مثل استدعاء API مباشر)،
        # فقط يمنع عرض واجهة لن تنجح أصلاً. لا نطبّقه لو quiz_id غير موجود أصلاً
        # (نفس الحالات القديمة بلا quiz_id تبقى بسلوكها المعتاد لاحقاً بمسار الحفظ).
        if quiz_id:
            creator_id = await get_quiz_creator_id(quiz_id)
            if not can_edit_quiz(msg.from_user.id, creator_id):
                await msg.answer("⛔ لا تملك صلاحية تعديل هذا الكويز. يمكن لمالكه أو الأدمن فقط تعديله.")
                return

        await state.update_data(edit_question_index=question_index)
        await state.set_state(QuizState.waiting_for_question_edit_choice)

        question = questions[question_index]
        # 🆕 لأسئلة الرياضيات المصوّرة (is_math): التعديل النصي البسيط داخل الشات غير
        # كافٍ (السؤال يظهر كصورة، وقد يحتوي جدول/مصفوفة لا يمكن التعبير عنهما بنص
        # عادي). نفتح محرراً كاملاً بصفحة ويب (معاينة LaTeX حية + محرر جدول/مصفوفات)
        # بدلاً من لوحة الاختيار النصية - فقط لو متوفر رابط WebApp ومعرف كويز حقيقي
        # (بدونهما، الحفظ نفسه غير ممكن أصلاً - راجع update_quiz_question)، وإلا
        # نرجع تلقائياً لنفس التعديل النصي البسيط كخط دفاع ثانٍ.
        if question.get("is_math") and quiz_id and WEBAPP_PUBLIC_BASE_URL:
            url = f"{WEBAPP_PUBLIC_BASE_URL}/webapp/question_edit.html?quiz_id={quiz_id}&question_index={question_index}"
            await msg.answer(
                "✏️ هذا سؤال رياضيات مصوّر. افتح المحرر الكامل بالأسفل لتعديل نص السؤال أو "
                "الإجابات أو الجدول أو المصفوفة، مع معاينة حية للصيغة الرياضية:",
                reply_markup=get_math_question_edit_keyboard(url),
            )
            return

        await msg.answer(
            "✏️ يمكنك تعديل السؤال، اختر ما تريد تعديله بالضبط:",
            reply_markup=get_question_edit_keyboard(),
        )
    except Exception as e:
        log_error(logger, f"Error starting question edit: {e}", exception=e)


@router.callback_query(QuizState.waiting_for_question_edit_choice, F.data == "edit_question_text")
async def request_question_text_edit(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    question_index = data.get("edit_question_index")
    questions = data.get("questions", [])
    if not isinstance(question_index, int) or not 0 <= question_index < len(questions):
        await call.answer("❌ انتهت جلسة التعديل، أرسل النقطة مجدداً.", show_alert=True)
        return
    current_text = str(questions[question_index].get("question", ""))
    await state.set_state(QuizState.waiting_for_question_edit_text)
    await call.message.answer(
        "✏️ أرسل الآن نص السؤال المعدّل.\n\n"
        "👇 النص الحالي (اضغط عليه لنسخه، ثم عدّل ما تريد وأرسله):\n"
        f"<code>{html.escape(current_text)}</code>",
        parse_mode="HTML",
        reply_markup=get_cancel_edit_keyboard(),
    )
    await call.answer()


@router.callback_query(QuizState.waiting_for_question_edit_choice, F.data == "edit_question_answer")
async def request_answer_edit(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    question_index = data.get("edit_question_index")
    questions = data.get("questions", [])
    if not isinstance(question_index, int) or not 0 <= question_index < len(questions):
        await call.answer("❌ انتهت جلسة التعديل، أرسل النقطة مجدداً.", show_alert=True)
        return
    await state.set_state(QuizState.waiting_for_answer_edit_choice)
    await call.message.answer(
        "📝 اختر الإجابة التي تريد تعديلها:",
        reply_markup=get_answer_edit_keyboard(questions[question_index].get("options", [])),
    )
    await call.answer()


@router.callback_query(QuizState.waiting_for_answer_edit_choice, F.data.startswith("edit_answer_"))
async def select_answer_to_edit(call: types.CallbackQuery, state: FSMContext):
    try:
        option_index = int(call.data.replace("edit_answer_", ""))
        data = await state.get_data()
        question_index = data.get("edit_question_index")
        questions = data.get("questions", [])
        if not isinstance(question_index, int) or not 0 <= question_index < len(questions):
            raise ValueError("invalid question index")
        options = questions[question_index].get("options", [])
        if not 0 <= option_index < len(options):
            raise ValueError("invalid option index")
        await state.update_data(edit_option_index=option_index)
        await state.set_state(QuizState.waiting_for_answer_edit_text)
        current_answer_text = str(options[option_index])
        await call.message.answer(
            f"✏️ أرسل النص الجديد للإجابة رقم {option_index + 1}.\n\n"
            "👇 النص الحالي (اضغط عليه لنسخه، ثم عدّل ما تريد وأرسله):\n"
            f"<code>{html.escape(current_answer_text)}</code>",
            parse_mode="HTML",
            reply_markup=get_cancel_edit_keyboard(),
        )
        await call.answer()
    except Exception as e:
        log_error(logger, f"Error selecting answer to edit: {e}", exception=e)
        await call.answer("❌ تعذر اختيار الإجابة للتعديل.", show_alert=True)


@router.callback_query(StateFilter(*QUIZ_EDIT_STATES), F.data == "cancel_question_edit")
async def cancel_question_edit(call: types.CallbackQuery, state: FSMContext) -> None:
    """
    🩹 إصلاح خلل حقيقي: قبل هذا الهاندلر، أي دخول لمسار تعديل سؤال (حتى لو بالخطأ -
    مثلاً رد المستخدم بنقطة "." على سؤال رياضيات أو سؤال عادي بغرض آخر) كان بلا مخرج:
    الحالة تنتقل لإحدى QUIZ_EDIT_STATES، وأزرار الكويز الأساسية (التالي/تلميح/إنهاء)
    مقيّدة بـ ACTIVE_QUIZ_STATES فقط فتتوقف عن الاستجابة، فيبدو الكويز "متجمّداً" لحين
    إتمام تعديل فعلي أو مغادرة المحادثة تماماً. هذا الزر يعيد الحالة لـ answering_quiz
    مباشرة دون أي تعديل، فيتابع الطالب اختباره بشكل طبيعي (سؤال الـ Poll نفسه لم
    يُمس ولا يزال قابلاً للإجابة عليه، وأزرار التحكم أسفله تعمل من جديد فوراً).
    """
    try:
        await state.update_data(edit_question_index=None, edit_option_index=None)
        await state.set_state(QuizState.answering_quiz)
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await call.message.answer("↩️ تم التراجع، يمكنك متابعة اختبارك بشكل طبيعي.")
    except Exception as e:
        log_error(logger, f"Error in cancel_question_edit: {e}", exception=e)
    finally:
        await call.answer()


async def apply_question_edit_and_resume(
    chat_id: int,
    user_id: int,
    state: FSMContext,
    question_index: int,
    question: dict,
    edit_type: str,
    option_index: Optional[int] = None,
) -> Tuple[bool, str]:
    """
    🆕 المنطق المشترك لحفظ سؤال مُعدَّل واستئناف الكويز بعده - يُستخدم من مسارين:
    1. التعديل النصي السريع داخل الشات (سؤال عادي، أو حقل واحد بسؤال رياضيات).
    2. محرر أسئلة الرياضيات الكامل عبر صفحة الويب (نص + إجابات + جدول + مصفوفات
       سوا بنداء واحد - راجع save_question_edit_from_web أدناه).
    يرجع (نجح؟, رسالة تُعرض للمستخدم) بدل إرسال الرسالة مباشرة، لأن المسار الثاني
    (من خلفية webhook_server.py) لا يملك أصلاً كائن Message لإرسال رد عليه.
    """
    data = await state.get_data()
    questions = data.get("questions", [])
    if not 0 <= question_index < len(questions):
        await state.set_state(QuizState.answering_quiz)
        return False, "❌ انتهت جلسة التعديل، أرسل النقطة على السؤال مرة أخرى."

    quiz_id = data.get("quiz_id")
    # 🧹 إبطال أي صورة قديمة مخزنة للسؤال المعدّل، لأن أسئلة الرياضيات تُرسم كصورة
    # ويجب إعادة رسمها من البيانات الجديدة بعد كل تعديل، وإلا سيستمر Telegram في
    # إظهار النسخة القديمة من `image_url` داخل نفس الجلسة أو حتى في السجل المركزي.
    normalized_question = _sanitize_question_for_render(question)

    saved = await update_quiz_question(quiz_id, question_index, normalized_question, user_id) if quiz_id else None
    if saved is None:
        await state.set_state(QuizState.answering_quiz)
        return False, "⛔ لا تملك صلاحية تعديل هذا الكويز. يمكن لمالكه أو الأدمن فقط تعديله."
    if not saved:
        await state.set_state(QuizState.answering_quiz)
        return False, "❌ تعذر حفظ التعديل في قاعدة البيانات، ولم يتم تغيير الكويز."

    spawn(log_usage_event(user_id, "quiz_question_edited", {
        "quiz_id": quiz_id,
        "question_index": question_index,
        "edit_type": edit_type,
        "option_index": option_index,
        "database_updated": True,
    }))
    questions = list(questions)
    questions[question_index] = normalized_question
    await state.update_data(questions=questions, current_index=question_index)
    await state.set_state(QuizState.answering_quiz)
    await send_question_by_ids(chat_id, user_id, state)
    return True, "✅ تم تعديل السؤال بنجاح ! يمكنك الان متابعة اختبارك."


async def _save_edited_question(
    msg: types.Message,
    state: FSMContext,
    question_index: int,
    question: dict,
    edit_type: str,
    option_index: Optional[int] = None,
) -> None:
    """غلاف رقيق فوق apply_question_edit_and_resume لمسار التعديل النصي داخل الشات."""
    _, reply_text = await apply_question_edit_and_resume(
        msg.chat.id, msg.from_user.id, state, question_index, question, edit_type, option_index,
    )
    await msg.answer(reply_text)


async def fetch_question_for_edit_web(chat_id: int, user_id: int, quiz_id: str, question_index: int) -> Tuple[Optional[dict], Optional[str]]:
    """
    🆕 تُستدعى من webhook_server.py (POST /api/question-edit/fetch) للتحقق من صلاحية
    جلسة تعديل سؤال رياضي عبر صفحة الويب، وإرجاع بيانات السؤال الحالية لتعبئة النموذج.
    الحماية: نبني FSMContext الحقيقي لنفس المستخدم (نفس آلية build_state_for_chat
    المستخدمة برفع الصوت/الملفات عبر الويب) ونتحقق أن الحالة الحالية بالضبط
    waiting_for_question_edit_choice (الحالة التي تدخلها request_question_edit فور
    الرد بنقطة)، وأن quiz_id/question_index مطابقان تماماً لما خزّنته تلك الدالة -
    لا نثق بأي قيمة قادمة من الصفحة نفسها بمعزل عن جلسة FSM الفعلية.

    🩹 إصلاح خلل حقيقي: مسار التعديل النصي العادي (غير الرياضيات) يتحقق دائماً من
    صلاحية "المالك أو الأدمن" (can_edit_quiz) عند الحفظ الفعلي عبر update_quiz_question
    بـ apply_question_edit_and_resume، بينما محرر الرياضيات الكامل هنا كان يكتفي فقط
    بصحة جلسة FSM (الطالب الجالس فعلياً يحل هذا الكويز بالذات الآن) بدون أي تحقق من
    creator_id/الأدمن على الإطلاق - أي طالب يحل كويزاً رياضياً *مشتركاً* من غيره كان
    يقدر يفتح المحرر ويشاهد بيانات السؤال (حتى لو الحفظ لاحقاً كان سيُرفض بصمت من
    طبقة أعمق). نفس فحص can_edit_quiz المستخدم بمسار الحذف/التعديل العادي مُضاف هنا
    الآن مبكراً - بنفس طريقة get_quiz_creator_id الخفيفة (بدون جلب quiz_data الثقيل).

    🩹 إصلاح إضافي: ترجع الآن (question, رسالة الرفض) بدل question فقط - قبلاً كل
    أسباب الفشل (جلسة منتهية/quiz_id غير مطابق/عدم امتلاك صلاحية) كانت تُرجع None
    بلا تمييز، فكانت webhook_server.py تعرض نفس الرسالة العامة "الجلسة منتهية، أعد
    المحاولة" حتى لطالب لا يملك صلاحية الكويز أصلاً - وإعادة المحاولة معه لن تنجح
    أبداً مهما فعل. الآن حالة "لا صلاحية" ترجع رسالتها الدقيقة الخاصة بها (نفس نص
    الرسالة المستخدم أصلاً بـ save_question_edit_from_web أدناه لتوحيد الصياغة عبر
    مساري الجلب/الحفظ)، وبقية حالات الفشل (جلسة/تطابق) ترجع None للرسالة فتعرض
    الرسالة العامة الافتراضية كما كانت (هي فعلاً السبب الصحيح بتلك الحالات).
    """
    state = build_state_for_chat(chat_id, user_id)
    current_state = await state.get_state()
    if current_state != QuizState.waiting_for_question_edit_choice:
        return None, None
    data = await state.get_data()
    if data.get("quiz_id") != quiz_id or data.get("edit_question_index") != question_index:
        return None, None
    questions = data.get("questions", [])
    if not 0 <= question_index < len(questions):
        return None, None
    question = questions[question_index]
    if not question.get("is_math"):
        return None, None
    creator_id = await get_quiz_creator_id(quiz_id)
    if not can_edit_quiz(user_id, creator_id):
        log_warning(logger, f"Rejected math question fetch by user {user_id} for quiz {quiz_id} (not owner/admin)")
        return None, "⛔ لا تملك صلاحية تعديل هذا الكويز. يمكن لمالكه أو الأدمن فقط تعديله."
    return question, None


async def save_question_edit_from_web(
    chat_id: int,
    user_id: int,
    quiz_id: str,
    question_index: int,
    question_text: str,
    options: List[str],
    table: Optional[Dict[str, Any]],
    matrices: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    """
    🆕 نظير _save_edited_question لكن لمحرر الويب الكامل (نص + إجابات + جدول +
    مصفوفات سوا بنداء واحد بدل التعديل المجزّأ داخل الشات). نفس تحقق الصلاحية
    المستخدم بـ fetch_question_for_edit_web أعلاه بالضبط - راجعها لتفاصيل الحماية.
    """
    state = build_state_for_chat(chat_id, user_id)
    current_state = await state.get_state()
    if current_state != QuizState.waiting_for_question_edit_choice:
        return False, "❌ انتهت جلسة التعديل، ارجع للمحادثة وأرسل النقطة على السؤال من جديد."
    data = await state.get_data()
    if data.get("quiz_id") != quiz_id or data.get("edit_question_index") != question_index:
        return False, "❌ انتهت جلسة التعديل، ارجع للمحادثة وأرسل النقطة على السؤال من جديد."
    questions = data.get("questions", [])
    if not 0 <= question_index < len(questions):
        return False, "❌ انتهت جلسة التعديل، ارجع للمحادثة وأرسل النقطة على السؤال من جديد."
    original = questions[question_index]
    if not original.get("is_math"):
        return False, "❌ هذا المحرر متاح فقط لأسئلة الرياضيات المصوّرة."

    # 🩹 إصلاح خلل حقيقي: نفس فحص can_edit_quiz (مالك الكويز أو الأدمن) المطبَّق
    # مسبقاً بمسار التعديل النصي العادي (عبر update_quiz_question بنهاية
    # apply_question_edit_and_resume تحت) - مُضاف هنا الآن أيضاً بشكل مبكر وصريح
    # قبل أي معالجة إضافية (بدل الاعتماد فقط على الفحص المتأخر بتلك الدالة نهاية
    # هذا المسار)، برسالة رفض مطابقة تماماً لرسالة المسار العادي.
    creator_id = await get_quiz_creator_id(quiz_id)
    if not can_edit_quiz(user_id, creator_id):
        log_warning(logger, f"Rejected math question save by user {user_id} for quiz {quiz_id} (not owner/admin)")
        return False, "⛔ لا تملك صلاحية تعديل هذا الكويز. يمكن لمالكه أو الأدمن فقط تعديله."

    original_options = original.get("options") or []
    if len(options) != len(original_options):
        return False, "❌ عدد الإجابات يجب أن يبقى كما هو (لا يمكن إضافة أو حذف إجابة من هذا المحرر)."

    question = dict(original)
    question["question"] = question_text
    question["options"] = options
    if table and (table.get("headers") or table.get("rows")):
        question["table"] = table
    else:
        question.pop("table", None)
    question["matrices"] = matrices or []
    # 🆕 نفس مبدأ التعديل النصي: إبطال الصورة المولَّدة سابقاً حتى يُعاد رسمها من
    # القيم الجديدة عند إعادة عرض السؤال (راجع send_question_by_ids → send_quiz_poll).
    for stale_key in _STALE_QUESTION_CACHE_KEYS:
        question.pop(stale_key, None)

    return await apply_question_edit_and_resume(chat_id, user_id, state, question_index, question, "web_full_edit")


@router.message(QuizState.waiting_for_question_edit_text, F.text)
async def save_question_text_edit(msg: types.Message, state: FSMContext):
    text = msg.text.strip()
    if not text or len(text) > 2000:
        await msg.answer("❌ نص السؤال غير صالح، أرسله بحد أقصى 2000 حرف.")
        return
    data = await state.get_data()
    question_index = data.get("edit_question_index")
    questions = data.get("questions", [])
    if not isinstance(question_index, int) or not 0 <= question_index < len(questions):
        await msg.answer("❌ انتهت جلسة التعديل، أرسل النقطة على السؤال مرة أخرى.")
        await state.set_state(QuizState.answering_quiz)
        return
    question = dict(questions[question_index])
    question["question"] = text
    question.pop("image_url", None)
    await _save_edited_question(msg, state, question_index, question, "question_text")


@router.message(QuizState.waiting_for_answer_edit_text, F.text)
async def save_answer_text_edit(msg: types.Message, state: FSMContext):
    text = msg.text.strip()
    if not text or len(text) > 500:
        await msg.answer("❌ نص الإجابة غير صالح، أرسله بحد أقصى 500 حرف.")
        return
    data = await state.get_data()
    question_index = data.get("edit_question_index")
    option_index = data.get("edit_option_index")
    questions = data.get("questions", [])
    if (not isinstance(question_index, int) or not isinstance(option_index, int) or
            not 0 <= question_index < len(questions)):
        await msg.answer("❌ انتهت جلسة التعديل، أرسل النقطة على السؤال مرة أخرى.")
        await state.set_state(QuizState.answering_quiz)
        return
    question = dict(questions[question_index])
    options = list(question.get("options", []))
    if not 0 <= option_index < len(options):
        await msg.answer("❌ تعذر العثور على الإجابة المطلوبة.")
        await state.set_state(QuizState.answering_quiz)
        return
    options[option_index] = text
    question["options"] = options
    question.pop("image_url", None)
    await _save_edited_question(msg, state, question_index, question, "answer_text", option_index)

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data == "next_question")
@user_locked()
async def handle_next(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        if data.get("is_switching_question"):
            await call.answer()
            return
            
        await state.update_data(is_switching_question=True, current_index=data['current_index'] + 1)
        try:
            await call.message.delete()
        except Exception:
            pass
        await send_question(call, state)
    except Exception as e:
        log_error(logger, f"Error in handle_next: {e}", exception=e)
        await state.update_data(is_switching_question=False)
    finally:
        await call.answer()

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data == "get_hint")
async def handle_hint(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        q = data['questions'][data['current_index']]
        hint_text = q['hint']
        if q.get("is_math"):
            # التلميح يُعرض داخل تنبيه Telegram عادي لا يدعم LaTeX إطلاقاً، لذا
            # نحوّل أي صيغة LaTeX (حتى لو تسرّبت رغم تعليمات البرومبت) لنص عادي
            # مقروء (كسور/جذور/أسس/رموز يونانية...) بدل تجريد $ فقط وترك الأوامر خام.
            hint_text = latex_to_plain(hint_text)
        # 🩹 UX: show_alert=True لأن التلميح نص يحتاج وقتاً ليُقرأ؛ الإشعار الخاطف
        # (toast) كان يختفي خلال ثانية أو ثانيتين قبل أن يتمكن الطالب من قراءته كاملاً.
        await call.answer(f"💡 تلميح ذكي:\n{hint_text}", show_alert=True)
    except Exception as e:
        log_error(logger, f"Error in handle_hint: {e}", exception=e)
        await call.answer("❌ خطأ في جلب التلميح", show_alert=True)

@router.callback_query(QuizState.answering_quiz, F.data == "start_first_question")
async def start_quiz_after_warning(call: types.CallbackQuery, state: FSMContext):
    try:
        await call.message.delete()
        await send_question(call, state)
    except Exception as e:
        log_error(logger, f"Error in start_quiz_after_warning: {e}", exception=e)
    finally:
        await call.answer()

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data == "quiz_stop")
async def request_stop_confirmation(call: types.CallbackQuery, state: FSMContext):
    try:
        confirm_kb = get_quiz_exit_confirmation_keyboard()
        await call.message.answer(
            "🏁 <b>تأكيد إنهاء الاختبار</b>\n\n"
            "هل أنت متأكد من رغبتك في إنهاء الكويز الآن؟ سيتم احتساب نتيجتك الحالية بناءً على الأسئلة التي أجبت عليها حتى هذه اللحظة.",
            reply_markup=confirm_kb,
            parse_mode="HTML"
        )
    except Exception as e:
        log_error(logger, f"Error requesting stop confirmation: {e}")
    finally:
        await call.answer()

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data == "quiz_stop_confirmed")
@user_locked()
async def stop_quiz_confirmed(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        questions = data.get("questions", [])
        # 🩹 نضع علامة صريحة على التوقف المبكر بدل الاعتماد على مساواة current_index
        # بـ len(questions) (كان هذا يجعل _handle_quiz_completion يحتسبها دائماً
        # "مكتملة" لأن send_question لا يستدعيها إلا عندما current_index >= total أصلاً).
        await state.update_data(current_index=len(questions), stopped_early=True)
        try:
            await call.message.delete()
        except Exception:
            pass
        await send_question(call, state)
    except Exception as e:
        log_error(logger, f"Error in stop_quiz_confirmed: {e}", exception=e)
        await call.answer("❌ تعذر إنهاء الكويز", show_alert=True)
    finally:
        await call.answer()

@router.callback_query(StateFilter(*ACTIVE_QUIZ_STATES), F.data == "quiz_resume_flow")
async def resume_quiz_flow(call: types.CallbackQuery):
    try:
        await call.message.delete()
        await call.answer("🔄 ممتاز! تم إلغاء الإيقاف، يمكنك مواصلة حل أسئلتك الآن بنجاح.", show_alert=True)
    except Exception as e:
        log_error(logger, f"Error in resume_quiz_flow: {e}")
    finally:
        await call.answer()

@router.callback_query(F.data == "quiz_replay")
@user_locked()
async def replay_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        questions = data.get("questions", [])
        if not questions:
            await call.answer("❌ لا يوجد كويز محفوظ لإعادة تشغيله", show_alert=True)
            return
        
        from handlers.session_navigation import offer_transition
        if await offer_transition(state, call.from_user.id, call.message.chat.id, {"kind": "loaded", "kwargs": {
                "quiz_data": questions, "source_title": data.get("source_title", "كويز"),
                "origin": data.get("quiz_origin", "shared"), "quiz_id": data.get("quiz_id") or ""}}):
            return
        await state.set_state(QuizState.answering_quiz)
        # 🩹 إعادة التشغيل يجب أن تُسجَّل كمحاولة جديدة تماماً في قاعدة البيانات، وإلا
        # فإن complete_quiz_attempt اللاحقة ستستخدم نفس attempt_id القديم وتكتب فوق
        # بيانات المحاولة الأولى (النتيجة والمدة) بدل تسجيل محاولة مستقلة.
        user_id = call.from_user.id
        new_attempt_id = start_quiz_attempt(user_id, data.get("quiz_id") or None, data.get("quiz_origin", "shared"), len(questions))
        spawn(log_usage_event(user_id, "quiz_started", {
            "origin": data.get("quiz_origin", "shared"), "quiz_id": data.get("quiz_id"), "questions": len(questions),
        }))
        await state.update_data(
            current_index=0, score=0, answered_questions={}, navigation_session_id=None, quiz_completed=False, is_switching_question=False,
            stopped_early=False, attempt_id=new_attempt_id
        )
        try:
            await call.message.delete()
        except Exception:
            pass
        await send_question(call, state)
    except Exception as e:
        log_error(logger, f"Error in replay_quiz: {e}", exception=e)
        await call.answer("❌ تعذر إعادة تشغيل الكويز", show_alert=True)
    finally:
        await call.answer()

@router.callback_query(F.data == "quiz_home")
async def quiz_home(call: types.CallbackQuery, state: FSMContext):
    try:
        # Old result messages can be tapped during a newer quiz. Navigation is read-only.
        await send_main_menu(call, call.from_user.id)
    finally:
        await call.answer()

@router.callback_query(F.data == "ignored")
async def handle_ignored_click(call: types.CallbackQuery):
    await call.answer("✅ تم تسجيل إجابتك")

# ==================== معالجات التقييم والملاحظات ====================

@router.callback_query(F.data.startswith("rate_like_"))
async def rate_like_quiz(call: types.CallbackQuery):
    try:
        quiz_id = call.data.replace("rate_like_", "")
        success = await submit_quiz_vote(quiz_id, call.from_user.id, "like")
        if success:
            spawn(log_usage_event(call.from_user.id, "quiz_rated", {"quiz_id": quiz_id, "vote": "like"}))
            await call.answer("👍 شكراً لك! تم تسجيل إعجابك وتحديث تقييم الاختبار بنجاح.", show_alert=True)
        else:
            await call.answer("⚠️ لقد قمت بالتصويت على هذا الاختبار مسبقاً!", show_alert=True)
    except Exception as e:
        log_error(logger, f"Error in rate_like_quiz: {e}")
        await call.answer("❌ حدث خطأ أثناء معالجة الإعجاب.", show_alert=True)

@router.callback_query(F.data.startswith("rate_dislike_"))
async def rate_dislike_quiz(call: types.CallbackQuery):
    try:
        quiz_id = call.data.replace("rate_dislike_", "")
        success = await submit_quiz_vote(quiz_id, call.from_user.id, "dislike")
        if success:
            spawn(log_usage_event(call.from_user.id, "quiz_rated", {"quiz_id": quiz_id, "vote": "dislike"}))
            await call.answer("👎 تم احتساب تقييمك السلبي. سيتولى النظام تصفية وحذف الاختبارات الرديئة تلقائياً.", show_alert=True)
        else:
            await call.answer("⚠️ لقد قمت بالتصويت على هذا الاختبار مسبقاً!", show_alert=True)
    except Exception as e:
        log_error(logger, f"Error in rate_dislike_quiz: {e}")
        await call.answer("❌ حدث خطأ أثناء تسجيل التقييم السلبي.", show_alert=True)

@router.callback_query(F.data.startswith("rate_feedback_"))
async def prompt_feedback(call: types.CallbackQuery, state: FSMContext):
    try:
        quiz_id = call.data.replace("rate_feedback_", "")
        await state.update_data(feedback_quiz_id=quiz_id)
        await state.set_state(QuizState.waiting_for_quiz_feedback)
        await call.message.answer(MSG_FEEDBACK_PROMPT, parse_mode="HTML")
    except Exception as e:
        log_error(logger, f"Error in prompt_feedback: {e}")
        await call.answer("❌ تعذر فتح واجهة الملاحظات والشكاوى.", show_alert=True)
    finally:
        await call.answer()

@router.message(QuizState.waiting_for_quiz_feedback, F.text)
async def process_quiz_feedback(msg: types.Message, state: FSMContext):
    try:
        comment = msg.text.strip()
        if not comment:
            await msg.answer("❌ الملاحظة فارغة، يرجى كتابة نص شكوى واضح ومفهوم:")
            return
        
        data = await state.get_data()
        quiz_id = data.get("feedback_quiz_id")
        if quiz_id:
            await save_quiz_feedback(quiz_id, msg.from_user.id, comment[:500])
            spawn(log_usage_event(msg.from_user.id, "feedback_submitted", {"quiz_id": quiz_id}))
            await msg.answer(MSG_FEEDBACK_SAVED, parse_mode="HTML")
        else:
            await msg.answer("❌ حدث خطأ داخلي، لم يتم العثور على المعرف المركزي لهذا الاختبار.")
            
        await state.set_state(None)
    except Exception as e:
        log_error(logger, f"Error in process_quiz_feedback: {e}")
        await msg.answer("❌ نعتذر منك، حدث خطأ أثناء إرسال ملاحظتك.")

# ==================== معالجات ويزارد حفظ الكويز للمفضلة ====================

@router.callback_query(StateFilter(*SAVE_WIZARD_STATES), F.data.in_({"save_quiz", "quiz_favorite"}))
async def handle_save_quiz_start(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        if data.get("is_saved_in_session"):
            await call.answer("✅ هذا الكويز محفوظ بالفعل في قائمتك المفضلة!", show_alert=True)
            return

        questions, quiz_id = data.get("questions"), data.get("quiz_id")
        if not questions:
            await call.answer("❌ لا يوجد كويز لحفظه!", show_alert=True)
            return

        user_favorites = await list_favorite_quizzes(call.from_user.id)
        if user_favorites and quiz_id and any(fav.get("quiz_id") == quiz_id for fav in user_favorites):
            await state.update_data(is_saved_in_session=True)
            await call.answer("💡 هذا الكويز موجود بالفعل في قائمتك المفضلة مسبقاً!", show_alert=True)
            return

        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="📄 حفظ بالاسم الحالي", callback_data="save_name_current")],
            [types.InlineKeyboardButton(text="✏️ حفظ باسم مخصص", callback_data="save_name_custom")]
        ])
        await call.message.answer("📝 **خطوة 1 من 2: تسمية الاختبار**\n\nكيف تود تسمية هذا الكويز في المفضلة؟", reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        log_error(logger, f"Error starting save wizard: {e}", exception=e)
        await call.answer("❌ حدث خطأ أثناء بدء عملية الحفظ.", show_alert=True)
    finally:
        await call.answer()

@router.callback_query(StateFilter(*SAVE_WIZARD_STATES), F.data == "save_name_current")
async def save_name_current_handler(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        title = data.get("source_title") or data.get("title") or "كويز بدون عنوان"
        await state.update_data(final_save_title=title)
        await _prompt_section_selection(call.message, state)
        try:
            await call.message.delete()
        except Exception:
            pass
    except Exception as e:
        log_error(logger, f"Error in save_name_current: {e}", exception=e)
    finally:
        await call.answer()

@router.callback_query(StateFilter(*SAVE_WIZARD_STATES), F.data == "save_name_custom")
async def save_name_custom_handler(call: types.CallbackQuery, state: FSMContext):
    try:
        await state.set_state(QuizState.waiting_for_custom_name)
        await call.message.edit_text("✏️ **أرسل الآن الاسم المخصص** الذي تريده لهذا الاختبار في رسالة نصية مباشرة:")
    except Exception as e:
        log_error(logger, f"Error in save_name_custom: {e}", exception=e)
    finally:
        await call.answer()

@router.message(QuizState.waiting_for_custom_name, F.text)
async def process_custom_name(msg: types.Message, state: FSMContext):
    try:
        custom_title = msg.text.strip()
        if not custom_title:
            await msg.answer("❌ الاسم المرسل غير صالح، يرجى إرسال اسم نصي واضح:")
            return
        await state.update_data(final_save_title=custom_title)
        await _prompt_section_selection(msg, state)
    except Exception as e:
        log_error(logger, f"Error in process_custom_name: {e}", exception=e)

async def _prompt_section_selection(msg_or_call_msg: types.Message, state: FSMContext):
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🌐 حفظ في قسم عام", callback_data="save_sec_general")],
        [types.InlineKeyboardButton(text="📁 حفظ ضمن قسم مخصص", callback_data="save_sec_choose")]
    ])
    await msg_or_call_msg.answer("📁 **خطوة 2 من 2: تصنيف مكان الحفظ**\n\nأين تريد تصنيف هذا الاختبار في المفضلة؟", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(StateFilter(*SAVE_WIZARD_STATES), F.data == "save_sec_general")
async def handle_save_general(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        title, questions, quiz_id = data.get("final_save_title") or "كويز بدون عنوان", data.get("questions"), data.get("quiz_id")
        
        fav_id = await save_favorite_quiz(call.from_user.id, title, questions, None, None, quiz_id)
        if not fav_id:
            await call.answer("❌ تعذر حفظ الكويز في المفضلة، حاول مجدداً.", show_alert=True)
            return
        await state.update_data(is_saved_in_session=True)
        spawn(log_usage_event(call.from_user.id, "quiz_saved_favorite", {"quiz_id": quiz_id, "section": "عام"}))
        await call.message.edit_text(f"✅ **تم الحفظ بنجاح!**\n\n📦 الاسم: `{title}`\n🗂 القسم: `عام`", parse_mode="Markdown")
        await state.set_state(None if data.get("quiz_completed") else QuizState.answering_quiz)
    except Exception as e:
        log_error(logger, f"Error saving to general: {e}", exception=e)
        await call.answer("❌ حدث خطأ أثناء حفظ الكويز.", show_alert=True)
    finally:
        await call.answer()

@router.callback_query(StateFilter(*SAVE_WIZARD_STATES), F.data == "save_sec_choose")
async def handle_save_choose_section(call: types.CallbackQuery, state: FSMContext):
    try:
        sections = await list_favorite_sections(call.from_user.id)
        inline_keyboard = [[types.InlineKeyboardButton(text=f"📁 {sec['title']}", callback_data=f"save_to_sec_{sec['section_id']}")] for sec in (sections or [])]
        inline_keyboard.append([types.InlineKeyboardButton(text="➕ إنشاء قسم جديد واختياره", callback_data="save_sec_create_new")])
        inline_keyboard.append([types.InlineKeyboardButton(text="🌐 إلغاء وحفظ في عام", callback_data="save_sec_general")])
        
        await call.message.edit_text("📂 **اختر أحد أقسامك المفضلة الحالية لتصنيف الاختبار داخله:**", reply_markup=types.InlineKeyboardMarkup(inline_keyboard=inline_keyboard), parse_mode="Markdown")
    except Exception as e:
        log_error(logger, f"Error showing sections: {e}", exception=e)
        await call.answer("❌ تعذر جلب قائمة الأقسام حالياً.", show_alert=True)
    finally:
        await call.answer()

@router.callback_query(StateFilter(*SAVE_WIZARD_STATES), F.data.startswith("save_to_sec_"))
async def handle_save_to_existing_section(call: types.CallbackQuery, state: FSMContext):
    try:
        section_id = call.data.replace("save_to_sec_", "")
        data = await state.get_data()
        title, questions, quiz_id = data.get("final_save_title") or "كويز بدون عنوان", data.get("questions"), data.get("quiz_id")
        
        fav_id = await save_favorite_quiz(call.from_user.id, title, questions, section_id, None, quiz_id)
        if not fav_id:
            await call.answer("❌ تعذر حفظ الكويز ضمن هذا القسم، حاول مجدداً.", show_alert=True)
            return
        await state.update_data(is_saved_in_session=True)
        spawn(log_usage_event(call.from_user.id, "quiz_saved_favorite", {"quiz_id": quiz_id, "section_id": section_id}))
        await call.message.edit_text(f"✅ **تم حفظ الاختبار بنجاح ضمن القسم المختار!**\n\n📦 الاسم: `{title}`", parse_mode="Markdown")
        await state.set_state(None if data.get("quiz_completed") else QuizState.answering_quiz)
    except Exception as e:
        log_error(logger, f"Error saving to existing section: {e}", exception=e)
    finally:
        await call.answer()

@router.callback_query(StateFilter(*SAVE_WIZARD_STATES), F.data == "save_sec_create_new")
async def handle_request_new_section(call: types.CallbackQuery, state: FSMContext):
    try:
        await state.set_state(QuizState.waiting_for_new_section_title)
        await call.message.edit_text("➕ **أرسل الآن اسم القسم الجديد** المراد إنشاؤه لتصنيف الكويز داخله:")
    except Exception as e:
        log_error(logger, f"Error in request new section: {e}", exception=e)
    finally:
        await call.answer()

@router.message(QuizState.waiting_for_new_section_title, F.text)
async def process_new_section_title_and_save(msg: types.Message, state: FSMContext):
    try:
        section_title = msg.text.strip()
        if not section_title:
            await msg.answer("❌ اسم القسم غير صالح، يرجى إدخال نص واضح:")
            return
            
        user_id = msg.from_user.id
        data = await state.get_data()
        title, questions, quiz_id = data.get("final_save_title") or "كويز بدون عنوان", data.get("questions"), data.get("quiz_id")
        
        new_section_id = await create_favorite_section(user_id, section_title)
        fav_id = await save_favorite_quiz(user_id, title, questions, new_section_id, None, quiz_id)
        if not fav_id:
            await msg.answer("❌ تعذر حفظ الاختبار، حاول مجدداً.")
            await state.set_state(None if data.get("quiz_completed") else QuizState.answering_quiz)
            return
        await state.update_data(is_saved_in_session=True)
        spawn(log_usage_event(user_id, "quiz_saved_favorite", {"quiz_id": quiz_id, "new_section": section_title}))
        await msg.answer(f"✅ **تم إنشاء القسم وحفظ الاختبار بنجاح!**\n\n📦 الاسم: `{title}`\n🗂 القسم الجديد: `{section_title}`", parse_mode="Markdown")
        await state.set_state(None if data.get("quiz_completed") else QuizState.answering_quiz)
    except Exception as e:
        log_error(logger, f"Error in creating section and saving: {e}", exception=e)
        await state.set_state(QuizState.answering_quiz)

@router.callback_query(F.data == "force_stop_previous_quiz")
@user_locked()
async def force_stop_previous_quiz_handler(call: types.CallbackQuery, state: FSMContext):
    from handlers.session_navigation import offer_transition
    await call.answer()
    await offer_transition(state, call.from_user.id, call.message.chat.id, {"kind": "stop"})

@router.callback_query(F.data == "delete_warning_msg")
async def delete_warning_msg_handler(call: types.CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    finally:
        await call.answer()

quiz_runner_router = router
