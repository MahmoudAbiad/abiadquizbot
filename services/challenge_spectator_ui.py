"""Read-only spectator screens using the existing source-media delivery contract."""
import datetime as dt
from uuid import UUID
from services.challenge_live_ui import Btn
from services.challenge_presentation import PhotoText, source_image


_ARABIC_OPTIONS = ('أ', 'ب', 'ج', 'د', 'هـ', 'و', 'ز', 'ح')


def option_letter(index):
    return _ARABIC_OPTIONS[index] if isinstance(index, int) and 0 <= index < len(_ARABIC_OPTIONS) else '—'


def screen(state, count=None, page=0):
    mid = UUID(state['id']).hex
    labels = state['labels']
    heading = f"👁 مشاهدة الهجوم\n⚔️ {labels['attacker']} يهاجم برج {labels['subject']}\n📚 {labels['unit']}\n"
    rows = []
    if state['status'] != 'in_progress':
        status = {'completed':'اكتمل الهجوم','expired':'انتهت مهلة الهجوم','abandoned':'انسحب المهاجم'}.get(state['status'],'الهجوم غير جارٍ')
        r = state.get('result') or {}
        text = (heading + f"\n🏁 {status}\n✅ {r.get('correct',0)}/{state['total']} — النقاط: {r.get('score',0)}\n"
                f"{'⭐' * (r.get('stars') or 0) or 'دون نجوم'}\n"
                + ('🛡️ خسر دفاع أرضك' if r.get('attacker_won') else '🛡️ نجح دفاع أرضك')
                + f"\nمكافأة المهاجم: {r.get('attacker_xp',0)} XP — {r.get('attacker_trophies',0):+} كأس\n"
                'المشاهدة لا تمنح مكافآت ولا تخصم موارد من أرضك.')
        return text, [[Btn('↩️ أرضي','cg:home:0')]]
    q = state['question']
    now = dt.datetime.fromisoformat(state['server_now'].replace('Z','+00:00'))
    deadline = dt.datetime.fromisoformat(state['deadline'].replace('Z','+00:00'))
    remaining = max(0,int((deadline-now).total_seconds()))
    text = heading + f"\nالسؤال {state['index']+1} / {state['total']} — ⏱ حوالي {remaining} ثانية\n"
    text += f"النقاط للأسئلة المنتهية: {state['score']} — ✅ {state['correct']}\n"
    previous = state.get('previous')
    if previous:
        outcome = '⌛ لم يجب' if previous['timed_out'] else '✅ صحيحة' if previous['is_correct'] else '❌ خاطئة'
        text += f"السؤال السابق {previous['index']+1}: {outcome} (+{previous['score']})\n"
        chosen = 'لم يجب' if previous['timed_out'] else option_letter(previous.get('option_id'))
        text += f"اختار المهاجم: {chosen} — الإجابة الصحيحة: {option_letter(previous.get('correct_option_id'))}\n"
    image = source_image(q)
    # Source images already contain the question/options. Keep their caption compact.
    # Text-only questions retain their prompt/options so letter-only buttons remain usable.
    body = '' if image else q['text'] + '\n\n' + '\n'.join(
        f'{option_letter(i)}) {option}' for i, option in enumerate(q['options']))
    options = [Btn(option_letter(i), 'sp:readonly')
               for i, option in enumerate(q['options'])]
    # Telegram lays out keyboard columns left-to-right: reverse each pair visually,
    # while keeping the original option indices and their Arabic letters unchanged.
    rows.extend(list(reversed(options[i:i+2])) for i in range(0, len(options), 2))
    size = max(80, (950 if image else 3900) - len(text) - 160)
    chunks = [body[i:i+size] for i in range(0,len(body),size)] or ['']
    page = min(max(0,page),len(chunks)-1)
    text += '\n' + chunks[page]
    text += f"\n\n👁 جلسات مشاهدة حديثة: {count}" if count is not None else '\n\n👁 الحضور غير متاح مؤقتًا'
    text += '\nالحضور لمدة 60 ثانية من آخر ضغطة؛ اضغط تحديث لمتابعة المشاهدة.'
    if len(chunks)>1:
        nav = []
        if page: nav.append(Btn('السابق',f'sp:p:{mid}:{state["index"]}:{page-1}'))
        if page+1<len(chunks): nav.append(Btn('التالي',f'sp:p:{mid}:{state["index"]}:{page+1}'))
        rows.append(nav)
    rows.extend([[Btn('🔄 تحديث',f'sp:r:{mid}')],
                 [Btn('❌ إيقاف المشاهدة',f'sp:x:{mid}'),Btn('↩️ أرضي',f'sp:x:{mid}')]])
    return (PhotoText(text,image) if image else text),rows
