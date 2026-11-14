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

import functools
import re
import sys
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
    import config
    client = getattr(config, "redis_client", None)
    if client is not None:
        return client
    return getattr(config, "redis", None)


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
    install_ui_localization_hooks()
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
    install_ui_localization_hooks()
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
                    text=_support_contact_button_label(lang),\n                    callback_data="recharge_choose_package",
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


# ==================== ترجمة الواجهة الرئيسية والإعدادات والمحفظة (6 لغات) ====================

SETTINGS_STRINGS: Dict[str, Dict[str, str]] = {
    "ar": {
        "title": "⚙️ الإعدادات",
        "renewal_schedule": "🔄 النقاط المجانية تتجدد يوميًا عند الساعة 8 مساءً بتوقيت دمشق.",
        "renewal_status_on": "🔔 مفعّلة",
        "renewal_status_off": "🔕 متوقفة",
        "renewal_label": "إشعارات التجديد",
        "lang_label": "🌐 لغة البوت",
        "note": "إيقاف الإشعار لا يؤثر على تجدد نقاطك.",
        "btn_disable_renewal": "🔕 إيقاف إشعارات التجديد",
        "btn_enable_renewal": "🔔 تفعيل إشعارات التجديد",
        "btn_home": "🏠 الرئيسية",
        "btn_lang": "🌐 اللغة / Language ({lang_label})",
        "off_banner": "🔕 تم إيقاف إشعارات التجديد. يمكنك إعادة تفعيلها من ⚙️ الإعدادات.",
    },
    "en": {
        "title": "⚙️ Settings",
        "renewal_schedule": "🔄 Free points renew daily at 8:00 PM Damascus time.",
        "renewal_status_on": "🔔 Enabled",
        "renewal_status_off": "🔕 Disabled",
        "renewal_label": "Renewal notifications",
        "lang_label": "🌐 Bot language",
        "note": "Disabling notifications does not affect your daily points renewal.",
        "btn_disable_renewal": "🔕 Disable renewal notifications",
        "btn_enable_renewal": "🔔 Enable renewal notifications",
        "btn_home": "🏠 Main Menu",
        "btn_lang": "🌐 Language ({lang_label})",
        "off_banner": "🔕 Renewal notifications disabled. You can re-enable them anytime from ⚙️ Settings.",
    },
    "tr": {
        "title": "⚙️ Ayarlar",
        "renewal_schedule": "🔄 Ücretsiz puanlar her gün Şam saatiyle 20:00'de yenilenir.",
        "renewal_status_on": "🔔 Açık",
        "renewal_status_off": "🔕 Kapalı",
        "renewal_label": "Yenileme bildirimleri",
        "lang_label": "🌐 Bot dili",
        "note": "Bildirimleri kapatmak günlük puan yenilenmesini etkilemez.",
        "btn_disable_renewal": "🔕 Yenileme bildirimlerini kapat",
        "btn_enable_renewal": "🔔 Yenileme bildirimlerini aç",
        "btn_home": "🏠 Ana Menü",
        "btn_lang": "🌐 Dil / Language ({lang_label})",
        "off_banner": "🔕 Yenileme bildirimleri kapatıldı. ⚙️ Ayarlar'dan tekrar açabilirsiniz.",
    },
    "fr": {
        "title": "⚙️ Paramètres",
        "renewal_schedule": "🔄 Les points gratuits se renouvellent chaque jour à 20h00 (heure de Damas).",
        "renewal_status_on": "🔔 Activées",
        "renewal_status_off": "🔕 Désactivées",
        "renewal_label": "Notifications de renouvellement",
        "lang_label": "🌐 Langue du bot",
        "note": "La désactivation des notifications n'affecte pas le renouvellement de vos points.",
        "btn_disable_renewal": "🔕 Désactiver les notifications",
        "btn_enable_renewal": "🔔 Activer les notifications",
        "btn_home": "🏠 Menu principal",
        "btn_lang": "🌐 Langue / Language ({lang_label})",
        "off_banner": "🔕 Notifications désactivées. Vous pouvez les réactiver depuis ⚙️ Paramètres.",
    },
    "de": {
        "title": "⚙️ Einstellungen",
        "renewal_schedule": "🔄 Gratispunkte werden täglich um 20:00 Uhr (Damaskus-Zeit) erneuert.",
        "renewal_status_on": "🔔 Aktiviert",
        "renewal_status_off": "🔕 Deaktiviert",
        "renewal_label": "Erneuerungsbenachrichtigungen",
        "lang_label": "🌐 Bot-Sprache",
        "note": "Das Deaktivieren der Benachrichtigungen hat keinen Einfluss auf die Punkteerneuerung.",
        "btn_disable_renewal": "🔕 Benachrichtigungen deaktivieren",
        "btn_enable_renewal": "🔔 Benachrichtigungen aktivieren",
        "btn_home": "🏠 Hauptmenü",
        "btn_lang": "🌐 Sprache / Language ({lang_label})",
        "off_banner": "🔕 Benachrichtigungen deaktiviert. Du kannst sie jederzeit unter ⚙️ Einstellungen wieder aktivieren.",
    },
    "fa": {
        "title": "⚙️ تنظیمات",
        "renewal_schedule": "🔄 امتیازهای رایگان هر روز ساعت ۸ شب به وقت دمشق تمدید می‌شوند.",
        "renewal_status_on": "🔔 فعال",
        "renewal_status_off": "🔕 غیرفعال",
        "renewal_label": "اعلان‌های تمدید",
        "lang_label": "🌐 زبان ربات",
        "note": "غیرفعال کردن اعلان تاثیری بر تمدید روزانه امتیازهای شما ندارد.",
        "btn_disable_renewal": "🔕 غیرفعال کردن اعلان‌های تمدید",
        "btn_enable_renewal": "🔔 فعال کردن اعلان‌های تمدید",
        "btn_home": "🏠 منوی اصلی",
        "btn_lang": "🌐 زبان / Language ({lang_label})",
        "off_banner": "🔕 اعلان‌های تمدید غیرفعال شد. می‌توانید از بخش ⚙️ تنظیمات دوباره آن را فعال کنید.",
    },
}

WALLET_STRINGS: Dict[str, Dict[str, Any]] = {
    "ar": {
        "btn_recharge": "💳 شحن الرصيد",
        "btn_gifts": "🎁 هداياي وصلاحيتها",
        "btn_redeem": "🎟 شحن كود هدية",
        "btn_home": "🔙 رجوع للقائمة الرئيسية",
        "btn_back_wallet": "🔙 رصيدي وهداياي",
        "btn_prev": "⬅️ السابق",
        "btn_next": "التالي ➡️",
        "btn_cancel": "إلغاء",
        "btn_return_home": "🏠 العودة للرئيسية",
        "btn_create_quiz": "📝 إنشاء كويز",
        "fetch_error": "تعذر جلب الرصيد، حاول مجددًا.",
        "account_error": "تعذر الوصول للحساب. حاول مجددًا.",
        "balance_header": "💰 <b>رصيدك: {points:.2f} نقطة</b>",
        "free_line": "🔄 مجانية: {free_points:.2f}",
        "gift_line": "🎁 هدايا: {gift_points:.2f}",
        "paid_line": "💳 مدفوعة: {paid_points:.2f}",
        "consumption_note": (
            "تُستهلك المجانية أولًا، ثم الهدايا الأقرب انتهاءً، ثم المدفوعة.\n"
            "الهدايا المنتهية أو الملغاة لا تدخل في الرصيد المتاح."
        ),
        "page_unavailable": "صفحة غير متاحة. أعد فتح قائمة الهدايا.",
        "gifts_header": "🎁 <b>هداياي وصلاحيتها · صفحة {page}</b>",
        "no_gifts": "لا توجد هدايا في حسابك بعد.",
        "busy_alert": "أنهِ العملية الحالية أولًا أو ألغِها، ثم أدخل الكود.",
        "prompt_code": "🎟 أرسل كود الهدية:",
        "cancel_inactive": "طلب إدخال الكود لم يعد نشطاً.",
        "cancel_done": "✅ تم إلغاء إدخال كود الهدية.",
        "cancel_toast": "تم الإلغاء",
        "invalid_format": "❌ صيغة الكود غير صحيحة. انسخه كاملًا ثم أعد المحاولة.",
        "redeem_errors": {
            "invalid": "الكود غير موجود.",
            "expired": "انتهت مهلة شحن هذا الكود.",
            "disabled": "هذا الكود موقوف.",
            "exhausted": "وصل الكود إلى العدد الأقصى للمستفيدين.",
            "already_used": "سبق لك استخدام هذا الكود. يمكن لكل طالب استخدامه مرة واحدة فقط.",
            "missing_user": "شغّل البوت باستخدام /start أولًا.",
            "default": "تعذر الشحن. حاول مجددًا.",
        },
        "redeem_success": (
            "✅ أُضيفت <b>{points:g} نقطة هدية</b>.\n"
            "⏳ {remaining}\n"
            "📅 انتهاء الهدية: {expiry}"
        ),
    },
    "en": {
        "btn_recharge": "💳 Recharge Balance",
        "btn_gifts": "🎁 My Gifts & Validity",
        "btn_redeem": "🎟 Redeem Gift Code",
        "btn_home": "🔙 Back to Main Menu",
        "btn_back_wallet": "🔙 My Wallet & Gifts",
        "btn_prev": "⬅️ Previous",
        "btn_next": "Next ➡️",
        "btn_cancel": "Cancel",
        "btn_return_home": "🏠 Return to Main Menu",
        "btn_create_quiz": "📝 Create Quiz",
        "fetch_error": "Could not fetch your balance. Please try again.",
        "account_error": "Could not access your account. Please try again.",
        "balance_header": "💰 <b>Your Balance: {points:.2f} pts</b>",
        "free_line": "🔄 Free: {free_points:.2f}",
        "gift_line": "🎁 Gifts: {gift_points:.2f}",
        "paid_line": "💳 Paid: {paid_points:.2f}",
        "consumption_note": (
            "Free points are used first, then gifts expiring soonest, then paid points.\n"
            "Expired or cancelled gifts are not included in your available balance."
        ),
        "page_unavailable": "Page unavailable. Please reopen the gifts list.",
        "gifts_header": "🎁 <b>My Gifts & Validity · Page {page}</b>",
        "no_gifts": "You have no gifts in your account yet.",
        "busy_alert": "Finish or cancel your current operation first, then enter the code.",
        "prompt_code": "🎟 Send your gift code:",
        "cancel_inactive": "Gift code input is no longer active.",
        "cancel_done": "✅ Gift code entry cancelled.",
        "cancel_toast": "Cancelled",
        "invalid_format": "❌ Invalid code format. Copy the full code and try again.",
        "redeem_errors": {
            "invalid": "Code not found.",
            "expired": "This code has expired.",
            "disabled": "This code is disabled.",
            "exhausted": "This code has reached its maximum number of redemptions.",
            "already_used": "You have already used this code. Each student can use it only once.",
            "missing_user": "Please start the bot with /start first.",
            "default": "Could not redeem code. Please try again.",
        },
        "redeem_success": (
            "✅ Added <b>{points:g} gift points</b>.\n"
            "⏳ {remaining}\n"
            "📅 Gift expires: {expiry}"
        ),
    },
    "tr": {
        "btn_recharge": "💳 Bakiye Yükle",
        "btn_gifts": "🎁 Hediyelerim ve Süreleri",
        "btn_redeem": "🎟 Hediye Kodu Kullan",
        "btn_home": "🔙 Ana Menüye Dön",
        "btn_back_wallet": "🔙 Cüzdanım ve Hediyelerim",
        "btn_prev": "⬅️ Önceki",
        "btn_next": "Sonraki ➡️",
        "btn_cancel": "İptal",
        "btn_return_home": "🏠 Ana Menüye Dön",
        "btn_create_quiz": "📝 Test Oluştur",
        "fetch_error": "Bakiye alınamadı, lütfen tekrar deneyin.",
        "account_error": "Hesaba erişilemedi. Lütfen tekrar deneyin.",
        "balance_header": "💰 <b>Bakiyeniz: {points:.2f} puan</b>",
        "free_line": "🔄 Ücretsiz: {free_points:.2f}",
        "gift_line": "🎁 Hediye: {gift_points:.2f}",
        "paid_line": "💳 Ücretli: {paid_points:.2f}",
        "consumption_note": (
            "Önce ücretsiz puanlar, ardından süresi en yakın hediyeler, sonra ücretli puanlar kullanılır.\n"
            "Süresi dolmuş veya iptal edilmiş hediyeler kullanılabilir bakiyeye dahil edilmez."
        ),
        "page_unavailable": "Sayfa mevcut değil. Hediye listesini yeniden açın.",
        "gifts_header": "🎁 <b>Hediyelerim ve Süreleri · Sayfa {page}</b>",
        "no_gifts": "Hesabınızda henüz hediye bulunmuyor.",
        "busy_alert": "Önce mevcut işlemi tamamlayın veya iptal edin, ardından kodu girin.",
        "prompt_code": "🎟 Hediye kodunuzu gönderin:",
        "cancel_inactive": "Kod girişi artık aktif değil.",
        "cancel_done": "✅ Hediye kodu girişi iptal edildi.",
        "cancel_toast": "İptal edildi",
        "invalid_format": "❌ Geçersiz kod biçimi. Kodun tamamını kopyalayıp tekrar deneyin.",
        "redeem_errors": {
            "invalid": "Kod bulunamadı.",
            "expired": "Bu kodun kullanım süresi dolmuş.",
            "disabled": "Bu kod devre dışı bırakılmış.",
            "exhausted": "Bu kod maksimum kullanım sınırına ulaştı.",
            "already_used": "Bu kodu zaten kullandınız. Her öğrenci yalnızca bir kez kullanabilir.",
            "missing_user": "Lütfen önce /start ile botu başlatın.",
            "default": "Kod kullanılamadı. Lütfen tekrar deneyin.",
        },
        "redeem_success": (
            "✅ <b>{points:g} hediye puanı</b> eklendi.\n"
            "⏳ {remaining}\n"
            "📅 Bitiş tarihi: {expiry}"
        ),
    },
    "fr": {
        "btn_recharge": "💳 Recharger le solde",
        "btn_gifts": "🎁 Mes cadeaux et validité",
        "btn_redeem": "🎟 Utiliser un code cadeau",
        "btn_home": "🔙 Retour au menu principal",
        "btn_back_wallet": "🔙 Mon solde et mes cadeaux",
        "btn_prev": "⬅️ Précédent",
        "btn_next": "Suivant ➡️",
        "btn_cancel": "Annuler",
        "btn_return_home": "🏠 Retour à l'accueil",
        "btn_create_quiz": "📝 Créer un quiz",
        "fetch_error": "Impossible de récupérer le solde, veuillez réessayer.",
        "account_error": "Impossible d'accéder au compte. Veuillez réessayer.",
        "balance_header": "💰 <b>Votre solde : {points:.2f} points</b>",
        "free_line": "🔄 Gratuits : {free_points:.2f}",
        "gift_line": "🎁 Cadeaux : {gift_points:.2f}",
        "paid_line": "💳 Payants : {paid_points:.2f}",
        "consumption_note": (
            "Les points gratuits sont utilisés en premier, puis les cadeaux expirant le plus tôt, puis les points payants.\n"
            "Les cadeaux expirés ou annulés ne sont pas inclus dans le solde disponible."
        ),
        "page_unavailable": "Page indisponible. Veuillez rouvrir la liste des cadeaux.",
        "gifts_header": "🎁 <b>Mes cadeaux et validité · Page {page}</b>",
        "no_gifts": "Vous n'avez pas encore de cadeaux sur votre compte.",
        "busy_alert": "Terminez ou annulez d'abord l'opération en cours, puis saisissez le code.",
        "prompt_code": "🎟 Envoyez votre code cadeau :",
        "cancel_inactive": "La saisie du code n'est plus active.",
        "cancel_done": "✅ Saisie du code cadeau annulée.",
        "cancel_toast": "Annulé",
        "invalid_format": "❌ Format du code invalide. Copiez le code complet et réessayez.",
        "redeem_errors": {
            "invalid": "Code introuvable.",
            "expired": "Ce code a expiré.",
            "disabled": "Ce code est désactivé.",
            "exhausted": "Ce code a atteint le nombre maximal d'utilisations.",
            "already_used": "Vous avez déjà utilisé ce code. Chaque étudiant ne peut l'utiliser qu'une seule fois.",
            "missing_user": "Veuillez d'abord démarrer le bot avec /start.",
            "default": "Impossible d'utiliser le code. Veuillez réessayer.",
        },
        "redeem_success": (
            "✅ <b>{points:g} points cadeaux</b> ajoutés.\n"
            "⏳ {remaining}\n"
            "📅 Expiration : {expiry}"
        ),
    },
    "de": {
        "btn_recharge": "💳 Guthaben aufladen",
        "btn_gifts": "🎁 Meine Geschenke & Gültigkeit",
        "btn_redeem": "🎟 Geschenkcode einlösen",
        "btn_home": "🔙 Zurück zum Hauptmenü",
        "btn_back_wallet": "🔙 Mein Guthaben & Geschenke",
        "btn_prev": "⬅️ Zurück",
        "btn_next": "Weiter ➡️",
        "btn_cancel": "Abbrechen",
        "btn_return_home": "🏠 Zurück zum Hauptmenü",
        "btn_create_quiz": "📝 Quiz erstellen",
        "fetch_error": "Guthaben konnte nicht geladen werden. Bitte versuche es erneut.",
        "account_error": "Kontozugriff fehlgeschlagen. Bitte versuche es erneut.",
        "balance_header": "💰 <b>Dein Guthaben: {points:.2f} Punkte</b>",
        "free_line": "🔄 Gratis: {free_points:.2f}",
        "gift_line": "🎁 Geschenke: {gift_points:.2f}",
        "paid_line": "💳 Bezahlt: {paid_points:.2f}",
        "consumption_note": (
            "Gratispunkte werden zuerst verbraucht, dann bald ablaufende Geschenke und zuletzt bezahlte Punkte.\n"
            "Abgelaufene oder stornierte Geschenke zählen nicht zum verfügbaren Guthaben."
        ),
        "page_unavailable": "Seite nicht verfügbar. Bitte öffne die Geschenkliste erneut.",
        "gifts_header": "🎁 <b>Meine Geschenke & Gültigkeit · Seite {page}</b>",
        "no_gifts": "Du hast noch keine Geschenke in deinem Konto.",
        "busy_alert": "Beende oder brich zuerst den aktuellen Vorgang ab und gib dann den Code ein.",
        "prompt_code": "🎟 Sende deinen Geschenkcode:",
        "cancel_inactive": "Die Code-Eingabe ist nicht mehr aktiv.",
        "cancel_done": "✅ Eingabe des Geschenkcodes abgebrochen.",
        "cancel_toast": "Abgebrochen",
        "invalid_format": "❌ Ungültiges Code-Format. Kopiere den vollständigen Code und versuche es erneut.",
        "redeem_errors": {
            "invalid": "Code nicht gefunden.",
            "expired": "Dieser Code ist abgelaufen.",
            "disabled": "Dieser Code ist deaktiviert.",
            "exhausted": "Dieser Code hat die maximale Anzahl an Einlösungen erreicht.",
            "already_used": "Du hast diesen Code bereits verwendet. Jeder Student kann ihn nur einmal nutzen.",
            "missing_user": "Bitte starte den Bot zuerst mit /start.",
            "default": "Einlösung fehlgeschlagen. Bitte versuche es erneut.",
        },
        "redeem_success": (
            "✅ <b>{points:g} Geschenkpunkte</b> hinzugefügt.\n"
            "⏳ {remaining}\n"
            "📅 Gültig bis: {expiry}"
        ),
    },
    "fa": {
        "btn_recharge": "💳 شارژ اعتبار",
        "btn_gifts": "🎁 هدایای من و اعتبار آن‌ها",
        "btn_redeem": "🎟 ثبت کد هدیه",
        "btn_home": "🔙 بازگشت به منوی اصلی",
        "btn_back_wallet": "🔙 موجودی و هدایای من",
        "btn_prev": "⬅️ قبلی",
        "btn_next": "بعدی ➡️",
        "btn_cancel": "لغو",
        "btn_return_home": "🏠 بازگشت به منوی اصلی",
        "btn_create_quiz": "📝 ساخت کوئیز",
        "fetch_error": "دریافت موجودی انجام نشد، دوباره تلاش کنید.",
        "account_error": "دسترسی به حساب ممکن نشد. دوباره تلاش کنید.",
        "balance_header": "💰 <b>موجودی شما: {points:.2f} امتیاز</b>",
        "free_line": "🔄 رایگان: {free_points:.2f}",
        "gift_line": "🎁 هدیه: {gift_points:.2f}",
        "paid_line": "💳 خریداری‌شده: {paid_points:.2f}",
        "consumption_note": (
            "ابتدا امتیازهای رایگان، سپس هدایایی که زودتر منقضی می‌شوند و در نهایت امتیازهای خریداری‌شده مصرف می‌شوند.\n"
            "هدایای منقضی یا لغوشده در موجودی قابل استفاده محاسبه نمی‌شوند."
        ),
        "page_unavailable": "صفحه در دسترس نیست. فهرست هدایا را دوباره باز کنید.",
        "gifts_header": "🎁 <b>هدایای من و اعتبار آن‌ها · صفحه {page}</b>",
        "no_gifts": "هنوز هدیه‌ای در حساب شما ثبت نشده است.",
        "busy_alert": "ابتدا عملیات فعلی را تکمیل یا لغو کنید، سپس کد را وارد نمایید.",
        "prompt_code": "🎟 کد هدیه خود را ارسال کنید:",
        "cancel_inactive": "درخواست وارد کردن کد دیگر فعال نیست.",
        "cancel_done": "✅ وارد کردن کد هدیه لغو شد.",
        "cancel_toast": "لغو شد",
        "invalid_format": "❌ فرمت کد نادرست است. کد را کامل کپی کرده و دوباره تلاش کنید.",
        "redeem_errors": {
            "invalid": "کد یافت نشد.",
            "expired": "مهلت استفاده از این کد به پایان رسیده است.",
            "disabled": "این کد غیرفعال شده است.",
            "exhausted": "ظرفیت استفاده از این کد تکمیل شده است.",
            "already_used": "شما قبلاً از این کد استفاده کرده‌اید. هر دانشجو فقط یک بار می‌تواند از آن استفاده کند.",
            "missing_user": "لطفاً ابتدا ربات را با دستور /start راه‌اندازی کنید.",
            "default": "ثبت کد انجام نشد. دوباره تلاش کنید.",
        },
        "redeem_success": (
            "✅ <b>{points:g} امتیاز هدیه</b> اضافه شد.\n"
            "⏳ {remaining}\n"
            "📅 انقضای هدیه: {expiry}"
        ),
    },
}

UI_STRINGS: Dict[str, Dict[str, str]] = {
    "en": {
        "welcome_renewed": "🎉 Your daily balance has been renewed!\n\n",
        "welcome_body": (
            "Welcome to <b>Abiad Smart Quiz Maker</b> ✨\n\n"
            "💰 <b>Your Balance: {points:.2f} pts</b>\n"
            "🔄 Free: {free_points:.2f} · 🎁 Gifts: {gift_points:.2f} · 💳 Paid: {paid_points:.2f}\n\n"
            "📤 <b>Send your study content now to generate a quiz:</b>\n"
            "• 📄 File (PDF, Word, PowerPoint)\n"
            "• 🖼 Clear photo of a page or slide\n"
            "• ✍️ Direct text message (at least 100 characters)\n\n"
            "Tap any button below to manage your saved quizzes, wallet, or settings:"
        ),
        "home_text": (
            "Welcome to <b>Abiad Smart Quiz Maker</b> ✨\n\n"
            "📤 Send a file (PDF, Word, PowerPoint), a photo, or text (100+ chars) to create an interactive quiz immediately!"
        ),
        "upload_text": (
            "📤 <b>Send your study material now!</b>\n\n"
            "• 📄 File: PDF, Word (.docx), or PowerPoint (.pptx)\n"
            "• 🖼 Photo: Clear image of notes or textbook pages\n"
            "• ✍️ Text: Paste your lecture text directly (100+ chars)\n"
            "• 🌐 Large file? Use the web uploader button below."
        ),
        "help_text": (
            "❓ <b>How to Use Abiad Quiz Maker</b>\n\n"
            "1️⃣ <b>Send Content:</b> Upload a PDF/Word/PPTX file, send a photo, or paste lecture text.\n"
            "2️⃣ <b>Customize Quiz:</b> Choose question count, difficulty, and quiz mode (interactive poll, test, or document export).\n"
            "3️⃣ <b>Review & Save:</b> Practice mistakes with spaced repetition and save quizzes to ⭐ My Saved Quizzes."
        ),
        "new_user_welcome": (
            "👋 Welcome <b>{first_name}</b> to <b>Abiad Smart Quiz Maker</b>!\n\n"
            "🎁 You have received <b>{points:g} free points</b> to start creating AI quizzes from your lectures, PDFs, or photos.\n\n"
            "Tap the button below or send any file/text right now to build your first quiz!"
        ),
        "new_user_btn": "🚀 Start Creating Your First Quiz",
        "btn_help": "❓ How to use the bot?",
        "btn_land": "🏕️ My Land & Challenges",
        "btn_large_upload": "📚 Large file or lecture? Tap here",
        "btn_favorites": "⭐ My Saved Quizzes",
        "btn_wallet": "💰 My Wallet & Gifts",
        "btn_settings": "⚙️ Settings",
        "btn_channel": "📢 News Channel",
        "btn_support": "💬 Technical Support",
        "btn_home": "🏠 Main Menu",
        "btn_referral": "🎁 Invite a Friend · {pts} pts",
        "btn_channel_reward": "📢 Join Channel · {pts} pts",
        "channel_reward_claim_btn": "✅ Verify & Claim Reward",
    },
    "tr": {
        "welcome_renewed": "🎉 Günlük bakiyeniz yenilendi!\n\n",
        "welcome_body": (
            "<b>Abiad Akıllı Test Oluşturucu</b>'ya hoş geldiniz ✨\n\n"
            "💰 <b>Bakiyeniz: {points:.2f} puan</b>\n"
            "🔄 Ücretsiz: {free_points:.2f} · 🎁 Hediye: {gift_points:.2f} · 💳 Ücretli: {paid_points:.2f}\n\n"
            "📤 <b>Test oluşturmak için ders içeriğinizi hemen gönderin:</b>\n"
            "• 📄 Dosya (PDF, Word, PowerPoint)\n"
            "• 🖼 Sayfa veya slaytın net fotoğrafı\n"
            "• ✍️ Doğrudan metin mesajı (en az 100 karakter)\n\n"
            "Kayıtlı testlerinizi, cüzdanınızı veya ayarlarınızı yönetmek için aşağıdaki düğmeleri kullanın:"
        ),
        "home_text": (
            "<b>Abiad Akıllı Test Oluşturucu</b>'ya hoş geldiniz ✨\n\n"
            "📤 Hemen etkileşimli bir test oluşturmak için dosya (PDF, Word, PowerPoint), fotoğraf veya metin (100+ karakter) gönderin!"
        ),
        "upload_text": (
            "📤 <b>Ders materyalinizi şimdi gönderin!</b>\n\n"
            "• 📄 Dosya: PDF, Word (.docx) veya PowerPoint (.pptx)\n"
            "• 🖼 Fotoğraf: Notların veya kitap sayfalarının net görüntüsü\n"
            "• ✍️ Metin: Ders metnini doğrudan yapıştırın (100+ karakter)\n"
            "• 🌐 Büyük dosya mı? Aşağıdaki web yükleyici düğmesini kullanın."
        ),
        "help_text": (
            "❓ <b>Abiad Test Oluşturucu Nasıl Kullanılır?</b>\n\n"
            "1️⃣ <b>İçerik Gönderin:</b> PDF/Word/PPTX dosyası yükleyin, fotoğraf gönderin veya ders metnini yapıştırın.\n"
            "2️⃣ <b>Testi Özelleştirin:</b> Soru sayısını, zorluk seviyesini ve test modunu seçin.\n"
            "3️⃣ <b>Tekrar Edin ve Kaydedin:</b> Hatalarınızı gözden geçirin ve testleri ⭐ Kayıtlı Testlerim'e kaydedin."
        ),
        "new_user_welcome": (
            "👋 Merhaba <b>{first_name}</b>, <b>Abiad Akıllı Test Oluşturucu</b>'ya hoş geldiniz!\n\n"
            "🎁 Derslerinizden, PDF dosyalarınızdan veya fotoğraflarınızdan yapay zeka testleri oluşturmaya başlamanız için <b>{points:g} ücretsiz puan</b> tanımlandı.\n\n"
            "İlk testinizi oluşturmak için aşağıdaki düğmeye dokunun veya hemen bir dosya/metin gönderin!"
        ),
        "new_user_btn": "🚀 İlk Testini Oluşturmaya Başla",
        "btn_help": "❓ Bot nasıl kullanılır?",
        "btn_land": "🏕️ Alanım ve Görevler",
        "btn_large_upload": "📚 Dosyanız veya dersiniz büyük mü? Buraya dokunun",
        "btn_favorites": "⭐ Kayıtlı Testlerim",
        "btn_wallet": "💰 Cüzdanım ve Hediyelerim",
        "btn_settings": "⚙️ Ayarlar",
        "btn_channel": "📢 Haber Kanalı",
        "btn_support": "💬 Teknik Destek",
        "btn_home": "🏠 Ana Menü",
        "btn_referral": "🎁 Arkadaşını Davet Et · {pts} puan",
        "btn_channel_reward": "📢 Kanala Katıl · {pts} puan",
        "channel_reward_claim_btn": "✅ Doğrula ve Ödülü Al",
    },
    "fr": {
        "welcome_renewed": "🎉 Votre solde quotidien a été renouvelé !\n\n",
        "welcome_body": (
            "Bienvenue sur <b>Abiad Créateur de Quiz Intelligent</b> ✨\n\n"
            "💰 <b>Votre solde : {points:.2f} points</b>\n"
            "🔄 Gratuits : {free_points:.2f} · 🎁 Cadeaux : {gift_points:.2f} · 💳 Payants : {paid_points:.2f}\n\n"
            "📤 <b>Envoyez votre support de cours maintenant pour générer un quiz :</b>\n"
            "• 📄 Fichier (PDF, Word, PowerPoint)\n"
            "• 🖼 Photo nette d'une page ou diapositive\n"
            "• ✍️ Texte direct (au moins 100 caractères)\n\n"
            "Utilisez les boutons ci-dessous pour gérer vos quiz enregistrés, votre portefeuille ou vos paramètres :"
        ),
        "home_text": (
            "Bienvenue sur <b>Abiad Créateur de Quiz Intelligent</b> ✨\n\n"
            "📤 Envoyez un fichier (PDF, Word, PowerPoint), une photo ou un texte (100+ caractères) pour créer un quiz interactif !"
        ),
        "upload_text": (
            "📤 <b>Envoyez votre support de cours maintenant !</b>\n\n"
            "• 📄 Fichier : PDF, Word (.docx) ou PowerPoint (.pptx)\n"
            "• 🖼 Photo : Image nette de vos notes ou pages de cours\n"
            "• ✍️ Texte : Collez directement le texte du cours (100+ caractères)\n"
            "• 🌐 Fichier volumineux ? Utilisez le bouton de téléversement web ci-dessous."
        ),
        "help_text": (
            "❓ <b>Comment utiliser Abiad Quiz Maker</b>\n\n"
            "1️⃣ <b>Envoyez du contenu :</b> Téléversez un fichier PDF/Word/PPTX, envoyez une photo ou collez votre texte.\n"
            "2️⃣ <b>Personnalisez le quiz :</b> Choisissez le nombre de questions, la difficulté et le mode.\n"
            "3️⃣ <b>Révisez et sauvegardez :</b> Retrouvez vos quiz dans ⭐ Mes quiz enregistrés."
        ),
        "new_user_welcome": (
            "👋 Bienvenue <b>{first_name}</b> sur <b>Abiad Créateur de Quiz Intelligent</b> !\n\n"
            "🎁 Vous avez reçu <b>{points:g} points gratuits</b> pour créer des quiz IA à partir de vos cours, PDF ou photos.\n\n"
            "Appuyez sur le bouton ci-dessous ou envoyez un fichier/texte dès maintenant !"
        ),
        "new_user_btn": "🚀 Créer mon premier quiz",
        "btn_help": "❓ Comment utiliser le bot ?",
        "btn_land": "🏕️ Mon espace & Défis",
        "btn_large_upload": "📚 Fichier ou cours volumineux ? Appuyez ici",
        "btn_favorites": "⭐ Mes quiz enregistrés",
        "btn_wallet": "💰 Mon solde et mes cadeaux",
        "btn_settings": "⚙️ Paramètres",
        "btn_channel": "📢 Chaîne d'actualités",
        "btn_support": "💬 Support technique",
        "btn_home": "🏠 Menu principal",
        "btn_referral": "🎁 Inviter un ami · {pts} pts",
        "btn_channel_reward": "📢 Rejoindre la chaîne · {pts} pts",
        "channel_reward_claim_btn": "✅ Vérifier et réclamer",
    },
    "de": {
        "welcome_renewed": "🎉 Dein tägliches Guthaben wurde erneuert!\n\n",
        "welcome_body": (
            "Willkommen beim <b>Abiad Smart Quiz Maker</b> ✨\n\n"
            "💰 <b>Dein Guthaben: {points:.2f} Punkte</b>\n"
            "🔄 Gratis: {free_points:.2f} · 🎁 Geschenke: {gift_points:.2f} · 💳 Bezahlt: {paid_points:.2f}\n\n"
            "📤 <b>Sende jetzt dein Lernmaterial, um ein Quiz zu erstellen:</b>\n"
            "• 📄 Datei (PDF, Word, PowerPoint)\n"
            "• 🖼 Klares Foto einer Seite oder Folie\n"
            "• ✍️ Direkte Textnachricht (mindestens 100 Zeichen)\n\n"
            "Nutze die Schaltflächen unten für gespeicherte Quizze, Guthaben oder Einstellungen:"
        ),
        "home_text": (
            "Willkommen beim <b>Abiad Smart Quiz Maker</b> ✨\n\n"
            "📤 Sende eine Datei (PDF, Word, PowerPoint), ein Foto oder Text (100+ Zeichen), um sofort ein Quiz zu erstellen!"
        ),
        "upload_text": (
            "📤 <b>Sende jetzt dein Lernmaterial!</b>\n\n"
            "• 📄 Datei: PDF, Word (.docx) oder PowerPoint (.pptx)\n"
            "• 🖼 Foto: Klares Bild von Notizen oder Buchseiten\n"
            "• ✍️ Text: Vorlesungstext direkt einfügen (100+ Zeichen)\n"
            "• 🌐 Große Datei? Nutze den Web-Upload-Button unten."
        ),
        "help_text": (
            "❓ <b>So nutzt du den Abiad Quiz Maker</b>\n\n"
            "1️⃣ <b>Inhalt senden:</b> Lade eine PDF-/Word-/PPTX-Datei hoch, sende ein Foto oder füge Text ein.\n"
            "2️⃣ <b>Quiz anpassen:</b> Wähle Fragenanzahl, Schwierigkeit und Quizmodus.\n"
            "3️⃣ <b>Üben & Speichern:</b> Speichere deine Quizze unter ⭐ Meine gespeicherten Quizze."
        ),
        "new_user_welcome": (
            "👋 Willkommen <b>{first_name}</b> beim <b>Abiad Smart Quiz Maker</b>!\n\n"
            "🎁 Du hast <b>{points:g} Gratispunkte</b> erhalten, um KI-Quizze aus deinen Vorlesungen, PDFs oder Fotos zu erstellen.\n\n"
            "Tippe unten auf den Button oder sende direkt eine Datei oder einen Text!"
        ),
        "new_user_btn": "🚀 Erstes Quiz erstellen",
        "btn_help": "❓ Wie benutze ich den Bot?",
        "btn_land": "🏕️ Mein Bereich & Challenges",
        "btn_large_upload": "📚 Große Datei oder Vorlesung? Hier tippen",
        "btn_favorites": "⭐ Meine gespeicherten Quizze",
        "btn_wallet": "💰 Mein Guthaben & Geschenke",
        "btn_settings": "⚙️ Einstellungen",
        "btn_channel": "📢 News-Kanal",
        "btn_support": "💬 Technischer Support",
        "btn_home": "🏠 Hauptmenü",
        "btn_referral": "🎁 Freund einladen · {pts} Pkt.",
        "btn_channel_reward": "📢 Kanal abonnieren · {pts} Pkt.",
        "channel_reward_claim_btn": "✅ Prüfen & Belohnung abholen",
    },
    "fa": {
        "welcome_renewed": "🎉 اعتبار روزانه شما تمدید شد!\n\n",
        "welcome_body": (
            "به <b>آبیض - سازنده هوشمند کوئیز</b> خوش آمدید ✨\n\n"
            "💰 <b>موجودی شما: {points:.2f} امتیاز</b>\n"
            "🔄 رایگان: {free_points:.2f} · 🎁 هدیه: {gift_points:.2f} · 💳 خریداری‌شده: {paid_points:.2f}\n\n"
            "📤 <b>برای ساخت کوئیز، محتوای درسی خود را همین حالا ارسال کنید:</b>\n"
            "• 📄 فایل (PDF، Word، PowerPoint)\n"
            "• 🖼 عکس واضح از صفحه کتاب یا جزوه\n"
            "• ✍️ متن مستقیم (حداقل ۱۰۰ کاراکتر)\n\n"
            "از دکمه‌های زیر برای مدیریت کوئیزهای ذخیره‌شده، کیف پول یا تنظیمات استفاده کنید:"
        ),
        "home_text": (
            "به <b>آبیض - سازنده هوشمند کوئیز</b> خوش آمدید ✨\n\n"
            "📤 یک فایل (PDF، Word، PowerPoint)، عکس یا متن (بیش از ۱۰۰ کاراکتر) بفرستید تا بلافاصله کوئیز تعاملی ساخته شود!"
        ),
        "upload_text": (
            "📤 <b>محتوای درسی خود را اکنون ارسال کنید!</b>\n\n"
            "• 📄 فایل: PDF، Word (.docx) یا PowerPoint (.pptx)\n"
            "• 🖼 عکس: تصویر واضح از جزوه یا صفحات کتاب\n"
            "• ✍️ متن: متن درس را مستقیماً ارسال کنید (حداقل ۱۰۰ کاراکتر)\n"
            "• 🌐 فایل حجیم است؟ از دکمه بارگذاری وب در پایین استفاده کنید."
        ),
        "help_text": (
            "❓ <b>راهنمای استفاده از ربات آبیض</b>\n\n"
            "1️⃣ <b>ارسال محتوا:</b> فایل PDF/Word/PPTX، عکس یا متن درس را ارسال کنید.\n"
            "2️⃣ <b>تنظیم کوئیز:</b> تعداد سؤالات، سطح دشواری و نوع آزمون را انتخاب کنید.\n"
            "3️⃣ <b>مرور و ذخیره:</b> آزمون‌های خود را در بخش ⭐ کوئیزهای ذخیره‌شده من نگه دارید."
        ),
        "new_user_welcome": (
            "👋 <b>{first_name}</b> عزیز، به <b>آبیض - سازنده هوشمند کوئیز</b> خوش آمدید!\n\n"
            "🎁 شما <b>{points:g} امتیاز رایگان</b> برای ساخت کوئیزهای هوشمند از جزوات، فایل‌های PDF یا عکس‌ها دریافت کردید.\n\n"
            "روی دکمه زیر بزنید یا همین حالا یک فایل یا متن بفرستید!"
        ),
        "new_user_btn": "🚀 ساخت اولین کوئیز",
        "btn_help": "❓ چگونه از ربات استفاده کنم؟",
        "btn_land": "🏕️ سرزمین من و چالش‌ها",
        "btn_large_upload": "📚 فایل یا جزوه شما حجیم است؟ اینجا بزنید",
        "btn_favorites": "⭐ کوئیزهای ذخیره‌شده من",
        "btn_wallet": "💰 موجودی و هدایای من",
        "btn_settings": "⚙️ تنظیمات",
        "btn_channel": "📢 کانال اخبار",
        "btn_support": "💬 پشتیبانی فنی",
        "btn_home": "🏠 منوی اصلی",
        "btn_referral": "🎁 دعوت از دوستان · {pts} امتیاز",
        "btn_channel_reward": "📢 عضویت در کانال · {pts} امتیاز",
        "channel_reward_claim_btn": "✅ بررسی و دریافت پاداش",
    },
}


def build_settings_panel_content(
    lang: str = DEFAULT_LANGUAGE,
    enabled: bool = True,
    lang_label: Optional[str] = None,
) -> Tuple[str, types.InlineKeyboardMarkup]:
    code = normalize_language_code(lang)
    s = SETTINGS_STRINGS.get(code, SETTINGS_STRINGS[DEFAULT_LANGUAGE])
    resolved_label = lang_label or LANGUAGE_LABELS.get(code, LANGUAGE_LABELS[DEFAULT_LANGUAGE])
    status_str = s["renewal_status_on"] if enabled else s["renewal_status_off"]
    text = (
        f"{s['title']}\n\n"
        f"{s['renewal_schedule']}\n"
        f"{s['renewal_label']}: {status_str}\n"
        f"{s['lang_label']}: {resolved_label}\n\n"
        f"{s['note']}"
    )
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=s["btn_disable_renewal"] if enabled else s["btn_enable_renewal"],
                    callback_data="renewal:off" if enabled else "renewal:on",
                )
            ],
            [types.InlineKeyboardButton(text=s["btn_home"], callback_data="student_home")],
            [
                types.InlineKeyboardButton(
                    text=s["btn_lang"].format(lang_label=resolved_label),
                    callback_data="settings:lang",
                )
            ],
        ]
    )
    return text, keyboard


def get_renewal_disabled_banner(lang: str = DEFAULT_LANGUAGE) -> str:
    code = normalize_language_code(lang)
    s = SETTINGS_STRINGS.get(code, SETTINGS_STRINGS[DEFAULT_LANGUAGE])
    return s["off_banner"]


def get_wallet_strings(lang: str = DEFAULT_LANGUAGE) -> Dict[str, Any]:
    code = normalize_language_code(lang)
    return WALLET_STRINGS.get(code, WALLET_STRINGS[DEFAULT_LANGUAGE])


def get_ui_strings(lang: str = DEFAULT_LANGUAGE) -> Optional[Dict[str, str]]:
    code = normalize_language_code(lang)
    if code == "ar":
        return None
    return UI_STRINGS.get(code)


def _extract_number(text: str, default: str = "5") -> str:
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", text or "")
    return m.group(1) if m else default


def translate_main_menu_keyboard(
    markup: Optional[types.InlineKeyboardMarkup],
    lang: str = DEFAULT_LANGUAGE,
) -> Optional[types.InlineKeyboardMarkup]:
    code = normalize_language_code(lang)
    if not markup or code == "ar":
        return markup
    ui = UI_STRINGS.get(code)
    if not ui:
        return markup

    new_rows: List[List[types.InlineKeyboardButton]] = []
    for row in getattr(markup, "inline_keyboard", []) or []:
        new_row: List[types.InlineKeyboardButton] = []
        for btn in row:
            cb = getattr(btn, "callback_data", None)
            url = getattr(btn, "url", None)
            sw = getattr(btn, "switch_inline_query", None)
            if sw is None and hasattr(btn, "extra") and isinstance(btn.extra, dict):
                sw = btn.extra.get("switch_inline_query")
            text = getattr(btn, "text", "") or ""
            new_text = text

            if cb == "student_help":
                new_text = ui["btn_help"]
            elif cb == "cg:home:0":
                new_text = ui["btn_land"]
            elif cb == "upload_open:all":
                new_text = ui["btn_large_upload"]
            elif cb == "favorites_menu":
                new_text = ui["btn_favorites"]
            elif cb == "gift:wallet":
                new_text = ui["btn_wallet"]
            elif cb == "student_settings":
                new_text = ui["btn_settings"]
            elif cb == "student_home":
                new_text = ui["btn_home"]
            elif cb == "new_user_send_now":
                new_text = ui["new_user_btn"]
            elif cb == "channel_reward:open":
                pts = _extract_number(text, "10")
                new_text = ui["btn_channel_reward"].format(pts=pts)
            elif cb == "channel_reward:claim":
                new_text = ui["channel_reward_claim_btn"]
            elif sw is not None:
                pts = _extract_number(text, "5")
                new_text = ui["btn_referral"].format(pts=pts)
            elif url:
                if "قناة" in text or "اشترك" in text:
                    new_text = ui["btn_channel"]
                elif "الدعم" in text or "تواصل" in text:
                    new_text = ui["btn_support"]

            btn_kwargs: Dict[str, Any] = {"text": new_text}
            if cb is not None:
                btn_kwargs["callback_data"] = cb
            if url is not None:
                btn_kwargs["url"] = url
            if sw is not None:
                btn_kwargs["switch_inline_query"] = sw
            style = getattr(btn, "style", None)
            if style is not None:
                btn_kwargs["style"] = style
            web_app = getattr(btn, "web_app", None)
            if web_app is not None:
                btn_kwargs["web_app"] = web_app
            new_row.append(types.InlineKeyboardButton(**btn_kwargs))
        new_rows.append(new_row)
    return types.InlineKeyboardMarkup(inline_keyboard=new_rows)


def install_ui_localization_hooks() -> None:
    """يربط دالة أزرار القائمة الرئيسية وشاشات /start باللغات الـ 6 تلقائياً."""
    try:
        kb_mod = sys.modules.get("keyboards")
        if kb_mod is None:
            import keyboards as kb_mod  # type: ignore
        orig_get_main_menu = getattr(kb_mod, "get_main_menu_keyboard", None)
        if orig_get_main_menu and not getattr(orig_get_main_menu, "_i18n_wrapped", False):
            @functools.wraps(orig_get_main_menu)
            async def _localized_get_main_menu_keyboard(
                bot_username: str,
                user_id: Optional[int] = None,
                *args: Any,
                **kwargs: Any,
            ) -> types.InlineKeyboardMarkup:
                kb = await orig_get_main_menu(bot_username, user_id, *args, **kwargs)
                if user_id is None:
                    return kb
                try:
                    lang = await get_user_language(int(user_id))
                    return translate_main_menu_keyboard(kb, lang) or kb
                except Exception:
                    return kb

            _localized_get_main_menu_keyboard._i18n_wrapped = True  # type: ignore[attr-defined]
            kb_mod.get_main_menu_keyboard = _localized_get_main_menu_keyboard
    except Exception as exc:
        logger.debug("Skipping keyboards hook: %s", exc)

    try:
        start_mod = sys.modules.get("handlers.start")
        if start_mod is None:
            return
        from helpers import student_ui

        kb_mod = sys.modules.get("keyboards")
        if kb_mod and hasattr(kb_mod, "get_main_menu_keyboard"):
            start_mod.get_main_menu_keyboard = kb_mod.get_main_menu_keyboard

        orig_edit_screen = getattr(start_mod, "_edit_student_screen", None)
        if orig_edit_screen and not getattr(orig_edit_screen, "_i18n_wrapped", False):
            @functools.wraps(orig_edit_screen)
            async def _localized_edit_student_screen(call: Any, text: str, markup: Any = None, *a: Any, **kw: Any):
                try:
                    lang = await get_user_language(call.from_user.id, fallback_user_or_code=call.from_user)
                    if normalize_language_code(lang) != "ar":
                        if text == getattr(student_ui, "UPLOAD_TEXT", None):
                            text = student_ui.get_upload_text(lang)
                        elif text == getattr(student_ui, "HELP_TEXT", None):
                            text = student_ui.get_help_text(lang)
                        elif text == getattr(student_ui, "HOME_TEXT", None):
                            text = student_ui.get_home_text(lang)
                        markup = translate_main_menu_keyboard(markup, lang) or markup
                except Exception:
                    pass
                return await orig_edit_screen(call, text, markup, *a, **kw)

            _localized_edit_student_screen._i18n_wrapped = True  # type: ignore[attr-defined]
            start_mod._edit_student_screen = _localized_edit_student_screen

        orig_home_text = getattr(start_mod, "_student_home_text", None)
        if orig_home_text and not getattr(orig_home_text, "_i18n_wrapped", False):
            @functools.wraps(orig_home_text)
            async def _localized_student_home_text(user_id: int) -> str:
                lang = await get_user_language(user_id)
                if normalize_language_code(lang) == "ar":
                    return await orig_home_text(user_id)
                try:
                    from helpers.supabase_helper import supabase
                    user = (
                        await supabase.table("users")
                        .select("points, paid_points")
                        .eq("user_id", user_id)
                        .maybe_single()
                        .execute()
                    )
                    row = getattr(user, "data", None)
                    if not row:
                        return student_ui.get_home_text(lang)
                    free = float(row.get("points") or 0)
                    paid = float(row.get("paid_points") or 0)
                    gifts = await start_mod.gift_balances(user_id)
                    gift = float(gifts.get(user_id, 0.0))
                    return student_ui.welcome_text(free + paid + gift, free, gift, paid, lang=lang)
                except Exception:
                    return student_ui.get_home_text(lang)

            _localized_student_home_text._i18n_wrapped = True  # type: ignore[attr-defined]
            start_mod._student_home_text = _localized_student_home_text

        orig_start = getattr(start_mod, "start", None)
        if orig_start and not getattr(orig_start, "_i18n_wrapped", False):
            @functools.wraps(orig_start)
            async def _localized_start(msg: types.Message, command: Any = None, state: Any = None, *a: Any, **kw: Any):
                args_str = getattr(command, "args", None) if command else None
                if args_str:
                    return await orig_start(msg, command, state, *a, **kw)
                lang = await get_user_language(msg.from_user.id, fallback_user_or_code=msg.from_user)
                if normalize_language_code(lang) == "ar":
                    return await orig_start(msg, command, state, *a, **kw)
                ui = get_ui_strings(lang)
                if not ui:
                    return await orig_start(msg, command, state, *a, **kw)

                user_data = await start_mod.check_or_add_user(
                    msg.from_user.id,
                    msg.from_user.username or "Unknown",
                    msg.from_user.first_name,
                    msg.from_user.last_name or "Unknown",
                )
                if user_data.get("status") == "error":
                    return await orig_start(msg, command, state, *a, **kw)

                try:
                    start_mod.track_usage(msg.from_user.id, "bot_start", {"status": user_data["status"]})
                except Exception:
                    pass

                if user_data["status"] == "new":
                    welcome_new = ui["new_user_welcome"].format(
                        first_name=msg.from_user.first_name or "",
                        points=float(user_data.get("points", 30)),
                    )
                    kb_new = types.InlineKeyboardMarkup(
                        inline_keyboard=[
                            [types.InlineKeyboardButton(text=ui["new_user_btn"], callback_data="new_user_send_now")]
                        ]
                    )
                    await msg.answer(welcome_new, parse_mode="HTML", reply_markup=kb_new)
                    return

                bot_info = await start_mod.get_bot_info(msg.bot)
                text = student_ui.welcome_text(
                    user_data["points"],
                    user_data.get("free_points", 0),
                    user_data.get("gift_points", 0),
                    user_data.get("paid_points", 0),
                    user_data["status"] == "renewed",
                    lang=lang,
                )
                kb = await start_mod.get_main_menu_keyboard(bot_info.username, msg.from_user.id)
                kb = translate_main_menu_keyboard(kb, lang) or kb
                await msg.answer(text, parse_mode="HTML", reply_markup=kb)

            _localized_start._i18n_wrapped = True  # type: ignore[attr-defined]
            start_mod.start = _localized_start
            for h in getattr(start_mod.router.message, "handlers", []):
                if getattr(h, "callback", None) is orig_start:
                    h.callback = _localized_start

        orig_help = getattr(start_mod, "student_help", None)
        if orig_help and not getattr(orig_help, "_i18n_wrapped", False):
            @functools.wraps(orig_help)
            async def _localized_student_help(msg: types.Message, *a: Any, **kw: Any):
                lang = await get_user_language(msg.from_user.id, fallback_user_or_code=msg.from_user)
                if normalize_language_code(lang) == "ar":
                    return await orig_help(msg, *a, **kw)
                kb = translate_main_menu_keyboard(start_mod._student_info_keyboard(), lang)
                await msg.answer(student_ui.get_help_text(lang), parse_mode="HTML", reply_markup=kb)

            _localized_student_help._i18n_wrapped = True  # type: ignore[attr-defined]
            start_mod.student_help = _localized_student_help
            for h in getattr(start_mod.router.message, "handlers", []):
                if getattr(h, "callback", None) is orig_help:
                    h.callback = _localized_student_help

        orig_ack = getattr(start_mod, "ack_new_user_start", None)
        if orig_ack and not getattr(orig_ack, "_i18n_wrapped", False):
            @functools.wraps(orig_ack)
            async def _localized_ack_new_user_start(call: types.CallbackQuery, *a: Any, **kw: Any):
                lang = await get_user_language(call.from_user.id, fallback_user_or_code=call.from_user)
                if normalize_language_code(lang) == "ar":
                    return await orig_ack(call, *a, **kw)
                kb = translate_main_menu_keyboard(start_mod._student_info_keyboard(), lang)
                try:
                    await call.message.edit_text(
                        student_ui.get_upload_text(lang),
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                except Exception:
                    await call.message.answer(
                        student_ui.get_upload_text(lang),
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                await call.answer()

            _localized_ack_new_user_start._i18n_wrapped = True  # type: ignore[attr-defined]
            start_mod.ack_new_user_start = _localized_ack_new_user_start
            for h in getattr(start_mod.router.callback_query, "handlers", []):
                if getattr(h, "callback", None) is orig_ack:
                    h.callback = _localized_ack_new_user_start
    except Exception as exc:
        logger.debug("Deferred start hooks: %s", exc)
