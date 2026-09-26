# services/audio_flow_service.py
"""
==============================================================================
MODULE: Audio Flow Business Logic Service
==============================================================================
الوصف:
يجمع منطق الأعمال المشترك بين مساري رفع المحاضرة الصوتية الاثنين بـ
handlers/audio.py:
1) المسار المباشر عبر رسالة تيليجرام (handle_audio_message).
2) مسار الرفع عبر صفحة الويب / Mini App (process_web_uploaded_audio).

قبل هذا الفصل، كان أربع خطوات متطابقة تقريباً حرفياً مكررة بين المسارين:
قياس المدة الفعلية -> حساب الدقائق -> التحقق من السقف الأقصى -> حساب التكلفة،
بالإضافة لتكرار حساب "الرصيد/العجز" ونص رسالة الرصيد غير الكافي، وتكرار كتابة
ملف txt مؤقت (نص مفرّغ/ملخص) عند تجاوز الطول الآمن لرسالة تيليجرام.

evaluate_audio_cost() هي الدالة المحورية هنا: توحّد الخطوات الأربع أعلاه في
استدعاء واحد يستخدمه كلا المسارين، فيصبح تصحيح/تعديل منطق التسعير أو القياس
مستقبلاً بمكان واحد بدل مكانين قد ينحرفا عن بعضهما بمرور الوقت.

DOWNLOADS_DIR مُعرَّف هنا كمصدر وحيد للحقيقة (Single Source of Truth) لأنه
يُستخدم من كل من طبقة الـ Handler (تحميل الصوت من تيليجرام/الويب) وهذه
الخدمة (كتابة نصوص التصدير المؤقتة) - handlers/audio.py يستورده من هنا بدل
تعريفه محلياً مرة أخرى.
==============================================================================
"""

import os
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from constants import MAX_AUDIO_DURATION_MINUTES
from helpers.points_calculator import calculate_audio_transcription_cost
from services.audio_duration_service import probe_actual_duration_seconds
from supabase_helper import check_or_add_user, log_usage_event
from utils import ensure_directory_exists

DOWNLOADS_DIR = "downloads"


# ==================== المستخدم ====================

async def get_or_create_audio_user(user: Any) -> Dict[str, Any]:
    """يجلب/ينشئ سجل المستخدم بنفس منطق check_or_add_user المستخدم بباقي المشروع.
    يقبل أي كائن يحمل id/username/first_name/last_name (message.from_user،
    call.from_user، أو كائن مُصطنَع لمستخدم رفع الويب - راجع process_web_uploaded_audio)."""
    return await check_or_add_user(
        user.id,
        user.username or "Unknown",
        user.first_name or "Unknown",
        user.last_name or "Unknown",
    )


# ==================== قياس المدة + التسعير الموحّد ====================

@dataclass
class AudioCostResult:
    """نتيجة موحّدة لتقييم مدة/تكلفة محاضرة صوتية، تُستخدم من كلا مساري الرفع."""
    probed_seconds: Optional[int]   # المدة المقيسة فعلياً من الملف نفسه (None لو تعذّر القياس تماماً)
    duration_seconds: int           # المدة النهائية المعتمدة بعد دمج المُعلَنة/المقيسة
    duration_minutes: int           # نفس المدة مُقرَّبة لأعلى لأقرب دقيقة (حد أدنى دقيقة واحدة)
    cost: float                     # التكلفة بالنقاط (0.0 لو exceeds_cap=True، لم تُحسب أصلاً)
    exceeds_cap: bool                # هل تجاوزت المدة MAX_AUDIO_DURATION_MINUTES


async def evaluate_audio_cost(file_path: str, reported_duration_seconds: int = 0) -> AudioCostResult:
    """🆕 يوحّد الخطوات الأربع المكررة سابقاً بين handle_audio_message وprocess_web_uploaded_audio:
    قياس المدة الفعلية -> دمجها مع المدة المُعلَنة (إن وُجدت) -> حساب الدقائق -> التحقق من
    السقف الأقصى -> حساب التكلفة.

    reported_duration_seconds: المدة التي أرسلها تيليجرام مع الرسالة (message.voice.duration
    أو message.audio.duration). تُترك 0 بمسار رفع الويب حيث لا توجد قيمة مُعلَنة من تيليجرام
    أصلاً - عندها المعادلة تعتمد كلياً على القياس الفعلي (نفس ناتج duration_seconds =
    actual_duration_seconds or 0 بالكود الأصلي لـ process_web_uploaded_audio).

    التسعير النهائي دائماً من القيمة الأكبر بين المُعلنة والفعلية تفادياً لأي دفع أقل من
    التكلفة الحقيقية (نفس المنطق الأصلي بالمسار المباشر)."""
    probed_seconds = await probe_actual_duration_seconds(file_path)
    duration_seconds = max(reported_duration_seconds, probed_seconds or 0) or reported_duration_seconds
    duration_minutes = max(1, (duration_seconds + 59) // 60)
    exceeds_cap = duration_minutes > MAX_AUDIO_DURATION_MINUTES
    cost = 0.0 if exceeds_cap else calculate_audio_transcription_cost(duration_minutes)
    return AudioCostResult(
        probed_seconds=probed_seconds,
        duration_seconds=duration_seconds,
        duration_minutes=duration_minutes,
        cost=cost,
        exceeds_cap=exceeds_cap,
    )


# ==================== الرصيد والعجز ====================

def compute_balance_and_deficit(user_info: Dict[str, Any], required: float) -> Tuple[float, float]:
    """يحسب الرصيد الإجمالي الحالي (مجاني + مدفوع) والعجز المطلوب شحنه لتغطية التكلفة."""
    balance = float(user_info.get("free_points") or 0) + float(user_info.get("paid_points") or 0)
    deficit = max(0.0, required - balance)
    return balance, deficit


def build_insufficient_balance_text(user_info: Dict[str, Any], required: float) -> str:
    """نص رسالة "الرصيد لا يكفي" الموحّد لتفريغ محاضرة صوتية - نفس النص المُستخدم سابقاً
    بدالة _insufficient_balance بالمسار المباشر، أصبح الآن قابلاً لإعادة الاستخدام بمسار
    الويب أيضاً دون تكرار."""
    balance, deficit = compute_balance_and_deficit(user_info, required)
    return (
        "❌ <b>رصيدك الحالي لا يكفي لتفريغ وتحليل هذه المحاضرة الصوتية.</b>\n\n"
        f"🎁 المجاني: <code>{float(user_info.get('free_points') or 0):.2f}</code>\n"
        f"💳 المدفوع: <code>{float(user_info.get('paid_points') or 0):.2f}</code>\n"
        f"💰 الإجمالي الحالي: <code>{balance:.2f}</code> / المطلوب: <code>{required:.2f}</code>\n"
        f"⚠️ العجز المطلوب شحنه: <b>{deficit:.2f} نقطة</b>"
    )


async def log_insufficient_balance_event(
    user_id: int, action: str, required: float, balance: float, user_info: Dict[str, Any]
) -> None:
    """🆕 تتبع تحليلي موحّد لحدث "insufficient_balance_blocked" - كان مكرراً بصياغتين
    شبه متطابقتين بين المسار المباشر ومسار رفع الويب. يُستدعى دائماً عبر
    asyncio.create_task(...) من طرف المستدعي (Fire-and-forget) للحفاظ على نفس السلوك
    الأصلي بعدم انتظار اكتمال التسجيل قبل إتمام الرد على الطالب."""
    deficit = max(0.0, required - balance)
    await log_usage_event(user_id, "insufficient_balance_blocked", {
        "action": action, "required_points": required, "current_balance": balance,
        "deficit_points": deficit, "free_points": float(user_info.get("free_points") or 0),
        "paid_points": float(user_info.get("paid_points") or 0),
    })


async def log_audio_uploaded_event(user_id: int, duration_minutes: int, cost: float, source: str) -> None:
    """🆕 تتبع تحليلي موحّد لحدث "audio_uploaded" - كان مكرراً حرفياً تقريباً بين المسار
    المباشر (source="telegram_message") ومسار رفع الويب (source="web_upload"). يُسجَّل
    بمجرد معرفة مدة/تكلفة الملف، بغض النظر عن قرار الطالب لاحقاً بشاشة التأكيد. يُستدعى
    دائماً عبر asyncio.create_task(...) من طرف المستدعي (Fire-and-forget)."""
    await log_usage_event(user_id, "audio_uploaded", {
        "duration_minutes": duration_minutes, "cost": cost, "source": source,
    })


# ==================== استرجاع جزئي (نجاح جزئي بالتفريغ) ====================

def compute_partial_refund(debited_free: float, debited_paid: float, ratio: float = 0.5) -> Tuple[float, float, float]:
    """🆕 [طبقة 3] يحسب الاسترجاع الجزئي عند انقطاع التفريغ قبل نهاية المحاضرة (نجاح جزئي)،
    بتطبيق نفس النسبة (افتراضياً 50%) على شقّي الرصيد (المجاني والمدفوع) كلٍّ على حدة -
    بدل حساب المبلغ من التكلفة الإجمالية وإرجاعه كله لـ paid_points - حتى يبقى التوزيع
    النسبي بين الرصيدين صحيحاً بعد الاسترجاع الجزئي. يُرجع (free_refund, paid_refund, total)."""
    free_refund = round(debited_free * ratio, 2)
    paid_refund = round(debited_paid * ratio, 2)
    return free_refund, paid_refund, free_refund + paid_refund


# ==================== تصدير نص مؤقت (txt) ====================

def write_temp_text_export(prefix: str, user_id: int, content: str) -> str:
    """🆕 يوحّد كتابة ملف txt مؤقت (نص مفرّغ كامل أو ملخص أكاديمي) عند تجاوز الطول الآمن
    لرسالة تيليجرام (MAX_INLINE_TEXT_CHARS) - كان هذا النمط مكرراً حرفياً بين
    handle_audio_send_text وhandle_audio_summarize بـ handlers/audio.py. المستدعي
    مسؤول عن حذف الملف بعد الإرسال (نفس مسؤولية safe_file_cleanup الأصلية)."""
    ensure_directory_exists(DOWNLOADS_DIR)
    path = os.path.join(DOWNLOADS_DIR, f"{prefix}_{user_id}_{uuid.uuid4().hex}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path
