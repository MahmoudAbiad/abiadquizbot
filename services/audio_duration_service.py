# services/audio_duration_service.py
"""
==============================================================================
MODULE: Audio Duration Probing Service
==============================================================================
الوصف:
خدمة مسؤولة حصراً عن قياس المدة الفعلية لملف صوتي بعد تحميله محلياً على القرص.
لا يوجد بهذا الملف أي استيراد لـ aiogram أو أي كائن Telegram (Message/CallbackQuery) -
كل الدوال هنا منطق تقني/أعمال بحت (I/O على القرص + عمليات subprocess)، قابل للاختبار
واستدعاء بشكل مستقل عن طبقة الـ Handlers.

نُقل هذا المنطق بالكامل من handlers/audio.py دون أي تغيير سلوكي - نفس ترتيب
المحاولات (mutagen -> ffprobe -> ffmpeg stderr)، نفس معالجة الأخطاء الصامتة،
ونفس آلية التقريب لأعلى لأقرب ثانية.

سلسلة القياس (بالترتيب، كل واحدة fallback للي قبلها):
1) mutagen (ترويسة الملف) - الأسرع والأدق لملفات سليمة الترويسة.
2) ffprobe (إذا كان متوفراً على PATH بالنظام) - يمسح الملف كاملاً، يفيد بالملفات
   المجزّأة (fragmented mp4/m4a) التي تفشل بها mutagen غالباً.
3) ffmpeg الثنائي الساكن من حزمة imageio-ffmpeg (لا يعتمد على تثبيت نظامي، يعمل
   على Heroku خلافاً لـ ffprobe) - نقرأ سطر Duration من stderr يدوياً لأن
   ffprobe نفسه غير مرفق مع هذه الحزمة.
==============================================================================
"""

import asyncio
import json
import re
import shutil
import subprocess
from typing import Optional

from mutagen import File as MutagenFile

from constants import MAX_AUDIO_DURATION_MINUTES


def resolve_ffprobe_path() -> Optional[str]:
    """🩹 يحدد مسار ffprobe القابل للاستخدام فعلياً بالبيئة الحالية:
    1) ffprobe من PATH مباشرة (إذا كانت البيئة تحتوي ffmpeg مثبت عبر النظام - حالة Docker
       لو أُضيف لاحقاً apt-get install ffmpeg).
    2) الثنائي الساكن (static binary) المرفق مع حزمة imageio-ffmpeg (مضافة لـ requirements.txt
       خصيصاً لهذا السبب) - يعمل بلا أي اعتماد على النظام، وهو الحل الفعلي المطلوب لأن
       عملية النشر الحالية على Heroku (Procfile + heroku/python buildpack) لا تملك ffmpeg
       مثبتاً على مستوى النظام إطلاقاً، وDockerfile الموجود بالمشروع لا يُثبّته أيضاً - فكان
       الفشل هون صامتاً ودائماً 100% من المرات رغم وجود كود fallback يبدو صحيحاً.
    ffprobe نفسه غير مرفق مع imageio-ffmpeg (فقط ffmpeg)، فنعتمد بالحالة الثانية على ffmpeg
    نفسه لقراءة المدة (عبر stderr) بدل ffprobe - راجع probe_duration_ffmpeg_stderr_sync."""
    system_path = shutil.which("ffprobe")
    if system_path:
        return system_path
    return None


def probe_duration_ffprobe_sync(file_path: str) -> Optional[int]:
    """🩹 Fallback ثانٍ لما mutagen يفشل بقراءة المدة - صار يحصل بشكل متكرر تحديداً لملفات
    m4a المرفوعة عبر صفحة الويب (audio_web_*). السبب الأرجح: بعض متصفحات/أجهزة المستخدمين
    تسجّل عبر MediaRecorder وتنتج mp4/m4a بدون moov atom بأول الملف (fragmented/streamed)،
    وmutagen ما بيقدر يلاقي ترويسة المدة بهالحالة رغم إنو الملف سليم ومشغَّل تماماً. ffprobe
    أقوى بكتير بقراءة هيك ملفات لأنه بيقدر يمسح الملف كامل مش يعتمد فقط عالترويسة المتوقعة
    بأول الملف. يُرجع None لو ffprobe نفسه غير متوفر أو فشل (عندها نجرب ffmpeg الساكن)."""
    ffprobe_path = resolve_ffprobe_path()
    if not ffprobe_path:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe_path, "-v", "error", "-show_entries", "format=duration",
                "-of", "json", file_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        duration = float(json.loads(result.stdout)["format"]["duration"])
        if duration > 0:
            return int(duration + 0.999)  # تقريب لأعلى لأقرب ثانية
    except Exception:  # أي فشل هون (timeout، ملف تالف فعلياً...) غير حرج
        pass
    return None


def probe_duration_ffmpeg_stderr_sync(file_path: str) -> Optional[int]:
    """🩹 Fallback ثالث وأخير: يستعمل الثنائي الساكن لـ ffmpeg (imageio_ffmpeg.get_ffmpeg_exe -
    يُنزَّل تلقائياً مرة واحدة مع pip install، ويعمل بلا أي اعتماد على apt/النظام، وبالتالي
    يشتغل فعلياً على Heroku خلافاً لـ ffprobe). ffmpeg بدون ffprobe ما بيرجّع JSON منظم،
    فنقرأ سطر 'Duration: HH:MM:SS.xx' من stderr يدوياً."""
    try:
        import imageio_ffmpeg
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None
    try:
        result = subprocess.run(
            [ffmpeg_path, "-i", file_path, "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
        )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", result.stderr or "")
        if not match:
            return None
        hours, minutes, seconds = match.groups()
        total_seconds = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if total_seconds > 0:
            return int(total_seconds + 0.999)  # تقريب لأعلى لأقرب ثانية
    except Exception:  # أي فشل هون (timeout، ملف تالف فعلياً...) غير حرج
        pass
    return None


def probe_actual_duration_seconds_sync(file_path: str) -> Optional[int]:
    """يقرأ المدة الفعلية للملف الصوتي بعد تحميله محلياً (من ترويسة الملف نفسها،
    وليس من الـ metadata التي أرسلها تطبيق تيليجرام للعميل مع الرسالة). مهم لأن
    `message.audio.duration` (خلافاً لـ `message.voice.duration` الذي يقيسه تيليجرام
    وقت التسجيل) قيمة يُرسلها العميل مع الملف وقد لا تطابق الملف الفعلي (بالخطأ أو
    عمداً)، بينما التكلفة الحقيقية عند Gemini مرتبطة بطول الصوت الفعلي لا المُعلَن.
    يُرجع None إن تعذّرت القراءة تماماً (لا mutagen ولا ffprobe ولا ffmpeg قدروا) - عندها
    نرجع للاعتماد على مدة تيليجرام المُعلَنة (أو دقيقة واحدة كحد أدنى) كحل احتياطي أخير."""
    try:
        audio = MutagenFile(file_path)
        if audio is not None and audio.info is not None and audio.info.length:
            return int(audio.info.length + 0.999)  # تقريب لأعلى لأقرب ثانية
    except Exception:  # أي فشل هون (صيغة غير مدعومة، ملف تالف...) غير حرج، نجرب ffprobe/ffmpeg بعدها
        pass
    return probe_duration_ffprobe_sync(file_path) or probe_duration_ffmpeg_stderr_sync(file_path)


async def probe_actual_duration_seconds(file_path: str) -> Optional[int]:
    """🆕 نسخة async من probe_actual_duration_seconds_sync، تغلّف asyncio.to_thread داخلياً
    كي لا يُضطر كل Handler لتكرار asyncio.to_thread(probe_actual_duration_seconds_sync, ...)
    بنفسه - كانت هذه العبارة مكررة حرفياً بين المسار المباشر (handle_audio_message) ومسار
    الرفع عبر الويب (process_web_uploaded_audio) بـ handlers/audio.py."""
    return await asyncio.to_thread(probe_actual_duration_seconds_sync, file_path)


def format_duration_cap_exceeded_text(duration_minutes: int) -> str:
    """🆕 رسالة موحّدة عند تجاوز مدة المحاضرة للحد الأقصى (MAX_AUDIO_DURATION_MINUTES) -
    مشتركة بين مسار تيليجرام المباشر ومسار رفع الويب."""
    return (
        f"❌ مدة هذه المحاضرة ({duration_minutes} دقيقة) تتجاوز الحد الأقصى المسموح به حالياً "
        f"({MAX_AUDIO_DURATION_MINUTES // 60} ساعات لكل ملف واحد). يرجى تقسيم التسجيل إلى "
        "أجزاء أصغر وإرسال كل جزء على حدة."
    )
