# handlers/admin/ai_control.py
"""🆕 لوحة تحكم الأدمن بالذكاء الاصطناعي: إدارة سلسلة موديلات توليد الأسئلة (cascade)،
موديل الفحص السريع (detection)، وموديل Groq السريع (groq_fast) - كلها مباشرة من تيليجرام
دون أي تعديل كود أو إعادة نشر (تُخزَّن بجدول ai_model_slots بسوبا بيس). لوحة إعدادات
النقاط القديمة (settings.py) أصبحت قسماً فرعياً هنا بدل زر مستقل بالقائمة الرئيسية."""

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
from supabase_helper import admin_get_quiz_generation_log
from logger import get_logger
from .dashboard import AdminState, IsAdminFilter, safe_edit_text

logger = get_logger(__name__)
router = Router()

router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

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
        "📊 <b>سجل توليد الكويزات:</b> وقت كل عملية توليد + الموديل المستخدم فيها\n"
        "💰 <b>إعدادات النقاط:</b> نقاط الترحيب/التجديد/الإحالة (كانت لوحة منفصلة سابقاً)"
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
    if provider != "gemini" and slot == "cascade":
        warning = (
            "\n\n⚠️ ملاحظة: التنفيذ الفعلي لتوليد الأسئلة من الملفات (PDF/صور) عبر "
            f"{PROVIDER_LABELS.get(provider, provider)} غير مفعّل بعد بالكود (يحتاج دمج SDK "
            "مخصص لهذه الشركة). سيُضاف الموديل للسلسلة كبيانات، لكن سيُتخطى تلقائياً وقت "
            "التوليد الفعلي حتى يُضاف هذا التكامل - Gemini يبقى يغطي كل الطلبات بهذه الأثناء."
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

            report_lines.append(
                f"<b>{idx}. {name}</b> ({username_str}) — 🆔 <code>{row.get('user_id')}</code>\n"
                f" ┣ 🤖 <code>[{provider}] {html_escape(str(model))}</code>\n"
                f" ┣ ⏱ {duration_str} — 🧮 {q_count} سؤال\n"
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
            f"───────────────────\n\n" +
            "\n".join(report_lines)
        )
        await safe_edit_text(call.message, text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb_rows))
        await call.answer()
    except Exception as e:
        logger.error(f"Error rendering quiz generation log: {e}")
        await call.answer("❌ حدث خطأ أثناء جلب السجل.", show_alert=True)
