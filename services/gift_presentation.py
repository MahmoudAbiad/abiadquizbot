"""Pure formatting/validation shared by the gift user and admin screens."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from html import escape

DAMASCUS = ZoneInfo('Asia/Damascus')


def expiry_text(value):
    if not value:
        return 'بدون انتهاء'
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(DAMASCUS).strftime('%Y-%m-%d %H:%M') + ' بتوقيت دمشق'


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
    return (
        f"🎁 <b>{escape(gift['title'])}</b> — {status}\n"
        f"المتبقي: <code>{float(gift['remaining_amount']):g}</code> من {float(gift['original_amount']):g} نقطة\n"
        f"الصلاحية: {expiry_text(gift.get('expires_at'))}"
    )
