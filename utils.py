import fitz  # PyMuPDF

def process_file_smart(path):
    results = []
    try:
        doc = fitz.open(path)
        for page in doc:
            text = page.get_text().strip()
            if text:
                results.append({"type": "text", "content": text})
            else:
                # تحويل الصفحة لصورة وإرسالها لـ Gemini
                pix = page.get_pixmap(dpi=150)
                img_bytes = pix.tobytes("png")
                results.append({"type": "image", "content": img_bytes})
        doc.close()
    except Exception as e:
        print(f"Error in process_file_smart: {e}")
    return results
