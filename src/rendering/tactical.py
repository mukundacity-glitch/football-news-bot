"""Locked-brand 4K renderer for verified Premier League tactical posts.

Only the central content area and the central heading text change. Header/footer
geometry and the established FPL Vortex palette are preserved from the master
renderer. No external image is downloaded here: if an approved local player or
club asset is unavailable, the renderer uses an original pitch diagram.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from PIL import Image, ImageDraw, ImageEnhance, ImageOps

from .layout import (
    BODY_BOTTOM,
    BODY_TOP,
    CANVAS,
    FOOTER_H,
    HEADER_H,
    alpha_panel,
    draw_angled_banner,
    draw_icon,
    fit_font,
    fit_wrapped_text,
    font,
    paste_contain,
    text_width,
    truncate,
)

W, H = CANVAS
CYAN = (35, 225, 239)
WHITE = (248, 250, 255)
MUTED = (190, 193, 207)
BLACK = (0, 0, 0)
GOLD = (246, 213, 33)
LIME = (92, 236, 75)
MAGENTA = (196, 28, 211)

ALLOWED_HEADINGS = {
    "TACTICAL WATCH",
    "TACTICAL REVIEW",
    "MATCHDAY TACTICAL ALERT",
    "THE DETAIL THAT CHANGED IT",
    "ONE TACTICAL EDGE",
    "TACTICAL TREND",
    "FPL TACTICAL WATCH",
}


def _safe_heading(value: str) -> str:
    heading = " ".join(str(value or "").split()).upper()
    if heading not in ALLOWED_HEADINGS:
        raise ValueError(f"unapproved tactical heading: {heading}")
    return heading


def _words(value: str) -> int:
    return len([part for part in str(value or "").split() if part])


class TacticalGraphicRenderer:
    def __init__(self) -> None:
        self.branding = Path("assets/branding")

    def render(self, post: Mapping[str, object], output_path: str | Path) -> str:
        heading = _safe_heading(str(post.get("heading") or ""))
        thesis = str(post.get("thesis") or "").strip()
        explanation = str(post.get("explanation") or "").strip()
        evidence = list(post.get("evidence") or [])[:3]
        if not 8 <= _words(thesis) <= 14:
            raise ValueError("tactical thesis must contain 8-14 words")
        if _words(explanation) > 28:
            raise ValueError("tactical explanation exceeds 28 words")
        if len(evidence) > 3:
            raise ValueError("maximum three evidence cards")

        image = self._base()
        self._header(image, heading)
        self._footer(image, post)
        self._body(image, post, evidence)
        if image.size != CANVAS:
            raise RuntimeError(f"renderer escaped 4K canvas: {image.size}")

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        image.convert("RGB").save(path, "PNG", optimize=True)
        return str(path)

    def _base(self) -> Image.Image:
        image = Image.new("RGB", CANVAS, BLACK)
        texture_path = self.branding / "stadium_texture.jpg"
        if texture_path.exists():
            texture = Image.open(texture_path).convert("RGB")
            texture = ImageOps.fit(
                texture,
                (W, BODY_BOTTOM - BODY_TOP),
                method=Image.Resampling.LANCZOS,
                centering=(0.2, 0.5),
            )
            texture = ImageEnhance.Contrast(texture).enhance(1.1)
            image.paste(texture, (0, BODY_TOP))
        tint = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
        td = ImageDraw.Draw(tint)
        td.rectangle((0, BODY_TOP, W, BODY_BOTTOM), fill=(20, 18, 120, 72))
        for x in range(W):
            alpha = int(20 + 170 * (x / W) ** 1.8)
            td.line((x, BODY_TOP, x, BODY_BOTTOM), fill=(0, 0, 0, alpha))
        image.paste(tint, (0, 0), tint)
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, W, HEADER_H), fill=BLACK)
        draw.rectangle((0, BODY_BOTTOM, W, H), fill=BLACK)
        return image

    def _header(self, image: Image.Image, heading: str) -> None:
        draw = ImageDraw.Draw(image)
        full_brand = self.branding / "fpl_vortex_full.png"
        if full_brand.exists():
            paste_contain(image, Image.open(full_brand).convert("RGBA"), (55, 18, 1040, 335))
        banner = (1160, 68, 2700, 292)
        draw_angled_banner(draw, banner, (123, 38, 238), outline=WHITE, width=3)
        icon_box = (1195, 92, 1380, 268)
        draw.rounded_rectangle(icon_box, radius=28, outline=WHITE, width=7)
        draw_icon(draw, "analysis", (1225, 112, 1350, 245), WHITE)
        heading_font = fit_font(draw, heading, 1210, max_size=112, min_size=72, role="condensed")
        draw.text((1435, 180), heading, anchor="lm", font=heading_font, fill=WHITE)
        pl_path = self.branding / "premier_league.png"
        if pl_path.exists():
            paste_contain(image, Image.open(pl_path).convert("RGBA"), (3440, 28, 3745, 325))
        draw.line((3385, 45, 3385, 320), fill=MAGENTA, width=5)
        draw.line((0, HEADER_H - 8, W, HEADER_H - 8), fill=(123, 18, 146), width=5)
        draw.line((0, HEADER_H - 3, W, HEADER_H - 3), fill=CYAN, width=2)

    def _footer(self, image: Image.Image, post: Mapping[str, object]) -> None:
        draw = ImageDraw.Draw(image)
        top = H - FOOTER_H
        zones = [0, 1260, 2320, W]
        colors = [GOLD, CYAN, LIME]
        for index in range(3):
            x1, x2 = zones[index], zones[index + 1]
            draw.polygon(
                [(x1, top), (x2 + 45, top), (x2, H), (x1, H)],
                fill=(4, 5, 12),
                outline=colors[index],
            )
            draw.line((x1, top, x2 + 45, top), fill=colors[index], width=6)
        source = str(post.get("source_label") or "VERIFIED DATA")
        checked = str(post.get("checked_local") or "")
        status = str(post.get("status") or "ANALYSIS").upper()
        self._footer_item(draw, (45, top + 25, 1215, H - 18), "source", "VERIFIED SOURCE", source, colors[0])
        self._footer_item(draw, (1315, top + 25, 2270, H - 18), "clock", "DATA CHECKED", checked, colors[1])
        self._footer_item(draw, (2385, top + 25, 3795, H - 18), "x", status, "@FPLVORTEXM", colors[2])

    def _footer_item(self, draw, box, icon_kind, label, value, color) -> None:
        x1, y1, x2, y2 = box
        icon_w = 155
        text_x = x1 + icon_w + 24
        text_w = x2 - text_x - 20
        draw_icon(draw, icon_kind, (x1, y1 + 13, x1 + icon_w, y2 - 13), WHITE)
        label_font = fit_font(draw, label, text_w, max_size=68, min_size=54, role="condensed")
        draw.text((text_x, y1 + 12), label, font=label_font, fill=color)
        value_font = fit_font(draw, value, text_w, max_size=62, min_size=54, role="bold")
        draw.text((text_x, y1 + 112), truncate(draw, value, value_font, text_w), font=value_font, fill=WHITE)

    def _body(self, image: Image.Image, post: Mapping[str, object], evidence: Sequence[object]) -> None:
        draw = ImageDraw.Draw(image)
        left = (120, 420, 2440, 1870)
        right = (2505, 420, 3720, 1870)
        alpha_panel(image, left, fill=(1, 3, 10, 246), outline=(*CYAN, 255), width=7, radius=38, glow=True)
        alpha_panel(image, right, fill=(1, 3, 10, 235), outline=(123, 38, 238, 255), width=7, radius=38, glow=True)

        topic = str(post.get("topic_line") or "")
        topic_font = fit_font(draw, topic, 2150, max_size=64, min_size=56, role="bold")
        draw.text((190, 500), truncate(draw, topic, topic_font, 2150), font=topic_font, fill=GOLD)

        thesis = str(post.get("thesis") or "")
        thesis_font, thesis_lines, thesis_step = fit_wrapped_text(
            draw, thesis, 2130, 250, 3, max_size=76, min_size=64, role="bold", line_spacing=1.05
        )
        y = 620
        for line in thesis_lines:
            draw.text((190, y), line, font=thesis_font, fill=WHITE)
            y += thesis_step

        explanation = str(post.get("explanation") or "")
        exp_font, exp_lines, exp_step = fit_wrapped_text(
            draw, explanation, 2130, 190, 3, max_size=46, min_size=40, role="regular", line_spacing=1.2
        )
        y = 910
        for line in exp_lines:
            draw.text((190, y), line, font=exp_font, fill=MUTED)
            y += exp_step

        card_y = 1210
        card_gap = 24
        card_w = 680
        for idx, item in enumerate(evidence[:3]):
            x1 = 180 + idx * (card_w + card_gap)
            box = (x1, card_y, x1 + card_w, 1765)
            draw.rounded_rectangle(box, radius=28, fill=(7, 10, 20), outline=CYAN, width=4)
            if isinstance(item, Mapping):
                label = str(item.get("label") or "EVIDENCE").upper()
                value = str(item.get("value") or "")
            else:
                label, value = "EVIDENCE", str(item)
            label_font = fit_font(draw, label, card_w - 70, max_size=34, min_size=30, role="bold")
            draw.text((x1 + 34, card_y + 35), label, font=label_font, fill=CYAN)
            value_font, value_lines, value_step = fit_wrapped_text(
                draw, value, card_w - 70, 330, 4, max_size=74, min_size=44, role="bold", line_spacing=1.08
            )
            vy = card_y + 130
            for line in value_lines:
                draw.text((x1 + 34, vy), line, font=value_font, fill=WHITE)
                vy += value_step

        self._pitch(draw, right, post)

    def _pitch(self, draw: ImageDraw.ImageDraw, box, post: Mapping[str, object]) -> None:
        x1, y1, x2, y2 = box
        pad = 85
        pitch = (x1 + pad, y1 + 120, x2 - pad, y2 - 145)
        px1, py1, px2, py2 = pitch
        draw.rounded_rectangle(pitch, radius=24, outline=WHITE, width=8)
        mid = (py1 + py2) // 2
        draw.line((px1, mid, px2, mid), fill=WHITE, width=6)
        cx = (px1 + px2) // 2
        draw.ellipse((cx - 120, mid - 120, cx + 120, mid + 120), outline=WHITE, width=6)
        draw.rectangle((cx - 210, py1, cx + 210, py1 + 180), outline=WHITE, width=6)
        draw.rectangle((cx - 210, py2 - 180, cx + 210, py2), outline=WHITE, width=6)

        direction = str(post.get("diagram_focus") or "pressure").casefold()
        if direction == "set_piece":
            points = [(cx - 250, py1 + 230), (cx, py1 + 250), (cx + 250, py1 + 220), (cx, py1 + 430)]
        elif direction == "possession":
            points = [(cx - 260, mid - 280), (cx + 250, mid - 120), (cx - 120, mid + 120), (cx + 280, mid + 280)]
        else:
            points = [(cx - 260, mid + 330), (cx, mid + 150), (cx + 260, mid - 40), (cx, mid - 300)]
        for px, py in points:
            draw.ellipse((px - 28, py - 28, px + 28, py + 28), fill=CYAN, outline=WHITE, width=4)
        for a, b in zip(points, points[1:]):
            draw.line((a[0], a[1], b[0], b[1]), fill=GOLD, width=10)

        label = str(post.get("diagram_label") or "TACTICAL PATTERN").upper()
        fnt = fit_font(draw, label, x2 - x1 - 120, max_size=54, min_size=42, role="condensed")
        draw.text(((x1 + x2) // 2, y1 + 52), label, anchor="mm", font=fnt, fill=GOLD)
        score = str(post.get("score_label") or "")
        if score:
            sf = fit_font(draw, score, x2 - x1 - 120, max_size=44, min_size=34, role="bold")
            draw.text(((x1 + x2) // 2, y2 - 72), score, anchor="mm", font=sf, fill=MUTED)
