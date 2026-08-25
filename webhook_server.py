"""
Webhook configuration and FastAPI setup for Azure/Railway deployment.
Handles HTTP server setup safely with modern lifespan context and proper Pydantic validation.
"""

import os
import asyncio  
from typing import List
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from contextlib import asynccontextmanager
from aiogram.types import Update
from config import bot, dp, set_bot_commands, redis_client
from logger import get_logger
from constants import (
    WEBHOOK_PATH, WEBHOOK_PORT, TELEGRAM_WEBHOOK_SECRET,
    MAX_AUDIO_WEB_UPLOAD_SIZE, AUDIO_UPLOAD_INIT_DATA_MAX_AGE_SECONDS,
    AUDIO_UPLOAD_RATE_LIMIT_MAX_REQUESTS, AUDIO_UPLOAD_RATE_LIMIT_WINDOW_SECONDS,
    MAX_FILE_WEB_UPLOAD_SIZE, FILE_UPLOAD_INIT_DATA_MAX_AGE_SECONDS,
    FILE_UPLOAD_RATE_LIMIT_MAX_REQUESTS, FILE_UPLOAD_RATE_LIMIT_WINDOW_SECONDS,
    MAX_IMAGE_WEB_UPLOAD_COUNT, MAX_IMAGE_WEB_UPLOAD_SIZE_PER_IMAGE,
)
from telegram_webapp_auth import verify_telegram_init_data

# 🆕 دوال التحليلات وتنظيف قاعدة البيانات تضل من Supabase (Postgres) - لا علاقة لها
# بمشكلة سقف الـ50MB (تلك خاصة بـStorage فقط، مو بقاعدة البيانات).
from supabase_helper import flush_analytics_queue, auto_cleanup_old_analytics_data

# 🆕 دوال التخزين المؤقت (رفع الصوت/الملفات/الصور) انتقلت من Supabase Storage
# لـ Cloudflare R2 (راجع helpers/r2_helper.py) - نفس الأسماء تماماً (drop-in)
# لتفادي أي تعديل إضافي بمكان استدعائها هون أو بـ handlers/audio.py و files.py.
from r2_helper import (
    create_audio_upload_target, cleanup_stale_audio_uploads,
    get_audio_temp_object_size, delete_audio_temp,
    create_file_upload_target, create_image_upload_targets, cleanup_stale_file_uploads,
    get_file_temp_object_size, delete_file_temp, delete_file_temp_batch,
)
from handlers.audio import process_web_uploaded_audio
from handlers.files import process_web_uploaded_file, process_web_uploaded_images

logger = get_logger(__name__)

# ==================== Background Tasks ====================

async def process_update_safely(update: Update):
    """
    معالجة التحديث الخاص بـ Telegram في الخلفية مع التقاط الأخطاء
    لضمان عدم توقف المهمة أو ضياع السجلات عند حدوث استثناء.
    """
    try:
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"Error processing update in background task: {e}", exc_info=True)


async def scheduled_analytics_batch_loop():
    """
    مهمة خلفية دورية تعمل كل دقيقة (60 ثانية) لتفريغ قائمة الأحداث المتجمعة
    في Redis ورفعها دفعة واحدة (Batch) إلى Supabase لتخفيض الاتصالات.
    """
    while True:
        try:
            await flush_analytics_queue()
        except Exception as e:
            logger.error(f"Error inside the background analytics batch task: {e}")
        await asyncio.sleep(60)  # رفع التحديثات كل دقيقة


async def scheduled_cleanup_loop():
    """
    مهمة خلفية دورية تعمل كل 12 ساعة لفحص قاعدة البيانات وتنظيف
    الأحداث القديمة جداً (30 يوماً) والكويزات الرديئة التي تجاوزت 3 أيام.
    """
    while True:
        try:
            # تم تعديل اسم الدالة للاستدعاء الصحيح من supabase_helper
            await auto_cleanup_old_analytics_data()
            # 🆕 شبكة أمان: حذف أي ملفات صوتية مؤقتة تبقّت بـ R2 لأكثر من ساعة
            # (معالجة انقطعت استثنائياً قبل الوصول لـ finally)
            deleted = await cleanup_stale_audio_uploads(older_than_seconds=3600)
            if deleted:
                logger.info(f"Cleaned up {deleted} stale audio-temp file(s) from R2.")
            # 🆕 نفس شبكة الأمان لباكيت الملفات (مستندات + ألبومات صور) المؤقت
            deleted_files = await cleanup_stale_file_uploads(older_than_seconds=3600)
            if deleted_files:
                logger.info(f"Cleaned up {deleted_files} stale file-temp object(s) from R2.")
        except Exception as e:
            logger.error(f"Error inside the background scheduled cleanup task: {e}")
        await asyncio.sleep(43200)  # فحص وتنظيف كل 12 ساعة (43200 ثانية)

# ==================== Lifespan Context (Modern Event Handling) ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    إدارة أحداث بدء وإيقاف السيرفر بشكل آمن وبالمعايير الحديثة لـ FastAPI.
    """
    # [حدث الـ Startup]: يتم تنفيذه عند إقلاع السيرفر
    try:
        # تنظيف وجرف مجلد التحميلات بالكامل عند إقلاع السيرفر على Railway
        import shutil
        if os.path.exists("downloads"):
            shutil.rmtree("downloads")
        os.makedirs("downloads", exist_ok=True)
        
        webhook_url = os.getenv("WEBHOOK_URL")
        if webhook_url:
            full_webhook_url = f"{webhook_url.rstrip('/')}{WEBHOOK_PATH}"

            if not TELEGRAM_WEBHOOK_SECRET:
                raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is not configured")
            
            # تنظيف الـ Webhook القديم وتفعيل الجديد
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.set_webhook(
                url=full_webhook_url,
                drop_pending_updates=True,
                allowed_updates=["message", "callback_query", "poll_answer", "poll"],
                secret_token=TELEGRAM_WEBHOOK_SECRET,
            )
            print(f"✅ تم تفعيل Webhook بنجاح على: {full_webhook_url}")
            
            # تفعيل قائمة الأوامر
            await set_bot_commands(bot)
            
        # ⚡ إطلاق مهمة تجميع وتفريغ تحليلات Redis كل دقيقة
        asyncio.create_task(scheduled_analytics_batch_loop())
        print("⚡ تم جدولة رفع أحداث التحليلات كل دقيقة عبر Redis Batching.")

        # 🧹 إطلاق مهمة التنظيف التلقائي الدوري للبيانات
        asyncio.create_task(scheduled_cleanup_loop())
        print("🔄 تم جدولة تنظيف البيانات التلقائي كل 12 ساعة.")
            
    except Exception as e:
        print(f"❌ فشل تفعيل الـ Webhook أو المهام الدورية أثناء تشغيل السيرفر: {e}")
        logger.error(f"Failed to set webhook or tasks on startup: {e}")

    yield  # هنا يعمل السيرفر ويستقبل الطلبات...

    # [حدث الـ Shutdown]: يتم تنفيذه عند إغلاق السيرفر
    try:
        # تفريغ أخير لأي أحداث متبقية في Redis قبل إيقاف السيرفر
        await flush_analytics_queue()
        await bot.session.close()
        print("🛑 تم تفريغ السجل الأخير وإغلاق جلسة البوت بنجاح.")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")


# إنشاء تطبيق FastAPI وتمرير الـ lifespan له
app = FastAPI(title="Quiz Maker Bot", version="2.0", lifespan=lifespan)

# 🆕 تقديم صفحة رفع الملفات الصوتية (Mini App) كملفات ستاتيك من نفس الدومين
app.mount("/webapp", StaticFiles(directory="webapp"), name="webapp")

# ==================== Endpoints ====================

@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    return {"status": "ok", "bot": "running"}

@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    """
    استقبال التحديثات من تلغرام والتحقق منها، ثم إحالتها للمعالجة في الخلفية
    والرد فوراً لـ Telegram بـ OK لمنع الـ Retries والبطء.
    """
    if TELEGRAM_WEBHOOK_SECRET:
        incoming_secret = request.headers.get("x-telegram-bot-api-secret-token")
        if incoming_secret != TELEGRAM_WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Forbidden")

    try:
        update_data = await request.json()
        update = Update.model_validate(update_data)
        
        # ⚡ تشغيل المعالجة الكاملة للتحديث في الخلفية دون انتظار
        asyncio.create_task(process_update_safely(update))
        
    except Exception as e:
        logger.error(f"Error parsing incoming webhook update: {e}", exc_info=True)

    # الرد فوراً بـ OK لتلغرام لإنهاء طلب الـ HTTP بلمح البصر
    return {"ok": True}

# ==================== Audio Web Upload (Telegram Mini App) ====================

async def _enforce_upload_rate_limit(user_id: int, bucket: str, max_requests: int, window_seconds: int) -> None:
    """
    🆕 تحديد معدل طلبات بسيط عبر Redis (نفس الاتصال المستخدم أصلاً لبقية المشروع)،
    لكل مستخدم مُتحقَّق منه (وليس IP - لتفادي حظر مستخدمين شرعيين خلف نفس الـ NAT)
    ولكل endpoint على حدة (bucket مثل "audio_init"/"file_init"/"images_init"، حتى لا
    يُستهلك نفس الرصيد من endpoint واحد على حساب الآخر). عُمِّمت لتقبل max_requests/
    window_seconds كباراميترات بدل ثوابت الصوت المُثبَّتة، لإعادة استخدامها بمسارات
    الملفات والصور بنفس الآلية دون تكرار.

    يستخدم عداد بسيط (INCR + EXPIRE عند أول طلب بالنافذة) بدل خوارزمية Sliding
    Window أدق - كافٍ تماماً لمنع إساءة استخدام هذه الـ endpoints تحديداً، وبكلفة
    نداء Redis واحد أو اثنين فقط لكل طلب.

    يرمي HTTPException 429 عند تجاوز الحد، ولا يفعل شيئاً غير ذلك.
    """
    key = f"upload_rl:{bucket}:{user_id}"
    try:
        current = await redis_client.incr(key)
        if current == 1:
            await redis_client.expire(key, window_seconds)
        if current > max_requests:
            raise HTTPException(status_code=429, detail="طلبات كثيرة جداً، يرجى الانتظار قليلاً قبل إعادة المحاولة.")
    except HTTPException:
        raise
    except Exception as e:
        # فشل Redis نفسه لا يجب أن يوقف ميزة الرفع بالكامل - يُسجَّل فقط كتحذير،
        # ويُسمح للطلب بالمتابعة (فشل مفتوح/fail-open) بدل حجب المستخدمين الشرعيين.
        logger.warning(f"Upload rate-limit check failed (allowing request): {e}")


async def _enforce_audio_upload_rate_limit(user_id: int, bucket: str) -> None:
    """نظير رقيق فوق _enforce_upload_rate_limit بثوابت الصوت تحديداً - أُبقيَ عليه
    بنفس الاسم لعدم تغيير أي استدعاء موجود مسبقاً."""
    await _enforce_upload_rate_limit(user_id, bucket, AUDIO_UPLOAD_RATE_LIMIT_MAX_REQUESTS, AUDIO_UPLOAD_RATE_LIMIT_WINDOW_SECONDS)


class AudioUploadInitRequest(BaseModel):
    init_data: str
    file_size: int
    file_name: str = ""


class AudioUploadCompleteRequest(BaseModel):
    init_data: str
    object_path: str
    file_name: str = ""


@app.post("/api/audio-upload/init")
async def audio_upload_init(payload: AudioUploadInitRequest):
    """
    يتحقق من initData ومن حجم الملف المُعلَن، ثم يولّد رابط رفع موقّع (presigned PUT
    URL) على Cloudflare R2. الرفع الفعلي بعدها يصير مباشرة من متصفح المستخدم لـ R2
    (مو عبر Heroku/Railway) - حد الـ 30 ثانية لراوتر السيرفر مو مشكلة هون.
    """
    ok, user = verify_telegram_init_data(
        payload.init_data,
        bot.token,
        max_age_seconds=AUDIO_UPLOAD_INIT_DATA_MAX_AGE_SECONDS,
    )
    if not ok or not user:
        raise HTTPException(status_code=403, detail="جلسة غير صالحة، افتح صفحة الرفع من البوت من جديد.")

    user_id = user.get("id")
    await _enforce_audio_upload_rate_limit(user_id, "audio_init")

    # 🆕 دفاع ثانٍ من طرف السيرفر - لا نثق بفحص الحجم من طرف المتصفح وحده
    if payload.file_size <= 0 or payload.file_size > MAX_AUDIO_WEB_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="حجم الملف يتجاوز الحد المسموح (250 ميغابايت).")
    ext = os.path.splitext(payload.file_name)[1] if payload.file_name else ""

    upload_target = await create_audio_upload_target(user_id, ext)
    if not upload_target or not upload_target.get("path"):
        raise HTTPException(status_code=500, detail="تعذر تجهيز جلسة الرفع، حاول مجدداً بعد قليل.")

    return {
        "upload_url": upload_target.get("upload_url"),
        "object_path": upload_target.get("path"),
    }


@app.post("/api/audio-upload/complete")
async def audio_upload_complete(payload: AudioUploadCompleteRequest):
    """
    يُستدعى من صفحة الويب فور اكتمال الرفع فعلياً على Supabase. يتحقق من initData
    مجدداً (نفس المستخدم)، ثم يُطلق معالجة الصوت بالخلفية فوراً (لا ننتظرها هون -
    الرد لازم يرجع بسرعة حتى تقفل صفحة الويب وترجع المستخدم للمحادثة، والمعالجة
    الفعلية بتظهر كرسائل متتالية من البوت نفسه).
    """
    ok, user = verify_telegram_init_data(
        payload.init_data,
        bot.token,
        max_age_seconds=AUDIO_UPLOAD_INIT_DATA_MAX_AGE_SECONDS,
    )
    if not ok or not user:
        raise HTTPException(status_code=403, detail="جلسة غير صالحة.")

    user_id = user.get("id")
    await _enforce_audio_upload_rate_limit(user_id, "audio_complete")

    # 🆕 تحقق أمني: المسار لازم يبدأ بمعرف نفس المستخدم (منع استخدام object_path لمستخدم آخر)
    if not payload.object_path.startswith(f"{user_id}/"):
        raise HTTPException(status_code=403, detail="غير مسموح.")

    # 🆕 دفاع أول (الأهم): تحقق من الحجم الفعلي للملف المرفوع فعلياً عبر TUS مباشرة
    # لـ Supabase (وليس فقط الحجم الذي صرّح به العميل بمرحلة /init، والذي لم يمر
    # أصلاً عبر سيرفرنا). يُرفض الملف ويُحذف فوراً لو تجاوز الحد أو تعذّر التحقق
    # من حجمه (متساهل=رفض، وليس العكس، لأننا لا نستطيع التأكد أنه آمن).
    actual_size = await get_audio_temp_object_size(payload.object_path)
    if actual_size is None or actual_size > MAX_AUDIO_WEB_UPLOAD_SIZE:
        await delete_audio_temp(payload.object_path)
        raise HTTPException(status_code=413, detail="حجم الملف المرفوع يتجاوز الحد المسموح أو تعذر التحقق منه.")

    asyncio.create_task(
        process_web_uploaded_audio(
            user_id=user_id,
            chat_id=user_id,  # محادثة خاصة - chat_id يساوي user_id بالبوتات الفردية
            object_path=payload.object_path,
            declared_file_name=payload.file_name,
        )
    )

    return {"ok": True}


# ==================== 🆕 File Web Upload (Telegram Mini App) ====================

class FileUploadInitRequest(BaseModel):
    init_data: str
    file_size: int
    file_name: str = ""


class FileUploadCompleteRequest(BaseModel):
    init_data: str
    object_path: str
    file_name: str = ""


@app.post("/api/file-upload/init")
async def file_upload_init(payload: FileUploadInitRequest):
    """نظير audio_upload_init لمستند (PDF/Word/PowerPoint/نص) بدل ملف صوتي - راجع
    توثيق audio_upload_init فوق لشرح كل خطوة، نفس المنطق تماماً بـ bucket مختلف."""
    ok, user = verify_telegram_init_data(
        payload.init_data, bot.token, max_age_seconds=FILE_UPLOAD_INIT_DATA_MAX_AGE_SECONDS,
    )
    if not ok or not user:
        raise HTTPException(status_code=403, detail="جلسة غير صالحة، افتح صفحة الرفع من البوت من جديد.")

    user_id = user.get("id")
    await _enforce_upload_rate_limit(user_id, "file_init", FILE_UPLOAD_RATE_LIMIT_MAX_REQUESTS, FILE_UPLOAD_RATE_LIMIT_WINDOW_SECONDS)

    if payload.file_size <= 0 or payload.file_size > MAX_FILE_WEB_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="حجم الملف يتجاوز الحد المسموح (100 ميغابايت).")
    ext = os.path.splitext(payload.file_name)[1] if payload.file_name else ""

    upload_target = await create_file_upload_target(user_id, ext)
    if not upload_target or not upload_target.get("path"):
        raise HTTPException(status_code=500, detail="تعذر تجهيز جلسة الرفع، حاول مجدداً بعد قليل.")

    return {
        "upload_url": upload_target.get("upload_url"),
        "object_path": upload_target.get("path"),
    }


@app.post("/api/file-upload/complete")
async def file_upload_complete(payload: FileUploadCompleteRequest):
    """نظير audio_upload_complete لمستند مرفوع - يُطلق process_web_uploaded_file بالخلفية."""
    ok, user = verify_telegram_init_data(
        payload.init_data, bot.token, max_age_seconds=FILE_UPLOAD_INIT_DATA_MAX_AGE_SECONDS,
    )
    if not ok or not user:
        raise HTTPException(status_code=403, detail="جلسة غير صالحة.")

    user_id = user.get("id")
    await _enforce_upload_rate_limit(user_id, "file_complete", FILE_UPLOAD_RATE_LIMIT_MAX_REQUESTS, FILE_UPLOAD_RATE_LIMIT_WINDOW_SECONDS)

    if not payload.object_path.startswith(f"{user_id}/"):
        raise HTTPException(status_code=403, detail="غير مسموح.")

    actual_size = await get_file_temp_object_size(payload.object_path)
    if actual_size is None or actual_size > MAX_FILE_WEB_UPLOAD_SIZE:
        await delete_file_temp(payload.object_path)
        raise HTTPException(status_code=413, detail="حجم الملف المرفوع يتجاوز الحد المسموح أو تعذر التحقق منه.")

    asyncio.create_task(
        process_web_uploaded_file(
            user_id=user_id,
            chat_id=user_id,
            object_path=payload.object_path,
            declared_file_name=payload.file_name,
        )
    )
    return {"ok": True}


# ==================== 🆕 Images Web Upload (ألبوم صور كبير عبر Mini App) ====================

class ImageUploadInitRequest(BaseModel):
    init_data: str
    file_sizes: List[int]
    file_names: List[str] = []


class ImageUploadCompleteRequest(BaseModel):
    init_data: str
    object_paths: List[str]


@app.post("/api/image-upload/init")
async def image_upload_init(payload: ImageUploadInitRequest):
    """
    🆕 يولّد دفعة روابط رفع موقّعة سوا لكل صور الألبوم بنداء واحد (بدل نداء منفصل لكل
    صورة) - كل الصور تُخزَّن تحت نفس مجلد الجلسة (راجع create_image_upload_targets).
    """
    ok, user = verify_telegram_init_data(
        payload.init_data, bot.token, max_age_seconds=FILE_UPLOAD_INIT_DATA_MAX_AGE_SECONDS,
    )
    if not ok or not user:
        raise HTTPException(status_code=403, detail="جلسة غير صالحة، افتح صفحة الرفع من البوت من جديد.")

    user_id = user.get("id")
    await _enforce_upload_rate_limit(user_id, "images_init", FILE_UPLOAD_RATE_LIMIT_MAX_REQUESTS, FILE_UPLOAD_RATE_LIMIT_WINDOW_SECONDS)

    count = len(payload.file_sizes)
    if count <= 0 or count > MAX_IMAGE_WEB_UPLOAD_COUNT:
        raise HTTPException(status_code=413, detail=f"عدد الصور يجب أن يكون بين 1 و{MAX_IMAGE_WEB_UPLOAD_COUNT}.")
    if any(size <= 0 or size > MAX_IMAGE_WEB_UPLOAD_SIZE_PER_IMAGE for size in payload.file_sizes):
        raise HTTPException(status_code=413, detail="حجم إحدى الصور يتجاوز الحد المسموح لكل صورة (15 ميغابايت).")

    extensions = [
        os.path.splitext(payload.file_names[i])[1] if i < len(payload.file_names) and payload.file_names[i] else ".jpg"
        for i in range(count)
    ]
    targets = await create_image_upload_targets(user_id, extensions)
    if not targets:
        raise HTTPException(status_code=500, detail="تعذر تجهيز جلسة الرفع، حاول مجدداً بعد قليل.")

    return {
        "targets": [{"object_path": t.get("path"), "upload_url": t.get("upload_url")} for t in targets],
    }


@app.post("/api/image-upload/complete")
async def image_upload_complete(payload: ImageUploadCompleteRequest):
    """نظير file_upload_complete لكن لدفعة صور سوا - يتحقق من كل الحجوم فعلياً قبل
    الإطلاق، ويرفض الدفعة كاملة لو صورة واحدة فقط فشل التحقق منها (لا معالجة جزئية)."""
    ok, user = verify_telegram_init_data(
        payload.init_data, bot.token, max_age_seconds=FILE_UPLOAD_INIT_DATA_MAX_AGE_SECONDS,
    )
    if not ok or not user:
        raise HTTPException(status_code=403, detail="جلسة غير صالحة.")

    user_id = user.get("id")
    await _enforce_upload_rate_limit(user_id, "images_complete", FILE_UPLOAD_RATE_LIMIT_MAX_REQUESTS, FILE_UPLOAD_RATE_LIMIT_WINDOW_SECONDS)

    object_paths = payload.object_paths
    if not object_paths or len(object_paths) > MAX_IMAGE_WEB_UPLOAD_COUNT:
        raise HTTPException(status_code=413, detail=f"عدد الصور يجب أن يكون بين 1 و{MAX_IMAGE_WEB_UPLOAD_COUNT}.")
    if any(not path.startswith(f"{user_id}/") for path in object_paths):
        raise HTTPException(status_code=403, detail="غير مسموح.")

    for object_path in object_paths:
        actual_size = await get_file_temp_object_size(object_path)
        if actual_size is None or actual_size > MAX_IMAGE_WEB_UPLOAD_SIZE_PER_IMAGE:
            await delete_file_temp_batch(object_paths)
            raise HTTPException(status_code=413, detail="حجم إحدى الصور يتجاوز الحد المسموح أو تعذر التحقق منه.")

    asyncio.create_task(
        process_web_uploaded_images(
            user_id=user_id,
            chat_id=user_id,
            object_paths=object_paths,
        )
    )
    return {"ok": True}


# ==================== Run Server Function ====================

def run_webhook_server():
    import uvicorn
    port = int(os.getenv("PORT", WEBHOOK_PORT))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")