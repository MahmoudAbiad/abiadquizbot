"""Private admin UI for provider CRUD; never accepts actual API secrets."""
from html import escape
import json

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from helpers.ai_providers_helper import get_providers, save_provider, delete_provider
from helpers.ai_models_helper import get_all_models, update_model, VALID_SLOTS
from services.ai.runtime import credentials_for
from services.ai.provider_presets import provider_preset
from .dashboard import IsAdminFilter, safe_edit_text

router = Router()
router.message.filter(IsAdminFilter(), F.chat.type == "private")
router.callback_query.filter(IsAdminFilter(), F.message.chat.type == "private")


class ProviderState(StatesGroup):
    account_id = State()
    identifier = State()
    label = State()
    transport = State()
    base_url = State()
    environment = State()
    confirm = State()
    model = State()


def keyboard(rows):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows])


def cancel_keyboard():
    return keyboard([[("إلغاء والعودة للمزوّدين", "aip:list")]])


@router.callback_query(F.data == "aip:list")
async def list_providers(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        providers = await get_providers(force_refresh=True)
    except RuntimeError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    rows = []
    for provider in providers.values():
        status = "✅" if provider["is_enabled"] else "⏸"
        ready = "" if credentials_for(provider) else " — متغيرات البيئة ناقصة"
        rows.append([(f"{status} {provider['label']}{ready}", "aip:show:" + provider["id"])])
    rows.extend([[("➕ إضافة مزوّد", "aip:new")], [("رجوع", "admin_ai_menu")]])
    await safe_edit_text(call.message,
        "🔌 <b>إدارة المزوّدين</b>\nاختر مزوّدًا لتعديله أو تعطيله أو حذفه.\n"
        "بعد إضافته أضف نماذجه من سلسلة التوليد أو الفحص أو المسار النصي السريع.\n"
        "تُحفظ أسماء متغيرات البيئة هنا؛ ضع مفاتيح API على السيرفر فقط.", reply_markup=keyboard(rows))
    await call.answer()


@router.callback_query(F.data.startswith("aip:show:"))
async def show_provider(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    identifier = call.data.split(":")[2]
    provider = (await get_providers()).get(identifier)
    if not provider:
        await call.answer("المزوّد غير موجود", show_alert=True)
        return
    summary = escape(json.dumps(provider, ensure_ascii=False, indent=2))
    from services.ai.provider_presets import chat_service
    hint = ("\nلفحص الصور وPDF أضف النموذج <code>@cf/meta/llama-3.2-11b-vision-instruct</code> "
            "إلى سلسلة الفحص، وعدّل قدراته إلى <code>text,image</code>. "
            "يفحص أول صورة أو أول صفحة PDF فقط. يجب قبول ترخيص Meta في حساب Cloudflare قبل أول استخدام."
            if provider["base_url"] and chat_service(provider["base_url"]) == "cloudflare" else "")
    await safe_edit_text(call.message, f"<b>{escape(provider['label'])}</b>\n<pre>{summary}</pre>" + hint,
        reply_markup=keyboard([
            [("✏️ تعديل الإعدادات", "aip:edit:" + identifier)],
            [("⏸ تعطيل" if provider["is_enabled"] else "✅ تفعيل", "aip:toggle:" + identifier)],
            [("🗑 حذف المزوّد ونماذجه", "aip:delete:" + identifier)],
            [("رجوع", "aip:list")]]))
    await call.answer()


@router.callback_query(F.data == "aip:new")
async def choose_provider_setup(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit_text(call.message, "اختر مزوّدًا بإعداد جاهز، أو أضف مزوّدًا آخر:",
        reply_markup=keyboard([
            [("OpenRouter", "aip:preset:openrouter")],
            [("Cloudflare Workers AI", "aip:preset:cloudflare")],
            [("مزوّد آخر / إعداد مخصص", "aip:custom")],
            [("إلغاء", "aip:list")]]))
    await call.answer()


@router.callback_query(F.data.startswith("aip:preset:"))
async def choose_provider_preset(call: types.CallbackQuery, state: FSMContext):
    service = call.data.split(":")[2]
    if service not in {"openrouter", "cloudflare"}:
        return
    if service in await get_providers(force_refresh=True):
        await show_provider(call, state)
        return
    await state.clear()
    await state.update_data(create=True)
    if service == "cloudflare":
        await state.set_state(ProviderState.account_id)
        await call.message.answer("أرسل Cloudflare Account ID من لوحة Cloudflare.\n"
            "هذا معرّف الحساب، لا ترسل API Token. سنستخدم متغير البيئة CLOUDFLARE_API_TOKEN.",
            reply_markup=cancel_keyboard())
    else:
        await state.update_data(draft=provider_preset(service))
        await preview_provider(call.message, state)
    await call.answer()


@router.message(ProviderState.account_id)
async def provider_cloudflare_account(message: types.Message, state: FSMContext):
    try:
        draft = provider_preset("cloudflare", account_id=(message.text or "").strip())
    except ValueError as exc:
        await message.answer(escape(str(exc)), reply_markup=cancel_keyboard())
        return
    await state.update_data(draft=draft)
    await preview_provider(message, state)


@router.callback_query(F.data == "aip:custom")
async def new_provider(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(create=True, draft={"is_enabled": True})
    await state.set_state(ProviderState.identifier)
    await safe_edit_text(call.message, "أرسل معرّف المزوّد: حروف إنجليزية صغيرة وأرقام وشرطة، حتى 24 حرفًا. مثال: my-provider",
                         reply_markup=cancel_keyboard())
    await call.answer()


@router.message(ProviderState.identifier)
async def provider_identifier(message: types.Message, state: FSMContext):
    import re
    identifier = (message.text or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", identifier):
        await message.answer("معرّف غير صالح. مثال: my-provider", reply_markup=cancel_keyboard())
        return
    if identifier in await get_providers(force_refresh=True):
        await message.answer("هذا المعرّف موجود، اختر معرّفًا آخر.")
        return
    data = await state.get_data()
    await state.update_data(draft={**data["draft"], "id": identifier})
    await state.set_state(ProviderState.label)
    await message.answer("أرسل اسم العرض للمزوّد:", reply_markup=cancel_keyboard())


@router.callback_query(F.data.startswith("aip:edit:"))
async def edit_provider(call: types.CallbackQuery, state: FSMContext):
    identifier = call.data.split(":")[2]
    provider = (await get_providers(force_refresh=True)).get(identifier)
    if not provider:
        await call.answer("المزوّد غير موجود", show_alert=True)
        return
    await state.clear()
    await state.update_data(create=False, draft=provider)
    await state.set_state(ProviderState.label)
    await safe_edit_text(call.message, "أرسل اسم العرض الجديد (أو الاسم الحالي للإبقاء عليه):\n" + escape(provider["label"]),
                         reply_markup=cancel_keyboard())
    await call.answer()


@router.message(ProviderState.label)
async def provider_label(message: types.Message, state: FSMContext):
    label = (message.text or "").strip()
    if not 1 <= len(label) <= 80:
        await message.answer("أرسل اسمًا بين حرف و80 حرفًا.")
        return
    data = await state.get_data()
    await state.update_data(draft={**data["draft"], "label": label})
    await state.set_state(ProviderState.transport)
    await message.answer("اختر واجهة الاتصال:", reply_markup=keyboard([
        [("Gemini AI Studio", "aip:transport:gemini")],
        [("Vertex AI", "aip:transport:vertex")],
        [("متوافق مع OpenAI (مثل Groq)", "aip:transport:openai_compatible")],
        [("إلغاء", "aip:list")]]))


@router.callback_query(ProviderState.transport, F.data.startswith("aip:transport:"))
async def provider_transport(call: types.CallbackQuery, state: FSMContext):
    transport = call.data.split(":")[2]
    if transport not in {"gemini", "vertex", "openai_compatible"}:
        return
    data = await state.get_data()
    draft = {**data["draft"], "transport": transport, "base_url": None, "api_keys_env": None}
    await state.update_data(draft=draft)
    if transport == "vertex":
        await preview_provider(call.message, state)
    elif transport == "openai_compatible":
        await state.set_state(ProviderState.base_url)
        await call.message.answer("أرسل عنوان API الأساسي HTTPS، مثل https://api.groq.com/openai/v1\n"
                                  "بلا مفاتيح وبلا اللاحقة /chat/completions.", reply_markup=cancel_keyboard())
    else:
        await prompt_environment(call.message, state)
    await call.answer()


@router.message(ProviderState.base_url)
async def provider_base_url(message: types.Message, state: FSMContext):
    data = await state.get_data()
    draft = {**data["draft"], "base_url": (message.text or "").strip()}
    from helpers.ai_providers_helper import validate_provider
    try:
        validate_provider({**draft, "api_keys_env": "EXAMPLE_API_KEYS"})
    except ValueError as exc:
        await message.answer(escape(str(exc)), reply_markup=cancel_keyboard())
        return
    await state.update_data(draft=draft)
    await prompt_environment(message, state)


async def prompt_environment(message, state):
    await state.set_state(ProviderState.environment)
    await message.answer("أرسل اسم متغير البيئة الذي يحمل المفتاح أو قائمة المفاتيح.\n"
                         "مثال: GROQ_API_KEY أو GEMINI_API_KEYS\nلا ترسل المفتاح نفسه.", reply_markup=cancel_keyboard())


@router.message(ProviderState.environment)
async def provider_environment(message: types.Message, state: FSMContext):
    data = await state.get_data()
    from helpers.ai_providers_helper import validate_provider
    try:
        draft = validate_provider({**data["draft"], "api_keys_env": (message.text or "").strip()})
    except ValueError as exc:
        await message.answer(escape(str(exc)), reply_markup=cancel_keyboard())
        return
    await state.update_data(draft=draft)
    await preview_provider(message, state)


async def preview_provider(message, state):
    data = await state.get_data()
    await state.set_state(ProviderState.confirm)
    await message.answer("راجع الإعدادات ثم احفظ. ضع المفتاح في متغير البيئة المذكور على السيرفر، "
                         "ثم أضف نماذج المزوّد من سلسلة التوليد أو الفحص أو المسار النصي السريع:\n<pre>" +
                         escape(json.dumps(data["draft"], ensure_ascii=False, indent=2)) + "</pre>",
                         reply_markup=keyboard([[("💾 حفظ", "aip:save")], [("إلغاء", "aip:list")]]))


@router.callback_query(ProviderState.confirm, F.data == "aip:save")
async def commit_provider(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    try:
        await save_provider(data["draft"], create=data["create"])
    except Exception:
        await call.answer("تعذّر الحفظ؛ تحقق من migration والاتصال وعدم تكرار المعرّف.", show_alert=True)
        return
    await list_providers(call, state)


@router.callback_query(F.data.startswith("aip:toggle:"))
async def toggle_provider(call: types.CallbackQuery, state: FSMContext):
    provider = (await get_providers(force_refresh=True)).get(call.data.split(":")[2])
    if not provider:
        await call.answer("المزوّد غير موجود", show_alert=True)
        return
    try:
        await save_provider({**provider, "is_enabled": not provider["is_enabled"]})
    except Exception:
        await call.answer("تعذّر حفظ الحالة", show_alert=True)
        return
    await list_providers(call, state)


@router.callback_query(F.data.startswith("aip:delete:"))
async def confirm_delete_provider(call: types.CallbackQuery):
    identifier = call.data.split(":")[2]
    await safe_edit_text(call.message,
        f"حذف المزوّد <code>{escape(identifier)}</code> سيحذف نماذجه من جميع السلاسل.\n"
        "لن تُحذف الكويزات السابقة ولا سجلات التكلفة. قد يتوقف التوليد إذا لم يبقَ بديل مناسب.",
        reply_markup=keyboard([[("تأكيد الحذف", "aip:remove:" + identifier)], [("إلغاء", "aip:list")]]))
    await call.answer()


@router.callback_query(F.data.startswith("aip:remove:"))
async def remove_provider(call: types.CallbackQuery, state: FSMContext):
    try:
        removed = await delete_provider(call.data.split(":")[2])
    except Exception:
        removed = False
    if not removed:
        await call.answer("تعذّر الحذف أو المزوّد محذوف مسبقًا", show_alert=True)
        return
    await list_providers(call, state)


@router.callback_query(F.data.startswith("aim:edit:"))
async def edit_model(call: types.CallbackQuery, state: FSMContext):
    _, _, slot, identifier = call.data.split(":")
    if slot not in VALID_SLOTS:
        return
    model = next((row for row in await get_all_models(slot, force_refresh=True)
                  if str(row["id"]) == identifier), None)
    if not model:
        await call.answer("الموديل غير موجود", show_alert=True)
        return
    await state.clear()
    await state.update_data(model_id=model["id"], slot=slot)
    await state.set_state(ProviderState.model)
    await safe_edit_text(call.message,
        "أرسل الاسم والقدرات بهذا الشكل:\n<code>model-name | text,image</code>\n"
        "القدرات: text للنص، image للصور، pdf للملفات، audio للصوت.\n"
        "اختر فقط ما يدعمه النموذج. الواجهة المتوافقة مع OpenAI تدعم هنا text وimage.\n"
        "القيم الحالية:\n<code>" + escape(model["model_name"] + " | " + ",".join(model["capabilities"])) + "</code>",
        reply_markup=keyboard([[("إلغاء", "admin_ai_slot_" + slot)]]))
    await call.answer()


@router.message(ProviderState.model)
async def save_model(message: types.Message, state: FSMContext):
    data = await state.get_data()
    try:
        name, caps = (message.text or "").split("|", 1)
        await update_model(data["model_id"], data["slot"], name.strip(), [cap.strip() for cap in caps.split(",")])
    except Exception:
        await message.answer("تعذّر التعديل. تحقق من الاسم والقدرات وعدم تكرار النموذج.")
        return
    await state.clear()
    from .ai_control import _render_slot_screen
    await _render_slot_screen(message, data["slot"])
