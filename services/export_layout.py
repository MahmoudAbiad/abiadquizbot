"""تخطيط الصفحات لخيار «عدد الأسئلة في الصفحة» - دوال نقيّة قابلة للاختبار.

القاعدة: الرقم الذي يختاره المعلم هو **سقف** لا حصة ثابتة. إن لم تتسع الأسئلة المطلوبة
في صفحة واحدة (أسئلة طويلة/خيارات كثيرة/جداول) توضع أسئلة أقل في تلك الصفحة، فلا يوجد
تزاحم ولا سؤال مقصوص. وسؤال أطول من صفحة كاملة يأخذ صفحته ويكمل بنفس آلية المتابعة
القديمة (نص/كتل بحدود ارتفاع) ثم تبدأ المجموعة التالية بصفحة جديدة.
"""
from dataclasses import dataclass
from math import ceil
from typing import AbstractSet, List, Sequence, Tuple

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
                      hard_max: int = MAX_PER_PAGE, columns: int = 1) -> int:
    """أعلى عدد مريح بالصفحة لكويز: يعتمد على السؤال «النموذجي» (الشريحة 75%) لا على
    أطول سؤال شاذ، لأن المخطط نفسه يخفّض العدد تلقائياً بالصفحات التي فيها أسئلة أطول.

    capacity = ارتفاع العمود الواحد، و heights مقيسة بعرض العمود؛ فالعمودان يضاعفان العدد."""
    if not heights or capacity <= 0:
        return hard_max
    ordered = sorted(heights)
    typical = ordered[min(len(ordered) - 1, ceil(0.75 * len(ordered)) - 1)]
    fit = int(capacity // max(typical, 1.0)) * max(1, int(columns))
    return max(MIN_PER_PAGE, min(hard_max, fit))


@dataclass(frozen=True)
class PagePlan:
    """صفحة مخططة: columns = فهارس الأسئلة في كل عمود (بترتيب القراءة)،
    و full = سؤال بعرض الصفحة كاملاً (أطول من عمود أو لا يتسع بعرض العمود) يأخذ صفحته."""
    columns: Tuple[Tuple[int, ...], ...]
    full: bool = False


def plan_column_pages(heights: Sequence[float], per_page, columns: int, first_capacity: float,
                      capacity: float, wide: AbstractSet[int] = frozenset()) -> List[PagePlan]:
    """تخطيط صفحات بعدة أعمدة. heights مقيسة بعرض العمود، والسعة ارتفاع العمود الواحد.

    - per_page (أو None = كل ما يتسع) سقف لمجموع أسئلة الصفحة؛ يُقسَّم بالتساوي تقريباً بين
      الأعمدة (ceil(per_page / columns) لكل عمود) فلا يمتلئ الأول ويبقى الثاني فارغاً.
    - عند امتلاء العمود (ارتفاعاً أو عدداً) ننتقل للعمود التالي ثم للصفحة التالية.
    - سؤال في wide (لا يتسع بعرض العمود) أو أطول من عمود كامل يأخذ صفحة بعرض كامل
      ويكمل عبر الصفحات بآلية المتابعة المعتادة، ثم تبدأ المجموعة التالية بصفحة جديدة.
    """
    columns = max(1, int(columns))
    limit = max(MIN_PER_PAGE, ceil(int(per_page) / columns)) if per_page else None
    page_cap = max(MIN_PER_PAGE, int(per_page)) if per_page else None
    pages: List[PagePlan] = []
    cols: List[List[int]] = [[]]
    used: List[float] = [0.0]
    cap = first_capacity

    def has_content() -> bool:
        return any(cols)

    def flush() -> None:
        nonlocal cols, used, cap
        if has_content():
            pages.append(PagePlan(tuple(tuple(col) for col in cols if col)))
        cols, used, cap = [[]], [0.0], capacity

    for index, height in enumerate(heights):
        oversize = height > (cap if not has_content() else capacity)
        if index in wide or oversize:
            flush()
            pages.append(PagePlan(((index,),), full=True))
            continue
        total = sum(len(col) for col in cols)
        col = len(cols) - 1
        fits = (used[col] + height <= cap) and (limit is None or len(cols[col]) < limit)
        if page_cap is not None and total >= page_cap:
            flush()
        elif cols[col] and not fits:
            if col + 1 < columns:
                cols.append([])
                used.append(0.0)
            else:
                flush()
        col = len(cols) - 1
        cols[col].append(index)
        used[col] += height
    flush()
    # الصفحة الأخيرة تُوازَن بين العمودين (بدل امتلاء الأول وبقاء الثاني فارغاً).
    if columns == 2 and pages and not pages[-1].full:
        flat = [i for col_items in pages[-1].columns for i in col_items]
        if len(flat) >= 2:
            best_k = min(range(1, len(flat)),
                         key=lambda k: max(sum(heights[i] for i in flat[:k]), sum(heights[i] for i in flat[k:])))
            pages[-1] = PagePlan((tuple(flat[:best_k]), tuple(flat[best_k:])))
    return pages
