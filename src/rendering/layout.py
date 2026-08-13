"""Reusable 4K layout primitives for the FPL VORTEX broadcast renderer."""
from __future__ import annotations

import math
import textwrap
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont

CANVAS = (3840, 2160)
HEADER_H = 365
FOOTER_H = 260
BODY_TOP = HEADER_H
BODY_BOTTOM = CANVAS[1] - FOOTER_H

FONT_DIR = Path("assets/fonts")
FONT_CANDIDATES = {
    "condensed": [
        FONT_DIR / "BebasNeue-Regular.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf"),
    ],
    "bold": [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ],
    "regular": [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    ],
}
_FONT_CACHE: dict[tuple[str, int], ImageFont.ImageFont] = {}


def font(size: int, role: str = "bold") -> ImageFont.ImageFont:
    key = (role, int(size))
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    for path in FONT_CANDIDATES.get(role, FONT_CANDIDATES["bold"]):
        if path.exists():
            try:
                result = ImageFont.truetype(str(path), int(size))
                _FONT_CACHE[key] = result
                return result
            except Exception:
                continue
    result = ImageFont.load_default()
    _FONT_CACHE[key] = result
    return result


def clean_text(value: object, fallback: str = "NOT REPORTED") -> str:
    value = " ".join(str(value or "").replace("\u00a0", " ").split()).strip()
    if not value or value.casefold() in {"none", "null", "unknown", "n/a", "na"}:
        return fallback
    return value


def text_width(draw: ImageDraw.ImageDraw, value: str, fnt: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), value, font=fnt)
    return max(0, box[2] - box[0])


def fit_font(
    draw: ImageDraw.ImageDraw,
    value: object,
    max_width: int,
    *,
    max_size: int,
    min_size: int,
    role: str = "bold",
) -> ImageFont.ImageFont:
    text = clean_text(value)
    lo, hi = int(min_size), int(max_size)
    best = font(lo, role)
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = font(mid, role)
        if text_width(draw, text, candidate) <= max_width:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def truncate(draw: ImageDraw.ImageDraw, value: object, fnt: ImageFont.ImageFont, max_width: int) -> str:
    text = clean_text(value)
    if text_width(draw, text, fnt) <= max_width:
        return text
    suffix = "…"
    while text and text_width(draw, text + suffix, fnt) > max_width:
        text = text[:-1].rstrip()
    return (text + suffix) if text else suffix


def wrap_text(
    draw: ImageDraw.ImageDraw,
    value: object,
    fnt: ImageFont.ImageFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    words = clean_text(value).split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        trial = " ".join([*current, word])
        if current and text_width(draw, trial, fnt) > max_width:
            lines.append(" ".join(current))
            current = [word]
            if len(lines) == max_lines:
                break
        else:
            current.append(word)
    if len(lines) < max_lines and current:
        lines.append(" ".join(current))
    if lines and len(lines) == max_lines and sum(len(x.split()) for x in lines) < len(words):
        lines[-1] = truncate(draw, lines[-1] + " " + " ".join(words[sum(len(x.split()) for x in lines):]), fnt, max_width)
    return lines


def alpha_panel(
    image: Image.Image,
    box: tuple[int, int, int, int],
    *,
    fill=(0, 0, 0, 240),
    outline=(30, 224, 238, 255),
    width=6,
    radius=28,
    glow=False,
) -> None:
    if glow and outline:
        glow_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_layer)
        for extra, alpha in ((18, 25), (10, 50)):
            expanded = (box[0]-extra, box[1]-extra, box[2]+extra, box[3]+extra)
            gd.rounded_rectangle(expanded, radius=radius+extra, outline=(*outline[:3], alpha), width=extra)
        image.paste(glow_layer, (0, 0), glow_layer)
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)
    image.paste(layer, (0, 0), layer)


def paste_contain(base: Image.Image, asset: Image.Image, box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    source = asset.convert("RGBA")
    scale = min((x2-x1)/max(1, source.width), (y2-y1)/max(1, source.height))
    source = source.resize(
        (max(1, round(source.width*scale)), max(1, round(source.height*scale))),
        Image.Resampling.LANCZOS,
    )
    x = x1 + (x2-x1-source.width)//2
    y = y1 + (y2-y1-source.height)//2
    base.paste(source, (x, y), source)


def paste_cover(base: Image.Image, asset: Image.Image, box: tuple[int, int, int, int], *, rounded=0) -> None:
    from PIL import ImageOps

    x1, y1, x2, y2 = box
    fitted = ImageOps.fit(asset.convert("RGBA"), (x2-x1, y2-y1), method=Image.Resampling.LANCZOS, centering=(0.5, 0.3))
    if rounded:
        mask = Image.new("L", fitted.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, fitted.width-1, fitted.height-1), radius=rounded, fill=255)
        base.paste(fitted, (x1, y1), mask)
    else:
        base.paste(fitted, (x1, y1), fitted)


def draw_icon(draw: ImageDraw.ImageDraw, kind: str, box: tuple[int, int, int, int], color=(255,255,255)) -> None:
    """Simple thick-line icons that remain legible at 1080p."""
    x1, y1, x2, y2 = box
    w, h = x2-x1, y2-y1
    cx, cy = (x1+x2)//2, (y1+y2)//2
    stroke = max(8, round(min(w, h)*0.09))
    k = str(kind or "").casefold()
    if k in {"player", "person", "name", "manager"}:
        r = min(w, h)//5
        draw.ellipse((cx-r, y1+h*0.08, cx+r, y1+h*0.08+2*r), fill=color)
        draw.rounded_rectangle((x1+w*0.22, y1+h*0.48, x2-w*0.22, y2-h*0.08), radius=r, fill=color)
    elif k in {"calendar", "return", "gameweek", "matches", "updated", "date"}:
        draw.rounded_rectangle((x1+w*0.14, y1+h*0.2, x2-w*0.14, y2-h*0.12), radius=12, outline=color, width=stroke)
        draw.line((x1+w*0.14, y1+h*0.38, x2-w*0.14, y1+h*0.38), fill=color, width=stroke)
        for i in range(3):
            for j in range(2):
                draw.ellipse((x1+w*(0.28+i*0.22), y1+h*(0.52+j*0.18), x1+w*(0.34+i*0.22), y1+h*(0.58+j*0.18)), fill=color)
    elif k in {"injury", "medical", "status", "plus"}:
        draw.rounded_rectangle((cx-stroke, y1+h*0.12, cx+stroke, y2-h*0.12), radius=stroke//2, fill=color)
        draw.rounded_rectangle((x1+w*0.12, cy-stroke, x2-w*0.12, cy+stroke), radius=stroke//2, fill=color)
    elif k in {"clock", "time"}:
        draw.ellipse((x1+w*0.12, y1+h*0.12, x2-w*0.12, y2-h*0.12), outline=color, width=stroke)
        draw.line((cx, cy, cx, y1+h*0.3), fill=color, width=stroke)
        draw.line((cx, cy, x2-w*0.28, cy+h*0.12), fill=color, width=stroke)
    elif k in {"shield", "severity", "club"}:
        pts = [(cx, y1+h*0.08), (x2-w*0.15, y1+h*0.24), (x2-w*0.22, y2-h*0.2), (cx, y2-h*0.05), (x1+w*0.22, y2-h*0.2), (x1+w*0.15, y1+h*0.24)]
        draw.polygon(pts, outline=color)
        draw.line(pts+[pts[0]], fill=color, width=stroke)
    elif k in {"warning", "suspension", "reason"}:
        pts = [(cx, y1+h*0.07), (x2-w*0.08, y2-h*0.08), (x1+w*0.08, y2-h*0.08)]
        draw.polygon(pts, outline=color)
        draw.line(pts+[pts[0]], fill=color, width=stroke)
        draw.line((cx, y1+h*0.35, cx, y1+h*0.63), fill=color, width=stroke)
        draw.ellipse((cx-stroke//2, y1+h*0.72, cx+stroke//2, y1+h*0.8), fill=color)
    elif k in {"money", "fee", "price"}:
        f = font(round(h*0.72), "bold")
        draw.text((cx, cy), "$", anchor="mm", font=f, fill=color)
    elif k in {"quote", "press"}:
        f = font(round(h*0.8), "bold")
        draw.text((cx, cy), "“", anchor="mm", font=f, fill=color)
    elif k in {"transfer", "swap"}:
        draw.line((x1+w*0.12, y1+h*0.35, x2-w*0.18, y1+h*0.35), fill=color, width=stroke)
        draw.polygon([(x2-w*0.18, y1+h*0.18), (x2-w*0.04, y1+h*0.35), (x2-w*0.18, y1+h*0.52)], fill=color)
        draw.line((x2-w*0.12, y1+h*0.68, x1+w*0.18, y1+h*0.68), fill=color, width=stroke)
        draw.polygon([(x1+w*0.18, y1+h*0.51), (x1+w*0.04, y1+h*0.68), (x1+w*0.18, y1+h*0.85)], fill=color)
    elif k == "source":
        for offset in (0.22, 0.42, 0.62):
            yy = y1+h*offset
            draw.ellipse((x1+w*0.16, yy, x2-w*0.16, yy+h*0.22), outline=color, width=stroke)
        draw.line((x1+w*0.16, y1+h*0.33, x1+w*0.16, y1+h*0.75), fill=color, width=stroke)
        draw.line((x2-w*0.16, y1+h*0.33, x2-w*0.16, y1+h*0.75), fill=color, width=stroke)
    elif k == "x":
        draw.line((x1+w*0.2, y1+h*0.18, x2-w*0.2, y2-h*0.18), fill=color, width=stroke)
        draw.line((x2-w*0.2, y1+h*0.18, x1+w*0.2, y2-h*0.18), fill=color, width=stroke)
    elif k == "youtube":
        draw.rounded_rectangle((x1+w*0.08, y1+h*0.23, x2-w*0.08, y2-h*0.23), radius=20, outline=color, width=stroke)
        draw.polygon([(cx-w*0.08, cy-h*0.13), (cx-w*0.08, cy+h*0.13), (cx+w*0.15, cy)], fill=color)
    elif k == "target":
        draw.ellipse((x1+w*0.08, y1+h*0.08, x2-w*0.08, y2-h*0.08), outline=color, width=stroke)
        draw.ellipse((x1+w*0.28, y1+h*0.28, x2-w*0.28, y2-h*0.28), outline=color, width=stroke)
        draw.ellipse((cx-stroke, cy-stroke, cx+stroke, cy+stroke), fill=color)
    else:
        draw.ellipse((x1+w*0.18, y1+h*0.18, x2-w*0.18, y2-h*0.18), outline=color, width=stroke)


def draw_angled_banner(draw: ImageDraw.ImageDraw, box, fill, outline=None, width=5) -> None:
    x1, y1, x2, y2 = box
    slant = round((y2-y1)*0.25)
    pts = [(x1+slant, y1), (x2, y1), (x2-slant, y2), (x1, y2)]
    draw.polygon(pts, fill=fill)
    if outline:
        draw.line(pts+[pts[0]], fill=outline, width=width, joint="curve")
