"""
Quiz execution module - handles answering questions, hints, and results.
Resilient to Telegram Poll string limitations.
"""
import asyncio
from typing import Union

# استيراد دالات الأقسام من قاعدة البيانات
from supabase_helper import (
    list_favorite_quizzes, 
    update_user_stats, 
    save_favorite_quiz,
    list_favorite_sections,
    create_favorite_section
)
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from config import bot, QuizState
from constants import MSG_QUIZ_STOPPED
from keyboards import (
    get_main_menu_keyboard, get_quiz_result_keyboard, 
)
from logger import get_logger, log_error, log_info, log_warning
import json # للتعامل مع البيانات قبل حفظها في Redis
from config import redis_client # لاستخدام المخزن الدائم

logger = get_logger(__name__)

router = Router()

async def _send_main_menu(call_or_message: Union[types.Message, types.CallbackQuery], user_id: int) -> None:
    bot_info = await bot.get_me()
    menu = get_main_menu_keyboard(bot_info.username, user_id)
    text = "🏠 القائمة الرئيسية"
    if isinstance(call_or_message, types.CallbackQuery):
        await call_or_message.message.answer(text, reply_markup=menu)
    else:
        await call_or_message.answer(text, reply_markup=menu)

async def _start_loaded_quiz(msg_or_call: Union[types.Message, types.CallbackQuery], state: FSMContext, quiz_data: list, source_title: str, origin: str = "shared", quiz_id: str = "") -> None:
    await state.update_data(
        questions=quiz_data, current_index=0, score=0,
        total_count=len(quiz_data), source_title=source_title,
        quiz_origin=origin, quiz_completed=False, quiz_id=quiz_id,
        is_saved_in_session=False
    )
    await state.set_state(QuizState.answering_quiz)
    if isinstance(msg_or_call, types.CallbackQuery):
        try:
            await msg_or_call.message.delete()
        except Exception:
            pass
    await send_question(msg_or_call, state)

async def send_question(msg_or_call: Union[types.Message, types.CallbackQuery], state: FSMContext) -> None:
    idx = 0
    try:
        data = await state.get_data()
        questions = data['questions']
        idx = data['current_index']
        
        # 1. التحقق من انتهاء الاختبار
        if idx >= len(questions):
            score = data['score']
            total = data['total_count']
            chat_id = msg_or_call.chat.id if isinstance(msg_or_call, types.Message) else msg_or_call.message.chat.id
            
            percentage = (score / total * 100) if total > 0 else 0
            result_text = (
                f"🏁 **اكتمل الاختبار بنجاح!**\n\n"
                f"🎯 نتيجتك النهائية: **{score}** من **{total}**\n"
                f"📊 النسبة المئوية: **{percentage:.1f}%**\n\n"
                f"{'🏆 ممتاز!' if percentage >= 80 else '👍 جيد!' if percentage >= 60 else '📚 استمر في الممارسة!'}"
            )
            
            await bot.send_message(chat_id, result_text, reply_markup=get_quiz_result_keyboard(), parse_mode="Markdown")
            log_info(logger, f"Quiz completed for user {chat_id}: {score}/{total}")
            await state.update_data(quiz_completed=True)
            await state.set_state(None)
            return
        
        # 2. جلب بيانات السؤال الحالي وتأمين الهويات
        q = questions[idx]
        chat_id = msg_or_call.chat.id if isinstance(msg_or_call, types.Message) else msg_or_call.message.chat.id
        
        # 🔐 جلب معرف الطالب الحقيقي دائماً من مفتاح الـ FSM المتصل بالعملية
        user_id = state.key.user_id

        # ⚙️ معالجة واقتطاع النصوص للامتثال لقيود تليجرام الصارمة لضمان الاستقرار الإرسالي
        raw_question = f"📝 السؤال {idx + 1} من {len(questions)}:\n{q['question']}"
        clean_question = raw_question if len(raw_question) <= 300 else raw_question[:297] + "..."
        
        clean_options = []
        for opt in q['options']:
            opt_str = str(opt).strip()
            clean_options.append(opt_str if len(opt_str) <= 100 else opt_str[:97] + "...")
            
        raw_explanation = q.get("explanation") or "إجابة صحيحة!"
        clean_explanation = raw_explanation if len(raw_explanation) <= 200 else raw_explanation[:197] + "..."

        # 3. إنشاء أزرار التحكم السفلية
        control_kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="💡 طلب تلميح", callback_data="get_hint")],
            [
                types.InlineKeyboardButton(text="⏹ إيقاف", callback_data="quiz_stop"),
                types.InlineKeyboardButton(text="🔗 مشاركة", callback_data="quiz_share"),
                types.InlineKeyboardButton(text="💾 حفظ", callback_data="save_quiz")
            ],
            [types.InlineKeyboardButton(text="التالي ➡️", callback_data="next_question")]
        ])

# 1. إرسال السؤال كـ Poll رسمي بالبيانات النظيفة
        poll_msg = await bot.send_poll(
            chat_id=chat_id,
            question=clean_question,
            options=clean_options,
            type="quiz",
            correct_option_id=int(q['correct_option_id']),
            explanation=clean_explanation,
            reply_markup=control_kb
        )

        # 2. الآن نستخرج الـ poll_id الحقيقي من رسالة الـ Poll التي أرسلناها للتو
        poll_id = poll_msg.poll.id

        # 3. نجهز البيانات التي نريد حفظها
        quiz_data = {
            "chat_id": chat_id,
            "user_id": user_id,
            "correct_option_id": int(q['correct_option_id']) # تأكد أنك تستخدم القيمة الصحيحة من q
        }

        # 4. نحفظ البيانات في Redis باستخدام الـ poll_id الصحيح
        await redis_client.set(f"poll:{poll_id}", json.dumps(quiz_data), ex=7200)
    except Exception as e:
        log_error(logger, f"Error in send_question: {e}", exception=e)
        chat_id = msg_or_call.chat.id if isinstance(msg_or_call, types.Message) else msg_or_call.message.chat.id
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ **نعتذر منك، واجه النظام مشكلة تقنية في عرض السؤال رقم ({idx + 1}) كـ Poll.**\n\n"
                     f"📌 السبب الغالب يعود لطول صياغة السؤال أو الخيارات القادمة من الموديل.\n"
                     f"⏩ يمكنك تخطي هذا السؤال مباشرة لعدم عرقلة اختبارك.",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="تخطي هذا السؤال والانتقال للتالي ➡️", callback_data="next_question")]
                ])
            )
        except Exception:
            pass

@router.callback_query(QuizState.answering_quiz, F.data == "start_first_question")
async def start_quiz_after_warning(call: types.CallbackQuery, state: FSMContext):
    try:
        await call.message.delete()
        await send_question(call, state)
    except Exception as e:
        log_error(logger, f"Error in start_quiz_after_warning: {e}", exception=e)
    finally:
        await call.answer()

@router.callback_query(F.data == "quiz_stop")
async def stop_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        await state.clear() 
        try: await call.message.delete()
        except Exception: pass
        await _send_main_menu(call, call.from_user.id)
        await call.message.answer(MSG_QUIZ_STOPPED)
    except Exception as e:
        log_error(logger, f"Error in stop_quiz: {e}", exception=e)
        await call.answer("❌ تعذر إيقاف الكويز", show_alert=True)
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
        await state.update_data(current_index=0, score=0, quiz_completed=False)
        try: await call.message.delete()
        except Exception: pass
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

@router.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer, state: FSMContext):
    try:
        poll_id = poll_answer.poll_id
        
        # 1. محاولة استرجاع بيانات السؤال من Redis
        data_json = await redis_client.get(f"poll:{poll_id}")
        
        if not data_json:
            # إذا لم نجد البيانات، فهذا يعني أن الكويز قديم أو تم مسحه من Redis
            log_warning(logger, f"⚠️ لا توجد بيانات للكويز {poll_id} في الذاكرة (ربما انتهت صلاحيته).")
            return

        # 2. تحويل البيانات من نص (JSON) إلى قاموس (Dictionary)
        quiz_info = json.loads(data_json)
        
        # استخراج البيانات المخزنة
        correct_opt = quiz_info["correct_option_id"]
        user_id = quiz_info["user_id"]
        # chat_id = quiz_info["chat_id"] # يمكنك استخدامه إذا أردت إرسال رسائل خاصة بعد الإجابة

        # 3. التحقق من الإجابة
        # تأكد أن المستخدم اختار إجابة (option_ids تحتوي على رقم الخيار)
        if not poll_answer.option_ids:
            return
            
        selected_opt = poll_answer.option_ids[0]

        # 4. معالجة النتيجة وتحديث الـ State
        if selected_opt == correct_opt:
            # جلب النتيجة الحالية من الـ State (المخزنة أيضاً في Redis)
            current_data = await state.get_data()
            current_score = current_data.get('score', 0)
            
            # تحديث النتيجة
            await state.update_data(score=current_score + 1)
            log_info(logger, f"✅ إجابة صحيحة للمستخدم {user_id}. النتيجة الجديدة: {current_score + 1}")
        else:
            log_info(logger, f"❌ إجابة خاطئة للمستخدم {user_id}.")

    except Exception as e:
        log_error(logger, f"❌ خطأ فادح في معالجة الإجابة (handle_poll_answer): {e}", exception=e)
        
@router.callback_query(QuizState.answering_quiz, F.data == "get_hint")
async def handle_hint(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        q = data['questions'][data['current_index']]
        await call.answer(f"💡 تلميح: {q['hint']}", show_alert=False)
    except Exception as e:
        log_error(logger, f"Error in handle_hint: {e}", exception=e)
        await call.answer("❌ خطأ في جلب التلميح", show_alert=True)


# ==================== معالج حفظ الكويز التفاعلي الذكي ====================

@router.callback_query(F.data.in_({"save_quiz", "quiz_favorite"}))
async def handle_save_quiz_start(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        if data.get("is_saved_in_session"):
            await call.answer("✅ هذا الكويز محفوظ بالفعل في قائمتك المفضلة!", show_alert=True)
            return

        questions = data.get("questions")
        quiz_id = data.get("quiz_id")
        
        if not questions:
            await call.answer("❌ لا يوجد كويز لحفظه!", show_alert=True)
            return

        user_favorites = await asyncio.to_thread(list_favorite_quizzes, call.from_user.id)
        is_already_saved = False
        if user_favorites and quiz_id and str(quiz_id).strip():
            is_already_saved = any(fav.get("quiz_id") == quiz_id for fav in user_favorites)

        if is_already_saved:
            await state.update_data(is_saved_in_session=True)
            await call.answer("💡 هذا الكويز موجود بالفعل في قائمتك المفضلة مسبقاً!", show_alert=True)
            return

        current_status = await state.get_state()
        await state.update_data(prev_quiz_state=current_status)

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


@router.callback_query(F.data == "save_name_current")
async def save_name_current_handler(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        title = data.get("source_title") or data.get("title") or "كويز بدون عنوان"
        await state.update_data(final_save_title=title)
        
        await _prompt_section_selection(call.message, state)
        try: await call.message.delete()
        except Exception: pass
    except Exception as e:
        log_error(logger, f"Error in save_name_current: {e}", exception=e)
    finally:
        await call.answer()


@router.callback_query(F.data == "save_name_custom")
async def save_name_custom_handler(call: types.CallbackQuery, state: FSMContext):
    try:
        await state.set_state(QuizState.waiting_for_custom_name)
        await call.message.edit_text("✏️ **أرسل الآن الاسم المخصص** الذي تريده لهذا الاختبار في رسالة نصية:")
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


@router.callback_query(F.data == "save_sec_general")
async def handle_save_general(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        title = data.get("final_save_title") or "كويز بدون عنوان"
        questions = data.get("questions")
        quiz_id = data.get("quiz_id")
        
        await asyncio.to_thread(save_favorite_quiz, call.from_user.id, title, questions, None, None, quiz_id)
        await state.update_data(is_saved_in_session=True)
        
        await call.message.edit_text(f"✅ **تم الحفظ بنجاح!**\n\n📦 الاسم: `{title}`\n🗂 القسم: `عام`", parse_mode="Markdown")
        
        # 🛠 [إصلاح ذكي]: التحقق لمنع تعليق حالة المستخدم بعد الحفظ النهائي
        if data.get("quiz_completed"):
            await state.set_state(None)
        else:
            await state.set_state(QuizState.answering_quiz)
            
    except Exception as e:
        log_error(logger, f"Error saving to general: {e}", exception=e)
        await call.answer("❌ حدث خطأ أثناء حفظ الكويز.", show_alert=True)
    finally:
        await call.answer()


@router.callback_query(F.data == "save_sec_choose")
async def handle_save_choose_section(call: types.CallbackQuery, state: FSMContext):
    try:
        user_id = call.from_user.id
        sections = await asyncio.to_thread(list_favorite_sections, user_id)
        
        inline_keyboard = []
        if sections:
            for sec in sections:
                inline_keyboard.append([
                    types.InlineKeyboardButton(text=f"📁 {sec['title']}", callback_data=f"save_to_sec_{sec['section_id']}")
                ])
        
        inline_keyboard.append([types.InlineKeyboardButton(text="➕ إنشاء قسم جديد واختياره", callback_data="save_sec_create_new")])
        inline_keyboard.append([types.InlineKeyboardButton(text="🌐 إلغاء وحفظ في عام", callback_data="save_sec_general")])
        
        kb = types.InlineKeyboardMarkup(inline_keyboard=inline_keyboard)
        await call.message.edit_text("📂 **اختر أحد أقسامك المفضلة الحالية لتصنيف الاختبار داخله:**", reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        log_error(logger, f"Error showing sections: {e}", exception=e)
        await call.answer("❌ تعذر جلب قائمة الأقسام حالياً.", show_alert=True)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("save_to_sec_"))
async def handle_save_to_existing_section(call: types.CallbackQuery, state: FSMContext):
    try:
        section_id = call.data.replace("save_to_sec_", "")
        data = await state.get_data()
        title = data.get("final_save_title") or "كويز بدون عنوان"
        questions = data.get("questions")
        quiz_id = data.get("quiz_id")
        
        await asyncio.to_thread(save_favorite_quiz, call.from_user.id, title, questions, section_id, None, quiz_id)
        await state.update_data(is_saved_in_session=True)
        
        await call.message.edit_text(f"✅ **تم حفظ الاختبار بنجاح ضمن القسم المختار!**\n\n📦 الاسم: `{title}`", parse_mode="Markdown")
        
        # 🛠 [إصلاح ذكي]: التحقق لمنع تعليق حالة المستخدم بعد الحفظ النهائي
# الكود المصحح الجديد ✨:
        if data.get("quiz_completed"):
            await state.set_state(None)
        else:
            await state.set_state(QuizState.answering_quiz)
            
    except Exception as e:
        log_error(logger, f"Error saving to existing section: {e}", exception=e)
    finally:
        await call.answer()


@router.callback_query(F.data == "save_sec_create_new")
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
        title = data.get("final_save_title") or "كويز بدون عنوان"
        questions = data.get("questions")
        quiz_id = data.get("quiz_id")
        
        new_section_id = await asyncio.to_thread(create_favorite_section, user_id, section_title)
        if not new_section_id:
            await msg.answer("⚠️ تعذر إنشاء القسم، تم تحويل مسار الحفظ تلقائياً إلى 'عام'.")
            new_section_id = None
            
        await asyncio.to_thread(save_favorite_quiz, user_id, title, questions, new_section_id, None, quiz_id)
        await state.update_data(is_saved_in_session=True)
        
        await msg.answer(f"✅ **تم إنشاء القسم وحفظ الاختبار بنجاح!**\n\n📦 الاسم: `{title}`\n🗂 القسم الجديد: `{section_title}`", parse_mode="Markdown")
        
        # 🛠 [إصلاح ذكي]: التحقق لمنع تعليق حالة المستخدم بعد الحفظ النهائي
# الكود المصحح الجديد ✨:
        if data.get("quiz_completed"):
            await state.set_state(None)
        else:
            await state.set_state(QuizState.answering_quiz)
            
    except Exception as e:
        log_error(logger, f"Error in creating section and saving: {e}", exception=e)


@router.callback_query(QuizState.answering_quiz, F.data == "next_question")
async def handle_next(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        await state.update_data(current_index=data['current_index'] + 1)
        try: await call.message.delete()
        except Exception: pass
        await send_question(call, state)
    except Exception as e:
        log_error(logger, f"Error in handle_next: {e}", exception=e)
    finally:
        await call.answer()

@router.callback_query(F.data == "ignored")
async def handle_ignored_click(call: types.CallbackQuery):
    await call.answer("✅ تم تسجيل إجابتك")

@router.callback_query(F.data == "quiz_share")
async def handle_inline_quiz_share(call: types.CallbackQuery, state: FSMContext):
    try:
        bot_info = await bot.get_me()
        data = await state.get_data()
        
        quiz_id = data.get("quiz_id", "")
        title = data.get("source_title", "اختبار مميز")
        
        if quiz_id:
            start_param = f"quiz_{quiz_id}"
            share_text = f"🎯 جرب حل كويز «{title}» وتحدى نفسك معي في الدراسة!"
        else:
            start_param = f"start_{call.from_user.id}"
            share_text = "🔥 جرب حل هذا الكويز الرهيب وتحدى نفسك معي في الدراسة!"
            
        share_url = f"https://t.me/share/url?url=https://t.me/{bot_info.username}?start={start_param}&text={share_text}"
        share_kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🚀 شارك هذا الكويز الآن", url=share_url)]
        ])
        
        await call.message.answer(
            f"🔗 يمكنك مشاركة كويز «{title}» مع أصدقائك عبر الضغط على الزر أدناه:",
            reply_markup=share_kb
        )
    except Exception as e:
        log_error(logger, f"Error in handle_inline_quiz_share: {e}", exception=e)
    finally:
        await call.answer()

execution_router = router