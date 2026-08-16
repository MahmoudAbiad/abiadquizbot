"""
Leaderboard module - handles displaying the top 5 users and toggling the
current student's own visibility (publish/hide) directly under the board.
"""
import asyncio
from aiogram import Router, types, F
from supabase_helper import (
    publish_score_to_leaderboard, hide_score_from_leaderboard,
    get_top_5_leaderboard, get_my_leaderboard_status, log_usage_event,
)
from keyboards import get_leaderboard_keyboard
from logger import get_logger, log_error

logger = get_logger(__name__)
router = Router()


async def _build_leaderboard_view(quiz_id: str, user_id: int) -> tuple[str, types.InlineKeyboardMarkup]:
    """يبني نص لوحة الشرف (أعلى 5) + كيبورد إدارة الرؤية تحتها، حسب حالة
    الطالب الحالية. يُستخدم عند أول عرض وبعد كل تبديل نشر/إخفاء عشان
    القائمة تنعكس فوراً بالتغيير (مثلاً لما حدا يخفي نتيجته وينزل عن Top 5)."""
    top_scores = await get_top_5_leaderboard(quiz_id)

    if not top_scores:
        text = "🏆 **لوحة الشرف (أعلى 5 نتائج)** 🏆\n\nلا توجد نتائج علنية مسجلة لهذا الكويز حتى الآن."
    else:
        medals = ["🥇", "🥈", "🥉", "🏅", "🏅"]
        text = "🏆 **لوحة الشرف (أعلى 5 نتائج)** 🏆\n\n"

        for index, score_data in enumerate(top_scores):
            user_info = score_data.get("users", {})
            first_name = user_info.get("first_name", "طالب") if user_info else "طالب"
            last_name = user_info.get("last_name", "") if user_info else ""

            if not last_name or last_name.lower() == "unknown":
                last_name = ""

            full_name = f"{first_name} {last_name}".strip()
            score = score_data.get("highest_score", 0)
            total = score_data.get("total_questions", 0)

            text += f"{medals[index]} **{full_name}**: {score} / {total}\n"

        text += "\n✨ *شد حيلك وادخل القائمة!*"

    my_status = await get_my_leaderboard_status(user_id, quiz_id)
    kb = get_leaderboard_keyboard(quiz_id, is_public=my_status)
    return text, kb


@router.callback_query(F.data.startswith("publish_score_"))
async def handle_publish_score(call: types.CallbackQuery):
    """معالجة زر "📢 انشر نتيجتي في لوحة الشرف" تحت لوحة الشرف نفسها."""
    try:
        quiz_id = call.data.replace("publish_score_", "", 1).rsplit("_", 1)[0]
        user_id = call.from_user.id

        success = await publish_score_to_leaderboard(user_id, quiz_id)

        if not success:
            await call.answer("❌ حدث خطأ أثناء نشر النتيجة. يرجى المحاولة لاحقاً.", show_alert=True)
            return

        asyncio.create_task(log_usage_event(user_id, "score_published", {"quiz_id": quiz_id}))
        await call.answer("✅ تم نشر نتيجتك بنجاح في لوحة الشرف!")

        text, kb = await _build_leaderboard_view(quiz_id, user_id)
        await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

    except Exception as e:
        log_error(logger, f"Error in handle_publish_score: {e}", exception=e)
        await call.answer("❌ تعذر نشر النتيجة.", show_alert=True)


@router.callback_query(F.data.startswith("hide_score_"))
async def handle_hide_score(call: types.CallbackQuery):
    """معالجة زر "🙈 إخفاء نتيجتي من لوحة الشرف" تحت لوحة الشرف نفسها."""
    try:
        quiz_id = call.data.replace("hide_score_", "", 1).rsplit("_", 1)[0]
        user_id = call.from_user.id

        success = await hide_score_from_leaderboard(user_id, quiz_id)

        if not success:
            await call.answer("❌ حدث خطأ أثناء إخفاء النتيجة. يرجى المحاولة لاحقاً.", show_alert=True)
            return

        asyncio.create_task(log_usage_event(user_id, "score_hidden", {"quiz_id": quiz_id}))
        await call.answer("🙈 تم إخفاء نتيجتك من لوحة الشرف.")

        text, kb = await _build_leaderboard_view(quiz_id, user_id)
        await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

    except Exception as e:
        log_error(logger, f"Error in handle_hide_score: {e}", exception=e)
        await call.answer("❌ تعذر إخفاء النتيجة.", show_alert=True)


@router.callback_query(F.data.startswith("leaderboard_"))
async def handle_show_leaderboard(call: types.CallbackQuery):
    """معالجة زر "🏆 عرض لوحة الشرف" - وتحتها زر إدارة رؤية نتيجة الطالب نفسه
    (إخفاء/إظهار) إذا كان أخد هالكويز أصلاً."""
    try:
        quiz_id = call.data.replace("leaderboard_", "", 1)

        text, kb = await _build_leaderboard_view(quiz_id, call.from_user.id)

        asyncio.create_task(log_usage_event(call.from_user.id, "leaderboard_viewed", {"quiz_id": quiz_id}))

        await call.message.answer(text, reply_markup=kb, parse_mode="Markdown")
        await call.answer()

    except Exception as e:
        log_error(logger, f"Error in handle_show_leaderboard: {e}", exception=e)
        await call.answer("❌ تعذر تحميل لوحة الشرف.", show_alert=True)

# تصدير الـ Router لربطه بالملف الرئيسي
leaderboard_router = router
