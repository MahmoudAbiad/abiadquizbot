"""Pure rendering helpers for the live-challenge Telegram UI (no aiogram, no network, no DB).

Everything here maps a *backend state* (PostgreSQL via helpers.supabase_helper.challenge_live) to
(text, rows). Nothing is computed that the backend did not return: scores, correctness, winners,
rewards and deadlines all come from `match_state()`; time is only ever the difference between two
SERVER timestamps (`server_now`, `deadline`, `countdown_until`) and is display-only.

Answer-leak rule: while a question is open the state contains no correct option and no opponent pick;
this module additionally renders `previous.correct_option_id` ONLY from the `previous` block (a resolved
question). The open question is rendered from `state["question"]` (text/options/image only).
"""
import hashlib
import math
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

TERMINAL = {"completed", "expired", "abandoned", "cancelled"}
PAGE_OPTIONS = 4          # options per page (long option lists paginate; callback stays tiny)
PAGE_ITEMS = 6            # subjects / units per menu page
MAX_QUESTION_CHARS = 1200
MAX_OPTION_CHARS = 300
EMPTY = "📚 لا توجد اختبارات متاحة لهذه الوحدة حاليًا."
DISABLED = "⚔️ المواجهات المباشرة غير متاحة حاليًا."


@dataclass(frozen=True)
class Btn:
    text: str
    data: str = None
    url: str = None


# ----------------------------------------------------------------------------- callback data
def hexid(value):
    return UUID(str(value)).hex


def fullid(hex_value):
    return str(UUID(hex_value))


def cb(*parts):
    """`lv:<action>:...` — max 64 bytes (Telegram). Carries ids only: never an answer, snapshot or user id."""
    data = ":".join(["lv", *[str(p) for p in parts]])
    if len(data.encode()) > 64:
        raise ValueError("callback_data too long")
    return data


def request_key(user_id, callback_id):
    """Idempotent search key derived from the Telegram callback: a retried/duplicated callback maps to the
    SAME key (backend returns the current state instead of re-queueing); a new press is a new key."""
    digest = hashlib.sha1(f"{user_id}:{callback_id}".encode()).hexdigest()[:12]
    return f"lv:{digest}"


def token_of(key):
    return key[3:] if key and key.startswith("lv:") else "-"


def key_of(token):
    """'-' means 'no key known' (recovered screen): cancel then applies to the user's own waiting search."""
    return None if token in ("", "-", None) else f"lv:{token}"


# ----------------------------------------------------------------------------- time (server timestamps only)
def parse_ts(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None


def seconds_until(target, server_now):
    t, n = parse_ts(target), parse_ts(server_now)
    if not t or not n:
        return 0
    return max(0, int(math.ceil((t - n).total_seconds())))


def is_overdue(state):
    """True when server time already passed what the worker should have ticked (countdown end / deadline)."""
    now = parse_ts(state.get("server_now"))
    if not now:
        return False
    if state.get("status") == "created":
        until = parse_ts(state.get("countdown_until"))
        return bool(until and until <= now)
    if state.get("status") == "in_progress":
        deadline = parse_ts(state.get("deadline"))
        return bool(deadline and deadline <= now)
    return False


def state_key(state):
    return (state.get("status"), state.get("index"))


def signature(screen):
    text, rows = screen
    raw = text + "|" + "|".join(f"{b.text}>{b.data or b.url}" for row in rows for b in row)
    return hashlib.sha1(raw.encode()).hexdigest()


# ----------------------------------------------------------------------------- errors
ERRORS = {
    "already_queued": "أنت في قائمة البحث بالفعل.",
    "in_live_match": "لديك مباراة جارية بالفعل.",
    "active_attack": "لديك هجوم جارٍ على أرض. أنهِه أو انسحب منه أولًا.",
    "game_disabled": DISABLED,
    "no_content": EMPTY,
    "branch_mismatch": "هذه الوحدة ليست ضمن فرعك الدراسي الحالي.",
    "profile_missing": "تعذر فتح ملفك الآن. افتح «أرضي» أولًا ثم أعد المحاولة.",
    "invalid_request": "تعذر بدء البحث. حاول مرة أخرى.",
    "not_your_match": "هذه المباراة ليست لك.",
    "not_found": "هذه المباراة غير موجودة.",
    "invalid_option": "الخيار غير صالح. حدّث السؤال.",
}
GENERIC_ERROR = "تعذرت العملية الآن. يمكنك الضغط على «متابعة المباراة» لاستعادة حالتك دون فقد تقدّمك."


def error_code(exc):
    text = str(exc)
    return next((code for code in ERRORS if code in text), None)


def error_screen(exc):
    code = error_code(exc)
    rows = [[Btn("↩️ أرضي", "cg:home:0")]]
    if code in ("already_queued", "in_live_match"):
        rows.insert(0, [Btn("⚔️ متابعة المباراة", cb("q"))])
    elif code in ("no_content", "branch_mismatch"):
        rows.insert(0, [Btn("🔁 اختيار وحدة أخرى", cb("m", 0))])
    return ERRORS.get(code, GENERIC_ERROR), rows


# ----------------------------------------------------------------------------- menus
def _clip(value, limit):
    value = str(value or "")
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def menu_subjects(branch_name, subjects, page=0):
    page = max(0, page)
    if not subjects:
        return EMPTY, [[Btn("↩️ أرضي", "cg:home:0")]]
    chunk = subjects[page * PAGE_ITEMS:(page + 1) * PAGE_ITEMS]
    rows = [[Btn(_clip(s["name"], 40), cb("u", hexid(s["id"]), 0))] for s in chunk]
    nav = []
    if page:
        nav.append(Btn("السابق", cb("m", page - 1)))
    if len(subjects) > (page + 1) * PAGE_ITEMS:
        nav.append(Btn("التالي", cb("m", page + 1)))
    if nav:
        rows.append(nav)
    rows.append([Btn("↩️ أرضي", "cg:home:0")])
    return f"⚔️ تحدي طالب\n📚 {branch_name}\n\nاختر المادة:", rows


def menu_units(subject_name, subject_id, units, page=0):
    page = max(0, page)
    if not units:
        return EMPTY, [[Btn("↩️ المواد", cb("m", 0))]]
    chunk = units[page * PAGE_ITEMS:(page + 1) * PAGE_ITEMS]
    rows = [[Btn(_clip(u["name"], 40), cb("j", hexid(u["id"])))] for u in chunk]
    nav = []
    if page:
        nav.append(Btn("السابق", cb("u", hexid(subject_id), page - 1)))
    if len(units) > (page + 1) * PAGE_ITEMS:
        nav.append(Btn("التالي", cb("u", hexid(subject_id), page + 1)))
    if nav:
        rows.append(nav)
    rows.append([Btn("↩️ المواد", cb("m", 0))])
    return (f"⚔️ تحدي طالب\n📘 {subject_name}\n\nاختر الوحدة لبدء البحث عن خصم.\n"
            "⏱ لكل سؤال مؤقت، والإجابة المتأخرة لا تُحسب."), rows


# ----------------------------------------------------------------------------- queue screens
def searching(labels, queue, token):
    waited = int((queue or {}).get("waited_seconds") or 0)
    left = seconds_until((queue or {}).get("expires_at"), (queue or {}).get("server_now"))
    text = ("☁️🔎 جارٍ البحث عن خصم…\n"
            f"📘 {labels.get('subject', '—')} — {labels.get('unit', '—')}\n"
            f"⏳ مدة الانتظار: {waited} ث"
            + (f" — ينتهي البحث خلال نحو {left} ث" if left else ""))
    return text, [[Btn("❌ إلغاء البحث", cb("c", token))], [Btn("🔄 تحديث", cb("q"))]]


def queue_ended(kind, unit_hex=None):
    if kind == "expired":
        retry = cb("j", unit_hex) if unit_hex else cb("m", 0)
        return "لم نجد خصمًا مناسبًا حاليًا.", [
            [Btn("🔄 إعادة المحاولة", retry)],
            [Btn("🏕️ هجوم على أرض", "cg:l:-1:0")],
            [Btn("↩️ رجوع", "cg:home:0")]]
    return "تم إلغاء البحث.", [[Btn("🔁 تحدي جديد", cb("m", 0))], [Btn("↩️ أرضي", "cg:home:0")]]


def no_live_match():
    return "لا توجد مباراة جارية الآن.", [[Btn("🔁 تحدي جديد", cb("m", 0))], [Btn("↩️ أرضي", "cg:home:0")]]


def leave_confirm(match_id, in_countdown):
    mid = hexid(match_id)
    text = ("تأكيد إلغاء المباراة؟ لم تبدأ بعد، ولن تُسجَّل نتيجة." if in_countdown else
            "تأكيد الانسحاب؟ ستُحسب خسارتك ولن تحصل على أي مكافأة.")
    return text, [[Btn("🏳️ تأكيد", cb("z", mid)), Btn("↩️ متابعة", cb("r", mid))]]


# ----------------------------------------------------------------------------- match screens
def _round_line(who, side):
    if not side or side.get("is_correct") is None and not side.get("timed_out"):
        return f"{who}: —"
    if side.get("timed_out"):
        return f"{who}: ⌛ لم يجب في الوقت"
    mark = "✅ صحيحة" if side.get("is_correct") else "❌ خاطئة"
    return f"{who}: {mark} (+{side.get('score') or 0})"


def _previous_block(state):
    prev = state.get("previous")
    if not prev:
        return ""
    opp = state["opponent"]["name"] or "الخصم"
    text = ("📌 نتيجة السؤال السابق\n"
            f"{_round_line('أنت', prev.get('mine'))}\n{_round_line(opp, prev.get('opponent'))}\n")
    correct = prev.get("correct_option_id")
    if isinstance(correct, int):
        text += f"الإجابة الصحيحة: الخيار {correct + 1}\n"
    return text + "\n"


def _found(state):
    opp = state["opponent"]["name"] or "طالب"
    left = seconds_until(state.get("countdown_until"), state.get("server_now"))
    mid = hexid(state["id"])
    head = ("⚔️ تم العثور على خصم!\n"
            f"👤 خصمك: {opp}\n"
            f"📘 {state['labels']['subject']} — {state['labels']['unit']}\n\n")
    head += f"⏳ تبدأ المباراة بعد {left}…" if left > 0 else "🔥 ابدأ!"
    return head, [[Btn("❌ إلغاء المباراة", cb("x", mid)), Btn("🔄 تحديث", cb("r", mid))]]


def question_screen(state, page=0):
    mid, idx, total = hexid(state["id"]), state["index"], state["total"]
    q, me, opp = state["question"], state["me"], state["opponent"]
    options = list(q.get("options") or [])
    pages = max(1, math.ceil(len(options) / PAGE_OPTIONS))
    page = min(max(page, 0), pages - 1)
    left = seconds_until(state.get("deadline"), state.get("server_now"))
    opp_name = opp["name"] or "الخصم"
    text = ("🔥 ابدأ!\n" if idx == 0 and not state.get("previous") else "")
    text += _previous_block(state)
    text += (f"⚔️ السؤال {idx + 1} / {total}\n"
             f"🟢 أنت: {me['score']} — 🔵 {opp_name}: {opp['score']}\n"
             f"⏱ لديك نحو {left} ثانية\n\n{_clip(q.get('text'), MAX_QUESTION_CHARS)}\n\n")
    rows = []
    if me.get("answered"):
        text += "✅ تم تسجيل إجابتك — بانتظار الخصم…" if not opp.get("answered") else "✅ تم تسجيل إجابتك."
        for i, option in enumerate(options):
            text += f"\n{i + 1}) {_clip(option, MAX_OPTION_CHARS)}"
    else:
        shown = range(page * PAGE_OPTIONS, min(len(options), (page + 1) * PAGE_OPTIONS))
        for i in shown:
            text += f"{i + 1}) {_clip(options[i], MAX_OPTION_CHARS)}\n\n"
        buttons = [Btn(f"الإجابة {i + 1}", cb("a", mid, idx, i)) for i in shown]
        rows += [buttons[k:k + 2] for k in range(0, len(buttons), 2)]
        nav = []
        if page:
            nav.append(Btn("خيارات سابقة", cb("p", mid, idx, page - 1)))
        if page + 1 < pages:
            nav.append(Btn("خيارات أخرى", cb("p", mid, idx, page + 1)))
        if nav:
            rows.append(nav)
        if opp.get("answered"):
            text += f"⚡ {opp_name} أجاب بالفعل"
    image = q.get("image_url")
    if image and str(image).startswith("https://"):
        rows.append([Btn("🖼 صورة السؤال", url=image)])
    rows.append([Btn("🔄 تحديث المباراة", cb("r", mid)), Btn("🏳️ انسحاب", cb("x", mid))])
    return text.rstrip(), rows


def _mine(result, user_id):
    for p in (result or {}).get("players") or []:
        if int(p.get("user_id", -1)) == int(user_id):
            return p
    return {}


def final_screen(state, profile=None):
    r = state.get("result") or {}
    me, opp, status = state["me"], state["opponent"], state["status"]
    mine, theirs = _mine(r, me["id"]), _mine(r, opp["id"])
    total = state.get("total") or r.get("total") or 0
    rows = [[Btn("🔁 تحدي جديد", cb("m", 0)), Btn("📊 التفاصيل", "cg:s:0")], [Btn("🏕️ أرضي", "cg:home:0")]]
    where = f"📘 {state['labels']['subject']} — {state['labels']['unit']}\n"
    opp_name = opp["name"] or "الخصم"
    if status == "cancelled":
        who = r.get("forfeit_by")
        why = "ألغيت المباراة قبل أن تبدأ." if who is not None and int(who) == int(me["id"]) else "ألغى خصمك المباراة قبل أن تبدأ."
        return f"🚫 المباراة أُلغيت\n{where}{why}\nلا نتيجة ولا مكافآت.", rows
    if status == "expired":
        return (f"⌛ انتهت مهلة المباراة قبل اكتمالها\n{where}"
                "لا مكافآت ولا إحصاءات لأي طرف."), rows
    xp, trophies = mine.get("xp", 0), mine.get("trophies", 0)
    reward = f"⭐ +{xp} XP — 🏆 {trophies:+} كأس"
    if status == "abandoned":
        quitter = r.get("forfeit_by")
        if quitter is not None and int(quitter) == int(me["id"]):
            head = "🏳️ انسحبت من المباراة\nتُحسب خسارتك ولا مكافأة لك."
        else:
            head = f"🏆 فزت! انسحب {opp_name} من المباراة.\nلا XP للمباريات غير المكتملة."
        return f"{head}\n{where}{reward}" + _streak(profile), rows
    winner = r.get("winner_id")
    if r.get("draw"):
        verdict = "🤝 تعادل!"
    elif winner is not None and int(winner) == int(me["id"]):
        verdict = "🏆 الفائز: أنت!"
    else:
        verdict = f"🏆 الفائز: {opp_name}"
    avg = (mine.get("average_response_ms") or 0) / 1000
    text = (f"🏁 انتهت المواجهة\n{where}{verdict}\n\n"
            f"🟢 أنت: {mine.get('score', 0)} نقطة — ✅ {mine.get('correct', 0)}/{total}\n"
            f"🔵 {opp_name}: {theirs.get('score', 0)} نقطة — ✅ {theirs.get('correct', 0)}/{total}\n"
            + (f"⏱ متوسط زمن إجابتك: {avg:.1f} ث\n" if avg else "") + reward)
    if xp == 0 and trophies == 0:
        text += "\nلا مكافأة لهذه المباراة: التكرار أو السقف اليومي."
    return text + _streak(profile), rows


def _streak(profile):
    if not profile:
        return ""
    return f"\n🔥 سلسلة الفوز: {profile.get('win_streak', 0)} — 🏆 رصيدك: {profile.get('trophies', 0)} كأس"


def match_screen(state, page=0, profile=None):
    status = state["status"]
    if status == "created":
        return _found(state)
    if status == "in_progress" and state.get("question"):
        return question_screen(state, page)
    if status in ("in_progress",):          # transient: last question resolved, settlement not yet visible
        return "⏳ جارٍ احتساب النتيجة…", [[Btn("🔄 تحديث المباراة", cb("r", hexid(state["id"])))]]
    return final_screen(state, profile)
