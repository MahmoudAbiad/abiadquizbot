"""
services/ceremony_renderer.py
==============================================================================
رسم «حفل التكريم» للكويز الجماعي: صورة سوداء بلهب متناظر على الطرفين، اللوغو بالأعلى، وأسماء
الأوائل (البانرات الذهبية/الفضية/البرونزية للمراكز 1-3 + أعمدة للباقي). بصيغتين:

  png  صورة ثابتة (بتنرسل مع الأغنية كرسالة صوتية منفصلة)
  mp4  فيديو ~19 ثانية: لهب متحرك + ظهور تدريجي للأسماء + الأغنية بنفس الملف

⚠️ الإنتاج ما بيحسب لهب أبداً: اللهب أصول جاهزة بـ assets/ceremony (تتولّد بـ
`scripts/build_ceremony_assets.py` وتتعمل commit). هون Pillow فقط (بلا numpy)، والفيديو
بأمر ffmpeg واحد (الثنائي الساكن من imageio-ffmpeg، نفس اعتماد audio_duration_service).

يعمل بالسيرفر الثاني (render_worker) عبر `POST /v1/render/ceremony`، أو محلياً كاحتياط بخيط
واحد فقط (حماية ذاكرة 512MB). ما بيستورد aiogram ولا قاعدة البيانات.
==============================================================================
"""

import asyncio
import io
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from services.certificate_renderer import _fit, _font, _kind_for, _logo, _shape
from services.group_ceremony import (
    ASSETS_DIR, MAX_WINNERS, MUSIC_SECONDS, SILENT_SECONDS, THEME_AUDIO_PATH, VIDEO_HEIGHT as VIDEO_H, VIDEO_WIDTH as VIDEO_W,
)
from services.group_certificate import GROUP_MAX_CHARS, NAME_MAX_CHARS, TITLE_MAX_CHARS, clean_text

FLAMES_STILL_PATH = os.path.join(ASSETS_DIR, "flames_still.png")
FLAMES_LOOP_PATH = os.path.join(ASSETS_DIR, "flames_loop.mp4")

FRAME_W, FRAME_H = 1400, 1750          # الصورة الثابتة
FLAME_W = 300                          # عرض شريط اللهب لكل طرف
VIDEO_FPS = 16                         # VIDEO_W/H والمدد من group_ceremony (مصدر واحد للـ handler والرسم)
FFMPEG_TIMEOUT_SECONDS = 90

MAX_BANNERS = 6                        # أكتر من هيك (تعادلات كبيرة) بتنزل للأعمدة
FORMATS = ("png", "mp4")
DISCLAIMER = "حفل تكريم غير رسمي، صادر عن بوت أبيض"
PREVIEW_DISCLAIMER = "معاينة تقريبية، الأسماء للتجربة فقط"

GOLD = {"a": (255, 226, 120), "b": (200, 140, 20), "text": (60, 38, 0), "glow": (255, 190, 40)}
SILVER = {"a": (240, 244, 250), "b": (150, 160, 176), "text": (30, 36, 48), "glow": (200, 215, 240)}
BRONZE = {"a": (236, 170, 110), "b": (150, 84, 36), "text": (52, 24, 6), "glow": (230, 140, 70)}
_BANNER_SPEC = {1: (900, 168, GOLD), 2: (820, 132, SILVER), 3: (750, 120, BRONZE)}


class CeremonyError(ValueError):
    """مدخلات رسم غير صالحة، أو ffmpeg/الأصول غير متوفرة."""


# ==================== التحقق (السيرفر الثاني مكشوف للإنترنت) ====================
def validate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """أنواع وحدود وتنظيف نصوص. يرمي CeremonyError. يرجّع payload نظيف بمفاتيح ثابتة."""
    if not isinstance(payload, dict):
        raise CeremonyError("payload must be an object")
    fmt = payload.get("fmt")
    if fmt not in FORMATS:
        raise CeremonyError("fmt must be 'png' or 'mp4'")
    for key in ("music", "preview"):
        if not isinstance(payload.get(key), bool):
            raise CeremonyError(f"{key} must be a boolean")
    for key in ("title", "group_name"):
        if not isinstance(payload.get(key, ""), str) or len(payload.get(key, "")) > 400:
            raise CeremonyError(f"{key} must be a string")
    raw = payload.get("winners")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_WINNERS or any(not isinstance(w, dict) for w in raw):
        raise CeremonyError(f"winners must be a list of 1..{MAX_WINNERS} objects")
    winners: List[Dict[str, Any]] = []
    for w in raw:
        for key, lo, hi in (("rank", 1, 1000), ("score", 0, 100000), ("total", 1, 100000)):
            value = w.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise CeremonyError(f"winner.{key} must be an integer in [{lo}, {hi}]")
        if not isinstance(w.get("name", ""), str) or len(w.get("name", "")) > 400:
            raise CeremonyError("winner.name must be a string")
        winners.append({
            "rank": w["rank"], "score": w["score"], "total": w["total"],
            "name": clean_text(w.get("name"), NAME_MAX_CHARS, ellipsis=False) or "متفوّق",
        })
    if [w["rank"] for w in winners] != sorted(w["rank"] for w in winners):
        raise CeremonyError("winners must be ordered by rank")
    return {
        "fmt": fmt, "music": payload["music"], "preview": payload["preview"],
        "title": clean_text(payload.get("title"), TITLE_MAX_CHARS) or "كويز",
        "group_name": clean_text(payload.get("group_name"), GROUP_MAX_CHARS),
        "winners": winners,
    }


# ==================== الرسم ====================
def _gradient(size: Tuple[int, int], top, bottom) -> Image.Image:
    """تدرّج عمودي بلا numpy: linear_gradient (256×256 أسود→أبيض) ممدود ومُلوَّن."""
    return ImageOps.colorize(Image.linear_gradient("L").resize(size, Image.BILINEAR), black=top, white=bottom).convert("RGBA")


def _banner(base: Image.Image, cx: int, y: int, w: int, h: int, pal, rank: int, name: str, score_txt: str) -> None:
    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle((cx - w // 2, y, cx + w // 2, y + h), radius=h // 3, fill=pal["glow"] + (150,))
    base.alpha_composite(glow.filter(ImageFilter.GaussianBlur(26)))
    body = _gradient((w, h), pal["a"], pal["b"])
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 3, fill=255)
    base.paste(body, (cx - w // 2, y), mask)
    d = ImageDraw.Draw(base)
    d.rounded_rectangle((cx - w // 2 + 5, y + 5, cx + w // 2 - 5, y + h - 5), radius=h // 3 - 4, outline=(255, 255, 255, 120), width=2)
    shine = Image.new("RGBA", (w, h // 2), (255, 255, 255, 0))
    ImageDraw.Draw(shine).rounded_rectangle((8, 6, w - 8, h // 2), radius=h // 3, fill=(255, 255, 255, 46))
    base.alpha_composite(shine, (cx - w // 2, y))

    r = int(h * 0.36)
    ccx, ccy = cx + w // 2 - r - 26, y + h // 2          # دائرة المركز يمين (RTL)
    d.ellipse((ccx - r, ccy - r, ccx + r, ccy + r), fill=pal["text"] + (255,))
    d.ellipse((ccx - r + 6, ccy - r + 6, ccx + r - 6, ccy + r - 6), outline=pal["a"] + (255,), width=3)
    d.text((ccx, ccy + 2), str(rank), font=_font("name_lat", int(r * 1.25)), fill=pal["a"], anchor="mm")

    score_font = _font("name_lat", int(h * 0.30))
    left = cx - w // 2 + 40
    d.text((left, ccy), score_txt, font=score_font, fill=pal["text"], anchor="lm")
    score_w = int(d.textlength(score_txt, font=score_font))
    room = (ccx - r - 24) - (left + score_w + 30)
    font, text = _fit(d, name, _kind_for(name, "name_ar", "name_lat"), int(h * 0.58), 30, room)
    d.text(((ccx - r - 24 + left + score_w + 30) // 2, ccy + 2), _shape(text), font=font, fill=pal["text"], anchor="mm")


def static_layer(data: Dict[str, Any]) -> Image.Image:
    """الطبقة الشفافة (لوغو + عنوان + أسماء) بحجم FRAME_W×FRAME_H. `data` = validate_payload."""
    W, H = FRAME_W, FRAME_H
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = W // 2

    logo = _logo(200)
    halo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse((cx - 150, 60, cx + 150, 360), fill=(255, 170, 40, 90))
    img.alpha_composite(halo.filter(ImageFilter.GaussianBlur(40)))
    if logo is not None:
        img.alpha_composite(logo, (cx - 100, 70))

    d.text((cx, 372), _shape("حفل تكريم"), font=_font("title", 124), fill=(255, 214, 102), anchor="mm")
    d.line((cx - 260, 486, cx - 28, 486), fill=(255, 190, 70), width=3)
    d.line((cx + 28, 486, cx + 260, 486), fill=(255, 190, 70), width=3)
    d.polygon([(cx, 474), (cx + 12, 486), (cx, 498), (cx - 12, 486)], fill=(255, 190, 70))
    sub_font, sub_text = _fit(d, f"«{data['title']}»", "bold", 54, 30, 860)
    d.text((cx, 540), _shape(sub_text), font=sub_font, fill=(235, 235, 240), anchor="mm")

    winners = data["winners"]
    top = [w for w in winners if w["rank"] <= 3][:MAX_BANNERS]
    top_ids = {id(w) for w in top}
    rest = [w for w in winners if id(w) not in top_ids]
    k = 1.0 if len(top) <= 3 else max(0.62, 3.0 / len(top))      # تعادلات بالمراكز الأولى: بانرات أصغر
    y = 596
    for w_ in top:
        bw, bh, pal = _BANNER_SPEC[w_["rank"]]
        bh = int(bh * k)
        _banner(img, cx, y, bw if k == 1.0 else int(bw * (0.9 + 0.1 * k)), bh, pal, w_["rank"], w_["name"], f"{w_['score']}/{w_['total']}")
        y += bh + int(34 * k)

    if rest:                                                     # أعمدة، الأول يمين (RTL)
        y += 18
        row_h, row_gap, gap = 84, 12, 24
        rows_fit = max(1, int((H - 110 - y + row_gap) // (row_h + row_gap)))
        per_col = min(rows_fit, 5)
        ncols = max(1, -(-len(rest) // per_col))
        per_col = -(-len(rest) // ncols)                         # توزيع متوازن
        col_w = min(430, int((W - 2 * 130 - (ncols - 1) * gap) / ncols))
        cols = [rest[i:i + per_col] for i in range(0, len(rest), per_col)]
        right = cx + (len(cols) * col_w + (len(cols) - 1) * gap) // 2
        rr = 28
        for ci, col in enumerate(cols):
            x_right = right - ci * (col_w + gap)
            for ri, w_ in enumerate(col):
                ry = y + ri * (row_h + row_gap)
                mid = ry + row_h // 2
                d.rounded_rectangle((x_right - col_w, ry, x_right, ry + row_h), radius=26, fill=(255, 255, 255, 22), outline=(255, 170, 60, 150), width=2)
                d.ellipse((x_right - 16 - 2 * rr, mid - rr, x_right - 16, mid + rr), fill=(255, 150, 40, 255))
                d.text((x_right - 16 - rr, mid + 1), str(w_["rank"]), font=_font("name_lat", 32), fill=(40, 16, 0), anchor="mm")
                score = f"{w_['score']}/{w_['total']}"
                score_font = _font("name_lat", 26)
                d.text((x_right - col_w + 22, mid), score, font=score_font, fill=(255, 200, 120), anchor="lm")
                score_w = int(d.textlength(score, font=score_font))
                room = col_w - 16 - 2 * rr - 22 - 22 - score_w - 28
                font, text = _fit(d, w_["name"], _kind_for(w_["name"], "name_ar", "name_lat"), 44, 24, room)
                nx = (x_right - 16 - 2 * rr - 14 + (x_right - col_w + 22 + score_w + 14)) // 2
                d.text((nx, mid + 2), _shape(text), font=font, fill=(250, 246, 240), anchor="mm")

    foot = PREVIEW_DISCLAIMER if data["preview"] else DISCLAIMER
    if data["group_name"]:
        foot = f"{data['group_name']}  •  {foot}"
    foot_font, foot_text = _fit(d, foot, "bold", 30, 22, 900)
    d.text((cx, H - 50), _shape(foot_text), font=foot_font, fill=(170, 150, 130), anchor="mm")
    return img


def _require_assets(*paths: str) -> None:
    missing = [os.path.basename(p) for p in paths if not os.path.isfile(p)]
    if missing:
        raise CeremonyError(f"ceremony assets missing: {', '.join(missing)}")


def render_ceremony_png(payload: Dict[str, Any]) -> bytes:
    """الصورة الثابتة: خلفية سودا + لهب جاهز + طبقة الأسماء."""
    data = validate_payload(payload)
    _require_assets(FLAMES_STILL_PATH)
    base = Image.new("RGBA", (FRAME_W, FRAME_H), (0, 0, 0, 255))
    with Image.open(FLAMES_STILL_PATH) as flames:
        base.alpha_composite(flames.convert("RGBA"))
    base.alpha_composite(static_layer(data))
    out = io.BytesIO()
    base.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def _ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        path = shutil.which("ffmpeg")
        if path:
            return path
    raise CeremonyError("ffmpeg is not available")


def build_ffmpeg_command(ffmpeg: str, overlay_path: str, out_path: str, *, music: bool) -> List[str]:
    """أمر واحد: دورة اللهب (مكرّرة) + طبقة الأسماء (تظهر بعد ثانية) + الأغنية. دالة صرفة (قابلة للاختبار)."""
    fade_text = "[1:v]format=rgba,fade=t=in:st=1:d=1.6:alpha=1[ov]"
    fade_fire = "[0:v]fade=t=in:st=0:d=2[bg]"
    graph = f"{fade_fire};{fade_text};[bg][ov]overlay=0:0:format=auto,format=yuv420p[v]"
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-stream_loop", "-1", "-i", FLAMES_LOOP_PATH,
        "-loop", "1", "-framerate", str(VIDEO_FPS), "-i", overlay_path,
    ]
    if music:
        cmd += ["-i", THEME_AUDIO_PATH]
    cmd += ["-filter_complex", graph, "-map", "[v]"]
    if music:
        cmd += ["-map", "2:a", "-c:a", "aac", "-b:a", "96k"]
    else:
        cmd += ["-an"]
    cmd += [
        "-t", str(MUSIC_SECONDS if music else SILENT_SECONDS), "-r", str(VIDEO_FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-movflags", "+faststart", out_path,
    ]
    return cmd


def render_ceremony_video(payload: Dict[str, Any]) -> bytes:
    """فيديو mp4 كامل. ما بيعتمد على أي ملف خارجي غير الأصول الثلاثة بـ assets/ceremony."""
    data = validate_payload(payload)
    _require_assets(FLAMES_LOOP_PATH, *( [THEME_AUDIO_PATH] if data["music"] else [] ))
    ffmpeg = _ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="ceremony_") as tmp:
        overlay_path = os.path.join(tmp, "overlay.png")
        out_path = os.path.join(tmp, "ceremony.mp4")
        static_layer(data).resize((VIDEO_W, VIDEO_H), Image.LANCZOS).save(overlay_path, format="PNG")
        try:
            result = subprocess.run(
                build_ffmpeg_command(ffmpeg, overlay_path, out_path, music=data["music"]),
                capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise CeremonyError("ffmpeg timed out")
        if result.returncode != 0 or not os.path.isfile(out_path):
            tail = (result.stderr or b"").decode("utf-8", "replace")[-300:]
            raise CeremonyError(f"ffmpeg failed: {tail}")
        with open(out_path, "rb") as fh:
            return fh.read()


def render_ceremony(payload: Dict[str, Any]) -> Tuple[bytes, str]:
    """نقطة الدخول المتزامنة (بالسيرفر الثاني وبالاحتياط المحلي). (bytes, kind) حيث kind: 'png' | 'mp4'."""
    fmt = payload.get("fmt") if isinstance(payload, dict) else None
    if fmt == "mp4":
        return render_ceremony_video(payload), "mp4"
    return render_ceremony_png(payload), "png"


_LOCAL_SEMAPHORE: "asyncio.Semaphore | None" = None


async def render_ceremony_async(payload: Dict[str, Any]) -> bytes:
    """السيرفر الثاني أولاً (لو مفعّل)، ثم احتياط محلي بخيط واحد فقط (حماية ذاكرة 512MB).
    ترمي استثناء عند الفشل - المستدعي (الإرسال/المعاينة) مسؤول عن ابتلاعه."""
    global _LOCAL_SEMAPHORE
    from services import offload

    remote = await offload.remote_render_ceremony(payload)
    if remote is not None:
        return remote
    if _LOCAL_SEMAPHORE is None:
        _LOCAL_SEMAPHORE = asyncio.Semaphore(1)
    async with _LOCAL_SEMAPHORE:
        data, _kind = await asyncio.to_thread(render_ceremony, payload)
        return data
