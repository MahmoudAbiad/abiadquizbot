"""
Leaderboard module - handles the quiz-end publish prompt and displaying top 5 users.
"""
import asyncio
from aiogram import Router, types, F
from supabase_helper import (
    publish_score_to_leaderboard, hide_score_from_leaderboard,
    get_top_5_leaderboard, get_my_leaderboard_status, log_usage_event,
)
from keyboards import get_rating_keyboard, get_leaderboard_keyboard
from logger import get_logger, log_error

logger = get_logger(__name__)
router = Router()


def _split_quiz_id_and_source(data: str, prefix: str) -> tuple[str, str]:
    """يفصل quiz_id عن مصدر الزر (end = شاشة نتيجة الكويز، lb = تحت لوحة الشرف).
    آمن لأنو الـ UUID ما فيه underscore، فآخر جزء بعد _ هو المصدر دايماً."""
    remainder = data.replace(prefix, "", 1)
    quiz_id, _, source = remainder.rpartition("_")
    return quiz_id, source


@router.callback_query(F.data.startswith("publish_score_"))
async def handle_publish_score(call: types.CallbackQuery):
    """
    معالجة زر "نعم" (من شاشة نتيجة الكويز) أو زر "انشر نتيجتي" (من تحت لوحة الشرف).
    """
    try:
        quiz_id, source = _split_quiz_id_and_source(call.data, "publish_score_")
        user_id = call.from_user.id

        success = await publish_score_to_leaderboard(user_id, quiz_id)

        if not success:
            await call.answer("❌ حدث خطأ أثناء نشر النتيجة. يرجى المحاولة لاحقاً.", show_alert=True)
            return

        asyncio.create_task(log_usage_event(user_id, "score_published", {"quiz_id": quiz_id}))
        await call.answer("✅ تم نشر نتيجتك بنجاح في لوحة الشرف!", show_alert=True)

        if source == "lb":
            # الزر كان تحت رسالة لوحة الشرف - نبدّل نصه لـ "إخفاء" بمكانه
            kb = get_leaderboard_keyboard(quiz_id, is_public=True)
            await call.message.edit_reply_markup(reply_markup=kb)
        else:
            # الزر كان بشاشة نتيجة الكويز (سؤال نعم/لا) - نستبدل صف السؤال
            # بزر "عرض لوحة الشرف" العادي بعد ما انحسم القرار.
            kb = get_rating_keyboard(quiz_id, quiz_id=quiz_id, show_publish_prompt=False)
            await call.message.edit_reply_markup(reply_markup=kb)

    except Exception as e:
        log_error(logger, f"Error in handle_publish_score: {e}", exception=e)
        await call.answer("❌ تعذر نشر النتيجة.", show_alert=True)


@router.callback_query(F.data.startswith("decline_score_"))
async def handle_decline_score(call: types.CallbackQuery):
    """
    معالجة زر "لا" من سؤال النشر بشاشة نتيجة الكويز (النتيجة أصلاً خاصة
    افتراضياً، فقط نأكّد الحالة ونحدّث الأزرار).
    """
    try:
        quiz_id, _source = _split_quiz_id_and_source(call.data, "decline_score_")
        user_id = call.from_user.id

        await hide_score_from_leaderboard(user_id, quiz_id)  # idempotent safety-net
        asyncio.create_task(log_usage_event(user_id, "score_publish_declined", {"quiz_id": quiz_id}))
        await call.answer("🔒 خلّينا نتيجتك خاصة.", show_alert=False)

        kb = get_rating_keyboard(quiz_id, quiz_id=quiz_id, show_publish_prompt=False)
        await call.message.edit_reply_markup(reply_markup=kb)

    except Exception as e:
        log_error(logger, f"Error in handle_decline_score: {e}", exception=e)
        await call.answer("❌ حدث خطأ.", show_alert=True)


@router.callback_query(F.data.startswith("hide_score_"))
async def handle_hide_score(call: types.CallbackQuery):
    """
    معالجة زر "إخفاء نتيجتي" تحت لوحة الشرف.
    """
    try:
        quiz_id, _source = _split_quiz_id_and_source(call.data, "hide_score_")
        user_id = call.from_user.id

        success = await hide_score_from_leaderboard(user_id, quiz_id)

        if success:
            asyncio.create_task(log_usage_event(user_id, "score_hidden", {"quiz_id": quiz_id}))
            await call.answer("🙈 تم إخفاء نتيجتك من لوحة الشرف.", show_alert=True)
            kb = get_leaderboard_keyboard(quiz_id, is_public=False)
            await call.message.edit_reply_markup(reply_markup=kb)
        else:
            await call.answer("❌ حدث خطأ أثناء إخفاء النتيجة. يرجى المحاولة لاحقاً.", show_alert=True)

    except Exception as e:
        log_error(logger, f"Error in handle_hide_score: {e}", exception=e)
        await call.answer("❌ تعذر إخفاء النتيجة.", show_alert=True)


@router.callback_query(F.data.startswith("leaderboard_"))
async def handle_show_leaderboard(call: types.CallbackQuery):
    """
    معالجة زر عرض لوحة الشرف (Top 5) - وتحتها زر إدارة رؤية نتيجة الطالب نفسه
    (إخفاء/إظهار) إذا كان أخد هالكويز أصلاً.
    """
    try:
        quiz_id = call.data.replace("leaderboard_", "")

        top_scores = await get_top_5_leaderboard(quiz_id)

        if not top_scores:
            await call.answer("🏆 لا توجد نتائج علنية مسجلة لهذا الكويز حتى الآن.", show_alert=True)
            return

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

        asyncio.create_task(log_usage_event(call.from_user.id, "leaderboard_viewed", {"quiz_id": quiz_id}))

        # 🆕 زر إدارة الرؤية صار هون تحت لوحة الشرف نفسها (مش بشاشة نتيجة الكويز) -
        # ما بيظهر إلا إذا الطالب أخد هالكويز فعلياً وعنده قرار مسجّل.
        my_status = await get_my_leaderboard_status(call.from_user.id, quiz_id)
        kb = get_leaderboard_keyboard(quiz_id, is_public=my_status)

        await call.message.answer(text, reply_markup=kb, parse_mode="Markdown")
        await call.answer()

    except Exception as e:
        log_error(logger, f"Error in handle_show_leaderboard: {e}", exception=e)
        await call.answer("❌ تعذر تحميل لوحة الشرف.", show_alert=True)

# تصدير الـ Router لربطه بالملف الرئيسي
leaderboard_router = router
