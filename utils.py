import fitz  # PyMuPDF

def process_file_smart(path):
    """
    تحليل ذكي لملف الـ PDF. 
    إذا كانت الصفحة نصية، يسحب النص.
    إذا كانت الصفحة عبارة عن صورة (سكانر)، يحولها لبيانات صورة (Bytes) ليرسلها لاحقاً لـ Gemini API.
    """
    results = []
    try:
        doc = fitz.open(path)
        for page in doc:
            text = page.get_text().strip()
            # إذا كان النص أقل من 50 حرفاً، فالصفحة غالباً صورة (Scan)
            if len(text) > 50:
                results.append({"type": "text", "content": text})
            else:
                # تحويل الصفحة إلى صورة بجودة مناسبة
                pix = page.get_pixmap(dpi=150)
                img_bytes = pix.tobytes("png")
                results.append({"type": "image", "content": img_bytes})
        doc.close()
    except Exception as e:
        print(f"Error in process_file_smart: {e}")
    return results
