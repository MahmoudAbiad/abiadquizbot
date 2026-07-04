import os
import asyncio
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from config import bot, QuizState, MAX_DOC_SIZE, MAX_PHOTO_SIZE, MAX_PDF_PAGES
from utils import process_file_smart
from gemini_helper import get_questions_from_text, extract_content
from supabase_helper import check_or_add_user, update_user_stats
from keyboards import get_main_menu_keyboard

router = Router()

@router.message(F.document)
async def handle_document(msg: types.Message, state: FSMContext):
    if not msg.document.file_name.lower().endswith('.pdf'):
        await msg.answer("❌ عذراً، البوت يدعم ملفات PDF فقط.")
        return

    path = f"downloads/{msg.document.file_id}.pdf"
    file = await bot.get_file(msg.document.file_id)
    await bot.download_file(file.file_path, path)
    
    await state.update_data(file_path=path, is_photo=False)
    await state.set_state(QuizState.waiting_for_count)
    await msg.answer("✅ تم رفع الملف! كم سؤالاً تريد؟")

@router.message(F.photo)
async def handle_photo(msg: types.Message, state: FSMContext):
    path = f"downloads/{msg.photo[-1].file_id}.png"
    file = await bot.get_file(msg.photo[-1].file_id)
    await bot.download_file(file.file_path, path)
    
    await state.update_data(file_path=path, is_photo=True)
    await state.set_state(QuizState.waiting_for_count)
    await msg.answer("✅ تم استقبال الصورة! كم سؤالاً تريد؟")

@router.message(QuizState.waiting_for_count, F.text.isdigit())
async def process_count(msg: types.Message, state: FSMContext):
    count = int(msg.text)
    data = await state.get_data()
    path = data.get('file_path')
    
    processing_msg = await msg.answer("🤖 جاري المعالجة...")
    try:
        full_text = ""
        if data.get('is_photo'):
            with open(path, "rb") as f: full_text = await extract_content(f.read(), "image/png")
        else:
            processed_data = await asyncio.to_thread(process_file_smart, path)
            for item in processed_data:
                if item["type"] == "text":
                    full_text += item["content"] + "\n"
                else:
                    full_text += await extract_content(item["content"], "image/png") + "\n"
        
        quiz_data = await get_questions_from_text(full_text, count)
        
        if not quiz_data:
            await processing_msg.edit_text("❌ لم يتمكن الذكاء الاصطناعي من استخراج أسئلة. حاول بملف أوضح.")
            await state.clear()
            return

        await update_user_stats(msg.from_user.id, len(quiz_data))
        await state.update_data(questions=quiz_data, current_index=0, score=0, total_count=len(quiz_data))
        await state.set_state(QuizState.answering_quiz)
        await processing_msg.delete()
        await send_question(msg, state)
            
    except Exception as e:
        await processing_msg.edit_text(f"❌ خطأ: `{e}`")
        await state.clear()
    finally:
        if path and os.path.exists(path): os.remove(path)
