"""
services/i18n.py
==============================================================================
نظام دعم اللغات المتعددة (العربية، الإنجليزية، التركية، الفرنسية، الألمانية، الفارسية)
وتسعير باقات الشحن حسب لغة المستخدم وعملته (SYP / USD):

1) اللغات المدعومة (6 لغات):
   - العربية (ar)
   - الإنجليزية (en)
   - التركية (tr)
   - الفرنسية (fr)
   - الألمانية (de)
   - الفارسية (fa)

2) شاشة اللغة بعد الموافقة على شروط الخصوصية (وفي الإعدادات /settings):
   - تتعرف تلقائياً على لغة المستخدم من تيليجرام (language_code) وتضع عليها علامة (✅).
   - تعرض زر متابعة بارزاً بلون مميز (style="success") للمتابعة بضغطة واحدة أو تبديل اللغة.

3) الاستفادة من رقم الهاتف (إن كان متوفراً فقط دون طلبه أبداً من المستخدم):
   - لا يطلب البوت رقم الهاتف نهائياً (لا يستخدم request_contact).
   - إذا كان رقم الهاتف متوفراً مسبقاً في الكائن الوارد وكان غير سوري (لا يبدأ بـ +963 / 00963 / 963)،
     يُستخدم الدولار الأمريكي (USD) لعرض أسعار الباقات.

4) سياسة باقات الشحن والعملات:
   - للغة العربية (ar):
     * الباقات الثابتة: 200 نقطة (65 ل.س / 0.5$)، 500 نقطة (130 ل.س / 1$)، 1000 نقطة (260 ل.س / 2$).
     * تُعرض افتراضياً بالليرة السورية (SYP) مع زر تبديل لرؤية الأسعار بالدولار الأمريكي (USD) والعكس.
     * عند اختيار باقة بالدولار يُوجَّه المستخدم لمراسلة الدعم (@abiadd) برسالة جاهزة.
   - لأي لغة أخرى غير العربية (en, tr, fr, de, fa):
     * تُعرض الباقات بالدولار الأمريكي (USD) حصراً وبالأسعار الدولية المتفق عليها حصراً:
       - 400 نقطة — 1$
       - 900 نقطة — 2$ (مع توضيح نسبة الخصم 11%)
       - 1600 نقطة — 3$ (مع توضيح نسبة الخصم 25% — الخيار الأوفر، وتلوين الزر بـ style="success")
     * عند اختيار أي باقة يُوجَّه المستخدم لمراسلة الدعم (@abiadd) برسالة جاهزة بلغته.
==============================================================================
"""

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from aiogram import types
from cachetools import TTLCache

from logger import get_logger

logger = get_logger(__name__)

# ==================== اللغات المدعومة ====================

SUPPORTED_LANGUAGES: Tuple[str, ...] = ("ar", "en", "tr", "fr", "de", "fa")
DEFAULT_LANGUAGE = "ar"

LANGUAGE_LABELS: Dict[str, str] = {
    "ar": "العربية 🇸🇾",
    "en": "English 🇬🇧",
    "tr": "Türkçe 🇹🇷",
    "fr": "Français 🇫🇷",
    "de": "Deutsch 🇩🇪",
    "fa": "فارسی 🇮🇷",
}

LANGUAGE_NATIVE_NAMES: Dict[str, str] = {
    "ar": "العربية",
    "en": "English",
    "tr": "Türkçe",
    "fr": "Français",
    "de": "Deutsch",
    "fa": "فارسی",
}

_LANG_ALIASES: Dict[str, str] = {
    "ar": "ar",
    "ara": "ar",
    "en": "en",
    "eng": "en",
    "tr": "tr",
    "tur": "tr",
    "fr": "fr",
    "fra": "fr",
    "fre": "fr",
    "de": "de",
    "deu": "de",
    "ger": "de",
    "fa": "fa",
    "per": "fa",
    "pes": "fa",
    "fas": "fa",
}

_LANG_CACHE_TTL = 300
_LANG_REDIS_TTL = 30 * 86400
_lang_cache: TTLCache = TTLCache(maxsize=50000, ttl=_LANG_CACHE_TTL)

_DIGITS_TRANS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _redis():
    from config import redis_client
    return redis_client


def _lang_redis_key(user_id: int) -> str:
    return f"user:lang:{int(user_id)}"


def normalize_language_code(code: Any, default: str = DEFAULT_LANGUAGE) -> str:
    """تطبيع كود اللغة القادم من تيليجرام (مثل en-US أو fa-IR) إلى إحدى اللغات الـ 6 المدعومة."""
    if not code or not isinstance(code, str):
        return default
    cleaned = code.strip().lower().replace("_", "-")
    primary = cleaned.split("-", 1)[0]
    if primary in _LANG_ALIASES:
        return _LANG_ALIASES[primary]
    return default


def detect_telegram_language(user_or_code: Any, default: str = DEFAULT_LANGUAGE) -> str:
    """التعرف التلقائي على لغة المستخدم من كائن تيليجرام (User / Message / CallbackQuery) أو كود نصي."""
    if isinstance(user_or_code, str):
        return normalize_language_code(user_or_code, default=default)
    if user_or_code is None:
        return default
    lang_code = getattr(user_or_code, "language_code", None)
    if not lang_code:
        from_user = getattr(user_or_code, "from_user", None)
        if from_user is not None:
            lang_code = getattr(from_user, "language_code", None)
    return normalize_language_code(lang_code, default=default)


# ==================== فحص رقم الهاتف إن وُجد (دون طلبه أبداً) ====================

def extract_available_phone(obj: Any) -> Optional[str]:
    """استخراج رقم الهاتف فقط إذا كان متوفراً مسبقاً في الكائن أو البيانات (دون طلب جهة الاتصال أبداً)."""
    if obj is None:
        return None
    if isinstance(obj, str):
        raw = obj
    elif isinstance(obj, dict):
        raw = obj.get("phone_number") or obj.get("phone")
        if not raw and isinstance(obj.get("contact"), dict):
            raw = obj["contact"].get("phone_number")
    else:
        raw = getattr(obj, "phone_number", None)
        if not raw:
            contact = getattr(obj, "contact", None)
            if contact is not None:
                raw = getattr(contact, "phone_number", None)
        if not raw:
            from_user = getattr(obj, "from_user", None)
            if from_user is not None:
                raw = getattr(from_user, "phone_number", None)
        if not raw:
            message = getattr(obj, "message", None)
            if message is not None:
                msg_contact = getattr(message, "contact", None)
                if msg_contact is not None:
                    raw = getattr(msg_contact, "phone_number", None)

    if not raw or not isinstance(raw, str):
        return None
    normalized = raw.translate(_DIGITS_TRANS).strip()
    digits_and_plus = re.sub(r"[^0-9+]", "", normalized)
    return digits_and_plus if len(re.sub(r"\D", "", digits_and_plus)) >= 7 else None


def is_non_syrian_phone(obj: Any) -> bool:
    """يرجع True فقط إذا كان رقم الهاتف متوفراً فعلياً وكان غير سوري (لا يبدأ بـ +963 أو 00963 أو 963)."""
    phone = extract_available_phone(obj)
    if not phone:
        return False
    cleaned = phone.lstrip("+")
    if cleaned.startswith("00"):
        cleaned = cleaned[2:]
    if cleaned.startswith("963"):
        return False
    # الأرقام السورية المحلية (مثل 09xxxxxxxx المؤلفة من 10 أرقام)
    if cleaned.startswith("09") and len(cleaned) == 10:
        return False
    return True


# ==================== حفظ واسترجاع لغة المستخدم ====================

async def get_user_language(user_id: int, fallback_user_or_code: Any = None) -> str:
    """جلب لغة المستخدم المحفوظة (من الكاش ثم Redis ثم قاعدة البيانات)، أو التعرف التلقائي عليها."""
    uid = int(user_id)
    if uid in _lang_cache:
        return _lang_cache[uid]

    try:
        raw = await _redis().get(_lang_redis_key(uid))
        if raw is not None:
            val = raw.decode() if isinstance(raw, bytes) else str(raw)
            if val in SUPPORTED_LANGUAGES:
                _lang_cache[uid] = val
                return val
    except Exception as exc:
        logger.debug("Could not read user language from Redis for %s: %s", uid, exc)

    try:
        from helpers.supabase_helper import supabase
        res = await (
            supabase.table("users")
            .select("language")
            .eq("user_id", uid)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if rows and rows[0].get("language") in SUPPORTED_LANGUAGES:
            lang = rows[0]["language"]
            _lang_cache[uid] = lang
            try:
                await _redis().set(_lang_redis_key(uid), lang, ex=_LANG_REDIS_TTL)
            except Exception:
                pass
            return lang
    except Exception:
        # في حال لم يكن عمود language مضافاً بعد في القاعدة أو تعذر الاتصال، نكمل بأمان
        pass

    detected = detect_telegram_language(fallback_user_or_code, default=DEFAULT_LANGUAGE)
    return detected


async def set_user_language(user_id: int, lang_code: str) -> str:
    """حفظ لغة المستخدم المختارة في الكاش المحلي وRedis وقاعدة البيانات (إن توفر العمود)."""
    uid = int(user_id)
    lang = normalize_language_code(lang_code, default=DEFAULT_LANGUAGE)
    _lang_cache[uid] = lang

    try:
        await _redis().set(_lang_redis_key(uid), lang, ex=_LANG_REDIS_TTL)
    except Exception as exc:
        logger.warning("Could not store user language in Redis for %s: %s", uid, exc)

    try:
        from helpers.supabase_helper import supabase
        await (
            supabase.table("users")
            .update({"language": lang})
            .eq("user_id", uid)
            .execute()
        )
    except Exception:
        # آمن في حال كان المستخدم لم يُسجّل بجدول users بعد أو لم يُطبّق ترحيل العمود بعد
        pass

    return lang


async def purge_user_language_cache(user_id: int) -> None:
    uid = int(user_id)
    _lang_cache.pop(uid, None)
    try:
        await _redis().delete(_lang_redis_key(uid))
    except Exception:
        pass


# ==================== شاشة اختيار اللغة (بعد الموافقة وفي الإعدادات) ====================

LANGUAGE_SCREEN_TEXTS: Dict[str, str] = {
    "ar": (
        "🌐 <b>اختر لغتك المفضلة · Choose your language</b>\n\n"
        "تعرفنا تلقائياً على لغتك ووضعنا عليها إشارة (✅): <b>العربية 🇸🇾</b>\n"
        "إذا كانت هي لغتك المطلوبة، اضغط زر المتابعة الأخضر بالأسفل مباشرة، أو اختر لغة أخرى:"
    ),
    "en": (
        "🌐 <b>Choose Your Preferred Language · اختر لغتك</b>\n\n"
        "We automatically detected your language and marked it with (✅): <b>English 🇬🇧</b>\n"
        "If this is your preferred language, tap the green continue button below, or select another language:"
    ),
    "tr": (
        "🌐 <b>Tercih Ettiğiniz Dili Seçin · اختر لغتك</b>\n\n"
        "Dilinizi otomatik olarak algıladık ve (✅) ile işaretledik: <b>Türkçe 🇹🇷</b>\n"
        "Devam etmek için aşağıdaki yeşil butona dokunun veya başka bir dil seçin:"
    ),
    "fr": (
        "🌐 <b>Choisissez votre langue préférée · اختر لغتك</b>\n\n"
        "Nous avons détecté votre langue automatiquement (✅) : <b>Français 🇫🇷</b>\n"
        "Appuyez sur le bouton vert ci-dessous pour continuer, ou choisissez une autre langue :"
    ),
    "de": (
        "🌐 <b>Wählen Sie Ihre bevorzugte Sprache · اختر لغتك</b>\n\n"
        "Wir haben Ihre Sprache automatisch erkannt und mit (✅) markiert: <b>Deutsch 🇩🇪</b>\n"
        "Tippen Sie unten auf den grünen Button, um fortzufahren, oder wählen Sie eine andere Sprache:"
    ),
    "fa": (
        "🌐 <b>زبان مورد نظر خود را انتخاب کنید · اختر لغتك</b>\n\n"
        "زبان شما به صورت خودکار تشخیص داده شد و با (✅) علامت‌گذاری شد: <b>فارسی 🇮🇷</b>\n"
        "اگر این زبان مورد نظر شماست، دکمه سبز ادامه را در پایین بزنید یا زبان دیگری را انتخاب کنید:"
    ),
}

CONTINUE_BUTTON_LABELS: Dict[str, str] = {
    "ar": "✅ متابعة بهذه اللغة (العربية)",
    "en": "✅ Continue in English",
    "tr": "✅ Türkçe ile devam et",
    "fr": "✅ Continuer en Français",
    "de": "✅ Weiter auf Deutsch",
    "fa": "✅ ادامه با زبان فارسی",
}

SAVE_SETTINGS_LANG_LABELS: Dict[str, str] = {
    "ar": "✅ حفظ اللغة والعودة للإعدادات",
    "en": "✅ Save Language & Back to Settings",
    "tr": "✅ Dili Kaydet ve Ayarlara Dön",
    "fr": "✅ Enregistrer la langue",
    "de": "✅ Sprache speichern",
    "fa": "✅ ذخیره زبان و بازگشت به تنظیمات",
}

DEEP_LINK_CONTINUE_LABELS: Dict[str, str] = {
    "ar": "▶️ متابعة",
    "en": "▶️ Continue",
    "tr": "▶️ Devam et",
    "fr": "▶️ Continuer",
    "de": "▶️ Weiter",
    "fa": "▶️ ادامه",
}

CONSENT_CONFIRMED_TOASTS: Dict[str, str] = {
    "ar": "✅ تمت الموافقة وحفظ اللغة",
    "en": "✅ Consent & language saved",
    "tr": "✅ Onay ve dil kaydedildi",
    "fr": "✅ Consentement et langue enregistrés",
    "de": "✅ Zustimmung & Sprache gespeichert",
    "fa": "✅ موافقت و زبان ذخیره شد",
}


def build_language_screen_text(lang_code: str = DEFAULT_LANGUAGE) -> str:
    lang = normalize_language_code(lang_code)
    return LANGUAGE_SCREEN_TEXTS.get(lang, LANGUAGE_SCREEN_TEXTS[DEFAULT_LANGUAGE])


def build_language_screen_keyboard(
    selected_lang: str = DEFAULT_LANGUAGE,
    prefix: str = "consent:lang",
    is_settings: bool = False,
) -> types.InlineKeyboardMarkup:
    """بناء لوحة أزرار اللغات الـ 6 مع وضع ✅ على اللغة المختارة وتلوين زر المتابعة بـ style='success'."""
    selected = normalize_language_code(selected_lang)
    pairs = [
        ("ar", "en"),
        ("tr", "fr"),
        ("de", "fa"),
    ]
    rows: List[List[types.InlineKeyboardButton]] = []
    for left_code, right_code in pairs:
        row = []
        for code in (left_code, right_code):
            label = LANGUAGE_LABELS[code]
            prefix_mark = "✅ " if code == selected else ""
            row.append(
                types.InlineKeyboardButton(
                    text=f"{prefix_mark}{label}",
                    callback_data=f"{prefix}:pick:{code}",
                )
            )
        rows.append(row)

    confirm_text = (
        SAVE_SETTINGS_LANG_LABELS.get(selected, SAVE_SETTINGS_LANG_LABELS["ar"])
        if is_settings
        else CONTINUE_BUTTON_LABELS.get(selected, CONTINUE_BUTTON_LABELS["ar"])
    )
    rows.append([
        types.InlineKeyboardButton(
            text=confirm_text,
            callback_data=f"{prefix}:confirm:{selected}",
            style="success",
        )
    ])
    if is_settings:
        rows.append([
            types.InlineKeyboardButton(
                text="🔙 رجوع للإعدادات · Back",
                callback_data="student_settings",
            )
        ])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def build_localized_accepted_text(
    lang_code: str = DEFAULT_LANGUAGE,
    resuming: bool = False,
    callback_pending: bool = False,
) -> str:
    """نص الترحيب/الموافقة بعد تأكيد اللغة، مترجم حسب اللغة المختارة."""
    lang = normalize_language_code(lang_code)
    if lang == "ar":
        if resuming:
            return "✅ <b>شكراً لك، تمت الموافقة.</b>\n\n⏳ جارٍ متابعة طلبك السابق…"
        if callback_pending:
            return "✅ <b>شكراً لك، تمت الموافقة.</b>\n\n↩️ اضغط الزر الذي ضغطته قبل قليل مرة أخرى لمتابعة طلبك."
        return (
            "✅ <b>شكراً لك، تمت الموافقة.</b>\n\n"
            "أرسل ملفك أو نصك أو صورك لإنشاء اختبار، أو اكتب /start لعرض القائمة الرئيسية."
        )
    if lang == "en":
        if resuming:
            return "✅ <b>Thank you, consent confirmed.</b>\n\n⏳ Resuming your previous request…"
        if callback_pending:
            return "✅ <b>Thank you, consent confirmed.</b>\n\n↩️ Please tap the button you pressed earlier again to continue."
        return (
            "✅ <b>Thank you, consent confirmed.</b>\n\n"
            "Send your file, text, or images to generate a quiz, or type /start to open the main menu."
        )
    if lang == "tr":
        if resuming:
            return "✅ <b>Teşekkürler, onayınız kaydedildi.</b>\n\n⏳ Önceki isteğiniz devam ettiriliyor…"
        if callback_pending:
            return "✅ <b>Teşekkürler, onayınız kaydedildi.</b>\n\n↩️ Devam etmek için az önce bastığınız düğmeye tekrar dokunun."
        return (
            "✅ <b>Teşekkürler, onayınız kaydedildi.</b>\n\n"
            "Test oluşturmak için dosyanızı, metninizi veya görsellerinizi gönderin ya da ana menü için /start yazın."
        )
    if lang == "fr":
        if resuming:
            return "✅ <b>Merci, votre accord est enregistré.</b>\n\n⏳ Reprise de votre demande précédente…"
        if callback_pending:
            return "✅ <b>Merci, votre accord est enregistré.</b>\n\n↩️ Veuillez appuyer à nouveau sur le bouton précédent pour continuer."
        return (
            "✅ <b>Merci, votre accord est enregistré.</b>\n\n"
            "Envoyez votre fichier, texte ou images pour créer un quiz, ou tapez /start pour afficher le menu principal."
        )
    if lang == "de":
        if resuming:
            return "✅ <b>Vielen Dank, Ihre Zustimmung wurde gespeichert.</b>\n\n⏳ Ihre vorherige Anfrage wird fortgesetzt…"
        if callback_pending:
            return "✅ <b>Vielen Dank, Ihre Zustimmung wurde gespeichert.</b>\n\n↩️ Bitte tippen Sie erneut auf den zuvor gedrückten Button."
        return (
            "✅ <b>Vielen Dank, Ihre Zustimmung wurde gespeichert.</b>\n\n"
            "Senden Sie Ihre Datei, Ihren Text oder Ihre Bilder, um ein Quiz zu erstellen, oder geben Sie /start für das Hauptmenü ein."
        )
    # fa
    if resuming:
        return "✅ <b>سپاسگزاریم، موافقت شما ثبت شد.</b>\n\n⏳ در حال ادامه درخواست قبلی شما…"
    if callback_pending:
        return "✅ <b>سپاسگزاریم، موافقت شما ثبت شد.</b>\n\n↩️ لطفاً دکمه‌ای را که قبلاً زده بودید دوباره لمس کنید."
    return (
        "✅ <b>سپاسگزاریم، موافقت شما ثبت شد.</b>\n\n"
        "فایل، متن یا تصاویر خود را برای ساخت آزمون ارسال کنید، یا برای نمایش منوی اصلی /start را بنویسید."
    )


# ==================== باقات الشحن والعملات (SYP / USD) ====================

# 1) باقات اللغة العربية الثابتة (تُعرض بالليرة السورية افتراضياً مع إمكانية التبديل للدولار)
AR_SYP_PACKAGES: Dict[str, str] = {
    "200": "65",
    "500": "130",
    "1000": "260",
}

AR_USD_PACKAGES: Dict[str, str] = {
    "200": "0.5",
    "500": "1",
    "1000": "2",
}

# 2) باقات اللغات غير العربية (بالدولار الأمريكي حصراً مع نسب الخصم وتلوين الخيار الأوفر)
# السعر المرجعي: 400 نقطة = 1$ (0.0025$ للنقطة)
# 900 نقطة = 2$ بدل 2.25$ -> خصم 11%
# 1600 نقطة = 3$ بدل 4.00$ -> خصم 25% (الخيار الأوفر — يلوّن بـ style="success")
INTL_USD_PACKAGES: List[Dict[str, Any]] = [
    {"points": "400", "price": "1", "discount_pct": 0, "best_value": False},
    {"points": "900", "price": "2", "discount_pct": 11, "best_value": False},
    {"points": "1600", "price": "3", "discount_pct": 25, "best_value": True},
]

_INTL_PACKAGE_MAP: Dict[str, Dict[str, Any]] = {p["points"]: p for p in INTL_USD_PACKAGES}

CURRENCY_TOGGLE_TO_USD_TEXT = "💵 عرض الأسعار بالدولار الأمريكي ($)"
CURRENCY_TOGGLE_TO_SYP_TEXT = "🇸🇾 عرض الأسعار بالليرة السورية (ل.س)"

_RECHARGE_TEXTS: Dict[str, str] = {
    "ar_syp": (
        "🔋 <b>شحن النقاط</b>\n\n"
        "اختر الباقة المطلوبة، ثم اضغط «تواصل للشحن» لمعرفة طريقة الدفع، "
        "أو استخدم «🧮 حاسبة النقاط» لتصميم باقة مخصصة.\n\n"
        "🇸🇾 الأسعار معروضة بـ <b>الليرة السورية</b> (ويمكنك التبديل لعرضها بالدولار الأمريكي 💵)."
    ),
    "ar_usd": (
        "🔋 <b>شحن النقاط (بالدولار الأمريكي 💵)</b>\n\n"
        "اختر الباقة المطلوبة، ثم اضغط «تواصل للشحن» لمراسلة الدعم وإتمام الشحن:\n"
        "• <b>200 نقطة</b> — <b>0.5$</b> (تعادل 65 ل.س)\n"
        "• <b>500 نقطة</b> — <b>1$</b> (تعادل 130 ل.س)\n"
        "• <b>1000 نقطة</b> — <b>2$</b> (تعادل 260 ل.س)\n\n"
        "💵 الأسعار معروضة بـ <b>الدولار الأمريكي ($)</b> (ويمكنك التبديل للعرض بالليرة السورية 🇸🇾)."
    ),
    "en": (
        "🔋 <b>Recharge Points (USD $)</b>\n\n"
        "Choose your package below, then tap <b>Contact Support to Recharge</b>:\n"
        "• ⭐ <b>400 Points</b> — <b>$1</b>\n"
        "• 🔥 <b>900 Points</b> — <b>$2</b> <i>(11% Discount)</i>\n"
        "• 💎 <b>1600 Points</b> — <b>$3</b> <i>(25% Discount · Best Value)</i>\n\n"
        "💵 Prices are in <b>US Dollars (USD)</b>."
    ),
    "tr": (
        "🔋 <b>Puan Yükleme (USD $)</b>\n\n"
        "Aşağıdan paketinizi seçin ve <b>Yükleme İçin Destekle İletişime Geç</b> düğmesine dokunun:\n"
        "• ⭐ <b>400 Puan</b> — <b>1$</b>\n"
        "• 🔥 <b>900 Puan</b> — <b>2$</b> <i>(%11 İndirim)</i>\n"
        "• 💎 <b>1600 Puan</b> — <b>3$</b> <i>(%25 İndirim · En Avantajlı)</i>\n\n"
        "💵 Fiyatlar <b>ABD Doları (USD)</b> cinsindendir."
    ),
    "fr": (
        "🔋 <b>Recharger des points (USD $)</b>\n\n"
        "Choisissez votre forfait ci-dessous, puis appuyez sur <b>Contacter le support</b> :\n"
        "• ⭐ <b>400 Points</b> — <b>1$</b>\n"
        "• 🔥 <b>900 Points</b> — <b>2$</b> <i>(11% de réduction)</i>\n"
        "• 💎 <b>1600 Points</b> — <b>3$</b> <i>(25% de réduction · Meilleure offre)</i>\n\n"
        "💵 Les prix sont affichés en <b>Dollars américains (USD)</b>."
    ),
    "de": (
        "🔋 <b>Punkte aufladen (USD $)</b>\n\n"
        "Wählen Sie unten Ihr Paket aus und tippen Sie dann auf <b>Support kontaktieren</b>:\n"
        "• ⭐ <b>400 Punkte</b> — <b>1$</b>\n"
        "• 🔥 <b>900 Punkte</b> — <b>2$</b> <i>(11% Rabatt)</i>\n"
        "• 💎 <b>1600 Punkte</b> — <b>3$</b> <i>(25% Rabatt · Bestes Angebot)</i>\n\n"
        "💵 Alle Preise sind in <b>US-Dollar (USD)</b>."
    ),
    "fa": (
        "🔋 <b>شارژ امتیاز (دلار آمریکا $)</b>\n\n"
        "بسته مورد نظر خود را انتخاب کنید و سپس روی <b>ارتباط با پشتیبانی برای شارژ</b> بزنید:\n"
        "• ⭐ <b>400 امتیاز</b> — <b>1$</b>\n"
        "• 🔥 <b>900 امتیاز</b> — <b>2$</b> <i>(11% تخفیف)</i>\n"
        "• 💎 <b>1600 امتیاز</b> — <b>3$</b> <i>(25% تخفیف · به‌صرفه‌ترین گزینه)</i>\n\n"
        "💵 قیمت‌ها به <b>دلار آمریکا (USD)</b> می‌باشد."
    ),
}

_CHOOSE_PACKAGE_ALERTS: Dict[str, str] = {
    "ar": "اختر الباقة أولاً من الأزرار أعلاه ☝️",
    "en": "Please select a package from the buttons above first ☝️",
    "tr": "Lütfen önce yukarıdaki düğmelerden bir paket seçin ☝️",
    "fr": "Veuillez d'abord sélectionner un forfait ci-dessus ☝️",
    "de": "Bitte wählen Sie zuerst oben ein Paket aus ☝️",
    "fa": "لطفاً ابتدا یکی از بسته‌های بالا را انتخاب کنید ☝️",
}


def get_choose_package_alert(lang_code: str = DEFAULT_LANGUAGE) -> str:
    lang = normalize_language_code(lang_code)
    return _CHOOSE_PACKAGE_ALERTS.get(lang, _CHOOSE_PACKAGE_ALERTS["ar"])


def resolve_recharge_currency(
    lang_code: str,
    currency_override: Optional[str] = None,
    has_non_syrian_phone: bool = False,
) -> str:
    """تحديد العملة المعروضة:
    - غير العربية: دائماً USD حصراً.
    - العربية: SYP افتراضياً، أو USD إذا اختار المستخدم التبديل لـ USD أو كان رقمه غير سوري ولم يطلب SYP صراحة.
    """
    lang = normalize_language_code(lang_code)
    if lang != "ar":
        return "USD"
    if currency_override in ("SYP", "USD"):
        return currency_override
    if has_non_syrian_phone:
        return "USD"
    return "SYP"


def _format_intl_package_button_text(lang: str, pkg: Dict[str, Any], selected: bool) -> str:
    pts = pkg["points"]
    price = pkg["price"]
    disc = pkg["discount_pct"]
    best = pkg["best_value"]
    prefix = "✅ " if selected else ("💎 " if best else ("🔥 " if disc > 0 else "⭐ "))

    if lang == "tr":
        base = f"{prefix}{pts} Puan — {price}$"
        if best:
            return f"{base} (%{disc} İndirim · En Avantajlı)"
        if disc > 0:
            return f"{base} (%{disc} İndirim)"
        return base
    if lang == "fr":
        base = f"{prefix}{pts} Points — {price}$"
        if best:
            return f"{base} (-{disc}% · Meilleure offre)"
        if disc > 0:
            return f"{base} (-{disc}% de réduction)"
        return base
    if lang == "de":
        base = f"{prefix}{pts} Punkte — {price}$"
        if best:
            return f"{base} ({disc}% Rabatt · Bestes Angebot)"
        if disc > 0:
            return f"{base} ({disc}% Rabatt)"
        return base
    if lang == "fa":
        base = f"{prefix}{pts} امتیاز — {price}$"
        if best:
            return f"{base} ({disc}% تخفیف · به‌صرفه‌ترین)"
        if disc > 0:
            return f"{base} ({disc}% تخفیف)"
        return base
    # Default non-Arabic: English ("en")
    base = f"{prefix}{pts} Points — ${price}"
    if best:
        return f"{base} ({disc}% OFF · Best Value)"
    if disc > 0:
        return f"{base} ({disc}% OFF)"
    return base


def _build_support_draft(lang: str, points: str, price: str, currency: str, discount_pct: int = 0) -> str:
    if lang == "ar":
        unit = "ل.س" if currency == "SYP" else "$"
        return f"مرحباً، أريد شحن باقة {points} نقطة ({price} {unit}). ما طريقة الدفع؟"
    if lang == "tr":
        disc_note = f" (%{discount_pct} indirimli)" if discount_pct > 0 else ""
        return f"Merhaba, {points} puanlık ({price}${disc_note}) paketi yüklemek istiyorum. Ödeme yöntemi nedir?"
    if lang == "fr":
        disc_note = f" (-{discount_pct}%)" if discount_pct > 0 else ""
        return f"Bonjour, je souhaite recharger le forfait de {points} points ({price}${disc_note}). Quel est le moyen de paiement ?"
    if lang == "de":
        disc_note = f" ({discount_pct}% Rabatt)" if discount_pct > 0 else ""
        return f"Hallo, ich möchte das Paket mit {points} Punkten ({price}${disc_note}) aufladen. Wie kann ich bezahlen?"
    if lang == "fa":
        disc_note = f" ({discount_pct}% تخفیف)" if discount_pct > 0 else ""
        return f"سلام، می‌خواهم بسته {points} امتیازی ({price}${disc_note}) را شارژ کنم. روش پرداخت چگونه است؟"
    # en
    disc_note = f" ({discount_pct}% discount)" if discount_pct > 0 else ""
    return f"Hello, I would like to recharge the {points} points package (${price}{disc_note}). How can I pay?"


def _support_contact_button_label(lang: str, selected_points: Optional[str] = None, price: Optional[str] = None, currency: str = "USD") -> str:
    if lang == "ar":
        if selected_points and price:
            unit = "ل.س" if currency == "SYP" else "$"
            return f"💬 تواصل للشحن ({selected_points} نقطة — {price} {unit})"
        return "💬 تواصل للشحن"
    if lang == "tr":
        return f"💬 Destekle İletişime Geç ({selected_points} Puan — {price}$)" if selected_points else "💬 Yükleme İçin Destekle İletişime Geç"
    if lang == "fr":
        return f"💬 Contacter le support ({selected_points} pts — {price}$)" if selected_points else "💬 Contacter le support pour recharger"
    if lang == "de":
        return f"💬 Support kontaktieren ({selected_points} Pkt. — {price}$)" if selected_points else "💬 Support zum Aufladen kontaktieren"
    if lang == "fa":
        return f"💬 ارتباط با پشتیبانی ({selected_points} امتیاز — {price}$)" if selected_points else "💬 ارتباط با پشتیبانی برای شارژ"
    return f"💬 Contact Support ({selected_points} pts — ${price})" if selected_points else "💬 Contact Support to Recharge"


def build_recharge_view(
    lang_code: str = DEFAULT_LANGUAGE,
    currency_override: Optional[str] = None,
    selected_package: Optional[str] = None,
    has_non_syrian_phone: bool = False,
    admin_contact: str = "@abiadd",
) -> Tuple[str, types.InlineKeyboardMarkup]:
    """بناء نص ولوحة أزرار شاشة الشحن حسب لغة المستخدم وعملته."""
    lang = normalize_language_code(lang_code)
    currency = resolve_recharge_currency(
        lang,
        currency_override=currency_override,
        has_non_syrian_phone=has_non_syrian_phone,
    )
    contact_handle = admin_contact.lstrip("@")

    # الحالة 1: لغة المستخدم غير العربية -> باقات الدولار الدولية حصراً (400/$1, 900/$2, 1600/$3)
    if lang != "ar":
        text = _RECHARGE_TEXTS.get(lang, _RECHARGE_TEXTS["en"])
        rows: List[List[types.InlineKeyboardButton]] = []
        for pkg in INTL_USD_PACKAGES:
            pts = pkg["points"]
            is_selected = selected_package == pts
            btn_kwargs: Dict[str, Any] = {
                "text": _format_intl_package_button_text(lang, pkg, is_selected),
                "callback_data": f"recharge_package:intl:{pts}",
            }
            # تلوين الخيار الأوفر (1600 نقطة بـ 3$) بلون مميز (أخضر style="success")
            if pkg["best_value"]:
                btn_kwargs["style"] = "success"
            rows.append([types.InlineKeyboardButton(**btn_kwargs)])

        chosen = _INTL_PACKAGE_MAP.get(str(selected_package or ""))
        if chosen:
            draft = _build_support_draft(
                lang,
                chosen["points"],
                chosen["price"],
                "USD",
                discount_pct=chosen["discount_pct"],
            )
            url = f"https://t.me/{contact_handle}?{urlencode({'text': draft})}"
            rows.append([
                types.InlineKeyboardButton(
                    text=_support_contact_button_label(lang, chosen["points"], chosen["price"], "USD"),
                    url=url,
                    style="primary",
                )
            ])
        else:
            rows.append([
                types.InlineKeyboardButton(
                    text=_support_contact_button_label(lang),
                    callback_data="recharge_choose_package",
                )
            ])
        return text, types.InlineKeyboardMarkup(inline_keyboard=rows)

    # الحالة 2: لغة المستخدم العربية -> التبديل بين الليرة السورية (SYP) والدولار (USD) بالباقات الثابتة (200, 500, 1000)
    if currency == "USD":
        text = _RECHARGE_TEXTS["ar_usd"]
        rows = []
        for pts, usd_price in AR_USD_PACKAGES.items():
            prefix = "✅ " if selected_package == pts else "⭐ "
            rows.append([
                types.InlineKeyboardButton(
                    text=f"{prefix}{pts} نقطة — {usd_price}$",
                    callback_data=f"recharge_package:ar_usd:{pts}",
                )
            ])
        rows.append([
            types.InlineKeyboardButton(
                text=CURRENCY_TOGGLE_TO_SYP_TEXT,
                callback_data="recharge_currency:SYP",
            )
        ])
        rows.append([
            types.InlineKeyboardButton(
                text="🧮 حاسبة النقاط",
                callback_data="calc:menu",
            )
        ])
        if selected_package in AR_USD_PACKAGES:
            usd_price = AR_USD_PACKAGES[selected_package]
            draft = _build_support_draft("ar", selected_package, usd_price, "USD")
            url = f"https://t.me/{contact_handle}?{urlencode({'text': draft})}"
            rows.append([
                types.InlineKeyboardButton(
                    text=_support_contact_button_label("ar", selected_package, usd_price, "USD"),
                    url=url,
                    style="success",
                )
            ])
        else:
            rows.append([
                types.InlineKeyboardButton(
                    text=_support_contact_button_label("ar"),
                    callback_data="recharge_choose_package",
                )
            ])
        return text, types.InlineKeyboardMarkup(inline_keyboard=rows)

    # العربية بالليرة السورية (الافتراضي)
    text = _RECHARGE_TEXTS["ar_syp"]
    rows = [
        [
            types.InlineKeyboardButton(
                text=f"{'✅ ' if selected_package == pts else '⭐ '}{pts} نقطة — {syp_price} ل.س",
                callback_data=f"recharge_package:{pts}",
            )
        ]
        for pts, syp_price in AR_SYP_PACKAGES.items()
    ]
    rows.append([
        types.InlineKeyboardButton(
            text=CURRENCY_TOGGLE_TO_USD_TEXT,
            callback_data="recharge_currency:USD",
        )
    ])
    rows.append([
        types.InlineKeyboardButton(
            text="🧮 حاسبة النقاط",
            callback_data="calc:menu",
        )
    ])
    if selected_package in AR_SYP_PACKAGES:
        syp_price = AR_SYP_PACKAGES[selected_package]
        draft = _build_support_draft("ar", selected_package, syp_price, "SYP")
        url = f"https://t.me/{contact_handle}?{urlencode({'text': draft})}"
        rows.append([
            types.InlineKeyboardButton(
                text=_support_contact_button_label("ar", selected_package, syp_price, "SYP"),
                url=url,
                style="success",
            )
        ])
    else:
        rows.append([
            types.InlineKeyboardButton(
                text=_support_contact_button_label("ar"),
                callback_data="recharge_choose_package",
            )
        ])
    return text, types.InlineKeyboardMarkup(inline_keyboard=rows)
