# handlers/quiz_delete.py
"""
==============================================================================
🆕 معالج مشترك واحد لزر "🗑 حذف الكويز نهائياً" الظاهر بأي شاشة يعرض كويزاً
==============================================================================
هذا الراوتر عام (غير محصور بحالة FSM معينة ولا بفلتر أدمن على مستوى الراوتر
نفسه لأن مالك الكويز - وليس فقط الأدمن - مسموح له بالحذف أيضاً)، ويُستدعى من
أي مكان أضاف زر get_delete_quiz_button_row (keyboards.py):
    - قائمة الكويزات المخزّنة/الكاش لكل ملف (handlers/files.py)
    - شاشة نتيجة الاختبار (handlers/quiz_runner.py)
    - تفاصيل كويز محفوظ بالمفضلة (handlers/favorites.py)

الحذف الفعلي عبر admin_delete_quiz (helpers/supabase_helper.py) يمسح صف
الكويز بالكامل من الجدول المركزي "quizzes" - وهو نفسه مصدر الكاش/القائمة
المعروضة بكل الشاشات أعلاه. بفضل ON DELETE CASCADE بقاعدة البيانات، يُحذف معه
تلقائياً كل ما يرتبط به: تصويتات اللايك/الديسلايك، محاولات الكويز، النقاط
ولوحة المتصدرين، عناصر مفضلة كل الطلاب، والملاحظات/الشكاوى المرتبطة به.
🆕 كما ينظّف admin_delete_quiz يدوياً تصويتات/تثبيت التحقق المجتمعي من التصنيف
(classification_votes/classification_locks) لنفس file_hash هذا الكويز - غير
مرتبطين بـ quiz_id عبر FK فلا ينحذفون تلقائياً مع الكويز (يُحذفون فقط لو لم
يبقَ أي كويز آخر بنفس الـ file_hash، حفاظاً على تصنيف يخدم كويزات أخرى فعلية).

🩹 أمان: الصلاحية تُفحص من جديد هنا دائماً (can_delete_quiz) بجلب creator_id
الحقيقي والحالي من قاعدة البيانات - بغض النظر عن ظهور الزر أصلاً من عدمه،
لأن أي مستخدم يقدر تقنياً يرسل أي callback_data حتى لو لم يظهر له الزر.
"""
from aiogram import Router, types, F

from logger import get_logger, log_error
from supabase_helper import admin_get_quiz_by_id, admin_delete_quiz
from services.quiz_permissions import can_delete_quiz

logger = get_logger(__name__)
router = Router()


@router.callback_query(F.data.startswith("qdel_") & ~F.data.startswith("qdelok_"))
async def handle_quiz_delete_confirm(call: types.CallbackQuery) -> None:
    """يعرض رسالة تأكيد قبل الحذف الفعلي - بعد التحقق من الصلاحية أولاً."""
    try:
        if call.data == "qdel_cancel":
            try:
                await call.message.delete()
            except Exception:
                pass
            await call.answer()
            return

        quiz_id = call.data.replace("qdel_", "", 1)
        quiz = await admin_get_quiz_by_id(quiz_id)
        if not quiz:
            await call.answer("❌ هذا الكويز لم يعد موجوداً (ربما حُذف مسبقاً).", show_alert=True)
            return
        if not can_delete_quiz(call.from_user.id, quiz.get("creator_id")):
            await call.answer("❌ هذا الزر مخصص فقط لصاحب الكويز أو للإدارة.", show_alert=True)
            return

        source_title = quiz.get("source_title") or "كويز"
        kb = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="✅ نعم، احذف نهائياً", callback_data=f"qdelok_{quiz_id}")],
            [types.InlineKeyboardButton(text="◀️ تراجع", callback_data="qdel_cancel")],
        ])
        await call.message.answer(
            f"⚠️ <b>تأكيد حذف الكويز نهائياً</b>\n\n"
            f"سيتم حذف \"<code>{source_title}</code>\" نهائياً من كل مكان: الكاش/قائمة "
            f"الكويزات الجاهزة، مفضلات كل الطلاب اللي حفظوه، التصويتات، النقاط ولوحة "
            f"المتصدرين، والملاحظات المرتبطة به. هذا الإجراء لا يمكن التراجع عنه.\n\n"
            f"هل أنت متأكد؟",
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception as e:
        log_error(logger, f"Error preparing generic quiz delete confirmation: {e}")
        await call.answer("❌ حدث خطأ أثناء تجهيز الحذف.", show_alert=True)
    finally:
        await call.answer()


@router.callback_query(F.data.startswith("qdelok_"))
async def handle_quiz_delete_execute(call: types.CallbackQuery) -> None:
    """التنفيذ الفعلي للحذف - بعد إعادة فحص الصلاحية مرة أخيرة قبل الحذف."""
    try:
        quiz_id = call.data.replace("qdelok_", "", 1)
        quiz = await admin_get_quiz_by_id(quiz_id)
        if not quiz:
            await call.answer("❌ هذا الكويز محذوف بالفعل.", show_alert=True)
            try:
                await call.message.delete()
            except Exception:
                pass
            return
        if not can_delete_quiz(call.from_user.id, quiz.get("creator_id")):
            await call.answer("❌ هذا الزر مخصص فقط لصاحب الكويز أو للإدارة.", show_alert=True)
            return

        success = await admin_delete_quiz(quiz_id)
        if success:
            await call.answer("🗑️ تم حذف الكويز نهائياً من كل مكان.", show_alert=True)
            try:
                await call.message.edit_text("🗑 <b>تم حذف هذا الكويز نهائياً من كل مكان.</b>", parse_mode="HTML")
            except Exception:
                try:
                    await call.message.delete()
                except Exception:
                    pass
        else:
            await call.answer("❌ تعذر حذف الكويز، حاول مجدداً.", show_alert=True)
    except Exception as e:
        log_error(logger, f"Error executing generic quiz delete: {e}")
        await call.answer("❌ حدث خطأ أثناء الحذف.", show_alert=True)
