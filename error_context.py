"""
سياق تنفيذ الطلب الحالي (Request-scoped context).

الفكرة: ErrorTrackingMiddleware (راجع middlewares.py) يضبط هذا السياق في بداية معالجة
كل Update (رسالة/ضغطة زر/إجابة استفتاء...) بمعرّف المستخدم صاحب الطلب، ثم يُصفّره في
النهاية. أي استدعاء لاحق لـ log_error()/log_critical() (من logger.py) خلال معالجة نفس
الطلب — أينما وُجد بالمشروع، بما فيها كل نقاط try/except الموجودة سلفاً بكل الملفات —
يقرأ هذا السياق تلقائياً ويسجّل الخطأ كحدث تحليلات مرتبط بالمستخدم (usage_events,
event_type='error_occurred') دون الحاجة لتعديل يدوي بكل نقطة تسجيل خطأ بالمشروع.

خارج نطاق معالجة أي Update (مهام خلفية دورية، سكربتات...) يكون السياق فارغاً (None)
تلقائياً، فلا يُسجَّل أي شيء بالخطأ ولا يتأثر أي سلوك حالي.
"""

import contextvars
from typing import NamedTuple, Optional


class ErrorContext(NamedTuple):
    user_id: Optional[int]
    update_type: Optional[str]
    context: Optional[str]  # نص الرسالة أو callback_data المختصر، لمساعدة الأدمن على فهم "أين" حدث الخطأ


_current_context: "contextvars.ContextVar[Optional[ErrorContext]]" = contextvars.ContextVar(
    "current_error_context", default=None
)


def set_error_context(user_id: Optional[int] = None, update_type: Optional[str] = None,
                       context: Optional[str] = None):
    """يضبط سياق الطلب الحالي، ويرجع token يُستخدم لاحقاً مع reset_error_context."""
    return _current_context.set(ErrorContext(user_id=user_id, update_type=update_type, context=context))


def reset_error_context(token) -> None:
    try:
        _current_context.reset(token)
    except Exception:
        pass


def get_error_context() -> Optional[ErrorContext]:
    return _current_context.get()
