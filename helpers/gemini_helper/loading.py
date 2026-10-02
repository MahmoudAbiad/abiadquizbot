"""
helpers/gemini_helper/loading.py
رسالة الانتظار المتحركة أثناء التوليد.
"""

from typing import Any
import asyncio

from aiogram.exceptions import TelegramBadRequest

from logger import get_logger, log_warning

logger = get_logger(__name__)


LOADING_PHRASES = (
    "🔍 يقوم الذكاء الاصطناعي الآن بفحص ملفاتك المرفوعة...",
    "🧠 جاري تحليل النصوص واستخراج المفاهيم الأكاديمية...",
    "⚡ يتم الآن توليد الأسئلة التفاعلية وصيغ الكويز...",
    "✨ شارَفنا على الانتهاء... نقوم بتنسيق لوحة الاختبار...",
    "⏳ لحظات قليلة جداً ويصبح اختبارك التفاعلي جاهزاً للبدء...",
)


async def _loading_animation(message: Any, stop_event: asyncio.Event) -> None:
    """
    مهمة خلفية لتحديث رسالة الانتظار بعبارات تشجيعية.
    IMPORTANT: تم التعامل مع asyncio.CancelledError صراحة لمنع أخطاء إلغاء المهمة في الـ Logs.
    """
    phrase_index = 0
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3)
            break
        except asyncio.TimeoutError:
            try:
                await message.edit_text(LOADING_PHRASES[phrase_index])
            except TelegramBadRequest:
                # تحدث هذه الاستثناءات إذا لم يتغير النص في تلغرام
                pass
            except Exception as exc:
                log_warning(logger, f"Loading-status update failed: {exc}")
            phrase_index = (phrase_index + 1) % len(LOADING_PHRASES)
        except asyncio.CancelledError:
            # الخروج النظيف عند إلغاء المهمة من دالة generate_quiz_smart
            break
