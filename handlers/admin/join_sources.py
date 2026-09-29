# handlers/admin/join_sources.py
"""
📥 لوحة «طرق الانضمام إلى البوت» (لوحة الأدمن > التحليلات > طرق الانضمام).

الشاشات (كلها تعدّل نفس الرسالة):
  admin_joinsrc_{days}                         الرئيسية: إحصاءات + أزرار 7/30/90 + وصول سريع
  admin_joinsrc_list_{code}_{days}_{page}      المنضمون عبر وسيلة (bot|link|ref|web) حسب المدة
  admin_joinsrc_links_{days}_{page}            الروابط المشتركة: الكويز/صاحبه/تشغيلات/انضمامات

«قائمة الإحالات» (admin_referrals_1) هي نفس المعالج القديم في analytics.py — نُقل زرّها إلى هنا فقط.
مصدر البيانات: users.join_source (يُسجَّل عند إنشاء الحساب، راجع helpers/supabase_helper/users.py).
المستخدمون الذين انضموا قبل بدء التتبع (2026-08-30) يظهرون «غير مصنّف» عدا الإحالات (users.referred_by).
"""
import html
import re

from aiogram import Router, types, F

from constants import format_syria_time
from supabase_helper import (
    admin_get_join_source_stats,
    admin_get_joined_users,
    admin_get_shared_links_stats,
)
from logger import get_logger
from .dashboard import IsAdminFilter
from .admin_utils import safe_edit_text

logger = get_logger(__name__)
router = Router()

router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

DAY_OPTIONS = (7, 30, 90)
USERS_PAGE_SIZE = 10
LINKS_PAGE_SIZE = 5

# رمز قصير بـ callback_data -> القيمة المخزنة بـ users.join_source
SRC_CODE = {"bot": "bot", "link": "shared_link", "ref": "referral", "web": "website", "unk": "unknown"}
SRC_LABEL = {
    "bot": "🤖 من البوت مباشرة",
    "link": "🔗 عبر رابط كويز مشترك",
    "ref": "🤝 عبر الإحالات",
    "web": "🌐 من موقع abiad.me",
    "unk": "❔ غير مصنّف (قبل بدء التتبع)",
}


def _esc(value, limit: int = 60) -> str:
    text = (value or "").strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return html.escape(text)


def _person(row: dict) -> str:
    name = _esc(row.get("first_name")) or str(row["user_id"])
    username = row.get("username")
    tag = f" (@{_esc(username)})" if username and username != "Unknown" else ""
    return f'<a href="tg://user?id={row["user_id"]}">{name}</a>{tag} — 🆔 <code>{row["user_id"]}</code>'


def _days_row(builder, days: int) -> list:
    """صف أزرار 7/30/90؛ builder(d) يرجع callback_data لنفس الشاشة بالمدة الجديدة (ويصفّر الصفحة)."""
    return [
        types.InlineKeyboardButton(text=(f"✅ {d} يوم" if d == days else f"{d} يوم"), callback_data=builder(d))
        for d in DAY_OPTIONS
    ]


def _nav_row(builder, page: int, total: int, size: int) -> list:
    row = []
    if page > 0:
        row.append(types.InlineKeyboardButton(text="◀️ السابق", callback_data=builder(page - 1)))
    if (page + 1) * size < total:
        row.append(types.InlineKeyboardButton(text="التالي ▶️", callback_data=builder(page + 1)))
    return row


# ==================== الشاشة الرئيسية ====================
@router.callback_query(F.data.regexp(r"^admin_joinsrc_(7|30|90)$"))
async def show_join_sources_overview(call: types.CallbackQuery):
    days = int(call.data.rsplit("_", 1)[1])
    try:
        counts = await admin_get_join_source_stats(days)
    except Exception:
        await call.answer("❌ تعذر جلب إحصاءات طرق الانضمام.", show_alert=True)
        return

    total = sum(counts.values())

    def pct(n: int) -> str:
        return f" ({n * 100 // total}%)" if total else ""

    lines = [
        "📥 <b>طرق الانضمام إلى البوت</b>",
        f"📅 آخر <b>{days}</b> يوم",
        f"👥 إجمالي المنضمين الجدد: <code>{total}</code>",
        "───────────────────",
    ]
    for code in ("bot", "link", "ref", "web"):
        n = counts.get(SRC_CODE[code], 0)
        lines.append(f"{SRC_LABEL[code]}: <code>{n}</code>{pct(n)}")
    unknown = counts.get("unknown", 0)
    if unknown:
        lines.append(f"{SRC_LABEL['unk']}: <code>{unknown}</code>{pct(unknown)}")

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        _days_row(lambda d: f"admin_joinsrc_{d}", days),
        [
            types.InlineKeyboardButton(text="🎯 قائمة الإحالات", callback_data="admin_referrals_1"),
            types.InlineKeyboardButton(text="🔗 قائمة الروابط المشتركة", callback_data=f"admin_joinsrc_links_{days}_0"),
        ],
        [
            types.InlineKeyboardButton(text="👥 منضمّو البوت", callback_data=f"admin_joinsrc_list_bot_{days}_0"),
            types.InlineKeyboardButton(text="👥 منضمّو الروابط", callback_data=f"admin_joinsrc_list_link_{days}_0"),
        ],
        [
            types.InlineKeyboardButton(text="👥 منضمّو الإحالات", callback_data=f"admin_joinsrc_list_ref_{days}_0"),
            types.InlineKeyboardButton(text="👥 منضمّو الموقع", callback_data=f"admin_joinsrc_list_web_{days}_0"),
        ],
        [types.InlineKeyboardButton(text="📊 رجوع للتحليلات", callback_data=f"admin_analytics_{days}")],
        [types.InlineKeyboardButton(text="🏠 الرئيسية", callback_data="admin_main_menu")],
    ])
    await safe_edit_text(call.message, "\n".join(lines), reply_markup=kb)
    await call.answer()


# ==================== المنضمون عبر وسيلة ====================
@router.callback_query(F.data.regexp(r"^admin_joinsrc_list_(bot|link|ref|web|unk)_(7|30|90)_(\d+)$"))
async def show_joined_users(call: types.CallbackQuery):
    m = re.match(r"^admin_joinsrc_list_(bot|link|ref|web|unk)_(7|30|90)_(\d+)$", call.data)
    code, days, page = m.group(1), int(m.group(2)), int(m.group(3))
    try:
        rows = await admin_get_joined_users(SRC_CODE[code], days, USERS_PAGE_SIZE, page * USERS_PAGE_SIZE)
    except Exception:
        await call.answer("❌ تعذر جلب قائمة المنضمين.", show_alert=True)
        return

    total = int(rows[0]["total_count"]) if rows else 0
    total_pages = max(1, -(-total // USERS_PAGE_SIZE))
    lines = [
        f"👥 <b>{SRC_LABEL[code]}</b>",
        f"📅 آخر {days} يوم — العدد: <code>{total}</code> | صفحة {page + 1}/{total_pages}",
        "───────────────────",
        "",
    ]
    if not rows:
        lines.append("لا يوجد منضمون ضمن هذه الفترة.")
    for idx, r in enumerate(rows, start=page * USERS_PAGE_SIZE + 1):
        entry = f"{idx}. {_person(r)}\n    🕒 {format_syria_time(r['joined_at'], fmt='%m-%d %I:%M %p')}"
        if r.get("detail"):
            icon = "🔗 الكويز" if code == "link" else "🎯 بدعوة من"
            entry += f" — {icon}: {_esc(r['detail'])}"
        lines.append(entry)

    nav = _nav_row(lambda p: f"admin_joinsrc_list_{code}_{days}_{p}", page, total, USERS_PAGE_SIZE)
    kb_rows = [_days_row(lambda d: f"admin_joinsrc_list_{code}_{d}_0", days)]
    if nav:
        kb_rows.append(nav)
    kb_rows.append([types.InlineKeyboardButton(text="📥 رجوع لطرق الانضمام", callback_data=f"admin_joinsrc_{days}")])
    await safe_edit_text(call.message, "\n".join(lines), reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await call.answer()


# ==================== قائمة الروابط المشتركة ====================
@router.callback_query(F.data.regexp(r"^admin_joinsrc_links_(7|30|90)_(\d+)$"))
async def show_shared_links(call: types.CallbackQuery):
    m = re.match(r"^admin_joinsrc_links_(7|30|90)_(\d+)$", call.data)
    days, page = int(m.group(1)), int(m.group(2))
    try:
        rows = await admin_get_shared_links_stats(days, LINKS_PAGE_SIZE, page * LINKS_PAGE_SIZE)
    except Exception:
        await call.answer("❌ تعذر جلب قائمة الروابط المشتركة.", show_alert=True)
        return

    total = int(rows[0]["total_count"]) if rows else 0
    total_pages = max(1, -(-total // LINKS_PAGE_SIZE))
    lines = [
        "🔗 <b>الروابط المشتركة</b>",
        f"📅 آخر {days} يوم — كويزات نشطة: <code>{total}</code> | صفحة {page + 1}/{total_pages}",
        "───────────────────",
        "",
    ]
    if not rows:
        lines.append("لا يوجد نشاط على روابط مشتركة ضمن هذه الفترة.")
    for idx, r in enumerate(rows, start=page * LINKS_PAGE_SIZE + 1):
        lines.append(
            f"<b>{idx}. {_esc(r['title'], 70)}</b>\n"
            f" ┣ 👤 صاحب الكويز: {_esc(r.get('owner_name'))} — 🆔 <code>{r['owner_id']}</code>\n"
            f" ┣ ▶️ تشغيل: <code>{r['runs']}</code> (من <code>{r['unique_runners']}</code> مستخدم)\n"
            f" ┗ 🆕 انضمام عبر هذا الرابط: <code>{r['joins']}</code>\n"
        )

    nav = _nav_row(lambda p: f"admin_joinsrc_links_{days}_{p}", page, total, LINKS_PAGE_SIZE)
    kb_rows = [_days_row(lambda d: f"admin_joinsrc_links_{d}_0", days)]
    if nav:
        kb_rows.append(nav)
    kb_rows.append([types.InlineKeyboardButton(text="👥 منضمّو الروابط", callback_data=f"admin_joinsrc_list_link_{days}_0")])
    kb_rows.append([types.InlineKeyboardButton(text="📥 رجوع لطرق الانضمام", callback_data=f"admin_joinsrc_{days}")])
    await safe_edit_text(call.message, "\n".join(lines), reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await call.answer()
