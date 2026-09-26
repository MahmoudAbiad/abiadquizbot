# handlers/admin/ai_control.py
"""🆕 لوحة تحكم الأدمن بالذكاء الاصطناعي: إدارة سلسلة موديلات توليد الأسئلة (cascade)،
موديل الفحص السريع (detection)، وموديل Groq السريع (groq_fast) - كلها مباشرة من تيليجرام
دون أي تعديل كود أو إعادة نشر (تُخزَّن بجدول ai_model_slots بسوبا بيس). إعدادات النقاط
(settings.py) قسم مستقل تحت "الإعدادات العامة" - مش هون، لأنها ما إلها علاقة بالموديلات."""

from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from html import escape as html_escape

from keyboards import (
    get_ai_control_keyboard,
    get_ai_slot_keyboard,
    get_ai_provider_choice_keyboard,
    get_ai_cancel_keyboard,
)
from ai_models_helper import (
    get_all_models,
    add_model,
    remove_model,
    toggle_model,
    move_model,
    VALID_SLOTS,
    VALID_PROVIDERS,
    SLOT_LABELS,
    PROVIDER_LABELS,
    SLOT_DETECTION,
)
from supabase_helper import admin_get_quiz_generation_log, get_ai_model_pricing
from logger import get_logger
from .dashboard import AdminState, IsAdminFilter, safe_edit_text

logger = get_logger(__name__)
router = Router()

router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())
# 🩹 FIX (تسريب خصوصية): محمي أصلاً بهوية الأدمن (IsAdminFilter) بس بلا فلتر مكان -
# لو الأدمن كتب الأمر بالغلط جوا أي غروب هو عضو فيه، اللوحة/العملية كانت رح تنفّذ
# وتظهر علناً هناك. راجع نفس الملاحظة بـ handlers/start.py وhandlers/favorites.py.
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

# 🆕 لوحة "سجل توليد الكويزات" (وقت التوليد + الموديل المستخدم لكل كويز)
QUIZ_GEN_LOG_PAGE_SIZE = 6       # عدد الكويزات المعروضة بالصفحة الواحدة
QUIZ_GEN_LOG_FETCH_LIMIT = 200   # السقف الأقصى المجلوب من قاعدة البيانات دفعة واحدة قبل التصفح المحلي
QUIZ_GEN_LOG_DAYS = 7            # نطاق الأيام المشمولة بالجلب


# ==================== اللوحة الرئيسية ====================

async def _render_ai_menu(event, state: FSMContext = None):
    if state:
        await state.clear()

    text = (
        "🤖 <b>التحكم بالذكاء الاصطناعي</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "من هون بتقدر تتحكم بكل الموديلات المستخدَمة بالبوت مباشرة، بدون أي تعديل كود:\n\n"
        "🧠 <b>سلسلة توليد الأسئلة:</b> ترتيب الموديلات، تفعيل/تعطيل، إضافة أو حذف موديل\n"
        "🔍 <b>فحص المحتوى السريع:</b> الموديل المستخدم لفحص الرياضيات وتصنيف المادة\n"
        "⚡ <b>موديل Groq السريع:</b> المسار السريع لتوليد الأسئلة من نص صريح\n"
        "📊 <b>سجل توليد الكويزات:</b> وقت كل عملية توليد + الموديل المستخدم فيها"
    )
    reply_markup = get_ai_control_keyboard()

    if isinstance(event, types.Message):
        await event.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    elif isinstance(event, types.CallbackQuery):
        await safe_edit_text(event.message, text, reply_markup=reply_markup)
        await event.answer()


@router.callback_query(F.data == "admin_ai_menu")
async def open_ai_menu(call: types.CallbackQuery, state: FSMContext):
    await _render_ai_menu(call, state)


# ==================== عرض سلسلة موديلات slot معيّن ====================

async def _render_slot_screen(event, slot: str, state: FSMContext = None):
    if state:
        await state.clear()

    models = await get_all_models(slot, force_refresh=True)
    slot_label = SLOT_LABELS.get(slot, slot)

    if models:
        body = "اضغط ⬆️/⬇️ لإعادة الترتيب، ✅/🚫 للتفعيل والتعطيل، أو 🗑 للحذف."
    else:
        body = "ما في أي موديل مضاف حالياً بهذه السلسلة. أضف واحد بالزر تحت."

    extra_note = ""
    if slot == SLOT_DETECTION:
        extra_note = (
            "\n\n⚠️ ملاحظة: هذا الفحص يحتاج قراءة صور/ملفات مباشرة، لذلك مقيّد حالياً "
            "بموديلات <b>Gemini</b> فقط."
        )

    text = f"{slot_label}\n━━━━━━━━━━━━━━━━━━\n\n{body}{extra_note}"
    reply_markup = get_ai_slot_keyboard(slot, models)

    if isinstance(event, types.Message):
        await event.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    elif isinstance(event, types.CallbackQuery):
        await safe_edit_text(event.message, text, reply_markup=reply_markup)
        await event.answer()


@router.callback_query(F.data.startswith("admin_ai_slot_"))
async def open_slot_screen(call: types.CallbackQuery, state: FSMContext):
    slot = call.data.removeprefix("admin_ai_slot_")
    if slot not in VALID_SLOTS:
        await call.answer("❌ سلسلة غير معروفة.", show_alert=True)
        return
    await _render_slot_screen(call, slot, state)


@router.callback_query(F.data.startswith("admin_ai_model_"))
async def noop_model_row(call: types.CallbackQuery):
    """الضغط على سطر الموديل نفسه (بدل الأزرار تحته) - مجرد توضيح، بدون إجراء."""
    await call.answer("استخدم الأزرار تحت الموديل للتعديل ⬆️⬇️✅🚫🗑", show_alert=False)


# ==================== إعادة الترتيب / التفعيل / الحذف ====================

@router.callback_query(F.data.startswith("admin_ai_move_up_"))
async def move_up(call: types.CallbackQuery):
    remainder = call.data.removeprefix("admin_ai_move_up_")
    model_id_str, slot = remainder.split("_", 1)
    await move_model(int(model_id_str), slot, "up")
    await _render_slot_screen(call, slot)


@router.callback_query(F.data.startswith("admin_ai_move_down_"))
async def move_down(call: types.CallbackQuery):
    remainder = call.data.removeprefix("admin_ai_move_down_")
    model_id_str, slot = remainder.split("_", 1)
    await move_model(int(model_id_str), slot, "down")
    await _render_slot_screen(call, slot)


@router.callback_query(F.data.startswith("admin_ai_toggle_"))
async def toggle(call: types.CallbackQuery):
    remainder = call.data.removeprefix("admin_ai_toggle_")
    model_id_str, slot = remainder.split("_", 1)

    # 🔒 نفس حماية الحذف: لا تسمح بتعطيل آخر موديل مفعّل الوحيد بالسلسلة (يمنع تعطّل
    # التوليد بالكامل بضغطة خاطئة - يبقى دائماً موديل واحد فعّال على الأقل).
    models = await get_all_models(slot, force_refresh=True)
    enabled_models = [m for m in models if m.get("is_enabled")]
    target = next((m for m in models if str(m["id"]) == model_id_str), None)
    if target and target.get("is_enabled") and len(enabled_models) <= 1:
        await call.answer(
            "❌ ما فيك تعطّل آخر موديل مفعّل بهذه السلسلة - فعّل بديل أولاً.",
            show_alert=True,
        )
        return

    result = await toggle_model(int(model_id_str), slot)
    if result is None:
        await call.answer("❌ تعذّر تعديل حالة الموديل.", show_alert=True)
        return
    await _render_slot_screen(call, slot)


@router.callback_query(F.data.startswith("admin_ai_delete_"))
async def delete(call: types.CallbackQuery):
    remainder = call.data.removeprefix("admin_ai_delete_")
    model_id_str, slot = remainder.split("_", 1)

    # حماية بسيطة: لا تسمح بحذف آخر موديل مفعّل الوحيد بالسلسلة (يمنع تعطّل التوليد بالكامل بالخطأ).
    models = await get_all_models(slot, force_refresh=True)
    enabled_models = [m for m in models if m.get("is_enabled")]
    target = next((m for m in models if str(m["id"]) == model_id_str), None)
    if target and target.get("is_enabled") and len(enabled_models) <= 1:
        await call.answer(
            "❌ ما فيك تحذف آخر موديل مفعّل بهذه السلسلة - عطّله أو أضف بديل أولاً.",
            show_alert=True,
        )
        return

    await remove_model(int(model_id_str), slot)
    await call.answer("🗑 تم الحذف.")
    await _render_slot_screen(call, slot)


# ==================== إضافة موديل جديد ====================

@router.callback_query(F.data.startswith("admin_ai_add_"))
async def prompt_add_model(call: types.CallbackQuery, state: FSMContext):
    slot = call.data.removeprefix("admin_ai_add_")
    if slot not in VALID_SLOTS:
        await call.answer("❌ سلسلة غير معروفة.", show_alert=True)
        return

    await state.clear()
    text = f"➕ <b>إضافة موديل جديد لـ {SLOT_LABELS.get(slot, slot)}</b>\n\nاختر الشركة المزوّدة:"
    if slot == SLOT_DETECTION:
        text += "\n\n⚠️ هذا الفحص يقرأ صور/ملفات مباشرة، لذا يدعم حالياً Gemini فقط."
    await safe_edit_text(call.message, text, reply_markup=get_ai_provider_choice_keyboard(slot))
    try:
        await call.answer()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("admin_ai_provider_"))
async def choose_provider(call: types.CallbackQuery, state: FSMContext):
    remainder = call.data.removeprefix("admin_ai_provider_")
    provider, slot = remainder.split("_", 1)

    if provider not in VALID_PROVIDERS or slot not in VALID_SLOTS:
        await call.answer("❌ قيمة غير معروفة.", show_alert=True)
        return

    # قيد معماري حالي: فحص المحتوى (detection) يحتاج قراءة صور/ملفات مباشرة - مدعوم
    # فقط عبر Gemini اليوم (راجع ملاحظة helpers/ai_models_helper.py).
    if slot == SLOT_DETECTION and provider != "gemini":
        await call.answer(
            "❌ فحص المحتوى السريع يدعم حالياً موديلات Gemini فقط (يحتاج قراءة صور/ملفات مباشرة).",
            show_alert=True,
        )
        return

    await state.update_data(new_model_slot=slot, new_model_provider=provider)
    await state.set_state(AdminState.waiting_for_new_model_name)

    warning = ""
    if provider == "vertex" and slot == "cascade":
        warning = (
            "\n\n💡 ملاحظة: تأكد من ضبط متغيرات بيئة Vertex الثلاثة على السيرفر "
            "(GOOGLE_VERTEX_PROJECT / GOOGLE_VERTEX_LOCATION / GOOGLE_VERTEX_SA_KEY_JSON)، "
            "وإلا سيُتخطى هذا الصف بصمت وقت التوليد (باقي السلسلة تستمر بالعمل طبيعياً). "
            "اكتب اسم الموديل بالضبط كما يظهر في Vertex AI Model Garden - قد يختلف عن اسمه "
            "في AI Studio (مثال: قد يحتاج لاحقة -001)."
        )
    elif provider not in ("gemini", "vertex") and slot == "cascade":
        warning = (
            "\n\n⚠️ ملاحظة: التنفيذ الفعلي لتوليد الأسئلة من الملفات (PDF/صور) عبر "
            f"{PROVIDER_LABELS.get(provider, provider)} غير مفعّل بعد بالكود (يحتاج دمج SDK "
            "مخصص لهذه الشركة). سيُضاف الموديل للسلسلة كبيانات، لكن سيُتخطى تلقائياً وقت "
            "التوليد الفعلي حتى يُضاف هذا التكامل - Gemini/Vertex يبقيان يغطيان كل الطلبات "
            "بهذه الأثناء."
        )

    await safe_edit_text(
        call.message,
        f"✍️ <b>اسم الموديل ({PROVIDER_LABELS.get(provider, provider)})</b>\n\n"
        "أرسل الاسم بالضبط كما يظهر بتوثيق الشركة (مثال: gemini-3.6-flash):"
        f"{warning}",
        reply_markup=get_ai_cancel_keyboard(slot),
    )
    try:
        await call.answer()
    except TelegramBadRequest:
        pass


@router.message(AdminState.waiting_for_new_model_name)
async def process_new_model_name(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    slot = data.get("new_model_slot")
    provider = data.get("new_model_provider")

    if not slot or not provider:
        await msg.answer("❌ انتهت جلسة الإضافة، افتح لوحة الذكاء الاصطناعي من جديد.")
        await state.clear()
        await _render_ai_menu(msg)
        return

    model_name = (msg.text or "").strip()
    if not model_name or len(model_name) > 100:
        await msg.answer(
            "❌ اسم غير صالح. أرسل اسم الموديل مباشرة (بلا مسافات زائدة، أقل من 100 حرف):",
            reply_markup=get_ai_cancel_keyboard(slot),
        )
        return

    result = await add_model(slot, provider, model_name)
    await state.clear()

    if result is None:
        await msg.answer(
            "❌ تعذّرت الإضافة - إما الاسم يحتوي رموز غير مسموحة (يُسمح فقط بحروف/أرقام/نقطة/"
            "شرطة/سلاش)، أو مضاف مسبقاً بنفس الاسم والشركة. حاول مجدداً:",
            reply_markup=get_ai_cancel_keyboard(slot),
        )
        return

    await msg.answer(f"✅ تمت إضافة <code>{html_escape(model_name)}</code> بنجاح.", parse_mode="HTML")
    await _render_slot_screen(msg, slot)


def _estimate_quiz_cost(meta: dict, pricing: dict) -> tuple:
    """🆕 يحسب التكلفة التقديرية بالدولار لكويز واحد من metadata حدث quiz_generated
    (input_tokens/output_tokens/thoughts_tokens) وجدول ai_model_pricing (get_ai_model_pricing).
    توكنز التفكير (thoughts_tokens) تُحاسَب بسعر الإخراج (نفس منطق Google الفعلي: output
    pricing يشمل thinking tokens ضمنياً - راجع migration_ai_model_pricing.sql).

    يرجّع (cost_or_none, verified) - cost_or_none=None لو الموديل غير موجود إطلاقاً بجدول
    الأسعار (لا نعرض 0$ مضلّل بهالحالة) - verified=False لو السعر المستخدم لسا غير مؤكد
    من فاتورة حقيقية (راجع عمود verified بالجدول) حتى لو محسوب، لعرض تنبيه ⚠️ بجانبه."""
    provider = meta.get("ai_provider")
    model = meta.get("ai_model")
    price = pricing.get((provider, model))
    if not price:
        return None, False
    input_tokens = meta.get("input_tokens", 0) or 0
    output_tokens = meta.get("output_tokens", 0) or 0
    thoughts_tokens = meta.get("thoughts_tokens", 0) or 0
    cost = (input_tokens / 1_000_000 * price["input"]) + \
           ((output_tokens + thoughts_tokens) / 1_000_000 * price["output"])
    return cost, price["verified"]


# ==================== 🆕 سجل توليد الكويزات (وقت التوليد + الموديل المستخدم) ====================
# يقبل كلا الشكلين: "admin_quiz_gen_log" (الدخول أول مرة = صفحة 1) و
# "admin_quiz_gen_log_p_<page>" (التنقل بين الصفحات) - نفس نمط "🐞 آخر الأخطاء" بـ analytics.py.
@router.callback_query(F.data == "admin_quiz_gen_log")
@router.callback_query(F.data.startswith("admin_quiz_gen_log_p_"))
async def show_quiz_generation_log(call: types.CallbackQuery):
    try:
        page = 1
        if call.data.startswith("admin_quiz_gen_log_p_"):
            try:
                page = int(call.data.replace("admin_quiz_gen_log_p_", "", 1))
            except ValueError:
                page = 1

        rows = await admin_get_quiz_generation_log(limit=QUIZ_GEN_LOG_FETCH_LIMIT, days=QUIZ_GEN_LOG_DAYS)
        if not rows:
            await call.answer(f"✅ لا توجد أي كويزات مولَّدة خلال آخر {QUIZ_GEN_LOG_DAYS} أيام.", show_alert=True)
            return

        total = len(rows)
        total_pages = max(1, -(-total // QUIZ_GEN_LOG_PAGE_SIZE))
        page = max(1, min(page, total_pages))
        start = (page - 1) * QUIZ_GEN_LOG_PAGE_SIZE
        page_items = rows[start:start + QUIZ_GEN_LOG_PAGE_SIZE]

        # 🆕 متوسط زمن التوليد لكل الكويزات المجلوبة (لا فقط الصفحة الحالية) - مؤشر
        # سريع لصحة أداء الـ cascade عموماً (لو ارتفع فجأة يشير غالباً لازدحام/حظر متكرر).
        durations = [
            r.get("metadata", {}).get("generation_seconds")
            for r in rows if isinstance(r.get("metadata", {}).get("generation_seconds"), (int, float))
        ]
        avg_duration = f"{(sum(durations) / len(durations)):.1f}s" if durations else "—"

        # 🆕 التكلفة التقديرية الإجمالية لكل الكويزات المجلوبة (كل الأيام السبعة، لا فقط
        # الصفحة الحالية) - نفس منطق avg_duration فوق. كويزات بلا سعر معروف (موديل غير
        # موجود بـ ai_model_pricing) تُستثنى من المجموع صراحة (has_unpriced) بدل تصفيرها.
        pricing = await get_ai_model_pricing()
        total_cost = 0.0
        any_unverified = False
        has_unpriced = False
        for r in rows:
            c, verified = _estimate_quiz_cost(r.get("metadata") or {}, pricing)
            if c is None:
                has_unpriced = True
            else:
                total_cost += c
                if not verified:
                    any_unverified = True
        cost_str = f"~${total_cost:.3f}" if total_cost else "—"
        if any_unverified or has_unpriced:
            cost_str += " ⚠️"

        report_lines = []
        for idx, row in enumerate(page_items, start=start + 1):
            meta = row.get("metadata") or {}
            user = row.get("user") or {}
            username_str = f"@{user['username']}" if user.get("username") and user['username'] != "Unknown" else "بدون يوزر"
            name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or "بدون اسم"
            provider = meta.get("ai_provider") or "؟"
            model = meta.get("ai_model") or "غير مسجَّل"
            duration = meta.get("generation_seconds")
            duration_str = f"{duration:.1f}ث" if isinstance(duration, (int, float)) else "—"
            q_count = meta.get("questions_generated", "؟")

            # 🆕 تفصيل التوكنز (input/output/thoughts) بمسميات واضحة + التكلفة التقديرية -
            # راجع _estimate_quiz_cost فوق. كويزات قديمة (قبل هالتحديث) ما فيها هالحقول
            # إطلاقاً بالـ metadata، فبتظهر "غير مسجَّل" بدل أصفار مضلّلة.
            has_token_data = any(k in meta for k in ("input_tokens", "output_tokens", "total_tokens"))
            if has_token_data:
                input_tok = meta.get("input_tokens", 0) or 0
                output_tok = meta.get("output_tokens", 0) or 0
                thoughts_tok = meta.get("thoughts_tokens", 0) or 0
                total_tok = meta.get("total_tokens", 0) or 0
                tokens_line = f"📥إدخال {input_tok} + 📤إخراج {output_tok}"
                if thoughts_tok:
                    tokens_line += f" + 🧠تفكير {thoughts_tok}"
                tokens_line += f" = <b>{total_tok}</b> توكن"

                cost, verified = _estimate_quiz_cost(meta, pricing)
                if cost is None:
                    cost_line = "غير معروف السعر"
                else:
                    cost_line = f"${cost:.4f}" + ("" if verified else " ⚠️")
                token_cost_row = f" ┣ {tokens_line}\n ┣ 💰 {cost_line}\n"
            else:
                token_cost_row = ""

            # 🆕 نوع العملية المتبعة (عادي/Super PDF/Super Images/Groq السريع) + ترتيب
            # الموديل(ات) ضمن الكاسكيد *لحظة* نجاح هذا التوليد بالذات (cascade_rank/
            # cascade_total - راجع gemini_helper._execute_cascade) - ثابت بالسجل حتى لو
            # تغيّر ترتيب الكاسكيد لاحقاً من لوحة الأدمن.
            cascade_rank = meta.get("cascade_rank")
            cascade_total = meta.get("cascade_total")
            gen_mode = meta.get("generation_mode")
            chunk_details = meta.get("chunk_details")  # 🆕 تفصيل كل جزء بمسار Super (راجع gemini_helper.py)

            mode_labels = {
                "super_pdf": "🔀 Super PDF",
                "super_images": "🔀 Super Images",
                "regular": "➡️ عادي",
            }
            mode_line = f" ┣ 🧩 نوع العملية: {mode_labels.get(gen_mode, '➡️ عادي' if provider != 'groq' else '⚡ Groq السريع')}\n"

            if chunk_details:
                # 🆕 كل جزء (Task) استقل بكاسكيد خاص فيه (نماذج × مفاتيح) - فممكن كل جزء
                # يفوز بموديل/رتبة مختلفة عن التاني، هيك بنعرضهن مفصّلين بدل وصف عام واحد.
                chunk_lines = []
                for chunk_index, chunk in enumerate(chunk_details, start=1):
                    c_model = html_escape(str(chunk.get("model") or "؟"))
                    c_rank, c_total = chunk.get("cascade_rank"), chunk.get("cascade_total")
                    rank_part = f" (#{c_rank}/{c_total})" if c_rank and c_total else ""
                    chunk_lines.append(f"     {chunk_index}. <code>{c_model}</code>{rank_part}")
                rank_line = mode_line + " ┣ 📶 تفصيل الأجزاء:\n" + "\n".join(chunk_lines) + "\n"
            elif cascade_rank and cascade_total:
                rank_line = mode_line + f" ┣ 📶 الترتيب بالكاسكيد: #{cascade_rank} من {cascade_total}\n"
            elif provider == "groq":
                rank_line = mode_line
            else:
                rank_line = mode_line

            report_lines.append(
                f"<b>{idx}. {name}</b> ({username_str}) — 🆔 <code>{row.get('user_id')}</code>\n"
                f" ┣ 🤖 <code>[{provider}] {html_escape(str(model))}</code>\n"
                f"{rank_line}"
                f" ┣ ⏱ {duration_str} — 🧮 {q_count} سؤال\n"
                f"{token_cost_row}"
                f" ┗ 🕒 <code>{row.get('time_str')}</code>\n"
            )

        nav_row = []
        if page < total_pages:
            nav_row.append(types.InlineKeyboardButton(text="◀️ أقدم", callback_data=f"admin_quiz_gen_log_p_{page + 1}"))
        if page > 1:
            nav_row.append(types.InlineKeyboardButton(text="أحدث ▶️", callback_data=f"admin_quiz_gen_log_p_{page - 1}"))

        kb_rows = []
        if nav_row:
            kb_rows.append(nav_row)
        kb_rows.append([types.InlineKeyboardButton(text="🤖 رجوع للتحكم بالذكاء الاصطناعي", callback_data="admin_ai_menu")])
        kb_rows.append([types.InlineKeyboardButton(text="⚙️ لوحة التحكم الرئيسية", callback_data="admin_main_menu")])

        text = (
            f"📊 <b>سجل توليد الكويزات (آخر {QUIZ_GEN_LOG_DAYS} أيام)</b>\n"
            f"إجمالي: <code>{total}</code> | متوسط زمن التوليد: <code>{avg_duration}</code> | صفحة {page}/{total_pages}\n"
            f"💰 تكلفة تقديرية إجمالية: <code>{cost_str}</code>"
            + (" (⚠️ يشمل أسعار غير مؤكدة أو كويزات بلا سعر معروف)" if (any_unverified or has_unpriced) else "") + "\n"
            f"───────────────────\n\n" +
            "\n".join(report_lines)
        )
        await safe_edit_text(call.message, text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb_rows))
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering quiz generation log: {e}")
        await call.answer("❌ حدث خطأ أثناء جلب السجل.", show_alert=True)
