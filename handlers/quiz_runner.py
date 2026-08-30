# Handlers/quiz_runner.py
import asyncio
import html
import json
from typing import Union, Optional, List, Dict, Any, Tuple

from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

from config import bot, QuizState, redis_client
from constants import (
    MSG_QUIZ_STOPPED, MSG_FEEDBACK_PROMPT, MSG_FEEDBACK_SAVED,
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
)
from logger import get_logger, log_error, log_info, log_warning
from services.latex_text import latex_to_plain
from handlers.audio import _build_state_for_chat  # 🆕 نفس بناء FSMContext اليدوي المستخدم لاستئناف الكويز من خلفية (محرر أسئلة الرياضيات عبر الويب)
from supabase_helper import (
    list_favorite_quizzes,
    update_user_stats,
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
    _is_valid_uuid
)
from services.quiz_engine import send_quiz_poll

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
    """يزيل أي بيانات صورة/كاش قديمة عن سؤال رياضي بحيث يعاد رسمه من البيانات الجديدة."""
    if not isinstance(question, dict):
        return {}
    cleaned = dict(question)
    for key in _STALE_QUESTION_CACHE_KEYS:
        cleaned.pop(key, None)
    return cleaned

# 🩹 نفس حالات الكويز النشط + None: تُستخدم حصراً لهاندلرات ويزارد "حفظ في المفضلة"
# لأن state يُصفَّر إلى None عند اكتمال الكويز (_handle_quiz_completion) قبل أن
# يصل المستخدم لصفحة النتيجة، وزر "⭐ حفظ في المفضلة" يظهر هناك تحديداً.
# لا تُستخدم لهاندلرات الكويز النشط الأخرى (next_question, get_hint, quiz_stop...)
# التي يجب أن تبقى مقيدة بحالة كويز فعلياً جارٍ.
SAVE_WIZARD_STATES = ACTIVE_QUIZ_STATES + (None,)

async def _send_main_menu(call_or_message: Union[types.Message, types.CallbackQuery], user_id: int) -> None:
    bot_info = await bot.get_me()
    menu = await get_main_menu_keyboard(bot_info.username, user_id)
    text = "🏠 القائمة الرئيسية"
    if isinstance(call_or_message, types.CallbackQuery):
        await call_or_message.message.answer(text, reply_markup=menu)
    else:
        await call_or_message.answer(text, reply_markup=menu)

@router.callback_query(F.data == "show_menu_after_first_quiz")
async def show_menu_after_first_quiz(call: types.CallbackQuery) -> None:
    """🆕 زر "📋 عرض القائمة" برسالة تهنئة أول اختبار مكتمل (راجع _handle_quiz_completion
    تحت) - يُظهر القائمة الرئيسية عند الطلب فقط بدل إرفاقها تلقائياً، تخفيفاً للازدحام
    البصري. يزيل الزر نفسه من الرسالة بعد الضغط لتفادي ضغطه أكثر من مرة بلا داعٍ."""
    try:
        await _send_main_menu(call, call.from_user.id)
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    except Exception as e:
        log_error(logger, f"Error in show_menu_after_first_quiz for {call.from_user.id}: {e}", exception=e)
    finally:
        await call.answer()

async def _start_loaded_quiz(msg_or_call: Union[types.Message, types.CallbackQuery], state: FSMContext, quiz_data: list, source_title: str, origin: str = "shared", quiz_id: Optional[str] = "") -> None:
    user_id = msg_or_call.from_user.id
    attempt_id = start_quiz_attempt(user_id, quiz_id or None, origin, len(quiz_data))
    asyncio.create_task(log_usage_event(user_id, "quiz_started", {
        "origin": origin, "quiz_id": quiz_id, "questions": len(quiz_data),
    }))

    await state.update_data(
        questions=quiz_data, current_index=0, score=0,
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

        # 2. تجهيز وإرسال السؤال الحالي
        q = _sanitize_question_for_render(questions[idx])
        questions[idx] = q
        await state.update_data(questions=questions)
        control_kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="💡 طلب تلميح", callback_data="get_hint")],
            [
                types.InlineKeyboardButton(text="⏹ إنهاء", callback_data="quiz_stop"),
                types.InlineKeyboardButton(text="🔗 مشاركة", callback_data="quiz_share"),
                types.InlineKeyboardButton(text="💾 حفظ", callback_data="save_quiz")
            ],
            [types.InlineKeyboardButton(text="التالي ➡️", callback_data="next_question")]
        ])

        await send_quiz_poll(chat_id, user_id, q, idx, len(questions), control_kb, quiz_id=data.get('quiz_id'))
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
        f"🏁 <b>اكتمل الاختبار بنجاح!</b>\n\n"
        f"🎯 نتيجتك الحالية: <b>{score}</b> من <b>{total}</b>\n"
        f"📊 النسبة المئوية: <b>{percentage:.1f}%</b>\n"
        f"{previous_score_text}\n"
        f"{'🏆 ممتاز!' if percentage >= 80 else '👍 جيد!' if percentage >= 60 else '📚 استمر في الممارسة!'}"
    )

    from supabase_helper import has_completed_any_quiz_before  # استيراد محلي لتفادي دورة استيراد غير ضرورية

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
        keyboard = get_rating_keyboard(quiz_id, quiz_id=quiz_id)
        result_text += "\n\n⭐ <b>كيف تقيم هذا الكويز؟</b> تقييمك المباشر يساعد الدفعة على فرز الكويزات الممتازة وتصفية الرديئة تلقائياً!"
    else:
        keyboard = get_quiz_result_keyboard(quiz_id=quiz_id)

    await bot.send_message(chat_id, result_text, reply_markup=keyboard, parse_mode="HTML")

    # 🆕 أول اختبار كامل لمستخدم جديد: نعرض رسالة تهنئة قصيرة بعد النتيجة مباشرة، لكن
    # بزر واحد فقط "📋 عرض القائمة" بدل إرفاق القائمة الرئيسية كاملة مباشرة - تخفيفاً
    # للازدحام البصري (طلب صريح من المستخدم 2026-08-28). الضغط على الزر هو ما يستدعي
    # فعلياً القائمة الرئيسية عبر المعالج الجديد show_main_menu_after_first_quiz تحت.
    if is_first_completion:
        try:
            await bot.send_message(
                chat_id,
                "🎉 <b>أحسنت في أول اختبار لك!</b>\n\n"
                "💡 تقدر تولّد كويزات جديدة في أي وقت تحب بنفس الطريقة اللي سويتها قبل شوي.\n"
                "🔄 ونقاطك تتجدد يومياً، فما تحتاج تقلق إذا خلصتها.",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="📋 عرض القائمة", callback_data="show_menu_after_first_quiz")]
                ]),
                parse_mode="HTML",
            )
        except Exception as e:
            log_error(logger, f"Error sending first-completion congrats to {user_id}: {e}", exception=e)

    # التمييز الدقيق في التتبع بين الكويز المكتمل والتوقف المبكر
    if stopped_early:
        asyncio.create_task(mark_quiz_attempt_stopped(data.get("attempt_id")))
        asyncio.create_task(log_usage_event(user_id, "quiz_stopped", {
            "quiz_id": quiz_id, "score": score, "total": total, "percentage": round(percentage, 1),
        }))
    else:
        asyncio.create_task(complete_quiz_attempt(data.get("attempt_id"), score))
        asyncio.create_task(log_usage_event(user_id, "quiz_completed", {
            "quiz_id": quiz_id, "score": score, "total": total, "percentage": round(percentage, 1),
        }))
    await state.update_data(quiz_completed=True, is_switching_question=False)
    await state.set_state(None)

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
        
        selected_opt = int(poll_answer.option_ids[0])
        if selected_opt == correct_opt:
            current_data = await state.get_data()
            await state.update_data(score=current_data.get('score', 0) + 1)
    except Exception as e:
        log_error(logger, f"Error in handle_poll_answer: {e}", exception=e)


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
        await state.update_data(edit_question_index=question_index)
        await state.set_state(QuizState.waiting_for_question_edit_choice)

        question = questions[question_index]
        quiz_id = data.get("quiz_id")
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
        )
        await call.answer()
    except Exception as e:
        log_error(logger, f"Error selecting answer to edit: {e}", exception=e)
        await call.answer("❌ تعذر اختيار الإجابة للتعديل.", show_alert=True)


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

    asyncio.create_task(log_usage_event(user_id, "quiz_question_edited", {
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


async def fetch_question_for_edit_web(chat_id: int, user_id: int, quiz_id: str, question_index: int) -> Optional[dict]:
    """
    🆕 تُستدعى من webhook_server.py (POST /api/question-edit/fetch) للتحقق من صلاحية
    جلسة تعديل سؤال رياضي عبر صفحة الويب، وإرجاع بيانات السؤال الحالية لتعبئة النموذج.
    الحماية: نبني FSMContext الحقيقي لنفس المستخدم (نفس آلية _build_state_for_chat
    المستخدمة برفع الصوت/الملفات عبر الويب) ونتحقق أن الحالة الحالية بالضبط
    waiting_for_question_edit_choice (الحالة التي تدخلها request_question_edit فور
    الرد بنقطة)، وأن quiz_id/question_index مطابقان تماماً لما خزّنته تلك الدالة -
    لا نثق بأي قيمة قادمة من الصفحة نفسها بمعزل عن جلسة FSM الفعلية.
    """
    state = _build_state_for_chat(chat_id, user_id)
    current_state = await state.get_state()
    if current_state != QuizState.waiting_for_question_edit_choice:
        return None
    data = await state.get_data()
    if data.get("quiz_id") != quiz_id or data.get("edit_question_index") != question_index:
        return None
    questions = data.get("questions", [])
    if not 0 <= question_index < len(questions):
        return None
    question = questions[question_index]
    if not question.get("is_math"):
        return None
    return question


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
    state = _build_state_for_chat(chat_id, user_id)
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
async def replay_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        questions = data.get("questions", [])
        if not questions:
            await call.answer("❌ لا يوجد كويز محفوظ لإعادة تشغيله", show_alert=True)
            return
        
        await state.set_state(QuizState.answering_quiz)
        # 🩹 إعادة التشغيل يجب أن تُسجَّل كمحاولة جديدة تماماً في قاعدة البيانات، وإلا
        # فإن complete_quiz_attempt اللاحقة ستستخدم نفس attempt_id القديم وتكتب فوق
        # بيانات المحاولة الأولى (النتيجة والمدة) بدل تسجيل محاولة مستقلة.
        user_id = call.from_user.id
        new_attempt_id = start_quiz_attempt(user_id, data.get("quiz_id") or None, data.get("quiz_origin", "shared"), len(questions))
        asyncio.create_task(log_usage_event(user_id, "quiz_started", {
            "origin": data.get("quiz_origin", "shared"), "quiz_id": data.get("quiz_id"), "questions": len(questions),
        }))
        await state.update_data(
            current_index=0, score=0, quiz_completed=False, is_switching_question=False,
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
        await state.clear()
        await _send_main_menu(call, call.from_user.id)
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
            asyncio.create_task(log_usage_event(call.from_user.id, "quiz_rated", {"quiz_id": quiz_id, "vote": "like"}))
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
            asyncio.create_task(log_usage_event(call.from_user.id, "quiz_rated", {"quiz_id": quiz_id, "vote": "dislike"}))
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
            asyncio.create_task(log_usage_event(msg.from_user.id, "feedback_submitted", {"quiz_id": quiz_id}))
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
        asyncio.create_task(log_usage_event(call.from_user.id, "quiz_saved_favorite", {"quiz_id": quiz_id, "section": "عام"}))
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
        asyncio.create_task(log_usage_event(call.from_user.id, "quiz_saved_favorite", {"quiz_id": quiz_id, "section_id": section_id}))
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
        asyncio.create_task(log_usage_event(user_id, "quiz_saved_favorite", {"quiz_id": quiz_id, "new_section": section_title}))
        await msg.answer(f"✅ **تم إنشاء القسم وحفظ الاختبار بنجاح!**\n\n📦 الاسم: `{title}`\n🗂 القسم الجديد: `{section_title}`", parse_mode="Markdown")
        await state.set_state(None if data.get("quiz_completed") else QuizState.answering_quiz)
    except Exception as e:
        log_error(logger, f"Error in creating section and saving: {e}", exception=e)
        await state.set_state(QuizState.answering_quiz)

@router.callback_query(F.data == "force_stop_previous_quiz")
async def force_stop_previous_quiz_handler(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        if data.get("attempt_id"):
            asyncio.create_task(mark_quiz_attempt_stopped(data["attempt_id"]))
        await state.clear()
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.message.answer("✅ <b>تم إيقاف الاختبار السابق بنجاح!</b>\n\nيمكنك الآن إرسال محتوى جديد فوراً. 🚀", parse_mode="HTML")
    except Exception as e:
        log_error(logger, f"Error in force_stop_previous_quiz: {e}")
    finally:
        await call.answer()

@router.callback_query(F.data == "delete_warning_msg")
async def delete_warning_msg_handler(call: types.CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    finally:
        await call.answer()

quiz_runner_router = router