"""FPL VORTEX 4K master broadcast renderer.

The uploaded transfer, injury, suspension and press-conference graphics are the
visual authority. Header/footer geometry is fixed; central content is generated
only from verified facts in a VerificationDecision.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from src.verification.models import EventType, VerificationDecision
from src.verification.reported_transfer_gate import is_reported_transfer, reported_status_label
from src.verification.source_registry import SourceRegistry
from .assets import resolve_club_logo, resolve_player_image, resolve_player_metadata
from .layout import (
    BODY_BOTTOM,
    BODY_TOP,
    CANVAS,
    FOOTER_H,
    HEADER_H,
    alpha_panel,
    clean_text,
    draw_angled_banner,
    draw_icon,
    fit_font,
    font,
    paste_contain,
    paste_cover,
    text_width,
    truncate,
    wrap_text,
)

W, H = CANVAS
CYAN = (35, 225, 239)
WHITE = (248, 250, 255)
MUTED = (190, 193, 207)
BLACK = (0, 0, 0)


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
        zones = [0, 900, 1570, 2130, 3260, W]
        colors = [(246, 213, 33), (228, 40, 210), (247, 211, 35), (62, 225, 59), (248, 45, 39)]
        for index in range(5):
            x1, x2 = zones[index], zones[index+1]
            draw.polygon([(x1, top), (x2+45, top), (x2, H), (x1, H)], fill=(4, 5, 12), outline=colors[index])
            draw.line((x1, top, x2+45, top), fill=colors[index], width=6)

        source = self._visible_footer_source(decision)
        updated = self._updated_date(decision)
        self._footer_item(draw, (40, top+28, 870, H-20), "source", "SOURCE:", source, colors[0])
        self._footer_item(draw, (935, top+28, 1540, H-20), "x", "FOLLOW ON X", "@FPLVORTEXM", colors[1])
        self._footer_item(draw, (1615, top+28, 2095, H-20), "calendar", "UPDATED:", updated, colors[2])
        self._footer_item(draw, (2180, top+28, 3225, H-20), "target", "DATA-DRIVEN DECISIONS", "ANALYTICS · INSIGHTS · TRENDS · WINNING STRATEGY", colors[3])
        self._footer_item(draw, (3310, top+28, 3805, H-20), "youtube", "YOUTUBE", "@FPLVORTEX", colors[4])

    def _footer_item(self, draw, box, icon_kind, label, value, color) -> None:
        x1, y1, x2, y2 = box
        icon_w = 125
        draw_icon(draw, icon_kind, (x1, y1+18, x1+icon_w, y2-18), WHITE)
        label_font = fit_font(draw, label, x2-x1-icon_w-25, max_size=48, min_size=30, role="condensed")
        draw.text((x1+icon_w+18, y1+18), label, font=label_font, fill=color)
        value_font = fit_font(draw, value, x2-x1-icon_w-25, max_size=35, min_size=22, role="bold")
        draw.text((x1+icon_w+18, y1+92), truncate(draw, value, value_font, x2-x1-icon_w-25), font=value_font, fill=WHITE)

    # ── Player-centred body ─────────────────────────────────────────────

    def _player_body(self, image: Image.Image, decision: VerificationDecision) -> None:
        facts = decision.verified_facts
        style = STYLES[decision.event_type]
        panel = (230, 425, 2330, 1850)
        visual = (2380, 390, 3710, 1885)
        alpha_panel(image, panel, fill=(0,0,0,244), outline=(*CYAN,255), width=7, radius=36, glow=True)
        self._visual_stage(image, visual, decision, style)

        if decision.event_type == EventType.TRANSFER:
            self._transfer_panel(image, panel, decision)
        else:
            rows = self._fields(decision)
            self._draw_rows(image, panel, rows)

    def _visual_stage(self, image: Image.Image, box, decision: VerificationDecision, style: Style) -> None:
        facts = decision.verified_facts
        draw = ImageDraw.Draw(image)
        x1, y1, x2, y2 = box
        # Dark stage remains behind the player and preserves atmosphere.
        alpha_panel(image, box, fill=(0,0,0,135), outline=(*style.banner,210), width=4, radius=20, glow=True)
        subject = clean_text(facts.get("subject_name"), "PLAYER")
        player, _source = resolve_player_image(subject, facts, fpl_data=self.fpl_data)
        inner = (x1+25, y1+25, x2-25, y2-65)
        if player:
            alpha = player.getchannel("A") if player.mode == "RGBA" else None
            transparent = bool(alpha and alpha.getextrema()[0] < 10)
            if transparent:
                max_w = int((inner[2]-inner[0]) * 0.88)
                max_h = int((inner[3]-inner[1]) * 0.88)
                scale = min(max_w / max(1, player.width), max_h / max(1, player.height))
                # Official/FotMob headshots are often only 250px. They are still
                # the highest-priority verified identity image, so upscale with
                # Lanczos and center rather than leaving a tiny head at the foot.
                resized = player.resize(
                    (max(1, round(player.width*scale)), max(1, round(player.height*scale))),
                    Image.Resampling.LANCZOS,
                )
                px = inner[0] + (inner[2]-inner[0]-resized.width)//2
                if resized.height <= resized.width * 1.15:
                    py = inner[1] + (inner[3]-inner[1]-resized.height)//2
                else:
                    py = inner[3] - resized.height
                image.paste(resized, (px, py), resized)
            else:
                paste_cover(image, player, inner, rounded=18)
        else:
            club = facts.get("club_to_name") or facts.get("club_name") or facts.get("club_from_name")
            provider_id = facts.get("provider_to_club_id") or facts.get("provider_from_club_id")
            crest = resolve_club_logo(str(club or ""), provider_id=provider_id, fpl_data=self.fpl_data)
            cx = (x1+x2)//2
            draw.ellipse((cx-170, y1+300, cx+170, y1+640), outline=(180,180,195), width=12)
            draw.rounded_rectangle((cx-330, y1+620, cx+330, y2-210), radius=170, outline=(180,180,195), width=12)
            if crest:
                paste_contain(image, crest, (cx-160, y1+760, cx+160, y1+1080))
            draw.text((cx, y2-165), "REAL IMAGE UNAVAILABLE", anchor="mm", font=font(42, "condensed"), fill=MUTED)

        metadata = resolve_player_metadata(subject, fpl_data=self.fpl_data)
        position = clean_text(facts.get("position") or metadata.get("position"), "")
        if position:
            strip = (x1+40, y2-185, x2-40, y2-45)
            draw.rounded_rectangle(strip, radius=20, fill=(0,0,0,225), outline=WHITE, width=3)
            pfont = fit_font(draw, position.upper(), strip[2]-strip[0]-50, max_size=78, min_size=45, role="condensed")
            draw.text(((strip[0]+strip[2])//2, (strip[1]+strip[3])//2), position.upper(), anchor="mm", font=pfont, fill=WHITE)

        if style.stamp:
            stamp_font = font(92, "condensed")
            stamp = style.stamp
            sw = text_width(draw, stamp, stamp_font) + 110
            stamp_box = (x2-sw-20, y2-270, x2-10, y2-115)
            draw.rounded_rectangle(stamp_box, radius=14, fill=(35,0,0,230), outline=style.banner, width=9)
            draw.text(((stamp_box[0]+stamp_box[2])//2, (stamp_box[1]+stamp_box[3])//2), stamp, anchor="mm", font=stamp_font, fill=style.banner)

    def _draw_rows(self, image: Image.Image, panel, rows: Sequence[Field]) -> None:
        if not rows:
            rows = [Field("STATUS", "VERIFIED UPDATE", "status")]
        draw = ImageDraw.Draw(image)
        x1, y1, x2, y2 = panel
        inner_x1, inner_x2 = x1+55, x2-55
        available = y2-y1-90
        gap = 22
        n = max(1, len(rows))
        row_h = min(215, max(150, int((available-gap*(n-1))/n)))
        total = row_h*n + gap*(n-1)
        y = y1 + (y2-y1-total)//2
        icon_w = 190
        label_w = 620
        for field in rows:
            row_box = (inner_x1, y, inner_x2, y+row_h)
            draw.rounded_rectangle(row_box, radius=22, fill=(1,2,6), outline=(22,115,126), width=2)
            icon_box = (inner_x1+20, y+25, inner_x1+icon_w-15, y+row_h-25)
            if field.logo:
                paste_contain(image, field.logo, icon_box)
            else:
                draw_icon(draw, field.icon, icon_box, CYAN)
            label_x = inner_x1+icon_w+15
            divider_x = label_x+label_w
            draw.line((divider_x, y+32, divider_x, y+row_h-32), fill=CYAN, width=6)
            label_font = fit_font(draw, field.label.upper(), label_w-45, max_size=70, min_size=45, role="condensed")
            draw.text((label_x+20, y+row_h//2), field.label.upper(), anchor="lm", font=label_font, fill=CYAN)
            value_x = divider_x+55
            value_w = inner_x2-value_x-30
            value_font = fit_font(draw, field.value, value_w, max_size=82, min_size=42, role="bold")
            draw.text((value_x, y+row_h//2), truncate(draw, field.value, value_font, value_w), anchor="lm", font=value_font, fill=WHITE)
            y += row_h+gap

    def _transfer_panel(self, image: Image.Image, panel, decision: VerificationDecision) -> None:
        facts = decision.verified_facts
        draw = ImageDraw.Draw(image)
        x1, y1, x2, y2 = panel
        name = clean_text(facts.get("subject_name"))
        metadata = resolve_player_metadata(name, fpl_data=self.fpl_data)
        age = clean_text(facts.get("age") or metadata.get("age"), "")
        origin = clean_text(facts.get("club_from_name"))
        destination = clean_text(facts.get("club_to_name"))
        from_logo = resolve_club_logo(origin, provider_id=facts.get("provider_from_club_id"), fpl_data=self.fpl_data)
        to_logo = resolve_club_logo(destination, provider_id=facts.get("provider_to_club_id"), fpl_data=self.fpl_data)

        top_rows = [Field("NAME", name, "player")]
        if age:
            top_rows.append(Field("AGE", age, "calendar"))
        else:
            current_club = clean_text(facts.get("club_from_name"), "")
            if current_club:
                top_rows.append(Field("CURRENT CLUB", current_club, "club", from_logo))

        inner_x1, inner_x2 = x1+70, x2-70
        y = y1+75
        for field in top_rows:
            self._draw_single_transfer_row(image, (inner_x1, y, inner_x2, y+185), field)
            y += 210

        # Direction block is a dedicated verified FROM -> TO row.
        route = (inner_x1, y, inner_x2, y+255)
        draw.rounded_rectangle(route, radius=22, fill=(1,2,6), outline=(18,115,45), width=3)
        left_box = (route[0]+25, route[1]+20, route[0]+520, route[3]-20)
        right_box = (route[2]-520, route[1]+20, route[2]-25, route[3]-20)
        if from_logo:
            paste_contain(image, from_logo, (left_box[0], left_box[1], left_box[0]+180, left_box[3]))
        if to_logo:
            paste_contain(image, to_logo, (right_box[2]-180, right_box[1], right_box[2], right_box[3]))
        from_font = fit_font(draw, origin, 290, max_size=50, min_size=28, role="bold")
        to_font = fit_font(draw, destination, 290, max_size=50, min_size=28, role="bold")
        draw.text((left_box[0]+200, (left_box[1]+left_box[3])//2), truncate(draw, origin, from_font, 290), anchor="lm", font=from_font, fill=WHITE)
        draw.text((right_box[2]-200, (right_box[1]+right_box[3])//2), truncate(draw, destination, to_font, 290), anchor="rm", font=to_font, fill=WHITE)
        cx, cy = (route[0]+route[2])//2, (route[1]+route[3])//2
        for offset in (-90, -25, 40):
            pts = [(cx+offset, cy-55), (cx+offset+60, cy), (cx+offset, cy+55), (cx+offset+26, cy)]
            draw.polygon(pts, fill=(80, 245, 25))
        draw.text((route[0]+30, route[1]+18), "FROM", font=font(34, "condensed"), fill=(80,245,25))
        draw.text((route[2]-30, route[1]+18), "TO", anchor="ra", font=font(34, "condensed"), fill=(80,245,25))
        y += 280

        values: list[Field] = []
        if facts.get("fee"):
            values.append(Field("CONTRACT PRICE", clean_text(facts.get("fee")), "money"))
        if facts.get("contract_length"):
            values.append(Field("CONTRACT TERM", clean_text(facts.get("contract_length")), "calendar"))
        if facts.get("transfer_kind"):
            values.append(Field("DEAL TYPE", clean_text(facts.get("transfer_kind")), "status"))
        if facts.get("market_value") and len(values) < 3:
            values.append(Field("MARKET VALUE", clean_text(facts.get("market_value")), "money"))
        if is_reported_transfer(decision) and len(values) < 3:
            values.append(Field("STATUS", reported_status_label(decision.status, facts), "status"))
        for field in values[:3]:
            if y+180 > y2-45:
                break
            self._draw_single_transfer_row(image, (inner_x1, y, inner_x2, y+180), field)
            y += 202

    def _draw_single_transfer_row(self, image: Image.Image, box, field: Field) -> None:
        draw = ImageDraw.Draw(image)
        x1, y1, x2, y2 = box
        icon_w, label_w = 205, 580
        draw.rounded_rectangle(box, radius=20, fill=(1,2,6), outline=CYAN, width=3)
        if field.logo:
            paste_contain(image, field.logo, (x1+25, y1+20, x1+icon_w-20, y2-20))
        else:
            draw_icon(draw, field.icon, (x1+40, y1+28, x1+icon_w-35, y2-28), WHITE)
        draw.line((x1+icon_w, y1+8, x1+icon_w, y2-8), fill=CYAN, width=5)
        draw.line((x1+icon_w+label_w, y1+25, x1+icon_w+label_w, y2-25), fill=CYAN, width=4)
        lf = fit_font(draw, field.label.upper(), label_w-50, max_size=62, min_size=38, role="condensed")
        draw.text((x1+icon_w+35, (y1+y2)//2), field.label.upper(), anchor="lm", font=lf, fill=CYAN)
        value_x = x1+icon_w+label_w+50
        max_w = x2-value_x-35
        vf = fit_font(draw, field.value, max_w, max_size=78, min_size=40, role="bold")
        draw.text((value_x, (y1+y2)//2), truncate(draw, field.value, vf, max_w), anchor="lm", font=vf, fill=WHITE)

    # ── Press-conference body ───────────────────────────────────────────

    def _press_body(self, image: Image.Image, decision: VerificationDecision) -> None:
        facts = decision.verified_facts
        draw = ImageDraw.Draw(image)
        top, bottom = 410, 1870
        gap = 35
        widths = (1320, 1260, 1050)
        x = 35
        panels = []
        for w in widths:
            panels.append((x, top, x+w, bottom))
            x += w+gap
        for panel in panels:
            alpha_panel(image, panel, fill=(3,0,20,238), outline=(146,55,255,255), width=6, radius=34, glow=True)

        latest = self._list_value(facts.get("latest_news"))
        if not latest:
            latest = [
                f"{clean_text(facts.get('subject_name'), 'Speaker')} — {clean_text(facts.get('club_name'), 'Club')}",
                clean_text(facts.get("quote_topic"), "Press conference update"),
            ]
        quotes = self._list_value(facts.get("key_quotes"))
        if not quotes:
            quotes = [clean_text(facts.get("quote_summary"), "Verified quote unavailable")]
        notes = self._list_value(facts.get("manager_notes"))
        roundup = self._list_value(facts.get("roundup"))
        if not roundup:
            roundup = [
                f"{clean_text(facts.get('club_name'), 'Club')} — {clean_text(facts.get('subject_name'), 'Speaker')}",
                f"Topic: {clean_text(facts.get('quote_topic'), 'General update')}",
                f"Source: {self._actual_source(decision)}",
            ]

        self._press_column(image, panels[0], "LATEST NEWS", latest, icon="calendar", accent=(85,89,255))
        self._press_quotes(image, panels[1], quotes, notes)
        self._press_column(image, panels[2], "PRESS CONFERENCE ROUND-UP", roundup, icon="press", accent=(165,64,255))

    def _press_column(self, image, box, title, items, *, icon, accent) -> None:
        draw = ImageDraw.Draw(image)
        x1, y1, x2, y2 = box
        header = (x1+35, y1+25, x2-35, y1+150)
        draw.rounded_rectangle(header, radius=28, fill=accent, outline=(215,180,255), width=4)
        draw_icon(draw, icon, (header[0]+25, header[1]+20, header[0]+120, header[3]-20), WHITE)
        hf = fit_font(draw, title, header[2]-header[0]-150, max_size=64, min_size=38, role="condensed")
        draw.text((header[0]+140, (header[1]+header[3])//2), title, anchor="lm", font=hf, fill=WHITE)
        count = max(1, len(items))
        gap = 18
        row_h = min(145, max(82, int((y2-(y1+190)-45-gap*(count-1))/count)))
        y = y1+185
        for idx, item in enumerate(items):
            if y+row_h > y2-30:
                break
            row = (x1+30, y, x2-30, y+row_h)
            draw.rounded_rectangle(row, radius=22, fill=(13,7,38), outline=accent, width=3)
            draw.ellipse((row[0]+18, row[1]+20, row[0]+82, row[1]+84), fill=accent)
            nf = font(38, "bold")
            draw.text((row[0]+50, row[1]+52), str(idx+1), anchor="mm", font=nf, fill=WHITE)
            tf = fit_font(draw, item, row[2]-row[0]-125, max_size=43, min_size=28, role="bold")
            wrapped = wrap_text(draw, item, tf, row[2]-row[0]-125, 2)
            line_y = row[1]+22
            for line in wrapped:
                draw.text((row[0]+105, line_y), line, font=tf, fill=WHITE)
                line_y += int(tf.size*1.1)
            y += row_h+gap

    def _press_quotes(self, image, box, quotes, notes) -> None:
        draw = ImageDraw.Draw(image)
        x1, y1, x2, y2 = box
        header = (x1+35, y1+25, x2-35, y1+150)
        draw.rounded_rectangle(header, radius=28, fill=(117,32,224), outline=(215,180,255), width=4)
        draw_icon(draw, "quote", (header[0]+25, header[1]+15, header[0]+120, header[3]-15), WHITE)
        draw.text((header[0]+145, (header[1]+header[3])//2), "KEY QUOTES", anchor="lm", font=font(62, "condensed"), fill=WHITE)
        y = y1+185
        for quote in quotes[:4]:
            box_q = (x1+35, y, x2-35, y+220)
            draw.rounded_rectangle(box_q, radius=24, fill=(13,7,38), outline=(154,70,246), width=3)
            draw.text((box_q[0]+25, box_q[1]+20), "“", font=font(80, "bold"), fill=(255,233,25))
            qf = fit_font(draw, quote, box_q[2]-box_q[0]-120, max_size=47, min_size=31, role="regular")
            lines = wrap_text(draw, quote, qf, box_q[2]-box_q[0]-120, 3)
            ly = box_q[1]+40
            for line in lines:
                draw.text((box_q[0]+95, ly), line, font=qf, fill=WHITE)
                ly += int(qf.size*1.2)
            y += 240
        if notes and y < y2-230:
            draw.text((x1+55, y+15), "MANAGER'S NOTES", font=font(52, "condensed"), fill=(255,233,25))
            y += 85
            for note in notes[:4]:
                draw.ellipse((x1+55, y+18, x1+75, y+38), fill=(255,233,25))
                nf = fit_font(draw, note, x2-x1-150, max_size=39, min_size=28, role="regular")
                draw.text((x1+95, y), truncate(draw, note, nf, x2-x1-150), font=nf, fill=WHITE)
                y += 65

    # ── Data projection ─────────────────────────────────────────────────

    def _fields(self, decision: VerificationDecision) -> list[Field]:
        facts = decision.verified_facts
        event = decision.event_type
        rows: list[Field] = [Field("PLAYER", clean_text(facts.get("subject_name")), "player")]
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

    def _actual_source(self, decision: VerificationDecision) -> str:
        ids = decision.authority_source_ids or decision.source_ids
        names = []
        for source_id in ids[:2]:
            profile = self.sources.get(source_id)
            names.append(profile.display_name if profile else str(source_id))
        return " · ".join(names) or "FPL VORTEX"

    def _visible_footer_source(self, decision: VerificationDecision) -> str:
        ids = set(decision.authority_source_ids or decision.source_ids)
        if "official.fpl" in ids:
            return "FPL API"
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
