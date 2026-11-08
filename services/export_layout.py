"""تخطيط الصفحات لخيار «عدد الأسئلة في الصفحة» - دوال نقيّة قابلة للاختبار.

القاعدة: الرقم الذي يختاره المعلم هو **سقف** لا حصة ثابتة. إن لم تتسع الأسئلة المطلوبة
في صفحة واحدة (أسئلة طويلة/خيارات كثيرة/جداول) توضع أسئلة أقل في تلك الصفحة، فلا يوجد
تزاحم ولا سؤال مقصوص. وسؤال أطول من صفحة كاملة يأخذ صفحته ويكمل بنفس آلية المتابعة
القديمة (نص/كتل بحدود ارتفاع) ثم تبدأ المجموعة التالية بصفحة جديدة.
"""
from math import ceil
from typing import List, Sequence

from services.export_options import MAX_PER_PAGE, MIN_PER_PAGE

# أقصى فراغ إضافي يُوزَّع بين الأسئلة عند تحديد عدد ثابت بالصفحة (نقطة طباعة)
MAX_SPREAD_GAP = 42.0


def plan_pages(heights: Sequence[float], per_page: int, first_capacity: float,
               capacity: float) -> List[List[int]]:
    """يجمّع فهارس الأسئلة في صفحات: حتى per_page سؤالاً وبشرط أن يتسع مجموع الارتفاعات."""
    per_page = max(MIN_PER_PAGE, int(per_page))
    pages: List[List[int]] = []
    current: List[int] = []
    used = 0.0
    cap = first_capacity
    for index, height in enumerate(heights):
        if current and (len(current) >= per_page or used + height > cap):
            pages.append(current)
            current, used, cap = [], 0.0, capacity
        current.append(index)
        used += height
        if len(current) == 1 and height > cap:
            # سؤال أطول من صفحة: وحده، ويكمل عبر الصفحات بآليته، والتالي يبدأ صفحة جديدة.
            pages.append(current)
            current, used, cap = [], 0.0, capacity
    if current:
        pages.append(current)
    return pages


def spread_gap(free: float, count: int, cap: float = MAX_SPREAD_GAP) -> float:
    """الفراغ الإضافي بعد كل سؤال لتبدو الصفحة ورقة امتحان متوازنة (بحد أقصى)."""
    if count < 1 or free <= 0:
        return 0.0
    return max(0.0, min(cap, free / count))


def pick_max_per_page(heights: Sequence[float], capacity: float,
                      hard_max: int = MAX_PER_PAGE) -> int:
    """أعلى عدد مريح بالصفحة لكويز: يعتمد على السؤال «النموذجي» (الشريحة 75%) لا على
    أطول سؤال شاذ، لأن المخطط نفسه يخفّض العدد تلقائياً بالصفحات التي فيها أسئلة أطول."""
    if not heights or capacity <= 0:
        return hard_max
    ordered = sorted(heights)
    typical = ordered[min(len(ordered) - 1, ceil(0.75 * len(ordered)) - 1)]
    fit = int(capacity // max(typical, 1.0))
    return max(MIN_PER_PAGE, min(hard_max, fit))
