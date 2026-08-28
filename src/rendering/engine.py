"""FPL VORTEX 4K master broadcast renderer.

The uploaded transfer, injury, suspension and press-conference graphics are the
visual authority. Header/footer geometry is fixed; central content is generated
only from verified facts in a VerificationDecision.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from PIL import Image, ImageDraw, ImageEnhance, ImageOps

from src.verification.models import EventType, VerificationDecision
from src.verification.reported_transfer_gate import is_reported_transfer, reported_status_label
from src.verification.source_registry import SourceRegistry
from .assets import (
    identity_safe_portrait,
    resolve_club_logo,
    resolve_player_image,
    resolve_player_metadata,
)
from .layout import (
    BODY_BOTTOM,
    BODY_TOP,
    CANVAS,
    CORE_TEXT_MIN,
    FOOTER_H,
    HEADER_H,
    LABEL_TEXT_MIN,
    META_TEXT_MIN,
    alpha_panel,
    clean_text,
    draw_angled_banner,
    draw_icon,
    fit_font,
    fit_wrapped_text,
    font,
    paste_contain,
    paste_cover,
    stacked_rects,
    text_width,
    truncate,
)

W, H = CANVAS
CYAN = (35, 225, 239)
WHITE = (248, 250, 255)
MUTED = (190, 193, 207)
BLACK = (0, 0, 0)
GOLD = (255, 211, 51)
MAGENTA = (245, 68, 211)
LIME = (92, 236, 75)
CORAL = (255, 102, 78)


@dataclass(frozen=True)
class Style:
    heading: str
    banner: tuple[int, int, int]
    atmosphere: tuple[int, int, int]
    stamp: str = ""


STYLES = {
    EventType.TRANSFER: Style("TRANSFER NEWS", (25, 202, 53), (30, 0, 42)),
    EventType.INJURY: Style("INJURY NEWS", (232, 10, 18), (110, 0, 0), "INJURED"),
    EventType.SUSPENSION: Style("SUSPENSION NEWS", (244, 197, 30), (60, 5, 62), "SUSPENDED"),
    EventType.PRESS_CONFERENCE: Style("PRESS CONFERENCE", (123, 38, 238), (20, 18, 120)),
}


@dataclass(frozen=True)
class Field:
    label: str
    value: str
    icon: str
    logo: Optional[Image.Image] = None


class MasterGraphicRenderer:
    def __init__(self, sources: SourceRegistry, *, fpl_data: Optional[dict] = None) -> None:
        self.sources = sources
        self.fpl_data = fpl_data
        self.branding = Path("assets/branding")

    def render(self, decision: VerificationDecision, output_path: str | Path) -> str:
        if not decision.may_publish:
            raise ValueError("cannot render an unauthorized decision")
        if decision.event_type not in STYLES:
            raise ValueError(f"unsupported graphic category: {decision.event_type.value}")

        image = self._base(decision.event_type)
        self._header(image, decision)
        self._footer(image, decision)
        if decision.event_type == EventType.PRESS_CONFERENCE:
            self._press_body(image, decision)
        else:
            self._player_body(image, decision)
        self._quality_check(image)

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, "PNG", optimize=True)
        return str(path)

    # ── Fixed master framework ──────────────────────────────────────────

    def _base(self, event: EventType) -> Image.Image:
        style = STYLES[event]
        image = Image.new("RGB", CANVAS, (0, 0, 0))
        body_box = (0, BODY_TOP, W, BODY_BOTTOM)
        texture_path = self.branding / "stadium_texture.jpg"
        if texture_path.exists():
            texture = Image.open(texture_path).convert("RGB")
            texture = ImageOps.fit(texture, (W, BODY_BOTTOM-BODY_TOP), method=Image.Resampling.LANCZOS, centering=(0.2, 0.5))
            texture = ImageEnhance.Contrast(texture).enhance(1.1)
            image.paste(texture, (0, BODY_TOP))
        draw = ImageDraw.Draw(image)

        # Category atmosphere and a right-side dark image stage.
        tint = Image.new("RGBA", CANVAS, (*style.atmosphere, 0))
        td = ImageDraw.Draw(tint)
        td.rectangle(body_box, fill=(*style.atmosphere, 92))
        for x in range(W):
            alpha = int(20 + 170 * (x / W) ** 1.8)
            td.line((x, BODY_TOP, x, BODY_BOTTOM), fill=(0, 0, 0, alpha))
        image.paste(tint, (0, 0), tint)
        draw.rectangle((0, 0, W, HEADER_H), fill=(0, 0, 0))
        draw.rectangle((0, BODY_BOTTOM, W, H), fill=(0, 0, 0))
        return image

    def _header(self, image: Image.Image, decision: VerificationDecision) -> None:
        style = STYLES[decision.event_type]
        draw = ImageDraw.Draw(image)

        full_brand = self.branding / "fpl_vortex_full.png"
        if full_brand.exists():
            paste_contain(image, Image.open(full_brand).convert("RGBA"), (55, 18, 1040, 335))
        else:
            lion_path = self.branding / "fpl_lion.jpg"
            if lion_path.exists():
                lion = Image.open(lion_path).convert("RGB")
                paste_cover(image, lion, (55, 24, 320, 325), rounded=0)
            draw.text((320, 54), "FPL", font=font(112, "condensed"), fill=(246, 204, 35))
            draw.text((320, 164), "VORTEX", font=font(126, "condensed"), fill=(90, 224, 42))

        banner = (1160, 68, 2700, 292)
        draw_angled_banner(draw, banner, style.banner, outline=(255, 255, 255), width=3)
        icon_box = (1195, 92, 1380, 268)
        draw.rounded_rectangle(icon_box, radius=28, outline=WHITE, width=7)
        draw_icon(draw, self._heading_icon(decision.event_type), (1225, 112, 1350, 245), WHITE)
        heading = style.heading
        heading_font = fit_font(draw, heading, 1210, max_size=112, min_size=72, role="condensed")
        draw.text((1435, 180), heading, anchor="lm", font=heading_font, fill=WHITE)

        pl_path = self.branding / "premier_league.png"
        if pl_path.exists():
            pl = Image.open(pl_path).convert("RGBA")
            paste_contain(image, pl, (3440, 28, 3745, 325))
        draw.line((3385, 45, 3385, 320), fill=(196, 28, 211), width=5)
        draw.line((0, HEADER_H-8, W, HEADER_H-8), fill=(123, 18, 146), width=5)
        draw.line((0, HEADER_H-3, W, HEADER_H-3), fill=CYAN, width=2)

    def _footer(self, image: Image.Image, decision: VerificationDecision) -> None:
        draw = ImageDraw.Draw(image)
        top = H - FOOTER_H
        # Three large information zones replace the old five-column marketing
        # strip.  At phone size the previous 22px minimum became ~4px and was
        # unreadable.  Every footer value now stays at the 9px mobile target.
        zones = [0, 1260, 2320, W]
        colors = [(246, 213, 33), (35, 225, 239), (92, 236, 75)]
        for index in range(3):
            x1, x2 = zones[index], zones[index+1]
            draw.polygon([(x1, top), (x2+45, top), (x2, H), (x1, H)], fill=(4, 5, 12), outline=colors[index])
            draw.line((x1, top, x2+45, top), fill=colors[index], width=6)

        source = self._visible_footer_source(decision)
        updated = self._updated_date(decision)
        self._footer_item(draw, (45, top+25, 1215, H-18), "source", "VERIFIED SOURCE", source, colors[0])
        self._footer_item(draw, (1315, top+25, 2270, H-18), "calendar", "UPDATED", updated, colors[1])
        self._footer_item(
            draw, (2385, top+25, 3795, H-18), "x", "FOLLOW FPL VORTEX",
            "@FPLVORTEXM  •  VERIFIED UPDATES", colors[2],
        )

    def _footer_item(self, draw, box, icon_kind, label, value, color) -> None:
        x1, y1, x2, y2 = box
        icon_w = 155
        text_x = x1 + icon_w + 24
        text_w = x2 - text_x - 20
        draw_icon(draw, icon_kind, (x1, y1+13, x1+icon_w, y2-13), WHITE)
        label_font = fit_font(
            draw, label, text_w, max_size=68, min_size=META_TEXT_MIN,
            role="condensed",
        )
        draw.text((text_x, y1+12), label, font=label_font, fill=color)
        value_font = fit_font(
            draw, value, text_w, max_size=62, min_size=META_TEXT_MIN,
            role="bold",
        )
        draw.text(
            (text_x, y1+112), truncate(draw, value, value_font, text_w),
            font=value_font, fill=WHITE,
        )

    # ── Player-centred body ─────────────────────────────────────────────

    def _player_body(self, image: Image.Image, decision: VerificationDecision) -> None:
        style = STYLES[decision.event_type]
        panel = (135, 420, 2390, 1865)
        visual = (2450, 405, 3715, 1885)
        alpha_panel(
            image, panel, fill=(1, 3, 10, 246), outline=(*style.banner, 255),
            width=8, radius=38, glow=True,
        )
        draw = ImageDraw.Draw(image)
        draw.line((185, 455, 2340, 455), fill=CYAN, width=3)
        draw.line((185, 1830, 2340, 1830), fill=(*style.banner,), width=3)

        heading = (200, 485, 2325, 750)
        details = (200, 790, 2325, 1810)
        self._player_heading(image, heading, decision, style)
        self._visual_stage(image, visual, decision, style)

        if decision.event_type == EventType.TRANSFER:
            self._transfer_panel(image, details, decision)
        else:
            rows = self._fields(decision)
            self._draw_rows(image, details, rows, style)

    def _player_heading(
        self,
        image: Image.Image,
        box: tuple[int, int, int, int],
        decision: VerificationDecision,
        style: Style,
    ) -> None:
        """Shared CSS-like hero strip: large name, colored surname and metadata."""
        facts = decision.verified_facts
        draw = ImageDraw.Draw(image)
        x1, y1, x2, y2 = box
        tint = tuple(max(5, round(channel * 0.13)) for channel in style.banner)
        draw.rounded_rectangle(box, radius=30, fill=tint, outline=style.banner, width=5)
        draw.rounded_rectangle((x1+18, y1+18, x1+34, y2-18), radius=8, fill=CYAN)
        draw.line((x1+55, y2-18, x2-40, y2-18), fill=(*style.banner,), width=3)

        subject = clean_text(facts.get("subject_name"), "PLAYER")
        metadata = resolve_player_metadata(subject, fpl_data=self.fpl_data)
        club = clean_text(
            facts.get("club_name") or facts.get("club_from_name")
            or facts.get("club_to_name") or metadata.get("club_name"),
            "",
        )
        age = clean_text(facts.get("age") or metadata.get("age"), "")
        kicker = [style.heading]
        if club:
            kicker.append(club.upper())
        if age:
            kicker.append(f"AGE {age}")
        kicker_text = "   •   ".join(kicker)
        kicker_font = fit_font(
            draw, kicker_text, x2-x1-130, max_size=66, min_size=META_TEXT_MIN,
            role="condensed",
        )
        kicker_text = truncate(draw, kicker_text, kicker_font, x2-x1-130)
        draw.text(
            (x1+70, y1+34), kicker_text, font=kicker_font, fill=GOLD,
            stroke_width=1, stroke_fill=(0, 0, 0),
        )

        name_font = fit_font(
            draw, subject, x2-x1-130, max_size=180, min_size=96,
            role="condensed",
        )
        display_subject = truncate(draw, subject, name_font, x2-x1-130)
        words = display_subject.split()
        first = (" ".join(words[:-1]) + " ") if len(words) > 1 else ""
        last = words[-1] if words else display_subject
        name_y = y1 + 190
        name_x = x1 + 70
        if first:
            draw.text(
                (name_x, name_y), first, anchor="lm", font=name_font, fill=WHITE,
                stroke_width=2, stroke_fill=(0, 0, 0),
            )
            name_x += text_width(draw, first, name_font)
        draw.text(
            (name_x, name_y), last, anchor="lm", font=name_font,
            fill=style.banner, stroke_width=2, stroke_fill=(0, 0, 0),
        )

    def _visual_stage(self, image: Image.Image, box, decision: VerificationDecision, style: Style) -> None:
        facts = decision.verified_facts
        draw = ImageDraw.Draw(image)
        x1, y1, x2, y2 = box
        # Layered neon geometry gives the player stage depth without changing the
        # verified image. This is the bitmap renderer's equivalent of a modern
        # HTML/CSS glass card.
        alpha_panel(image, box, fill=(0, 0, 0, 168), outline=(*style.banner, 235), width=6, radius=28, glow=True)
        glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.ellipse((x1+85, y1+150, x2+170, y2+180), outline=(*style.banner, 80), width=45)
        gd.ellipse((x1+170, y1+260, x2+70, y2+90), outline=(*CYAN, 48), width=22)
        for offset in range(-160, 520, 120):
            gd.line((x1+offset, y2-40, x1+offset+510, y1+40), fill=(*style.banner, 28), width=12)
        image.paste(glow, (0, 0), glow)

        subject = clean_text(facts.get("subject_name"), "PLAYER")
        club = facts.get("club_name") or facts.get("club_from_name") or facts.get("club_to_name")
        provider_id = (
            facts.get("provider_club_id") or facts.get("provider_from_club_id")
            or facts.get("provider_to_club_id")
        )
        crest = resolve_club_logo(str(club or ""), provider_id=provider_id, fpl_data=self.fpl_data)
        if crest:
            watermark = crest.convert("RGBA").copy()
            alpha = watermark.getchannel("A")
            alpha = alpha.point(lambda value: round(value * 0.16))
            watermark.putalpha(alpha)
            paste_contain(image, watermark, (x1+155, y1+245, x2-85, y2-185))

        player, image_source = resolve_player_image(subject, facts, fpl_data=self.fpl_data)
        inner = (x1+25, y1+30, x2-25, y2-70)
        if player:
            player = identity_safe_portrait(player, image_source)
            if image_source == "Team shirt fallback":
                # Keep the existing team-shirt fallback presentation unchanged.
                max_w = int((inner[2]-inner[0]) * 0.76)
                max_h = int((inner[3]-inner[1]) * 0.76)
                scale = min(max_w / max(1, player.width), max_h / max(1, player.height))
                resized = player.resize(
                    (max(1, round(player.width*scale)), max(1, round(player.height*scale))),
                    Image.Resampling.LANCZOS,
                )
                px = inner[0] + (inner[2]-inner[0]-resized.width)//2
                py = inner[1] + (inner[3]-inner[1]-resized.height)//2 + 35
                image.paste(resized, (px, py), resized)
            else:
                # Real verified player images keep their complete source frame and
                # natural aspect ratio inside the existing right-hand image area.
                paste_contain(image, player, inner)
        else:
            cx = (x1+x2)//2
            draw.ellipse((cx-170, y1+300, cx+170, y1+640), outline=(180,180,195), width=12)
            draw.rounded_rectangle((cx-330, y1+620, cx+330, y2-210), radius=170, outline=(180,180,195), width=12)
            if crest:
                paste_contain(image, crest, (cx-160, y1+760, cx+160, y1+1080))
            draw.text((cx, y2-165), "IMAGE UNAVAILABLE", anchor="mm", font=font(58, "condensed"), fill=MUTED)

        if image_source:
            source_labels = {
                "FPL API": ("VERIFIED FPL HEADSHOT", LIME),
                "Wikipedia": ("WIKIPEDIA IMAGE", CYAN),
                "Reliable provider": ("PROVIDER HEADSHOT", MAGENTA),
                "Team shirt fallback": ("VERIFIED TEAM SHIRT", GOLD),
            }
            source_label, source_color = source_labels.get(image_source, (image_source.upper(), CYAN))
            sf = font(META_TEXT_MIN, "condensed")
            chip_w = text_width(draw, source_label, sf) + 72
            chip = (x1+30, y1+28, min(x2-30, x1+30+chip_w), y1+116)
            draw.rounded_rectangle(chip, radius=18, fill=(0, 0, 0, 226), outline=source_color, width=4)
            draw.ellipse((chip[0]+18, chip[1]+31, chip[0]+44, chip[1]+57), fill=source_color)
            draw.text((chip[0]+54, (chip[1]+chip[3])//2), source_label, anchor="lm", font=sf, fill=source_color)

        metadata = resolve_player_metadata(subject, fpl_data=self.fpl_data)
        position = self._full_position(
            clean_text(facts.get("position") or metadata.get("position"), "")
        )
        if position:
            strip = (x1+38, y2-190, x2-38, y2-38)
            draw.rounded_rectangle(strip, radius=22, fill=(0,0,0,238), outline=style.banner, width=7)
            pfont = fit_font(
                draw, position, strip[2]-strip[0]-55,
                max_size=124, min_size=CORE_TEXT_MIN, role="condensed",
            )
            draw.text(((strip[0]+strip[2])//2, (strip[1]+strip[3])//2), position, anchor="mm", font=pfont, fill=WHITE)

        if style.stamp:
            stamp_font = font(82, "condensed")
            stamp = style.stamp
            sw = text_width(draw, stamp, stamp_font) + 90
            stamp_box = (x2-sw-18, y1+122, x2-18, y1+258)
            draw.rounded_rectangle(stamp_box, radius=14, fill=(35,0,0,230), outline=style.banner, width=9)
            draw.text(((stamp_box[0]+stamp_box[2])//2, (stamp_box[1]+stamp_box[3])//2), stamp, anchor="mm", font=stamp_font, fill=style.banner)

    def _draw_rows(self, image: Image.Image, panel, rows: Sequence[Field], style: Style) -> None:
        if not rows:
            rows = [Field("STATUS", "VERIFIED UPDATE", "status")]
        draw = ImageDraw.Draw(image)
        row_boxes = stacked_rects(panel, len(rows), gap=22, max_height=500)
        icon_w = 190
        label_w = 545
        accents = (style.banner, CYAN, GOLD, MAGENTA, LIME, CORAL)
        for index, (field, row_box) in enumerate(zip(rows, row_boxes, strict=True)):
            accent = accents[index % len(accents)]
            x1, y1, x2, y2 = row_box
            row_h = y2-y1
            row_fill = tuple(max(3, round(channel * 0.075)) for channel in accent)
            draw.rounded_rectangle(row_box, radius=25, fill=row_fill, outline=accent, width=3)
            draw.rounded_rectangle((row_box[0]+12, row_box[1]+18, row_box[0]+22, row_box[3]-18), radius=5, fill=accent)
            icon_margin_y = max(28, (row_h-150)//2)
            icon_box = (x1+35, y1+icon_margin_y, x1+icon_w-5, y2-icon_margin_y)
            if field.logo:
                paste_contain(image, field.logo, icon_box)
            else:
                draw_icon(draw, field.icon, icon_box, accent)
            label_x = x1+icon_w+15
            divider_x = label_x+label_w
            draw.line((divider_x, y1+30, divider_x, y2-30), fill=accent, width=6)
            label_max = min(112, max(84, int(row_h*0.32)))
            label_font = fit_font(
                draw, field.label.upper(), label_w-50,
                max_size=label_max, min_size=LABEL_TEXT_MIN, role="condensed",
            )
            label = truncate(draw, field.label.upper(), label_font, label_w-50)
            draw.text((label_x+20, (y1+y2)//2), label, anchor="lm", font=label_font, fill=accent)
            value_x = divider_x+48
            value_w = x2-value_x-30
            value_max = min(158, max(108, int(row_h*0.38)))
            value_font, value_lines, value_step = fit_wrapped_text(
                draw, field.value, value_w, row_h-50, 2,
                max_size=value_max, min_size=CORE_TEXT_MIN, role="bold",
            )
            value_y = (y1+y2-value_step*len(value_lines))//2
            for value_line in value_lines:
                draw.text(
                    (value_x, value_y), value_line,
                    font=value_font, fill=WHITE,
                    stroke_width=2, stroke_fill=(0, 0, 0),
                )
                value_y += value_step

    def _transfer_panel(self, image: Image.Image, panel, decision: VerificationDecision) -> None:
        facts = decision.verified_facts
        draw = ImageDraw.Draw(image)
        x1, y1, x2, y2 = panel
        origin = clean_text(facts.get("club_from_name"))
        destination = clean_text(facts.get("club_to_name"))
        from_logo = resolve_club_logo(origin, provider_id=facts.get("provider_from_club_id"), fpl_data=self.fpl_data)
        to_logo = resolve_club_logo(destination, provider_id=facts.get("provider_to_club_id"), fpl_data=self.fpl_data)

        # Direction block is a dedicated verified FROM -> TO row.
        inner_x1, inner_x2 = x1, x2
        route = (inner_x1, y1, inner_x2, y1+326)
        cx, cy = (route[0]+route[2])//2, (route[1]+route[3])//2
        left_box = (route[0], route[1], cx-125, route[3])
        right_box = (cx+125, route[1], route[2], route[3])
        draw.rounded_rectangle(left_box, radius=26, fill=(3, 10, 15), outline=CYAN, width=4)
        draw.rounded_rectangle(right_box, radius=26, fill=(4, 14, 6), outline=LIME, width=4)
        if from_logo:
            paste_contain(image, from_logo, (left_box[0]+24, left_box[1]+65, left_box[0]+215, left_box[3]-24))
        if to_logo:
            paste_contain(image, to_logo, (right_box[0]+24, right_box[1]+65, right_box[0]+215, right_box[3]-24))
        from_w = left_box[2]-left_box[0]-270
        to_w = right_box[2]-right_box[0]-270
        from_font = fit_font(
            draw, origin, from_w, max_size=102, min_size=CORE_TEXT_MIN,
            role="bold",
        )
        to_font = fit_font(
            draw, destination, to_w, max_size=102, min_size=CORE_TEXT_MIN,
            role="bold",
        )
        draw.text((left_box[0]+250, left_box[1]+52), "FROM", font=font(META_TEXT_MIN, "condensed"), fill=CYAN)
        draw.text((right_box[0]+250, right_box[1]+52), "TO", font=font(META_TEXT_MIN, "condensed"), fill=LIME)
        draw.text((left_box[0]+250, cy+42), truncate(draw, origin, from_font, from_w), anchor="lm", font=from_font, fill=WHITE)
        draw.text((right_box[0]+250, cy+42), truncate(draw, destination, to_font, to_w), anchor="lm", font=to_font, fill=WHITE)
        draw.ellipse((cx-90, cy-90, cx+90, cy+90), fill=(0, 0, 0), outline=GOLD, width=7)
        for offset in (-42, 8):
            pts = [(cx+offset, cy-48), (cx+offset+50, cy), (cx+offset, cy+48), (cx+offset+20, cy)]
            draw.polygon(pts, fill=GOLD)
        y = route[3] + 24

        values: list[Field] = []
        if facts.get("fee"):
            values.append(Field("TRANSFER FEE", clean_text(facts.get("fee")), "money"))
        if facts.get("contract_length"):
            values.append(Field("CONTRACT TERM", clean_text(facts.get("contract_length")), "calendar"))
        if facts.get("transfer_kind"):
            values.append(Field("DEAL TYPE", clean_text(facts.get("transfer_kind")), "status"))
        if facts.get("market_value") and len(values) < 3:
            values.append(Field("MARKET VALUE", clean_text(facts.get("market_value")), "money"))
        if is_reported_transfer(decision) and len(values) < 3:
            values.append(Field("STATUS", reported_status_label(decision.status, facts), "status"))
        elif len(values) < 3:
            status = str(getattr(decision.status, "value", decision.status) or "OFFICIAL").upper()
            values.append(Field("STATUS", "OFFICIAL" if status in {"OFFICIAL", "COMPLETED"} else status, "status"))
        visible_values = values[:3]
        row_boxes = stacked_rects((inner_x1, y, inner_x2, y2), len(visible_values), gap=20)
        accents = (GOLD, MAGENTA, CYAN)
        for index, (field, row_box) in enumerate(zip(visible_values, row_boxes, strict=True)):
            self._draw_single_transfer_row(
                image, row_box, field,
                accent=accents[index % len(accents)],
            )

    def _draw_single_transfer_row(self, image: Image.Image, box, field: Field, *, accent=CYAN) -> None:
        draw = ImageDraw.Draw(image)
        x1, y1, x2, y2 = box
        icon_w, label_w = 165, 535
        row_fill = tuple(max(3, round(channel * 0.075)) for channel in accent)
        draw.rounded_rectangle(box, radius=24, fill=row_fill, outline=accent, width=4)
        draw.rounded_rectangle((x1+12, y1+18, x1+22, y2-18), radius=5, fill=accent)
        if field.logo:
            paste_contain(image, field.logo, (x1+34, y1+24, x1+icon_w-12, y2-24))
        else:
            draw_icon(draw, field.icon, (x1+42, y1+30, x1+icon_w-30, y2-30), accent)
        draw.line((x1+icon_w, y1+18, x1+icon_w, y2-18), fill=accent, width=5)
        draw.line((x1+icon_w+label_w, y1+30, x1+icon_w+label_w, y2-30), fill=accent, width=5)
        lf = fit_font(
            draw, field.label.upper(), label_w-50,
            max_size=104, min_size=LABEL_TEXT_MIN, role="condensed",
        )
        label = truncate(draw, field.label.upper(), lf, label_w-50)
        draw.text((x1+icon_w+34, (y1+y2)//2), label, anchor="lm", font=lf, fill=accent)
        value_x = x1+icon_w+label_w+50
        max_w = x2-value_x-35
        vf = fit_font(
            draw, field.value, max_w,
            max_size=148, min_size=CORE_TEXT_MIN, role="bold",
        )
        draw.text(
            (value_x, (y1+y2)//2), truncate(draw, field.value, vf, max_w),
            anchor="lm", font=vf, fill=WHITE,
            stroke_width=2, stroke_fill=(0, 0, 0),
        )

    # ── Press-conference body ───────────────────────────────────────────

    def _press_body(self, image: Image.Image, decision: VerificationDecision) -> None:
        facts = decision.verified_facts
        top, bottom = 410, 1870
        gap = 35
        widths = (1320, 1260, 1050)
        x = 35
        panels: list[tuple[int, int, int, int]] = []
        for width in widths:
            panels.append((x, top, x + width, bottom))
            x += width + gap
        for panel in panels:
            alpha_panel(
                image, panel, fill=(3, 0, 20, 238),
                outline=(146, 55, 255, 255), width=6, radius=34, glow=True,
            )

        latest = self._list_value(facts.get("latest_news"))
        if not latest:
            latest = [
                f"{clean_text(facts.get('subject_name'), 'Speaker')} — "
                f"{clean_text(facts.get('club_name'), 'Club')}",
                clean_text(facts.get("quote_topic"), "Press conference update"),
            ]
        quotes = self._list_value(facts.get("key_quotes"))
        if not quotes:
            quotes = [clean_text(facts.get("quote_summary"), "Verified quote unavailable")]
        notes = self._list_value(facts.get("manager_notes"))
        roundup = self._list_value(facts.get("roundup"))
        if not roundup:
            roundup = [
                f"{clean_text(facts.get('club_name'), 'Club')} — "
                f"{clean_text(facts.get('subject_name'), 'Speaker')}",
                f"Topic: {clean_text(facts.get('quote_topic'), 'General update')}",
            ]

        # This is the owner's approved press-conference reference structure:
        # latest news | key quotes/manager notes | the complete official
        # manager round-up.  The final column intentionally adapts down to the
        # number of official entries instead of hiding clubs behind a "+ more"
        # summary row.
        self._press_column(
            image, panels[0], "LATEST NEWS", latest,
            icon="calendar", accent=(85, 89, 255),
        )
        self._press_quotes(image, panels[1], quotes, notes)
        self._press_roundup_column(image, panels[2], roundup)

    @staticmethod
    def _press_row_gap(count: int) -> int:
        if count >= 16:
            return 4
        if count >= 10:
            return 7
        return 14

    def _press_header(self, image, box, title, *, icon, accent) -> int:
        draw = ImageDraw.Draw(image)
        x1, y1, x2, _y2 = box
        header = (x1+30, y1+24, x2-30, y1+158)
        draw.rounded_rectangle(
            header, radius=28, fill=accent, outline=(215, 180, 255), width=4,
        )
        draw_icon(
            draw, icon,
            (header[0]+22, header[1]+18, header[0]+120, header[3]-18), WHITE,
        )
        title_x = header[0]+140
        title_w = header[2]-title_x-20
        title_font = fit_font(
            draw, title, title_w, max_size=64, min_size=38, role="condensed",
        )
        draw.text(
            (title_x, (header[1]+header[3])//2),
            truncate(draw, title, title_font, title_w),
            anchor="lm", font=title_font, fill=WHITE,
        )
        return y1+185

    def _press_column(self, image, box, title, items, *, icon, accent) -> None:
        draw = ImageDraw.Draw(image)
        x1, _y1, x2, y2 = box
        content_top = self._press_header(
            image, box, title, icon=icon, accent=accent,
        )
        visible_items = list(items) or ["Verified update unavailable"]
        gap = self._press_row_gap(len(visible_items))
        rows = stacked_rects(
            (x1+30, content_top, x2-30, y2-30),
            len(visible_items), gap=gap,
            max_height=145 if len(visible_items) <= 8 else None,
        )
        for index, (item, row) in enumerate(
            zip(visible_items, rows, strict=True), start=1,
        ):
            draw.rounded_rectangle(
                row, radius=min(22, max(10, (row[3]-row[1])//4)),
                fill=(13, 7, 38), outline=accent, width=3,
            )
            row_h = row[3]-row[1]
            badge_d = min(64, max(34, row_h-18))
            badge_x = row[0]+18
            badge_y = (row[1]+row[3])//2
            badge = (
                badge_x, badge_y-badge_d//2,
                badge_x+badge_d, badge_y+badge_d//2,
            )
            draw.ellipse(badge, fill=accent)
            draw.text(
                ((badge[0]+badge[2])//2, badge_y), str(index), anchor="mm",
                font=font(min(38, max(24, badge_d-22)), "bold"), fill=WHITE,
            )
            text_x = badge[2]+20
            text_w = row[2]-text_x-20
            max_lines = 2 if row_h >= 95 else 1
            tf, lines, step = fit_wrapped_text(
                draw, item, text_w, row_h-16, max_lines,
                max_size=47, min_size=28, role="bold",
            )
            text_y = (row[1]+row[3]-step*len(lines))//2
            for line in lines:
                draw.text((text_x, text_y), line, font=tf, fill=WHITE)
                text_y += step

    def _press_quotes(self, image, box, quotes, notes) -> None:
        draw = ImageDraw.Draw(image)
        x1, _y1, x2, y2 = box
        content_top = self._press_header(
            image, box, "KEY QUOTES", icon="quote", accent=(117, 32, 224),
        )
        visible_quotes = list(quotes[:4]) or ["Verified quote unavailable"]
        visible_notes = list(notes[:3])
        notes_height = 345 if visible_notes else 0
        quote_bottom = y2-30-notes_height-(20 if visible_notes else 0)
        quote_rows = stacked_rects(
            (x1+30, content_top, x2-30, quote_bottom),
            len(visible_quotes), gap=14, max_height=245,
        )
        for quote, quote_box in zip(visible_quotes, quote_rows, strict=True):
            draw.rounded_rectangle(
                quote_box, radius=24, fill=(13, 7, 38),
                outline=(154, 70, 246), width=3,
            )
            draw.text(
                (quote_box[0]+22, quote_box[1]+12), "“",
                font=font(min(80, max(48, quote_box[3]-quote_box[1]-35)), "bold"),
                fill=GOLD,
            )
            text_x = quote_box[0]+92
            text_w = quote_box[2]-text_x-24
            qf, lines, step = fit_wrapped_text(
                draw, quote, text_w, quote_box[3]-quote_box[1]-24, 3,
                max_size=47, min_size=30, role="regular",
            )
            text_y = (quote_box[1]+quote_box[3]-step*len(lines))//2
            for line in lines:
                draw.text((text_x, text_y), line, font=qf, fill=WHITE)
                text_y += step

        if not visible_notes:
            return
        notes_top = quote_bottom+20
        draw.text(
            (x1+45, notes_top+8), "MANAGER'S NOTES",
            font=font(52, "condensed"), fill=GOLD,
        )
        note_rows = stacked_rects(
            (x1+45, notes_top+82, x2-35, y2-30),
            len(visible_notes), gap=8,
        )
        for note, row in zip(visible_notes, note_rows, strict=True):
            middle = (row[1]+row[3])//2
            draw.ellipse((row[0], middle-10, row[0]+20, middle+10), fill=GOLD)
            note_x = row[0]+42
            note_w = row[2]-note_x
            nf = fit_font(
                draw, note, note_w, max_size=39, min_size=28, role="regular",
            )
            draw.text(
                (note_x, middle), truncate(draw, note, nf, note_w),
                anchor="lm", font=nf, fill=WHITE,
            )

    @staticmethod
    def _roundup_club(item: str) -> str:
        for separator in (" — ", " – ", " - "):
            if separator in item:
                return item.split(separator, 1)[0].strip()
        return ""

    def _press_roundup_column(self, image, box, roundup) -> None:
        draw = ImageDraw.Draw(image)
        x1, _y1, x2, y2 = box
        content_top = self._press_header(
            image, box, "PRESS CONFERENCE ROUND-UP",
            icon="press", accent=(165, 64, 255),
        )
        items = list(roundup) or ["Verified round-up unavailable"]
        gap = self._press_row_gap(len(items))
        rows = stacked_rects(
            (x1+20, content_top, x2-20, y2-24), len(items), gap=gap,
            max_height=105 if len(items) <= 10 else None,
        )
        for index, (item, row) in enumerate(zip(items, rows, strict=True), start=1):
            self._draw_press_roundup_row(image, draw, row, index, item)

    def _draw_press_roundup_row(self, image, draw, row, index, item) -> None:
        row_h = row[3]-row[1]
        radius = min(20, max(8, row_h//4))
        draw.rounded_rectangle(
            row, radius=radius, fill=(8, 5, 28),
            outline=(140, 52, 238), width=2,
        )

        badge_d = min(58, max(32, row_h-12))
        middle = (row[1]+row[3])//2
        badge = (
            row[0]+8, middle-badge_d//2,
            row[0]+8+badge_d, middle+badge_d//2,
        )
        draw.ellipse(badge, fill=(101, 35, 225), outline=(215, 180, 255), width=2)
        draw.text(
            ((badge[0]+badge[2])//2, middle), str(index), anchor="mm",
            font=font(min(35, max(22, badge_d-20)), "bold"), fill=WHITE,
        )

        logo_x = badge[2]+8
        logo_size = min(52, max(30, row_h-12))
        club = self._roundup_club(item)
        crest = resolve_club_logo(club, fpl_data=self.fpl_data) if club else None
        if crest:
            paste_contain(
                image, crest,
                (logo_x, middle-logo_size//2, logo_x+logo_size, middle+logo_size//2),
            )
        else:
            draw.ellipse(
                (logo_x+5, middle-8, logo_x+21, middle+8), fill=CYAN,
            )

        text_x = logo_x+logo_size+8
        text_w = row[2]-text_x-10
        text_size = min(42, max(27, int(row_h*0.52)))
        tf = fit_font(
            draw, item, text_w, max_size=text_size, min_size=25, role="bold",
        )
        draw.text(
            (text_x, middle), truncate(draw, item, tf, text_w),
            anchor="lm", font=tf, fill=WHITE,
        )

    # ── Data projection ─────────────────────────────────────────────────

    def _fields(self, decision: VerificationDecision) -> list[Field]:
        facts = decision.verified_facts
        event = decision.event_type
        rows: list[Field] = []
        if event == EventType.INJURY:
            if facts.get("injury_status"):
                rows.append(Field("INJURY", clean_text(facts.get("injury_status")), "injury"))
            if facts.get("availability_status"):
                rows.append(Field("STATUS", clean_text(facts.get("availability_status")), "status"))
            if facts.get("return_date"):
                rows.append(Field("EXPECTED RETURN", clean_text(facts.get("return_date")), "clock"))
            if facts.get("severity"):
                rows.append(Field("SEVERITY", clean_text(facts.get("severity")), "shield"))
            if facts.get("club_name"):
                club = clean_text(facts.get("club_name"))
                crest = resolve_club_logo(club, provider_id=facts.get("provider_club_id"), fpl_data=self.fpl_data)
                rows.append(Field("CLUB", club, "club", crest))
        elif event == EventType.SUSPENSION:
            if facts.get("club_name"):
                club = clean_text(facts.get("club_name"))
                crest = resolve_club_logo(club, provider_id=facts.get("provider_club_id"), fpl_data=self.fpl_data)
                rows.append(Field("CLUB", club, "club", crest))
            if facts.get("suspension_length"):
                rows.append(Field("SUSPENSION", clean_text(facts.get("suspension_length")), "warning"))
            if facts.get("matches_to_miss"):
                rows.append(Field("MATCHES TO MISS", clean_text(facts.get("matches_to_miss")), "calendar"))
            if facts.get("suspension_status"):
                rows.append(Field("REASON", clean_text(facts.get("suspension_status")), "reason"))
            if facts.get("return_date"):
                rows.append(Field("RETURN DATE", clean_text(facts.get("return_date")), "clock"))
        return rows

    def _visible_footer_source(self, decision: VerificationDecision) -> str:
        ids = set(decision.authority_source_ids or decision.source_ids)
        if "official.fpl" in ids:
            return "FPL API"
        if "official.premier_league" in ids:
            return "PREMIERLEAGUE.COM"
        # Per master rule, secondary/fallback sources retain FPL VORTEX branding.
        return "FPL VORTEX"

    @staticmethod
    def _updated_date(decision: VerificationDecision) -> str:
        try:
            parsed = datetime.fromisoformat(str(decision.created_at).replace("Z", "+00:00"))
        except Exception:
            parsed = datetime.now(timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%d %b %Y").upper()

    @staticmethod
    def _heading_icon(event: EventType) -> str:
        return {
            EventType.TRANSFER: "transfer",
            EventType.INJURY: "injury",
            EventType.SUSPENSION: "warning",
            EventType.PRESS_CONFERENCE: "press",
        }[event]

    @staticmethod
    def _full_position(value: str) -> str:
        normalized = str(value or "").strip().upper().replace("-", " ")
        if not normalized:
            return ""
        compact = normalized.replace(" ", "")
        if compact in {"GK", "GKP", "GOALKEEPER"}:
            return "GOALKEEPER"
        if compact in {"DEF", "CB", "LB", "RB", "LWB", "RWB", "SW", "DEFENDER"}:
            return "DEFENDER"
        if compact in {
            "MID", "CM", "CDM", "DM", "CAM", "AM", "LM", "RM", "LW", "RW",
            "MIDFIELDER", "CENTRALMIDFIELDER", "ATTACKINGMIDFIELDER",
            "DEFENSIVEMIDFIELDER", "WINGER",
        }:
            return "MIDFIELDER"
        if compact in {"FWD", "FW", "ST", "CF", "STRIKER", "FORWARD"}:
            return "FORWARD"
        # Already descriptive provider roles remain readable and are not reduced
        # to abbreviations.
        return normalized

    @staticmethod
    def _list_value(value: Any) -> list[str]:
        if isinstance(value, (list, tuple)):
            return [clean_text(item, "") for item in value if clean_text(item, "")]
        if isinstance(value, str) and value.strip():
            return [line.strip(" •-") for line in value.splitlines() if line.strip(" •-")]
        return []

    @staticmethod
    def _quality_check(image: Image.Image) -> None:
        if image.size != CANVAS:
            raise RuntimeError(f"invalid canvas: {image.size}")
        if image.mode != "RGB":
            raise RuntimeError(f"invalid color mode: {image.mode}")
