import fitz
import io
from PIL import Image

def process_file_smart(file_path):
    """استخراج النص رقمياً إذا أمكن، أو تحويل الصفحات لصور للرؤية[cite: 1, 3]"""
    doc = fitz.open(file_path)
    final_content = [] 
    
    for page in doc:
        text = page.get_text().strip()
        
        # إذا كان النص طويلاً بما يكفي، نعتبره نصاً رقمياً نظيفاً
        if len(text) > 50:
            final_content.append({"type": "text", "content": text})
        else:
            # إذا كان النص قصيراً أو غير موجود، نحوله لصورة للمعالجة بالذكاء الاصطناعي
            pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
            mode = "RGBA" if pix.alpha else "RGB"
            img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
            final_content.append({"type": "image", "content": buffer.getvalue()})
            
    doc.close()
    return final_content