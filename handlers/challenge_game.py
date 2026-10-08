"""Private-chat land/attack UI. No FSM dependence: Postgres owns the entire session."""
import asyncio
import datetime as dt
import logging
from uuid import UUID
from collections import OrderedDict
from weakref import WeakValueDictionary

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from helpers.supabase_helper import challenges as content
from helpers.supabase_helper import challenge_game as repo
from helpers.supabase_helper import challenge_live as live_repo
from helpers.supabase_helper.feature_flags import is_feature_enabled
from services.challenge_game import load_rules, subject_icon, tower_progress
from services.challenge_notifications import notification_text
from services.challenge_presentation import PhotoText, source_image, image_option_label
from helpers.challenge_delivery import deliver_screen

router = Router(name="challenge_game")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
logger = logging.getLogger(__name__)
_attack_locks = WeakValueDictionary()
_attack_views = OrderedDict()
EMPTY = "📚 لا توجد اختبارات متاحة لهذه الوحدة حاليًا."
ERRORS = {
    "self_attack": "لا يمكنك مهاجمة أرضك.",
    "target_cooldown": "⏳ مهلة إعادة مهاجمة الطالب نفسه لم تنتهِ بعد.",
    "daily_limit": "وصلت إلى الحد اليومي للهجمات. عد غدًا.",
    "no_content": EMPTY,
    "branch_mismatch": "اختر أرضًا من فرعك الدراسي الحالي.",
    "profile_missing": "هذه الأرض غير متاحة الآن.",
    "not_your_attack": "هذا الهجوم ليس لك.",
    "game_disabled": "نظام التحديات غير متاح حاليًا.",
    "invalid_option": "الخيار غير صالح. حدّث السؤال.",
}


def button(text, data):
    if len(data.encode()) > 64:
        raise ValueError("callback_data too long")
    return types.InlineKeyboardButton(text=text, callback_data=data)


def keyboard(rows):
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def render(message, text, rows):
    try:
        return await message.edit_text(text, reply_markup=keyboard(rows), parse_mode=None)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return message
        return await message.answer(text, reply_markup=keyboard(rows), parse_mode=None)


async def identity(user, branch=None):
    # Deliberately never read phone, email, payment or paid-points tables.
    return await repo.profile(user.id, user.full_name or user.username or "طالب", branch)


async def home(message, user, page=0):
    p = await identity(user)
    if not p["branch_id"]:
        return await branches(message)
    towers = await repo.towers(user.id, p["branch_id"])
    rules = load_rules()
    text = f"🏕️ أرضي\n🏆 {p['trophies']} كأس — ⭐ {p['xp']} XP\n\n"
    for t in towers[page*6:page*6+6]:
        level = tower_progress(t["xp"], t["correct"], t["questions"], rules)
        text += (f"{subject_icon(t['name'])} {t['name']} — المستوى {level['level']}\n"
                 f"XP: {t['xp']} / {level['next_level_xp']}\n")
    if not towers:
        text += EMPTY
    rows = [[button("🗺️ البحث عن أرض", "cg:l:-1:0")],
            [button("⚔️ سجل هجماتي", "cg:h:a:0"), button("🛡️ سجل الدفاعات", "cg:h:d:0")],
            [button("📊 إحصائياتي", "cg:s:0"), button("📚 تغيير الفرع", "cg:branches")],
            [button("🔔 الإشعارات: " + ("مفعّلة" if p["notifications_enabled"] else "مطفأة"),
                    f"cg:n:{0 if p['notifications_enabled'] else 1}")]]
    nav = []
    if page: nav.append(button("السابق", f"cg:home:{page-1}"))
    if len(towers)>(page+1)*6: nav.append(button("التالي", f"cg:home:{page+1}"))
    if nav: rows.append(nav)
    active = await repo.active_attack(user.id)
    if active: rows.insert(0, [button("▶️ متابعة الهجوم الجاري", f"cg:r:{UUID(active).hex}")])
    if await is_feature_enabled("live_matches_enabled", default=False):  # hidden while the live flag is off
        rows.insert(1, [button("⚔️ تحدي طالب", "lv:m:0")])
    try:  # live recovery: a waiting search or an in-progress live match survives restarts (PostgreSQL state)
        if (await live_repo.queue_state(user.id)).get("status") in ("waiting", "matched"):
            rows.insert(0, [button("⚔️ متابعة المباراة", "lv:q")])
    except Exception:
        pass  # before migration 0010 exists the async home must keep working
    try:
        from handlers.challenge_spectator import repo as spectator_repo
        if await spectator_repo.incoming(user.id):
            rows.insert(0, [button("👁 الهجمات الجارية على أرضي", "sp:l:0")])
    except Exception:
        pass  # additive migration/DB failure must not break land recovery
    rows.append([button("🏠 القائمة الرئيسية", "cg:main")])
    return await render(message, text, rows)


async def branches(message, page=0):
    items = await content.get_playable_branches()
    rows = [[button(x["name"], f"cg:c:{x['id']}")] for x in items[page*6:page*6+6]]
    nav = []
    if page: nav.append(button("السابق", f"cg:branches:{page-1}"))
    if len(items)>(page+1)*6: nav.append(button("التالي", f"cg:branches:{page+1}"))
    if nav: rows.append(nav)
    rows.append([button("↩️ أرضي", "cg:home:0")])
    return await render(message, "📚 اختر فرعك الدراسي" if items else EMPTY, rows)


async def land(message, user, trophies, target):
    p = await identity(user)
    if not p["branch_id"]:
        return await branches(message)
    candidate = await repo.find_land(user.id, trophies, target)
    if not candidate:
        return await render(message, "لا توجد أرض أخرى ضمن نطاقك الآن. يمكنك إعادة البحث لاحقًا.",
                            [[button("🔄 إعادة البحث", "cg:l:-1:0")], [button("↩️ أرضي", "cg:home:0")]])
    return await land_towers(message, user, candidate["user_id"], 0)


async def land_towers(message, user, target, page):
    own, p = await repo.get_profile(user.id), await repo.get_profile(target)
    if not p or not own or target == user.id or own["branch_id"] != p["branch_id"]:
        raise ValueError("branch_mismatch")
    ts = await repo.towers(target, p["branch_id"])
    text = f"🏕️ أرض {p['display_name']}\n🏆 {p['trophies']} كأس\n\n"
    rows = []
    for t in ts[page*6:page*6+6]:
        level = tower_progress(t["xp"], t["correct"], t["questions"], load_rules())
        text += f"{subject_icon(t['name'])} {t['name']} — المستوى {level['level']}\n"
        rows.append([button(f"⚔️ مهاجمة برج {t['name']}", f"cg:u:{target}:{UUID(t['id']).hex}")])
    nav = []
    if page: nav.append(button("أبراج سابقة", f"cg:t:{target}:{page-1}"))
    if len(ts)>(page+1)*6: nav.append(button("أبراج تالية", f"cg:t:{target}:{page+1}"))
    if nav: rows.append(nav)
    rows.extend([[button("⏭ الأرض التالية", f"cg:l:{p['trophies']}:{target}")], [button("↩️ أرضي", "cg:home:0")]])
    return await render(message, text if ts else EMPTY, rows)


async def units(message, target, subject_id, page=0):
    items = await content.get_playable_units(subject_id)
    # Callback uses compact UUID, keeping max Telegram actor IDs within 64 bytes.
    rows = [[button(x["name"], f"cg:b:{target}:{UUID(x['id']).hex}")] for x in items[page*6:page*6+6]]
    nav = []
    if page: nav.append(button("السابق", f"cg:v:{target}:{UUID(subject_id).hex}:{page-1}"))
    if len(items)>(page+1)*6: nav.append(button("التالي", f"cg:v:{target}:{UUID(subject_id).hex}:{page+1}"))
    if nav: rows.append(nav)
    rows.append([button("↩️ الأبراج", f"cg:t:{target}:0")])
    return await render(message, ("📚 اختر الوحدة لبدء الهجوم باختبار رسمي يختاره البوت.\n"
                                "⏱ لكل سؤال مؤقت. الإجابة المتأخرة لا تُحسب.\n"
                                f"تكرار المحتوى خلال {load_rules()['repeat_content_seconds']//3600} ساعة لا يمنح مكافآت.") if items else EMPTY, rows)


def attack_screen(state, page=0):
    mid = UUID(state["id"]).hex
    if state["status"] != "in_progress":
        r = state.get("result") or {}
        status = {"completed": "اكتمل الهجوم", "expired": "انتهت مهلة الهجوم", "abandoned": "تم الانسحاب"}.get(state["status"], state["status"])
        text = (f"🏁 {status}\n{state['labels']['subject']} — {state['labels']['unit']}\n"
                f"{'⭐' * r.get('stars', 0) or 'دون نجوم'}\n"
                f"✅ {r.get('correct', 0)}/{state['total']} — النقاط: {r.get('score', 0)}\n"
                f"⭐ +{r.get('xp', 0)} XP — 🏆 {r.get('trophies', 0):+} كأس")
        if r.get("xp", 0) == 0:
            text += "\nلا مكافأة لهذا الهجوم: النتيجة أو التكرار أو السقف اليومي."
        return text, [[button("🏕️ أرضي", "cg:home:0")], [button("🗺️ أرض أخرى", "cg:l:-1:0")]]
    q, idx = state["question"], state["index"]
    options = q["options"]
    image_url = source_image(q)
    page = min(max(page, 0), (len(options)-1)//2)
    deadline = dt.datetime.fromisoformat(state["deadline"].replace("Z", "+00:00"))
    now = dt.datetime.fromisoformat(state["server_now"].replace("Z", "+00:00"))
    remaining = max(0, int((deadline-now).total_seconds()))
    text = f"⚔️ السؤال {idx+1}/{state['total']} — ⏱ نحو {remaining} ثانية\n\n"
    if state.get("viewer_count") is not None:
        text += f"👁 جلسات مشاهدة حديثة: {state['viewer_count']}\n"
    text += "اختر الإجابة بالاعتماد على الصورة.\n" if image_url else q["text"] + "\n\n"
    rows = []
    for i in range(page*2, min(page*2+2, len(options))):
        if not image_url:
            text += f"{i+1}) {options[i]}\n\n"
        rows.append([button(image_option_label(i) if image_url else f"الإجابة {i+1}", f"cg:a:{mid}:{idx}:{i}")])
    nav = []
    if page: nav.append(button("خيارات سابقة", f"cg:p:{mid}:{idx}:{page-1}"))
    if (page+1)*2 < len(options): nav.append(button("خيارات أخرى", f"cg:p:{mid}:{idx}:{page+1}"))
    if nav: rows.append(nav)
    rows.append([button("🔄 تحديث / استعادة", f"cg:r:{mid}"), button("🏳️ انسحاب", f"cg:x:{mid}")])
    return (PhotoText(text, image_url) if image_url else text), rows


async def present_attack(bot, state, message_id, page=0):
    """Shared cosmetic lock for callback/timer/presence deliveries; game state stays in SQL."""
    from handlers.challenge_spectator import presence
    mid = state["id"]
    lock = _attack_locks.setdefault(mid, asyncio.Lock())
    async with lock:
        old = _attack_views.get(mid)
        if old and state["server_now"] < old["stamp"]:
            return
        if page is None:
            page = old["page"] if old and old["index"] == state["index"] else 0
        count = await presence.count(mid) if state["status"] == "in_progress" else None
        text, rows = attack_screen(dict(state, viewer_count=count), page)
        sig = (str(text), getattr(text, "image_url", None), str(rows))
        shown = message_id
        if not old or old["sig"] != sig or old["message_id"] != message_id:
            shown = await deliver_screen(bot, state["attacker_id"], message_id, text, keyboard(rows))
        if shown is not None:
            await repo.attach_message(mid, state["attacker_id"], shown)
        _attack_views[mid] = dict(stamp=state["server_now"], index=state["index"], page=page,
                                  sig=sig, message_id=shown, count=count)
        _attack_views.move_to_end(mid)
        while len(_attack_views)>2000: _attack_views.popitem(last=False)


async def refresh_presence(bot, mid):
    from handlers.challenge_spectator import presence
    old = _attack_views.get(mid)
    count = await presence.count(mid)
    if old and old["count"] == count:
        return
    state = await repo.attack_display(mid)
    if state and state["status"] == "in_progress" and state.get("message_id"):
        await present_attack(bot, state, state["message_id"], None)


async def show_attack(message, state, page=0):
    from handlers.challenge_spectator import wake_spectators
    try:
        await present_attack(message.bot, state, message.message_id, page)
    finally:
        wake_spectators(state["id"])


async def show_history(message, user_id, direction, page):
    rows = await repo.history(user_id, "defense" if direction == "d" else "attack", page)
    text = "🛡️ سجل الدفاعات\n" if direction == "d" else "⚔️ سجل الهجمات\n"
    for row in rows[:5]:
        label, r = row["labels"], row.get("result") or {}
        opponent = label["attacker" if direction == "d" else "defender"]
        status = {"in_progress": "جارٍ", "completed": "مكتمل", "expired": "انتهت المهلة", "abandoned": "انسحاب"}.get(row["status"], "جارٍ")
        date = dt.datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        from zoneinfo import ZoneInfo
        date = date.astimezone(ZoneInfo("Asia/Damascus")).strftime("%Y-%m-%d %H:%M")
        text += (f"\n{opponent} — {label['subject']} / {label['unit']}\n"
                 f"{status} — {r.get('correct', 0)}/{r.get('total', 0)} — {'⭐'*r.get('stars',0) or 'دون نجوم'}\n"
                 f"{date} (دمشق) — المدة {r.get('duration_seconds',0)} ث\n")
    if not rows: text += "لا يوجد سجل بعد."
    nav = []
    if page: nav.append(button("السابق", f"cg:h:{direction}:{page-1}"))
    if len(rows)>5: nav.append(button("التالي", f"cg:h:{direction}:{page+1}"))
    return await render(message, text, ([nav] if nav else [])+[[button("↩️ أرضي", "cg:home:0")]])


async def stats(message, user_id, page):
    p = await repo.get_profile(user_id)
    units_data = await repo.unit_stats(user_id)
    subjects_data = await repo.subject_stats(user_id)
    accuracy = 100*p["correct"]/p["questions"] if p["questions"] else 0
    speed = p["response_ms"]/p["responses"]/1000 if p["responses"] else 0
    text = (f"📊 إحصائياتي\nالتحديات: {p['challenges']}\nفوز: {p['wins']} — خسارة: {p['losses']}\n"
            f"الدقة: {accuracy:.1f}% — متوسط الإجابة: {speed:.1f} ث\n"
            f"دفاع ناجح: {p['defense_wins']} — دفاع خاسر: {p['defense_losses']}\n"
            f"سلسلة الفوز: {p['win_streak']} — XP: {p['xp']} — 🏆 {p['trophies']}\n")
    entries = [("المادة", row["subject_name"], row) for row in subjects_data]
    entries += [("الوحدة", row["challenge_units"]["challenge_subjects"]["name"] + " / " + row["challenge_units"]["name"], row) for row in units_data]
    for kind, name, row in entries[page*6:page*6+6]:
        avg = row["response_ms"]/row["responses"]/1000 if row["responses"] else 0
        text += (f"\n{kind}: {name} — {row['correct']}/{row['questions']}\n"
                 f"فوز {row['wins']}/{row['challenges']} — متوسط الإجابة {avg:.1f} ث")
    nav=[]
    if page: nav.append(button("السابق", f"cg:s:{page-1}"))
    if len(entries)>(page+1)*6: nav.append(button("التالي", f"cg:s:{page+1}"))
    return await render(message,text,([nav] if nav else [])+[[button("↩️ أرضي", "cg:home:0")]])


@router.message(Command("land"))
async def land_command(message: types.Message):
    if not await is_feature_enabled("challenge_system_enabled", default=False):
        await message.answer(ERRORS["game_disabled"])
        return
    screen = await message.answer("🏕️ جارٍ فتح أرضك…")
    try:
        await home(screen, message.from_user)
    except Exception:
        logger.warning("game_home_failed user=%s", message.from_user.id)
        await screen.edit_text("تعذر فتح الأرض الآن. حاول لاحقًا.")


@router.callback_query(F.data.startswith("cg:"))
async def game_callback(c: types.CallbackQuery):
    try:
        await c.answer()
    except TelegramBadRequest:
        pass  # An expired callback acknowledgement must not block durable retry handling.
    parts = c.data.split(":")
    action, user = parts[1], c.from_user
    try:
        # In-flight attacks can finish even after flags are switched off.
        if action == "main":
            from keyboards import get_main_menu_keyboard
            me = await c.bot.get_me()
            return await c.message.edit_text("🏠 القائمة الرئيسية", reply_markup=await get_main_menu_keyboard(me.username, user.id), parse_mode=None)
        if action not in {"a", "r", "p", "x", "z"}:
            if not await is_feature_enabled("challenge_system_enabled", default=False):
                return await render(c.message, ERRORS["game_disabled"], [[button("🏠 القائمة الرئيسية", "cg:main")]])
            await identity(user)
        if action=="home": await home(c.message,user,max(0,int(parts[2])))
        elif action=="branches": await branches(c.message,max(0,int(parts[2])) if len(parts)>2 else 0)
        elif action=="c":
            await identity(user,str(UUID(parts[2])))
            await home(c.message,user)
        elif action=="l": await land(c.message,user,int(parts[2]),int(parts[3]))
        elif action=="t": await land_towers(c.message,user,int(parts[2]),max(0,int(parts[3])))
        elif action in {"u","v"}: await units(c.message,int(parts[2]),str(UUID(parts[3])),max(0,int(parts[4])) if action=="v" else 0)
        elif action=="b":
            state=await repo.start_attack(user.id,int(parts[2]),str(UUID(parts[3])),c.id)
            logger.info("attack_started match=%s user=%s",state["id"],user.id)
            await show_attack(c.message,state)
        elif action in {"a","r","p","z"}:
            mid=str(UUID(parts[2]))
            state=await repo.advance(mid,user.id,"answer" if action=="a" else "abandon" if action=="z" else "resume",
                                     int(parts[3]) if action=="a" else None,int(parts[4]) if action=="a" else None,c.id if action=="a" else None)
            logger.info("%s match=%s user=%s", "answer_received" if action=="a" else "attack_resumed", mid,user.id)
            if state["status"] != "in_progress": logger.info("attack_completed match=%s user=%s",mid,user.id)
            page=int(parts[4]) if action=="p" and int(parts[3])==state["index"] else 0
            await show_attack(c.message,state,page)
        elif action=="x":
            mid=UUID(parts[2]).hex
            await repo.advance(str(UUID(mid)),user.id) # verifies ownership, advances a timed-out question
            await render(c.message,"تأكيد الانسحاب؟ سيُسجّل الهجوم دون مكافآت.",
                         [[button("🏳️ تأكيد",f"cg:z:{mid}"),button("↩️ متابعة",f"cg:r:{mid}")]])
        elif action=="h": await show_history(c.message,user.id,parts[2],max(0,int(parts[3])))
        elif action=="s": await stats(c.message,user.id,max(0,int(parts[2])))
        elif action=="n":
            await repo.toggle_notifications(user.id,parts[2]=="1")
            await home(c.message,user)
    except Exception as exc:
        if "active_attack" in str(exc):
            mid=await repo.active_attack(user.id)
            if mid:
                return await show_attack(c.message,await repo.advance(mid,user.id))
        message=next((text for code,text in ERRORS.items() if code in str(exc)),
                     "تعذرت العملية الآن. يمكنك العودة لأرضك واستعادة الهجوم دون فقد تقدّمك.")
        logger.warning("game_callback_failed user=%s action=%s type=%s",user.id,action,type(exc).__name__)
        await render(c.message,message,[[button("↩️ أرضي", "cg:home:0")]])


async def challenge_heartbeat_loop(bot):
    """One bounded indexed due-query per 3s, only after schema readiness.
    Multiple workers are safe for rewards/answers; notifications are claimed atomically.
    No Redis state is needed to recover an attack after restart.
    """
    ready=False
    while True:
        try:
            if not ready:
                # Probe schema without requiring flags to remain on after a restart.
                # Existing attacks must continue even when new game entry is disabled.
                active = await repo.has_active_attacks()
                enabled = await is_feature_enabled("challenge_system_enabled",default=False)
                if not active and not enabled:
                    await asyncio.sleep(30)
                    continue
                ready=True
            now=dt.datetime.now(dt.timezone.utc).isoformat()
            for item in await repo.due_attacks(now):
                try:
                    state=await repo.advance(item["id"],item["attacker_id"],"tick")
                    from handlers.challenge_spectator import wake_spectators
                    try:
                        await present_attack(bot, state, state["message_id"])
                    finally:
                        wake_spectators(state["id"])
                except (TelegramBadRequest,TelegramForbiddenError):
                    # Session remains recoverable through /land. Don't resend on every heartbeat.
                    logger.info("game_delivery_unavailable match=%s",item["id"])
                except Exception:
                    logger.warning("game_tick_failed match=%s",item["id"])
            for notice in await repo.claim_notifications():
                try:
                    p=await repo.get_profile(notice["user_id"])
                    if not p or not p["notifications_enabled"]: continue
                    m=await repo.notification_summary(notice["match_id"])
                    if notice["kind"]=="attack_started" and m["status"]!="in_progress": continue
                    from handlers.challenge_spectator import notification_markup
                    await bot.send_message(notice["user_id"],notification_text(notice["kind"],m),parse_mode="HTML",
                                           reply_markup=await notification_markup(notice,m))
                except Exception:
                    logger.info("game_notice_unavailable match=%s",notice["match_id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            ready=False
            logger.debug("game_heartbeat_unavailable_or_migrations_not_applied")
            await asyncio.sleep(30)
        await asyncio.sleep(3)


# Uses the already registered async router in both polling and webhook entrypoints.
from handlers.challenge_spectator import router as spectator_router
router.include_router(spectator_router)
