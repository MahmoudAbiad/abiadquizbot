"""Presentation metadata only. Never generates images or changes answer order."""
from urllib.parse import urlsplit


class PhotoText(str):
    """A caption with an existing source image; compatible with (text, rows) screens."""
    def __new__(cls, text, image_url):
        obj = super().__new__(cls, text)
        obj.image_url = image_url
        return obj


def source_image(question):
    value = question.get("image_url")
    if not value and isinstance(question.get("metadata"), dict):
        value = question["metadata"].get("image_url")
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        return value if url.scheme == "https" and url.netloc else None
    except ValueError:
        return None


def image_option_label(index):
    # Both alphabets match the existing quiz renderer, without guessing from subject names
    # or importing its heavyweight drawing engine. JSON images can use numbers as well.
    ar = ("أ", "ب", "ج", "د", "هـ", "و", "ز", "ح")
    en = ("A", "B", "C", "D", "E", "F", "G", "H")
    return f"{index + 1} · {ar[index]} / {en[index]}" if index < len(ar) else str(index + 1)
