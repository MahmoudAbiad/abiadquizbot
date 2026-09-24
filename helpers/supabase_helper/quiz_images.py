"""
supabase_helper.quiz_images
=============================
Math Image Quiz Storage (نمط الكويز المصوّر LaTeX): رفع/تخزين صور الأسئلة
الرياضية وتحديث محتوى السؤال.
"""

import os
from typing import Optional, Dict, Any

from ._client import supabase, logger, log_error, log_warning, _is_valid_uuid

# ==================== Math Image Quiz Storage (نمط الكويز المصوّر LaTeX) ====================
# الباكت المخصص لتخزين صور الأسئلة الرياضية المُصاغة بـ LaTeX. يجب أن يكون
# عاماً (public) لأن bot.send_photo يستقبل رابطاً مباشراً بدل رفع الملف نفسه
# في كل مرة - راجع migration_math_image_quizzes.sql لإنشائه وصلاحياته.
QUIZ_IMAGES_BUCKET = "quiz-images"

async def upload_quiz_question_image(image_bytes: bytes, object_path: str) -> Optional[str]:
    """
    يرفع صورة سؤال رياضي مصوّر (LaTeX) إلى Supabase Storage ويرجع رابطها العام،
    ليُستخدم مباشرة مع bot.send_photo (يقبل Telegram روابط HTTP مباشرة). عند
    الفشل (مثال: الباكت غير موجود بعد) يعيد None ليتحول المتصل تلقائياً لإرسال
    الصورة كملف خام بدل رابط، دون كسر تجربة الطالب.
    """
    try:
        await supabase.storage.from_(QUIZ_IMAGES_BUCKET).upload(
            path=object_path,
            file=image_bytes,
            file_options={"content-type": "image/png", "upsert": "true"},
        )
        return await supabase.storage.from_(QUIZ_IMAGES_BUCKET).get_public_url(object_path)
    except Exception as e:
        log_warning(logger, f"Could not upload quiz question image to storage (falling back to raw bytes): {e}")
        return None

async def save_question_image_url(quiz_id: str, question_index: int, image_url: str) -> None:
    """
    يخزّن رابط الصورة المولّدة داخل عنصر السؤال المطابق ضمن quiz_data (JSONB)،
    بحيث لا يُعاد رسم/رفع نفس السؤال في كل مرة يُشغَّل فيها هذا الكويز المخزّن
    (كاش) من قبل نفس الطالب أو غيره من زملائه لاحقاً. عملية غير حرجة (fire-and-forget)
    تُستدعى بالخلفية ولا توقف تدفق الاختبار الجاري عند فشلها.
    """
    if not _is_valid_uuid(quiz_id):
        return
    try:
        res = await supabase.table("quizzes").select("quiz_data").eq("id", quiz_id).limit(1).execute()
        if not res.data:
            return
        quiz_data = res.data[0]["quiz_data"] or []
        if 0 <= question_index < len(quiz_data):
            quiz_data[question_index]["image_url"] = image_url
            await supabase.table("quizzes").update({"quiz_data": quiz_data}).eq("id", quiz_id).execute()
    except Exception as e:
        log_warning(logger, f"Could not cache question image URL for quiz {quiz_id}: {e}")


async def get_quiz_creator_id(quiz_id: str) -> Optional[int]:
    """جلب creator_id فقط (بدون quiz_data الثقيل) لكويز معيّن - يُستخدم لفحص صلاحية
    المالك/الأدمن مبكراً (مثلاً بمحرر أسئلة الرياضيات عبر الويب - راجع
    handlers/quiz_runner.py::fetch_question_for_edit_web/save_question_edit_from_web)
    قبل أي معالجة إضافية، بدل الاكتفاء بالفحص المتأخر داخل update_quiz_question."""
    if not _is_valid_uuid(quiz_id):
        return None
    try:
        res = await supabase.table("quizzes").select("creator_id").eq("id", quiz_id).limit(1).execute()
        return res.data[0].get("creator_id") if res.data else None
    except Exception as e:
        log_error(logger, f"Error fetching creator_id for quiz {quiz_id}: {e}")
        return None


async def update_quiz_question(
    quiz_id: str, question_index: int, question: Dict[str, Any], editor_id: int
) -> Optional[bool]:
    """تحديث سؤال بعد التحقق من أن المحرر هو المالك أو الأدمن.

    تعيد True عند النجاح، وFalse عند فشل التحديث، وNone عند رفض الصلاحية.
    """
    if not _is_valid_uuid(quiz_id):
        return None
    try:
        res = await supabase.table("quizzes").select("quiz_data, creator_id").eq("id", quiz_id).limit(1).execute()
        if not res.data:
            return False
        creator_id = res.data[0].get("creator_id")
        admin_id = os.getenv("ADMIN_ID", "0")
        if str(editor_id) != str(creator_id) and str(editor_id) != admin_id:
            log_warning(logger, f"Rejected quiz edit by user {editor_id} for quiz {quiz_id}")
            return None
        quiz_data = res.data[0].get("quiz_data") or []
        if not 0 <= question_index < len(quiz_data):
            return False
        normalized_question = dict(question)
        for stale_key in ("image_url", "rendered_image_url", "cached_image_url"):
            normalized_question.pop(stale_key, None)
        if "table" in normalized_question and not normalized_question.get("table"):
            normalized_question.pop("table", None)
        if "matrices" in normalized_question and normalized_question.get("matrices") is None:
            normalized_question["matrices"] = []
        quiz_data[question_index] = normalized_question
        await supabase.table("quizzes").update({"quiz_data": quiz_data}).eq("id", quiz_id).execute()
        return True
    except Exception as e:
        log_error(logger, f"Error updating question {question_index} in quiz {quiz_id}: {e}")
        return False

