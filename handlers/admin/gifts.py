"""Admin code wizard, beneficiary pagination and individual gift controls."""
import math
import uuid
from html import escape
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from helpers.supabase_helper._client import supabase
from helpers.supabase_helper.gifts import new_code, create_code, list_gifts
from services.gift_presentation import parse_expiry, expiry_text, gift_text
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


def kb(*rows):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text=t,callback_data=d) for t,d in row] for row in rows])


def cancel_kb():
    return kb([('إلغاء','ag:home:0')])


@router.callback_query(F.data.startswith('ag:home:'))
async def codes_home(call, state: FSMContext):
    await state.clear()
    page=max(0,int(call.data.split(':')[-1]))
    response=await supabase.table('gift_codes').select('*').order('created_at',desc=True).order('id').range(page*5,page*5+5).execute()
    rows=response.data or []
    buttons=[[('➕ إنشاء كود','ag:new')]]
    for c in rows[:5]:
        buttons.append([(f"{'✅' if c['enabled'] else '⛔'} {c['title'][:35]} · {c['used_count']} مستفيد",f"ag:view:{c['id']}")])
    nav=[]
    if page: nav.append(('السابق',f'ag:home:{page-1}'))
    if len(rows)>5: nav.append(('التالي',f'ag:home:{page+1}'))
    if nav: buttons.append(nav)
    buttons.append([('الإعدادات العامة','admin_settings_general')])
    await safe_edit_text(call.message,'🎟 <b>أكواد الهدايا</b>\nكل طالب يستخدم الكود مرة واحدة.\nاختر كودًا لمراجعة المستفيدين أو إيقافه.\n\nلإدارة هدايا طالب، افتح ملفه من إدارة المستخدمين.',reply_markup=kb(*buttons))
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
            f"الحالة: {'متاح' if c.get('enabled',True) else 'موقوف'}")


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
    response=await supabase.table('gift_codes').select('*').eq('id',code_id).single().execute()
    c=response.data
    await safe_edit_text(call.message,code_text(c)+'\n\nإيقاف الكود يمنع الشحن الجديد؛ الهدايا المشحونة تبقى بصلاحيتها.',reply_markup=kb(
        [('👥 المستفيدون',f'ag:users:{code_id}:0')],
        [('⛔ إيقاف الكود' if c['enabled'] else '✅ تفعيل الكود',f"ag:toggle:{code_id}:{0 if c['enabled'] else 1}")],
        [('الأكواد','ag:home:0')]))
    await call.answer()


@router.callback_query(F.data.startswith('ag:toggle:'))
async def toggle(call):
    _,_,code_id,enabled=call.data.split(':')
    await supabase.table('gift_codes').update({'enabled':enabled=='1'}).eq('id',code_id).execute()
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
