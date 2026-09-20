# services/quiz_engine.py
import asyncio
import hashlib
import json
import uuid
from typing import Dict, Any, Tuple, List, Optional
from config import bot, redis_client
from aiogram import types

from logger import get_logger, log_warning
# 🩹 FIX (memory-leak): render_question_image_async يفرض RENDER_SEMAPHORE (سقف
# تزامن الرسم) بدل استدعاء asyncio.to_thread(render_question_image, ...) مباشرة -
# راجع services/image_quiz_renderer.py للتفاصيل.
from services.image_quiz_renderer import render_question_image_async, looks_arabic, letters_for
from services.latex_text import latex_to_plain
from supabase_helper import _is_valid_uuid, save_question_image_url, upload_quiz_question_image

logger = get_logger(__name__)


def _question_image_object_path(quiz_id: Optional[str], idx: int, q: Dict[str, Any]) -> str:
    """ينشئ مسار صورة فريد لكل نسخة من السؤال، حتى لو نفس السؤال كتب مرة ثانية؛ هذا
    يمنع Telegram من إعادة استخدام الصورة القديمة نفسها لأن عنوان URL يظل ثابتاً."""
    if quiz_id and _is_valid_uuid(quiz_id):
        payload = json.dumps(q, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
        return f"{quiz_id}/{idx}-{digest}.png"
    return f"tmp/{uuid.uuid4().hex}.png"


# ==================== المؤقّت الداخلي للسؤال (open_period) ====================
# تيليجرام بيقبل open_period بين 5 و600 ثانية حصراً، وأي قيمة برّا المدى بترجع
# BadRequest وبتُسقط السؤال كامل. بما إنه القيمة جاية من كيبورد (ممكن يوصلها
# callback_data معدّل يدوياً)، منحصرها هون بمكان واحد بدل ما نثق بالواجهة.
# القيمة None (الافتراضية) = بلا مؤقّت إطلاقاً = السلوك الحالي للنمط الفردي.
TELEGRAM_MIN_OPEN_PERIOD = 5
TELEGRAM_MAX_OPEN_PERIOD = 600


def _sanitize_open_period(open_period: Optional[int]) -> Optional[int]:
    """يرجّع قيمة open_period صالحة لتيليجرام، أو None لو ما في مؤقّت/القيمة تالفة."""
    if open_period is None:
        return None
    try:
        value = int(open_period)
    except (TypeError, ValueError):
        log_warning(logger, f"Invalid open_period value ignored: {open_period!r}")
        return None
    if value <= 0:
        return None
    clamped = max(TELEGRAM_MIN_OPEN_PERIOD, min(TELEGRAM_MAX_OPEN_PERIOD, value))
    if clamped != value:
        log_warning(logger, f"open_period {value}s out of Telegram range, clamped to {clamped}s")
    return clamped


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

async def _send_math_image_question(
    chat_id: int,
    user_id: int,
    q: Dict[str, Any],
    idx: int,
    total: int,
    control_kb: types.InlineKeyboardMarkup,
    quiz_id: Optional[str],
    open_period: Optional[int] = None,
    poll_meta: Optional[Dict[str, Any]] = None,
    is_anonymous: bool = False,
    message_thread_id: Optional[int] = None,
):
    """
    نمط الكويز المصوّر LaTeX: يرسم صورة واحدة للسؤال + الخيارات، ثم Poll منفصل
    يعرض فقط حروف الإجابة (أ/ب/ج/د أو A/B/C/D) لأن المحتوى الكامل موجود بالصورة.
    الصورة تُخزَّن مرة واحدة على Supabase Storage ويُعاد استخدام رابطها العام في
    كل مرة يُشغَّل فيها نفس الكويز (كاش)، بدل إعادة الرسم والرفع في كل مرة.

    ⚠️ ملاحظة على `open_period` بهالمسار تحديداً: نمط الرياضيات = **رسالتين**
    (صورة + poll)، والمؤقّت بيبدأ لحظة إرسال الـ poll (الرسالة التانية) مش
    الصورة. برضو هاد النمط بيستهلك ضعف حصّة الـ rate limit للغروب - يُحسب
    عند اختيار الفاصل الزمني بمسار الغروب.
    """
    is_ar = looks_arabic(str(q.get("question", "")))
    options = q.get("options") or []
    letters = letters_for(is_ar, len(options))

    image_url = q.get("image_url")
    image_bytes = None
    if not image_url:
        image_bytes = await render_question_image_async(q, idx, total, is_ar)
        object_path = _question_image_object_path(quiz_id, idx, q)
        image_url = await upload_quiz_question_image(image_bytes, object_path)
        if image_url and quiz_id and _is_valid_uuid(quiz_id):
            q["image_url"] = image_url  # يبقى بالذاكرة طوال الجلسة الحالية أيضاً
            asyncio.create_task(save_question_image_url(quiz_id, idx, image_url))

    caption = f"السؤال {idx + 1} من {total} 📝"
    try:
        if image_url:
            await bot.send_photo(chat_id=chat_id, photo=image_url, caption=caption, message_thread_id=message_thread_id)
        else:
            # فشل الرفع لسبب ما (مثال: الباكت غير مُهيّأ) - نرسل الصورة مباشرة كملف بدل رابط
            if image_bytes is None:
                image_bytes = await render_question_image_async(q, idx, total, is_ar)
            await bot.send_photo(chat_id=chat_id, photo=types.BufferedInputFile(image_bytes, filename="question.png"), caption=caption, message_thread_id=message_thread_id)
    except Exception as exc:
        log_warning(logger, f"Failed sending math question image, retrying with raw bytes: {exc}")
        if image_bytes is None:
            image_bytes = await render_question_image_async(q, idx, total, is_ar)
        await bot.send_photo(chat_id=chat_id, photo=types.BufferedInputFile(image_bytes, filename="question.png"), caption=caption, message_thread_id=message_thread_id)

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
        is_anonymous=is_anonymous,
        open_period=_sanitize_open_period(open_period),
        message_thread_id=message_thread_id,
    )

    quiz_data = {"chat_id": chat_id, "user_id": user_id, "correct_option_id": int(q["correct_option_id"]), "question_index": idx}
    if poll_meta:
        quiz_data.update(poll_meta)
    await redis_client.set(f"poll:{poll_msg.poll.id}", json.dumps(quiz_data), ex=7200)
    return poll_msg


async def send_quiz_poll(
    chat_id: int,
    user_id: int,
    q: Dict[str, Any],
    idx: int,
    total: int,
    control_kb: types.InlineKeyboardMarkup,
    quiz_id: Optional[str] = None,
    open_period: Optional[int] = None,
    poll_meta: Optional[Dict[str, Any]] = None,
    is_anonymous: bool = False,
    message_thread_id: Optional[int] = None,
):
    """
    يقوم بإرسال السؤال كـ Poll أو Text Fallback وحفظ بيانات الجلسة في Redis.
    إذا كان السؤال مُعلَّماً بـ is_math (نمط الكويز المصوّر LaTeX)، يُحوَّل التنفيذ
    كاملاً لمسار الصورة + Poll الحروف بدل المسار النصي المعتاد.

    البارامترات الاختيارية (لمسار الغروب فقط - النمط الفردي ما بيمرّرها إطلاقاً
    فسلوكه مطابق تماماً للسابق، صفر أثر جانبي):

    - `open_period`: المؤقّت الداخلي للسؤال بالثواني (5-600). يُمرَّر وقت الإرسال
      فقط ولا يُخزَّن بالكويز نفسه (`quizzes.quiz_data`).
    - `poll_meta`: حقول إضافية تُدمج بسجل `poll:{poll_id}` بـ Redis (مثلاً
      `session_id` و`question_index` بمسار الغروب). سبب وجوده: منتجنّب إنه
      المستدعي يكتب فوق نفس المفتاح بكتابة تانية بعد الإرسال - بينها نافذة
      زمنية صغيرة ممكن يوصل فيها `poll_answer` ويُقرأ سجل ناقص.
    - `is_anonymous`: **True تعطّل تتبّع الإجابات بالكامل** - تيليجرام ما بيبعت
      `poll_answer` إطلاقاً للاستفتاءات المجهولة (موثّق رسمياً: "A user changed
      their answer in a non-anonymous poll")، فمحدا رح يوصله أي تحديث لهالسؤال.
      الافتراضي False = نفس سلوك النمط الفردي بالضبط.
    - `message_thread_id`: توبيك الغروب (Forum supergroup) يلي لازم السؤال يظهر
      فيه، إن وجد. `None` = بلا تغيير (سلوك النمط الفردي القديم بالضبط - يظهر
      بالغروب عادي بلا استهداف توبيك معيّن).
    """
    if q.get("is_math"):
        return await _send_math_image_question(
            chat_id, user_id, q, idx, total, control_kb, quiz_id,
            open_period=open_period, poll_meta=poll_meta, is_anonymous=is_anonymous,
            message_thread_id=message_thread_id,
        )

    raw_q, clean_opts, clean_exp, needs_fallback = prepare_question_payload(q, idx, total)

    if needs_fallback:
        full_text = f"📝 **السؤال {idx + 1} من {total}:**\n{q['question']}\n\n"
        poll_options = []
        for i, opt in enumerate(q['options'], 1):
            full_text += f"**{i}.** {str(opt).strip()}\n"
            poll_options.append(f"الخيار رقم {i}")

        await bot.send_message(chat_id=chat_id, text=full_text, parse_mode="Markdown", message_thread_id=message_thread_id)
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
        is_anonymous=is_anonymous,
        open_period=_sanitize_open_period(open_period),
        message_thread_id=message_thread_id,
    )

    # حفظ حالة الـ Poll في Redis
    quiz_data = {
        "chat_id": chat_id,
        "user_id": user_id,
        "correct_option_id": int(q['correct_option_id']),
        "question_index": idx,
    }
    if poll_meta:
        quiz_data.update(poll_meta)
    await redis_client.set(f"poll:{poll_msg.poll.id}", json.dumps(quiz_data), ex=7200)
    return poll_msg