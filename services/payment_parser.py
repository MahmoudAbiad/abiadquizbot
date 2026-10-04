"""
services/payment_parser.py
==============================================================================
منطق نقي (بدون شبكة/قاعدة بيانات) لنظام الدفع: تطبيع الأرقام، توليد كود الطلب،
وتحليل بطاقات سجل التحويلات في شام كاش من نص screen_data القادم من MacroDroid.

الشكل المعروف لبطاقة واحدة (من لقطات حقيقية):
    <اسم الطرف الثاني>
    #<رقم العملية 9 خانات>
    <المبلغ>
    <العملة: ل.س>
    <YYYY/MM/DD - HH:MM:SS>
    <ملاحظة اختيارية - سطر أو أكثر>

⚠️ البطاقة لا تحمل اتجاه الحوالة (وارد/صادر) - اللون فقط. لذلك هذا الملف لا يقرر
الاتجاه أبداً؛ الاتجاه يُستنتج من الإشعار الوارد، ولا يُقبل شيء تلقائياً من السجل لحاله.
==============================================================================
"""

import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, List, Optional, Sequence, Set

# سوريا بتوقيت ثابت UTC+3 (بلا توقيت صيفي منذ 2022). نتأكد منه بأول دفعة حقيقية.
SYRIA_TZ = timezone(timedelta(hours=3))

CODE_LENGTH = 6
TRANSFERS_SCREEN_MARKER = "آخر التحويلات"

# نصوص واجهة التطبيق التي قد تلي آخر بطاقة بنص الشاشة؛ لا تُحسب ملاحظة.
_UI_LABELS = {
    "الرئيسية", "التحويلات", "الخدمات", "حسابي", "متقدم", "آخر التحويلات",
}

_DIGITS_TRANSLATION = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)
_TX_RE = re.compile(r"#\s*(\d{9})(?!\d)")
_DATETIME_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})\s*-\s*(\d{2}):(\d{2}):(\d{2})")
_AMOUNT_RE = re.compile(r"^[+\-–−]?\s*[\d][\d,\.]*$")
_CODE_IN_NOTE_RE = re.compile(rf"(?<!\d)\d{{{CODE_LENGTH}}}(?!\d)")
_ITEM_SEP = "\n\u241e\n"

# MacroDroid's text dump can prefix screen items with an index, e.g. [5$]:.
# Only remove this exact leading marker, never dollar amounts or arbitrary brackets.
_SCREEN_PREFIX_RE = re.compile(r"^\s*\[\s*\d+\s*\$\s*\]\s*[:：]?\s*")


def clean_screen_text(text: Optional[str]) -> str:
    """Strip extraction prefixes per line; preserve the actual field contents."""
    return "\n".join(
        _SCREEN_PREFIX_RE.sub("", line).strip()
        for line in (text or "").splitlines()
    ).strip()


def normalize_digits(text: str) -> str:
    """يحوّل الأرقام العربية-الهندية والفارسية للغربية."""
    return (text or "").translate(_DIGITS_TRANSLATION)


def generate_code(length: int = CODE_LENGTH) -> str:
    """كود أرقام عشوائي آمن (secrets) بلا صفر بالبداية حتى لا يضيع بالنسخ/اللصق."""
    first = secrets.choice("123456789")
    rest = "".join(secrets.choice("0123456789") for _ in range(length - 1))
    return first + rest


def make_unique_code(existing: Iterable[str], length: int = CODE_LENGTH, attempts: int = 50) -> str:
    taken: Set[str] = set(existing)
    for _ in range(attempts):
        code = generate_code(length)
        if code not in taken:
            return code
    raise RuntimeError("could not generate a unique payment code")


def find_codes_in_note(note: Optional[str]) -> List[str]:
    """كل المرشحين للكود (6 أرقام مستقلة) داخل الملاحظة بعد التطبيع. لا يلتقط أرقام العمليات
    (9 خانات) ولا المبالغ الأطول/الأقصر لأن الشرط أرقام 6 بالضبط محاطة بغير أرقام."""
    return _CODE_IN_NOTE_RE.findall(normalize_digits(clean_screen_text(note)))


@dataclass
class ParsedCard:
    tx_id: str
    counterparty: str
    amount: Optional[Decimal]
    currency: Optional[str]
    tx_time: Optional[datetime]
    note: str
    raw: str


def _parse_amount(line: str) -> Optional[Decimal]:
    cleaned = normalize_digits(line).replace(",", "").replace("+", "").replace("-", "")
    cleaned = cleaned.replace("–", "").replace("−", "").strip()
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    return value if value >= 0 else None


def _parse_currency(line: str) -> Optional[str]:
    low = line.strip().lower()
    if "ل.س" in low or "ليرة" in low or "syp" in low or "s.p" in low:
        return "SYP"
    if "$" in low or "usd" in low or "دولار" in low:
        return "USD"
    return None


def _parse_datetime(line: str) -> Optional[datetime]:
    m = _DATETIME_RE.search(normalize_digits(line))
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, s, tzinfo=SYRIA_TZ)
    except ValueError:
        return None


def split_items(raw: str) -> List[str]:
    """MacroDroid يرسل المتغير القائمة بصيغ مختلفة. نقبل JSON array (حدود العناصر واضحة)
    وإلا نعيد النص كاملاً كعنصر واحد (ونحلله بالاعتماد على مواضع أرقام العمليات)."""
    text = (raw or "").strip()
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(x) for x in data]
        except (ValueError, TypeError):
            pass
    return [raw or ""]


def looks_like_transfers_screen(raw: str) -> bool:
    """العلامة تُفحص بعد فك JSON: ترميز \\uXXXX للعربي (ensure_ascii) لا يجب أن يخفيها."""
    return any(TRANSFERS_SCREEN_MARKER in item for item in split_items(raw))


def _clean_lines(block: str) -> List[str]:
    return [ln for ln in clean_screen_text(block).splitlines() if ln]


def _card_from_lines(before: List[str], tx_id: str, after: List[str], raw: str) -> ParsedCard:
    counterparty = before[-1] if before else ""
    amount = currency = tx_time = None
    note_lines: List[str] = []
    stage = "amount"
    for ln in after:
        if stage == "amount":
            if _AMOUNT_RE.match(normalize_digits(ln)):
                amount = _parse_amount(ln)
                stage = "currency"
                continue
            cur = _parse_currency(ln)
            if cur:  # المبلغ والعملة بنفس السطر مثل "100 ل.س"
                currency = cur
                stage = "datetime"
                continue
        if stage in ("amount", "currency"):
            cur = _parse_currency(ln)
            if cur:
                currency = cur
                stage = "datetime"
                continue
        if stage in ("amount", "currency", "datetime"):
            dt = _parse_datetime(ln)
            if dt:
                tx_time = dt
                stage = "note"
                continue
        if stage == "note":
            if ln in _UI_LABELS:
                break
            note_lines.append(ln)
    return ParsedCard(
        tx_id=tx_id,
        counterparty=counterparty,
        amount=amount,
        currency=currency,
        tx_time=tx_time,
        note=" ".join(note_lines),
        raw=raw.strip(),
    )


def parse_cards(raw_screen: str) -> List[ParsedCard]:
    """يستخرج كل بطاقات التحويل من نص الشاشة. يعتمد على regex لرقم العملية لا على مواضع
    ثابتة (الفهارس تتغير مع عدد الإشعارات). بالنص المتصل: آخر سطر قبل رقم العملية التالي هو
    اسم البطاقة التالية، وكل ما بين تاريخ البطاقة وهذا الاسم هو ملاحظتها."""
    cards: List[ParsedCard] = []
    seen: Set[str] = set()
    for item in split_items(raw_screen):
        lines = _clean_lines(item)
        tx_positions = [(i, m.group(1)) for i, ln in enumerate(lines) for m in [_TX_RE.search(normalize_digits(ln))] if m]
        for idx, (pos, tx_id) in enumerate(tx_positions):
            if tx_id in seen:
                continue
            seen.add(tx_id)
            prev_pos = tx_positions[idx - 1][0] if idx > 0 else -1
            next_pos = tx_positions[idx + 1][0] if idx + 1 < len(tx_positions) else len(lines)
            # الاسم: السطر السابق مباشرة لرقم العملية (إن كان بنفس سطر الرقم نكتفي بالباقي)
            before = lines[prev_pos + 1:pos]
            # نهاية هذه البطاقة: قبل اسم البطاقة التالية (سطر واحد قبل رقمها) إن وُجدت
            end = max(pos + 1, next_pos - 1) if idx + 1 < len(tx_positions) else next_pos
            after = lines[pos + 1:end]
            # لو العنصر الواحد يحوي بطاقة واحدة فقط (JSON array) فالاسم هو أول سطر قبل الرقم
            if len(tx_positions) == 1:
                before = lines[:pos]
            card_raw = "\n".join(lines[max(prev_pos + 1, 0):end])
            cards.append(_card_from_lines(before, tx_id, after, card_raw))
    return cards


# ==================== المصادقة التلقائية (منطق نقي) ====================

def _to_datetime(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def auto_confirm_verdict(order: Optional[dict], receipt: dict, now: datetime, *,
                         grace_before_minutes: int = 2, grace_after_minutes: int = 2) -> "tuple[bool, str]":
    """يقرر هل تُصادَق الدفعة تلقائياً. يرجع (True, "") فقط لو تحققت *كل* الشروط، وإلا (False, السبب).
    لا يلمس أي شبكة/قاعدة بيانات: القرار قابل للاختبار بالكامل. الشروط:
      1) طلب مطابق بالكود، والملاحظة تحوي كوداً واحداً فقط (لا لبس) وهو كود هذا الطلب.
      2) الطلب مفتوح فعلاً وغير منتهٍ (pending/awaiting_verification؛ المنتهي والمراجعة اليدوية يدويان دائماً).
      3) المبلغ والعملة مطابقان للطلب تماماً.
      4) وقت الحوالة معلوم وضمن عمر الطلب (مع سماحية صغيرة) وليس بالمستقبل.
    ⚠️ الاتجاه (وارد/صادر) لا يُفحص عمداً (قرار صاحب المشروع): كود الطلب سري ويُصدر لصاحب الطلب فقط،
    فوجوده بالملاحظة كافٍ."""
    if not order:
        return False, "لا طلب مطابق بالكود"
    codes = set(find_codes_in_note(receipt.get("note")))
    if len(codes) != 1 or order.get("code") not in codes:
        return False, "الملاحظة لا تحوي كود هذا الطلب وحده"
    if order.get("status") not in ("pending", "awaiting_verification"):
        return False, "الطلب ليس بحالة تسمح بالمصادقة التلقائية"
    amount = receipt.get("amount")
    if amount is None or not receipt.get("currency"):
        return False, "المبلغ أو العملة غير معروفين"
    if receipt["currency"] != order.get("currency") or Decimal(str(amount)) != Decimal(str(order.get("amount"))):
        return False, "المبلغ أو العملة لا يطابقان الطلب"
    tx_time = _to_datetime(receipt.get("tx_time"))
    created = _to_datetime(order.get("created_at"))
    expires = _to_datetime(order.get("expires_at"))
    if not (tx_time and created and expires):
        return False, "وقت الحوالة أو الطلب غير معروف"
    if tx_time < created - timedelta(minutes=grace_before_minutes):
        return False, "الحوالة أقدم من الطلب"
    if tx_time > expires + timedelta(minutes=grace_after_minutes):
        return False, "الحوالة بعد انتهاء صلاحية الطلب"
    if tx_time > now + timedelta(minutes=5):
        return False, "وقت الحوالة بالمستقبل"
    return True, ""
