"""
Leaderboard module - handles publishing scores and displaying top 5 users.
"""
import asyncio
from aiogram import Router, types, F
from supabase_helper import publish_score_to_leaderboard, get_top_5_leaderboard
from keyboards import get_quiz_result_keyboard
from logger import get_logger, log_error

logger = get_logger(__name__)
router = Router()

@router.callback_query(F.data.startswith("publish_score_"))
async def handle_publish_score(call: types.CallbackQuery):
    """
    معالجة زر نشر النتيجة في لوحة الشرف.
    """
    try:
        quiz_id = call.data.replace("publish_score_", "")
        user_id = call.from_user.id
        
        # تحديث حالة النتيجة في قاعدة البيانات
        success = await asyncio.to_thread(publish_score_to_leaderboard, user_id, quiz_id)
        
        if success:
            await call.answer("✅ تم نشر نتيجتك بنجاح في لوحة الشرف!", show_alert=True)
            # تحديث الأزرار لإزالة زر "مشاركة النتيجة" بعد نجاح العملية
            kb = get_quiz_result_keyboard(quiz_id=quiz_id, is_score_public=True)
            await call.message.edit_reply_markup(reply_markup=kb)
        else:
            await call.answer("❌ حدث خطأ أثناء نشر النتيجة. يرجى المحاولة لاحقاً.", show_alert=True)
            
    except Exception as e:
        log_error(logger, f"Error in handle_publish_score: {e}", exception=e)
        await call.answer("❌ تعذر نشر النتيجة.", show_alert=True)


@router.callback_query(F.data.startswith("leaderboard_"))
async def handle_show_leaderboard(call: types.CallbackQuery):
    """
    معالجة زر عرض لوحة الشرف (Top 5).
    """
    try:
        quiz_id = call.data.replace("leaderboard_", "")
        
        # جلب البيانات من الداتا بيز
        top_scores = await asyncio.to_thread(get_top_5_leaderboard, quiz_id)
        
        if not top_scores:
            await call.answer("🏆 لا توجد نتائج علنية مسجلة لهذا الكويز حتى الآن. كن أول من ينشر نتيجته!", show_alert=True)
            return
        
        medals = ["🥇", "🥈", "🥉", "🏅", "🏅"]
        text = "🏆 **لوحة الشرف (أعلى 5 نتائج)** 🏆\n\n"
        
        for index, score_data in enumerate(top_scores):
            # استخراج بيانات المستخدم بشكل آمن (معالجة الربط بين الجداول)
            user_info = score_data.get("users", {})
            first_name = user_info.get("first_name", "طالب") if user_info else "طالب"
            last_name = user_info.get("last_name", "") if user_info else ""
            
            # إخفاء كلمة Unknown إذا كانت مسجلة كاسم أخير
            if not last_name or last_name.lower() == "unknown":
                last_name = ""
                
            full_name = f"{first_name} {last_name}".strip()
            score = score_data.get("highest_score", 0)
            total = score_data.get("total_questions", 0)
            
            text += f"{medals[index]} **{full_name}**: {score} / {total}\n"
            
        text += "\n✨ *شد حيلك وادخل القائمة!*"
        
        # إرسال قائمة الشرف كرسالة جديدة 
        await call.message.answer(text, parse_mode="Markdown")
        await call.answer()
        
    except Exception as e:
        log_error(logger, f"Error in handle_show_leaderboard: {e}", exception=e)
        await call.answer("❌ تعذر تحميل لوحة الشرف.", show_alert=True)

# تصدير الـ Router لربطه بالملف الرئيسي
leaderboard_router = router