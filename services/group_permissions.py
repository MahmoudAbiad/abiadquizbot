# services/group_permissions.py
"""
==============================================================================
MODULE: صلاحية "أدمن/مالك الغروب" - بوابة بدء وإدارة الجلسات الجماعية
==============================================================================
موديول مستقل تماماً (صفر تبعيات على باقي منطق البوت) يجاوب على سؤال واحد فقط:

    هل هذا المستخدم أدمن (أو مالك) بهذا الغروب الآن؟

هاي هي الصلاحية الوحيدة المطلوبة لبدء جلسة كويز جماعية أو إنهائها - لا يوجد
مفهوم "معلّم" منفصل بقاعدة البيانات ولا جدول/عمود "دور" جديد إطلاقاً
(قرار معماري محسوم). المصدر الوحيد للحقيقة هو تيليجرام نفسه عبر
`bot.get_chat_member`.

**لماذا ملف منفصل عن `quiz_permissions.py`؟**
لأن ذاك الملف موثّق ومحصور أصلاً بصلاحيات "إدارة الكويز" (حذف/تعديل) وهي
صلاحية محلية بحتة (أدمن البوت أو مالك الكويز) بلا أي نداء شبكة. خلط فحص
صلاحية الغروب فيه بيكرر نفس مشكلة `supabase_helper.py` (god-file).

**لماذا كاش Redis؟**
`get_chat_member` نداء شبكة لتيليجرام، وبيُستدعى بكل تفاعل بمسار الغروب
(بدء الجلسة، زر الإنهاء، كل ضغطة على كيبورد الإعدادات...). بلا كاش منستهلك
حصّة الـ API بلا داعٍ ومنضيف زمن انتظار على كل ضغطة. TTL = 5 دقايق:
قصير كفاية إنه سحب صلاحية أدمن ينعكس بسرعة، وطويل كفاية إنه يغطي جلسة
إعداد كاملة بنداء واحد. Redis (مش كاش داخل العملية) لأنه البوت بيشتغل
بأكتر من نسخة/عملية مع بعض.
"""

from typing import Any, Optional

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError

from config import bot, redis_client
from logger import get_logger, log_error

logger = get_logger(__name__)

# حالات العضوية اللي بتعتبر "أدمن" - `creator` هو مالك الغروب و`administrator`
# أي أدمن عيّنه المالك. أي حالة تانية (member/restricted/left/kicked) = لأ.
ADMIN_STATUSES = frozenset({"administrator", "creator"})

_CACHE_PREFIX = "chat_admin:"
CACHE_TTL = 300  # 5 دقائق

# حساب تيليجرام الوهمي اللي بتوصل باسمه رسائل الأدمن المجهول (Anonymous Admin).
# منرجّع False بهالحالة عن قصد: تقنياً هو أدمن فعلاً، بس ما عنا user_id حقيقي
# نربط فيه الجلسة (`group_quiz_sessions.started_by` عندها FK على
# `users.user_id`)، ولا منقدر نميّز أي أدمن هو لو صار خلاف على الإنهاء.
# المعلّم لازم يبدأ الجلسة باسمه الحقيقي (يطفّي وضع "إرسال كأدمن مجهول").
GROUP_ANONYMOUS_BOT_ID = 1087968824


def _cache_key(chat_id: int, user_id: int) -> str:
    return f"{_CACHE_PREFIX}{chat_id}:{user_id}"


def _decode_cached(raw: Any) -> bool:
    """قيم Redis بترجع bytes بهالمشروع (ما في decode_responses بالإعداد)، فمنوحّد
    المقارنة على نص - نفس نمط `_decode_flag_value` بـ supabase_helper.py."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    return str(raw) == "1"


async def is_group_admin(chat_id: int, user_id: Optional[int]) -> bool:
    """يتحقق إذا كان `user_id` أدمن أو مالك بالغروب `chat_id`.

    fail-closed: أي غموض (مستخدم مجهول، خطأ شبكة، البوت مطرود من الغروب)
    بيرجّع False - لأنه هاي بوابة صلاحية، والرفض الخاطئ أرخص بكتير من
    السماح الخاطئ (أي عضو عادي يفجّر جلسة بغروب مش إله).
    """
    if not user_id:
        return False
    if user_id == GROUP_ANONYMOUS_BOT_ID:
        return False

    key = _cache_key(chat_id, user_id)

    # 1) الكاش أولاً - أي عطل بـ Redis ما بيمنع الفحص، منكمل لتيليجرام مباشرة.
    try:
        cached = await redis_client.get(key)
        if cached is not None:
            return _decode_cached(cached)
    except Exception as e:
        log_error(logger, f"Redis error reading admin cache for {chat_id}:{user_id} (falling back to Telegram): {e}")

    # 2) المصدر الحقيقي: تيليجرام.
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        # `member.status` بـ aiogram 3 هو ChatMemberStatus (str enum)، فالمقارنة
        # مع نصوص عادية شغّالة بلا تحويل.
        result = str(member.status) in ADMIN_STATUSES
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        # إجابة نهائية فعلياً من تيليجرام: المستخدم مش بالغروب، أو الغروب غير
        # صالح، أو البوت مطرود/محظور. منخزّنها بالكاش عادي.
        log_error(logger, f"Telegram rejected get_chat_member({chat_id}, {user_id}): {e}")
        result = False
    except TelegramAPIError as e:
        # عطل عابر (شبكة/rate limit/خطأ سيرفر) - منرفض هالمرة **بلا تخزين**،
        # حتى ما نقفل الصلاحية على المعلّم 5 دقايق كاملة بسبب ومضة واحدة.
        log_error(logger, f"Transient Telegram error on get_chat_member({chat_id}, {user_id}): {e}")
        return False
    except Exception as e:
        log_error(logger, f"Unexpected error on get_chat_member({chat_id}, {user_id}): {e}")
        return False

    # 3) تخزين النتيجة (إيجابية كانت أو سلبية) لتقليل نداءات تيليجرام.
    try:
        await redis_client.set(key, "1" if result else "0", ex=CACHE_TTL)
    except Exception as e:
        log_error(logger, f"Redis error caching admin status for {chat_id}:{user_id}: {e}")

    return result


async def invalidate_group_admin_cache(chat_id: int, user_id: Optional[int] = None) -> None:
    """يمسح الكاش يدوياً بدل انتظار الـ TTL.

    الاستخدام المتوقّع لاحقاً: عند وصول تحديث `chat_member` بتغيّر فيه صلاحيات
    عضو (ترقية/تنزيل)، أو لو بدنا نفرض إعادة فحص فورية قبل عملية حساسة
    (مثلاً إنهاء جلسة). بلا `user_id` بيمسح كل أعضاء الغروب المخزّنين.
    """
    try:
        if user_id:
            await redis_client.delete(_cache_key(chat_id, user_id))
            return
        pattern = f"{_CACHE_PREFIX}{chat_id}:*"
        # scan_iter بدل keys() - keys() بتقفل Redis على قواعد كبيرة.
        async for key in redis_client.scan_iter(match=pattern, count=100):
            await redis_client.delete(key)
    except Exception as e:
        log_error(logger, f"Redis error invalidating admin cache for {chat_id}: {e}")
