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
    draw.rounded_rectangle((x, y, x+w, y+h), radius=28, fill=(16, 20, 28), outline=(48, 55, 68), width=3)
    draw.rectangle((x, y, x+10, y+h), fill=accent)
    draw.text((x+34, y+28), label.upper(), font=_font(34, True), fill=accent)
    vf = _font(58, True)
    draw.text((x+34, y+86), _fit(draw, value, vf, w-68), font=vf, fill=(245,248,252))


def render_verified_card(decision: VerificationDecision, sources: SourceRegistry, output_path: str | Path, *, fpl_data: Optional[dict] = None) -> str:
    if not decision.may_publish:
        raise ValueError("cannot render card for unverified decision")
    event = decision.event_type
    if event not in THEMES:
        raise ValueError(f"unsupported verified card event: {event.value}")
    heading, accent, footer_tag = THEMES[event]
    is_reported = is_reported_transfer(decision)
    if is_reported:
        heading = "TRANSFER REPORTED"
        footer_tag = "TRANSFER • REPORTED"
    facts = decision.verified_facts
    subject = _value(facts.get("subject_name"), "OFFICIAL UPDATE")
    image = Image.new("RGB", SIZE, (3, 5, 8))
    draw = ImageDraw.Draw(image)
    W, H = SIZE

    draw.rectangle((0, 0, W, H), fill=(3, 5, 8))
    draw.ellipse((W-1700, -400, W+400, H+600), fill=(8, 15, 22))
    draw.rectangle((0, 0, 22, H), fill=accent)

    # The brand asset was renamed Logo.png -> logo.png. Linux runners are
    # case-sensitive, so the hardcoded name silently resolved to nothing and
    # every published card lost its lion mark. Take whichever exists.
    logo = next((p for p in (Path("logo.png"), Path("Logo.png")) if p.exists()), None)
    if logo is not None:
        try:
            brand = Image.open(logo).convert("RGBA")
            brand.thumbnail((130, 130), Image.Resampling.LANCZOS)
            image.paste(brand, (112, 92), brand)
        except Exception:
            pass
    draw.text((275, 105), "FPL", font=_font(82, True), fill=(245,248,252))
    draw.text((430, 105), "VORTEX", font=_font(82, True), fill=(0,255,90))

    draw.rounded_rectangle((112, 285, 1110, 415), radius=24, fill=accent)
    draw.text((155, 313), heading, font=_font(58, True), fill=(0,0,0))
    _draw_icon(draw, event, 1180, 350, accent)

    photo = _load_player_image(subject)
    frame = (2470, 170, 3700, 1880)
    draw.rounded_rectangle(frame, radius=48, fill=(9, 13, 18), outline=accent, width=6)
    if photo:
        _paste_cover(image, photo, (2480, 180, 3690, 1870))
    else:
        draw.ellipse((2870, 550, 3290, 970), outline=(90,98,110), width=10)
        draw.rounded_rectangle((2750, 950, 3410, 1710), radius=180, outline=(90,98,110), width=10)
    draw.rounded_rectangle(frame, radius=48, outline=accent, width=6)

    name_font = _font(142, True)
    draw.text((112, 540), _fit(draw, subject.upper(), name_font, 2150), font=name_font, fill=(248,250,252))

    if event == EventType.TRANSFER:
        origin = _value(facts.get("club_from_name"), "NOT REPORTED")
        destination = _value(facts.get("club_to_name"), "NOT REPORTED")
        _tile(draw, 112, 800, 960, 220, "ORIGIN", origin, accent)
        _tile(draw, 1120, 800, 960, 220, "DESTINATION", destination, accent)
        if is_reported:
            if facts.get("structured_source") == "fotmob_transfer_table":
                # Same visual system, four compact facts from the structured
                # listing: no invented fee/free status or contract detail.
                _tile(draw, 112, 1060, 468, 220, "DEAL", _value(facts.get("transfer_kind"), "NOT REPORTED"), accent)
                _tile(draw, 606, 1060, 468, 220, "FEE", _value(facts.get("fee"), "NOT REPORTED"), accent)
                _tile(draw, 1100, 1060, 468, 220, "CONTRACT", _value(facts.get("contract_length"), "NOT REPORTED"), accent)
                _tile(draw, 1594, 1060, 486, 220, "MARKET VALUE", _value(facts.get("market_value"), "NOT REPORTED"), accent)
            else:
                _tile(draw, 112, 1060, 620, 220, "STATUS", reported_status_label(decision.status, facts), accent)
                _tile(draw, 770, 1060, 620, 220, "REPORTED FEE", _value(facts.get("fee"), "NOT REPORTED"), accent)
                _tile(draw, 1428, 1060, 652, 220, "DEAL TYPE", _value(facts.get("transfer_kind"), "NOT REPORTED"), accent)
        else:
            _tile(draw, 112, 1060, 620, 220, "CONTRACT TERM", _value(facts.get("contract_length"), "NOT DISCLOSED"), accent)
            _tile(draw, 770, 1060, 620, 220, "CONTRACT FEE", _value(facts.get("fee"), "NOT DISCLOSED"), accent)
            _tile(draw, 1428, 1060, 652, 220, "CONTRACT TYPE", _value(facts.get("transfer_kind"), "NOT REPORTED"), accent)
    elif event == EventType.INJURY:
        _tile(draw, 112, 800, 1968, 220, "INJURY / DIAGNOSIS", _value(facts.get("injury_status"), "NOT REPORTED"), accent)
        _tile(draw, 112, 1060, 950, 220, "STATUS", _value(facts.get("injury_status"), "NOT REPORTED"), accent)
        _tile(draw, 1130, 1060, 950, 220, "EXPECTED RETURN", _value(facts.get("return_date"), "NOT OFFICIALLY CONFIRMED"), accent)
    elif event == EventType.SUSPENSION:
        _tile(draw, 112, 800, 1968, 220, "SUSPENSION STATUS", _value(facts.get("suspension_status"), "NOT REPORTED"), accent)
        _tile(draw, 112, 1060, 950, 220, "LENGTH", _value(facts.get("suspension_length"), "NOT REPORTED"), accent)
        _tile(draw, 1130, 1060, 950, 220, "RETURN", _value(facts.get("return_date"), "NOT OFFICIALLY CONFIRMED"), accent)
    else:
        _tile(draw, 112, 800, 1968, 220, "CLUB", _value(facts.get("club_name"), "NOT REPORTED"), accent)
        _tile(draw, 112, 1060, 950, 300, "WHAT WAS SAID", _value(facts.get("quote_summary"), "NOT REPORTED"), accent)
        _tile(draw, 1130, 1060, 950, 300, "TOPIC", _value(facts.get("quote_topic"), "NOT REPORTED"), accent)

    source = _source(decision, sources)
    draw.rectangle((0, 1960, W, H), fill=(8, 11, 16))
    draw.rectangle((0, 1960, W, 1972), fill=accent)
    authority_label = "REPORTED BY" if is_reported else "CONFIRMED BY"
    draw.text((112, 2020), f"{authority_label}: {source}", font=_font(42, True), fill=(205,212,223))
    draw.text((112, 2090), footer_tag, font=_font(30, True), fill=accent)

    # Right-aligned from the MEASURED width, not a fixed offset. A hardcoded
    # W-760 fits "SOURCE: Arsenal" and runs off the canvas the moment two
    # sources are credited ("Aston Villa · BBC Sport"), which is the common case.
    def _right(text, font, y, fill):
        width = draw.textlength(text, font=font)
        draw.text((W - 112 - width, y), text, font=font, fill=fill)

    _right(f"SOURCE: {source}", _font(42, True), 2020, (205,212,223))
    _right("3840 × 2160  •  16:9", _font(30, True), 2090, (120,130,145))

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, "PNG", optimize=True)
    return str(out)
