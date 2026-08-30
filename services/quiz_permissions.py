# services/quiz_permissions.py
"""
==============================================================================
MODULE: صلاحية إدارة الكويز (حذف/تعديل) - الأدمن أو مالك الكويز فقط
==============================================================================
موديول مشترك صغير يُستخدم من أي شاشة يظهر فيها كويز (قائمة الكويزات المخزّنة
للملف، نتيجة الاختبار، المفضلة، لوحات الإدارة، محرر أسئلة الرياضيات عبر
الويب...) لتحديد:
1. هل يظهر زر "🗑 حذف الكويز نهائياً" لهذا المستخدم بالذات على هذا الكويز؟
2. عند الضغط الفعلي على الزر (handlers/quiz_delete.py) أو عند حفظ سؤال معدَّل
   (handlers/quiz_runner.py - عادي أو رياضيات) - إعادة التحقق من نفس الصلاحية
   من جديد بجلب creator_id الحقيقي والحالي من قاعدة البيانات مباشرة (وليس
   الاعتماد على قيمة قد تكون قديمة وصلت مع الكيبورد أو رابط صفحة التعديل
   نفسه)، لأن أي مستخدم يقدر تقنياً يرسل أي callback_data أو نداء API مباشرة
   حتى لو لم يظهر له الزر/الرابط أصلاً.

القاعدة: الأدمن (ADMIN_ID) مسموح له دائماً بحذف/تعديل أي كويز، ومالك الكويز
(creator_id) مسموح له بحذف/تعديل كويزه هو فقط - لا أحد غيرهما. نفس القاعدة
بالضبط تنطبق على الحذف والتعديل معاً، فمنطقهما مُوحَّد هنا بدالة واحدة
(can_manage_quiz) بدل تكراره بمكانين قد يتباعدان لاحقاً بالخطأ.
"""

from typing import Optional

from config import ADMIN_ID


def can_manage_quiz(viewer_id: Optional[int], creator_id: Optional[int]) -> bool:
    """يتحقق إذا كان viewer_id مسموحاً له بإدارة (حذف أو تعديل) كويز منشئه creator_id."""
    if not viewer_id:
        return False
    if str(ADMIN_ID) != "0" and str(viewer_id) == str(ADMIN_ID):
        return True
    if creator_id is None:
        return False
    try:
        return int(creator_id) == int(viewer_id)
    except (TypeError, ValueError):
        return str(creator_id) == str(viewer_id)


# 🆕 اسمان مخصصان (delete/edit) لنفس القاعدة can_manage_quiz - لوضوح القراءة بمكان
# الاستدعاء فقط (handlers/quiz_delete.py وhandlers/quiz_runner.py على التوالي)
# بدون أي فرق فعلي بالمنطق بينهما.
def can_delete_quiz(viewer_id: Optional[int], creator_id: Optional[int]) -> bool:
    """يتحقق إذا كان viewer_id مسموحاً له بحذف كويز منشئه creator_id نهائياً."""
    return can_manage_quiz(viewer_id, creator_id)


def can_edit_quiz(viewer_id: Optional[int], creator_id: Optional[int]) -> bool:
    """يتحقق إذا كان viewer_id مسموحاً له بتعديل أسئلة كويز منشئه creator_id."""
    return can_manage_quiz(viewer_id, creator_id)
