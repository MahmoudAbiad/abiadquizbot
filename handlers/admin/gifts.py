"""Admin code wizard, beneficiary pagination and individual gift controls."""
import math
import uuid
import secrets
import time
from decimal import Decimal, InvalidOperation
from html import escape
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from helpers.supabase_helper._client import supabase
from helpers.supabase_helper.gifts import new_code, create_code, list_gifts
from services.gift_presentation import parse_expiry, expiry_text, gift_text
from services.gift_campaigns import campaign_report, campaign_report_buttons
from services.redis_reliability import user_locked
from .dashboard import IsAdminFilter, safe_edit_text

router = Router()
router.message.filter(IsAdminFilter(), F.chat.type == 'private')
router.callback_query.filter(IsAdminFilter(), F.message.chat.type == 'private')

class CodeState(StatesGroup):
    title=State()
    points=State()
    limit=State()
    expiry=State()
    days=State()
    confirm=State()
    gift_expiry=State()
    campaign_title=State()
    campaign_points=State()
    campaign_days=State()
    campaign_message=State()
    campaign_confirm=State()
    action_confirm=State()


def kb(*rows):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=t,callback_data=d) for t,d in row] for row in rows])


def cancel_kb():
    return kb([('إلغاء','ag:home:0')])


def _missing_gift_admin_schema(exc):
    return str(getattr(exc, 'code', '')) in {'42703', '42P01', 'PGRST202', 'PGRST204', 'PGRST205'}


@router.callback_query(F.data.startswith('ag:home:'))
@router.callback_query(F.data.startswith('ag:archive:'))
async def codes_home(call, state: FSMContext):
    await state.clear()
    page=max(0,int(call.data.split(':')[-1]))
    archived = call.data.startswith('ag:archive:')
    ready = True
    try:
        query = supabase.table('gift_codes').select('*')
        query = query.not_.is_('archived_at', 'null') if archived else query.is_('archived_at', 'null')
        response=await query.order('created_at',desc=True).order('id').range(page*5,page*5+5).execute()
    except Exception as exc:
        if not _missing_gift_admin_schema(exc):
            raise
        ready = False
        archived = False
        response = await supabase.table('gift_codes').select('*').order('created_at',desc=True).order('id').range(page*5,page*5+5).execute()
    rows=response.data or []
    buttons=[[('➕ إنشاء كود','ag:new')]]
    if ready:
        buttons += [[('🎁 منح هدية للجميع','agc:new')], [('📊 حملات الهدايا وتقاريرها','agc:list:0')]]
    for c in rows[:5]:
        buttons.append([(f"{'✅' if c['enabled'] else '⛔'} {c['title'][:35]} · {c['used_count']} مستفيد",f"ag:view:{c['id']}")])
    nav=[]
    prefix = 'ag:archive' if archived else 'ag:home'
    if page: nav.append(('السابق',f'{prefix}:{page-1}'))
    if len(rows)>5: nav.append(('التالي',f'{prefix}:{page+1}'))
    if nav: buttons.append(nav)
    if ready:
        buttons.append([('🔙 الأكواد الرئيسية','ag:home:0')] if archived else [('🗂 الأكواد المؤرشفة','ag:archive:0')])
    buttons.append([('الإعدادات العامة','admin_settings_general')])
    note = '' if ready else '\n\nℹ️ ميزات الأرشفة والحملات بانتظار تحديث قاعدة بيانات هذه البيئة.'
    await safe_edit_text(call.message,('🗂 <b>الأكواد المؤرشفة</b>' if archived else '🎟 <b>أكواد الهدايا</b>')+'\nكل طالب يستخدم الكود مرة واحدة.\nإيقاف الكود يمنع الشحن الجديد. إلغاء هداياه للجميع إجراء منفصل.\n\nلإدارة هدايا طالب، افتح ملفه من إدارة المستخدمين.'+note,reply_markup=kb(*buttons))
    await call.answer()


@router.callback_query(F.data == 'ag:new')
async def new(call,state:FSMContext):
    await state.clear()
    await state.set_state(CodeState.title)
    await call.message.answer('اكتب اسم الهدية (حتى 100 حرف):',reply_markup=cancel_kb())
    await call.answer()


@router.message(CodeState.title)
async def title(message,state:FSMContext):
    value=(message.text or '').strip()
    if not 1<=len(value)<=100:
        await message.answer('أرسل اسمًا بين حرف و100 حرف.'); return
    await state.update_data(gift_draft={'id':str(uuid.uuid4()),'code':new_code(),'title':value,'created_by':message.from_user.id})
    await state.set_state(CodeState.points)
    await message.answer('كم نقطة يمنحها الكود لكل طالب؟ (حتى 100000، بمنزلتين عشريتين)',reply_markup=cancel_kb())


@router.message(CodeState.points)
async def points(message,state:FSMContext):
    try:
        value=float((message.text or '').strip())
        if not math.isfinite(value) or not 0<value<=100000 or value!=round(value,2): raise ValueError()
    except ValueError:
        await message.answer('أدخل عدد نقاط صالحًا أكبر من صفر، بمنزلتين عشريتين كحد أقصى.'); return
    draft=(await state.get_data())['gift_draft'];draft['points']=value
    await state.update_data(gift_draft=draft)
    await state.set_state(CodeState.limit)
    await message.answer('اختر نوع الكود أو أرسل عدد الطلاب الأقصى للكود الجماعي:',reply_markup=kb(
        [('طالب واحد فقط','ag:limit:1')],[('جماعي بلا حد','ag:limit:0')],[('إلغاء','ag:home:0')]))


async def save_limit(message,state,limit):
    draft=(await state.get_data())['gift_draft'];draft['max_uses']=limit or None
    await state.update_data(gift_draft=draft)
    await state.set_state(CodeState.expiry)
    await message.answer('متى تنتهي مهلة إدخال الكود؟\nأرسل تاريخًا بتوقيت دمشق: <code>2026-12-31 23:59</code>\nأو <code>0</code> بدون انتهاء.\nصلاحية النقاط بعد الشحن تحددها في الخطوة التالية.',parse_mode='HTML',reply_markup=cancel_kb())


@router.callback_query(CodeState.limit,F.data.startswith('ag:limit:'))
async def select_limit(call,state:FSMContext):
    await save_limit(call.message,state,int(call.data.split(':')[-1]));await call.answer()


@router.message(CodeState.limit)
async def type_limit(message,state:FSMContext):
    try:
        value=int(message.text or '')
        if not 0<=value<=1000000: raise ValueError()
    except ValueError:
        await message.answer('أرسل عددًا صحيحًا من 1 إلى 1000000، أو 0 بلا حد.');return
    await save_limit(message,state,value)


@router.message(CodeState.expiry)
async def expiry(message,state:FSMContext):
    try: value=parse_expiry(message.text or '')
    except ValueError:
        await message.answer('أرسل تاريخًا مستقبليًا بصيغة سنة-شهر-يوم ساعة:دقيقة، أو 0.');return
    draft=(await state.get_data())['gift_draft'];draft['redeem_expires_at']=value
    await state.update_data(gift_draft=draft);await state.set_state(CodeState.days)
    await message.answer('كم يومًا تبقى نقاط الهدية صالحة بعد إدخال الكود؟\nأرسل عددًا من 1 إلى 3650، أو 0 بدون انتهاء.',reply_markup=cancel_kb())


@router.message(CodeState.days)
async def days(message,state:FSMContext):
    try:
        value=int(message.text or '')
        if not 0<=value<=3650: raise ValueError()
    except ValueError:
        await message.answer('أرسل عدد أيام صحيحًا من 0 إلى 3650.');return
    draft=(await state.get_data())['gift_draft'];draft['gift_valid_days']=value
    await state.update_data(gift_draft=draft);await state.set_state(CodeState.confirm)
    await message.answer(code_text(draft)+'\n\nهل تريد إنشاء هذا الكود؟',parse_mode='HTML',reply_markup=kb([('✅ إنشاء',f"ag:create:{draft['id']}")],[('إلغاء','ag:home:0')]))


def code_text(c):
    return (f"🎟 <b>{escape(c['title'])}</b>\n<code>{c['code']}</code>\n"
            f"النقاط لكل طالب: {c['points']:g}\nالحد: {c.get('max_uses') or 'بلا حد'}\n"
            f"المستفيدون: {c.get('used_count',0)}\n"
            f"مهلة إدخال الكود: {expiry_text(c.get('redeem_expires_at'))}\n"
            f"صلاحية النقاط بعد الشحن: {str(c['gift_valid_days'])+' يوم' if c['gift_valid_days'] else 'بدون انتهاء'}\n"
            f"الحالة: {'مؤرشف' if c.get('archived_at') else 'هداياه ملغاة' if c.get('gifts_revoked_at') else 'متاح' if c.get('enabled',True) else 'موقوف'}")


@router.callback_query(CodeState.confirm,F.data.startswith('ag:create:'))
async def confirm(call,state:FSMContext):
    draft=(await state.get_data())['gift_draft']
    if call.data.split(':')[-1] != draft['id']:
        await call.answer('هذه شاشة تأكيد قديمة. استخدم أحدث شاشة.', show_alert=True)
        return
    row=await create_code(draft)
    await state.clear()
    await safe_edit_text(call.message,'✅ تم إنشاء الكود\n\n'+code_text(row),reply_markup=kb([('تفاصيل الكود',f"ag:view:{row['id']}")],[('الأكواد','ag:home:0')]))
    await call.answer()


@router.callback_query(F.data.startswith('ag:view:'))
async def view(call):
    code_id=call.data.split(':')[-1]
    response=await supabase.table('gift_codes').select('*').eq('id',code_id).limit(1).execute()
    if not response.data:
        await call.answer('الكود غير موجود أو تم حذفه.', show_alert=True)
        return
    c=response.data[0]
    ready = 'archived_at' in c
    buttons = [[('👥 المستفيدون',f'ag:users:{code_id}:0')]]
    if c.get('archived_at'):
        buttons.append([('↩️ استعادة من الأرشيف', f'ag:action:restore:{code_id}')])
    elif not c.get('gifts_revoked_at'):
        buttons.append([('⛔ إيقاف الكود' if c['enabled'] else '✅ تفعيل الكود',f"ag:toggle:{code_id}:{0 if c['enabled'] else 1}")])
        if ready:
            buttons.append([('🗂 أرشفة الكود', f'ag:action:archive:{code_id}')])
    elif ready:
        buttons.append([('🗂 أرشفة الكود', f'ag:action:archive:{code_id}')])
    if ready and not c.get('gifts_revoked_at'):
        buttons.append([('🚫 إلغاء هدايا الكود للجميع', f'ag:action:revoke_all:{code_id}')])
    if ready and not c['used_count']:
        buttons.append([('🗑 حذف الكود غير المستخدم', f'ag:action:delete:{code_id}')])
    buttons.append([('🔙 الأكواد','ag:home:0')])
    await safe_edit_text(call.message,code_text(c)+'\n\nإيقاف الكود وأرشفته يمنعان الشحن الجديد؛ الهدايا المشحونة تبقى بصلاحيتها.\nإلغاء هدايا الكود للجميع يلغي المتبقي منها، ولا يمس أرصدة أخرى.',reply_markup=kb(*buttons))
    await call.answer()


@router.callback_query(F.data.startswith('ag:toggle:'))
async def toggle(call):
    _,_,code_id,enabled=call.data.split(':')
    try:
        result = await supabase.rpc('admin_gift_code_action', {'p_code_id': code_id,
            'p_action': 'enable' if enabled == '1' else 'disable'}).execute()
    except Exception as exc:
        if not _missing_gift_admin_schema(exc):
            raise
        # Backwards-compatible until the additive migration is installed.
        await supabase.table('gift_codes').update({'enabled': enabled == '1'}).eq('id', code_id).execute()
        status = 'ok'
    else:
        status = result.data['status']
    if status != 'ok':
        await call.answer('تعذر التفعيل: الكود مؤرشف أو هداياه ملغاة أو لم يعد موجوداً.', show_alert=True)
        return
    await safe_edit_text(call.message,'✅ تم تحديث حالة الكود.',reply_markup=kb([('تفاصيل الكود',f'ag:view:{code_id}')]))
    await call.answer()


@router.callback_query(F.data.startswith('ag:users:'))
async def beneficiaries(call):
    _,_,code_id,page=call.data.split(':');page=max(0,int(page))
    result=await supabase.table('gift_code_redemptions').select('*',count='exact').eq('code_id',code_id).order('id').range(page*5,page*5+5).execute()
    rows=result.data or []
    text=f'👥 <b>المستفيدون: {result.count}</b>\n\n'
    text+='\n\n'.join(f"{escape(r['display_name'])} · @{escape(r.get('username') or '—')}\n"
                      f"المعرّف: <code>{r['user_id']}</code>\nالشحن: {expiry_text(r['redeemed_at'])}" for r in rows[:5]) or 'لا يوجد مستفيدون.'
    nav=[]
    if page:nav.append(('السابق',f'ag:users:{code_id}:{page-1}'))
    if len(rows)>5:nav.append(('التالي',f'ag:users:{code_id}:{page+1}'))
    buttons=[[('هدايا '+r['display_name'][:25],f"ag:gifts:{r['user_id']}:0")] for r in rows[:5]]
    if nav:buttons.append(nav)
    buttons.append([('الكود',f'ag:view:{code_id}')])
    await safe_edit_text(call.message,text,reply_markup=kb(*buttons));await call.answer()


@router.callback_query(F.data.startswith('ag:gifts:'))
async def student_gifts(call):
    _,_,uid,page=call.data.split(':');uid=int(uid);page=max(0,int(page))
    rows=await list_gifts(uid,page)
    text=f'🎁 <b>هدايا الطالب {uid}</b>\n\n'+('\n\n'.join(gift_text(g) for g in rows[:5]) or 'لا توجد هدايا.')
    buttons=[]
    for i,g in enumerate(rows[:5],1):
        if not g['revoked_at']:
            buttons.append([(f'✏️ صلاحية الهدية {i}',f"ag:edit:{g['id']}"),(f'إلغاء الهدية {i}',f"ag:revoke:{g['id']}")])
    nav=[]
    if page:nav.append(('السابق',f'ag:gifts:{uid}:{page-1}'))
    if len(rows)>5:nav.append(('التالي',f'ag:gifts:{uid}:{page+1}'))
    if nav:buttons.append(nav)
    buttons.append([('ملف الطالب',f'admin_user_profile_{uid}')])
    await safe_edit_text(call.message,text,reply_markup=kb(*buttons));await call.answer()


async def get_gift(gid):
    result=await supabase.table('point_gifts').select('*').eq('id',gid).single().execute()
    return result.data


@router.callback_query(F.data.startswith('ag:revoke:'))
async def revoke_prompt(call):
    gift=await get_gift(call.data.split(':')[-1])
    await safe_edit_text(call.message,'⚠️ هل تريد إلغاء الرصيد المتبقي من هذه الهدية؟\n\n'+gift_text(gift),reply_markup=kb(
        [('تأكيد الإلغاء',f"ag:revokeyes:{gift['id']}")],[('رجوع',f"ag:gifts:{gift['user_id']}:0")]))
    await call.answer()


@router.callback_query(F.data.startswith('ag:revokeyes:'))
async def revoke_confirm(call):
    gift=await get_gift(call.data.split(':')[-1])
    await supabase.rpc('admin_edit_point_gift',{'p_user_id':gift['user_id'],'p_gift_id':gift['id'],'p_action':'revoke'}).execute()
    await safe_edit_text(call.message,'✅ تم إلغاء الهدية.',reply_markup=kb([('هدايا الطالب',f"ag:gifts:{gift['user_id']}:0")]))
    await call.answer()


@router.callback_query(F.data.startswith('ag:edit:'))
async def edit_prompt(call,state:FSMContext):
    gift=await get_gift(call.data.split(':')[-1])
    await state.update_data(edit_gift=gift);await state.set_state(CodeState.gift_expiry)
    await call.message.answer('أرسل تاريخ انتهاء جديدًا بتوقيت دمشق: سنة-شهر-يوم ساعة:دقيقة\nأو 0 بدون انتهاء.',reply_markup=cancel_kb())
    await call.answer()


@router.message(CodeState.gift_expiry)
async def edit_expiry(message,state:FSMContext):
    try: expiry=parse_expiry(message.text or '')
    except ValueError:
        await message.answer('التاريخ غير صالح أو مضى. أعد المحاولة.');return
    gift=(await state.get_data())['edit_gift']
    result=await supabase.rpc('admin_edit_point_gift',{'p_user_id':gift['user_id'],'p_gift_id':gift['id'],'p_action':'expiry','p_expires_at':expiry}).execute()
    await state.clear()
    await message.answer('✅ تم تعديل الصلاحية.' if result.data else 'تعذر التعديل؛ ربما أُلغيت الهدية.',reply_markup=kb([('هدايا الطالب',f"ag:gifts:{gift['user_id']}:0")]))


@router.callback_query(F.data.startswith('ag:action:'))
@user_locked()
async def code_action_prompt(call, state: FSMContext):
    _, _, action, code_id = call.data.split(':')
    descriptions = {
        'archive': 'نقل الكود للأرشيف وإيقاف الشحن الجديد. تبقى نقاط المستفيدين صالحة.',
        'restore': 'إعادة الكود للقائمة الرئيسية مع إبقائه موقوفاً. التفعيل إجراء منفصل.',
        'delete': 'حذف نهائي لكود غير مستخدم فقط. سيرفض النظام الحذف إذا استُخدم قبل التأكيد.',
        'revoke_all': 'إيقاف الكود وإلغاء المتبقي من هداياه لدى جميع المستفيدين. تبقى المبالغ المستهلكة والسجلات محفوظة، ولا تتأثر أي أرصدة أخرى. لا يمكن إعادة تفعيل هذا الكود بعد الإلغاء.',
    }
    if action not in descriptions:
        return await call.answer('إجراء غير صالح.', show_alert=True)
    result = await supabase.table('gift_codes').select('*').eq('id', code_id).limit(1).execute()
    if not result.data:
        return await call.answer('الكود لم يعد موجوداً.', show_alert=True)
    if 'archived_at' not in result.data[0]:
        return await call.answer('هذا الإجراء بانتظار تحديث قاعدة بيانات هذه البيئة.', show_alert=True)
    token = secrets.token_hex(8)
    await state.update_data(gift_code_action={'token': token, 'action': action, 'id': code_id, 'created': time.time()})
    await state.set_state(CodeState.action_confirm)
    details = ''
    if action == 'revoke_all':
        summary = await supabase.rpc('admin_gift_code_summary', {'p_code_id': code_id}).execute()
        details = f"\n\nالمستفيدون ذوو الرصيد الصالح المتبقي: {summary.data['affected']}\nالنقاط المتبقية التي ستُلغى حالياً: {summary.data['remaining_points']:g}"
    await safe_edit_text(call.message, code_text(result.data[0]) + '\n\n⚠️ ' + descriptions[action] + details,
        reply_markup=kb([('✅ تأكيد الإجراء', f'ag:do:{token}')], [('🔙 رجوع', f'ag:view:{code_id}')]))
    await call.answer()


@router.callback_query(F.data.startswith('ag:do:'))
@user_locked()
async def code_action_confirm(call, state: FSMContext):
    action = (await state.get_data()).get('gift_code_action') or {}
    if (await state.get_state() != CodeState.action_confirm.state
            or action.get('token') != call.data.split(':')[-1]
            or time.time() - action.get('created', 0) > 300):
        return await call.answer('شاشة التأكيد قديمة أو انتهت مهلتها. أعد فتح الإجراء.', show_alert=True)
    result = await supabase.rpc('admin_gift_code_action', {'p_code_id': action['id'], 'p_action': action['action']}).execute()
    value = result.data
    messages = {'missing': 'الكود غير موجود أو سبق حذفه.', 'used': 'لا يمكن حذف كود مستخدم. يمكنك أرشفته.',
                'already_revoked': 'سبق إلغاء هدايا هذا الكود. لم يتغير أي رصيد مجدداً.',
                'cannot_enable': 'لا يمكن تفعيل هذا الكود.'}
    text = messages.get(value['status'], '✅ تم تنفيذ الإجراء.')
    if value['status'] == 'ok' and action['action'] == 'revoke_all':
        text = f"✅ أُلغيت الهدايا المتبقية للكود.\nالمستفيدون المتأثرون: {value['affected']}\nالنقاط الصالحة المتبقية التي أُلغيت: {value['remaining_points']:g}"
    await state.clear()
    await safe_edit_text(call.message, text, reply_markup=kb([('🔙 أكواد الهدايا', 'ag:home:0')]))
    await call.answer()


@router.callback_query(F.data == 'agc:new')
@user_locked()
async def campaign_new(call, state: FSMContext):
    try:
        await supabase.table('admin_gift_campaigns').select('id').limit(1).execute()
    except Exception as exc:
        if not _missing_gift_admin_schema(exc):
            raise
        return await call.answer('الحملات الجماعية بانتظار تحديث قاعدة بيانات هذه البيئة.', show_alert=True)
    await state.clear()
    await state.update_data(gift_campaign_draft={'id': str(uuid.uuid4()), 'created_by': call.from_user.id})
    await state.set_state(CodeState.campaign_title)
    await call.message.answer('🎁 منح هدية مباشرة لجميع المستخدمين الحاليين.\nاكتب اسم الهدية (حتى 100 حرف):', reply_markup=cancel_kb())
    await call.answer()


@router.message(CodeState.campaign_title, F.text)
async def campaign_title(message, state: FSMContext):
    value = message.text.strip()
    if not 1 <= len(value) <= 100:
        return await message.answer('اكتب اسماً بين حرف و100 حرف.')
    draft = (await state.get_data())['gift_campaign_draft']
    draft['title'] = value
    await state.update_data(gift_campaign_draft=draft)
    await state.set_state(CodeState.campaign_points)
    await message.answer('كم نقطة لكل طالب؟ من 0.01 إلى 100000، بمنزلتين عشريتين كحد أقصى.', reply_markup=cancel_kb())


@router.message(CodeState.campaign_points, F.text)
async def campaign_points(message, state: FSMContext):
    try:
        amount = Decimal(message.text.strip())
        if not amount.is_finite() or not 0 < amount <= 100000 or amount != round(amount, 2):
            raise ValueError()
    except (ValueError, InvalidOperation):
        return await message.answer('أدخل عدد نقاط صالحاً من 0.01 إلى 100000، بمنزلتين عشريتين كحد أقصى.')
    draft = (await state.get_data())['gift_campaign_draft']
    draft['points'] = str(amount)
    await state.update_data(gift_campaign_draft=draft)
    await state.set_state(CodeState.campaign_days)
    await message.answer('صلاحية الهدية بعد منحها: أرسل عدد أيام من 1 إلى 3650، أو 0 بدون انتهاء.', reply_markup=cancel_kb())


@router.message(CodeState.campaign_days, F.text)
async def campaign_days(message, state: FSMContext):
    try:
        days = int(message.text.strip())
        if not 0 <= days <= 3650:
            raise ValueError()
    except ValueError:
        return await message.answer('أرسل عدد أيام صحيحاً من 0 إلى 3650.')
    draft = (await state.get_data())['gift_campaign_draft']
    draft['valid_days'] = days
    await state.update_data(gift_campaign_draft=draft)
    await state.set_state(CodeState.campaign_message)
    await message.answer('أرسل الرسالة التي ستصل مع الهدية، كنص عادي حتى 2000 حرف. سنضيف النقاط والصلاحية وأزرار إنشاء الكويز تلقائياً.', reply_markup=cancel_kb())


@router.message(CodeState.campaign_message, F.text)
@user_locked()
async def campaign_message(message, state: FSMContext):
    value = message.text.strip()
    if not 1 <= len(value) <= 2000:
        return await message.answer('اكتب رسالة بين حرف و2000 حرف.')
    draft = (await state.get_data())['gift_campaign_draft']
    draft['message'] = value
    response = await supabase.rpc('admin_gift_campaign_prepare', {
        'p_id': draft['id'], 'p_created_by': draft['created_by'], 'p_title': draft['title'],
        'p_points': draft['points'], 'p_valid_days': draft['valid_days'], 'p_message': value}).execute()
    preview = response.data
    await state.update_data(gift_campaign_draft=draft)
    await state.set_state(CodeState.campaign_confirm)
    validity = f"{draft['valid_days']} يوم بعد المنح" if draft['valid_days'] else 'بدون انتهاء'
    text = (f"🎁 <b>معاينة هدية جماعية — لم يتم المنح بعد</b>\n\n<b>{escape(draft['title'])}</b>\n"
            f"النقاط لكل طالب: {escape(draft['points'])}\nالصلاحية: {validity}\n"
            f"المستهدفون الحاليون: {preview['recipients']}\nإجمالي النقاط: {preview['total_points']:g}\n\n"
            f"<b>الرسالة:</b>\n{escape(value)}\n\n"
            'المستفيدون ثابتون من هذه المعاينة؛ المسجلون لاحقاً غير مشمولين. تُضاف النقاط تلقائياً مرة واحدة. قد يتعذر تسليم الرسالة لمن حظر البوت. مهلة التأكيد 30 دقيقة.')
    buttons = []
    if preview['recipients']:
        buttons.append([('✅ تأكيد المنح والإرسال', f"agc:confirm:{draft['id']}")])
    buttons.append([('إلغاء', f"agc:cancel:{draft['id']}")])
    await message.answer(text, parse_mode='HTML', reply_markup=kb(*buttons))


@router.callback_query(F.data.startswith('agc:confirm:'))
@user_locked()
async def campaign_confirm(call, state: FSMContext):
    draft = (await state.get_data()).get('gift_campaign_draft') or {}
    campaign_id = call.data.split(':')[-1]
    if await state.get_state() != CodeState.campaign_confirm.state or draft.get('id') != campaign_id:
        return await call.answer('تأكيد قديم؛ راجع الحملة من قائمة التقارير.', show_alert=True)
    result = await supabase.rpc('admin_gift_campaign_confirm', {'p_id': campaign_id,
        'p_created_by': call.from_user.id, 'p_chat_id': call.message.chat.id, 'p_message_id': call.message.message_id}).execute()
    if result.data not in ('running', 'completed'):
        return await call.answer('لم تبدأ الحملة: انتهت مهلة المعاينة أو لم يعد هناك مستفيدون أو أُلغيت المسودة.', show_alert=True)
    await state.clear()
    await safe_edit_text(call.message, '⏳ تم تأكيد الحملة. يجري المنح والإرسال تلقائياً ويمكنك متابعة التقرير.',
                         reply_markup=campaign_report_buttons(campaign_id))
    await call.answer()


@router.callback_query(F.data.startswith('agc:cancel:'))
@user_locked()
async def campaign_cancel(call, state: FSMContext):
    campaign_id = call.data.split(':')[-1]
    draft = (await state.get_data()).get('gift_campaign_draft') or {}
    if draft.get('id') != campaign_id:
        return await call.answer('هذه مسودة قديمة.', show_alert=True)
    await supabase.table('admin_gift_campaigns').update({'status': 'cancelled'}).eq(
        'id', campaign_id).eq('created_by', call.from_user.id).eq('status', 'draft').execute()
    await state.clear()
    await safe_edit_text(call.message, 'تم إلغاء المعاينة. الحملة المؤكدة مسبقاً تستمر، ويمكن متابعتها من التقارير.', reply_markup=kb([('أكواد الهدايا', 'ag:home:0')]))
    await call.answer()


@router.callback_query(F.data.startswith('agc:list:'))
async def campaign_list(call):
    page = max(0, int(call.data.split(':')[-1]))
    result = await supabase.table('admin_gift_campaigns').select('*').order('created_at', desc=True).order('id').range(page*5, page*5+5).execute()
    rows = result.data or []
    labels = {'draft': 'مسودة', 'running': 'جارية', 'completed': 'مكتملة', 'cancelled': 'ملغاة'}
    buttons = [[(f"{c['title'][:30]} · {labels[c['status']]}", f"agc:view:{c['id']}")] for c in rows[:5]]
    nav = []
    if page:
        nav.append(('السابق', f'agc:list:{page-1}'))
    if len(rows) > 5:
        nav.append(('التالي', f'agc:list:{page+1}'))
    if nav:
        buttons.append(nav)
    buttons.append([('🔙 أكواد الهدايا', 'ag:home:0')])
    await safe_edit_text(call.message, '🎁 <b>حملات الهدايا وتقاريرها</b>\nاختر حملة لعرض تقدم المنح وتسليم الرسائل.', reply_markup=kb(*buttons))
    await call.answer()


@router.callback_query(F.data.startswith('agc:view:'))
async def campaign_view(call):
    result = await supabase.rpc('admin_gift_campaign_status', {'p_id': call.data.split(':')[-1]}).execute()
    if not result.data:
        return await call.answer('الحملة غير موجودة.', show_alert=True)
    await safe_edit_text(call.message, campaign_report(result.data), reply_markup=campaign_report_buttons(result.data['id']))
    await call.answer()
