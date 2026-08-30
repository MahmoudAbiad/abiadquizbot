"""
Supabase database operations for user management and statistics.
Handles user registration, points management, database queries, centralized quiz caching, and community ratings.
"""

import asyncio
import os
import datetime
import uuid
import traceback
from typing import Optional, Dict, List, Any
from dotenv import load_dotenv, find_dotenv
from supabase import create_async_client
from logger import get_logger, log_error, log_warning, log_info
from constants import (
    DEFAULT_FAVORITE_SECTION_TITLE, MAX_FAVORITE_SECTIONS,
    SYRIA_TZ, to_syria_datetime, format_syria_time,
)
from validators import validate_user_id, validate_points_amount
from settings_helper import get_setting

dotenv_path = find_dotenv()
load_dotenv(dotenv_path)

logger = get_logger(__name__)


def _is_valid_uuid(value: Optional[str]) -> bool:
    """يتحقق أن القيمة UUID حقيقي وصالح قبل استخدامها في أعمدة uuid بقاعدة البيانات.
    يمنع تكرار خطأ 22P02 (invalid input syntax for type uuid) في حال تمرير معرف وهمي/ناقص."""
    if not value:
        return False
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _balance_payload(free_points: Any = 0, paid_points: Any = 0, **extra: Any) -> Dict[str, Any]:
    """Expose split balances while retaining ``points`` for older callers."""
    free = float(free_points or 0)
    paid = float(paid_points or 0)
    return {"free_points": free, "paid_points": paid, "points": free + paid, **extra}

# ==================== إعداد واقلاع عميل قاعدة البيانات بشكل آمن ====================
# 🆕 مخزّن كمتغير بمستوى الملف (وليس فقط داخل os.getenv أدناه) حتى يمكن استيراده
# من ملفات أخرى بنفس أسلوب الاستيراد المعتمد بباقي المشروع (بدون بادئة "helpers.").
SUPABASE_URL = os.getenv("SUPABASE_URL")

try:
    client_or_coro = create_async_client(SUPABASE_URL, os.getenv("SUPABASE_KEY"))
    
    if asyncio.iscoroutine(client_or_coro):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                supabase = loop.run_until_complete(client_or_coro)
            else:
                supabase = asyncio.run(client_or_coro)
        except RuntimeError:
            supabase = asyncio.run(client_or_coro)
    else:
        supabase = client_or_coro

    log_info(logger, "Supabase Async client initialized successfully with centralized schema mapping")
except Exception as e:
    log_error(logger, f"Failed to initialize Supabase Async: {e}", exception=e)
    raise
# ==================================================================================

# ==================== User Management ====================
async def check_or_add_user(user_id: int, username: str, first_name: str, last_name: str, referrer_id: Optional[int] = None) -> Dict[str, Any]:
    try:
        is_valid, error = validate_user_id(user_id)
        if not is_valid:
            return _balance_payload(status="error", referrer=None)
        
        today = datetime.date.today().isoformat()
        response = await supabase.table("users").select("*").eq("user_id", user_id).execute()
        
        if not response.data:
            return await _add_new_user(user_id, username, first_name, last_name, referrer_id, today)
        
        return await _check_daily_renewal(user_id, response.data[0], today)
    except Exception as e:
        log_error(logger, f"Error in check_or_add_user: {e}", exception=e)
        return _balance_payload(status="error", referrer=None)

async def _add_new_user(user_id: int, username: str, first_name: str, last_name: str, referrer_id: Optional[int], today: str) -> Dict[str, Any]:
    try:
        actual_referrer = None
        referrer_name = None
        if referrer_id and str(referrer_id) != str(user_id):
            ref_check = await supabase.table("users").select("first_name, last_name, username").eq("user_id", referrer_id).execute()
            if ref_check.data:
                actual_referrer = referrer_id
                referrer_row = ref_check.data[0]
                referrer_name = f"{referrer_row.get('first_name', '')} {referrer_row.get('last_name', '')}".strip() or referrer_row.get("username") or "غير معروف"

        # 🆕 نقاط الترحيب تُقرأ الآن من app_settings (قابلة للتعديل من لوحة الإدارة)
        # بدل الاعتماد على القيمة الثابتة في constants.py مباشرة.
        welcome_points = await get_setting("welcome_points")

        await supabase.table("users").insert({
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "last_name": last_name or "Unknown",
            "free_points": float(welcome_points),
            "paid_points": 0.0,
            "total_questions": 0,
            "referred_by": actual_referrer,
            "last_renewal": today
        }).execute()

        # 🆕 تسجيل نشاط "انضم عبر رابط دعوة بواسطة فلان" — حدث تحليلات عادي (usage_events)
        # يظهر بلوحة الأدمن ويُستخدم أيضاً كسجل زمني دقيق لكل إحالة جديدة من الآن فصاعداً
        if actual_referrer:
            asyncio.create_task(log_usage_event(user_id, "joined_via_referral", {
                "referrer_id": actual_referrer,
                "referrer_name": referrer_name,
            }))

        return _balance_payload(welcome_points, 0, status="new", referrer=actual_referrer)
    except Exception as e:
        log_error(logger, f"Error adding new user: {e}", exception=e)
        return _balance_payload(status="error", referrer=None)

async def reward_referrer_if_eligible(user_id: int) -> bool:
    """منح مكافأة الإحالة بعد أول توليد كويز ناجح فقط وبشكل غير مكرر."""
    try:
        user_response = await supabase.table("users").select(
            "referred_by, referral_reward_awarded"
        ).eq("user_id", user_id).limit(1).execute()
        if not user_response.data:
            return False

        user = user_response.data[0]
        referrer_id = user.get("referred_by")
        if not referrer_id or user.get("referral_reward_awarded"):
            return False

        # quiz_generated is written only after the generation workflow succeeds.
        # 🆕 نتحقق من "على الأقل مرة واحدة" وليس "بالضبط مرة واحدة": منع الصرف المكرر
        # مضمون فعلياً عبر شرط referral_reward_awarded = FALSE في claim_response أدناه
        # (تحديث ذري)، فلا حاجة لتحقق count == 1 هنا. لو اعتمدنا == 1 وفشل استدعاء هذه
        # الدالة لأي سبب (تعطل، انقطاع) بعد أول كويز ناجح، سيصبح العدّاد 2 عند ثاني
        # كويز حقيقي ويُحرم المُحيل من مكافأته للأبد رغم أن الشرط الفعلي (صديق أنجز
        # كويزاً حقيقياً) قد تحقق فعلاً.
        activity_response = await supabase.table("usage_events").select("id").eq(
            "user_id", user_id
        ).eq("event_type", "quiz_generated").limit(1).execute()
        if not activity_response.data:
            return False

        referrer_response = await supabase.table("users").select("user_id").eq(
            "user_id", referrer_id
        ).limit(1).execute()
        if not referrer_response.data:
            return False

        # Claim first so concurrent requests cannot award the same referral twice.
        claim_response = await supabase.table("users").update(
            {"referral_reward_awarded": True}
        ).eq("user_id", user_id).eq("referral_reward_awarded", False).select("user_id").execute()
        if not claim_response.data:
            return False

        # 🆕 يُنفَّذ عبر RPC ذري (UPDATE ... SET paid_points = paid_points + amount) بدل
        # قراءة الرصيد ثم كتابته من بايثون - القراءة-ثم-الكتابة كانت عرضة لفقدان
        # تحديثات (lost update) لو أكمل أكثر من صديق واحد لنفس المُحيل أول كويز له
        # بشكل شبه متزامن؛ الآن كل عملية زيادة مقفولة على مستوى الصف في قاعدة البيانات.
        # 🆕 مكافأة الإحالة تُقرأ من app_settings (قابلة للتعديل من لوحة الإدارة)
        referral_bonus = await get_setting("referral_bonus_points")

        try:
            await supabase.rpc("award_referral_bonus_atomic", {
                "referrer_user_id": referrer_id,
                "bonus_amount": referral_bonus,
            }).execute()
        except Exception:
            await supabase.table("users").update({
                "referral_reward_awarded": False
            }).eq("user_id", user_id).eq("referral_reward_awarded", True).execute()
            raise

        try:
            from config import bot
            await bot.send_message(
                referrer_id,
                f"🎉 قام صديقك بإجراء أول اختبار له، وتمت إضافة {int(referral_bonus)} نقطة مكافأة إلى رصيدك!",
            )
        except Exception as notification_error:
            log_warning(logger, f"Could not notify referrer {referrer_id}: {notification_error}")

        return True
    except Exception as e:
        log_error(logger, f"Error rewarding referrer for user {user_id}: {e}", exception=e)
        return False

async def _check_daily_renewal(user_id: int, user_data: Dict, today: str) -> Dict[str, Any]:
    try:
        # 🆕 نقاط التجديد اليومي تُقرأ من app_settings (قابلة للتعديل من لوحة الإدارة).
        # لو تعذّر الجلب لأي سبب، الدالة الذرية في قاعدة البيانات لديها خط أمان خاص بها
        # (تقرأ من app_settings مباشرة، ثم تعود لـ 50 كحد أخير) عند تمرير None.
        daily_renewal_points = await get_setting("daily_renewal_points")

        rpc_response = await supabase.rpc("check_and_apply_daily_renewal_atomic", {
            "target_user_id": user_id,
            "today_date": today,
            "renewal_amount": daily_renewal_points
        }).execute()
        
        if rpc_response.data:
            result = rpc_response.data[0] if isinstance(rpc_response.data, list) else rpc_response.data
            return {
                **_balance_payload(result.get("free_points"), result.get("paid_points")),
                "status": result["renewal_status"], 
                "referrer": None
            }
        
        return _balance_payload(user_data.get('free_points'), user_data.get('paid_points'), status="normal", referrer=None)
    except Exception as e:
        log_error(logger, f"Error checking daily renewal via RPC: {e}", exception=e)
        return _balance_payload(user_data.get('free_points'), user_data.get('paid_points'), status="error", referrer=None)
    
async def update_user_stats(user_id: int, points_to_deduct: float, questions_generated: Optional[int] = None) -> Optional[float]:
    try:
        is_valid, error = validate_user_id(user_id)
        if not is_valid: return None

        if questions_generated is None:
            questions_generated = int(points_to_deduct)
        
        rpc_response = await supabase.rpc("deduct_user_points_atomic", {
            "target_user_id": user_id,
            "points_to_deduct": points_to_deduct,
            "questions_generated": questions_generated
        }).execute()
        
        if rpc_response.data is not None:
            return float(rpc_response.data)
        return None
    except Exception as e:
        log_error(logger, f"Error updating user stats via RPC: {e}", exception=e)
        return None

async def refund_user_points(user_id: int, points_to_refund: float) -> bool:
    try:
        if points_to_refund <= 0:
            return True
        is_valid, error = validate_user_id(user_id)
        if not is_valid:
            return False
        response = await supabase.table("users").select("paid_points").eq("user_id", user_id).execute()
        if not response.data:
            return False
        current_paid = float(response.data[0].get("paid_points") or 0)
        new_paid = current_paid + float(points_to_refund)
        await supabase.table("users").update({"paid_points": new_paid}).eq("user_id", user_id).execute()
        log_info(logger, f"Refunded {points_to_refund} points to user {user_id}")
        return True
    except Exception as e:
        log_error(logger, f"Error refunding points for user {user_id}: {e}", exception=e)
        return False

# ==================== Central Quiz & Cache Operations ====================

def _is_transient_jwt_clock_skew_error(error: Exception) -> bool:
    """🩹 خطأ PGRST303 ('JWT issued at future') متقطع وغير متعلق بالكود - سببه انزياح
    بسيط بساعة نظام الـ dyno (clock skew) وقت restart أحياناً، وليس مشكلة بالتوكن نفسه
    (SUPABASE_KEY ثابت من env، لا يُبنى بالكود). عادة يزول خلال ثوانٍ لما الساعة تتزامن
    من جديد عبر NTP، فمحاولة واحدة بعد تأخير بسيط كفيلة بحله دون التأثير على أي مسار آخر."""
    message = str(error).lower()
    return "pgrst303" in message or "jwt issued at future" in message


async def get_file_quizzes(file_hash: str) -> list:
    """جلب كل الكويزات التابعة للملف مرتبة تلقائياً حسب التقييم الأعلى لزملائك الطلاب.
    🆕 يشمل الآن subject_type/question_type/question_type_label/difficulty لعرض
    تفاصيل كل كويز مخزّن (نوع + صعوبة) وللسماح بالفلترة والتحقق من سقف كل تركيبة
    على حدة بدل سقف مشترك واحد للملف بأكمله.
    🩹 يعيد المحاولة مرة واحدة عند PGRST303 (انزياح ساعة مؤقت) بدل الاستسلام فوراً
    وإرجاع قائمة فارغة (كانت تُفسَّر خطأً كـ"لا يوجد كويزات محفوظة لهذا الملف")."""
    for attempt in range(2):
        try:
            res = await supabase.table("quizzes").select(
                "id, creator_id, likes, dislikes, score, quiz_data, is_math_quiz, "
                "subject_type, question_type, question_type_label, difficulty"
            ).eq("file_hash", file_hash).order("score", desc=True).execute()
            return res.data or []
        except Exception as e:
            if attempt == 0 and _is_transient_jwt_clock_skew_error(e):
                log_warning(logger, f"Transient JWT clock-skew error getting file quizzes, retrying once: {e}")
                await asyncio.sleep(2)
                continue
            log_error(logger, f"Error getting file quizzes from central table: {e}")
            return []
    return []

async def save_file_quiz_multiple(
    file_hash: str, creator_id: int, source_title: str, quiz_data: list, total_tokens: int,
    is_math_quiz: bool = False, subject_type: str = "other", question_type: str = "general",
    question_type_label: Optional[str] = None, difficulty: str = "medium",
) -> Optional[str]:
    """حفظ كويز جديد مولد كلياً بالجدول المركزي وعزل التكرار لخدمة الدفعة الدراسية.
    🆕 يخزّن الآن تركيبة (subject_type, question_type, difficulty) مع كل كويز -
    راجع migration_quiz_options.sql - لدعم عرض التفاصيل والفلترة وسقف مستقل لكل تركيبة."""
    try:
        res = await supabase.table("quizzes").insert({
            "creator_id": creator_id,
            "file_hash": file_hash,
            "source_title": source_title,
            "quiz_data": quiz_data,
            "total_tokens": total_tokens,
            "is_math_quiz": is_math_quiz,
            "subject_type": subject_type,
            "question_type": question_type,
            "question_type_label": question_type_label,
            "difficulty": difficulty,
        }).execute()
        if res.data:
            return res.data[0]['id']
        return None
    except Exception as e:
        log_error(logger, f"Error saving central quiz data: {e}")
        return None

async def get_cached_quiz(file_hash: str) -> Optional[Dict[str, Any]]:
    """توجيه ذكي وفولباك (Backward Compatibility) لمحاذاة كود ملف البوت القديم مع الجدول المركزي الجديد"""
    try:
        res = await supabase.table("quizzes").select("quiz_data, total_tokens").eq("file_hash", file_hash).order("score", desc=True).limit(1).execute()
        if res.data:
            log_info(logger, f"Cache HIT (Central Table redirection) for hash: {file_hash}")
            row = res.data[0]
            return {
                "questions_data": row["quiz_data"],
                "total_tokens": row["total_tokens"]
            }
        return None
    except Exception as e:
        log_error(logger, f"Error reading fallback cache content: {e}")
        return None

async def save_quiz_to_cache(file_hash: str, quiz_data: List[Dict[str, Any]], total_tokens: int) -> bool:
    """دالة فولباك للتخزين السريع في المسار المركزي الافتراضي"""
    try:
        # استخدام معرف الإدارة كمنشئ افتراضي في حال عدم تمريره من السيرفر القديم
        admin_id = int(os.getenv("ADMIN_ID", "0"))
        res = await save_file_quiz_multiple(file_hash, admin_id, "كويز مخزن تلقائياً", quiz_data, total_tokens)
        return res is not None
    except Exception as e:
        log_error(logger, f"Error routing fallback cache saving: {e}")
        return False

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

# ==================== Shared Quiz Operations (Deep Linking) ====================
def create_shared_quiz_id() -> str:
    return uuid.uuid4().hex[:12]

async def save_shared_quiz(share_id: str, owner_id: int, title: str, quiz_data: List[Dict[str, Any]], quiz_id: Optional[str] = None) -> bool:
    """تفعيل ميزة المشاركة بدمج كود الرابط مباشرة بالخلية المركزية لمنع تكرار الـ JSONB وهدر المساحة"""
    try:
        target_id = None

        # 🆕 الأولوية دائماً لمعرف الكويز الحقيقي (UUID) القادم من جلسة المستخدم الحالية،
        # لأن المطابقة بالعنوان وحده غير موثوقة عند تكرار العناوين (مثلاً "كويز من ملف")
        # وقد تحقن رمز المشاركة بصف كويز مختلف تماماً عن اللي المستخدم يقصده فعلاً.
        if _is_valid_uuid(quiz_id):
            target_id = quiz_id
        else:
            # مسار احتياطي فقط لو ما توفر quiz_id صالح (مثلاً كويز نصي قديم بدون سجل مركزي بعد)
            check_res = await supabase.table("quizzes").select("id").eq("creator_id", owner_id).eq("source_title", title).order("created_at", desc=True).limit(1).execute()
            if check_res.data:
                target_id = check_res.data[0]["id"]

        if target_id:
            await supabase.table("quizzes").update({"share_code": share_id}).eq("id", target_id).execute()
            log_info(logger, f"Injected share code {share_id} into existing central quiz {target_id}")
        else:
            # إذا كان كويز نصي مباشر أو لم يعثر عليه، ننشئ له سجلاً مركزياً مخصصاً برمز مشاركة فريد
            await supabase.table("quizzes").insert({
                "creator_id": owner_id,
                "source_title": title,
                "quiz_data": quiz_data,
                "share_code": share_id
            }).execute()
            log_info(logger, f"Created new central row with share code: {share_id}")
        return True
    except Exception as e:
        log_error(logger, f"Error linking shared quiz code: {e}")
        return False

async def get_shared_quiz(share_id: str) -> Optional[Dict[str, Any]]:
    try:
        res = await supabase.table("quizzes").select("*").eq("share_code", share_id).execute()
        if res.data:
            return res.data[0]
        return None
    except Exception as e:
        log_error(logger, f"Error loading shared quiz from code: {e}")
        return None

# ==================== Favorite Quiz Operations ====================
async def count_favorite_sections(user_id: int) -> int:
    try:
        res = await supabase.table("favorite_quiz_sections").select("id", count="exact").eq("user_id", user_id).execute()
        return int(res.count or 0)
    except Exception as e:
        log_error(logger, f"Error counting favorite sections: {e}")
        return 0

async def list_favorite_sections(user_id: int) -> List[Dict[str, Any]]:
    try:
        res = await supabase.table("favorite_quiz_sections").select("id, title, created_at").eq("user_id", user_id).order("created_at", desc=False).execute()
        # إعادة تعيين المسميات لتطابق السير القديم في البوت (id -> section_id)
        return [{"section_id": r["id"], "title": r["title"], "created_at": r["created_at"]} for r in (res.data or [])]
    except Exception as e:
        log_error(logger, f"Error listing favorite sections: {e}")
        return []

async def create_favorite_section(user_id: int, title: str) -> Optional[str]:
    try:
        res = await supabase.table("favorite_quiz_sections").insert({
            "user_id": user_id,
            "title": title
        }).execute()
        if res.data:
            return res.data[0]["id"]
        return None
    except Exception as e:
        log_error(logger, f"Error creating favorite section: {e}")
        return None

async def save_favorite_quiz(user_id: int, title: str, quiz_data: List[Dict[str, Any]], section_id: Optional[str] = None, source_title: Optional[str] = None, quiz_id: Optional[str] = None) -> Optional[str]:
    try:
        target_quiz_uuid = None
        
        # التحقق إذا كان الآيدي الممرر عبارة عن UUID صحيح وجاهز للربط في السكيما المركزية
        if quiz_id:
            try:
                uuid.UUID(str(quiz_id))
                target_quiz_uuid = str(quiz_id)
            except ValueError:
                pass
                
        # إذا لم يتوفر UUID (مثل الكويزات القديمة أو النصية)، نضمن حقنها بالجدول المركزي أولاً لتوليد معرف فريد لها
        if not target_quiz_uuid:
            q_res = await supabase.table("quizzes").insert({
                "creator_id": user_id,
                "source_title": source_title or title,
                "quiz_data": quiz_data
            }).execute()
            if q_res.data:
                target_quiz_uuid = q_res.data[0]["id"]
                
        if not target_quiz_uuid:
            return None
            
        fav_id = str(uuid.uuid4())
        await supabase.table("favorite_quizzes").insert({
            "favorite_id": fav_id,
            "user_id": user_id,
            "quiz_id": target_quiz_uuid,
            "section_id": section_id if section_id else None,
            "custom_title": title
        }).execute()
        return fav_id
    except Exception as e:
        log_error(logger, f"Error saving favorite junction entity: {e}")
        return None

async def list_favorite_quizzes(user_id: int, search_query: Optional[str] = None, sort_by: str = "latest") -> List[Dict[str, Any]]:
    try:
        res = await supabase.table("favorite_quizzes").select("favorite_id, section_id, custom_title, created_at, quizzes(id, source_title, quiz_data)").eq("user_id", user_id).execute()
        
        sections_res = await supabase.table("favorite_quiz_sections").select("id, title").eq("user_id", user_id).execute()
        section_map = {s["id"]: s["title"] for s in (sections_res.data or [])}
        
        items = []
        for row in (res.data or []):
            quiz_info = row.get("quizzes") or {}
            item = {
                "favorite_id": row["favorite_id"],
                "quiz_id": quiz_info.get("id"),
                "title": row["custom_title"] or quiz_info.get("source_title") or "كويز",
                "source_title": quiz_info.get("source_title") or "محتوى مستخرج",
                "section_id": row["section_id"],
                "section_title": section_map.get(row["section_id"]) or DEFAULT_FAVORITE_SECTION_TITLE,
                "created_at": row["created_at"],
                "quiz_data": quiz_info.get("quiz_data", [])
            }
            items.append(item)
            
        if search_query:
            query = search_query.strip().lower()
            items = [
                i for i in items
                if query in i["title"].lower() or query in i["source_title"].lower() or query in i["section_title"].lower()
            ]

        if sort_by == "section":
            items.sort(key=lambda x: x["created_at"] or "", reverse=True)
            items.sort(key=lambda x: x["section_title"].lower())
        else:
            items.sort(key=lambda x: x["created_at"] or "", reverse=True)

        return items
    except Exception as e:
        log_error(logger, f"Error listing favorite central junction row: {e}")
        return []

async def can_create_more_favorite_sections(user_id: int) -> bool:
    return await count_favorite_sections(user_id) < MAX_FAVORITE_SECTIONS

async def get_favorite_quiz(user_id: int, favorite_id: str) -> Optional[Dict[str, Any]]:
    try:
        res = await supabase.table("favorite_quizzes").select("favorite_id, custom_title, section_id, quizzes(*)").eq("user_id", user_id).eq("favorite_id", favorite_id).execute()
        if res.data:
            row = res.data[0]
            quiz_info = row.get("quizzes") or {}
            return {
                "favorite_id": row["favorite_id"],
                "title": row["custom_title"] or quiz_info.get("source_title"),
                "quiz_data": quiz_info.get("quiz_data"),
                "section_id": row["section_id"],
                # 🆕 quiz_id/creator_id: مأخوذان مباشرة من quizzes(*) (مُحمَّلة أصلاً بهذا
                # الاستعلام) - يُستخدمان لإظهار زر "حذف الكويز نهائياً" فقط للأدمن أو
                # لمالك الكويز الفعلي (راجع services/quiz_permissions.py).
                "quiz_id": quiz_info.get("id"),
                "creator_id": quiz_info.get("creator_id"),
            }
        return None
    except Exception as e:
        log_error(logger, f"Error loading specific favorite quiz join row: {e}")
        return None

async def get_favorite_quiz_by_global_id(favorite_id: str) -> Optional[Dict[str, Any]]:
    try:
        res = await supabase.table("favorite_quizzes").select("favorite_id, custom_title, quizzes(*)").eq("favorite_id", favorite_id).execute()
        if res.data:
            row = res.data[0]
            quiz_info = row.get("quizzes") or {}
            return {
                "favorite_id": row["favorite_id"],
                "title": row["custom_title"] or quiz_info.get("source_title"),
                "quiz_data": quiz_info.get("quiz_data")
            }
        return None
    except Exception as e:
        log_error(logger, f"Error loading global id matching favorite element: {e}")
        return None

async def remove_favorite_quiz(user_id: int, favorite_id: str) -> bool:
    try:
        await supabase.table("favorite_quizzes").delete().eq("user_id", user_id).eq("favorite_id", favorite_id).execute()
        return True
    except Exception as e:
        log_error(logger, f"Error removing target favorite quiz connection: {e}")
        return False

# ==================== Rating, Feedbacks & Quality Control Operations ====================

async def admin_get_feedbacks_page(limit: int = 5, offset: int = 0) -> tuple[List[Dict[str, Any]], int]:
    """🆕 جلب صفحة من ملاحظات الطلاب مع معلومات الكويز (اسم الملف) والطالب (الاسم) المرتبطة بها،
    مع العدد الإجمالي لدعم التصفح بصفحات."""
    try:
        count_res = await supabase.table("quiz_feedbacks").select("id", count="exact").execute()
        total = count_res.count or 0

        res = await supabase.table("quiz_feedbacks").select(
            "id, comment, created_at, user_id, quiz_id, "
            "quizzes(id, source_title, file_hash)"
        ).order("created_at", desc=True).range(offset, offset + limit - 1).execute()
        rows = res.data or []
        if not rows:
            return [], total

        user_ids = list({row["user_id"] for row in rows})
        users_res = await supabase.table("users").select("user_id, first_name, last_name, username").in_("user_id", user_ids).execute()
        users_map = {u["user_id"]: u for u in (users_res.data or [])}
        for row in rows:
            row["student"] = users_map.get(row["user_id"])
        return rows, total
    except Exception as e:
        log_error(logger, f"Error fetching admin feedbacks page: {e}")
        return [], 0


async def admin_get_feedback_by_id(feedback_id: int) -> Optional[Dict[str, Any]]:
    """🆕 جلب ملاحظة واحدة بكامل تفاصيلها (الكويز + الطالب) لعرض شاشة التفاصيل الإدارية."""
    try:
        res = await supabase.table("quiz_feedbacks").select(
            "id, comment, created_at, user_id, quiz_id, "
            "quizzes(id, source_title, file_hash)"
        ).eq("id", feedback_id).limit(1).execute()
        if not res.data:
            return None
        row = res.data[0]
        user_res = await supabase.table("users").select("user_id, first_name, last_name, username").eq("user_id", row["user_id"]).limit(1).execute()
        row["student"] = user_res.data[0] if user_res.data else None
        return row
    except Exception as e:
        log_error(logger, f"Error fetching feedback {feedback_id}: {e}")
        return None


async def admin_get_quiz_board_position(file_hash: Optional[str], quiz_id: str) -> tuple[int, int]:
    """🆕 يرجع (رقم هذا الكويز، العدد الكلي) ضمن نفس لوحة/ملف الكويزات المخزّنة كاش،
    بنفس ترتيب الأفضلية (score) الذي يراه الطلاب فعلياً."""
    try:
        if not file_hash:
            return (0, 0)
        quizzes = await get_file_quizzes(file_hash)
        ids = [str(q["id"]) for q in quizzes]
        if str(quiz_id) in ids:
            return (ids.index(str(quiz_id)) + 1, len(ids))
        return (0, len(ids))
    except Exception as e:
        log_error(logger, f"Error computing quiz board position for {quiz_id}: {e}")
        return (0, 0)


async def admin_get_quiz_by_id(quiz_id: str) -> Optional[Dict[str, Any]]:
    """🆕 جلب بيانات كويز واحد كاملة من الجدول المركزي (تُستخدم لتجربة الكويز من لوحة الإدارة)."""
    try:
        res = await supabase.table("quizzes").select("id, source_title, quiz_data, file_hash, creator_id").eq("id", quiz_id).limit(1).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        log_error(logger, f"Error fetching quiz {quiz_id}: {e}")
        return None


async def admin_delete_quiz(quiz_id: str) -> bool:
    """🆕 حذف كويز بالكامل من الجدول المركزي؛ التصويتات والنقاط وعناصر المفضلة والملاحظات المرتبطة
    به تُحذف تلقائياً معه (ON DELETE CASCADE) على مستوى قاعدة البيانات."""
    try:
        await supabase.table("quizzes").delete().eq("id", quiz_id).execute()
        return True
    except Exception as e:
        log_error(logger, f"Error deleting quiz {quiz_id}: {e}")
        return False


async def submit_quiz_vote(quiz_id: str, user_id: int, vote_type: str) -> bool:
    """إرسال وحقن تصويت الطلاب (لايك/ديسلايك) عبر الـ RPC لضمان منع التكرار وحساب السكور لحظياً"""
    try:
        res = await supabase.rpc("vote_on_quiz", {
            "p_quiz_id": quiz_id,
            "p_user_id": user_id,
            "p_vote": vote_type
        }).execute()
        return bool(res.data)
    except Exception as e:
        log_error(logger, f"Error executing quiz atomic vote function: {e}")
        return False

async def save_quiz_feedback(quiz_id: str, user_id: int, comment: str) -> bool:
    """حفظ ملاحظات وإفادات الطلاب الأكاديمية لمراجعتها لاحقاً من قبل الإدارة"""
    try:
        await supabase.table("quiz_feedbacks").insert({
            "quiz_id": quiz_id,
            "user_id": user_id,
            "comment": comment
        }).execute()
        return True
    except Exception as e:
        log_error(logger, f"Error saving student feedback on quiz: {e}")
        return False

async def _get_safe_to_delete_quiz_ids(threshold: str) -> List[str]:
    """يرجع فقط IDs الكويزات المؤهلة للحذف الفعلي: قديمة + سيئة التقييم،
    وبنفس الوقت ماإلها share_code (مو مشاركة)، مو محفوظة بمفضلة أي مستخدم،
    وما حدا رجع لعبها أبداً (ما إلها أي صف بجدول quiz_scores).
    هيك منتجنب خرق foreign key constraint (quiz_scores_quiz_id_fkey) ومنحافظ
    على أي كويز عندو قيمة فعلية (مشاركة/مفضلة/استخدام)."""
    try:
        candidates_res = await supabase.table("quizzes") \
            .select("id") \
            .lt("created_at", threshold) \
            .lt("score", 0) \
            .is_("share_code", "null") \
            .execute()
        candidate_ids = [q["id"] for q in (candidates_res.data or [])]
        if not candidate_ids:
            return []

        fav_res = await supabase.table("favorite_quizzes") \
            .select("quiz_id") \
            .in_("quiz_id", candidate_ids) \
            .execute()
        favorited_ids = {r["quiz_id"] for r in (fav_res.data or [])}

        scores_res = await supabase.table("quiz_scores") \
            .select("quiz_id") \
            .in_("quiz_id", candidate_ids) \
            .execute()
        used_ids = {r["quiz_id"] for r in (scores_res.data or [])}

        return [qid for qid in candidate_ids if qid not in favorited_ids and qid not in used_ids]
    except Exception as e:
        log_error(logger, f"Error resolving safe-to-delete quiz ids: {e}")
        return []


async def auto_cleanup_bad_quizzes():
    """تنظيف تلقائي شامل للكويزات المرفوضة من الطلاب (ديسلايكات عالية) والتي تجاوزت 48 ساعة،
    باستثناء أي كويز عندو share_code أو محفوظ بالمفضلة أو تم استخدامه ولو مرة."""
    try:
        threshold = (datetime.datetime.utcnow() - datetime.timedelta(days=2)).isoformat()
        deletable_ids = await _get_safe_to_delete_quiz_ids(threshold)
        if deletable_ids:
            await supabase.table("quizzes").delete().in_("id", deletable_ids).execute()
        log_info(logger, f"Automated database garbage cleanup loop executed successfully. Deleted {len(deletable_ids)} quizzes.")
    except Exception as e:
        log_error(logger, f"Error running the background auto cleanup query: {e}")

# ==================== Admin Operations ====================
async def admin_add_points(target_id: int, amount: int, balance_type: str = "paid") -> Optional[int]:
    try:
        if balance_type not in ("free", "paid"):
            return None
        user = await supabase.table("users").select("free_points, paid_points").eq("user_id", target_id).execute()
        if user.data:
            paid_points = float(user.data[0].get('paid_points') or 0)
            free_points = float(user.data[0].get('free_points') or 0)
            if balance_type == "free":
                free_points += amount
            else:
                paid_points += amount
            await supabase.table("users").update({
                "free_points": free_points,
                "paid_points": paid_points,
            }).eq("user_id", target_id).execute()
            return int(free_points + paid_points)
        return None
    except Exception as e:
        logger.error(f"Error in admin_add_points: {e}")
        return None

async def admin_get_global_stats() -> Dict[str, int]:
    try:
        response = await supabase.table("users").select("user_id, total_questions").execute()
        if response.data:
            return {"total_users": len(response.data), "total_questions": sum(u['total_questions'] for u in response.data)}
        return {"total_users": 0, "total_questions": 0}
    except Exception as e:
        logger.error(f"Error getting global stats: {e}")
        return {"total_users": 0, "total_questions": 0}

async def admin_search_user(query: str) -> Optional[list]:
    try:
        query = query.strip()
        if query.isdigit():
            res = await supabase.table("users").select("*").eq("user_id", int(query)).execute()
        else:
            clean_username = query.lstrip('@')
            res = await supabase.table("users").select("*").ilike("username", f"%{clean_username}%").execute()
        return res.data
    except Exception as e:
        return None

# ==================== Quiz Scores & Leaderboard Operations ====================
async def get_or_update_high_score(user_id: int, quiz_id: str, current_score: int, total_questions: int) -> Dict[str, Any]:
    if not _is_valid_uuid(quiz_id):
        log_warning(logger, f"Skipping high score update: invalid quiz_id '{quiz_id}' (not a real UUID)")
        return {"previous_score": None, "highest_score": current_score, "is_public": False}
    try:
        res = await supabase.table("quiz_scores").select("*").eq("quiz_id", quiz_id).eq("user_id", user_id).execute()
        
        previous_score = None
        new_highest = current_score
        is_public = False
        
        if res.data:
            existing = res.data[0]
            previous_score = existing["highest_score"]
            is_public = existing["is_public"]
            
            if current_score > previous_score:
                await supabase.table("quiz_scores").update({
                    "highest_score": current_score,
                    "total_questions": total_questions,
                    "updated_at": datetime.datetime.utcnow().isoformat()
                }).eq("id", existing["id"]).execute()
            else:
                new_highest = previous_score
        else:
            # 🆕 أول محاولة على هالكويز: النتيجة تنحفظ خاصة افتراضياً، وشاشة النتيجة
            # (handlers/quiz_runner.py) بتسأل الطالب صراحة نعم/لا قبل ما تصير عامة.
            is_public = False
            await supabase.table("quiz_scores").insert({
                "quiz_id": quiz_id,
                "user_id": user_id,
                "highest_score": current_score,
                "total_questions": total_questions,
                "is_public": False
            }).execute()
            
        return {
            "previous_score": previous_score,
            "highest_score": new_highest,
            "is_public": is_public
        }
    except Exception as e:
        log_error(logger, f"Error updating high score: {e}", exception=e)
        return {"previous_score": None, "highest_score": current_score, "is_public": False}

async def publish_score_to_leaderboard(user_id: int, quiz_id: str) -> bool:
    if not _is_valid_uuid(quiz_id):
        log_warning(logger, f"Skipping leaderboard publish: invalid quiz_id '{quiz_id}' (not a real UUID)")
        return False
    try:
        await supabase.table("quiz_scores").update({"is_public": True}).eq("quiz_id", quiz_id).eq("user_id", user_id).execute()
        return True
    except Exception as e:
        log_error(logger, f"Error publishing score: {e}", exception=e)
        return False

async def hide_score_from_leaderboard(user_id: int, quiz_id: str) -> bool:
    """🆕 إخفاء نتيجة الطالب من لوحة الشرف (النتائج تُنشر فقط بعد موافقة صريحة،
    فهاي الدالة تسمح للطالب بالتراجع لاحقاً من زر الإخفاء تحت لوحة الشرف)."""
    if not _is_valid_uuid(quiz_id):
        log_warning(logger, f"Skipping leaderboard hide: invalid quiz_id '{quiz_id}' (not a real UUID)")
        return False
    try:
        await supabase.table("quiz_scores").update({"is_public": False}).eq("quiz_id", quiz_id).eq("user_id", user_id).execute()
        return True
    except Exception as e:
        log_error(logger, f"Error hiding score: {e}", exception=e)
        return False

async def get_my_leaderboard_status(user_id: int, quiz_id: str) -> Optional[bool]:
    """🆕 حالة نشر نتيجة الطالب الحالية لهالكويز - تُستخدم لعرض زر
    الإخفاء/الإظهار الصحيح تحت لوحة الشرف. ترجع None إذا الطالب ما أخد
    هالكويز أصلاً (ما في صف بجدول quiz_scores)."""
    if not _is_valid_uuid(quiz_id):
        return None
    try:
        res = await supabase.table("quiz_scores").select("is_public").eq("quiz_id", quiz_id).eq("user_id", user_id).execute()
        if res.data:
            return bool(res.data[0]["is_public"])
        return None
    except Exception as e:
        log_error(logger, f"Error getting leaderboard status: {e}", exception=e)
        return None

async def get_top_5_leaderboard(quiz_id: str) -> List[Dict[str, Any]]:
    if not _is_valid_uuid(quiz_id):
        log_warning(logger, f"Skipping leaderboard fetch: invalid quiz_id '{quiz_id}' (not a real UUID)")
        return []
    try:
        res = await supabase.table("quiz_scores") \
            .select("highest_score, total_questions, users(first_name, last_name)") \
            .eq("quiz_id", quiz_id) \
            .eq("is_public", True) \
            .order("highest_score", desc=True) \
            .limit(5) \
            .execute()
            
        return res.data or []
    except Exception as e:
        log_error(logger, f"Error getting leaderboard: {e}", exception=e)
        return []

# ==================== Usage Analytics & Tracking (Fixed & Complete) ====================

import json
from config import redis_client

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except (ValueError, TypeError):
    ADMIN_ID = 0
    
# 1️⃣ تسجيل الأحداث (تعريف واحد موحد: حفظ مباشر في الداتابيز مع مسار احتياطي لـ Redis)
async def log_usage_event(user_id: int, event_type: str, metadata: Optional[Dict[str, Any]] = None) -> None:
    """تسجيل حدث استخدام آمن (يستثني الآدمن لعدم التأثير على التحليلات)."""
    # 🚫 تجنب تسجيل نشاط الآدمن في التحليلات
    if ADMIN_ID and user_id == ADMIN_ID:
        return

    try:
        payload = {
            "user_id": user_id,
            "event_type": event_type,
            "metadata": metadata or {},
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        await supabase.table("usage_events").insert(payload).execute()
    except Exception as e:
        try:
            await redis_client.rpush("analytics_queue", json.dumps(payload))
        except Exception as redis_err:
            # ⚠️ نستخدم logger.error مباشرة هنا (وليس log_error) عن قصد: log_error تحاول
            # ربط أي خطأ بالمستخدم الحالي عبر log_error_event، والتي بدورها تنادي
            # log_usage_event — فلو استُخدمت log_error هنا لدخلنا بحلقة استدعاء ذاتية
            # لا نهائية عند فشل التسجيل. هذا الفشل بالذات (فشل تسجيل + فشل احتياطي Redis)
            # نادر جداً وغير حرج لتجربة الطالب، فيكفي تسجيله بالـ logger فقط.
            logger.error(f"Error logging usage event for user {user_id}: {e} | Redis fallback failed: {redis_err}")

# 1️⃣.5 تسجيل الأخطاء التي يواجهها الطالب فعلياً (خطأ = حدث بنوع 'error_occurred')
async def log_error_event(user_id: int, error_message: str, exception: Optional[Exception] = None,
                           update_type: Optional[str] = None, context: Optional[str] = None,
                           unhandled: bool = False) -> None:
    """
    تسجيل خطأ واجهه طالب فعلياً أثناء استخدام البوت، كحدث تحليلات عادي بجدول usage_events
    (event_type='error_occurred') — بنفس آلية log_usage_event تماماً (صامتة عند الفشل،
    مع مسار احتياطي عبر Redis)، فيظهر تلقائياً بلوحة الأدمن (قائمة الأحداث، تصدير CSV،
    وقسم "🐞 آخر الأخطاء" المخصص).

    تُستدعى تلقائياً من logger.log_error()/log_critical() لأي استدعاء بأي مكان بالمشروع
    طالما هناك سياق مستخدم حالي (راجع error_context.py)، بالإضافة لاستدعاء صريح من
    ErrorTrackingMiddleware عند حدوث استثناء غير متوقع بالكامل لم يلتقطه أي try/except
    محلي (unhandled=True).
    """
    tb_str = None
    if exception is not None:
        try:
            tb_str = "".join(
                traceback.format_exception(type(exception), exception, exception.__traceback__)
            )[-2000:]  # آخر 2000 حرف كافية عادةً لمعرفة مكان الخطأ دون تضخيم الصف بالداتابيز
        except Exception:
            tb_str = None

    metadata: Dict[str, Any] = {
        "message": (error_message or "")[:500],
        "error_type": type(exception).__name__ if exception else None,
        "update_type": update_type,
        "context": (context or "")[:200] if context else None,
        "unhandled": unhandled,
    }
    if tb_str:
        metadata["traceback"] = tb_str

    await log_usage_event(user_id, "error_occurred", metadata)


# 2️⃣ تفريغ طابور Redis بأمان دون فقدان البيانات (Transactional Pop)
async def flush_analytics_queue() -> None:
    """تفريغ الأحداث الاحتياطية من Redis ورفعها دفعة واحدة إلى Supabase مع ضمان عدم الفقدان."""
    try:
        events = []
        raw_items = []
        for _ in range(500):
            raw = await redis_client.lpop("analytics_queue")
            if not raw:
                break
            raw_items.append(raw)
            events.append(json.loads(raw))

        if events:
            try:
                await supabase.table("usage_events").insert(events).execute()
                log_info(logger, f"Successfully flushed {len(events)} analytics events to Supabase.")
            except Exception as db_err:
                # إرجاع البيانات إلى طابور Redis في حال فشل الإدراج لعدم ضياعها
                for item in reversed(raw_items):
                    await redis_client.lpush("analytics_queue", item)
                log_error(logger, f"Failed to insert flushed events into Supabase, re-queued items: {db_err}")
    except Exception as e:
        log_error(logger, f"Error flushing analytics queue: {e}")


# 3️⃣ إدارة محاولات الكويز مع تفادي الـ Race Conditions
def start_quiz_attempt(user_id: int, quiz_id: Optional[str], source_type: str, total_questions: int) -> str:
    """توليد معرف فريد وبدء المحاولة (تستثني الآدمن)."""
    client_ref = uuid.uuid4().hex
    # 🚫 تجنب تسجيل محاولات الآدمن بجدول المحاولات
    if ADMIN_ID and user_id == ADMIN_ID:
        return client_ref
        
    asyncio.create_task(_insert_quiz_attempt(client_ref, user_id, quiz_id, source_type, total_questions))
    return client_ref


async def _insert_quiz_attempt(client_ref: str, user_id: int, quiz_id: Optional[str], source_type: str, total_questions: int) -> None:
    try:
        clean_quiz_id = str(quiz_id) if _is_valid_uuid(quiz_id) else None

        await supabase.table("quiz_attempts").insert({
            "client_ref": client_ref,
            "user_id": user_id,
            "quiz_id": clean_quiz_id,
            "source_type": source_type,
            "total_questions": total_questions,
            "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }).execute()
    except Exception as e:
        log_error(logger, f"Error inserting quiz attempt tracking row ({client_ref}): {e}")


async def complete_quiz_attempt(attempt_ref: Optional[str], score: int) -> None:
    """إغلاق المحاولة المكتملة وحساب الوقت بدقة مع معالجة تأخير السجلات."""
    if not attempt_ref:
        return
    try:
        row = None
        for _ in range(3):
            res = await supabase.table("quiz_attempts").select("started_at").eq("client_ref", attempt_ref).limit(1).execute()
            if res.data:
                row = res.data[0]
                break
            await asyncio.sleep(0.4)  # انتظار 400ms في حال وجود تأخير في الشبكة

        duration = None
        if row and row.get("started_at"):
            try:
                started_str = str(row["started_at"]).replace("Z", "+00:00")
                started = datetime.datetime.fromisoformat(started_str)
                duration = int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds())
            except Exception as dt_err:
                log_warning(logger, f"Duration calculation issue for {attempt_ref}: {dt_err}")

        await supabase.table("quiz_attempts").update({
            "score": score,
            "is_completed": True,
            "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "duration_seconds": duration
        }).eq("client_ref", attempt_ref).execute()
    except Exception as e:
        log_error(logger, f"Error completing quiz attempt {attempt_ref}: {e}")


async def has_completed_any_quiz_before(user_id: int) -> bool:
    """
    🆕 UX: تُستخدم فقط لتحديد ما إذا كان المستخدم قد أنهى أي اختبار من قبل (لتأجيل ظهور
    القائمة الرئيسية عن مستخدم جديد إلى ما بعد أول اختبار كامل له - راجع
    handlers/quiz_runner.py::_handle_quiz_completion). فحص بسيط بـ count("exact")
    بدون جلب صفوف فعلية. أي خطأ هنا يُعامل بتحفّظ كـ "نعم أكمل من قبل" (True) - أي
    نُفضّل عدم إزعاج مستخدم قديم بقائمة إضافية غير متوقعة على خطر إخفاء القائمة عن
    مستخدم جديد فعلاً بسبب عطل مؤقت بالاستعلام.
    """
    try:
        res = await supabase.table("quiz_attempts").select(
            "id", count="exact"
        ).eq("user_id", user_id).eq("is_completed", True).limit(1).execute()
        total = res.count if res.count is not None else len(res.data or [])
        return total > 0
    except Exception as e:
        log_error(logger, f"Error checking prior completed quizzes for user {user_id}: {e}")
        return True


async def mark_quiz_attempt_stopped(attempt_ref: Optional[str]) -> None:
    """تسجيل توقف الطالب المبكر."""
    if not attempt_ref:
        return
    try:
        await supabase.table("quiz_attempts").update({
            "is_completed": False,
            "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }).eq("client_ref", attempt_ref).execute()
    except Exception as e:
        log_error(logger, f"Error marking quiz attempt {attempt_ref} as stopped: {e}")


# 4️⃣ دوال الاستعلامات الخاصة بـ لوحة التحكم والإدارة (Admin Analytics Queries)

async def admin_get_usage_overview(days: int = 7) -> Dict[str, Any]:
    """ملخص شامل لسلوك الاستخدام لـ الطلاب حصراً."""
    empty = {
        "days": days, "active_users": 0, "event_counts": {}, "total_attempts": 0,
        "completed_attempts": 0, "completion_rate": 0.0, "avg_duration_seconds": 0,
        "source_breakdown": {}, "avg_score_percentage": 0.0, "error_count": 0, "users_with_errors": 0,
    }
    try:
        since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()

        # استعلام الأحداث مع استبعاد الآدمن
        events_query = supabase.table("usage_events").select("user_id, event_type").gte("created_at", since)
        if ADMIN_ID:
            events_query = events_query.neq("user_id", ADMIN_ID)
        events_res = await events_query.execute()
        events = events_res.data or []

        active_users = len({e["user_id"] for e in events})
        event_counts: Dict[str, int] = {}
        for e in events:
            event_counts[e["event_type"]] = event_counts.get(e["event_type"], 0) + 1

        error_events = [e for e in events if e["event_type"] == "error_occurred"]
        error_count = len(error_events)
        users_with_errors = len({e["user_id"] for e in error_events})

        # استعلام المحاولات مع استبعاد الآدمن
        attempts_query = supabase.table("quiz_attempts").select(
            "is_completed, source_type, duration_seconds, score, total_questions"
        ).gte("started_at", since)
        if ADMIN_ID:
            attempts_query = attempts_query.neq("user_id", ADMIN_ID)
        attempts_res = await attempts_query.execute()
        attempts = attempts_res.data or []

        total_attempts = len(attempts)
        completed_attempts = sum(1 for a in attempts if a.get("is_completed"))
        completion_rate = (completed_attempts / total_attempts * 100) if total_attempts else 0.0

        durations = [a["duration_seconds"] for a in attempts if a.get("duration_seconds")]
        avg_duration = (sum(durations) / len(durations)) if durations else 0

        source_breakdown: Dict[str, int] = {}
        for a in attempts:
            src = a.get("source_type") or "unknown"
            source_breakdown[src] = source_breakdown.get(src, 0) + 1

        scored = [a for a in attempts if a.get("total_questions")]
        pct_list = [(a["score"] / a["total_questions"]) * 100 for a in scored if a["total_questions"] > 0]
        avg_score_pct = (sum(pct_list) / len(pct_list)) if pct_list else 0.0

        return {
            "days": days,
            "active_users": active_users,
            "event_counts": event_counts,
            "total_attempts": total_attempts,
            "completed_attempts": completed_attempts,
            "completion_rate": completion_rate,
            "avg_duration_seconds": avg_duration,
            "source_breakdown": source_breakdown,
            "avg_score_percentage": avg_score_pct,
            "error_count": error_count,
            "users_with_errors": users_with_errors,
        }
    except Exception as e:
        log_error(logger, f"Error building usage overview: {e}")
        return empty


async def admin_get_daily_active_users(days: int = 14) -> List[Dict[str, Any]]:
    """عدد المستخدمين النشطين يومياً (استبعاد الآدمن).

    ⚡ بيستعلم مباشرة من الـ View الجاهز `daily_active_users` (توقيت سوريا مطبّق مسبقاً
    داخل الـ View نفسه على مستوى قاعدة البيانات)، بدل جلب كل صفوف usage_events الخام على
    دفعات (pagination) وإعادة حساب اليوم/التجميع يدوياً في بايثون كما كان سابقاً. الـ View
    بيرجع صف واحد لكل (يوم، مستخدم نشط) بعد الدمج، فحجم البيانات المنقولة أصغر بكثير ولا
    داعي لأي pagination عملياً ضمن نطاق الأيام المطلوب.

    تعريف الـ View المتوقّع بقاعدة البيانات (Postgres):
        CREATE OR REPLACE VIEW daily_active_users AS
        SELECT
            ((created_at AT TIME ZONE 'UTC') + INTERVAL '3 hours')::date AS day,
            user_id
        FROM usage_events
        GROUP BY day, user_id;
    """
    try:
        since_day = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        ).strftime("%Y-%m-%d")

        query = supabase.table("daily_active_users").select("day, user_id").gte("day", since_day)
        if ADMIN_ID:
            query = query.neq("user_id", ADMIN_ID)
        res = await query.execute()
        rows = res.data or []

        by_day: Dict[str, set] = {}
        for r in rows:
            day = r.get("day")
            if not day:
                continue
            by_day.setdefault(str(day), set()).add(r["user_id"])

        return sorted(
            [{"day": d, "active_users": len(u)} for d, u in by_day.items()],
            key=lambda x: x["day"]
        )
    except Exception as e:
        log_error(logger, f"Error computing daily active users: {e}")
        return []

async def admin_get_user_activity(user_id: int, event_limit: int = 15) -> Dict[str, Any]:
    """سجل نشاط تفصيلي لطالب محدد."""
    empty = {"recent_events": [], "total_attempts": 0, "completed_attempts": 0, "avg_score_percentage": 0.0, "recent_attempts": []}
    try:
        events_res = await supabase.table("usage_events").select("event_type, metadata, created_at") \
            .eq("user_id", user_id).order("created_at", desc=True).limit(event_limit).execute()

        attempts_res = await supabase.table("quiz_attempts").select(
            "source_type, score, total_questions, is_completed, duration_seconds, started_at"
        ).eq("user_id", user_id).order("started_at", desc=True).execute()
        attempts = attempts_res.data or []

        total_attempts = len(attempts)
        completed = sum(1 for a in attempts if a.get("is_completed"))
        scored = [a for a in attempts if a.get("total_questions")]
        pct_list = [(a["score"] / a["total_questions"]) * 100 for a in scored if a["total_questions"] > 0]
        avg_pct = (sum(pct_list) / len(pct_list)) if pct_list else 0.0

        return {
            "recent_events": events_res.data or [],
            "total_attempts": total_attempts,
            "completed_attempts": completed,
            "avg_score_percentage": avg_pct,
            "recent_attempts": attempts[:10],
        }
    except Exception as e:
        log_error(logger, f"Error fetching user activity for {user_id}: {e}")
        return empty


async def admin_get_all_usage_events(limit: int = 5000) -> List[Dict[str, Any]]:
    """جلب سجل الأحداث الخام لـ الطلاب حصراً كملف CSV."""
    try:
        query = supabase.table("usage_events").select("user_id, event_type, metadata, created_at")
        if ADMIN_ID:
            query = query.neq("user_id", ADMIN_ID)
        res = await query.order("created_at", desc=True).limit(limit).execute()
        return res.data or []
    except Exception as e:
        log_error(logger, f"Error exporting usage events: {e}")
        return []


async def admin_get_recent_errors(limit: int = 20, days: int = 7) -> List[Dict[str, Any]]:
    """جلب آخر الأخطاء التي واجهها الطلاب فعلياً (event_type='error_occurred') مع بيانات الطالب."""
    try:
        since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
        query = supabase.table("usage_events") \
            .select("user_id, metadata, created_at") \
            .eq("event_type", "error_occurred") \
            .gte("created_at", since)
        if ADMIN_ID:
            query = query.neq("user_id", ADMIN_ID)
        res = await query.order("created_at", desc=True).limit(limit).execute()
        errors = res.data or []
        if not errors:
            return []

        user_ids = list({e["user_id"] for e in errors if e.get("user_id")})
        users_map = {}
        if user_ids:
            users_res = await supabase.table("users") \
                .select("user_id, first_name, last_name, username") \
                .in_("user_id", user_ids) \
                .execute()
            users_map = {u["user_id"]: u for u in (users_res.data or [])}

        for e in errors:
            e["user"] = users_map.get(e.get("user_id"), {})
            e["time_str"] = format_syria_time(e.get("created_at"), fmt="%I:%M %p (%Y-%m-%d)")

        return errors
    except Exception as e:
        log_error(logger, f"Error fetching recent errors: {e}")
        return []


async def admin_get_referral_leaderboard(limit: int = 30) -> List[Dict[str, Any]]:
    """
    يبني ترتيب الطلاب الذين أحالوا غيرهم (الأكثر إحالة أولاً)، مع القائمة الكاملة لمن
    انضم عن طريق كل واحد منهم (لعرضها كقائمة منفردة عند الطلب، حتى لا تزدحم الواجهة
    الرئيسية بأسماء كل المُحالين دفعة واحدة).

    المصدر: عمود users.referred_by (مصدر رسمي وكامل تاريخياً لكل الإحالات، وليس فقط ما
    بعد إضافة حدث joined_via_referral)، لذا يشمل كل الإحالات القديمة والجديدة.
    """
    try:
        res = await supabase.table("users") \
            .select("user_id, first_name, last_name, username, referred_by") \
            .not_.is_("referred_by", "null") \
            .execute()
        referred_users = res.data or []
        if not referred_users:
            return []

        referrer_ids = list({u["referred_by"] for u in referred_users if u.get("referred_by")})
        referrers_res = await supabase.table("users") \
            .select("user_id, first_name, last_name, username") \
            .in_("user_id", referrer_ids) \
            .execute()
        referrers_map = {u["user_id"]: u for u in (referrers_res.data or [])}

        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for u in referred_users:
            grouped.setdefault(u["referred_by"], []).append(u)

        def _display_name(row: Dict[str, Any]) -> str:
            return f"{row.get('first_name', '')} {row.get('last_name', '')}".strip() or row.get("username") or "غير معروف"

        leaderboard = []
        for ref_id, referred_list in grouped.items():
            referrer_row = referrers_map.get(ref_id, {})
            leaderboard.append({
                "referrer_id": ref_id,
                "referrer_name": _display_name(referrer_row),
                "referrer_username": referrer_row.get("username"),
                "referral_count": len(referred_list),
                "referred_users": [
                    {
                        "user_id": u["user_id"],
                        "name": _display_name(u),
                        "username": u.get("username"),
                    }
                    for u in referred_list
                ],
            })

        leaderboard.sort(key=lambda x: x["referral_count"], reverse=True)
        return leaderboard[:limit]
    except Exception as e:
        log_error(logger, f"Error building referral leaderboard: {e}")
        return []


async def admin_get_today_active_users() -> List[Dict[str, Any]]:
    """جلب قائمة الطلاب النشطين خلال الـ 24 ساعة الأخيرة حصراً (استبعاد الآدمن)."""
    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        twenty_four_hours_ago = (now_utc - datetime.timedelta(hours=24)).isoformat()

        query = supabase.table("usage_events") \
            .select("user_id, event_type, created_at") \
            .gte("created_at", twenty_four_hours_ago)
        if ADMIN_ID:
            query = query.neq("user_id", ADMIN_ID)
            
        res = await query.order("created_at", desc=True).execute()
        rows = res.data or []
        if not rows:
            return []

        latest_event_per_user: Dict[int, Dict[str, Any]] = {}
        for r in rows:
            uid = r["user_id"]
            if uid not in latest_event_per_user:
                latest_event_per_user[uid] = r

        user_ids = list(latest_event_per_user.keys())

        users_res = await supabase.table("users") \
            .select("user_id, first_name, last_name, username") \
            .in_("user_id", user_ids) \
            .execute()

        users_map = {u["user_id"]: u for u in (users_res.data or [])}
        active_list = []

        for uid, ev in latest_event_per_user.items():
            u_info = users_map.get(uid, {})
            syria_dt = to_syria_datetime(ev.get("created_at"))
            time_str = syria_dt.strftime("%I:%M %p (%Y-%m-%d)").replace("AM", "ص").replace("PM", "م") if syria_dt else "غير معروف"

            active_list.append({
                "user_id": uid,
                "first_name": u_info.get("first_name", "طالب"),
                "last_name": u_info.get("last_name", ""),
                "username": u_info.get("username", "Unknown"),
                "last_event": ev["event_type"],
                "time_str": time_str
            })

        return active_list
    except Exception as e:
        log_error(logger, f"Error fetching 24h active users: {e}")
        return []


async def admin_get_today_quizzes() -> List[Dict[str, Any]]:
    """جلب الكويزات المُولَّدة خلال آخر 24 ساعة (نافذة متحركة من اللحظة الحالية للخلف)
    وليس اليوم التقويمي منذ منتصف الليل - كي لا يُفقَد أي نشاط حصل قبل الساعة 12
    صباحاً بتوقيت سوريا (كان يُستثنى بالكامل بالمنطق السابق رغم كونه حديثاً فعلياً)."""
    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        start_of_today_utc = (now_utc - datetime.timedelta(hours=24)).isoformat()

        # 1. جلب كويزات آخر 24 ساعة (باستثناء الآدمن، أسوة ببقية دوال التحليلات)
        query = supabase.table("quizzes") \
            .select("id, source_title, created_at, creator_id") \
            .gte("created_at", start_of_today_utc)
        if ADMIN_ID:
            query = query.neq("creator_id", ADMIN_ID)
        res = await query \
            .order("created_at", desc=True) \
            .execute()
            
        quizzes = res.data or []
        if not quizzes:
            return []

        # 2. جلب بيانات الطلاب المنشئين بطلب آمن (بدون الاعتماد على اسم Foreign Key صريح)
        creator_ids = list({q["creator_id"] for q in quizzes if q.get("creator_id")})
        users_map = {}
        if creator_ids:
            users_res = await supabase.table("users") \
                .select("user_id, username, first_name, last_name") \
                .in_("user_id", creator_ids) \
                .execute()
            users_map = {u["user_id"]: u for u in (users_res.data or [])}

        # 3. دمج البيانات وتحويل التوقيت إلى توقيت سوريا (UTC+3) — بشكل موحّد ومقاوم للأخطاء
        for q in quizzes:
            cid = q.get("creator_id")
            q["users"] = users_map.get(cid, {})
            q["time_str"] = format_syria_time(q.get("created_at"), fmt="%I:%M %p (%Y-%m-%d)")

        return quizzes
    except Exception as e:
        log_error(logger, f"Error fetching today quizzes: {e}")
        return []


async def admin_get_user_quizzes(creator_id: int, limit: int = 5, offset: int = 0) -> tuple[List[Dict[str, Any]], int]:
    """جلب الكويزات الخاصة بطالب محدد مرتبة مع التصفح."""
    try:
        count_res = await supabase.table("quizzes") \
            .select("id", count="exact") \
            .eq("creator_id", creator_id) \
            .execute()
        
        total = count_res.count or 0

        res = await supabase.table("quizzes") \
            .select("id, source_title, created_at, likes, dislikes") \
            .eq("creator_id", creator_id) \
            .order("created_at", desc=True) \
            .range(offset, offset + limit - 1) \
            .execute()

        return res.data or [], total
    except Exception as e:
        log_error(logger, f"Error fetching user quizzes for {creator_id}: {e}")
        return [], 0


# 5️⃣ دالة تنظيف البيانات القديمة
async def auto_cleanup_old_analytics_data() -> None:
    """حذف سجلات الأحداث القديمة جداً والسيئة."""
    try:
        thirty_days_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).isoformat()
        three_days_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)).isoformat()
        
        await supabase.table("usage_events").delete().lt("created_at", thirty_days_ago).execute()

        deletable_ids = await _get_safe_to_delete_quiz_ids(three_days_ago)
        if deletable_ids:
            await supabase.table("quizzes").delete().in_("id", deletable_ids).execute()

        log_info(logger, f"Automated database cleanup executed successfully. Deleted {len(deletable_ids)} quizzes.")
    except Exception as e:
        log_error(logger, f"Error in database cleanup: {e}")

# ==============================================================================
# 🆕 Audio Web Upload (Telegram Mini App) - دوال التخزين المؤقت بـ Supabase Storage
# يستخدم نفس عميل supabase المُهيّأ أصلاً أعلى الملف (نفس نمط QUIZ_IMAGES_BUCKET)
# ==============================================================================

from constants import AUDIO_UPLOAD_BUCKET


async def create_audio_upload_target(user_id: int, file_extension: str = "") -> Optional[Dict[str, str]]:
    """
    يولّد مسار فريد ورابط رفع موقّع (signed upload URL) لملف صوتي مؤقت بـ bucket
    خاص (audio-temp). المسار مُصاغ بـ {user_id}/{uuid}{ext} حتى يسهل تتبعه أو حذفه
    يدوياً لو احتجنا (مثال: تنظيف طارئ لكل ملفات مستخدم معيّن).

    Returns:
        {"path": ..., "signed_url": ..., "token": ...} عند النجاح، أو None عند الفشل
        (مثال: الـ bucket غير موجود بعد بمشروع Supabase - يجب إنشاؤه يدوياً من لوحة
        التحكم أو عبر migration، راجع ملاحظة "خطوات الإعداد" أسفل الملف).
    """
    try:
        ext = file_extension if file_extension.startswith(".") else f".{file_extension}" if file_extension else ""
        object_path = f"{user_id}/{uuid.uuid4().hex}{ext}"
        result = await supabase.storage.from_(AUDIO_UPLOAD_BUCKET).create_signed_upload_url(object_path)
        # شكل الاستجابة حسب supabase-py 2.31.0 (تحقّق فعلي من الكود المصدري):
        # {"signed_url": ..., "signedUrl": ..., "token": ..., "path": ...}
        return {
            "path": result.get("path", object_path),
            "signed_url": result.get("signed_url") or result.get("signedUrl"),
            "token": result.get("token"),
        }
    except Exception as e:
        log_error(logger, f"Could not create signed audio upload URL: {e}")
        return None


async def get_audio_temp_object_size(object_path: str) -> Optional[int]:
    """
    🆕 يستعلم عن الحجم الفعلي (بالبايت) لملف مرفوع مسبقاً بـ bucket المؤقت، عبر
    metadata التي يرجعها Supabase Storage عند list() - دون تحميل أي محتوى فعلي.

    يُستخدم كخط دفاع أول (قبل التحميل) للتحقق من أن الحجم الفعلي للملف المرفوع
    عبر TUS مطابق فعلياً للحد الأقصى المسموح - لأن فحص `file_size` المُصرَّح به
    بمرحلة /api/audio-upload/init هو فحص من طرف العميل فقط (غير موثوق لوحده)،
    والرفع الفعلي بعدها يذهب مباشرة من متصفح المستخدم لـ Supabase دون المرور
    عبر سيرفرنا إطلاقاً.

    Returns:
        الحجم بالبايت عند النجاح، أو None لو تعذّر إيجاد الملف أو قراءة الـ metadata
        (يجب معاملة None كفشل تحقق - أي رفض الملف بدل السماح له بشكل متساهل).
    """
    try:
        folder, _, file_name = object_path.rpartition("/")
        entries = await supabase.storage.from_(AUDIO_UPLOAD_BUCKET).list(folder)
        for entry in entries:
            if entry.get("name") != file_name:
                continue
            metadata = entry.get("metadata") or {}
            size = metadata.get("size") or metadata.get("contentLength")
            return int(size) if size is not None else None
        return None
    except Exception as e:
        log_error(logger, f"Could not read size metadata for temp audio '{object_path}': {e}")
        return None


async def download_audio_temp_to_file(
    object_path: str, destination_path: str, max_size_bytes: Optional[int] = None
) -> bool:
    """
    يحمّل الملف الصوتي المؤقت من Supabase Storage إلى القرص المحلي (downloads/)
    تمهيداً لمعالجته بنفس مسار handle_audio_message الحالي (mutagen، ثم Gemini).

    🆕 max_size_bytes: خط دفاع ثانٍ (بعد get_audio_temp_object_size) - لو حُدِّد،
    يُرفض ويُحذف أي محتوى تم تحميله فعلياً يتجاوز هذا الحد، حتى لو تجاوز فحص
    الـ metadata لأي سبب (تعارض بين الحجم المُبلَّغ والحجم الفعلي مثلاً).
    """
    try:
        file_bytes = await supabase.storage.from_(AUDIO_UPLOAD_BUCKET).download(object_path)
        if max_size_bytes is not None and len(file_bytes) > max_size_bytes:
            log_error(
                logger,
                f"Downloaded audio '{object_path}' exceeds max allowed size "
                f"({len(file_bytes)} > {max_size_bytes} bytes) - rejecting.",
            )
            return False
        with open(destination_path, "wb") as f:
            f.write(file_bytes)
        return True
    except Exception as e:
        log_error(logger, f"Could not download temp audio '{object_path}' from storage: {e}")
        return False


async def delete_audio_temp(object_path: str) -> None:
    """
    حذف الملف الصوتي المؤقت من Supabase Storage فوراً بعد انتهاء المعالجة (نجاحاً
    أو فشلاً). يُستدعى دائماً من finally بمسار المعالجة - راجع
    handlers/audio.py::process_web_uploaded_audio. لا يرمي استثناء عند الفشل
    (السجل فقط) حتى لا يكسر تدفق الرد على الطالب لو تعذّر الحذف لأي سبب - شبكة
    التنظيف الدورية (scheduled cleanup) هي الخط الثاني للدفاع بهذه الحالة.
    """
    try:
        await supabase.storage.from_(AUDIO_UPLOAD_BUCKET).remove([object_path])
    except Exception as e:
        log_warning(logger, f"Could not delete temp audio '{object_path}' from storage (will rely on scheduled cleanup): {e}")


async def cleanup_stale_audio_uploads(older_than_seconds: int = 3600) -> int:
    """
    🆕 شبكة أمان: تُستدعى دورياً (راجع scheduled_cleanup_loop بـ webhook_server.py)
    لحذف أي ملفات صوتية مؤقتة تبقّت بالـ bucket لأكثر من ساعة - غالباً بسبب معالجة
    انقطعت بشكل استثنائي قبل الوصول لـ finally (كراش السيرفر نفسه مثلاً، وليس
    استثناء عادي بايثون يُلتقط). يمسح على مستوى كل مجلدات المستخدمين (list بمستوى
    الجذر أولاً، ثم كل مجلد مستخدم على حدة، لأن Storage API لا يدعم فحص عمق تعسفي
    بنداء واحد).

    Returns:
        عدد الملفات المحذوفة (للـ logging فقط).
    """
    deleted_count = 0
    try:
        user_folders = await supabase.storage.from_(AUDIO_UPLOAD_BUCKET).list()
        now = datetime.datetime.now(datetime.timezone.utc)
        for folder in user_folders:
            folder_name = folder.get("name")
            if not folder_name:
                continue
            try:
                files = await supabase.storage.from_(AUDIO_UPLOAD_BUCKET).list(folder_name)
            except Exception:
                continue
            stale_paths = []
            for file_info in files:
                created_at_raw = file_info.get("created_at")
                if not created_at_raw:
                    continue
                try:
                    created_at = datetime.datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if (now - created_at).total_seconds() > older_than_seconds:
                    stale_paths.append(f"{folder_name}/{file_info.get('name')}")
            if stale_paths:
                await supabase.storage.from_(AUDIO_UPLOAD_BUCKET).remove(stale_paths)
                deleted_count += len(stale_paths)
    except Exception as e:
        log_error(logger, f"Stale audio uploads cleanup failed: {e}")
    return deleted_count


# ==============================================================================
# 📋 خطوات إعداد يدوية لازمة مرة وحدة بلوحة تحكم Supabase (Storage):
# 1. أنشئ bucket جديد بالاسم "audio-temp" واختر Private (مو Public).
# 2. لا حاجة لأي RLS policy إضافية للرفع/التحميل عبر signed URLs بحد ذاتها، لكن
#    تأكد إن الـ service_role key المستخدم بـ create_async_client هو نفسه المستخدم
#    حالياً (نفس النمط الموجود أعلى الملف) لأن create_signed_upload_url ودوال
#    list/remove هذه تتطلب صلاحيات service_role وليس anon key.
# ==============================================================================


# ==============================================================================
# 🆕 File/Images Web Upload (Telegram Mini App) - نفس آلية Audio Web Upload فوق،
# مُعمَّمة لـ bucket ثاني (file-temp) تُستخدم له مستندات كبيرة (حتى 150 صفحة/100MB)
# وألبومات صور كبيرة (حتى 50 صورة). بدل تكرار كل الدوال من الصفر، الدوال هون بتاخد
# bucket كباراميتر وتُبنى فوقها أغلفة (wrappers) رقيقة بأسماء واضحة لكل استخدام -
# نفس منطق دوال audio_temp أعلاه تماماً، فقط أُعيد استخدامه بدل تكراره حرفياً.
# ==============================================================================

from constants import FILE_UPLOAD_BUCKET


async def _create_signed_upload_target(bucket: str, object_path: str) -> Optional[Dict[str, str]]:
    """يولّد رابط رفع موقّع (signed upload URL) لمسار مُعطى مسبقاً بـ bucket مُعطى."""
    try:
        result = await supabase.storage.from_(bucket).create_signed_upload_url(object_path)
        return {
            "path": result.get("path", object_path),
            "signed_url": result.get("signed_url") or result.get("signedUrl"),
            "token": result.get("token"),
        }
    except Exception as e:
        log_error(logger, f"Could not create signed upload URL for '{bucket}/{object_path}': {e}")
        return None


async def create_file_upload_target(user_id: int, file_extension: str = "") -> Optional[Dict[str, str]]:
    """نظير create_audio_upload_target لمستند مرفوع عبر صفحة الويب - مسار فريد
    {user_id}/{uuid}{ext} بـ bucket الملفات (FILE_UPLOAD_BUCKET)."""
    ext = file_extension if file_extension.startswith(".") else f".{file_extension}" if file_extension else ""
    object_path = f"{user_id}/{uuid.uuid4().hex}{ext}"
    return await _create_signed_upload_target(FILE_UPLOAD_BUCKET, object_path)


async def create_image_upload_targets(user_id: int, file_extensions: List[str]) -> Optional[List[Dict[str, str]]]:
    """
    🆕 يولّد دفعة روابط رفع موقّعة لعدة صور سوا (ألبوم كبير) تحت نفس مجلد الجلسة
    ({user_id}/{session_uuid}/{index}{ext}) - مجلد مشترك واحد لكل الصور يسهّل تتبعها/
    حذفها كمجموعة واحدة لاحقاً (delete_file_temp لكل مسار، أو التنظيف الدوري).
    يرجع None بالكامل لو فشل أي رابط توقيع واحد (فشل جزئي غير مقبول هون - إما كل
    الألبوم جاهز للرفع أو ولا شي، تفادياً لصور "يتيمة" بلا بقية الدفعة).
    """
    session_id = uuid.uuid4().hex
    targets: List[Dict[str, str]] = []
    for index, file_extension in enumerate(file_extensions):
        ext = file_extension if file_extension.startswith(".") else f".{file_extension}" if file_extension else ".jpg"
        object_path = f"{user_id}/{session_id}/{index}{ext}"
        target = await _create_signed_upload_target(FILE_UPLOAD_BUCKET, object_path)
        if not target:
            return None
        targets.append(target)
    return targets


async def get_file_temp_object_size(object_path: str) -> Optional[int]:
    """نظير get_audio_temp_object_size لـ bucket الملفات."""
    try:
        folder, _, file_name = object_path.rpartition("/")
        entries = await supabase.storage.from_(FILE_UPLOAD_BUCKET).list(folder)
        for entry in entries:
            if entry.get("name") != file_name:
                continue
            metadata = entry.get("metadata") or {}
            size = metadata.get("size") or metadata.get("contentLength")
            return int(size) if size is not None else None
        return None
    except Exception as e:
        log_error(logger, f"Could not read size metadata for temp file '{object_path}': {e}")
        return None


async def download_file_temp_to_file(
    object_path: str, destination_path: str, max_size_bytes: Optional[int] = None
) -> bool:
    """نظير download_audio_temp_to_file لـ bucket الملفات."""
    try:
        file_bytes = await supabase.storage.from_(FILE_UPLOAD_BUCKET).download(object_path)
        if max_size_bytes is not None and len(file_bytes) > max_size_bytes:
            log_error(
                logger,
                f"Downloaded file '{object_path}' exceeds max allowed size "
                f"({len(file_bytes)} > {max_size_bytes} bytes) - rejecting.",
            )
            return False
        with open(destination_path, "wb") as f:
            f.write(file_bytes)
        return True
    except Exception as e:
        log_error(logger, f"Could not download temp file '{object_path}' from storage: {e}")
        return False


async def delete_file_temp(object_path: str) -> None:
    """نظير delete_audio_temp لـ bucket الملفات. لا يرمي استثناء عند الفشل (تنظيف
    السجل فقط) - نفس مبدأ الدالة الأصلية بالضبط."""
    try:
        await supabase.storage.from_(FILE_UPLOAD_BUCKET).remove([object_path])
    except Exception as e:
        log_warning(logger, f"Could not delete temp file '{object_path}' from storage (will rely on scheduled cleanup): {e}")


async def delete_file_temp_batch(object_paths: List[str]) -> None:
    """🆕 حذف دفعة مسارات سوا (ألبوم صور كامل) بنداء واحد بدل حلقة نداءات منفصلة."""
    if not object_paths:
        return
    try:
        await supabase.storage.from_(FILE_UPLOAD_BUCKET).remove(object_paths)
    except Exception as e:
        log_warning(logger, f"Could not delete temp file batch from storage (will rely on scheduled cleanup): {e}")


async def cleanup_stale_file_uploads(older_than_seconds: int = 3600) -> int:
    """نظير cleanup_stale_audio_uploads لـ bucket الملفات - بيمشي على مستوى مجلدات
    المستخدمين، وبداخل كل مجلد مستخدم بيفحص أيضاً مجلدات الجلسات الفرعية لألبومات
    الصور (user_id/session_uuid/*) بالإضافة للملفات المباشرة (user_id/*)."""
    deleted_count = 0
    try:
        user_folders = await supabase.storage.from_(FILE_UPLOAD_BUCKET).list()
        now = datetime.datetime.now(datetime.timezone.utc)
        for folder in user_folders:
            folder_name = folder.get("name")
            if not folder_name:
                continue
            try:
                entries = await supabase.storage.from_(FILE_UPLOAD_BUCKET).list(folder_name)
            except Exception:
                continue
            stale_paths = []
            for entry in entries:
                entry_name = entry.get("name")
                if not entry_name:
                    continue
                # عنصر بلا "id" بالـ metadata عادةً مجلد فرعي (جلسة ألبوم صور) وليس ملفاً
                if entry.get("id") is None and entry.get("metadata") is None:
                    try:
                        sub_entries = await supabase.storage.from_(FILE_UPLOAD_BUCKET).list(f"{folder_name}/{entry_name}")
                    except Exception:
                        continue
                    for sub_entry in sub_entries:
                        created_at_raw = sub_entry.get("created_at")
                        if not created_at_raw:
                            continue
                        try:
                            created_at = datetime.datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        if (now - created_at).total_seconds() > older_than_seconds:
                            stale_paths.append(f"{folder_name}/{entry_name}/{sub_entry.get('name')}")
                    continue
                created_at_raw = entry.get("created_at")
                if not created_at_raw:
                    continue
                try:
                    created_at = datetime.datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if (now - created_at).total_seconds() > older_than_seconds:
                    stale_paths.append(f"{folder_name}/{entry_name}")
            if stale_paths:
                await supabase.storage.from_(FILE_UPLOAD_BUCKET).remove(stale_paths)
                deleted_count += len(stale_paths)
    except Exception as e:
        log_error(logger, f"Stale file uploads cleanup failed: {e}")
    return deleted_count


# ==============================================================================
# 📋 خطوة إعداد يدوية إضافية بلوحة تحكم Supabase (Storage):
# أنشئ bucket ثانٍ بالاسم "file-temp" (Private) - نفس خطوات "audio-temp" فوق بالضبط.
# ==============================================================================
