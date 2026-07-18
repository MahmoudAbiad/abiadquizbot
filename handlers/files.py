"""
Files and Text handling module - handles documents, multiple photos (albums),
and direct text inputs using Gemini for media and Groq for pure text.
"""

import os
import asyncio
import uuid 
import json  # 💡 مضاف لمعالجة تسلسل بيانات الألبوم المشتركة
from aiogram import Router, types, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

from config import bot, QuizState, redis_client  # 💡 تم استدعاء عميل Redis المشترك
from constants import (
    SUCCESS_MEDIA_RECEIVED, MSG_PROCESSING, ERROR_INSUFFICIENT_POINTS,
    MAX_IMAGES_IN_ALBUM, MAX_PDF_PAGES, MAX_TEXT_INPUT_SIZE, DAILY_RENEWAL_POINTS
)
from gemini_helper import generate_quiz_smart
from utils import safe_file_cleanup, ensure_directory_exists, calculate_file_hash
# ✅ تم دمج أسطر الاستيراد المتكررة هنا في سطر واحد نظيف يمنع التداخل
from supabase_helper import check_or_add_user, update_user_stats, get_cached_quiz, save_shared_quiz
from validators import validate_file_size, validate_question_count
from logger import get_logger, log_error
from PIL import Image

def calculate_image_hash(image_path: str) -> str:
    """
    توليد بصمة مرئية ثابتة للصورة بناءً على مظهرها لمنع تأثر الكاش بضغط تليغرام.
    """
    try:
        with Image.open(image_path) as img:
            # تحويل الصورة للون الرمادي وتصغيرها لـ 8x8 لتقليل الحساسية للفروقات الرقمية الصغيرة
            img = img.convert('L').resize((8, 8))
            pixels = list(img.getdata())
            avg = sum(pixels) / len(pixels)
            # بناء سلسلة البتات بمقارنة كل بكسل بمتوسط الإضاءة
            bits = "".join(['1' if p >= avg else '0' for p in pixels])
            # تحويل الـ 64 بت إلى نص سداسي عشر ثابت
            return f"img_{int(bits, 2):016x}"
    except Exception:
        return ""
    
logger = get_logger(__name__)
router = Router()

DOWNLOADS_DIR = "downloads"
processing_users_lock = asyncio.Lock()
processing_users: set[int] = set()

# 🚀 الموضع المضاف 1: دالة المؤقت الخلفي الذكي لإلغاء الانتظار وإشعار المستخدم بعد 15 دقيقة
async def auto_cancel_upload_timeout(chat_id: int, user_id: int, state: FSMContext, timeout: int = 900):
    await asyncio.sleep(timeout)
    current_state = await state.get_state()
    
    # التحقق: إذا مرت الـ 15 دقيقة ولا يزال المستخدم عالقاً في نفس الحالة ولم يرسل رقم الأسئلة
    if current_state == QuizState.waiting_for_count:
        data = await state.get_data()
        file_paths = data.get("file_paths", [])
        
        # 1. تنظيف الملفات فوراً من الهارد ديسك لتوفير مساحة السيرفر
        for path in file_paths:
            safe_file_cleanup(path)
            
        # 2. تصفير حالة المستخدم
        await state.clear()
        
        # 3. إشعار الطالب بلطف عبر رسالة تلغرام
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="⚠️ **انتهت مهلة الانتظار (15 دقيقة).**\nتم إلغاءطلبك الحالي تلقائياً وتنظيف الملفات لتوفير مساحة السيرفر. يمكنك إرسال ملف أو نص جديد في أي وقت تريد! 🔄",
                parse_mode="Markdown"
            )
        except Exception:
            pass

# 🚀 الموضع المضاف 1: دالة فحص وتطهير الملفات المهجورة التي مر عليها أكثر من ساعة
def clean_old_files_inline(directory: str, max_age_seconds: int = 900):
    import time
    try:
        if not os.path.exists(directory):
            return
        now = time.time()
        for filename in os.listdir(directory):
            file_path = os.path.join(directory, filename)
            if os.path.isfile(file_path):
                # إذا كان وقت تعديل الملف أقدم من ساعة، يتم حذفه فوراً
                if os.path.getmtime(file_path) < now - max_age_seconds:
                    os.remove(file_path)
                    logger.info(f"🗑️ تم حذف ملف مهجور تلقائياً لتوفير المساحة: {filename}")
    except Exception as e:
        logger.error(f"خطأ أثناء تنظيف الملفات المهجورة: {e}")

# 🎯 تم التحديث: تجميع ألبومات الصور بنظام العداد التنازلي الذكي عبر Redis عابر للـ Workers والخيوط
async def collect_album_photos_redis(msg: types.Message) -> list[dict]:
    """تجميع صور الألبوم بشكل آمن وموزع عبر Redis لضمان التوافق مع تعدد الـ Workers والخيوط"""
    if not msg.media_group_id:
        photo = msg.photo[-1]
        return [{"file_id": photo.file_id, "file_unique_id": photo.file_unique_id, "file_size": photo.file_size}]
    
    mg_id = msg.media_group_id
    photo = msg.photo[-1]
    photo_data = {
        "file_id": photo.file_id, 
        "file_unique_id": photo.file_unique_id, 
        "file_size": photo.file_size
    }
    
    list_key = f"album_list:{mg_id}"
    count_key = f"album_count:{mg_id}"
    
    # 1. دفع بيانات الصورة الحالية إلى قائمة الألبوم المركزية في Redis
    await redis_client.rpush(list_key, json.dumps(photo_data))
    await redis_client.expire(list_key, 60)
    
    # 2. زيادة العداد الذري لمعرفة الترتيب الحالي للرسالة المستلمة في الخيط الحالي
    current_index = await redis_client.incr(count_key)
    await redis_client.expire(count_key, 60)
    
    # 3. النوم لثانية ونصف لضمان وصول وتجميع كافة الرسائل المتوازية للألبوم بالكامل داخل القائمة
    await asyncio.sleep(1.4)
    
    # 4. جلب القيمة النهائية للعداد لمعرفة القيمة الأحدث التي استقرت في السيرفر
    final_count_raw = await redis_client.get(count_key)
    final_count = int(final_count_raw) if final_count_raw else current_index
    
    # 5. الرسالة الأخيرة المكتشفة شبكياً فقط (صاحبة الرقم الأكبر) هي من تتولى المعالجة للألبوم كاملاً
    if current_index == final_count:
        raw_items = await redis_client.lrange(list_key, 0, -1)
        
        # تنظيف فوري لمفاتيح الألبوم الحالية من Redis لمنع التراكم في الرام
        await redis_client.delete(list_key)
        await redis_client.delete(count_key)
        
        # تصفية المعرفات الفريدة لضمان عدم تكرار قراءة الصور تحت أي ظرف شبكي طارئ
        seen = set()
        unique_photos = []
        for item in raw_items:
            p = json.loads(item)
            if p["file_unique_id"] not in seen:
                seen.add(p["file_unique_id"])
                unique_photos.append(p)
        return unique_photos
    else:
        # الرسائل والـ Workers الفرعية السابقة تخرج بصمت تام لحماية البوت من التكرار والازدواجية
        return []

# ✅ لقراءة عدد الصفحات في خيط منفصل
def get_pdf_page_count_sync(file_path: str) -> int:
    import fitz
    doc = fitz.open(file_path)
    pages_total = len(doc)
    doc.close()
    return pages_total

# ==================== Media Handlers (Photos & Documents) ====================

@router.message(F.document | F.photo)
async def handle_media(msg: types.Message, state: FSMContext):
    try:
        clean_old_files_inline(DOWNLOADS_DIR)
        current_status = await state.get_state()
        
        # ⚠️ هنا يتم الفحص الذكي: المنع يشتغل فقط لو المستخدم داخل الاختبار فعلياً
        if current_status == QuizState.answering_quiz:
            await msg.answer("⚠️ **لديك اختبار قائم حالياً.** يرجى إكمال الاختبار أو الضغط على زر (⏹ إيقاف) أولاً.", parse_mode="Markdown")
            return

        ensure_directory_exists(DOWNLOADS_DIR)
        user_id = msg.from_user.id

        # معالجة ألبوم الصور أو صورة مفردة
        if msg.photo:
            all_photos = await collect_album_photos_redis(msg)
            if not all_photos: 
                return # الخروج بصمت للمهام الفرعية، خيط التنسيق الأساسي سيتولى الباقي
            
            # 🔥 ترتيب الصور شبكياً بناءً على معرّفها الفريد لضمان ثبات توليد الهاش المجمع للألبوم دائماً
            all_photos.sort(key=lambda p: p["file_unique_id"])
            
            if len(all_photos) > MAX_IMAGES_IN_ALBUM:
                await msg.answer(f"❌ الحد الأقصى هو {MAX_IMAGES_IN_ALBUM} صور في المرة الواحدة.")
                return

            file_paths = []
            for idx, p_data in enumerate(all_photos):
                f_id = p_data["file_id"]
                is_valid, error = validate_file_size(p_data["file_size"], "photo")
                if not is_valid:
                    await msg.answer(f"❌ الصورة رقم {idx+1}: {error}")
                    for path in file_paths: safe_file_cleanup(path)
                    return
                f_path = os.path.join(DOWNLOADS_DIR, f"{user_id}_{f_id}.jpg")
                await bot.download(file=f_id, destination=f_path)
                file_paths.append(f_path)
            
            # التمييز الذكي بين الصورة المفردة والألبوم وحساب الهاش المجمع لمنع التضارب
            if len(file_paths) > 1:
                file_title = f"كويز من ألبوم صور ({len(file_paths)} صور)"
                img_hashes = [await asyncio.to_thread(calculate_image_hash, path) for path in file_paths]
                file_hash = "-".join(img_hashes)
            else:
                file_title = "كويز من صورة"
                file_hash = await asyncio.to_thread(calculate_image_hash, file_paths[0])

        # معالجة المستندات بجميع أنواعها
        else:
            is_valid, error = validate_file_size(msg.document.file_size, "document")
            if not is_valid:
                await msg.answer(error)
                return
            
            # 1. الاحتفاظ بالاسم الأصلي بدون امتداد ليكون عنواناً للكويز
            full_file_name = msg.document.file_name or "document.pdf"
            file_title, ext = os.path.splitext(full_file_name)
            
            # 2. استخدام file_id والامتداد الأصلي لتفادي مشاكل اللغة العربية والمسافات
            safe_file_name = f"{user_id}_{msg.document.file_id}{ext}"
            f_path = os.path.join(DOWNLOADS_DIR, safe_file_name)
            
            # 3. تحميل الملف بالاسم الآمن الجديد
            await bot.download(msg.document, destination=f_path)
            file_paths = [f_path]
            file_hash = await asyncio.to_thread(calculate_file_hash, f_path)

        # [ميزة الكاش الذكي] - المحاذاة أصبحت صحيحة 100% خارج البلوكات للصور والمستندات معاً
        cached_data = await get_cached_quiz(file_hash)
        
        if cached_data and cached_data.get("questions_data"):
            questions_data = cached_data["questions_data"]
            q_count = len(questions_data)
            cache_cost = round(q_count * 0.1, 1)

            # 🔥 حفظ حقل file_hash هنا أيضاً لحمايته من الفقدان الشبكي داخل الـ State
            await state.update_data(
                file_paths=file_paths,
                source_title=file_title,
                cached_questions=questions_data,
                cache_cost=cache_cost,
                input_type="media",
                file_hash=file_hash
            )

            kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text=f"🎰 كويز جاهز ({cache_cost} نقطة)", callback_data="cache_action_yes")],
                [types.InlineKeyboardButton(text="🧠 توليد أسئلة جديدة (تكلفة كاملة)", callback_data="cache_action_no")]
            ])

            await state.set_state(QuizState.waiting_for_cache_decision)
            await msg.answer(
                f"💡 **هذا المحتوى تمت معالجته مسبقاً في السيرفر!**\n\n"
                f"📦 يحتوي على **{q_count}** سؤال جاهز.\n"
                f"🏷️ **عرض التوفير:** يمكنك خوض الاختبار بتكلفة **{cache_cost}** نقاط فقط!",
                reply_markup=kb, parse_mode="Markdown"
            )
            return

        # ⚡ حفظ حقل file_hash الموحد للألبوم داخل الـ State لربطه بمرحلة التوليد الفعلي
        await state.update_data(file_paths=file_paths, source_title=file_title, input_type="media", file_hash=file_hash)
        await state.set_state(QuizState.waiting_for_count)
        await msg.answer(SUCCESS_MEDIA_RECEIVED)

        # 🚀 إطلاق دالة الإلغاء التلقائي في الخلفية دون حظر السيرفر
        asyncio.create_task(auto_cancel_upload_timeout(msg.chat.id, msg.from_user.id, state, timeout=900))

    except Exception as e:
        log_error(logger, f"Error in handle_media: {e}", exception=e)
        await msg.answer("❌ حدث خطأ غير متوقع أثناء معالجة الوسائط.")


# ==================== Text Handler (Groq Exclusive) ====================

@router.message(StateFilter(None), F.text, ~F.text.startswith('/'))
async def handle_pure_text(msg: types.Message, state: FSMContext):
    text_content = msg.text.strip()
    
    if len(text_content) < 30:
        await msg.answer("⚠️ النص قصير جداً! يرجى إرسال نص تعليمي مفصل (30 حرف كحد أدنى) لتوليد الأسئلة منه.")
        return

    if len(text_content) > MAX_TEXT_INPUT_SIZE:
        await msg.answer(
            f"❌ **عذراً، النص المرسل طويل جداً!**\n"
            f"الحد الأقصى المسموح به لإدخال النصوص المباشرة هو **{MAX_TEXT_INPUT_SIZE}** حرف.\n"
            f"نصك الحالي يحتوي على **{len(text_content)}** حرف.\n\n"
            f"💡 *نصيحة:* يمكنك تقسيم النص وإرساله على أجزاء، أو حفظه داخل ملف PDF وإرساله للبوت كملف.",
            parse_mode="Markdown"
        )
        return

    file_title = text_content[:20] + "..."
    await state.update_data(pure_text=text_content, source_title=file_title, input_type="text")
    await state.set_state(QuizState.waiting_for_count)
    await msg.answer("✅ تم استقبال النص بنجاح! كم سؤال تريد استخراجه من هذا النص？")

    asyncio.create_task(auto_cancel_upload_timeout(msg.chat.id, msg.from_user.id, state, timeout=900))

# ==================== Cache Decision Handlers ====================

@router.callback_query(QuizState.waiting_for_cache_decision, F.data == "cache_action_yes")
async def handle_cache_yes(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        questions = data.get("cached_questions")
        cost = data.get("cache_cost", 0)
        source_title = data.get("source_title")
        file_paths = data.get("file_paths", [])

        user_info = await check_or_add_user(call.from_user.id, call.from_user.username or "Unknown", call.from_user.first_name or "Unknown", call.from_user.last_name or "Unknown")
        if user_info.get("status") == "renewed":
            await call.message.answer(f"☀️ <b>يا أهلاً، يومك سعيد!</b>\nتم تججديد رصيدك اليومي وإضافة <b>{DAILY_RENEWAL_POINTS} نقطة مجانية جديدة</b> لحسابك. 🔄", parse_mode="HTML")
        if user_info["points"] < cost:
            await call.answer(f"❌ نقاطك غير كافية!", show_alert=True)
            for path in file_paths: safe_file_cleanup(path)
            await state.clear()
            return

        await update_user_stats(call.from_user.id, cost, len(questions))

        quiz_id = uuid.uuid4().hex[:12]
        await save_shared_quiz(quiz_id, call.from_user.id, source_title, questions)

        from handlers.execution import _start_loaded_quiz
        await _start_loaded_quiz(call.message, state, questions, source_title, origin="cached_file", quiz_id=quiz_id)
        try: await call.message.delete()
        except Exception: pass

    except Exception as e:
        log_error(logger, f"Error processing cache acceptance: {e}")
    finally: await call.answer()


@router.callback_query(QuizState.waiting_for_cache_decision, F.data == "cache_action_no")
async def handle_cache_no(call: types.CallbackQuery, state: FSMContext):
    try:
        await state.set_state(QuizState.waiting_for_count)
        await call.message.edit_text("📝 **كم سؤال تريد استخراجه من هذا المحتوى؟**", parse_mode="Markdown")
        asyncio.create_task(auto_cancel_upload_timeout(call.message.chat.id, call.from_user.id, state, timeout=900))
    except Exception as e: log_error(logger, f"Error declining cache: {e}")
    finally: await call.answer()


# ==================== Question Count Handler ====================

@router.message(QuizState.waiting_for_count, F.text.isdigit())
async def process_count(msg: types.Message, state: FSMContext):
    count = int(msg.text)
    is_valid, error = validate_question_count(count)
    if not is_valid:
        await msg.answer(f"❌ {error}")
        return

    data = await state.get_data()
    input_type = data.get("input_type")
    
    user_info = await check_or_add_user(msg.from_user.id, msg.from_user.username or "Unknown", msg.from_user.first_name or "Unknown", msg.from_user.last_name or "Unknown")
    if user_info.get("status") == "renewed":
        await msg.answer(f"☀️ <b>يا أهلاً، يومك سعيد!</b>\nتم تجديد رصيدك اليومي وإضافة <b>{DAILY_RENEWAL_POINTS} نقطة مجانية جديدة</b> لحسابك. 🔄", parse_mode="HTML")
    if user_info["points"] < count:
        await msg.answer(f"❌ رصيدك لا يكفي! تحتاج {count} نقاط.")
        if input_type == "media":
            for path in data.get("file_paths", []): safe_file_cleanup(path)
        await state.clear()
        return

    if input_type == "media":
        file_paths = data.get("file_paths", [])
        if len(file_paths) == 1 and file_paths[0].lower().endswith('.pdf'):
            try:
                pages_total = await asyncio.to_thread(get_pdf_page_count_sync, file_paths[0])
                
                if pages_total > MAX_PDF_PAGES:
                    await state.update_data(pending_count=count)
                    
                    kb = types.InlineKeyboardMarkup(inline_keyboard=[
                        [types.InlineKeyboardButton(text=f"✅ المتابعة (أول {MAX_PDF_PAGES} صفحة فقط)", callback_data="limit_action_continue")],
                        [types.InlineKeyboardButton(text="❌ التراجع وإلغاء الطلب", callback_data="limit_action_cancel")]
                    ])
                    await state.set_state(QuizState.waiting_for_limit_decision)
                    
                    await msg.answer(
                        f"⚠️ المستند يحتوي على **{pages_total}** صفحة.\n"
                        f"سيقوم النظام بمعالجة **أول {MAX_PDF_PAGES} صفحة فقط** بناءً على حدود النظام.\n\n"
                        f"هل ترغب بالمتابعة؟", 
                        reply_markup=kb, 
                        parse_mode="Markdown"
                    )
                    return
            except Exception as e: 
                log_error(logger, f"Error checking pages: {e}")

    await trigger_quiz_generation(msg, msg.from_user.id, count, state)


@router.message(QuizState.waiting_for_count)
async def process_count_invalid(msg: types.Message):
    await msg.answer("⚠️ **الرجاء إرسال رقم صحيح فقط** (مثال: 5 أو 10) وتجنب كتابة الكلمات لتحديد عدد الأسئلة!")


# ====================================================================

async def trigger_quiz_generation(msg_obj: types.Message, user_id: int, count: int, state: FSMContext):
    async with processing_users_lock:
        if user_id in processing_users:
            await msg_obj.answer("⏳ جاري المعالجة, انتظر قليلاً...")
            return
        processing_users.add(user_id)

    processing_msg = await msg_obj.answer(MSG_PROCESSING)
    asyncio.create_task(_run_quiz_flow(msg_obj, user_id, count, state, processing_msg))


@router.callback_query(F.data == "limit_action_continue")
async def handle_limit_continue(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        count = data.get('pending_count')
        await call.message.edit_text(f"🔄 جاري توجيه ومعالجة أول {MAX_PDF_PAGES} صفحة من المستند...")
        await trigger_quiz_generation(call.message, call.from_user.id, count, state)
    except Exception as e: log_error(logger, f"Error in limit continue: {e}")
    finally: await call.answer()


@router.callback_query(F.data == "limit_action_cancel")
async def handle_limit_cancel(call: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        for path in data.get('file_paths', []): safe_file_cleanup(path)
        await state.clear()
        await call.message.edit_text("❌ تم إلغاء العملية بنجاح.")
    except Exception as e: log_error(logger, f"Error in limit cancel: {e}")
    finally: await call.answer()


async def _run_quiz_flow(msg, user_id: int, count: int, state: FSMContext, processing_msg):
    try:
        data = await state.get_data()
        input_type = data.get("input_type")
        source_title = data.get('source_title', "كويز")
        file_hash = data.get("file_hash") 

        quiz_data = await generate_quiz_smart(
            file_paths=data.get("file_paths") if input_type == "media" else None,
            pure_text=data.get("pure_text") if input_type == "text" else None,
            count=count,
            skip_cache=True,
            file_hash=file_hash
        )
        
        if not quiz_data:
            await processing_msg.edit_text("⚠️ فشل توليد الأسئلة، يرجى المحاولة لاحقاً.")
            return

        quiz_id = uuid.uuid4().hex[:12]
        await save_shared_quiz(quiz_id, user_id, source_title, quiz_data)
        await update_user_stats(user_id, len(quiz_data), len(quiz_data))
        
        from handlers.execution import _start_loaded_quiz
        await _start_loaded_quiz(msg, state, quiz_data, source_title, origin="file" if input_type == "media" else "text", quiz_id=quiz_id)
        await processing_msg.delete()

    except Exception as e:
        log_error(logger, f"Error in quiz flow: {e}", exception=e)
        await processing_msg.edit_text("⚠️ حدث خطأ تقني.")
    finally:
        if input_type == "media":
            for path in data.get("file_paths", []): safe_file_cleanup(path)
        async with processing_users_lock:
            processing_users.discard(user_id)

files_router = router