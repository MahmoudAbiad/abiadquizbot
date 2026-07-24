import asyncio
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext

from config import bot
from supabase_helper import create_shared_quiz_id, save_shared_quiz, get_shared_quiz, log_usage_event
from logger import get_logger, log_error

# استيراد الوظيفة المشتركة لتشغيل الكويز الجاهز
from handlers.quiz_runner import _start_loaded_quiz

logger = get_logger(__name__)
router = Router()

def _build_source_title(state_data: dict, fallback: str = "كويز") -> str:
    title = state_data.get("source_title") or fallback
    return title[:80]

@router.callback_query(F.data == "quiz_share")
async def share_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        questions = data.get("questions", [])
        if not questions:
            await call.answer("❌ لا يوجد كويز لمشاركته", show_alert=True)
            return

        share_id = data.get("share_id") or create_shared_quiz_id()
        title = _build_source_title(data)
        session_quiz_id = data.get("quiz_id")

        # حفظ الرابط بالجدول المركزي لمنع تكرار الـ JSONB وهدر المساحة
        saved = await save_shared_quiz(share_id, call.from_user.id, title, questions, quiz_id=session_quiz_id)
        if not saved:
            await call.answer("❌ تعذر حفظ رابط المشاركة حالياً", show_alert=True)
            return

        await state.update_data(share_id=share_id)
        bot_info = await bot.get_me()
        
        share_link = f"https://t.me/{bot_info.username}?start=share_{share_id}"
        old_style_text = (
            "تم إنشاء رابط مشاركة الكويز\n\n"
            f"{share_link}\n"
            "يمكنك مشاركته"
        )
        
        asyncio.create_task(log_usage_event(call.from_user.id, "share_link_created", {"share_id": share_id}))
        await call.message.answer(old_style_text, disable_web_page_preview=True)
        
    except Exception as e:
        log_error(logger, f"Error in share_quiz: {e}", exception=e)
        await call.answer("❌ حدث خطأ أثناء إنشاء رابط المشاركة", show_alert=True)
    finally:
        await call.answer()

@router.callback_query(F.data.startswith("share_load_"))
async def open_shared_quiz(call: types.CallbackQuery, state: FSMContext):
    try:
        share_id = call.data.replace("share_load_", "", 1)
        
        shared = await get_shared_quiz(share_id)
        if not shared:
            await call.answer("❌ انتهى رابط المشاركة أو غير موجود", show_alert=True)
            return
            
        asyncio.create_task(log_usage_event(call.from_user.id, "shared_link_opened", {"share_id": share_id}))

        # تمرير الـ UUID المركزي الحقيقي (shared["id"]) لضمان عمل القيود وجدول النتائج بسلاسة
        quiz_title = shared.get("source_title") or "كويز مشترك"
        await _start_loaded_quiz(
            call, state, shared["quiz_data"], 
            quiz_title, 
            origin="shared", 
            quiz_id=str(shared["id"])
        )
    except Exception as e:
        log_error(logger, f"Error in open_shared_quiz: {e}", exception=e)
        await call.answer("❌ تعذر فتح الكويز المشترك", show_alert=True)
    finally:
        await call.answer()