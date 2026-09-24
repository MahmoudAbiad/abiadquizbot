"""
supabase_helper._client
========================
العميل المشترك لقاعدة بيانات Supabase + الـ logger + دالة مساعدة عامة
(_is_valid_uuid) مستخدمة من أكثر من قسم واحد ضمن الباكج.

⚠️ قاعدة صارمة: أي ملف فرعي آخر بهذا الباكج (users.py, quiz_cache.py, ...)
يستورد `supabase` و`logger` (ودوال log_error/log_warning/log_info) من هذا
الملف حصراً (`from ._client import ...`) - ممنوع الاستيراد من `.` (أي من
__init__.py) أو من `supabase_helper` مباشرة، لتفادي أي Circular Import أثناء
تهيئة الباكج.
"""

import asyncio
import os
import uuid
from typing import Optional

from dotenv import load_dotenv, find_dotenv
from supabase import create_async_client

from logger import get_logger, log_error, log_warning, log_info

dotenv_path = find_dotenv()
load_dotenv(dotenv_path)

logger = get_logger(__name__)


def _is_valid_uuid(value: Optional[str]) -> bool:
    """يتحقق أن القيمة UUID حقيقي وصالح قبل استخدامها في أعمدة uuid بقاعدة البيانات.
    يمنع تكرار خطأ 22P02 (invalid input syntax for type uuid) في حال تمرير معرف وهمي/ناقص."""
    if not value:
        return False
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False

# ==================== إعداد واقلاع عميل قاعدة البيانات بشكل آمن ====================
# 🆕 مخزّن كمتغير بمستوى الملف (وليس فقط داخل os.getenv أدناه) حتى يمكن استيراده
# من ملفات أخرى بنفس أسلوب الاستيراد المعتمد بباقي المشروع (بدون بادئة "helpers.").
SUPABASE_URL = os.getenv("SUPABASE_URL")

try:
    client_or_coro = create_async_client(SUPABASE_URL, os.getenv("SUPABASE_KEY"))
    
    if asyncio.iscoroutine(client_or_coro):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                supabase = loop.run_until_complete(client_or_coro)
            else:
                supabase = asyncio.run(client_or_coro)
        except RuntimeError:
            supabase = asyncio.run(client_or_coro)
    else:
        supabase = client_or_coro

    log_info(logger, "Supabase Async client initialized successfully with centralized schema mapping")
except Exception as e:
    log_error(logger, f"Failed to initialize Supabase Async: {e}", exception=e)
    raise
# ==================================================================================
