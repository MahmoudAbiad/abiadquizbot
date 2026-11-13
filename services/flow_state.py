"""
services/flow_state.py
==============================================================================
بناء FSMContext يدوياً لمستخدم/محادثة معيّنة خارج سياق رسالة تيليجرام عادية
(مثلاً من endpoint استقبال ويب في webhook_server.py، أو لاستئناف كويز من خلفية)،
بالإضافة إلى أدوات فحص وتصفير جميع العمليات والأقفال النشطة للمستخدم في Redis.

يستخدم نفس RedisStorage المُهيّأ بـ config.py (dp.storage)، فالحالة متوافقة 100%
مع بقية تدفقات البوت العادية.
==============================================================================
"""

from typing import Any, Dict
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from config import bot, dp, redis_client
from logger import get_logger

logger = get_logger(__name__)

# ترجمة عربية واضحة لأهم حالات FSM التي قد يعلق بها الطالب
_STATE_LABELS = {
    "QuizState:processing_file_quiz": "⏳ توليد كويز من ملف/صور (قفل معالجة)",
    "QuizState:processing_web_file": "⏳ معالجة ملف/ألبوم مرفوع عبر الويب",
    "QuizState:processing_audio": "⏳ معالجة محاضرة صوتية (قفل صوت)",
    "QuizState:waiting_for_audio_confirm": "🎙 بانتظار تأكيد خصم نقاط الصوت",
    "QuizState:waiting_for_audio_action": "🎙 بانتظار اختيار نوع مخرجات الصوت",
    "QuizState:waiting_for_count": "🔢 بانتظار اختيار عدد الأسئلة",
    "QuizState:waiting_for_quiz_options": "⚙️ بانتظار خيارات نوع/صعوبة الأسئلة",
    "QuizState:waiting_for_quiz_extraction_choice": "📄 بانتظار اختيار طريقة استخراج الأسئلة",
    "QuizState:waiting_for_cache_decision": "♻️ بانتظار قرار استخدام الكاش أو توليد جديد",
    "QuizState:answering_quiz": "📝 داخل جلسة حل اختبار نشط",
    "QuizState:waiting_for_custom_name": "🏷 بانتظار تسمية الكويز لحفظه بالمفضلة",
    "QuizState:waiting_for_new_section_title": "📁 بانتظار اسم قسم مفضلة جديد",
    "QuizState:waiting_for_question_edit_choice": "✏️ تعديل سؤال (اختيار السؤال)",
    "QuizState:waiting_for_question_edit_text": "✏️ تعديل نص السؤال",
    "QuizState:waiting_for_answer_edit_choice": "✏️ تعديل الخيارات (اختيار الجواب)",
    "QuizState:waiting_for_answer_edit_text": "✏️ تعديل نص الجواب",
    "QuizState:searching_favorites": "🔍 بحث داخل المفضلة",
}


def format_fsm_state_label(state_str: Any) -> str:
    if not state_str:
        return "لا توجد حالة معلّقة"
    s = str(state_str)
    return _STATE_LABELS.get(s, f"حالة نشطة ({s})")


def build_state_for_chat(chat_id: int, user_id: int) -> FSMContext:
    """
    يبني FSMContext يدوياً لمستخدم/محادثة معيّنة (نفس منطق الدالة الأصلية بلا أي تغيير).
    بعد ما تخلص الدالة المستدعية، أي زر يضغطه الطالب من نفس اللوحة الحالية بيشتغل
    عادي بدون أي تعديل عليه.
    """
    key = StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=user_id)
    return FSMContext(storage=dp.storage, key=key)


async def get_user_redis_active_summary(user_id: int) -> Dict[str, Any]:
    """يرجع لمحة سريعة عن حالة الطالب الحالية في Redis (FSM + قفل المعالجة + انتقال الجلسة)."""
    uid = int(user_id)
    fsm_state = None
    has_processing_lock = False
    has_pending_navigation = False
    try:
        state = build_state_for_chat(uid, uid)
        fsm_state = await state.get_state()
    except Exception as exc:
        logger.warning("Could not inspect FSM state for user %s: %s", uid, exc)

    try:
        has_processing_lock = bool(await redis_client.exists(f"processing:user:{uid}"))
    except Exception:
        pass

    try:
        has_pending_navigation = bool(await redis_client.exists(f"session_navigation:pending:{uid}"))
    except Exception:
        pass

    return {
        "fsm_state": fsm_state,
        "fsm_label": format_fsm_state_label(fsm_state),
        "has_processing_lock": has_processing_lock,
        "has_pending_navigation": has_pending_navigation,
        "is_busy": bool(fsm_state or has_processing_lock or has_pending_navigation),
    }


async def reset_user_redis_operations(user_id: int, *, include_consent_cache: bool = False) -> Dict[str, Any]:
    """
    يصفّر كافة العمليات، الأقفال، وحالات الجلسات النشطة للمستخدم في Redis:
      1) مسح حالة وبيانات FSM (مع إغلاق أي محاولة كويز مفتوحة بقاعدة البيانات بشكل سليم).
      2) تحرير قفل المعالجة الموزع `processing:user:{uid}` (مما يلغي أيضاً أي مهمة معلّقة بالخلفية عبر نبضة UserLock).
      3) مسح انتقالات الجلسات المعلقة `session_navigation:pending:{uid}` وتنظيف أي ملفات R2 مؤقتة مرتبطة بها.
      4) مسح مفاتيح الانتظار المؤقتة للكويزات الجماعية والشهادات وبوابة الموافقة وحدود الرفع (Rate Limits).
      5) اختيارياً (عند حذف الموافقة أو حذف الحساب): مسح كاش الموافقة `consent:ok:{uid}:*`.
    """
    uid = int(user_id)
    prev_state = None
    stopped_attempt_id = None
    had_processing_lock = False
    had_pending_navigation = False
    deleted_keys = 0

    # 1. تفريغ FSMContext وإغلاق أي محاولة اختبار جارية
    try:
        state = build_state_for_chat(uid, uid)
        prev_state = await state.get_state()
        prev_data = await state.get_data()
        if isinstance(prev_data, dict) and prev_data.get("attempt_id") and not prev_data.get("quiz_completed"):
            stopped_attempt_id = prev_data.get("attempt_id")
            try:
                from helpers.supabase_helper import mark_quiz_attempt_stopped
                await mark_quiz_attempt_stopped(stopped_attempt_id)
            except Exception as exc:
                logger.warning("Could not mark quiz attempt stopped during Redis reset for %s: %s", uid, exc)
        await state.clear()
        if prev_state or prev_data:
            deleted_keys += 1
    except Exception as exc:
        logger.warning("Could not clear FSM state for user %s: %s", uid, exc)

    # 2. مسح انتقال الجلسة المعلّق وتنظيف أي مرفقات مؤقتة على R2
    try:
        from handlers.session_navigation import read_pending, discard_pending
        pending = await read_pending(uid)
        if pending:
            had_pending_navigation = True
            deleted_keys += 1
        await discard_pending(uid, cleanup=True)
    except Exception as exc:
        logger.warning("Could not discard pending session navigation for user %s: %s", uid, exc)

    # 3. تحرير قفل المعالجة الموزع ومفاتيح الانتظار المباشرة
    try:
        lock_key = f"processing:user:{uid}"
        if await redis_client.delete(lock_key):
            had_processing_lock = True
            deleted_keys += 1

        direct_keys = [
            f"gq:pending_channel:{uid}",
            f"gq:cert_text_wait:{uid}",
            f"gq:cert_preview:{uid}",
            f"gq:ceremony_preview:{uid}",
            f"consent:lock:{uid}",
            f"consent:pending:{uid}",
            f"consent:pending_cb:{uid}",
            f"consent:resume:{uid}",
        ]
        deleted_keys += int(await redis_client.delete(*direct_keys) or 0)

        # مسح أي مفاتيح FSM إضافية أو حدود معدل رفع (rate limits) مرتبطة بالمستخدم
        for pattern in (f"fsm:*:{uid}:*", f"upload_rl:*:{uid}", f"runner:rate:{uid}:*"):
            try:
                batch = []
                async for k in redis_client.scan_iter(match=pattern, count=100):
                    batch.append(k)
                if batch:
                    deleted_keys += int(await redis_client.delete(*batch) or 0)
            except Exception:
                pass
    except Exception as exc:
        logger.warning("Error deleting Redis keys for user %s: %s", uid, exc)

    # 4. مسح كاش الموافقة إذا طُلب ذلك (عند حذف الموافقة أو حذف المستخدم)
    if include_consent_cache:
        try:
            from services.consent import purge_user_consent_cache
            deleted_keys += await purge_user_consent_cache(uid)
        except Exception as exc:
            logger.warning("Could not purge consent cache for user %s: %s", uid, exc)

    return {
        "prev_state": prev_state,
        "prev_state_label": format_fsm_state_label(prev_state),
        "had_processing_lock": had_processing_lock,
        "had_pending_navigation": had_pending_navigation,
        "deleted_keys": deleted_keys,
        "stopped_attempt_id": stopped_attempt_id,
    }
