# services/quiz_engine.py
import json
from typing import Dict, Any, Tuple, List
from config import bot, redis_client
from aiogram import types

from services.math_renderer import render_question_image
from utils import safe_file_cleanup

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

async def send_quiz_poll(
    chat_id: int,
    user_id: int,
    q: Dict[str, Any],
    idx: int,
    total: int,
    control_kb: types.InlineKeyboardMarkup,
    content_type: str = "TEXT"
):
    """
    يقوم بإرسال السؤال كـ Poll عادي أو كـ صورة + Poll أحرف (إذا كان المحتوى MATH)
    مع حفظ بيانات الجلسة في Redis.
    """
    # ==================== المسار الأول: أسئلة الرياضيات (MATH) ====================
    if content_type == "MATH":
        # 1. توليد صورة السؤال والخيارات المنفذة بالـ LaTeX
        image_path = await render_question_image(q, idx + 1, total)

        # 2. إرسال الصورة إلى الشات
        photo_file = types.FSInputFile(image_path)
        await bot.send_photo(
            chat_id=chat_id,
            photo=photo_file,
            caption=f"📐 **السؤال {idx + 1} من {total}**"
        )

        # 3. إعداد استطلاع بأحرف الخيارات فقط
        labels = ["أ", "ب", "ج", "د"]
        math_options = labels[:len(q.get("options", []))]

        raw_exp = q.get("explanation") or "إجابة صحيحة!"
        clean_exp = raw_exp if len(raw_exp) <= 200 else raw_exp[:197] + "..."

        poll_msg = await bot.send_poll(
            chat_id=chat_id,
            question="اختر الإجابة الصحيحة من الصورة أعلاه 👆:",
            options=math_options,
            type="quiz",
            correct_option_id=int(q['correct_option_id']),
            explanation=clean_exp,
            reply_markup=control_kb,
            is_anonymous=False
        )

        # 4. تنظيف الصورة المحلية بعد إرسالها لتوفير المساحة
        safe_file_cleanup(image_path)

    # ==================== المسار الثاني: الأسئلة النصية العادية (TEXT) ====================
    else:
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

    # حفظ حالة الـ Poll في Redis لمطابقة الإجابات
    quiz_data = {
        "chat_id": chat_id,
        "user_id": user_id,
        "correct_option_id": int(q['correct_option_id'])
    }
    await redis_client.set(f"poll:{poll_msg.poll.id}", json.dumps(quiz_data), ex=7200)
    return poll_msg