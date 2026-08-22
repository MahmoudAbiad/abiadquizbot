"""
Webhook configuration and FastAPI setup for Azure/Railway deployment.
Handles HTTP server setup safely with modern lifespan context and proper Pydantic validation.
"""

import os
import asyncio  
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from contextlib import asynccontextmanager
from aiogram.types import Update
from config import bot, dp, set_bot_commands 
from logger import get_logger
from constants import (
    WEBHOOK_PATH, WEBHOOK_PORT, TELEGRAM_WEBHOOK_SECRET,
    MAX_AUDIO_WEB_UPLOAD_SIZE, AUDIO_UPLOAD_BUCKET, AUDIO_UPLOAD_INIT_DATA_MAX_AGE_SECONDS,
)
from telegram_webapp_auth import verify_telegram_init_data

# 🆕 استيراد دوال الدُفعات والتنظيف الدوري الاسم الصحيح المُعدّل في supabase_helper
# SUPABASE_URL مستوردة هون بنفس أسلوب المشروع (بدون بادئة helpers.) لبناء رابط TUS
from supabase_helper import (
    flush_analytics_queue, auto_cleanup_old_analytics_data,
    create_audio_upload_target, cleanup_stale_audio_uploads, SUPABASE_URL,
)
from handlers.audio import process_web_uploaded_audio

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
            # 🆕 شبكة أمان: حذف أي ملفات صوتية مؤقتة تبقّت بـ Supabase Storage
            # لأكثر من ساعة (معالجة انقطعت استثنائياً قبل الوصول لـ finally)
            deleted = await cleanup_stale_audio_uploads(older_than_seconds=3600)
            if deleted:
                logger.info(f"Cleaned up {deleted} stale audio-temp file(s) from Supabase Storage.")
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
    يتحقق من initData ومن حجم الملف المُعلَن، ثم يولّد جلسة رفع TUS موقّعة على
    Supabase Storage. الرفع الفعلي بعدها يصير مباشرة من متصفح المستخدم لـ Supabase
    (مو عبر Heroku) - حد الـ 30 ثانية لـ Heroku router مو مشكلة هون.
    """
    ok, user = verify_telegram_init_data(
        payload.init_data,
        bot.token,
        max_age_seconds=AUDIO_UPLOAD_INIT_DATA_MAX_AGE_SECONDS,
    )
    if not ok or not user:
        raise HTTPException(status_code=403, detail="جلسة غير صالحة، افتح صفحة الرفع من البوت من جديد.")

    # 🆕 دفاع ثانٍ من طرف السيرفر - لا نثق بفحص الحجم من طرف المتصفح وحده
    if payload.file_size <= 0 or payload.file_size > MAX_AUDIO_WEB_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="حجم الملف يتجاوز الحد المسموح (250 ميغابايت).")

    user_id = user.get("id")
    ext = os.path.splitext(payload.file_name)[1] if payload.file_name else ""

    upload_target = await create_audio_upload_target(user_id, ext)
    if not upload_target or not upload_target.get("path"):
        raise HTTPException(status_code=500, detail="تعذر تجهيز جلسة الرفع، حاول مجدداً بعد قليل.")

    # 🆕 إصلاح: مسار الرفع المرن (TUS) لازم ينتهي بـ /sign حتى يفعّل السيرفر مسار
    # التحقق عبر x-signature (التوكن الموقّع) بدل التحقق العادي بـ JWT - بدون هالـ
    # لاحقة، الـ x-signature header يُتجاهل تماماً ويفشل الطلب فوراً بـ 400.
    tus_endpoint = f"{SUPABASE_URL}/storage/v1/upload/resumable/sign"

    return {
        "upload_endpoint": tus_endpoint,
        "token": upload_target.get("token"),
        "object_path": upload_target.get("path"),
        "bucket": AUDIO_UPLOAD_BUCKET,
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

    # 🆕 تحقق أمني: المسار لازم يبدأ بمعرف نفس المستخدم (منع استخدام object_path لمستخدم آخر)
    if not payload.object_path.startswith(f"{user_id}/"):
        raise HTTPException(status_code=403, detail="غير مسموح.")

    asyncio.create_task(
        process_web_uploaded_audio(
            user_id=user_id,
            chat_id=user_id,  # محادثة خاصة - chat_id يساوي user_id بالبوتات الفردية
            object_path=payload.object_path,
            declared_file_name=payload.file_name,
        )
    )

    return {"ok": True}


# ==================== Run Server Function ====================

def run_webhook_server():
    import uvicorn
    port = int(os.getenv("PORT", WEBHOOK_PORT))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")