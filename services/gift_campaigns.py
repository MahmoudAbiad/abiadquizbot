"""Durable gift delivery. Grants are idempotent; uncertain Telegram sends are reported."""
import asyncio
from html import escape

from aiogram import types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from logger import get_logger
from services.gift_presentation import expiry_text, remaining_expiry_text

logger = get_logger(__name__)


def campaign_report(c):
    heading = {'completed': '✅ اكتملت حملة الهدية', 'running': '⏳ حملة الهدية قيد التنفيذ',
               'draft': '📝 مسودة هدية — لم تبدأ', 'cancelled': '✖️ مسودة ملغاة'}[c['status']]
    return (f"{heading}\n<b>{escape(c['title'])}</b>\n\n"
            f"👥 المستهدفون: {c['recipients']}\n"
            f"🎁 تم المنح: {c['granted']} · بانتظار المنح: {c['grant_pending']} · حسابات محذوفة: {c['skipped']}\n"
            f"✉️ تم تسليم الرسالة: {c['sent']}\n"
            f"🚫 حظروا البوت: {c['blocked']} · فشل الإرسال: {c['failed']}\n"
            f"❔ تسليم غير مؤكد: {c['uncertain']} · بانتظار الإرسال: {c['notice_pending']}\n\n"
            'النقاط تُمنح مرة واحدة لكل طالب. تعذّر تسليم الرسالة لا يلغي الهدية.')


def campaign_report_buttons(campaign_id):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text='🔄 تحديث التقرير', callback_data=f'agc:view:{campaign_id}')],
        [types.InlineKeyboardButton(text='🎁 حملات الهدايا', callback_data='agc:list:0')],
        [types.InlineKeyboardButton(text='🔙 أكواد الهدايا', callback_data='ag:home:0')],
    ])


def campaign_notice(c, recipient):
    return (f"🎁 <b>{escape(c['title'])}</b>\n\n{escape(c['message'])}\n\n"
            f"✅ أُضيفت <b>{c['points']:g} نقطة هدية</b> إلى رصيدك.\n"
            f"⏳ {remaining_expiry_text(recipient.get('expires_at'))}\n"
            f"📅 انتهاء الهدية: {expiry_text(recipient.get('expires_at'))}")


async def process_campaign(client, bot, c):
    """Bounded work, safe across workers/restarts. Never mutate the user's FSM."""
    campaign_id = c['id']
    if c['status'] == 'running':
        await client.rpc('admin_gift_campaign_grant_step', {'p_id': campaign_id, 'p_limit': 50}).execute()
        claimed = await client.rpc('admin_gift_campaign_claim', {'p_id': campaign_id, 'p_limit': 10}).execute()
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text='📝 إنشاء كويز', callback_data='student_create_quiz')],
            [types.InlineKeyboardButton(text='💰 رصيدي وهداياي', callback_data='gift:wallet')],
        ])
        for index, recipient in enumerate(claimed.data or []):
            params = dict(p_id=campaign_id, p_user_id=recipient['user_id'], p_token=recipient['token'],
                          p_status='sent', p_error='', p_retry=0)
            try:
                # Bound each send so a claimed batch stays within the 2-minute
                # ambiguity window, even when Telegram is temporarily unreachable.
                await bot.send_message(recipient['user_id'], campaign_notice(c, recipient),
                                       parse_mode='HTML', reply_markup=keyboard, request_timeout=10)
            except TelegramRetryAfter as exc:
                params.update(p_status='pending', p_error='rate limit', p_retry=max(1, int(exc.retry_after) + 1))
            except TelegramForbiddenError:
                params.update(p_status='blocked', p_error='bot blocked or account unavailable')
            except TelegramBadRequest:
                params.update(p_status='failed', p_error='Telegram rejected message')
            except Exception as exc:
                # A timeout might mean Telegram already accepted the message.
                params.update(p_status='uncertain', p_error=type(exc).__name__)
            await client.rpc('admin_gift_campaign_notice', params).execute()
            if params['p_status'] == 'pending':
                # A Telegram flood wait applies to the bot, not just one user.
                # Return remaining unsent claims to the queue without sending.
                remaining = (claimed.data or [])[index + 1:]
                for waiting in remaining:
                    await client.rpc('admin_gift_campaign_notice', dict(
                        p_id=campaign_id, p_user_id=waiting['user_id'], p_token=waiting['token'],
                        p_status='pending', p_error='waiting for Telegram rate limit', p_retry=params['p_retry'])).execute()
                break
            await asyncio.sleep(0.05)
    result = await client.rpc('admin_gift_campaign_status', {'p_id': campaign_id}).execute()
    report = result.data
    if report and report['status'] == 'completed' and not report['report_done']:
        # Claim the completion notice before sending. Never overwrite a menu the
        # admin may now be using; the durable report is always available in UI.
        claim = await client.table('admin_gift_campaigns').update({'report_done': True}).eq(
            'id', campaign_id).eq('report_done', False).execute()
        if not claim.data:
            return
        try:
            await bot.send_message(chat_id=report['report_chat_id'], text=campaign_report(report),
                parse_mode='HTML', reply_markup=campaign_report_buttons(campaign_id), request_timeout=10)
        except Exception:
            logger.warning('Campaign report notice unavailable id=%s; accessible from campaign list', campaign_id)


async def gift_campaign_loop():
    from config import bot
    from helpers.supabase_helper._client import supabase
    while True:
        try:
            response = await supabase.table('admin_gift_campaigns').select('*').or_(
                'status.eq.running,and(status.eq.completed,report_done.eq.false)').order('created_at').limit(5).execute()
            for campaign in response.data or []:
                try:
                    await process_campaign(supabase, bot, campaign)
                except Exception:
                    logger.exception('Campaign step failed; durable state retained id=%s', campaign['id'])
        except Exception:
            logger.exception('Gift campaign queue unavailable; retrying')
            await asyncio.sleep(50)
        await asyncio.sleep(10)
