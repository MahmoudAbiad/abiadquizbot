"""
supabase_helper.users
======================
User Management: تسجيل/تحديث بيانات المستخدم، التجديد اليومي، ومكافآت الإحالة.
"""

import asyncio
import datetime
from typing import Optional, Dict, Any

from ._client import supabase, logger, log_error, log_warning, log_info
from .analytics import log_usage_event
from validators import validate_user_id
from settings_helper import get_setting


def _balance_payload(free_points: Any = 0, paid_points: Any = 0, **extra: Any) -> Dict[str, Any]:
    """Expose split balances while retaining ``points`` for older callers."""
    free = float(free_points or 0)
    paid = float(paid_points or 0)
    return {"free_points": free, "paid_points": paid, "points": free + paid, **extra}

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
    
async def update_user_stats(user_id: int, points_to_deduct: float, questions_generated: Optional[int] = None) -> Optional[Dict[str, float]]:
    """
    🆕 [طبقة 2] الدالة كانت ترجع رقم واحد (الرصيد المتبقي) وترمي معلومة التقسيم
    اللي أصلاً محسوبة داخل deduct_user_points_atomic (كم اتخصم من free_points
    وكم من paid_points). هلق بترجع التقسيم كامل، لأنه هاي المعلومة لازم تُحفظ
    بالـ state لحظة الخصم (debited_free/debited_paid) عشان أي ريفوند لاحق يرجع
    كل جزء لمصدره الصحيح بدل ما يرجع الكل لـpaid_points (كان عم "يُرقّي" نقاط
    مجانية مؤقتة لنقاط مدفوعة دائمة عند كل فشل).

    ⚠️ Breaking change: القيمة المرجعة صارت dict {remaining_points, debited_free,
    debited_paid} بدل float. كل الأماكن اللي بتنادي هاي الدالة (services/quiz_service.py،
    handlers/files.py، handlers/audio.py) لازم تتحدّث بنفس الوقت لتقرأ الشكل الجديد
    وتخزّن debited_free/debited_paid بدل debited_cost بس - غير هيك رح تنكسر.
    """
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

        if not rpc_response.data:
            # المستخدم غير موجود، أو رصيده الإجمالي أقل من points_to_deduct
            # (الدالة الذرية بترجع نتيجة فاضية بالحالتين - RETURN بلا QUERY)
            return None

        row = rpc_response.data[0] if isinstance(rpc_response.data, list) else rpc_response.data
        return {
            "remaining_points": float(row["remaining_points"]),
            "debited_free": float(row["debited_free"]),
            "debited_paid": float(row["debited_paid"]),
        }
    except Exception as e:
        log_error(logger, f"Error updating user stats via RPC: {e}", exception=e)
        return None

async def refund_user_points_split(user_id: int, free_amount: float, paid_amount: float) -> bool:
    """
    🆕 [طبقة 2/3] بديل split-aware عن refund_user_points: بيرجع كل جزء لمصدره
    الصحيح (free_amount → free_points، paid_amount → paid_points) بدل ما يرمي
    الكل بـpaid_points. القيم المتوقّعة هون هي نفسها debited_free/debited_paid
    المحفوظة بالـ state من نتيجة update_user_stats وقت الخصم.

    ملاحظة لريفوند جزئي (زي partial_refund_amount = cost * 0.5 بـhandlers/audio.py):
    نفس النسبة لازم تنطبق على الشقّين (free_amount * النسبة، paid_amount * النسبة)
    عشان التوزيع النسبي يضل صحيح - هاي نقطة قرار محتاجة نحسمها سوا بالطبقة الثالثة.
    """
    try:
        free_amount = max(float(free_amount or 0), 0.0)
        paid_amount = max(float(paid_amount or 0), 0.0)
        if free_amount <= 0 and paid_amount <= 0:
            return True
        is_valid, error = validate_user_id(user_id)
        if not is_valid:
            return False

        rpc_response = await supabase.rpc("refund_user_points_split_atomic", {
            "target_user_id": user_id,
            "free_amount": free_amount,
            "paid_amount": paid_amount,
        }).execute()

        if not rpc_response.data:
            # المستخدم غير موجود بالجدول أصلاً
            return False

        log_info(logger, f"Refunded (free={free_amount}, paid={paid_amount}) points to user {user_id}")
        return True
    except Exception as e:
        log_error(logger, f"Error refunding split points for user {user_id}: {e}", exception=e)
        return False

async def refund_user_points(user_id: int, points_to_refund: float) -> bool:
    """
    ⚠️ الشكل القديم (كل الريفوند بيروح لـpaid_points بالكامل) - متروك مؤقتاً
    لأي مكان بالكود بيعمل ريفوند بدون ما يعرف أصلاً وين اتخصمت النقاط (مثلاً
    تصحيح إداري يدوي). بمجرد ما نحدّث نداءات quiz_service.py/handlers/audio.py/
    handlers/files.py لتستخدم refund_user_points_split أعلاه، هاي الدالة رح
    تضل بس fallback للحالات اللي فعلاً ما عندها تقسيم معروف.
    """
    try:
        if points_to_refund <= 0:
            return True
        is_valid, error = validate_user_id(user_id)
        if not is_valid:
            return False

        # 🆕 يُنفَّذ عبر RPC ذري (UPDATE ... SET paid_points = paid_points + amount) بدل
        # قراءة الرصيد ثم كتابته من بايثون - نفس نمط award_referral_bonus_atomic.
        # القراءة-ثم-الكتابة كانت عرضة لفقدان تحديثات (lost update) لو صار خصم
        # واسترجاع نقاط لنفس المستخدم بشكل شبه متزامن (مثلاً استرجاع بسبب فشل توليد
        # كويز بالتزامن مع خصم كويز آخر يولّده نفس المستخدم بجلسة ثانية).
        rpc_response = await supabase.rpc("refund_user_points_atomic", {
            "target_user_id": user_id,
            "points_to_refund": float(points_to_refund),
        }).execute()

        if rpc_response.data is None:
            # المستخدم غير موجود بالجدول أصلاً
            return False

        log_info(logger, f"Refunded {points_to_refund} points to user {user_id}")
        return True
    except Exception as e:
        log_error(logger, f"Error refunding points for user {user_id}: {e}", exception=e)
        return False

