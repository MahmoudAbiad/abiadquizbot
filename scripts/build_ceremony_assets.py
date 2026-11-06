"""
scripts/build_ceremony_assets.py
==============================================================================
أداة تطوير (مش جزء من الإنتاج): بتولّد أصول لهب «حفل التكريم» الجاهزة مرة وحدة، بحيث
الإنتاج ما يحسب لهب أبداً (لا numpy ولا CPU) - بس بيركّب طبقة الأسماء فوقها.

المخرجات (بتتعمل commit):
  assets/ceremony/flames_still.png  إطار لهب ثابت (RGBA، شفاف بالمنتصف) لنسخة الصورة
  assets/ceremony/flames_loop.mp4   دورة لهب صامتة 1 ثانية (تتكرر بـ ffmpeg -stream_loop) لنسخة الفيديو

التشغيل:  python scripts/build_ceremony_assets.py        (يحتاج numpy + imageio-ffmpeg أو ffmpeg بالـ PATH)

الدورة متكرّرة بسلاسة: كل حركة اللهب والشرر مضروبة بأعداد صحيحة من الدورة.
==============================================================================
"""
import math
import os
import random
import shutil
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from services.ceremony_renderer import FLAME_W, FRAME_H, FRAME_W, VIDEO_FPS, VIDEO_H, VIDEO_W  # noqa: E402

OUT = os.path.join(ROOT, "assets", "ceremony")
W, H = FRAME_W, FRAME_H
LOOP_FRAMES = VIDEO_FPS  # ثانية وحدة

_PALETTE = [
    (0.00, (0, 0, 0)), (0.12, (60, 0, 0)), (0.30, (170, 20, 0)), (0.52, (240, 80, 0)),
    (0.74, (255, 170, 20)), (0.92, (255, 235, 120)), (1.00, (255, 252, 220)),
]


def _lut() -> np.ndarray:
    xs = np.array([p[0] for p in _PALETTE])
    out = np.zeros((256, 3), dtype=np.float32)
    for c in range(3):
        out[:, c] = np.interp(np.linspace(0, 1, 256), xs, [p[1][c] for p in _PALETTE])
    return out.astype(np.uint8)


_LUT = _lut()


def _tongues(seed: int = 7):
    rnd = random.Random(seed)
    items, n = [], 46
    for i in range(n):
        u = i / (n - 1)
        items.append({
            "cx": rnd.uniform(18, FLAME_W - 90), "base": 260 + u * (H - 260) + rnd.uniform(-30, 30),
            "h": rnd.uniform(170, 330) * (0.55 + 0.7 * u), "w": rnd.uniform(38, 74) * (0.7 + 0.5 * u),
            "ph": rnd.uniform(0, 6.28), "sp": rnd.choice((1, 1, 2)),
        })
    return items


_TONGUES = _tongues()


def flame_strip(phase: float) -> Image.Image:
    """شريط اللهب الأيسر (RGBA). phase بالدورات: 0 و1 متطابقان تماماً (حلقة سلسة)."""
    ys, xs = np.mgrid[0:H, 0:FLAME_W].astype(np.float32)
    heat = np.zeros((H, FLAME_W), dtype=np.float32)
    ang = phase * 2 * math.pi
    for t in _TONGUES:
        tt = (t["base"] - ys) / t["h"]
        valid = (tt > 0) & (tt < 1)
        sway = np.sin(tt * 5.0 + t["ph"] + ang * t["sp"]) * 16 * tt
        flick = 0.88 + 0.12 * math.sin(ang * 2 + t["ph"])
        half = t["w"] * np.power(np.clip(1 - tt, 0, 1), 0.85) * flick
        dist = np.abs(xs - (t["cx"] + sway)) / np.maximum(half, 1)
        inten = np.clip(1 - dist, 0, 1) ** 1.15 * np.power(np.clip(1 - tt, 0, 1), 0.45)
        heat = np.maximum(heat, np.where(valid, inten, 0))
    edge = np.clip(1 - (xs / FLAME_W), 0, 1) ** 0.7
    heat = np.clip(heat * (0.35 + 0.85 * edge) * 1.15, 0, 1)
    img = Image.fromarray((heat * 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(2.2))
    h8 = np.asarray(img)
    alpha = np.clip(h8.astype(np.float32) * 1.4, 0, 255).astype(np.uint8)
    return Image.fromarray(np.dstack([_LUT[h8], alpha]), "RGBA")


def _sparks(seed: int = 3, n: int = 70):
    rnd = random.Random(seed)
    return [{
        "side": rnd.choice((0, 1)), "x0": rnd.uniform(20, FLAME_W + 40), "y0": rnd.uniform(0, H),
        "k": rnd.choice((1, 2)),               # صعود بعدد صحيح من الدورات = حلقة سلسة
        "r": rnd.uniform(1.5, 4.0), "a": int(rnd.uniform(110, 230)), "ph": rnd.uniform(0, 6.28), "g": rnd.randint(150, 220),
    } for _ in range(n)]


_SPARKS = _sparks()


def embers(phase: float, w: int, h: int) -> Image.Image:
    s = w / W
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for sp in _SPARKS:
        y = (sp["y0"] - phase * H * sp["k"] * 0.6) % H
        x = sp["x0"] + math.sin(y / 90 + sp["ph"]) * 14
        if sp["side"]:
            x = W - x
        r = sp["r"]
        d.ellipse(((x - r) * s, (y - r) * s, (x + r) * s, (y + r) * s), fill=(255, sp["g"], 60, sp["a"]))
    return layer.filter(ImageFilter.GaussianBlur(0.8 * s))


def compose_flames(phase: float, w: int, h: int) -> Image.Image:
    """طبقة لهب الطرفين + الشرر (RGBA بحجم w×h) من نفس الـ phase."""
    strip = flame_strip(phase)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layer.alpha_composite(strip, (0, 0))
    layer.alpha_composite(strip.transpose(Image.FLIP_LEFT_RIGHT), (W - FLAME_W, 0))
    if (w, h) != (W, H):
        layer = layer.resize((w, h), Image.LANCZOS)
    layer.alpha_composite(embers(phase, w, h))
    return layer


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        path = shutil.which("ffmpeg")
        if not path:
            raise SystemExit("ffmpeg غير متوفر (pip install imageio-ffmpeg)")
        return path


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    still = compose_flames(0.0, W, H)
    still.save(os.path.join(OUT, "flames_still.png"), optimize=True)
    print("flames_still.png", os.path.getsize(os.path.join(OUT, "flames_still.png")) // 1024, "KB")

    cmd = [ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{VIDEO_W}x{VIDEO_H}", "-r", str(VIDEO_FPS), "-i", "-", "-an",
           "-c:v", "libx264", "-preset", "veryslow", "-crf", "20", "-pix_fmt", "yuv420p",
           "-g", str(LOOP_FRAMES), "-movflags", "+faststart", os.path.join(OUT, "flames_loop.mp4")]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for k in range(LOOP_FRAMES):
        frame = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 255))
        frame.alpha_composite(compose_flames(k / LOOP_FRAMES, VIDEO_W, VIDEO_H))
        proc.stdin.write(frame.convert("RGB").tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit("ffmpeg failed")
    print("flames_loop.mp4", os.path.getsize(os.path.join(OUT, "flames_loop.mp4")) // 1024, "KB")


if __name__ == "__main__":
    main()
