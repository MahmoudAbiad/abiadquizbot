# services/quiz_engine.py
import asyncio
import json
import uuid
from typing import Dict, Any, Tuple, List, Optional
from config import bot, redis_client
from aiogram import types

from logger import get_logger, log_warning
from services.image_quiz_renderer import render_question_image, looks_arabic, letters_for
from services.latex_text import latex_to_plain
from supabase_helper import _is_valid_uuid, save_question_image_url, upload_quiz_question_image

logger = get_logger(__name__)

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

    # حقل explanation يُعرض داخل Telegram Poll الذي لا يدعم LaTeX إطلاقاً - نحوّله
    # لنص عادي مقروء أولاً (خط دفاع ثانٍ، آمن تماماً على أي نص لا يحوي LaTeX أصلاً)
    raw_exp = latex_to_plain(q.get("explanation") or "إجابة صحيحة!")
    clean_explanation = raw_exp if len(raw_exp) <= 200 else raw_exp[:197] + "..."

    return raw_question, clean_options, clean_explanation, needs_fallback

async def _send_math_image_question(chat_id: int, user_id: int, q: Dict[str, Any], idx: int, total: int, control_kb: types.InlineKeyboardMarkup, quiz_id: Optional[str]):
    """
    نمط الكويز المصوّر LaTeX: يرسم صورة واحدة للسؤال + الخيارات، ثم Poll منفصل
    يعرض فقط حروف الإجابة (أ/ب/ج/د أو A/B/C/D) لأن المحتوى الكامل موجود بالصورة.
    الصورة تُخزَّن مرة واحدة على Supabase Storage ويُعاد استخدام رابطها العام في
    كل مرة يُشغَّل فيها نفس الكويز (كاش)، بدل إعادة الرسم والرفع في كل مرة.
    """
    is_ar = looks_arabic(str(q.get("question", "")))
    options = q.get("options") or []
    letters = letters_for(is_ar, len(options))

    image_url = q.get("image_url")
    image_bytes = None
    if not image_url:
        image_bytes = await asyncio.to_thread(render_question_image, q, idx, total, is_ar)
        object_path = f"{quiz_id}/{idx}.png" if quiz_id and _is_valid_uuid(quiz_id) else f"tmp/{uuid.uuid4().hex}.png"
        image_url = await upload_quiz_question_image(image_bytes, object_path)
        if image_url and quiz_id and _is_valid_uuid(quiz_id):
            q["image_url"] = image_url  # يبقى بالذاكرة طوال الجلسة الحالية أيضاً
            asyncio.create_task(save_question_image_url(quiz_id, idx, image_url))

    caption = f"السؤال {idx + 1} من {total} 📝"
    try:
        if image_url:
            await bot.send_photo(chat_id=chat_id, photo=image_url, caption=caption)
        else:
            # فشل الرفع لسبب ما (مثال: الباكت غير مُهيّأ) - نرسل الصورة مباشرة كملف بدل رابط
            if image_bytes is None:
                image_bytes = await asyncio.to_thread(render_question_image, q, idx, total, is_ar)
            await bot.send_photo(chat_id=chat_id, photo=types.BufferedInputFile(image_bytes, filename="question.png"), caption=caption)
    except Exception as exc:
        log_warning(logger, f"Failed sending math question image, retrying with raw bytes: {exc}")
        if image_bytes is None:
            image_bytes = await asyncio.to_thread(render_question_image, q, idx, total, is_ar)
        await bot.send_photo(chat_id=chat_id, photo=types.BufferedInputFile(image_bytes, filename="question.png"), caption=caption)

    poll_question = "اختر الإجابة الصحيحة بالاعتماد على الصورة أعلاه 👆" if is_ar else "Choose the correct answer based on the image above 👆"
    # نمط الكويز المصوّر: explanation يُعرض بحقل Poll نصي عادي (وليس صورة)، لذا لازم
    # يتحوّل لنص عادي بالكامل قبل الإرسال - راجع services/latex_text.py
    raw_exp = latex_to_plain(q.get("explanation") or ("إجابة صحيحة!" if is_ar else "Correct answer!"))
    clean_exp = raw_exp if len(raw_exp) <= 200 else raw_exp[:197] + "..."

    poll_msg = await bot.send_poll(
        chat_id=chat_id,
        question=poll_question,
        options=letters,
        type="quiz",
        correct_option_id=int(q["correct_option_id"]),
        explanation=clean_exp,
        reply_markup=control_kb,
        is_anonymous=False,
    )

    quiz_data = {"chat_id": chat_id, "user_id": user_id, "correct_option_id": int(q["correct_option_id"]), "question_index": idx}
    await redis_client.set(f"poll:{poll_msg.poll.id}", json.dumps(quiz_data), ex=7200)
    return poll_msg


async def send_quiz_poll(chat_id: int, user_id: int, q: Dict[str, Any], idx: int, total: int, control_kb: types.InlineKeyboardMarkup, quiz_id: Optional[str] = None):
    """
    يقوم بإرسال السؤال كـ Poll أو Text Fallback وحفظ بيانات الجلسة في Redis.
    إذا كان السؤال مُعلَّماً بـ is_math (نمط الكويز المصوّر LaTeX)، يُحوَّل التنفيذ
    كاملاً لمسار الصورة + Poll الحروف بدل المسار النصي المعتاد.
    """
    if q.get("is_math"):
        return await _send_math_image_question(chat_id, user_id, q, idx, total, control_kb, quiz_id)

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
        "correct_option_id": int(q['correct_option_id']),
        "question_index": idx,
    }
    await redis_client.set(f"poll:{poll_msg.poll.id}", json.dumps(quiz_data), ex=7200)
    return poll_msg