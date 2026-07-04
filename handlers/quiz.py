import os
import asyncio
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from pypdf import PdfReader
from config import bot, QuizState, MAX_DOC_SIZE, MAX_PHOTO_SIZE, MAX_PDF_PAGES
from utils import process_file_smart
from gemini_helper import get_questions_from_text, extract_text_from_image
from supabase_helper import check_or_add_user, update_user_stats
from keyboards import get_main_menu_keyboard

# يضمن وجود المجلد بمجرد تشغيل البوت
if not os.path.exists("downloads"):
    os.makedirs("downloads")

router = Router()

@router.message(F.document)
async def handle_document(msg: types.Message, state: FSMContext):
    if msg.document.file_size > MAX_DOC_SIZE:
        await msg.answer("❌ هذا الملف كبير جداً! الحد الأقصى المسموح به هو 20 ميجابايت.")
        return

    if not os.path.exists("downloads"): os.makedirs("downloads")
    path = f"downloads/{msg.document.file_name}"
    file = await bot.get_file(msg.document.file_id)
    await bot.download_file(file.file_path, path)
    
    # تم تعديل المسافات هنا ليكون الـ try داخل الدالة
    try:
        reader = PdfReader(path)
        page_count = len(reader.pages)
        
        # التأكد من أن الملف ليس فارغاً فعلياً
        all_text = ""
        for page in reader.pages:
            all_text += page.extract_text() or ""
            
        if len(all_text.strip()) < 10:
            await msg.answer("⚠️ الملف يبدو كصور ضوئية، سأحاول معالجته بالذكاء الاصطناعي...")

        if page_count > MAX_PDF_PAGES:
            await msg.answer(f"❌ الملف يحتوي على ({page_count}) صفحة! الحد الأقصى {MAX_PDF_PAGES}.")
            if os.path.exists(path): os.remove(path)
            return
            
    except Exception as e:
        print(f"DEBUG: Error reading PDF: {e}")

    await state.update_data(file_path=path, is_photo=False)
    await state.set_state(QuizState.waiting_for_count)
    await msg.answer("✅ تم رفع وقراءة الملف بنجاح!\nكم سؤالاً تريد توليده؟ (أرسل رقماً فقط)")

@router.message(F.photo)
async def handle_photo(msg: types.Message, state: FSMContext):
    photo = msg.photo[-1]
    if photo.file_size > MAX_PHOTO_SIZE:
        await msg.answer("❌ حجم الصورة كبير جداً! الحد الأقصى هو 10 ميجابايت.")
        return

    if not os.path.exists("downloads"): os.makedirs("downloads")
    path = f"downloads/{photo.file_id}.png"
    file = await bot.get_file(photo.file_id)
    await bot.download_file(file.file_path, path)
    
    await state.update_data(file_path=path, is_photo=True)
    await state.set_state(QuizState.waiting_for_count)
    await msg.answer("✅ تم استقبال الصورة بنجاح!\nكم سؤالاً تريد توليده؟ (أرسل رقماً فقط)")

@router.message(QuizState.waiting_for_count, F.text.isdigit())
async def process_count(msg: types.Message, state: FSMContext):
    count = int(msg.text)
    data = await state.get_data()
    path = data.get('file_path')
    is_photo = data.get('is_photo', False)
    
    user_info = await asyncio.to_thread(check_or_add_user, msg.from_user.id, msg.from_user.username or "Unknown")
    current_points = user_info["points"]
    if current_points < count:
        bot_info = await bot.get_me()
        await msg.answer(
            f"❌ رصيدك الحالي ({current_points}) لا يكفي لتوليد {count} أسئلة.\nيمكنك الشحن أو دعوة زملائك للحصول على نقاط إضافية عن طريق الخيارات أدناه 👇",
            reply_markup=get_main_menu_keyboard(bot_info.username, msg.from_user.id)
        )
        if path and os.path.exists(path): os.remove(path)
        await state.clear()
        return
    
    processing_msg = await msg.answer("🤖 جاري قراءة المحتوى ومعالجته بالذكاء الاصطناعي...")
    try:
        full_text = ""
        if is_photo:
            with open(path, "rb") as f: image_bytes = f.read()
            full_text = await asyncio.to_thread(extract_text_from_image, image_bytes)
        else:
            processed_data = await asyncio.to_thread(process_file_smart, path)
            for item in processed_data:
                if item["type"] == "text":
                    full_text += item["content"] + "\n"
                else:
                    img_text = await asyncio.to_thread(extract_text_from_image, item["content"])
                    full_text += img_text + "\n"
                    await asyncio.sleep(2)
        
        if not full_text.strip(): raise ValueError("لم نتمكن من استخراج أي نصوص مقروءة.")

        quiz_data = await asyncio.to_thread(get_questions_from_text, full_text, count)
        if not quiz_data:
            await processing_msg.edit_text("❌ لم يتمكن الذكاء الاصطناعي من استخراج أسئلة مفيدة.")
            await state.clear()
            return

        actual_count = len(quiz_data)
        await asyncio.to_thread(update_user_stats, msg.from_user.id, actual_count)
        
        await state.update_data(questions=quiz_data, current_index=0, score=0, total_count=actual_count)
        await state.set_state(QuizState.answering_quiz)
        await processing_msg.delete()

        corrupted_questions = [f"السؤال رقم {i+1}" for i, q in enumerate(quiz_data) if q.get('was_corrupted_text_fixed')]
        if corrupted_questions:
            warning_text = f"⚠️ **تنبيه قبل بداية الاختبار:**\nنظراً لوجود كلمات غير واضحة بالورقة، أصلح الذكاء الاصطناعي السياق تلقائياً لـ ({', '.join(corrupted_questions)}).\n\nاضغط أدناه للبدء 👇"
            start_kb = [[types.InlineKeyboardButton(text="🚀 ابدأ الاختبار الآن", callback_data="start_first_question")]]
            await msg.answer(warning_text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=start_kb))
        else:
            await send_question(msg, state)
            
    except Exception as e:
        await processing_msg.edit_text(f"❌ عذراً، واجهنا مشكلة أثناء معالجة الملف.")
        print(f"Developer Error: {e}")
        await state.clear()
    finally:
        if path and os.path.exists(path):
            try: os.remove(path)
            except Exception: pass

@router.callback_query(QuizState.answering_quiz, F.data == "start_first_question")
async def start_quiz_after_warning(call: types.CallbackQuery, state: FSMContext):
    await call.message.delete()
    await send_question(call, state)
    await call.answer()

async def send_question(msg_or_call, state: FSMContext):
    data = await state.get_data()
    questions = data['questions']
    idx = data['current_index']
    
    if idx >= len(questions):
        score = data['score']
        total = data['total_count']
        chat_id = msg_or_call.chat.id if isinstance(msg_or_call, types.Message) else msg_or_call.message.chat.id
        bot_info = await bot.get_me()
        await bot.send_message(chat_id, f"🏁 **اكتمل الاختبار بنجاح!**\n\n🎯 نتيجتك النهائية: {score} من {total}", reply_markup=get_main_menu_keyboard(bot_info.username, chat_id))
        await state.clear()
        return
        
    q = questions[idx]
    text = f"📝 **السؤال {idx + 1} من {len(questions)}:**\n\n{q['question']}"
    
    kb = []
    for i, opt in enumerate(q['options']):
        kb.append([types.InlineKeyboardButton(text=opt, callback_data=f"ans_{i}")])
    kb.append([types.InlineKeyboardButton(text="💡 طلب تلميح", callback_data="get_hint")])
    
    if isinstance(msg_or_call, types.Message):
        await msg_or_call.answer(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb))
    else:
        await msg_or_call.message.answer(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(QuizState.answering_quiz, F.data.startswith("ans_"))
async def handle_answer(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    questions = data['questions']
    idx = data['current_index']
    q = questions[idx]
    
    selected_opt = int(call.data.split("_")[1])
    correct_opt = q['correct_option_id']
    
    score = data['score']
    if selected_opt == correct_opt:
        score += 1
        await state.update_data(score=score)
        status_text = "✅ **إجابة صحيحة وممتازة!**"
    else:
        status_text = f"❌ **إجابة خاطئة!**\n💡 الإجابة الصحيحة هي: **{q['options'][correct_opt]}**"
        
    new_kb = []
    for i, opt in enumerate(q['options']):
        prefix = "🟢 " if i == correct_opt else ("🔴 " if i == selected_opt and selected_opt != correct_opt else "")
        new_kb.append([types.InlineKeyboardButton(text=f"{prefix}{opt}", callback_data="ignored")])
    
    new_kb.append([types.InlineKeyboardButton(text="➡️ السؤال التالي", callback_data="next_question")])
    updated_text = f"📝 **السؤال {idx + 1} من {len(questions)}:**\n\n{q['question']}\n\n📊 {status_text}"
    
    await call.message.edit_text(updated_text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=new_kb))
    await call.answer()

@router.callback_query(QuizState.answering_quiz, F.data == "get_hint")
async def handle_hint(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    q = data['questions'][data['current_index']]
    await call.answer(f"💡 تلميح: {q['hint']}", show_alert=True)

@router.callback_query(QuizState.answering_quiz, F.data == "next_question")
async def handle_next(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(current_index=data['current_index'] + 1)
    try: await call.message.delete()
    except Exception: pass
    await send_question(call, state)
    await call.answer()

@router.callback_query(F.data == "ignored")
async def handle_ignored_click(call: types.CallbackQuery):
    await call.answer()
