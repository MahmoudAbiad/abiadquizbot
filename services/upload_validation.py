"""Local format/resource checks, without credentials or external calls."""
import os
import warnings

DOCUMENT_EXTENSIONS = frozenset({'.pdf', '.docx', '.pptx', '.txt'})
TELEGRAM_IMAGE_DOCUMENT_EXTENSIONS = frozenset({'.png', '.jpg', '.jpeg'})
IMAGE_EXTENSIONS = frozenset({'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff'})
AUDIO_EXTENSIONS = frozenset({'.mp3', '.m4a', '.aac', '.wav', '.ogg', '.oga', '.opus', '.flac', '.aiff', '.aif', '.wma', '.webm', '.mp4', '.3gp', '.amr'})
MAX_IMAGE_PIXELS = 25_000_000
MAX_IMAGE_DIMENSION = 16_000
MAX_PDF_PARTS_BYTES = 300 * 1024 * 1024
MIN_FREE_DISK_BYTES = 64 * 1024 * 1024


def cleanup_stale_downloads(directory='downloads', max_age=48 * 3600):
    """Keep files belonging to live 24h FSM sessions across process restarts."""
    import time
    if not os.path.isdir(directory):
        return
    cutoff = time.time() - max_age
    for entry in os.scandir(directory):
        try:
            if entry.is_file(follow_symlinks=False) and entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
        except FileNotFoundError:
            pass


def supported_extension(name, allowed):
    ext = os.path.splitext(name or '')[1].lower()
    if ext not in allowed:
        raise ValueError('صيغة الملف غير مدعومة، يرجى اختيار صيغة مدعومة من البوت.')
    return ext


def validate_image(path):
    # Inspect dimensions before pixel decoding; verify also detects broken images.
    from PIL import Image
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(path) as image:
            width, height = image.size
            if max(width, height) > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
                raise ValueError('أبعاد الصورة تتجاوز الحد المسموح، يرجى تصغير الصورة.')
            if getattr(image, 'n_frames', 1) != 1:
                raise ValueError('يرجى رفع صورة ثابتة بدلاً من صورة متحركة.')
            image.verify()


class InvalidUploadError(ValueError):
    """ملف مرفوع غير صالح لسبب محدد - رسالته (str) موجّهة للطالب مباشرة وجاهزة للعرض.
    ترث من ValueError لتبقى متوافقة مع أي كود يلتقط ValueError."""


MSG_PDF_PASSWORD_PROTECTED = 'ملف PDF محمي بكلمة مرور. أزل الحماية ثم أعد إرساله.'
MSG_PDF_INVALID = 'ملف PDF غير صالح أو فارغ من الصفحات.'
MSG_PDF_UNREADABLE = 'تعذر قراءة ملف PDF، قد يكون تالفاً. جرّب إعادة تصديره ثم أرسله من جديد.'


def validate_document(path):
    ext = supported_extension(path, DOCUMENT_EXTENSIONS)
    if ext == '.pdf':
        import fitz
        # الملف المفقود من القرص ليس "PDF تالفاً/محمياً" - نرفع FileNotFoundError صراحة
        # (fitz يرفع استثناءً خاصاً به لا يرث من FileNotFoundError المدمج) ليميّزه المستدعي.
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        try:
            with fitz.open(path) as pdf:
                if not pdf.is_pdf:
                    raise InvalidUploadError(MSG_PDF_INVALID)
                if pdf.needs_pass:
                    raise InvalidUploadError(MSG_PDF_PASSWORD_PROTECTED)
                if len(pdf) < 1:
                    raise InvalidUploadError(MSG_PDF_INVALID)
                return len(pdf)
        except InvalidUploadError:
            raise
        except FileNotFoundError:
            raise
        except Exception as exc:
            if not os.path.exists(path):  # حُذف الملف أثناء الفتح
                raise FileNotFoundError(path) from exc
            raise InvalidUploadError(MSG_PDF_UNREADABLE) from exc
    if ext in {'.docx', '.pptx'}:
        from utils import _validate_office_archive
        import zipfile
        _validate_office_archive(path)
        expected = 'word/document.xml' if ext == '.docx' else 'ppt/presentation.xml'
        with zipfile.ZipFile(path) as archive:
            if expected not in archive.namelist() or '[Content_Types].xml' not in archive.namelist():
                raise ValueError('المستند لا يطابق صيغة Office المطلوبة.')
    if ext == '.txt':
        # Incremental scan; do not read an arbitrary 100MB binary blob into RAM.
        import codecs
        decoder = codecs.getincrementaldecoder('utf-8')()
        with open(path, 'rb') as source:
            while chunk := source.read(64 * 1024):
                if b'\x00' in chunk:
                    raise ValueError('الملف النصي يحتوي على بيانات غير نصية.')
                decoder.decode(chunk)
            decoder.decode(b'', final=True)
    return 1
