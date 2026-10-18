"""Pure formatting/validation shared by the gift user and admin screens."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from html import escape

DAMASCUS = ZoneInfo('Asia/Damascus')


def _parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(DAMASCUS)
    except (TypeError, ValueError):
        return None


def _format_datetime(value):
    parsed = _parse_datetime(value)
    if not parsed:
        return 'غير محدد'
    return parsed.strftime('%Y-%m-%d %H:%M') + ' بتوقيت دمشق'


def _duration_text(start_value, end_value):
    start_dt = _parse_datetime(start_value)
    end_dt = _parse_datetime(end_value)
    if not end_dt:
        return 'غير محددة (بدون انتهاء)'
    if not start_dt:
        return 'غير محددة'
    total_seconds = int((end_dt - start_dt).total_seconds())
    if total_seconds <= 0:
        return '0 دقيقة'
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f'{days:g} يوم')
    if hours:
        parts.append(f'{hours:g} ساعة')
    if minutes:
        parts.append(f'{minutes:g} دقيقة')
    return ' و '.join(parts) or '0 دقيقة'


def expiry_text(value):
    if not value:
        return 'بدون انتهاء'
    return _format_datetime(value)


def _countdown_unit(amount, singular, dual, plural, many):
    if amount == 1:
        return singular
    if amount == 2:
        return dual
    return f'{amount} {plural if 3 <= amount <= 10 else many}'


def remaining_expiry_text(value, now=None):
    """Describe the actual remaining validity, never the gift's initial duration."""
    if not value:
        return 'نقاط هذه الهدية بدون انتهاء صلاحية.'
    end = _parse_datetime(value)
    if not end:
        return 'تعذر تحديد المدة المتبقية لهذه الهدية.'
    now = now or datetime.now(timezone.utc)
    seconds = (end - now).total_seconds()
    if seconds <= 0:
        return 'انتهت صلاحية نقاط هذه الهدية.'
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(_countdown_unit(days, 'يوم', 'يومان', 'أيام', 'يوماً'))
    if hours:
        parts.append(_countdown_unit(hours, 'ساعة', 'ساعتان', 'ساعات', 'ساعة'))
    if not days and minutes:
        parts.append(_countdown_unit(minutes, 'دقيقة', 'دقيقتان', 'دقائق', 'دقيقة'))
    duration = ' و '.join(parts) or 'أقل من دقيقة'
    return f'باقي على انتهاء صلاحية نقاط هذه الهدية: {duration}.'


def parse_expiry(text):
    text = text.strip()
    if text == '0':
        return None
    value = datetime.strptime(text, '%Y-%m-%d %H:%M').replace(tzinfo=DAMASCUS)
    if value <= datetime.now(DAMASCUS):
        raise ValueError('past expiry')
    return value.astimezone(timezone.utc).isoformat()


def gift_text(gift, *, compact=False):
    end_dt = _parse_datetime(gift.get('expires_at'))
    expired = bool(end_dt and end_dt.astimezone(timezone.utc) <= datetime.now(timezone.utc))
    status = 'ملغاة' if gift.get('revoked_at') else ('منتهية' if expired else ('مستهلكة' if float(gift['remaining_amount']) == 0 else 'صالحة'))
    start_at = gift.get('granted_at') or gift.get('created_at') or gift.get('issued_at')
    expires_at = gift.get('expires_at')
    summary = (
        f"🎁 <b>{escape(gift['title'])}</b> — {status}\n"
        f"المتبقي: <code>{float(gift['remaining_amount']):g}</code> من {float(gift['original_amount']):g} نقطة\n"
    )
    if compact:
        countdown = f'⏳ {remaining_expiry_text(expires_at)}\n' if status == 'صالحة' else ''
        return summary + countdown + f'📅 انتهاء الهدية: {expiry_text(expires_at)}'
    return (
        summary +
        f"بداية الهدية: {_format_datetime(start_at)}\n"
        f"انتهاء الهدية: {expiry_text(expires_at)}\n"
        f"المدة التقديرية: {_duration_text(start_at, expires_at)}"
    )
