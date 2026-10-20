"""Issue user-bound launch links only after checking server-side quiz access."""
from urllib.parse import urlencode
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from services.runner_core import enabled, sign_launch, build_questions
from services.runner_static import RUNNER_ASSET_VERSION

router = Router()
router.callback_query.filter(F.message.chat.type == 'private')


def launch_keyboard(quiz_id, user_id):
    from config import bot
    from constants import WEBAPP_PUBLIC_BASE_URL
    if not enabled() or not WEBAPP_PUBLIC_BASE_URL:
        return None
    query = urlencode({'quiz_id':str(quiz_id), 'launch':sign_launch(quiz_id,user_id,bot.token), 'v':RUNNER_ASSET_VERSION})
    return types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(
        text='🏃 افتح رحلة أبيض', web_app=types.WebAppInfo(url=WEBAPP_PUBLIC_BASE_URL+'/webapp/runner/index.html?'+query))]])


@router.callback_query(F.data.startswith('runner:'))
async def launch(call: types.CallbackQuery, state: FSMContext):
    if not enabled():
        await call.answer('اللعبة لم تُفعّل على بيئة الاختبار بعد.',show_alert=True)
        return
    # Serialize centralization of legacy text quizzes using the existing user lock.
    await _launch_locked(call, state)


# Same distributed/per-user guard used by the existing quiz workflows.
from services.redis_reliability import user_locked


@user_locked()
async def _launch_locked(call, state):
    from helpers.supabase_helper import supabase
    from services.quiz_permissions import can_manage_quiz
    from helpers.supabase_helper.quiz_cache import save_file_quiz_multiple
    uid = call.from_user.id
    try:
        target = call.data.split(':',1)[1]
        data = await state.get_data()
        if target == 'current':
            questions = data.get('questions') or []
            build_questions(questions)  # Reject ineligible input before persistence.
            qid = data.get('quiz_id')
            if not qid:
                qid = await save_file_quiz_multiple(None,uid,data.get('source_title') or 'اختبار نصي',questions,0,
                    is_math_quiz=any(q.get('is_math') for q in questions))
                if not qid:
                    raise ValueError('تعذر حفظ الاختبار؛ حاول مجدداً.')
                await state.update_data(quiz_id=qid)
            # Current FSM data belongs to this authenticated Telegram user.
        else:
            from uuid import UUID
            qid = str(UUID(target))
            rows = (await supabase.table('quizzes').select('creator_id,quiz_data').eq('id',qid).execute()).data
            if not rows:
                raise ValueError('الاختبار غير موجود.')
            favorite = await supabase.table('favorite_quizzes').select('favorite_id').eq('user_id',uid).eq('quiz_id',qid).limit(1).execute()
            if not can_manage_quiz(uid,rows[0]['creator_id']) and not favorite.data:
                raise ValueError('لا تملك حق الوصول لهذا الاختبار.')
            build_questions(rows[0]['quiz_data'])
        keyboard = launch_keyboard(qid,uid)
        if keyboard:
            await call.message.answer('🏃 رحلة أبيض: اسحب لتغيير المسار، واسحب للأعلى للقفز. اختر مسار الإجابة ثم ثبّته قبل انتهاء الوقت.',reply_markup=keyboard)
        await call.answer()
    except ValueError as exc:
        await call.answer(str(exc),show_alert=True)
    except Exception:
        await call.answer('تعذر تجهيز اللعبة حالياً. حاول لاحقاً.',show_alert=True)
