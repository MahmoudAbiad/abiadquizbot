import os
import asyncio
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from config import bot, QuizState, MAX_DOC_SIZE, MAX_PHOTO_SIZE
from gemini_helper import get_questions_from_text, extract_content
from supabase_helper import check_or_add_user, update_user_stats
from keyboards import get_main_menu_keyboard

# يضمن وجود المجلد
if not os.path.exists("downloads"):
    os.makedirs("downloads")

router = Router()

@router.message(F.document)
async def handle_document(msg: types.Message, state: FSMContext):
    if msg.document.file_size > MAX_DOC_SIZE:
        await msg.answer("❌ الملف كبير جداً! الحد الأقصى 20 ميجابايت.")
        return

    path = f"downloads/{msg.document.file_id}_{msg.document.file_name}"
    file = await bot.get_file(msg.document.file_id)
    await bot.download_file(file.file_path, path)
    
    # تحديد نوع الملف لـ Gemini
    mime_type = "application/pdf" if msg.document.mime_type == "application/pdf" else "image/jpeg"
    
    await state.update_data(file_path=path, is_photo=False, mime_type=mime_type)
    await state.set_state(QuizState.waiting_for_count)
    await msg.answer("✅ تم رفع الملف بنجاح!\nكم سؤالاً تريد توليده؟ (أرسل رقماً فقط)")

@router.message(F.photo)
async def handle_photo(msg: types.Message, state: FSMContext):
    photo = msg.photo[-1]
    if photo.file_size > MAX_PHOTO_SIZE:
        await msg.answer("❌ حجم الصورة كبير جداً! الحد الأقصى 10 ميجابايت.")
        return

    path = f"downloads/{photo.file_id}.png"
    file = await bot.get_file(photo.file_id)
    await bot.download_file(file.file_path, path)
    
    await state.update_data(file_path=path, is_photo=True, mime_type="image/jpeg")
    await state.set_state(QuizState.waiting_for_count)
    await msg.answer("✅ تم استقبال الصورة بنجاح!\nكم سؤالاً تريد توليده؟ (أرسل رقماً فقط)")

@router.message(QuizState.waiting_for_count, F.text.isdigit())
async def process_count(msg: types.Message, state: FSMContext):
    count = int(msg.text)
    data = await state.get_data()
    path = data.get('file_path')
    mime_type = data.get('mime_type', 'image/jpeg')
    
    user_info = await asyncio.to_thread(check_or_add_user, msg.from_user.id, msg.from_user.username or "Unknown")
    current_points = user_info["points"]
    
    if current_points < count:
        bot_info = await bot.get_me()
        await msg.answer(
            f"❌ رصيدك الحالي ({current_points}) لا يكفي.\nيمكنك الشحن أو دعوة زملائك.",
            reply_markup=get_main_menu_keyboard(bot_info.username, msg.from_user.id)
        )
        if path and os.path.exists(path): os.remove(path)
        await state.clear()
        return
    
    processing_msg = await msg.answer("🤖 جاري قراءة الملف عبر الذكاء الاصطناعي...")
    
    try:
        # قراءة الملف كـ Bytes
        with open(path, "rb") as f:
            file_bytes = f.read()
            
        # معالجة الملف مباشرة عبر Gemini
        full_text = await asyncio.to_thread(extract_content, file_bytes, mime_type=mime_type)

        if not full_text or "❌" in full_text:
            await processing_msg.edit_text("❌ لم يتمكن الذكاء الاصطناعي من قراءة الملف. حاول مرة أخرى.")
            await state.clear()
            return

        # توليد الأسئلة
        quiz_data = await asyncio.to_thread(get_questions_from_text, full_text, count)
        
        if not quiz_data:
            await processing_msg.edit_text("❌ تعذر استخراج أسئلة من هذا المحتوى.")
            await state.clear()
            return

        actual_count = len(quiz_data)
        await asyncio.to_thread(update_user_stats, msg.from_user.id, actual_count)
        
        await state.update_data(questions=quiz_data, current_index=0, score=0, total_count=actual_count)
        await state.set_state(QuizState.answering_quiz)
        await processing_msg.delete()

        await send_question(msg, state)
            
    except Exception as e:
        await processing_msg.edit_text(f"❌ عذراً، واجهنا خطأ تقني.")
        print(f"Error: {e}")
        await state.clear()
    finally:
        if path and os.path.exists(path):
            try: os.remove(path)
            except: pass

# --- بقية الدوال (handle_answer, handle_hint, إلخ) تظل كما هي في كودك ---
# (لم أغيرها لأنها لا تحتاج لتغيير)
