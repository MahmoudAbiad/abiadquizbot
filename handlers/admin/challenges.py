# handlers/admin/challenges.py
"""🏆 إدارة محتوى التحديات (الفرع → المادة → الوحدة → الاختبارات) من لوحة الأدمن.
التصميم: docs/challenges/DESIGN.md. كل callbacks بالبادئة `adm_ch:` (غير مستخدمة بمكان آخر).

الحماية Server-Side: الراوتر كله خلف IsAdminFilter (يقارن from_user.id بـ ADMIN_ID لكل رسالة وكل
ضغطة زر، بما فيها رفع الملفات داخل حالات FSM) - إخفاء الأزرار وحده غير كافٍ ولا يُعتمد عليه.
المنطق بـ services/challenge_content.py (نقي) + helpers/supabase_helper/challenges.py (قاعدة البيانات).
"""

import html
import io
import os
from typing import Any, Dict, List, Optional, Tuple

from aiogram import Router, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from helpers.supabase_helper import challenges as repo, is_feature_enabled
from helpers.supabase_helper.challenges import ChallengeRepoError, CHALLENGE_FLAG
from logger import get_logger, log_error
from services.challenge_content import (
    ChallengeContentError, ParsedTest, parse_challenge_json, from_existing_quiz, paginate,
    MAX_FILE_BYTES, SOURCE_JSON, SOURCE_EXISTING,
)
from .dashboard import IsAdminFilter

logger = get_logger(__name__)
router = Router()
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

PAGE_SIZE = 8
PREVIEW_PAGE_SIZE = 3
P = "adm_ch:"
KIND_LABEL = {"b": ("branch", "الفرع"), "s": ("subject", "المادة"), "u": ("unit", "الوحدة")}
CHILD_KIND = {"b": "s", "s": "u"}
STATUS_ICON = {"active": "✅", "disabled": "⛔", "archived": "🗄", "draft": "📝"}
SOURCE_LABEL = {SOURCE_JSON: "JSON", SOURCE_EXISTING: "كويز أبيض", "manual_future": "يدوي"}


class ChallengeAdminState(StatesGroup):
    waiting_name = State()        # data: mode(add|ren), kind, id (parent للإضافة / العنصر لإعادة التسمية)
    waiting_order = State()       # data: kind, id
    waiting_json = State()        # data: unit_id
    waiting_quiz_ref = State()    # data: unit_id
    waiting_test_title = State()  # data: unit_id, parsed
    confirm_import = State()      # data: unit_id, parsed


# ==================== أدوات العرض ====================

def _e(s: Any) -> str:
    return html.escape(str(s or ""), quote=False)


def _btn(text: str, data: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text=text, callback_data=data)


async def _render(event, text: str, rows: List[List[types.InlineKeyboardButton]]) -> None:
    """يعدّل الرسالة الحالية؛ وإن تعذّر التعديل (رسالة قديمة/محذوفة) يرسل رسالة جديدة بدلها."""
    markup = types.InlineKeyboardMarkup(inline_keyboard=rows)
    if isinstance(event, types.CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "not modified" not in str(e).lower():
                await event.message.answer(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup, parse_mode="HTML")


def _nav_row(items: List[Any], page: int, pages: int, cb: str) -> List[types.InlineKeyboardButton]:
    row = []
    if page > 0:
        row.append(_btn("◀️", f"{P}{cb}:{page - 1}"))
    if page < pages - 1:
        row.append(_btn("▶️", f"{P}{cb}:{page + 1}"))
    return row


async def _guard_enabled_notice() -> str:
    on = await is_feature_enabled(CHALLENGE_FLAG, default=False)
    return "🟢 النظام مفعّل للطلاب" if on else "⚪ النظام غير مفعّل للطلاب (يمكنك تجهيز المحتوى بأمان؛ التفعيل من مفاتيح التحكم)"


# ==================== الشاشات ====================

async def show_home(event, page: int = 0) -> None:
    branches = await repo.list_nodes("branch")
    chunk, page, pages = paginate(branches, page, PAGE_SIZE)
    text = ("🏆 <b>إدارة التحديات</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"{await _guard_enabled_notice()}\n\nاختر فرعاً أو أضف جديداً:" +
            ("" if branches else "\n\n<i>لا توجد فروع بعد.</i>"))
    rows = [[_btn(f"📂 {b['name']}", f"{P}b:{b['id']}:0")] for b in chunk]
    nav = _nav_row(branches, page, pages, "home")
    if nav:
        rows.append(nav)
    rows.append([_btn("➕ فرع جديد", f"{P}add:b:none")])
    rows.append([_btn("🏠 الرئيسية", "admin_main_menu")])
    await _render(event, text, rows)


async def show_node(event, kind: str, node_id: str, page: int = 0) -> None:
    """kind: b (فرع→مواد) | s (مادة→وحدات)."""
    node_kind = KIND_LABEL[kind][0]
    node = await repo.get_node(node_kind, node_id)
    if not node or node["status"] != "active":
        await _render(event, "⚠️ العنصر غير موجود أو مؤرشف.", [[_btn("🔙 رجوع", f"{P}home:0")]])
        return
    child = CHILD_KIND[kind]
    children = await repo.list_nodes(KIND_LABEL[child][0], node_id)
    chunk, page, pages = paginate(children, page, PAGE_SIZE)
    text = (f"📂 <b>{_e(node['name'])}</b> — {KIND_LABEL[kind][1]}\n"
            f"الترتيب: {node['sort_order']}\n\n"
            f"{'المواد' if kind == 'b' else 'الوحدات'} ({len(children)}):" +
            ("" if children else "\n<i>لا يوجد شيء بعد.</i>"))
    rows = [[_btn(f"{'📘' if child == 's' else '📑'} {c['name']}", f"{P}{child}:{c['id']}:0")] for c in chunk]
    nav = _nav_row(children, page, pages, f"{kind}:{node_id}")
    if nav:
        rows.append(nav)
    rows.append([_btn(f"➕ {'مادة' if kind == 'b' else 'وحدة'} جديدة", f"{P}add:{child}:{node_id}")])
    rows.append([_btn("✏️ اسم", f"{P}ren:{kind}:{node_id}"), _btn("🔢 ترتيب", f"{P}ord:{kind}:{node_id}"),
                 _btn("🗄 أرشفة", f"{P}x:{kind}:{node_id}")])
    back = f"{P}home:0" if kind == "b" else f"{P}b:{node['branch_id']}:0"
    rows.append([_btn("🔙 رجوع", back)])
    await _render(event, text, rows)


async def show_unit(event, unit_id: str, page: int = 0) -> None:
    unit = await repo.get_node("unit", unit_id)
    if not unit or unit["status"] != "active":
        await _render(event, "⚠️ الوحدة غير موجودة أو مؤرشفة.", [[_btn("🔙 رجوع", f"{P}home:0")]])
        return
    tests = await repo.list_tests(unit_id)
    chunk, page, pages = paginate(tests, page, PAGE_SIZE)
    playable = sum(1 for t in tests if t["status"] == "active" and t["question_count"] > 0)
    lines = [f"📑 <b>{_e(unit['name'])}</b> — وحدة\n"
             f"اختبارات صالحة للعب: <b>{playable}</b> من {len(tests)}"
             + ("" if playable else "  ⚠️ <i>لن تظهر للطلاب</i>") + "\n"]
    for i, t in enumerate(chunk, start=1):
        lines.append(f"{i}. {STATUS_ICON.get(t['status'], '')} {_e(t['title'])} — {t['question_count']} سؤال — "
                     f"{str(t['created_at'])[:10]} — {SOURCE_LABEL.get(t['source_type'], '')}")
    if not tests:
        lines.append("<i>لا توجد اختبارات بعد.</i>")
    rows = []
    row: List[types.InlineKeyboardButton] = []
    for i, t in enumerate(chunk, start=1):
        row.append(_btn(str(i), f"{P}t:{t['id']}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = _nav_row(tests, page, pages, f"u:{unit_id}")
    if nav:
        rows.append(nav)
    rows.append([_btn("📤 رفع JSON", f"{P}imp:json:{unit_id}"), _btn("🧩 من كويز أبيض", f"{P}imp:quiz:{unit_id}")])
    rows.append([_btn("✏️ اسم", f"{P}ren:u:{unit_id}"), _btn("🔢 ترتيب", f"{P}ord:u:{unit_id}"),
                 _btn("🗄 أرشفة", f"{P}x:u:{unit_id}")])
    rows.append([_btn("🔙 رجوع", f"{P}s:{unit['subject_id']}:0")])
    await _render(event, "\n".join(lines), rows)


async def _ancestors_active(unit_id: str) -> bool:
    unit = await repo.get_node("unit", unit_id)
    if not unit or unit["status"] != "active":
        return False
    subject = await repo.get_node("subject", unit["subject_id"])
    if not subject or subject["status"] != "active":
        return False
    branch = await repo.get_node("branch", subject["branch_id"])
    return bool(branch and branch["status"] == "active")


async def show_test(event, test_id: str) -> None:
    t = await repo.get_test(test_id)
    if not t:
        await _render(event, "⚠️ الاختبار غير موجود.", [[_btn("🔙 رجوع", f"{P}home:0")]])
        return
    playable = t["status"] == "active" and t["question_count"] > 0 and await _ancestors_active(t["unit_id"])
    text = (f"📝 <b>{_e(t['title'])}</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"الحالة: {STATUS_ICON.get(t['status'], '')} {t['status']}\n"
            f"عدد الأسئلة: {t['question_count']}\n"
            f"المصدر: {SOURCE_LABEL.get(t['source_type'], t['source_type'])}\n"
            f"أُضيف: {str(t['created_at'])[:16].replace('T', ' ')}\n"
            f"صالح للاستخدام: {'نعم ✅' if playable else 'لا ❌'}")
    rows = [[_btn("👁 معاينة الأسئلة", f"{P}tp:{test_id}:0")]]
    if t["status"] == "active":
        rows.append([_btn("⛔ تعطيل", f"{P}ts:{test_id}:d")])
    elif t["status"] == "disabled":
        rows.append([_btn("✅ إعادة تفعيل", f"{P}ts:{test_id}:e")])
    if t["status"] != "archived":
        rows.append([_btn("🗄 أرشفة", f"{P}x:t:{test_id}")])
    rows.append([_btn("🔙 رجوع", f"{P}u:{t['unit_id']}:0")])
    await _render(event, text, rows)


def _preview_text(questions: List[Dict[str, Any]], title: str, page: int, pages: int) -> str:
    lines = [f"👁 <b>{_e(title)}</b> — صفحة {page + 1}/{pages}\n"]
    for q in questions:
        qt = _e(q["question_text"])[:300]
        lines.append(f"<b>{q['question_order'] + 1}.</b> {qt}")
        for i, o in enumerate(q["options"]):
            mark = "✅" if i == q["correct_option_id"] else "▫️"
            lines.append(f"  {mark} {_e(o)[:80]}")
        lines.append("")
    return "\n".join(lines)[:3900]


async def show_preview(event, test_id: str, page: int) -> None:
    t = await repo.get_test(test_id)
    if not t:
        await _render(event, "⚠️ الاختبار غير موجود.", [[_btn("🔙 رجوع", f"{P}home:0")]])
        return
    qs = await repo.get_test_questions_admin(test_id)
    chunk, page, pages = paginate(qs, page, PREVIEW_PAGE_SIZE)
    rows = []
    nav = _nav_row(qs, page, pages, f"tp:{test_id}")
    if nav:
        rows.append(nav)
    rows.append([_btn("🔙 رجوع", f"{P}t:{test_id}")])
    await _render(event, _preview_text(chunk, t["title"], page, pages), rows)


# ==================== التنقل ====================

@router.callback_query(F.data.startswith(P + "home:"))
async def cb_home(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_home(call, int(call.data.split(":")[2]))


@router.callback_query(F.data == "admin_challenges")
async def cb_open_from_dashboard(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_home(call, 0)


@router.callback_query(F.data.regexp(r"^adm_ch:[bs]:[0-9a-f-]{36}:\d+$"))
async def cb_node(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    _, kind, node_id, page = call.data.split(":")
    await show_node(call, kind, node_id, int(page))


@router.callback_query(F.data.regexp(r"^adm_ch:u:[0-9a-f-]{36}:\d+$"))
async def cb_unit(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    _, _, unit_id, page = call.data.split(":")
    await show_unit(call, unit_id, int(page))


@router.callback_query(F.data.regexp(r"^adm_ch:t:[0-9a-f-]{36}$"))
async def cb_test(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_test(call, call.data.split(":")[2])


@router.callback_query(F.data.regexp(r"^adm_ch:tp:[0-9a-f-]{36}:\d+$"))
async def cb_preview(call: types.CallbackQuery):
    _, _, test_id, page = call.data.split(":")
    await show_preview(call, test_id, int(page))


# ==================== حالة الاختبار / الأرشفة ====================

@router.callback_query(F.data.regexp(r"^adm_ch:ts:[0-9a-f-]{36}:[de]$"))
async def cb_test_status(call: types.CallbackQuery):
    _, _, test_id, code = call.data.split(":")
    ok = await repo.set_test_status(test_id, "disabled" if code == "d" else "active", call.from_user.id)
    if not ok:
        await call.answer("تعذّر تغيير الحالة (تغيّرت مسبقاً).", show_alert=True)
    await show_test(call, test_id)


@router.callback_query(F.data.regexp(r"^adm_ch:x:[bsut]:[0-9a-f-]{36}$"))
async def cb_archive_ask(call: types.CallbackQuery):
    _, _, kind, node_id = call.data.split(":")
    what = {"b": "الفرع", "s": "المادة", "u": "الوحدة", "t": "الاختبار"}[kind]
    extra = "" if kind == "t" else "\nسيختفي كل ما تحتها عن الطلاب، لكن تبقى البيانات محفوظة."
    await _render(call, f"🗄 أرشفة {what}؟{extra}\nلا يمكن التراجع من البوت.",
                  [[_btn("✅ نعم، أرشف", f"{P}xy:{kind}:{node_id}"), _btn("❌ إلغاء", f"{P}cancelx:{kind}:{node_id}")]])


@router.callback_query(F.data.regexp(r"^adm_ch:cancelx:[bsut]:[0-9a-f-]{36}$"))
async def cb_archive_cancel(call: types.CallbackQuery):
    _, _, kind, node_id = call.data.split(":")
    if kind == "t":
        await show_test(call, node_id)
    elif kind == "u":
        await show_unit(call, node_id)
    else:
        await show_node(call, kind, node_id)


@router.callback_query(F.data.regexp(r"^adm_ch:xy:[bsut]:[0-9a-f-]{36}$"))
async def cb_archive_do(call: types.CallbackQuery):
    _, _, kind, node_id = call.data.split(":")
    admin_id = call.from_user.id
    if kind == "t":
        t = await repo.get_test(node_id)
        await repo.set_test_status(node_id, "archived", admin_id)
        return await (show_unit(call, t["unit_id"]) if t else show_home(call))
    node_kind = KIND_LABEL[kind][0]
    node = await repo.get_node(node_kind, node_id)
    await repo.archive_node(node_kind, node_id, admin_id)
    if not node:
        return await show_home(call)
    if kind == "b":
        await show_home(call)
    elif kind == "s":
        await show_node(call, "b", node["branch_id"])
    else:
        await show_node(call, "s", node["subject_id"])


# ==================== إضافة / إعادة تسمية / ترتيب ====================

@router.callback_query(F.data.regexp(r"^adm_ch:add:[bsu]:(none|[0-9a-f-]{36})$"))
async def cb_add(call: types.CallbackQuery, state: FSMContext):
    _, _, kind, parent = call.data.split(":")
    await state.set_state(ChallengeAdminState.waiting_name)
    await state.update_data(mode="add", kind=kind, id=None if parent == "none" else parent)
    await _render(call, f"✍️ أرسل اسم {KIND_LABEL[kind][1]} الجديد (حتى 80 حرفاً):", [[_btn("❌ إلغاء", f"{P}home:0")]])


@router.callback_query(F.data.regexp(r"^adm_ch:ren:[bsu]:[0-9a-f-]{36}$"))
async def cb_rename(call: types.CallbackQuery, state: FSMContext):
    _, _, kind, node_id = call.data.split(":")
    await state.set_state(ChallengeAdminState.waiting_name)
    await state.update_data(mode="ren", kind=kind, id=node_id)
    await _render(call, f"✍️ أرسل الاسم الجديد لـ{KIND_LABEL[kind][1]}:", [[_btn("❌ إلغاء", f"{P}home:0")]])


@router.message(StateFilter(ChallengeAdminState.waiting_name), F.text)
async def msg_name(msg: types.Message, state: FSMContext):
    d = await state.get_data()
    kind, mode, ref = d["kind"], d["mode"], d.get("id")
    node_kind = KIND_LABEL[kind][0]
    try:
        if mode == "add":
            await repo.create_node(node_kind, msg.text, ref, msg.from_user.id)
        else:
            await repo.update_node(node_kind, ref, msg.from_user.id, name=msg.text)
    except ChallengeRepoError as e:
        await msg.answer(f"⚠️ {_e(e)}\nأرسل اسماً آخر أو اضغط إلغاء.", parse_mode="HTML")
        return
    except Exception as e:
        log_error(logger, f"challenge name save failed: {e}")
        await msg.answer("❌ حدث خطأ أثناء الحفظ. حاول لاحقاً.")
        return
    await state.clear()
    if mode == "add":
        if kind == "b":
            await show_home(msg)
        else:  # ref = الأب: فرع للمادة، ومادة للوحدة
            await show_node(msg, "b" if kind == "s" else "s", ref)
    elif kind == "u":
        await show_unit(msg, ref)
    else:
        await show_node(msg, kind, ref)


@router.callback_query(F.data.regexp(r"^adm_ch:ord:[bsu]:[0-9a-f-]{36}$"))
async def cb_order(call: types.CallbackQuery, state: FSMContext):
    _, _, kind, node_id = call.data.split(":")
    await state.set_state(ChallengeAdminState.waiting_order)
    await state.update_data(kind=kind, id=node_id)
    await _render(call, "🔢 أرسل رقم الترتيب (عدد صحيح؛ الأصغر يظهر أولاً):", [[_btn("❌ إلغاء", f"{P}home:0")]])


@router.message(StateFilter(ChallengeAdminState.waiting_order), F.text)
async def msg_order(msg: types.Message, state: FSMContext):
    txt = (msg.text or "").strip()
    if not (txt.lstrip("-").isdigit() and len(txt) <= 6):
        await msg.answer("⚠️ أرسل رقماً صحيحاً فقط.")
        return
    d = await state.get_data()
    await repo.update_node(KIND_LABEL[d["kind"]][0], d["id"], msg.from_user.id, sort_order=int(txt))
    await state.clear()
    if d["kind"] == "u":
        await show_unit(msg, d["id"])
    else:
        await show_node(msg, d["kind"], d["id"])


# ==================== استيراد: JSON ====================

@router.callback_query(F.data.regexp(r"^adm_ch:imp:json:[0-9a-f-]{36}$"))
async def cb_import_json(call: types.CallbackQuery, state: FSMContext):
    unit_id = call.data.split(":")[3]
    await state.set_state(ChallengeAdminState.waiting_json)
    await state.update_data(unit_id=unit_id)
    example = ('{"title": "اسم الاختبار", "questions": [{"question": "...", "options": ["أ", "ب", "ج"], '
               '"correct_option_id": 0, "explanation": "اختياري"}]}')
    await _render(call,
                  "📤 أرسل ملف <b>.json</b> (UTF-8، حتى "
                  f"{MAX_FILE_BYTES // 1024}KB).\nالصيغة:\n<code>{_e(example)}</code>\n"
                  "correct_option_id يبدأ من 0. يُقبل أيضاً ملف قائمة أسئلة مباشرة.",
                  [[_btn("❌ إلغاء", f"{P}u:{unit_id}:0")]])


@router.message(StateFilter(ChallengeAdminState.waiting_json), F.document)
async def msg_json_file(msg: types.Message, state: FSMContext):
    d = await state.get_data()
    doc = msg.document
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await msg.answer(f"❌ الملف أكبر من {MAX_FILE_BYTES // 1024} كيلوبايت.")
        return
    try:
        buf = io.BytesIO()
        await msg.bot.download(doc, destination=buf)
        parsed = parse_challenge_json(buf.getvalue(), default_title=os.path.splitext(doc.file_name or "")[0])
    except ChallengeContentError as e:
        await msg.answer(f"❌ ملف غير صالح:\n{_e(e)}\n\nصحّحه وأعد الإرسال، أو اضغط إلغاء.", parse_mode="HTML")
        return
    except Exception as e:
        log_error(logger, f"challenge json download failed: {e}")
        await msg.answer("❌ تعذّر قراءة الملف. أعد المحاولة.")
        return
    await _ask_confirm(msg, state, d["unit_id"], parsed)


@router.message(StateFilter(ChallengeAdminState.waiting_json))
async def msg_json_wrong(msg: types.Message):
    await msg.answer("📤 أرسل ملف .json كمستند (Document)، أو اضغط إلغاء من الرسالة السابقة.")


# ==================== استيراد: كويز أبيض موجود ====================

@router.callback_query(F.data.regexp(r"^adm_ch:imp:quiz:[0-9a-f-]{36}$"))
async def cb_import_quiz(call: types.CallbackQuery, state: FSMContext):
    unit_id = call.data.split(":")[3]
    await state.set_state(ChallengeAdminState.waiting_quiz_ref)
    await state.update_data(unit_id=unit_id)
    await _render(call, "🧩 أرسل <b>معرّف الكويز</b> (UUID) أو <b>كود المشاركة</b> (share code).\n"
                        "سيُنسخ محتوى الأسئلة فقط كنسخة مستقلة (بلا بيانات صاحب الكويز).",
                  [[_btn("❌ إلغاء", f"{P}u:{unit_id}:0")]])


@router.message(StateFilter(ChallengeAdminState.waiting_quiz_ref), F.text)
async def msg_quiz_ref(msg: types.Message, state: FSMContext):
    d = await state.get_data()
    try:
        row = await repo.find_quiz_for_import(msg.text)
    except Exception as e:
        log_error(logger, f"challenge quiz lookup failed: {e}")
        await msg.answer("❌ خطأ أثناء البحث. حاول لاحقاً.")
        return
    if not row:
        await msg.answer("⚠️ لم أجد كويزاً بهذا المعرّف/الكود. أعد المحاولة أو اضغط إلغاء.")
        return
    try:
        parsed = from_existing_quiz(row)
    except ChallengeContentError as e:
        await msg.answer(f"❌ لا يصلح هذا الكويز للتحديات:\n{_e(e)}", parse_mode="HTML")
        return
    await _ask_confirm(msg, state, d["unit_id"], parsed)


# ==================== التأكيد (مشترك) ====================

def _parsed_to_dict(p: ParsedTest) -> Dict[str, Any]:
    return {"title": p.title, "questions": p.questions, "source_type": p.source_type,
            "source_reference": p.source_reference, "metadata": p.metadata}


async def _breadcrumb(unit_id: str) -> Tuple[str, str, str]:
    unit = await repo.get_node("unit", unit_id)
    subject = await repo.get_node("subject", unit["subject_id"]) if unit else None
    branch = await repo.get_node("branch", subject["branch_id"]) if subject else None
    return (branch or {}).get("name", "?"), (subject or {}).get("name", "?"), (unit or {}).get("name", "?")


async def _ask_confirm(msg: types.Message, state: FSMContext, unit_id: str, parsed: ParsedTest) -> None:
    await state.set_state(ChallengeAdminState.confirm_import)
    await state.update_data(unit_id=unit_id, parsed=_parsed_to_dict(parsed))
    branch, subject, unit = await _breadcrumb(unit_id)
    await msg.answer(
        "📋 <b>ملخص الاستيراد</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"الاسم: <b>{_e(parsed.title)}</b>\n"
        f"المسار: {_e(branch)} ← {_e(subject)} ← {_e(unit)}\n"
        f"عدد الأسئلة: <b>{parsed.question_count}</b>\n"
        f"المصدر: {SOURCE_LABEL.get(parsed.source_type, '')}",
        parse_mode="HTML",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [_btn("✅ اعتماد", f"{P}ok"), _btn("❌ إلغاء", f"{P}u:{unit_id}:0")],
            [_btn("✏️ تغيير الاسم", f"{P}rt")],
        ]))


@router.callback_query(StateFilter(ChallengeAdminState.confirm_import), F.data == P + "rt")
async def cb_retitle(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(ChallengeAdminState.waiting_test_title)
    await call.message.answer("✍️ أرسل الاسم الجديد للاختبار (حتى 120 حرفاً):")
    await call.answer()


@router.message(StateFilter(ChallengeAdminState.waiting_test_title), F.text)
async def msg_new_title(msg: types.Message, state: FSMContext):
    d = await state.get_data()
    title = (msg.text or "").strip()[:120]
    if not title:
        await msg.answer("⚠️ الاسم فارغ.")
        return
    p = ParsedTest(**{**d["parsed"], "title": title})
    await _ask_confirm(msg, state, d["unit_id"], p)


@router.callback_query(F.data == P + "ok")
async def cb_confirm_import(call: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if await state.get_state() != ChallengeAdminState.confirm_import.state or not d.get("parsed"):
        # الضغطة الثانية (أو جلسة منتهية): لا تنفيذ ثانٍ
        await call.answer("انتهت هذه العملية أو نُفّذت مسبقاً.", show_alert=True)
        return
    # نفرّغ الحالة قبل أي await طويل: أي ضغطة مزدوجة تجد الحالة فارغة (والـ unique hash حماية ثانية)
    await state.clear()
    parsed = ParsedTest(**d["parsed"])
    try:
        out = await repo.import_test(d["unit_id"], parsed, call.from_user.id)
    except ChallengeRepoError as e:
        await _render(call, f"❌ {_e(e)}", [[_btn("🔙 رجوع", f"{P}home:0")]])
        return
    except Exception:
        await _render(call, "❌ فشل الحفظ ولم يُحفظ أي شيء (العملية ذرية). حاول مجدداً.",
                      [[_btn("🔙 رجوع", f"{P}u:{d['unit_id']}:0")]])
        return
    note = "✅ تم حفظ الاختبار." if out.get("created") else "ℹ️ هذا المحتوى موجود مسبقاً في الوحدة - لم يُنشأ تكرار."
    await _render(call, note, [[_btn("📝 فتح الاختبار", f"{P}t:{out['test_id']}")],
                               [_btn("🔙 للوحدة", f"{P}u:{d['unit_id']}:0")]])
