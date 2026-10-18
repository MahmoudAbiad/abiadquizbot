"""Pure formatting/validation shared by the gift user and admin screens."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from html import escape

DAMASCUS = ZoneInfo('Asia/Damascus')


def expiry_text(value):
    if not value:
        return 'بدون انتهاء'
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(DAMASCUS).strftime('%Y-%m-%d %H:%M') + ' بتوقيت دمشق'


def _format_local_datetime(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(DAMASCUS).strftime('%Y-%m-%d %H:%M')


def _estimated_duration_text(start_value, end_value):
    if not start_value or not end_value:
        return 'غير محددة'
    start = datetime.fromisoformat(start_value.replace('Z', '+00:00'))
    end = datetime.fromisoformat(end_value.replace('Z', '+00:00'))
    if end <= start:
        return 'غير محددة'
    delta = end - start
    total_seconds = int(delta.total_seconds())
    days, rem = divmod(total_seconds, 24 * 3600)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    chunks = []
    if days:
        chunks.append(f'{days} يوم')
    if hours:
        chunks.append(f'{hours} ساعة')
    if minutes and len(chunks) < 2:
        chunks.append(f'{minutes} دقيقة')
    return ' و '.join(chunks[:2]) or 'أقل من دقيقة'


def parse_expiry(text):
    text = text.strip()
    if text == '0':
        return None
    value = datetime.strptime(text, '%Y-%m-%d %H:%M').replace(tzinfo=DAMASCUS)
    if value <= datetime.now(DAMASCUS):
        raise ValueError('past expiry')
    return value.astimezone(timezone.utc).isoformat()


def gift_text(gift):
    expired = gift.get('expires_at') and datetime.fromisoformat(gift['expires_at'].replace('Z', '+00:00')) <= datetime.now(timezone.utc)
    status = 'ملغاة' if gift.get('revoked_at') else ('منتهية' if expired else ('مستهلكة' if float(gift['remaining_amount']) == 0 else 'صالحة'))
    start_raw = _format_local_datetime(gift.get('granted_at'))
    end_raw = _format_local_datetime(gift.get('expires_at'))
    start_text = f'{start_raw} بتوقيت دمشق' if start_raw else 'غير متاح'
    end_text = f'{end_raw} بتوقيت دمشق' if end_raw else 'بدون انتهاء'
    duration_text = _estimated_duration_text(gift.get('granted_at'), gift.get('expires_at'))
    return (
        f"🎁 <b>{escape(gift['title'])}</b> — {status}\n"
        f"المتبقي: <code>{float(gift['remaining_amount']):g}</code> من {float(gift['original_amount']):g} نقطة\n"
        f"تاريخ البدء: {start_text}\n"
        f"تاريخ الانتهاء: {end_text}\n"
        f"المدة التقديرية: {duration_text}"
    )
