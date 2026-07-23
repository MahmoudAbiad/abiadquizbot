# services/quiz_engine.py
import json
from typing import Dict, Any, Tuple, List
from config import bot, redis_client
from aiogram import types

def prepare_question_payload(q: Dict[str, Any], idx: int, total: int) -> Tuple[str, List[str], str, bool]:
    """
    تأخذ السؤال وتتحقق من حد أطوال التليجرام لتقرير هل تحتاج Text Fallback أم لا
    """
    raw_question = f"📝 السؤال {idx + 1} من {total}:\n{q['question']}"
    needs_fallback = len(raw_question) > 300
    clean_options = []

    for opt in q['options']:
        opt_str = str(opt).strip()
        if len(opt_str) > 100:
            needs_fallback = True
        clean_options.append(opt_str if len(opt_str) <= 100 else opt_str[:97] + "...")

    raw_exp = q.get("explanation") or "إجابة صحيحة!"
    clean_explanation = raw_exp if len(raw_exp) <= 200 else raw_exp[:197] + "..."

    return raw_question, clean_options, clean_explanation, needs_fallback

async def send_quiz_poll(chat_id: int, user_id: int, q: Dict[str, Any], idx: int, total: int, control_kb: types.InlineKeyboardMarkup):
    """
    يقوم بإرسال السؤال كـ Poll أو Text Fallback وحفظ بيانات الجلسة في Redis
    """
    raw_q, clean_opts, clean_exp, needs_fallback = prepare_question_payload(q, idx, total)

    if needs_fallback:
        full_text = f"📝 **السؤال {idx + 1} من {total}:**\n{q['question']}\n\n"
        poll_options = []
        for i, opt in enumerate(q['options'], 1):
            full_text += f"**{i}.** {str(opt).strip()}\n"
            poll_options.append(f"الخيار رقم {i}")

        await bot.send_message(chat_id=chat_id, text=full_text, parse_mode="Markdown")
        clean_q = "اختر الإجابة الصحيحة بناءً على التفاصيل أعلاه 👆:"
        clean_opts = poll_options
    else:
        clean_q = raw_q

    poll_msg = await bot.send_poll(
        chat_id=chat_id,
        question=clean_q,
        options=clean_opts,
        type="quiz",
        correct_option_id=int(q['correct_option_id']),
        explanation=clean_exp,
        reply_markup=control_kb,
        is_anonymous=False
    )

    # حفظ حالة الـ Poll في Redis
    quiz_data = {
        "chat_id": chat_id,
        "user_id": user_id,
        "correct_option_id": int(q['correct_option_id'])
    }
    await redis_client.set(f"poll:{poll_msg.poll.id}", json.dumps(quiz_data), ex=7200)
    return poll_msg