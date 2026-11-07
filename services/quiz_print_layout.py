"""Language-aware text and adaptive columns shared by native PDF/Word exports."""
import re


_ARABIC = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
_TRAILING_TRANSLATION = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$", re.DOTALL)


def text_is_arabic(text, fallback=False):
    """Use the first letter, so an Arabic translation doesn't flip English prose."""
    first = _LETTER.search(str(text))
    return bool(_ARABIC.match(first.group())) if first else fallback


def text_parts(text, fallback=False, split_translation=False):
    """Preserve explicit newlines; also support old trailing Arabic translations.

    Only split a parenthesis when its entire letter content is Arabic and the
    source starts in another language. Ordinary bilingual prose is left intact.
    """
    result = []
    for line in str(text).splitlines():
        line = line.strip()
        if not line:
            continue
        match = _TRAILING_TRANSLATION.match(line) if split_translation else None
        if match and not text_is_arabic(match[1], fallback):
            letters = _LETTER.findall(match[2])
            if letters and all(_ARABIC.match(ch) for ch in letters):
                result.extend(((match[1].strip(), False), (match[2].strip(), True)))
                continue
        result.append((line, text_is_arabic(line, fallback)))
    return result or [("", fallback)]


def option_columns(options, width, measure, padding=20):
    """Prefer 4, then 2, then 1 column without reducing the font size."""
    for columns in (4, 2):
        if len(options) >= columns and all(
            len(parts) == 1 and measure(*parts[0]) <= width / columns - padding
            for parts in options
        ):
            return columns
    return 1
