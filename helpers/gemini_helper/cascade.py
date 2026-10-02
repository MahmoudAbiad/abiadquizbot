"""
helpers/gemini_helper/cascade.py
سلسلة الأولوية (النماذج × المفاتيح): الحظر/التبريد، Round-Robin، و_execute_cascade.
"""

from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
import datetime

from google import genai

from constants import QUOTA_ERROR_KEYWORDS
from logger import get_logger, log_error, log_info, log_warning
from helpers.ai_models_helper import get_cascade_models
from helpers.gemini_helper.clients import (
    API_KEYS,
    VERTEX_KEY_INDEX,
    _GEMINI_CLIENTS,
    _VERTEX_CLIENT,
)
from helpers.gemini_helper.state import _last_model_used_var

logger = get_logger(__name__)


# ==============================================================================
# 🆕 MODEL WATERFALL CASCADE + PER-(KEY, MODEL) ROUND-ROBIN STATE
# ==============================================================================
# 🆕 سلسلة أولوية النماذج لم تعد ثابتة بالكود - انتقلت لجدول ai_model_slots بسوبا بيس
# (slot="cascade") وتُدار الآن مباشرة من لوحة تحكم الأدمن (handlers/admin/ai_control.py)
# دون أي حاجة لتعديل الكود أو إعادة نشر البوت: إعادة ترتيب، تفعيل/تعطيل، إضافة/حذف موديل.
# راجع helpers/ai_models_helper.py::get_cascade_models للتفاصيل والكاش وخط الأمان الافتراضي
# (نفس القيم الأربعة اللي كانت هون ثابتة سابقاً، مزروعة بالجدول كقيم أولية).
# الحلقة الخارجية بمنطق التنفيذ (_execute_cascade) تستنفد كل المفاتيح على النموذج الحالي
# قبل النزول للنموذج التالي بالسلسلة، بنفس الترتيب المُخزَّن بـ display_order.
async def _get_models_cascade() -> List[Dict[str, str]]:
    """يرجع قائمة {'provider', 'model_name'} بالترتيب لسلسلة الـ cascade، للمزوّدين
    المربوطين فعلياً بتنفيذ SDK هنا: Gemini (AI Studio، بالتناوب على API_KEYS) وVertex AI
    (Google Cloud، عبر _VERTEX_CLIENT أعلاه). راجع ملاحظة helpers/ai_models_helper.py حول
    القيود الحالية على أي مزوّد آخر (groq/openai) ضمن هذا الـ slot تحديداً."""
    entries = await get_cascade_models()
    supported = [
        {"provider": e["provider"], "model_name": e["model_name"]}
        for e in entries if e.get("provider") in ("gemini", "vertex")
    ]
    if not supported:
        log_warning(logger, "No enabled Gemini/Vertex models in cascade slot; falling back to gemini-3.5-flash-lite")
        return [{"provider": "gemini", "model_name": "gemini-3.5-flash-lite"}]
    unsupported = [e for e in entries if e.get("provider") not in ("gemini", "vertex")]
    if unsupported:
        skipped = ", ".join(f"{e.get('provider')}:{e.get('model_name')}" for e in unsupported)
        log_warning(logger, f"Skipping unsupported-provider cascade entries (no SDK integration wired yet): {skipped}")
    return supported


# AI-NOTE: تتبّع دقيق لكل زوج (فهرس المفتاح، اسم النموذج) على حدة - بدل حظر المفتاح
# بالكامل عبر كل النماذج، هيك حظر مفتاح على نموذج مُعيّن (بسبب حصته انتهت مثلاً) لا يمنعه
# إطلاقاً من الاستمرار بالعمل على نموذج آخر من السلسلة بنفس اللحظة.
blocked_model_keys: Dict[Tuple[int, str], datetime.datetime] = {}

# AI-NOTE: مؤشر عام (Round-Robin) يحدد أي مفتاح نبدأ منه بكل طلب توليد جديد، بحيث تتوزع
# الطلبات المتتالية على كل المفاتيح المتاحة بالتساوي تقريباً بدل التحيّز الدائم لأول مفتاح.
current_key_pointer: int = 0

# مدة حظر (مفتاح، نموذج) عند نفاد الحصة (Quota Exhaustion: 429 / resource_exhausted / quota)
MODEL_KEY_BLOCK_QUOTA_HOURS = 24
# مدة التبريد القصيرة عند ازدحام/تعطّل مؤقت بالسيرفر (503 / 500 / overloaded / unavailable)
# أو أي خطأ آخر غير متوقع - لتفادي "حلقة ساخنة" (hot-loop) على نفس الزوج (مفتاح، نموذج) العاطل
# بينما بقية المفاتيح/النماذج بالسلسلة متاحة وجاهزة للتجربة فوراً.
MODEL_KEY_BLOCK_OVERLOAD_MINUTES = 1

# AI-NOTE: كلمات مفتاحية لتحديد أخطاء الضغط والازدحام في سيرفرات Gemini
OVERLOAD_ERROR_KEYWORDS = ["overloaded", "unavailable", "503", "internal error", "500"]

# 🆕 (طلب صريح من المستخدم) بدل إعادة محاولة نفس الزوج (مفتاح، موديل) عدة مرات مع تأخير
# تصاعدي قبل الانتقال للتالي - كل زوج يُجرَّب مرة واحدة بالضبط. لو غير متاح فوراً (أي خطأ
# غير FileNotFoundError)، يُحظر فوراً (_mark_model_key_failure) وينتقل الـ cascade فوراً
# للزوج التالي بلا أي انتظار. هذا يقلّل زمن استجابة الطالب بشكل كبير عند تعطّل موديل ما،
# على حساب عدم إعطاء نفس الزوج "فرصة ثانية" لازدحام عابر جداً (< ثانية) - مقبول لأن باقي
# السلسلة (نماذج × مفاتيح أخرى) توفر مسار احتياطي فوري أصلاً.

# ==============================================================================
# 🆕 MODEL WATERFALL + ROUND-ROBIN KEY MANAGEMENT
# ==============================================================================
def _is_model_key_blocked(key_index: int, model: str) -> bool:
    """فحص لحظي (بدون أي تأخير - بمعنى zero-delay) على القاموس بالذاكرة لمعرفة إذا كان
    هذا الزوج (مفتاح، نموذج) بالذات محظوراً حالياً. يُنظّف تلقائياً أي إدخال انتهت صلاحيته."""
    blocked_until = blocked_model_keys.get((key_index, model))
    if blocked_until is None:
        return False
    if datetime.datetime.now() >= blocked_until:
        blocked_model_keys.pop((key_index, model), None)
        return False
    return True


def _mark_model_key_failure(key_index: int, model: str, error: Exception) -> None:
    """حظر الزوج (مفتاح، نموذج) الفاشل لفترة محددة حسب نوع الخطأ:
    - نفاد الحصة (429/resource_exhausted/quota) → حظر 24 ساعة على هذا الزوج تحديداً.
    - ازدحام/تعطّل مؤقت (503/500/overloaded/unavailable) → تبريد دقيقة واحدة فقط.
    - أي خطأ آخر غير متوقع → نفس تبريد الدقيقة الواحدة (تحوّطاً من حلقة ساخنة على زوج
      عاطل بشكل دائم، دون معاقبته بحظر طويل غير مبرر كما بحالة نفاد الحصة الصريحة)."""
    message = str(error).lower()
    now = datetime.datetime.now()
    if any(keyword in message for keyword in QUOTA_ERROR_KEYWORDS):
        until = now + datetime.timedelta(hours=MODEL_KEY_BLOCK_QUOTA_HOURS)
        reason = "نفاد الحصة (quota)"
    else:
        until = now + datetime.timedelta(minutes=MODEL_KEY_BLOCK_OVERLOAD_MINUTES)
        reason = "ازدحام/تعطّل مؤقت (overload)"
    blocked_model_keys[(key_index, model)] = until

    # 🆕 (طلب صريح من المستخدم) سطر واضح ومميّز بالـ logs الحية (stdout/stderr - تظهر
    # مباشرة بـ `heroku logs --tail`) عند حظر أي زوج (مفتاح، موديل)، بغض النظر من أي
    # مسار استُدعيت منه (cascade العادي أو Super PDF/Images). نفاد الحصة أخطر (يُحظر
    # 24 ساعة) فيُسجَّل كـ log_error ليبرز بوضوح بين رسائل الـ INFO العادية؛ التبريد
    # القصير (دقيقة واحدة) يُسجَّل كـ log_warning فقط.
    block_msg = (
        f"🚫 BLOCKED (key={key_index}, model={model}) — السبب: {reason} — "
        f"محظور حتى: {until.strftime('%Y-%m-%d %H:%M:%S')} — الخطأ الأصلي: {error}"
    )
    if any(keyword in message for keyword in QUOTA_ERROR_KEYWORDS):
        log_error(logger, block_msg)
    else:
        log_warning(logger, block_msg)


def _is_overload_error(error: Exception) -> bool:
    """التحقق مما إذا كان الخطأ ناتجاً عن ضغط/ازدحام مؤقت في سيرفرات AI."""
    message = str(error).lower()
    return any(keyword in message for keyword in OVERLOAD_ERROR_KEYWORDS)


def _round_robin_key_order() -> List[int]:
    """يبني ترتيب تجربة المفاتيح بدءاً من current_key_pointer الحالي (دورانياً عبر كل
    المفاتيح المتوفرة)، ثم يُقدّم المؤشر العام خطوة واحدة استعداداً لطلب التوليد التالي -
    هيك تتوزع الطلبات المتعاقبة على كل المفاتيح بالتساوي تقريباً (Round-Robin)."""
    global current_key_pointer
    total = len(API_KEYS)
    if total == 0:
        return []
    start = current_key_pointer % total
    order = [(start + offset) % total for offset in range(total)]
    current_key_pointer = (start + 1) % total
    return order


async def _execute_cascade(
    attempt_fn: Callable[[genai.Client, int, str, int, int], Awaitable[Any]],
    exclude_vertex: bool = False,
    mode: str = "regular",
) -> Optional[Any]:
    """المنفّذ العام لسلسلة الأولوية الكاملة:
    - الحلقة الخارجية: تمشي على صفوف الـ cascade (provider + model) من الأذكى للأضعف
      بترتيب display_order (لوحة الأدمن) - بغض النظر عن المزوّد (Gemini أو Vertex).
    - لكل صف provider="gemini": حلقة داخلية على كل مفاتيح AI Studio بدءاً من
      current_key_pointer (Round-Robin)، بنفس المنطق الأصلي تماماً.
    - لكل صف provider="vertex": محاولة واحدة فقط عبر _VERTEX_CLIENT (عميل واحد، لا مجموعة
      مفاتيح) - يُتخطى بصمت لو العميل غير مُعدّ (متغيرات بيئة ناقصة، راجع أعلى الملف).
      🆕 هذا بالضبط ما يمنح التحكم بأولوية Vertex مقابل مفاتيح AI Studio: مكانه بترتيب
      السلسلة (⬆️⬇️ بلوحة الأدمن) هو من يقرر متى يُجرَّب - لا حاجة لأي منطق أولوية إضافي.
    - فحص لحظي بالذاكرة (بدون أي تأخير) ضد blocked_model_keys لتفادي أي زوج (مفتاح، نموذج)
      محظور حالياً، والانتقال فوراً للزوج التالي.
    - 🆕 كل زوج (مفتاح، موديل) يُجرَّب مرة واحدة بالضبط - بدون أي إعادة محاولة أو انتظار
      على نفس الزوج. أي فشل (بما فيه ازدحام 503 مؤقت) يُحظر فوراً وينتقل الـ cascade
      فوراً للزوج التالي (راجع _mark_model_key_failure لمدة الحظر حسب نوع الخطأ).

    🆕 exclude_vertex: يستثني صفوف provider="vertex" من هذه السلسلة تحديداً (تُستخدم من
    مساري Super PDF/Super Images المتوازيين - راجع _generate_super_pdf/_generate_super_images).
    السبب: عميل Vertex واحد فقط (لا مجموعة مفاتيح)، وquota الـ Gemini Enterprise Agent
    Platform (الاسم الجديد لـ Vertex AI) محسوبة على مستوى المشروع كله (per_project_per_
    base_model) وليس لكل client/عملية على حدة - فلو 3 أجزاء متوازية كلهن اختاروا Vertex
    بنفس اللحظة، بيضربوا نفس الـ quota pool المشترك دفعة وحدة بدل التوزّع على 3 مصادر
    مستقلة كما هو الهدف من التوازي هون. الاستثناء ينطبق فقط على هذا النداء (المسار
    العادي غير المتوازي بيضل يقدر يستخدم Vertex عادي، بأولويته بالسلسلة كما هي).

    🆕 mode: وسم نصي حر ("regular"، "super_pdf"، "super_images"...) يُحفظ ضمن
    _last_model_used_var["mode"] بعد كل نجاح - يُقرأ لاحقاً بلوحة الأدمن ("📊 سجل توليد
    الكويزات") لعرض نوع العملية المتبعة. القيمة الافتراضية "regular" تغطي كل الاستدعاءات
    الحالية (generate_structured_with_cascade/generate_text_with_cascade/_generate_regular)
    بدون أي تعديل عليها. مساري Super PDF/Images بيمرّروا mode الخاص فيهن صراحة، وبيتجاوزوا
    هالقيمة بأي حال بـ _last_model_used_var.set() الخاص فيهن بالدالة الأم (راجع الملاحظة
    عن ContextVar وasyncio.gather أعلى _build_super_chunk_attempt_fn) - فمرّرتها هون بس
    لضمان صحة القيمة حتى لو استُدعيت _execute_cascade مستقبلاً من مسار جديد ما بيعمل
    override صريح متل هالاثنين.

    🆕 cascade_rank/cascade_total تُمرَّر الآن أيضاً لـ attempt_fn نفسها (وليس فقط
    محفوظة بـ _last_model_used_var) - ضروري لمسار Super PDF/Images تحديداً حيث كل جزء
    Task منفصل عبر asyncio.gather، فلازم attempt_fn ترجّع رقم ترتيبها صراحة بقيمة
    العودة (راجع _build_super_chunk_attempt_fn) بدل الاعتماد على ContextVar اللي ما
    بيترشح لمهمة الأب."""
    if not API_KEYS and (exclude_vertex or _VERTEX_CLIENT is None):
        log_error(logger, "GEMINI_API_KEYS is not configured" + ("" if exclude_vertex else " and Vertex client is not initialized"))
        return None

    key_order = _round_robin_key_order()
    last_exc: Optional[Exception] = None
    models_cascade = await _get_models_cascade()
    cascade_total = len(models_cascade)

    for cascade_rank, entry in enumerate(models_cascade, start=1):
        provider = entry["provider"]
        model = entry["model_name"]

        if provider == "vertex":
            if exclude_vertex:
                continue  # مُستبعد صراحة بهذا النداء (Super PDF/Images) - راجع الملاحظة أعلى الدالة
            if _VERTEX_CLIENT is None:
                continue  # غير مُعدّ حالياً على هذا السيرفر - يُتخطى بصمت، السلسلة تكمل بباقي الصفوف
            if _is_model_key_blocked(VERTEX_KEY_INDEX, model):
                continue
            try:
                result = await attempt_fn(_VERTEX_CLIENT, VERTEX_KEY_INDEX, model, cascade_rank, cascade_total)
                log_info(logger, f"✅ Cascade success: provider=vertex model={model} rank={cascade_rank}/{cascade_total}")
                # 🆕 cascade_rank/cascade_total: ترتيب هذا الصف ضمن سلسلة الأولوية *لحظة*
                # نجاح هذا التوليد تحديداً (لا الترتيب الحالي - قد يتغيّر لاحقاً من لوحة
                # الأدمن) - يُقرأ بشاشة "📊 سجل توليد الكويزات" لعرض "كان ترتيبه #كذا حين
                # تولّد هذا الكويز بالذات"، حتى لو تغيّر ترتيب الكاسكيد بعدها.
                _last_model_used_var.set({
                    "provider": "vertex", "model": model, "key_index": VERTEX_KEY_INDEX,
                    "cascade_rank": cascade_rank, "cascade_total": cascade_total, "mode": mode,
                })
                return result
            except Exception as exc:
                last_exc = exc
                if isinstance(exc, FileNotFoundError):
                    log_error(logger, f"Local file missing during Vertex upload attempt (not a key/model issue, aborting cascade without penalizing keys): {exc}")
                    return None
                _mark_model_key_failure(VERTEX_KEY_INDEX, model, exc)
            continue

        # provider == "gemini" (المسار الأصلي، بدون أي تعديل بالمنطق)
        if not API_KEYS:
            continue
        for key_index in key_order:
            if _is_model_key_blocked(key_index, model):
                continue
            client = _GEMINI_CLIENTS[key_index]
            try:
                result = await attempt_fn(client, key_index, model, cascade_rank, cascade_total)
                # 🆕 سطر تأكيد وحيد عند النجاح - يوضّح بالـ logs مباشرة (بدون الرجوع
                # لقاعدة البيانات) إن السلسلة الديناميكية (ai_model_slots) هي فعلاً
                # اللي حُكّمت هون: أي موديل نجح، بأي ترتيب، وبأي مفتاح.
                log_info(logger, f"✅ Cascade success: provider=gemini model={model} key_index={key_index} rank={cascade_rank}/{cascade_total}")
                # 🆕 تسجيل الموديل الفائز لهذا الـ Task الحالي - يُقرأ لاحقاً من
                # generate_quiz_smart لبناء بيانات التتبع (لوحة الأدمن). cascade_rank/total
                # راجع نفس الملاحظة أعلى فرع provider=="vertex".
                _last_model_used_var.set({
                    "provider": "gemini", "model": model, "key_index": key_index,
                    "cascade_rank": cascade_rank, "cascade_total": cascade_total, "mode": mode,
                })
                return result
            except Exception as exc:
                last_exc = exc
                # 🩹 إصلاح خلل حقيقي: FileNotFoundError خطأ محلي (ملف حُذف من القرص من
                # جهتنا) لا علاقة له إطلاقاً بحالة مفتاح/موديل Gemini الفعلية. معاملته
                # كفشل عادي كانت تحظر كل زوج (مفتاح، موديل) لدقيقة كاملة (نفس منطق أي
                # خطأ "آخر غير متوقع" بـ _mark_model_key_failure) رغم أنه سيتكرر حتماً
                # على كل الأزواج التالية بما إن الملف نفسه مفقود لا محالة - هذا كان
                # يُفشّل أي طلب آخر (حتى لطالب مختلف كلياً) خلال تلك الدقيقة بلا أي
                # محاولة فعلية ("no available (key, model) pairs" باللوغز). نوقف
                # الـ cascade فوراً هنا بدل حظر أي زوج أو تجربة باقي المفاتيح/الموديلات.
                if isinstance(exc, FileNotFoundError):
                    log_error(logger, f"Local file missing during Gemini upload attempt (not a key/model issue, aborting cascade without penalizing keys): {exc}")
                    return None
                _mark_model_key_failure(key_index, model, exc)

    if last_exc:
        log_error(logger, f"Model waterfall cascade exhausted across all models/keys: {last_exc}")
    else:
        log_error(logger, "Model waterfall cascade exhausted: no available (key, model) pairs")
    return None
