# services/quiz_engine.py
import json
import re
from typing import Dict, Any, Tuple, List, Optional
from config import bot, redis_client
from aiogram import types
from aiogram.fsm.context import FSMContext

from services.math_renderer import render_question_image
from utils import safe_file_cleanup


def _clean_poll_text(text: str) -> str:
    """
    تنظيف نصوص الاستطلاعات النصية في تلغرام وتحويل رموز LaTeX الخام إلى Unicode مقروء.
    """
    if not text:
        return ""

    # 1. إزالة أقواس LaTeX الخام \( \) و \[ \]
    cleaned = re.sub(r'\\\(|\\\)|\\\[|\\\]', '', text)

    # 2. استبدال الرموز اليونانية الشائعة
    cleaned = cleaned.replace(r'\eta', 'η').replace('eta', 'η')

    # 3. تحويل الأسس الشائعة إلى Unicode للظهور المنسق في التلغرام
    superscripts = {
        '^-1': '⁻¹', '^-2': '⁻²', '^-3': '⁻³',
        '^1': '¹', '^2': '²', '^3': '³', '^0': '⁰'
    }
    for key, val in superscripts.items():
        cleaned = cleaned.replace(key, val)

    return cleaned


def prepare_question_payload(q: Dict[str, Any], idx: int, total: int) -> Tuple[str, List[str], str, bool]:
    """
    تأخذ السؤال وتتحقق من حد أطوال التليجرام لتقرير هل تحتاج Text Fallback أم لا
    مع تنظيف نصوص LaTeX الخام لتبدو مقروءة.
    """
    raw_q_text = _clean_poll_text(q.get('question', ''))
    raw_question = f"📝 السؤال {idx + 1} من {total}:\n{raw_q_text}"
    needs_fallback = len(raw_question) > 300
    clean_options = []

    for opt in q.get('options', []):
        opt_str = _clean_poll_text(str(opt).strip())
        if len(opt_str) > 100:
            needs_fallback = True
        clean_options.append(opt_str if len(opt_str) <= 100 else opt_str[:97] + "...")

    raw_exp = _clean_poll_text(q.get("explanation") or "إجابة صحيحة!")
    clean_explanation = raw_exp if len(raw_exp) <= 200 else raw_exp[:197] + "..."

    return raw_question, clean_options, clean_explanation, needs_fallback


async def send_quiz_poll(
    chat_id: int,
    user_id: int,
    q: Dict[str, Any],
    idx: int,
    total: int,
    control_kb: types.InlineKeyboardMarkup,
    content_type: str = "TEXT",
    state: Optional[FSMContext] = None
):
    """
    يقوم بإرسال السؤال كـ Poll عادي أو كـ صورة + Poll أحرف (إذا كان المحتوى MATH)
    مع حفظ بيانات الجلسة وحذف صورة السؤال السابق تلقائياً.
    """
    # ==================== المسار الأول: أسئلة الرياضيات (MATH) ====================
    if content_type == "MATH":
        # 0. حذف صورة السؤال السابق لمنع تراكم الصور في المحادثة
        if state:
            st_data = await state.get_data()
            prev_photo_id = st_data.get("last_photo_message_id")
            if prev_photo_id:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=prev_photo_id)
                except Exception:
                    pass
                await state.update_data(last_photo_message_id=None)

        # 1. توليد صورة السؤال والخيارات المنفذة بالـ LaTeX
        image_path = await render_question_image(q, idx + 1, total)

        # 2. إرسال الصورة إلى الشات
        photo_file = types.FSInputFile(image_path)
        photo_msg = await bot.send_photo(
            chat_id=chat_id,
            photo=photo_file,
            caption=f"📐 **السؤال {idx + 1} من {total}**"
        )

        # حفظ معرّف الرسالة لتنظيفها عند السؤال التالي
        if state:
            await state.update_data(last_photo_message_id=photo_msg.message_id)

        # 3. إعداد استطلاع بأحرف الخيارات فقط
        labels = ["أ", "ب", "ج", "د"]
        math_options = labels[:len(q.get("options", []))]

        raw_exp = _clean_poll_text(q.get("explanation") or "إجابة صحيحة!")
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
            full_text = f"📝 **السؤال {idx + 1} من {total}:**\n{_clean_poll_text(q.get('question', ''))}\n\n"
            poll_options = []
            for i, opt in enumerate(q.get('options', []), 1):
                opt_cleaned = _clean_poll_text(str(opt).strip())
                full_text += f"**{i}.** {opt_cleaned}\n"
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