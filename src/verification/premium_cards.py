"""Unified FPL VORTEX verified-news card renderer.

This module is deliberately fact-only. It receives a VerificationDecision and
never parses raw articles or invents missing facts. All four production card
families share the same 3840x2160 canvas and typography hierarchy while their
accent treatment communicates the category:
TRANSFER green, INJURY red, SUSPENSION amber, PRESS CONFERENCE cyan.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional
import base64
import hashlib
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageOps

from src.cards.background import CATEGORIES, content_box_px, load_background
from .models import EventType, VerificationDecision
from .reported_transfer_gate import (
    is_reported_transfer,
    reported_status_label,
)
from .source_registry import SourceRegistry

SIZE = (3840, 2160)
THEMES = {
    EventType.TRANSFER: ("TRANSFER CONFIRMED", (0, 255, 90), "TRANSFER"),
    EventType.INJURY: ("INJURY UPDATE", (255, 51, 51), "INJURY"),
    EventType.SUSPENSION: ("SUSPENSION UPDATE", (255, 170, 0), "SUSPENSION"),
    EventType.PRESS_CONFERENCE: ("PRESS CONFERENCE", (0, 191, 255), "PRESS"),
}


def _font(size: int, bold: bool = False):
    candidates = (
        "Montserrat-Black.ttf" if bold else "Montserrat-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _fit(draw, text, font, max_width: int) -> str:
    value = " ".join(str(text or "").split())
    if not value:
        return "NOT REPORTED"
    while value and draw.textbbox((0, 0), value, font=font)[2] > max_width:
        value = value[:-1].rstrip()
    return value if value == str(text or "").strip() else value.rstrip(" .,") + "…"


def _value(value, missing="NOT REPORTED") -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text or text.lower() in {"none", "null", "unknown", "n/a", "na"}:
        return missing
    return text


def _source(decision: VerificationDecision, sources: SourceRegistry) -> str:
    source_ids = decision.authority_source_ids or decision.source_ids
    if not source_ids:
        return "NOT REPORTED"
    names = []
    for sid in source_ids[:2]:
        profile = sources.get(sid)
        names.append(profile.display_name if profile else sid)
    return " · ".join(names)


def _load_player_image(subject: str):
    """Resolve only an identity-verified player image.

    Order is deliberately narrow: canonical FPL player asset first, then the
    renderer's independently verified Wikipedia footballer page. Story media,
    article og:image, ESPN/BBC/FotMob search results and club crests are never
    accepted as player identity. If identity cannot be verified, return None.
    """
    try:
        from src.fpl_feed import fetch_fpl_data, find_player_in_fpl
        from src.renderer import _data_uri, _download_asset, _wikipedia_player_image

        fpl = fetch_fpl_data()
        player = find_player_in_fpl(subject, fpl)
        if player:
            pid = player.get("code")
            if pid:
                path = Path(f"players/{pid}.png")
                if not path.exists():
                    _download_asset(
                        f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{pid}.png",
                        path,
                    )
                uri = _data_uri(path)
                if uri.startswith("data:image"):
                    raw = base64.b64decode(uri.split(",", 1)[1])
                    return Image.open(BytesIO(raw)).convert("RGB")

        image_url = _wikipedia_player_image(subject)
        if image_url:
            path = Path("players/wiki_" + hashlib.md5(subject.encode()).hexdigest()[:12] + ".jpg")
            if not path.exists():
                _download_asset(image_url, path)
            uri = _data_uri(path)
            if uri.startswith("data:image"):
                raw = base64.b64decode(uri.split(",", 1)[1])
                return Image.open(BytesIO(raw)).convert("RGB")
    except Exception as exc:
        print(f"  [PHOTO] verified image lookup failed for {subject!r}: {exc}")
    print(f"  [PHOTO] no identity-verified image available for {subject!r}; neutral silhouette used")
    return None


def _load_crest(club_key: str):
    try:
        from src.renderer import _crest_uri
        uri = _crest_uri(club_key)
        if uri.startswith("data:image"):
            raw = base64.b64decode(uri.split(",", 1)[1])
            return Image.open(BytesIO(raw)).convert("RGBA")
    except Exception:
        pass
    return None


def _paste_cover(base: Image.Image, source: Image.Image, box):
    x1, y1, x2, y2 = box
    crop = ImageOps.fit(source, (x2 - x1, y2 - y1), method=Image.Resampling.LANCZOS, centering=(0.5, 0.35))
    base.paste(crop, (x1, y1))


def _draw_icon(draw, event: EventType, cx: int, cy: int, accent):
    if event == EventType.INJURY:
        draw.rounded_rectangle((cx-28, cy-85, cx+28, cy+85), radius=14, fill=accent)
        draw.rounded_rectangle((cx-85, cy-28, cx+85, cy+28), radius=14, fill=accent)
    elif event == EventType.SUSPENSION:
        draw.ellipse((cx-75, cy-75, cx+75, cy+75), outline=accent, width=18)
        draw.line((cx, cy-42, cx, cy+45), fill=accent, width=18)
        draw.ellipse((cx-12, cy+55, cx+12, cy+79), fill=accent)
    elif event == EventType.PRESS_CONFERENCE:
        draw.rounded_rectangle((cx-75, cy-30, cx+50, cy+35), radius=18, outline=accent, width=16)
        draw.line((cx+50, cy+10, cx+100, cy+60), fill=accent, width=16)
        draw.ellipse((cx-12, cy-70, cx+12, cy-46), fill=accent)
    else:
        draw.ellipse((cx-72, cy-72, cx+72, cy+72), outline=accent, width=18)
        draw.line((cx-38, cy, cx-5, cy+34), fill=accent, width=20)
        draw.line((cx-5, cy+34, cx+48, cy-34), fill=accent, width=20)


def _tile(draw, x, y, w, h, label, value, accent):
    draw.rounded_rectangle((x, y, x+w, y+h), radius=28, fill=(9, 12, 24), outline=accent, width=3)
    draw.rectangle((x, y, x+10, y+h), fill=accent)
    draw.text((x+34, y+24), label.upper(), font=_font(30, True), fill=accent)
    vf = _font(50, True)
    draw.text((x+34, y+76), _fit(draw, value, vf, w-68), font=vf, fill=(250,252,255))


def _glass_panel(image: Image.Image, box, *, radius=40, fill=(3, 5, 14, 218), outline=None, width=4):
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    odraw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)
    image.paste(overlay, (0, 0), overlay)


def _paste_rounded_cover(base: Image.Image, source: Image.Image, box, *, radius=42, outline=(0, 230, 90)):
    x1, y1, x2, y2 = box
    fitted = ImageOps.fit(
        source.convert("RGB"), (x2-x1, y2-y1),
        method=Image.Resampling.LANCZOS, centering=(0.5, 0.32),
    )
    mask = Image.new("L", fitted.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, *fitted.size), radius=radius, fill=255)
    base.paste(fitted, (x1, y1), mask)
    ImageDraw.Draw(base).rounded_rectangle(box, radius=radius, outline=outline, width=8)


def _club_block(image: Image.Image, draw, box, label, club, accent):
    x1, y1, x2, y2 = box
    _glass_panel(image, box, radius=30, fill=(5, 8, 20, 232), outline=accent, width=3)
    draw.text((x1+30, y1+22), label.upper(), font=_font(28, True), fill=accent)
    crest = _load_crest(club)
    crest_space = 150 if crest else 20
    if crest:
        crest.thumbnail((112, 112), Image.Resampling.LANCZOS)
        image.paste(crest, (x2-135, y1+42), crest)
    font = _font(54, True)
    draw.text((x1+30, y1+82), _fit(draw, club, font, x2-x1-60-crest_space), font=font, fill=(250,252,255))


def render_verified_card(decision: VerificationDecision, sources: SourceRegistry, output_path: str | Path, *, fpl_data: Optional[dict] = None) -> str:
    """Composite verified facts onto Claude's approved category slide.

    The artwork in ``assets/frames`` remains untouched. All variable content is
    constrained to its measured clear content box, with the same composition
    used for official and reported stories. Authority wording changes; branding
    and slide design do not.
    """
    if not decision.may_publish:
        raise ValueError("cannot render card for unverified decision")
    event = decision.event_type
    if event not in THEMES:
        raise ValueError(f"unsupported verified card event: {event.value}")

    image = load_background(event.value, SIZE)
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = content_box_px(SIZE)
    accent = CATEGORIES[event.value].accent
    facts = decision.verified_facts
    subject = _value(facts.get("subject_name"), "OFFICIAL UPDATE")
    source = _source(decision, sources)
    is_reported = is_reported_transfer(decision)

    # Approved frame content regions: facts on the left, verified player image
    # on the right. Both sit strictly between the frame's header and footer.
    info_box = (left + 20, top + 20, left + 2220, bottom - 20)
    photo_box = (left + 2310, top + 35, right - 35, bottom - 35)
    _glass_panel(image, info_box, radius=46, fill=(3, 4, 15, 218), outline=(91, 37, 118, 220), width=4)
    _glass_panel(image, photo_box, radius=46, fill=(3, 4, 15, 226), outline=accent, width=6)

    # Authority/status pill. Reported transfers can never visually masquerade
    # as a first-party confirmation.
    if event == EventType.TRANSFER:
        status_text = (
            reported_status_label(decision.status, facts)
            if is_reported else "OFFICIAL TRANSFER CONFIRMED"
        )
    elif event == EventType.INJURY:
        status_text = "OFFICIAL INJURY UPDATE"
    elif event == EventType.SUSPENSION:
        status_text = "OFFICIAL SUSPENSION UPDATE"
    else:
        status_text = "VERIFIED PRESS CONFERENCE"
    pill = (left + 70, top + 65, left + 1020, top + 175)
    draw.rounded_rectangle(pill, radius=28, fill=accent)
    pill_font = _font(42, True)
    draw.text((pill[0] + 34, pill[1] + 28), _fit(draw, status_text, pill_font, pill[2]-pill[0]-68), font=pill_font, fill=(0, 0, 0))

    name_font = _font(118, True)
    draw.text(
        (left + 70, top + 235),
        _fit(draw, subject.upper(), name_font, 2070),
        font=name_font,
        fill=(255, 255, 255),
    )
    if facts.get("position"):
        pos_font = _font(34, True)
        draw.text((left + 75, top + 385), f"POSITION  {str(facts['position']).upper()}", font=pos_font, fill=(204, 188, 220))

    if event == EventType.TRANSFER:
        origin = _value(facts.get("club_from_name"), "NOT REPORTED")
        destination = _value(facts.get("club_to_name"), "NOT REPORTED")
        route_y1, route_y2 = top + 455, top + 675
        _club_block(image, draw, (left + 70, route_y1, left + 980, route_y2), "Origin", origin, accent)
        _club_block(image, draw, (left + 1190, route_y1, left + 2100, route_y2), "Destination", destination, accent)
        cx, cy = left + 1090, (route_y1 + route_y2) // 2
        draw.ellipse((cx-72, cy-72, cx+72, cy+72), fill=accent)
        draw.line((cx-35, cy, cx+35, cy), fill=(0,0,0), width=18)
        draw.line((cx+10, cy-28, cx+40, cy), fill=(0,0,0), width=18)
        draw.line((cx+10, cy+28, cx+40, cy), fill=(0,0,0), width=18)

        fact_y = top + 740
        gap = 22
        tile_w = 522
        if facts.get("structured_source") == "fotmob_transfer_table":
            values = [
                ("Deal", _value(facts.get("transfer_kind"), "NOT REPORTED")),
                ("Fee", _value(facts.get("fee"), "NOT REPORTED")),
                ("Contract", _value(facts.get("contract_length"), "NOT REPORTED")),
                ("Market value", _value(facts.get("market_value"), "NOT REPORTED")),
            ]
        elif is_reported:
            values = [
                ("Status", reported_status_label(decision.status, facts)),
                ("Deal", _value(facts.get("transfer_kind"), "NOT REPORTED")),
                ("Fee", _value(facts.get("fee"), "NOT REPORTED")),
                ("Contract", _value(facts.get("contract_length"), "NOT REPORTED")),
            ]
        else:
            values = [
                ("Deal", _value(facts.get("transfer_kind"), "NOT REPORTED")),
                ("Fee", _value(facts.get("fee"), "UNDISCLOSED")),
                ("Contract", _value(facts.get("contract_length"), "NOT DISCLOSED")),
                ("Status", "OFFICIAL"),
            ]
        for index, (label, value) in enumerate(values):
            x = left + 70 + index * (tile_w + gap)
            _tile(draw, x, fact_y, tile_w, 190, label, value, accent)

    elif event == EventType.INJURY:
        _tile(draw, left+70, top+500, 2030, 210, "Injury / diagnosis", _value(facts.get("injury_status")), accent)
        _tile(draw, left+70, top+750, 990, 200, "Status", _value(facts.get("injury_status")), accent)
        _tile(draw, left+1110, top+750, 990, 200, "Expected return", _value(facts.get("return_date"), "NOT CONFIRMED"), accent)
    elif event == EventType.SUSPENSION:
        _tile(draw, left+70, top+500, 2030, 210, "Suspension", _value(facts.get("suspension_status")), accent)
        _tile(draw, left+70, top+750, 990, 200, "Length", _value(facts.get("suspension_length")), accent)
        _tile(draw, left+1110, top+750, 990, 200, "Return", _value(facts.get("return_date"), "NOT CONFIRMED"), accent)
    else:
        _tile(draw, left+70, top+500, 2030, 200, "Club", _value(facts.get("club_name")), accent)
        _tile(draw, left+70, top+740, 2030, 260, "What was said", _value(facts.get("quote_summary")), accent)
        _tile(draw, left+70, top+1040, 2030, 200, "Topic", _value(facts.get("quote_topic")), accent)

    # Actual story authority is visible only on the card, as requested. The
    # caption renderer deliberately contains no source name or URL.
    authority = "REPORTED" if is_reported else "CONFIRMED"
    source_box = (left + 70, bottom - 230, left + 2100, bottom - 80)
    _glass_panel(image, source_box, radius=28, fill=(5, 8, 20, 238), outline=accent, width=3)
    draw.text((source_box[0]+30, source_box[1]+24), f"{authority} SOURCE", font=_font(28, True), fill=accent)
    src_font = _font(46, True)
    draw.text((source_box[0]+30, source_box[1]+70), _fit(draw, source, src_font, source_box[2]-source_box[0]-60), font=src_font, fill=(255,255,255))

    photo = _load_player_image(subject)
    inner_photo = (photo_box[0]+28, photo_box[1]+28, photo_box[2]-28, photo_box[3]-28)
    if photo:
        _paste_rounded_cover(image, photo, inner_photo, radius=36, outline=accent)
    else:
        pd = ImageDraw.Draw(image)
        x1, y1, x2, y2 = inner_photo
        cx = (x1+x2)//2
        pd.ellipse((cx-190, y1+250, cx+190, y1+630), outline=(180,160,200), width=12)
        pd.rounded_rectangle((cx-330, y1+620, cx+330, y2-180), radius=170, outline=(180,160,200), width=12)
        pd.text((x1+70, y2-125), "IDENTITY VERIFIED • PHOTO UNAVAILABLE", font=_font(28, True), fill=(200,190,215))

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, "PNG", optimize=True)
    return str(out)
