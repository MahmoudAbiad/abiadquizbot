"""
services/points_calculator_service.py
==============================================================================
خدمة حاسبة النقاط الذكية والباقات المخصصة:
  1) الشق الأول: شرح مبسط وواضح لسعر السؤال والصفحة وآلية الحساب مع أمثلة عملية.
  2) الشق الثاني:
     - فحص نشاط المستخدم في آخر 14 يوماً من جدول wallet_debits (لا يظهر خيار الحساب
       التلقائي إلا للمستخدم النشط الذي لديه سجل استخدام فعلي).
     - حساب الاستهلاك يدوياً (بأزرار تفاعلية سريعة أو إدخال نصي مباشر) أو تلقائياً من
       سجل الاستخدام الفعلي.
     - اقتراح باقة شحن مخصصة (ليست محصورة بالباقات الثابتة) مع حساب خصم الكمية
       التدريجي وتوضيح مقدار التوفير بالليرة السورية ونسبة الخصم، وإمكانية المتابعة
       السلسة مباشرة إلى خطوة الشحن.
==============================================================================
"""

import datetime
import math
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from constants import (
    DISCOUNT_RATE_FOR_CACHED,
    MAX_ALBUM_IMAGES,
    MAX_LIMIT_PAGES,
    MAX_STANDARD_PAGES,
    MAX_STANDARD_QUESTIONS,
)
from helpers.points_calculator import (
    AUDIO_OVERAGE_RATE,
    AUDIO_STANDARD_MINUTES,
    calculate_quiz_points_cost,
)
from helpers.settings_helper import format_setting_value, get_setting
from helpers.supabase_helper import supabase
from logger import get_logger

logger = get_logger(__name__)

# ==================== ثوابت التسعير والنشاط ====================

# السعر الأساسي للنقطة الواحدة بالليرة السورية (مشتق من باقة البداية: 200 نقطة = 65 ل.س -> 0.325 ل.س/نقطة)
BASE_POINT_PRICE_SYP = Decimal("0.325")
DEFAULT_CURRENCY = "SYP"

# حدود الباقات المخصصة المسموح بها
MIN_CUSTOM_PACKAGE_POINTS = 100
MAX_CUSTOM_PACKAGE_POINTS = 10000
CUSTOM_PACKAGE_STEP = 50

# نافذة فحص نشاط المستخدم للحساب التلقائي (بالأيام) ومعايير الأهلية
ACTIVITY_WINDOW_DAYS = 14
MIN_ACTIVE_OPERATIONS = 2
MIN_ACTIVE_POINTS_SPENT = 30.0

# شرائح خصم الكمية التدريجي (تبدأ من السعر الأساسي 0.325 ل.س/نقطة وتتدرج مع زيادة النقاط):
# - 100..249 نقطة: 0% خصم (0.325 ل.س/نقطة — مثال: 200 نقطة = 65 ل.س)
# - 250..349 نقطة: 10% خصم (0.2925 ل.س/نقطة — مثال: 300 نقطة = 88 ل.س بدل 98 ل.س)
# - 350..499 نقطة: 15% خصم (0.27625 ل.س/نقطة — مثال: 400 نقطة = 111 ل.س بدل 130 ل.س)
# - 500..1000 نقطة: 20% خصم (0.260 ل.س/نقطة — مطابق لباقتي 500 نقطة = 130 ل.س و1000 نقطة = 260 ل.س)
# - 1001..1499 نقطة: 22% خصم (0.2535 ل.س/نقطة)
# - 1500..2499 نقطة: 25% خصم (0.24375 ل.س/نقطة)
# - 2500+ نقطة: 30% خصم (0.2275 ل.س/نقطة)
VOLUME_DISCOUNT_TIERS: List[Tuple[int, int]] = [
    (2500, 30),
    (1500, 25),
    (1001, 22),
    (500, 20),
    (350, 15),
    (250, 10),
    (0, 0),
]


def normalize_custom_points(raw_points: float) -> int:
    """تقريب عدد النقاط المطلوب لأقرب مضاعف 50 نقطة (أو 25 للشرائح الصغيرة) ضمن الحدود المسموحة."""
    pts = max(1, int(math.ceil(float(raw_points or 0))))
    if pts <= MIN_CUSTOM_PACKAGE_POINTS:
        return MIN_CUSTOM_PACKAGE_POINTS
    if pts >= MAX_CUSTOM_PACKAGE_POINTS:
        return MAX_CUSTOM_PACKAGE_POINTS
    step = 25 if pts < 300 else CUSTOM_PACKAGE_STEP
    rounded = int(math.ceil(pts / step) * step)
    return max(MIN_CUSTOM_PACKAGE_POINTS, min(MAX_CUSTOM_PACKAGE_POINTS, rounded))


def get_volume_discount_percent(points: int) -> int:
    """إرجاع نسبة خصم الكمية المئوية (0..30%) حسب عدد نقاط الباقة."""
    pts = max(0, int(points))
    for min_pts, pct in VOLUME_DISCOUNT_TIERS:
        if pts >= min_pts:
            return pct
    return 0


def build_custom_package_quote(points: int, currency: str = DEFAULT_CURRENCY) -> Dict[str, Any]:
    """حساب تسعيرة باقة مخصصة شاملة السعر قبل الخصم، السعر النهائي، قيمة التوفير، وتلميح الشريحة الأعلى."""
    pts = max(MIN_CUSTOM_PACKAGE_POINTS, min(MAX_CUSTOM_PACKAGE_POINTS, int(points)))
    discount_pct = get_volume_discount_percent(pts)

    raw_base_price = Decimal(pts) * BASE_POINT_PRICE_SYP
    base_price = raw_base_price.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    discount_factor = (Decimal(100 - discount_pct)) / Decimal(100)
    final_price = (raw_base_price * discount_factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if final_price < Decimal("1"):
        final_price = Decimal("1")

    saved_amount = max(Decimal("0"), base_price - final_price)

    # حساب تلميح الشريحة الأعلى التالية لتشجيع المستخدم وتوضيح التوفير الإضافي
    next_tier_hint: Optional[Dict[str, Any]] = None
    for min_pts, pct in sorted(VOLUME_DISCOUNT_TIERS, key=lambda t: t[0]):
        if min_pts > pts and pct > discount_pct:
            target_pts = 500 if min_pts == 500 else (1500 if min_pts == 1001 else min_pts)
            raw_next_base = Decimal(target_pts) * BASE_POINT_PRICE_SYP
            next_base = raw_next_base.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            next_final = (raw_next_base * Decimal(100 - pct) / Decimal(100)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
            next_saved = max(Decimal("0"), next_base - next_final)
            next_tier_hint = {
                "points": target_pts,
                "discount_pct": pct,
                "base_price": int(next_base),
                "price": int(next_final),
                "saved": int(next_saved),
                "currency": currency,
            }
            break

    return {
        "id": None,
        "points": pts,
        "base_price": int(base_price),
        "price": int(final_price),
        "saved": int(saved_amount),
        "discount_pct": discount_pct,
        "currency": currency,
        "unit_price": float(( Decimal(final_price) / Decimal(pts) ).quantize(Decimal("0.001"))),
        "next_tier_hint": next_tier_hint,
        "is_custom": True,
    }


# ==================== الشق الأول: شرح آلية الحساب وأسعار النقاط ====================

async def build_pricing_explanation_text() -> str:
    """بناء شرح مبسط وواضح لسعر السؤال والصفحة وآلية حساب النقاط في البوت."""
    std_rate = float(await get_setting("quiz_standard_rate"))
    over_rate = float(await get_setting("quiz_overage_rate"))
    daily_free = float(await get_setting("daily_renewal_points"))

    std_str = format_setting_value(std_rate)
    over_str = format_setting_value(over_rate)
    daily_str = format_setting_value(daily_free)
    cached_pct = int(round((1.0 - DISCOUNT_RATE_FOR_CACHED) * 100))

    example_normal = round(5 * std_rate + 10 * std_rate, 2)
    example_cached = round(example_normal * DISCOUNT_RATE_FOR_CACHED, 2)

    lines = [
        "💡 <b>كيف تُحسب النقاط في البوت؟ (بكل بساطة)</b>",
        "",
        "تُحسب تكلفة أي اختبار بجمع <b>سعر المحتوى (الصفحات/الصور) + سعر الأسئلة</b>:",
        "",
        "1️⃣ <b>الوضع العادي (الأكثر استخداماً):</b>",
        f"• كل صفحة (أو صورة) حتى {MAX_STANDARD_PAGES} صفحة = <b>{std_str} نقطة</b>",
        f"• كل سؤال حتى {MAX_STANDARD_QUESTIONS} سؤالاً = <b>{std_str} نقطة</b>",
        f"📌 <i>مثال:</i> ملف من 5 صفحات + 10 أسئلة = <code>{example_normal:g}</code> نقطة فقط.",
        "",
        "2️⃣ <b>الطلبات الموسّعة والفائقة (السوبر):</b>",
        f"• ما يزيد عن {MAX_STANDARD_PAGES} صفحة (حتى {MAX_LIMIT_PAGES}) أو عن {MAX_STANDARD_QUESTIONS} سؤالاً يُحسب بـ <b>{over_str} نقطة</b> للوحدة الإضافية.",
        f"• الملفات الضخمة (أكثر من {MAX_LIMIT_PAGES} صفحة أو ألبوم فوق {MAX_ALBUM_IMAGES} صور) تُعالج بالتوازي الفائق بسعر <b>{over_str} نقطة</b> لكل صفحة وسؤال.",
        "",
        "3️⃣ <b>توفير إضافي ومزايا مجانية:</b>",
        f"• ♻️ <b>الكويزات الجاهزة لنفس الملف:</b> خصم <b>{cached_pct}%</b> (مثلاً بدل {example_normal:g} نقطة تدفع <code>{example_cached:g}</code> نقطة فقط!).",
        f"• 🎙️ <b>تفريغ المحاضرات الصوتية:</b> أول {AUDIO_STANDARD_MINUTES} دقائق بـ <b>1 نقطة/دقيقة</b>، وما بعدها بـ <b>{AUDIO_OVERAGE_RATE:g} نقطة/دقيقة</b>.",
        f"• 🔄 <b>التجديد اليومي المجاني:</b> يتجدد رصيدك المجاني تلقائياً ليصل إلى <b>{daily_str} نقطة يومياً</b>.",
        "",
        "👇 <b>انتقل الآن للشق الثاني لتحديد حجم استهلاكك والحصول على باقة مخصصة بخصم كمية:</b>",
    ]
    return "\n".join(lines)


# ==================== الشق الثاني: فحص نشاط المستخدم والحساب التلقائي ====================

def _extract_debit_net_points(row: Dict[str, Any]) -> float:
    """حساب صافي النقاط المستهلكة من صف واحد في جدول wallet_debits بعد حسم أي استرجاع."""
    refunded_ratio = float(row.get("refunded_ratio") or 0.0)
    if refunded_ratio >= 1.0:
        return 0.0
    free_amt = float(row.get("free_amount") or 0.0)
    paid_amt = float(row.get("paid_amount") or 0.0)
    gifts_raw = row.get("gifts") or []
    gift_amt = 0.0
    if isinstance(gifts_raw, list):
        for g in gifts_raw:
            if isinstance(g, dict):
                gift_amt += float(g.get("amount") or 0.0)
    gross = max(0.0, free_amt + paid_amt + gift_amt)
    return round(gross * (1.0 - max(0.0, min(1.0, refunded_ratio))), 2)


async def get_user_activity_summary(
    user_id: int, window_days: int = ACTIVITY_WINDOW_DAYS
) -> Dict[str, Any]:
    """فحص سجل استهلاك المستخدم خلال آخر `window_days` يوماً من جدول wallet_debits.

    يُعتبر المستخدم نشطاً (`is_active=True`) إذا حقق خلال الفترة:
      - عمليتي استخدام فعليتين على الأقل (`valid_ops >= MIN_ACTIVE_OPERATIONS`)، أو
      - إجمالي استهلاك لا يقل عن `MIN_ACTIVE_POINTS_SPENT` (30 نقطة).
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff_iso = (now_utc - datetime.timedelta(days=window_days)).isoformat()
    syria_tz = ZoneInfo("Asia/Damascus")

    try:
        res = await (
            supabase.table("wallet_debits")
            .select("free_amount,paid_amount,gifts,refunded_ratio,created_at")
            .eq("user_id", int(user_id))
            .gte("created_at", cutoff_iso)
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        )
        rows = res.data or []
    except Exception as exc:
        logger.warning(f"get_user_activity_summary failed for user {user_id}: {exc}")
        rows = []

    valid_ops = 0
    total_spent = 0.0
    active_dates = set()
    oldest_dt: Optional[datetime.datetime] = None

    for row in rows:
        net = _extract_debit_net_points(row)
        if net <= 0:
            continue
        valid_ops += 1
        total_spent += net
        raw_created = row.get("created_at")
        if raw_created:
            try:
                dt = (
                    raw_created
                    if isinstance(raw_created, datetime.datetime)
                    else datetime.datetime.fromisoformat(str(raw_created).replace("Z", "+00:00"))
                )
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                local_dt = dt.astimezone(syria_tz)
                active_dates.add(local_dt.date())
                if oldest_dt is None or dt < oldest_dt:
                    oldest_dt = dt
            except Exception:
                pass

    total_spent = round(total_spent, 2)
    active_days_count = max(1, len(active_dates)) if valid_ops > 0 else 0
    if oldest_dt is not None:
        span_days = max(1, int(math.ceil((now_utc - oldest_dt).total_seconds() / 86400.0)))
    else:
        span_days = 0

    is_active = (valid_ops >= MIN_ACTIVE_OPERATIONS) or (total_spent >= MIN_ACTIVE_POINTS_SPENT)

    # متوسط الاستهلاك باليوم النشط ومتوسط الاستهلاك اليومي العام
    avg_per_active_day = round(total_spent / active_days_count, 1) if active_days_count > 0 else 0.0
    effective_window = max(3, min(window_days, max(span_days, active_days_count))) if is_active else window_days
    daily_rate = round(total_spent / effective_window, 1) if is_active else 0.0
    avg_per_op = round(total_spent / valid_ops, 1) if valid_ops > 0 else 0.0

    return {
        "is_active": is_active,
        "window_days": window_days,
        "valid_ops": valid_ops,
        "total_spent": total_spent,
        "active_days": active_days_count,
        "span_days": span_days,
        "avg_per_active_day": avg_per_active_day,
        "daily_rate": daily_rate,
        "avg_per_op": avg_per_op,
    }


def build_auto_recommendation(activity: Dict[str, Any], target_days: int = 14) -> Dict[str, Any]:
    """حساب الباقة المخصصة المقترحة بناءً على سجل نشاط المستخدم الفعلي وعدد أيام التغطية المطلوبة."""
    target_days = max(7, min(90, int(target_days)))
    total_spent = float(activity.get("total_spent") or 0.0)
    active_days = max(1, int(activity.get("active_days") or 1))
    daily_rate = float(activity.get("daily_rate") or 0.0)
    avg_per_active_day = float(activity.get("avg_per_active_day") or 0.0)

    # تقدير ذكي يدمج المعدل اليومي المستمر مع كثافة أيام الدراسة الفعلية للطالب
    blended_daily = max(daily_rate, avg_per_active_day * min(1.0, (active_days + 1) / 7.0))
    projected_points = max(total_spent * (target_days / max(7.0, float(activity.get("window_days") or 14))),
                           blended_daily * target_days)
    recommended_points = normalize_custom_points(projected_points)
    quote = build_custom_package_quote(recommended_points)
    return {
        "target_days": target_days,
        "projected_points": int(math.ceil(projected_points)),
        "quote": quote,
        "activity": activity,
    }


# ==================== الشق الثاني: الحساب اليدوي ====================

async def build_manual_recommendation(
    pages: int = 15, questions: int = 20, quizzes_count: int = 10
) -> Dict[str, Any]:
    """حساب استهلاك الطالب يدوياً بناءً على (عدد الصفحات × عدد الأسئلة × عدد الاختبارات) واقتراح باقة مخصصة."""
    pages = max(1, min(150, int(pages)))
    questions = max(1, min(120, int(questions)))
    quizzes_count = max(1, min(200, int(quizzes_count)))

    cost_per_quiz = await calculate_quiz_points_cost(pages, questions, is_album=False)
    raw_total_points = round(cost_per_quiz * quizzes_count, 2)
    recommended_points = normalize_custom_points(raw_total_points)
    quote = build_custom_package_quote(recommended_points)

    return {
        "pages": pages,
        "questions": questions,
        "quizzes_count": quizzes_count,
        "cost_per_quiz": cost_per_quiz,
        "raw_total_points": raw_total_points,
        "quote": quote,
    }


def parse_custom_manual_input(text: str) -> Optional[Dict[str, int]]:
    """تحليل مدخلات الطالب النصية في الحاسبة اليدوية:
    - إما رقم واحد يمثل عدد النقاط المراد شحنها مباشرة (مثال: `450` أو `750 نقطة`)
    - أو 3 أرقام تمثل: `صفحات أسئلة اختبارات` (مثال: `10 20 8` أو `10، 20، 8`)
    """
    cleaned = (text or "").strip()
    # استخراج الأرقام العربية أو الإنجليزية
    trans = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    cleaned = cleaned.translate(trans)
    numbers = [int(n) for n in re.findall(r"\d+", cleaned)]
    if not numbers:
        return None
    if len(numbers) == 1:
        pts = numbers[0]
        if not (50 <= pts <= MAX_CUSTOM_PACKAGE_POINTS):
            return None
        return {"mode": "direct_points", "points": pts}
    if len(numbers) >= 3:
        pages, questions, quizzes_count = numbers[0], numbers[1], numbers[2]
        if not (1 <= pages <= 150 and 1 <= questions <= 120 and 1 <= quizzes_count <= 200):
            return None
        return {
            "mode": "breakdown",
            "pages": pages,
            "questions": questions,
            "quizzes_count": quizzes_count,
        }
    return None


# ==================== تنسيق نصوص العرض للطالب ====================

def format_quote_block(quote: Dict[str, Any], title: str = "🎁 الباقة المخصصة المقترحة لك") -> List[str]:
    """تنسيق بطاقة الباقة المخصصة مع توضيح السعر الأصلي، خصم الكمية، ومقدار التوفير بالليرة السورية."""
    pts = quote["points"]
    base_price = quote["base_price"]
    price = quote["price"]
    saved = quote["saved"]
    discount_pct = quote["discount_pct"]
    unit = "ل.س" if quote.get("currency", "SYP") == "SYP" else "$"

    lines = [
        f"<b>{title}:</b>",
        f"📦 الكمية: <b>{pts} نقطة</b>",
    ]
    if discount_pct > 0 and saved > 0:
        lines.extend([
            f"🏷️ السعر الأساسي قبل الخصم: <s>{base_price} {unit}</s>",
            f"✨ <b>السعر بعد خصم الكمية ({discount_pct}%): {price} {unit}</b>",
            f"💰 <b>توفيرك المباشر في هذه الباقة: {saved} {unit}!</b>",
        ])
    else:
        lines.extend([
            f"💵 السعر: <b>{price} {unit}</b> (بالسعر الأساسي — يبدأ خصم الكمية من 250 نقطة فأعلى)",
        ])

    hint = quote.get("next_tier_hint")
    if hint:
        lines.append(
            f"💡 <i>معلومة توفير:</i> لو شحنت <b>{hint['points']} نقطة</b> ستحصل على "
            f"<b>خصم {hint['discount_pct']}%</b> (بسعر <b>{hint['price']} {unit}</b> بدل {hint['base_price']} {unit}) "
            f"وتوفّر <b>{hint['saved']} {unit}</b>!"
        )
    return lines
