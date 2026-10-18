"""Private Telegram admin: discover, probe, and enable Google API models."""
import asyncio
import math
import time
from html import escape

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext

from helpers.ai_models_helper import (
    SLOT_DETECTION, add_model, get_all_models, remove_model, toggle_model, update_model,
)
from helpers.ai_providers_helper import get_providers
from services.ai.google_catalog import CatalogError, list_working_google_models, probe_google_model
from services.ai.runtime import credentials_for
from .admin_utils import safe_edit_text
from .dashboard import IsAdminFilter

router = Router()
router.callback_query.filter(IsAdminFilter(), F.message.chat.type == "private")

PAGE_SIZE = 7
CATALOG_TTL_SECONDS = 600
PROBE_TTL_SECONDS = 300
_save_lock = asyncio.Lock()


def _kb(rows):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=text, callback_data=callback) for text, callback in row]
        for row in rows
    ])


def _google_providers(providers):
    return [row for row in providers.values()
            if row.get("transport") == "gemini" and row.get("is_enabled")]


async def _get_current_catalog(state):
    data = await state.get_data()
    if not isinstance(data.get("gmc_models"), list) or time.time() - data.get("gmc_timestamp", 0) > CATALOG_TTL_SECONDS:
        return None
    provider_id = data.get("gmc_provider")
    provider = (await get_providers()).get(provider_id)
    if not provider or provider.get("transport") != "gemini" or not provider.get("is_enabled"):
        return None
    keys = credentials_for(provider)
    if not keys:
        return None
    return data, provider, keys[0]


async def _render_page(message, state, page=0):
    current = await _get_current_catalog(state)
    if not current:
        await safe_edit_text(
            message, "انتهت صلاحية قائمة النماذج أو تغيّرت إعدادات Google. اضغط تحديث.",
            reply_markup=_kb([[("🔄 تحديث الكتالوج", "gmc:start")],
                              [("🔙 لوحة الذكاء الاصطناعي", "admin_ai_menu")]]))
        return
    data, _, _ = current
    models = data["gmc_models"]
    stats = data.get("gmc_stats") or {}
    count_pages = max(1, math.ceil(len(models) / PAGE_SIZE))
    page = max(0, min(page, count_pages - 1))
    rows = []
    for index in range(page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, len(models))):
        model = models[index]
        rows.append([(f"🧪 {model[:45]}", f"gmc:test:{index}")])
    if count_pages > 1:
        nav = []
        if page:
            nav.append(("◀️ السابق", f"gmc:page:{page - 1}"))
        if page + 1 < count_pages:
            nav.append(("التالي ▶️", f"gmc:page:{page + 1}"))
        rows.append(nav)
    rows.append([("🔄 تحديث من Google", "gmc:start")])
    rows.append([("🔙 سلسلة فحص المحتوى", "admin_ai_slot_detection")])
    text = (
        "🔎 <b>نماذج Google المناسبة لفحص المحتوى</b>\n"
        f"المزوّد: <code>{escape(data['gmc_provider'])}</code>\n"
        f"✅ نجحت بفحص الصورة + JSON: <b>{len(models)}</b> "
        f"من {stats.get('candidates', 0)} مرشّح حديث\n"
        f"⏳ تعذّر التحقق بسبب المهلة: {stats.get('timeout', 0)} "
        f"| الحصة (429): {stats.get('quota', 0)} "
        f"| أسباب أخرى: {stats.get('other', 0)}\n"
        f"صفحة {page + 1}/{count_pages}\n\n"
        + ("" if models else
           "لم يثبت عمل أي نموذج ضمن الحصة المتاحة حالياً. "
           "جرّب التحديث لاحقاً؛ 429 والمهلة لا يعنيان أن النموذج متقاعد.\n\n")
        + "القائمة لا تعرض نماذج توليد الصور/الصوت أو النسخ القديمة. "
        "اضغط على نموذج لإجراء اختبار النص والصورة الكامل قبل تفعيله.\n\n"
        "⚠️ النجاح لا يضمن المجانية أو بقاء الحصة. "
        "الفحص المباشر قد يستهلك توكنات مدفوعة حسب مشروع Google."
    )
    await safe_edit_text(message, text, reply_markup=_kb(rows))


async def _load_provider(call, state, provider_id):
    providers = await get_providers(force_refresh=True)
    provider = providers.get(provider_id)
    if not provider or provider.get("transport") != "gemini" or not provider.get("is_enabled"):
        await safe_edit_text(call.message, "مزوّد Google غير متاح أو معطّل.",
                             reply_markup=_kb([[("رجوع", "admin_ai_menu")]]))
        return
    keys = credentials_for(provider)
    if not keys:
        await safe_edit_text(
            call.message, "متغير مفتاح Google غير موجود في Render.",
            reply_markup=_kb([[("رجوع", "admin_ai_menu")]]))
        return
    await safe_edit_text(
        call.message,
        "🔎 أجلب النماذج الحديثة المناسبة، ثم أتحقق من كل مرشح "
        "بصورة حسابية اصطناعية وتصنيف JSON. قد يستغرق الفحص دقيقة ونصف "
        "عند بطء Google، ويستهلك طلباً واحداً لكل مرشح لم يُختبر حديثاً.")
    try:
        models, stats = await list_working_google_models(keys[0])
    except CatalogError as exc:
        await safe_edit_text(call.message, escape(str(exc)),
                             reply_markup=_kb([[("إعادة المحاولة", "gmc:start")],
                                               [("رجوع", "admin_ai_menu")]]))
        return
    await state.update_data(
        gmc_provider=provider_id,
        gmc_models=[m["name"] for m in models],
        gmc_stats=stats,
        gmc_timestamp=time.time(),
        gmc_verified=None,
    )
    await _render_page(call.message, state)


@router.callback_query(F.data == "gmc:start")
async def open_google_catalog(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await safe_edit_text(
        call.message,
        "🔎 <b>كتالوج نماذج Google لفحص المحتوى</b>\n\n"
        "رح يتم استبعاد النماذج القديمة ونماذج توليد الصور والصوت والنسخ غير المناسبة. "
        "بعدها نختبر المرشحين بطلب API حقيقي بصورة صغيرة وJSON، "
        "ولا نعرض إلا ما نجح الآن.\n\n"
        "⚠️ الاختبارات قد تستهلك حصة API، وربما تكون مدفوعة بحسب مشروعك. "
        "انتهاء الحصة (429) أو التأخر لا يعني توقف النموذج نهائياً.\n\n"
        "هل تريد بدء التحقق؟",
        reply_markup=_kb([[("✅ تحقق واعرض النماذج العاملة", "gmc:scan")],
                          [("رجوع", "admin_ai_menu")]]))


@router.callback_query(F.data == "gmc:scan")
async def scan_google_catalog(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    providers = _google_providers(await get_providers(force_refresh=True))
    if not providers:
        await safe_edit_text(
            call.message, "لا يوجد مزوّد Gemini API مفعّل. أضفه أولاً من إدارة المزوّدين.",
            reply_markup=_kb([[("🔌 إدارة المزوّدين", "aip:list")],
                              [("رجوع", "admin_ai_menu")]]))
        return
    if len(providers) == 1:
        await _load_provider(call, state, providers[0]["id"])
        return
    rows = [[(p["label"][:45], "gmc:provider:" + p["id"])] for p in providers]
    rows.append([("رجوع", "admin_ai_menu")])
    await safe_edit_text(call.message, "اختر مفتاح/مزوّد Google الذي تريد عرض كتالوجه:",
                         reply_markup=_kb(rows))


@router.callback_query(F.data.startswith("gmc:provider:"))
async def choose_google_provider(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await _load_provider(call, state, call.data.removeprefix("gmc:provider:"))


@router.callback_query(F.data.startswith("gmc:page:"))
async def turn_catalog_page(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    try:
        page = int(call.data.rsplit(":", 1)[-1])
    except ValueError:
        return
    await _render_page(call.message, state, page)


@router.callback_query(F.data.startswith("gmc:test:"))
async def test_catalog_model(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    current = await _get_current_catalog(state)
    if not current:
        await _render_page(call.message, state)
        return
    data, provider, key = current
    try:
        index = int(call.data.rsplit(":", 1)[-1])
        model = data["gmc_models"][index] if 0 <= index < len(data["gmc_models"]) else None
    except (ValueError, TypeError):
        model = None
    if not model:
        await call.message.answer("انتهى اختيار النموذج؛ أعد فتح القائمة.")
        return
    await state.update_data(gmc_verified=None)
    await safe_edit_text(call.message,
        f"🧪 جاري فحص <code>{escape(model)}</code>\n"
        "اختبار نص عربي مع JSON ثم صورة تجريبية مع JSON.\n"
        "قد يستغرق الفحص حتى 50 ثانية عند بطء المزوّد، "
        "وقد يُحتسب من حصة Google.")
    try:
        result = await probe_google_model(key, model)
    except CatalogError as exc:
        await safe_edit_text(call.message, escape(str(exc)),
                            reply_markup=_kb([[("🔙 القائمة", "gmc:page:0")]]))
        return
    # Re-check the provider and the selected snapshot before permitting activation.
    latest = await _get_current_catalog(state)
    if not latest or latest[0]["gmc_provider"] != provider["id"]:
        await safe_edit_text(call.message, "انتهت صلاحية الجلسة خلال الاختبار؛ حدّث الكتالوج.",
                            reply_markup=_kb([[("تحديث", "gmc:start")]]))
        return
    if result.ready:
        await state.update_data(gmc_verified={
            "provider": provider["id"], "name": model,
            "timestamp": time.time(), "caps": ["text", "image"],
        })
    rows = []
    if result.ready:
        rows.append([("✅ أضف وفعّل في فحص المحتوى", f"gmc:enable:{index}")])
    rows.append([("🔄 إعادة الاختبار", f"gmc:test:{index}")])
    rows.append([("🔙 قائمة النماذج", "gmc:page:0")])
    timed_out = "انتهت المهلة" in result.text_reason or "انتهت المهلة" in result.image_reason
    text = (
        f"🧪 <b>نتيجة فحص {escape(model)}</b>\n\n"
        f"{'✅' if result.text_ok else '❌'} النص العربي + JSON: {escape(result.text_reason)} "
        f"({result.text_elapsed_ms / 1000:.1f} ث)\n"
        f"{'✅' if result.image_ok else '❌'} الصورة + JSON: {escape(result.image_reason)} "
        f"({result.image_elapsed_ms / 1000:.1f} ث)\n"
        f"⏱ الإجمالي: {result.elapsed_ms / 1000:.1f} ثانية\n\n"
        + ("✅ النموذج اجتاز اختباراً مصغّراً، وليس ضماناً لدقته مع جميع ملفات الطلاب.\n"
           "⚠️ Google لا تحدد لنا إذا كانت الحصة مجانية. التفعيل قد يُسبب تكاليف."
           if result.ready else
           "⚠️ انتهاء المهلة لا يعني أن النموذج غير متوافق؛ "
           "يمكن إعادة الاختبار عند انخفاض الضغط.\n"
           "لن يتم تفعيل النموذج تلقائياً."
           if timed_out else
           "لن يُضاف النموذج إلى سلسلة الفحص قبل نجاح الاختبارين.")
    )
    await safe_edit_text(call.message, text, reply_markup=_kb(rows))


@router.callback_query(F.data.startswith("gmc:enable:"))
async def enable_catalog_model(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    current = await _get_current_catalog(state)
    if not current:
        await _render_page(call.message, state)
        return
    data, provider, _ = current
    verified = data.get("gmc_verified") or {}
    try:
        index = int(call.data.rsplit(":", 1)[-1])
        model = data["gmc_models"][index] if 0 <= index < len(data["gmc_models"]) else None
    except (TypeError, ValueError):
        model = None
    if (not model or verified.get("provider") != provider["id"] or
            verified.get("name") != model or
            time.time() - verified.get("timestamp", 0) > PROBE_TTL_SECONDS):
        await safe_edit_text(call.message, "لا يوجد اختبار صالح لهذا النموذج. أعد اختباره.",
                            reply_markup=_kb([[("🔄 عودة للقائمة", "gmc:page:0")]]))
        return
    await safe_edit_text(call.message, "⏳ أحفظ النموذج الذي اجتاز الاختبار في سلسلة الفحص...")
    async with _save_lock:
        # No duplicate creation if the callback is pressed twice concurrently.
        old = next((item for item in await get_all_models(SLOT_DETECTION, force_refresh=True)
                    if item["provider"] == provider["id"] and item["model_name"] == model), None)
        created = False
        if old is None:
            old = await add_model(SLOT_DETECTION, provider["id"], model)
            created = bool(old)
            if not old:
                await safe_edit_text(call.message, "تعذّرت إضافة النموذج. قد يكون موجوداً بالفعل.",
                                    reply_markup=_kb([[("عرض السلسلة", "admin_ai_slot_detection")]]))
                return
        try:
            await update_model(old["id"], SLOT_DETECTION, model, ["text", "image"])
            if not old.get("is_enabled"):
                toggled = await toggle_model(old["id"], SLOT_DETECTION)
                if toggled is None:
                    raise RuntimeError("Could not enable model")
        except Exception:
            if created:
                await remove_model(old["id"], SLOT_DETECTION)
            await safe_edit_text(
                call.message, "تعذّر ضبط النموذج؛ حاول مجدداً من لوحة السلسلة.",
                reply_markup=_kb([[("عرض السلسلة", "admin_ai_slot_detection")]]))
            return
        await state.update_data(gmc_verified=None)
    await safe_edit_text(
        call.message,
        f"✅ تم تفعيل <code>{escape(model)}</code> في <b>فحص المحتوى</b> بنجاح.\n"
        "أُضيف آخر السلسلة دون تغيير ترتيب النماذج الأخرى.\n"
        "القدرات المفعّلة: text,image فقط (PDF/audio غير مختبرين).\n"
        "⚠️ راقب الحدود المجانية والأسعار في Google AI Studio.",
        reply_markup=_kb([[("🔍 عرض سلسلة الفحص", "admin_ai_slot_detection")],
                          [("🔎 نماذج Google", "gmc:start")]]))
