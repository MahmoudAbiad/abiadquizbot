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

