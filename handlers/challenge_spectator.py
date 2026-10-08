"""Defender-only async watching. No writes to PostgreSQL and no fake heartbeat."""
import asyncio
import json
import logging
from collections import OrderedDict
from uuid import UUID
from weakref import WeakValueDictionary
from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from config import redis_client
from helpers.supabase_helper import challenge_spectator as repo
from helpers.supabase_helper.feature_flags import is_feature_enabled
from helpers.challenge_delivery import deliver_screen
from services.challenge_spectator_presence import Presence
from services.challenge_spectator_ui import screen
from services.challenge_live_ui import Btn

router = Router(name='challenge_spectator')
router.callback_query.filter(F.message.chat.type == 'private')
logger = logging.getLogger(__name__)
presence = Presence(redis_client)
_locks = WeakValueDictionary()
_cache = OrderedDict()
_slots = asyncio.Semaphore(4)
_wake = asyncio.Event()
_pending = OrderedDict()


def markup(rows):
    return types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text=b.text,callback_data=b.data) for b in row] for row in rows])


async def send(bot, uid, message_id, text, rows):
    return await deliver_screen(bot,uid,message_id,text,markup(rows))


async def notification_markup(notice, match):
    if (notice['kind']=='attack_started' and match and match['status']=='in_progress'
            and await is_feature_enabled('spectator_mode_enabled',default=False)):
        return markup([[Btn('👁 مشاهدة الهجوم',f'sp:o:{UUID(notice["match_id"]).hex}')]])
    return None


async def incoming(bot, uid, message_id, page=0):
    rows = await repo.incoming(uid,page)
    buttons = [[Btn(f"👁 {r['attacker'][:40]} — {r['subject'][:40]}",f'sp:o:{UUID(r["id"]).hex}')] for r in rows[:5]]
    nav = []
    if page: nav.append(Btn('السابق',f'sp:l:{page-1}'))
    if len(rows)>5: nav.append(Btn('التالي',f'sp:l:{page+1}'))
    if nav: buttons.append(nav)
    buttons.append([Btn('↩️ أرضي','cg:home:0')])
    return await send(bot,uid,message_id,'👁 الهجمات الجارية على أرضك' if rows else 'لا توجد هجمات متاحة للمشاهدة الآن.',buttons)


async def update_attacker(bot, mid):
    # Cosmetic and isolated: never call advance/tick from a viewer action.
    try:
        from handlers.challenge_game import refresh_presence
        async with asyncio.timeout(5):
            await refresh_presence(bot,mid)
    except Exception:
        logger.info('spectator_attacker_ui_unavailable match=%s',mid)


async def present(bot, mid, uid, *, message_id=None, user_activity=False, page=0, index=None, opening=False):
    lock = _locks.setdefault((mid,uid),asyncio.Lock())
    async with lock:
        # Read *under* the presentation lock; an old RPC result never replaces a newer screen.
        state = await repo.state(mid,uid)
        viewers = await presence.viewers(mid)
        ref = (viewers or {}).get(uid)
        if not user_activity and ref is None:
            return True
        old = json.loads(ref) if ref else {}
        if user_activity:
            if index is not None and index != state['index']: page=0
            if state['status']=='in_progress':
                message_id = old.get('message_id') or (None if opening else message_id)
                ref = await presence.activity(mid,uid,message_id,page)
                viewers = await presence.viewers(mid)
            # A completed match is displayed without creating a new session.
        else:
            page = old.get('page',0) if old.get('index') == state['index'] else 0
            message_id = old.get('message_id')
        key = (state['status'],state['index'],len(viewers) if viewers is not None else None)
        cached = _cache.get((mid,uid))
        if not user_activity and cached and cached == (ref,key):
            return True
        if not user_activity and cached and cached[1][:2] != key[:2]: page=0
        text,rows = screen(state,len(viewers) if viewers is not None else None,page)
        shown = await send(bot,uid,message_id,text,rows)
        if shown is None:
            await presence.leave(mid,uid)
            return True
        if ref:
            new = json.loads(ref)
            new.update(message_id=shown,page=page,index=state["index"])
            new_ref = json.dumps(new)
            await presence.reference(mid,uid,ref,new_ref)
            ref = new_ref
        _cache[(mid,uid)] = (ref,key)
        _cache.move_to_end((mid,uid))
        while len(_cache)>2000: _cache.popitem(last=False)
        if state['status']!='in_progress':
            await presence.leave(mid,uid)
        return True


async def notify(bot, mid):
    """Event push / retry. Failures here never propagate into the attack path."""
    try:
        viewers = await presence.viewers(mid)
        if viewers is None: return
        if not viewers:
            await update_attacker(bot,mid)
            await presence.clear_empty(mid)
            return
        for uid in viewers:  # V1 SQL authorizes exactly one; recheck on every read.
            try:
                async with _slots, asyncio.timeout(5):
                    await present(bot,mid,uid)
            except Exception as exc:
                if 'spectator_disabled' in str(exc) or 'spectator_denied' in str(exc):
                    await presence.leave(mid,uid)
                logger.info('spectator_delivery_unavailable match=%s type=%s',mid,type(exc).__name__)
        if await presence.count(mid)==0:
            await update_attacker(bot,mid)
            await presence.clear_empty(mid)
    except Exception:
        logger.info('spectator_unavailable match=%s',mid)


def wake_spectators(mid):
    # Hints only, bounded/coalesced; PostgreSQL remains authoritative after restart.
    _pending[mid] = None
    while len(_pending)>2000: _pending.popitem(last=False)
    _wake.set()


async def beat(bot):
    # At most 50 event hints + 50 Redis discoveries, four concurrent deliveries.
    # No Telegram edits for unchanged screens; no delivery renews viewer activity.
    hints = [_pending.popitem(last=False)[0] for _ in range(min(50,len(_pending)))]
    matches = list(dict.fromkeys(hints + await presence.batch()))
    slots = asyncio.Semaphore(4)
    async def one(mid):
        async with slots:
            await notify(bot,mid)
    await asyncio.gather(*(one(mid) for mid in matches))


async def challenge_spectator_loop(bot):
    """Independent worker: slow spectators never delay the async attack timer."""
    while True:
        _wake.clear()
        try:
            await beat(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.info('spectator_beat_unavailable')
        try:
            await asyncio.wait_for(_wake.wait(),timeout=3)
        except asyncio.TimeoutError:
            pass


@router.callback_query(F.data.startswith('sp:'))
async def callback(c: types.CallbackQuery):
    try: await c.answer()
    except TelegramBadRequest: pass
    try:
        parts = c.data.split(':')
        action = parts[1]
        uid = c.from_user.id
        if action=='l' and len(parts)==3:
            return await incoming(c.bot,uid,c.message.message_id,max(0,int(parts[2])))
        if action not in {'o','r','p','x'} or len(parts)!=(5 if action=='p' else 3):
            raise ValueError('spectator_denied')
        mid = str(UUID(parts[2]))
        if action=='x':
            try:
                await repo.state(mid,uid)  # SQL checks ownership before checking the flag.
            except Exception as exc:
                if 'spectator_disabled' not in str(exc): raise
            lock = _locks.setdefault((mid,uid),asyncio.Lock())
            async with lock:
                # Idempotent removal of THIS actor only; no match data is mutated.
                left = await presence.leave(mid,uid)
                _cache.pop((mid,uid),None)
                text = ('تم إيقاف المشاهدة.' if left is not None else
                        'الحضور غير متاح مؤقتًا؛ تنتهي جلسة المشاهدة تلقائيًا خلال دقيقة.')
                await send(c.bot,uid,c.message.message_id,text,[[Btn('↩️ أرضي','cg:home:0')]])
            await update_attacker(c.bot,mid)
            return
        await present(c.bot,mid,uid,message_id=c.message.message_id,user_activity=True,
                      opening=action=='o',page=max(0,int(parts[4])) if action=='p' else 0,
                      index=int(parts[3]) if action=='p' else None)
        await update_attacker(c.bot,mid)
    except Exception as exc:
        text = ('المشاهدة غير مفعّلة حاليًا.' if 'spectator_disabled' in str(exc) else
                'لا يمكنك مشاهدة هذا الهجوم.' if 'spectator_denied' in str(exc) else
                'تعذرت المشاهدة الآن. أعد فتحها من أرضك.')
        logger.info('spectator_callback_failed type=%s',type(exc).__name__)
        await send(c.bot,c.from_user.id,c.message.message_id,text,[[Btn('↩️ أرضي','cg:home:0')]])
