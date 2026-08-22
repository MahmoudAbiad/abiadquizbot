"""
telegram_webapp_auth.py
==============================================================================
التحقق من صحة initData القادمة من Telegram Web App (Mini App) عبر HMAC-SHA256،
حسب الآلية الرسمية الموثّقة من تيليجرام:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app

هاد ملف مستقل بدون أي استيراد من باقي المشروع (غير من المكتبة القياسية) حتى
يسهل اختباره بمعزل، ويُستدعى من webhook_server.py عند أي endpoint بيستقبل
initData من صفحة الرفع.
==============================================================================
"""

import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qsl


def verify_telegram_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int = 3600,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    يتحقق من توقيع initData ومن عدم انتهاء صلاحيتها (auth_date قديم جداً = محاولة
    إعادة استخدام / replay attack محتملة)، ويرجع بيانات المستخدم (user) عند النجاح.

    Returns:
        (True, user_dict)  عند نجاح التحقق.
        (False, None)      عند أي فشل (توقيع خاطئ، بيانات ناقصة، انتهاء صلاحية...).
    """
    if not init_data or not bot_token:
        return False, None

    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except (ValueError, TypeError):
        return False, None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return False, None

    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except (TypeError, ValueError):
        return False, None

    if auth_date <= 0 or (time.time() - auth_date) > max_age_seconds:
        return False, None

    # بناء data-check-string حسب الترتيب الأبجدي للمفاتيح المتبقية (بدون hash)
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))

    # secret_key = HMAC_SHA256("WebAppData", bot_token) -- الترتيب هون إلزامي وثابت من تيليجرام
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return False, None

    user_raw = parsed.get("user")
    if not user_raw:
        return False, None

    try:
        user = json.loads(user_raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False, None

    if not user.get("id"):
        return False, None

    return True, user
