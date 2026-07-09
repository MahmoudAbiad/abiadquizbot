"""
Files handling module - handles document and photo uploads,
and background quiz generation using the Hybrid API flow.
"""

import os
import asyncio
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from pymupdf import message

from config import bot, QuizState
from constants import (
    SUCCESS_FILE_UPLOADED, SUCCESS_PHOTO_UPLOADED, MSG_PROCESSING,
    ERROR_INSUFFICIENT_POINTS
)
# 🆕 تم إضافة استيراد الدالة لحساب الهاش من ملف الـ utils
from gemini_helper import generate_quiz_smart
from utils import safe_file_cleanup, ensure_directory_exists, calculate_file_hash
# 🆕 تم إضافة استيراد دالة جلب الكاش من ملف الـ supabase_helper
from supabase_helper import check_or_add_user, update_user_stats, get_cached_quiz
from keyboards import get_main_menu_keyboard
from validators import validate_file_size, validate_question_count
from logger import get_logger, log_error, log_info, log_warning

logger = get_logger(__name__)
router = Router()

DOWNLOADS_DIR = "downloads"
processing_users_lock = asyncio.Lock()
processing_users: set[int] = set()

# ==================== File Handlers ====================

@router.message(F.document | F.photo)
async def handle_media(msg: types.Message, state: FSMContext):
    try:
        # فحص الحالة الحالية لحماية الكويز القائم
        current_status = await state.get_state()
        if current_status == QuizState.answering_quiz:
            await msg.answer("⚠️ **لديك اختبار قائم حالياً.** يرجى إكمال الاختبار أو الضغط على زر (⏹ إيقاف) أولاً قبل رفع ملفات أو صور جديدة.", parse_mode="Markdown")
            return

        ensure_directory_exists(DOWNLOADS_DIR)
        user_id = msg.from_user.id
        
        # 1. التحقق من الحجم قبل التنزيل (صمام أمان)
        if msg.document:
            is_valid, error = validate_file_size(msg.document.file_size, "document")
            if not is_valid:
                await msg.answer(error)
                return
            
            # [تعديل] استخراج اسم الملف الأصلي وتنظيفه من الامتداد (مثل .pdf)
            full_file_name = msg.document.file_name or "كويز من ملف"
            file_title, _ = os.path.splitext(full_file_name)
            
            file_path = os.path.join(DOWNLOADS_DIR, f"{user_id}_{msg.document.file_name}")
            await bot.download(msg.document, destination=file_path)
        else:
            photo = msg.photo[-1]
            is_valid, error = validate_file_size(photo.file_size, "photo")
            if not is_valid:
                await msg.answer(error)
                return
                
            # [تعديل] تعيين اسم افتراضي مخصص ومناسب للصور
            file_title = "كويز من صورة"
            file_path = os.path.join(DOWNLOADS_DIR, f"{user_id}_{photo.file_id}.jpg")
            await bot.download(photo, destination=file_path)

        # 🆕 [ميزة الكاش الذكي]: فحص الهاش والتحقق هل تمت معالجة هذا الملف مسبقاً أم لا
        file_hash = await asyncio.to_thread(calculate_file_hash, file_path)
        cached_data = await asyncio.to_thread(get_cached_quiz, file_hash)

        if cached_data and cached_data.get("questions_data"):
            questions_data = cached_data["questions_data"]
            q_count = len(questions_data)
            cache_cost = round(q_count * 0.1, 1)  # حساب تكلفة الـ 10% بدقة عشارية

            # تخزين البيانات مؤقتاً في جلسة المستخدم الحالية
            await state.update_data(
                file_path=file_path, 
                source_title=file_title,
                cached_questions=questions_data,
                cache_cost=cache_cost
            )

            # بناء لوحة الاختيارات (أزرار إنلاين) للمستخدم
            kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text=f"🎰 كويز جاهز ({cache_cost} نقطة)", callback_data="cache_action_yes")],
                [types.InlineKeyboardButton(text="🧠 توليد أسئلة جديدة (تكلفة كاملة)", callback_data="cache_action_no")]
            ])

            await state.set_state(QuizState.waiting_for_cache_decision)
            await msg.answer(
                f"💡 **هذا الملف تمت معالجته مسبقاً في السيرفر!**\n\n"
                f"📦 يحتوي على **{q_count}** سؤال جاهز تماماً.\n"
                f"🏷️ **عرض التوفير:** يمكنك خوض هذا الاختبار فوراً بتكلفة **{cache_cost}** نقاط فقط بدلاً من {q_count} نقطة!",
                reply_markup=kb,
                parse_mode="Markdown"
            )
            return

        # [تعديل] تخزين اسم الملف النظيف في الـ source_title بدلاً من الاسم الموحد القديم (السير الطبيعي)
        await state.update_data(file_path=file_path, source_title=file_title)
        await state.set_state(QuizState.waiting_for_count)
        await msg.answer("✅ تم رفع الملف بنجاح! كم سؤال تريد استخراجه من هذا المحتوى؟")

    except Exception as e:
        log_error(logger, f"Error in handle_media: {e}", exception=e)
        await msg.answer("❌ حدث خطأ غير متوقع أثناء معالجة الملف.")


# ==================== Cache Decision Handlers ====================

@router.callback_query(QuizState.waiting_for_cache_decision, F.data == "cache_action_yes")
async def handle_cache_yes(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        questions = data.get("cached_questions")
        cost = data.get("cache_cost", 0)
        source_title = data.get("source_title", "كويز من ملف")
        file_path = data.get("file_path")

        # التحقق من رصيد المستخدم الحالي
        user_info = await asyncio.to_thread(check_or_add_user, call.from_user.id, call.from_user.username or "Unknown", call.from_user.first_name or "Unknown", call.from_user.last_name or "Unknown")
        if user_info["points"] < cost:
            await call.answer(f"❌ نقاطك غير كافية! تكلفة الاختبار {cost} ورصيدك {user_info['points']}", show_alert=True)
            safe_file_cleanup(file_path)
            await state.clear()
            return

        # الخصم المخفض بنسبة 10% من قاعدة البيانات وتحديث إحصائيات المستخدم بالأسئلة المستخرجة
        await asyncio.to_thread(update_user_stats, call.from_user.id, cost, len(questions))

        from handlers.execution import _start_loaded_quiz
        # تشغيل الكويز المستخرج مباشرة من الكاش للمستخدم
        await _start_loaded_quiz(call.message, state, questions, source_title, origin="cached_file")
        try:
            await call.message.delete()
        except Exception:
            pass

    except Exception as e:
        log_error(logger, f"Error processing cache acceptance: {e}", exception=e)
        await call.answer("❌ حدث خطأ تقني أثناء تحميل الاختبار.", show_alert=True)
    finally:
        await call.answer()


@router.callback_query(QuizState.waiting_for_cache_decision, F.data == "cache_action_no")
async def handle_cache_no(call: types.CallbackQuery, state: FSMContext):
    try:
        # الانتقال للمسار التقليدي وسؤال المستخدم عن عدد الأسئلة المطلوبة
        await state.set_state(QuizState.waiting_for_count)
        await call.message.edit_text("🔄 رائع، سيتم تجاهل الكاش وتوليد أسئلة جديدة كلياً عبر الذكاء الاصطناعي.\n\n📝 **كم سؤال تريد استخراجه من هذا المحتوى؟**", parse_mode="Markdown")
    except Exception as e:
        log_error(logger, f"Error declining cache: {e}", exception=e)
    finally:
        await call.answer()


# ==================== Question Count Handler ====================

# ==================== Question Count Handler ====================

@router.message(QuizState.waiting_for_count, F.text.isdigit())
async def process_count(msg: types.Message, state: FSMContext):
    count = int(msg.text)
    is_valid, error = validate_question_count(count)
    if not is_valid:
        await msg.answer(f"❌ {error}")
        return

    data = await state.get_data()
    file_path = data.get('file_path')
    
    # التحقق من الرصيد
    user_info = await asyncio.to_thread(check_or_add_user, msg.from_user.id, msg.from_user.username or "Unknown", msg.from_user.first_name or "Unknown", msg.from_user.last_name or "Unknown")
    if user_info["points"] < count:
        await msg.answer(ERROR_INSUFFICIENT_POINTS.format(current=user_info["points"], required=count))
        safe_file_cleanup(file_path)
        await state.clear()
        return

    # 🆕 [ميزتك الجديدة]: فحص عدد الصفحات قبل بدء معالجة الملف
    if file_path.lower().endswith('.pdf'):
        try:
            import fitz
            doc = fitz.open(file_path)
            pages_total = len(doc)
            doc.close()
            
            if pages_total > 15:
                # حفظ عدد الأسئلة مؤقتاً في جلسة المستخدم للمتابعة لاحقاً
                await state.update_data(pending_count=count)
                
                # بناء أزرار المتابعة أو التراجع
                kb = types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="✅ المتابعة (أول 15 صفحة فقط)", callback_data="limit_action_continue")],
                    [types.InlineKeyboardButton(text="❌ التراجع وإلغاء الطلب", callback_data="limit_action_cancel")]
                ])
                
                # نقل المستخدم لحالة انتظار قرار الحجم
                await state.set_state(QuizState.waiting_for_cache_decision) # يمكن استخدام نفس الحالة المؤقتة أو إضافة واحدة جديدة في config
                await msg.answer(
                    f"⚠️ **تنبيه بخصوص حجم الملف:**\n\n"
                    f"الملف المرفوع يحتوي على **{pages_total}** صفحة. نظراً للضغط الحالي على سيرفرات الذكاء الاصطناعي، نحن غير قادرين على معالجة سوى 15 صفحة فقط من الملف.\n\n"
                    f"سيقوم النظام بقراءة **أول 15 صفحة فقط** وتوليد الأسئلة منها. هل ترغب في المتابعة أم التراجع؟",
                    reply_markup=kb
                )
                return
        except Exception as e:
            log_error(logger, f"خطأ أثناء فحص عدد الصفحات: {e}")

    # إذا كان الملف أقل من 15 صفحة، يستمر السير الطبيعي فوراً
    await trigger_quiz_generation(msg, file_path, count, state)


# 🆕 دالة مساعدة لبدء التوليد الفعلي منعاً لتكرار الكود
async def trigger_quiz_generation(msg: types.Message, file_path: str, count: int, state: FSMContext):
    async with processing_users_lock:
        if msg.from_user.id in processing_users:
            await msg.answer("⏳ جاري المعالجة، انتظر قليلاً...")
            return
        processing_users.add(msg.from_user.id)

    processing_msg = await msg.answer(MSG_PROCESSING)
    asyncio.create_task(_run_quiz_flow(msg, file_path, count, state, processing_msg))


# 🆕 مستقبلات الأزرار الجديدة للتعامل مع قرار الـ 15 صفحة
@router.callback_query(F.data == "limit_action_continue")
async def handle_limit_continue(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        file_path = data.get('file_path')
        count = data.get('pending_count')
        
        await call.message.edit_text("🔄 جاري التوجيه ومعالجة أول 15 صفحة من المستند...")
        await trigger_quiz_generation(call.message, file_path, count, state)
    except Exception as e:
        log_error(logger, f"Error in limit continue callback: {e}")
    finally:
        await call.answer()


@router.callback_query(F.data == "limit_action_cancel")
async def handle_limit_cancel(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        file_path = data.get('file_path')
        
        safe_file_cleanup(file_path)
        await state.clear()
        
        await call.message.edit_text("❌ تم إلغاء العملية بنجاح بناءً على طلبك. يمكنك رفع ملف آخر في أي وقت.")
    except Exception as e:
        log_error(logger, f"Error in limit cancel callback: {e}")
    finally:
        await call.answer()


async def _run_quiz_flow(msg, file_path, count, state, processing_msg):
    """
    سير العمل الموحد: توليد الأسئلة ثم البدء.
    """
    try:
        # [تعديل] جلب اسم الملف الديناميكي الذي حفظناه في الخطوة السابقة من الـ State
        data = await state.get_data()
        source_title = data.get('source_title', "كويز من ملف")

        # استدعاء الدالة الذكية (Gemini) مباشرة مع تمرير العلم skip_cache=True لتوليد كويز جديد كلياً
        quiz_data = await generate_quiz_smart(file_path=file_path, count=count, skip_cache=True)
        
        if not quiz_data:
            await processing_msg.edit_text("هناك ضغط على السيرفر، يرجى المحاولة بعد قليل")
            return

        # تحديث الإحصائيات وبدء الكويز
        await asyncio.to_thread(update_user_stats, msg.from_user.id, len(quiz_data), len(quiz_data))
        
        from handlers.execution import _start_loaded_quiz
        # [تعديل] تمرير متغير source_title الديناميكي هنا بدلاً من الكلمة الثابتة
        await _start_loaded_quiz(msg, state, quiz_data, source_title, origin="file")
        await processing_msg.delete()

    except Exception as e:
        log_error(logger, f"Error in quiz flow: {e}", exception=e)
        await processing_msg.edit_text("⚠️ حدث خطأ تقني.")
    finally:
        safe_file_cleanup(file_path)
        async with processing_users_lock:
            processing_users.discard(msg.from_user.id)
            await state.update_data(quiz_processing=False)

files_router = router