"""
helpers/gemini_helper/schemas.py
مخططات Pydantic للمخرجات المُهيكلة (QuizQuestion/QuizResponse/...).
"""

from typing import List, Optional

from pydantic import BaseModel, Field


# ==============================================================================
# PYDANTIC SCHEMAS (STRUCTURED OUTPUT)
# ==============================================================================
# AI-NOTE: نضمن إجبار Gemini و Groq على التقيُّد بهذه البنية الدقيقة لتفادي أخطاء Parsing
class QuizTable(BaseModel):
    """🆕 جدول بيانات هيكلي (زي جداول التوزيعات التكرارية بالإحصاء) - يُملأ فقط
    عند نمط الكويز المصوّر الرياضي (is_math_mode) ولمسائل تعتمد فعلياً على جدول،
    بدل محاولة حشر الجدول كنص/LaTeX داخل question (غير مدعوم برسم الصورة).
    راجع services/image_quiz_renderer.py لكيفية رسمه فعلياً ضمن صورة السؤال."""
    headers: List[str] = Field(default_factory=list, description="Table column headers")
    rows: List[List[str]] = Field(default_factory=list, description="Table data rows")


class QuizMatrix(BaseModel):
    """🆕 مصفوفة رياضية هيكلية واحدة (زي جداول البيانات أعلاه، نفس المبدأ) - تُملأ فقط
    عند نمط الكويز المصوّر الرياضي ولمسائل تعتمد فعلياً على مصفوفة/محدّد، بدل محاولة
    كتابتها بصيغة LaTeX (\\begin{matrix}/\\begin{pmatrix} غير مدعومة إطلاقاً بمحرك الرسم
    mathtext). راجع services/image_quiz_renderer.py._draw_matrices لكيفية رسمها فعلياً
    بأقواس حقيقية (مربعة/دائرية/خط عمودي للمحدّد) بعناصر رسم مباشرة."""
    label: str = Field(default="", description="Optional short label before the bracket, e.g. 'A ='")
    rows: List[List[str]] = Field(default_factory=list, description="Matrix rows, each a list of cell values")
    bracket: str = Field(default="square", description="'square' (matrix) | 'round' (pmatrix) | 'bar' (determinant)")


class QuizQuestion(BaseModel):
    question: str = Field(description="Question text")
    options: List[str] = Field(description="Four answer options")
    correct_option_id: int = Field(description="Correct option index")
    hint: str = Field(description="Hint")
    explanation: str = Field(default="", description="Explanation")
    # 🆕 اختياري - يُملأ فقط لو المسألة تعتمد فعلياً على جدول بيانات (راجع QuizTable أعلاه)
    table: Optional[QuizTable] = Field(default=None, description="Optional structured data table")
    # 🆕 اختياري - يُملأ فقط لو المسألة تعتمد فعلياً على مصفوفة/مصفوفتين أو أكثر (راجع
    # QuizMatrix أعلاه). تُرسم بجانب بعضها أفقياً بترتيب القائمة.
    matrices: List[QuizMatrix] = Field(default_factory=list, description="Optional structured matrices")


class QuizResponse(BaseModel):
    questions: List[QuizQuestion]
