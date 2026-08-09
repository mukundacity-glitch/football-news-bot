"""Verified-facts-only image cards for X posts.

TRANSFER cards are official-confirmed-only: ``create_verified_card`` never
generates a transfer graphic unless ``validate_official_transfer`` passes.
Every other publishable category (INJURY, SUSPENSION) keeps the existing
fact-only card design below the transfer path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

from .models import EventType, VerificationDecision
from .official_transfer_gate import (
    log_skipped_unverified_transfer,
    validate_official_transfer,
)
from .player_image import resolve_player_image
from .source_registry import SourceRegistry


_BG = (9, 16, 35)
_PANEL = (18, 29, 58)
_WHITE = (247, 249, 255)
_MUTED = (173, 188, 216)
_GREEN = (46, 204, 113)
_BLUE = (73, 141, 255)

# Policy: transfer graphics must be at least 1080x1350 (portrait, social-safe).
TRANSFER_CARD_SIZE = (1080, 1350)


class UnverifiedTransferError(RuntimeError):
    """Raised when a caller tries to render a transfer card that failed the
    strict official-only validation gate. Callers must never fall back to a
    softer card in this case -- the correct action is to skip the post."""


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    names = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def create_verified_card(
    decision: VerificationDecision,
    sources: SourceRegistry,
    output_path: str | Path,
    *,
    fpl_data: Optional[dict] = None,
) -> str:
    if not decision.may_publish:
        raise ValueError("cannot render card for unverified decision")

    if decision.event_type == EventType.TRANSFER:
        validation = validate_official_transfer(decision, sources)
        if not validation.ok:
            log_skipped_unverified_transfer(decision, validation.reason)
            raise UnverifiedTransferError(
                f"SKIPPED_UNVERIFIED_TRANSFER: {validation.reason}"
            )
        return _create_official_transfer_card(
            decision, sources, output_path, fpl_data=fpl_data,
            verified_at=validation.verified_at,
        )

    return _create_fact_only_card(decision, sources, output_path)


# ── OFFICIAL TRANSFER CARD (1080x1350) ────────────────────────────────────

def _load_club_badge(club_key: Optional[str], box: int = 140) -> Optional[Image.Image]:
    if not club_key:
        return None
    try:
        from src.renderer import _load_crest
        return _load_crest(club_key, box=box)
    except Exception:
        return None


def _club_key_for(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    try:
        from src.renderer import _verified_club_key
        return _verified_club_key(name)
    except Exception:
        return None


def _fit_cover(image: Image.Image, box: tuple[int, int]) -> Image.Image:
    """Scale to cover the box while preserving aspect ratio, then center-crop.

    Never stretches the image and never produces a badly cropped head: crop
    bias is weighted toward the top third, where a portrait photo's face sits.
    """
    target_w, target_h = box
    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    resized = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = max(0, (new_w - target_w) // 2)
    top = max(0, int((new_h - target_h) * 0.18))
    top = min(top, max(0, new_h - target_h))
    return resized.crop((left, top, left + target_w, top + target_h))


def _draw_wrapped(
    draw: ImageDraw.ImageDraw, text: str, xy: tuple[int, int], font: ImageFont.ImageFont,
    fill, max_width: int, line_height: int,
) -> int:
    words = text.split()
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height
    return y


def _create_official_transfer_card(
    decision: VerificationDecision,
    sources: SourceRegistry,
    output_path: str | Path,
    *,
    fpl_data: Optional[dict],
    verified_at: Optional[str],
) -> str:
    facts = decision.verified_facts
    width, height = TRANSFER_CARD_SIZE
    image = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(image)

    # ── Green "TRANSFER CONFIRMED" label ──────────────────────────────
    # Plain text only: the default PIL/DejaVu rendering path has no colour
    # emoji glyphs, so an emoji here would draw as a tofu box on the card.
    draw.rounded_rectangle((40, 40, width - 40, 108), radius=16, fill=_GREEN)
    draw.text((64, 58), "TRANSFER CONFIRMED", font=_font(34, True), fill=(6, 20, 12))

    player_name = str(facts.get("subject_name") or "Unknown player")
    to_club = str(facts.get("club_to_name") or "")
    from_club = str(facts.get("club_from_name") or "")
    position = str(facts.get("position") or "").strip()
    nationality = str(facts.get("nationality") or "").strip()
    age = facts.get("age")
    dob = facts.get("birth_date")
    fee = facts.get("fee")
    contract = facts.get("contract_length")

    to_key = _club_key_for(to_club)
    from_key = _club_key_for(from_club)
    to_badge = _load_club_badge(to_key)
    from_badge = _load_club_badge(from_key)

    # ── Large, sharp player image: ~42% of card height ────────────────
    photo_box = (width - 120, int(height * 0.42))
    photo_top = 130
    try:
        parts = player_name.rsplit(" ", 1)
        first_name = parts[0] if len(parts) > 1 else ""
        last_name = parts[-1]
        photo, photo_source, match = resolve_player_image(
            full_name=player_name,
            first_name=first_name,
            last_name=last_name,
            club_name=to_club,
            position=position or None,
            nationality=nationality or None,
            fpl_data=fpl_data,
            club_badge=to_badge,
        )
        if match is not None and not position:
            position = str(_position_label(match.element, fpl_data) or position)
    except Exception:
        from .player_image import generate_placeholder
        photo = generate_placeholder(player_name, to_badge)

    fitted = _fit_cover(photo.convert("RGB"), photo_box)
    photo_x = (width - photo_box[0]) // 2
    draw.rectangle(
        (photo_x - 6, photo_top - 6, photo_x + photo_box[0] + 6, photo_top + photo_box[1] + 6),
        outline=_GREEN, width=4,
    )
    image.paste(fitted, (photo_x, photo_top))
    draw = ImageDraw.Draw(image)

    y = photo_top + photo_box[1] + 36

    # ── Player full name ───────────────────────────────────────────────
    name_font = _font(56, True)
    y = _draw_wrapped(draw, player_name.upper(), (60, y), name_font, _WHITE, width - 120, 62) + 6

    # ── Position | Nationality | Age/DOB ───────────────────────────────
    meta_bits = [b for b in (position, nationality, (f"Age {age}" if age else (str(dob) if dob else ""))) if b]
    if meta_bits:
        draw.text((60, y), "  |  ".join(meta_bits), font=_font(30), fill=_MUTED)
    y += 54

    # ── FROM -> TO with badges and arrow ───────────────────────────────
    club_row_top = y + 10
    club_box = 120
    from_x, to_x = 90, width - 90 - club_box
    if from_badge:
        image.paste(from_badge.convert("RGBA"), (from_x, club_row_top), from_badge.convert("RGBA"))
    if to_badge:
        image.paste(to_badge.convert("RGBA"), (to_x, club_row_top), to_badge.convert("RGBA"))
    draw = ImageDraw.Draw(image)
    arrow_y = club_row_top + club_box // 2
    draw.line((from_x + club_box + 24, arrow_y, to_x - 24, arrow_y), fill=_GREEN, width=8)
    draw.polygon(
        [(to_x - 24, arrow_y - 18), (to_x - 24, arrow_y + 18), (to_x + 4, arrow_y)],
        fill=_GREEN,
    )
    draw.text((from_x, club_row_top + club_box + 10), "FROM", font=_font(22, True), fill=_MUTED)
    draw.text((from_x, club_row_top + club_box + 36), from_club, font=_font(28, True), fill=_WHITE)
    to_label_w = draw.textbbox((0, 0), "TO", font=_font(22, True))[2]
    draw.text((to_x + club_box - to_label_w, club_row_top + club_box + 10), "TO", font=_font(22, True), fill=_MUTED)
    to_name_w = draw.textbbox((0, 0), to_club, font=_font(28, True))[2]
    draw.text((to_x + club_box - to_name_w, club_row_top + club_box + 36), to_club, font=_font(28, True), fill=_WHITE)

    y = club_row_top + club_box + 90

    # ── Fee (or undisclosed) + contract ────────────────────────────────
    draw.rounded_rectangle((60, y, width - 60, y + 64), radius=12, fill=_PANEL)
    fee_text = f"Fee: {fee}" if fee else "Fee: undisclosed"
    draw.text((80, y + 16), fee_text, font=_font(28, True), fill=_GREEN if fee else _MUTED)
    y += 78
    if contract:
        draw.text((60, y), f"Contract: {contract}", font=_font(26), fill=_MUTED)
        y += 42

    # ── Footer: official source, date, verified timestamp ──────────────
    source_id = decision.source_ids[0] if decision.source_ids else ""
    profile = sources.get(source_id)
    source_name = profile.display_name if profile else source_id
    source_date = decision.created_at

    footer_top = height - 130
    draw.line((60, footer_top - 12, width - 60, footer_top - 12), fill=(50, 62, 96), width=2)
    draw.text((60, footer_top), f"Official source: {source_name}", font=_font(24, True), fill=_WHITE)
    draw.text((60, footer_top + 32), f"Source date: {_format_ts(source_date)}", font=_font(20), fill=_MUTED)
    draw.text((60, footer_top + 60), f"Verified: {_format_ts(verified_at)}", font=_font(20), fill=_MUTED)

    wordmark = "FPL VORTEX"
    wm_font = _font(24, True)
    wm_w = draw.textbbox((0, 0), wordmark, font=wm_font)[2]
    draw.text((width - 60 - wm_w, footer_top), wordmark, font=wm_font, fill=_BLUE)

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)
    return str(path)


def _position_label(element, fpl_data) -> str:
    if not element or not fpl_data:
        return ""
    et = element.get("element_type")
    record = next((x for x in (fpl_data.get("element_types") or []) if x.get("id") == et), None)
    if record:
        return str(record.get("singular_name_short") or record.get("singular_name") or "")
    return ""


def _format_ts(value) -> str:
    if not value:
        return "unknown"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(value)


# ── FACT-ONLY CARD (INJURY / SUSPENSION / everything non-transfer) ───────

def _create_fact_only_card(
    decision: VerificationDecision,
    sources: SourceRegistry,
    output_path: str | Path,
) -> str:
    facts = dict(decision.verified_facts)
    facts["_event_status"] = decision.status.value

    # Use the established FPL VORTEX branded-card treatment whenever
    # Playwright is available: player image, club crest, logo, channel name,
    # and official source handle in the footer. The adapter receives only V2
    # verified facts.
    try:
        from src.renderer import create_verified_branded_card
        handles = []
        for source_id in decision.source_ids:
            profile = sources.get(source_id)
            if profile and profile.handles:
                handles.append(profile.handles[0])
        if create_verified_branded_card(
            decision.event_type.value,
            str(facts.get("subject_name") or facts.get("club_name") or ""),
            facts,
            handles,
            str(output_path),
        ):
            return str(output_path)
    except Exception:
        # A graphics dependency/network failure must never prevent a verified
        # post; the local fact-only branded card below is the safe fallback.
        pass

    image = Image.new("RGB", (1200, 675), _BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((50, 45, 1150, 630), radius=28, fill=_PANEL)
    draw.rectangle((50, 45, 1150, 55), fill=_GREEN)

    _draw_brand(draw, image)

    event_label = {
        EventType.INJURY: "OFFICIAL INJURY UPDATE",
        EventType.SUSPENSION: "OFFICIAL SUSPENSION",
        EventType.MANAGER: "OFFICIAL MANAGER UPDATE",
        EventType.CONTRACT: "OFFICIAL CONTRACT EXTENSION",
        EventType.OFFICIAL_STATEMENT: "OFFICIAL CLUB STATEMENT",
    }.get(decision.event_type, "OFFICIAL UPDATE")
    draw.text((90, 85), event_label, font=_font(34, True), fill=_GREEN)

    subject = str(facts.get("subject_name") or facts.get("club_name") or "Official update")
    subject = _ellipsize(draw, subject, _font(62, True), 1000)
    draw.text((90, 150), subject, font=_font(62, True), fill=_WHITE)

    lines = _fact_lines(decision)
    y = 255
    for line in lines[:4]:
        line = _ellipsize(draw, line, _font(31), 1000)
        draw.text((92, y), line, font=_font(31), fill=_WHITE)
        y += 58

    source_id = decision.source_ids[0] if decision.source_ids else ""
    profile = sources.get(source_id)
    source_name = profile.display_name if profile else source_id
    draw.text((90, 555), f"Verified source: {source_name}", font=_font(25), fill=_MUTED)
    draw.text((905, 555), "FPL VORTEX", font=_font(25, True), fill=_BLUE)

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)
    return str(path)


def _draw_brand(draw: ImageDraw.ImageDraw, image: Image.Image) -> None:
    """Draw the channel logo and name on every verified card.

    Branding is local-only: it cannot introduce an external fetch or change any
    verified football fact shown in the card.
    """
    logo_path = Path("Logo.png")
    if logo_path.exists():
        try:
            logo = Image.open(logo_path).convert("RGBA")
            logo.thumbnail((68, 68), Image.Resampling.LANCZOS)
            x, y = 1070, 65
            image.alpha_composite(logo, (x, y)) if image.mode == "RGBA" else image.paste(
                logo, (x, y), logo
            )
        except Exception:
            # A missing/corrupt decorative asset must never block an official
            # news post; the channel name below remains visible.
            pass
    wordmark = "FPL VORTEX"
    font = _font(24, True)
    width = draw.textbbox((0, 0), wordmark, font=font)[2]
    draw.text((1045 - width, 92), wordmark, font=font, fill=_WHITE)


def _fact_lines(decision: VerificationDecision) -> List[str]:
    f = decision.verified_facts
    if decision.event_type == EventType.INJURY:
        lines = [str(f.get("club_name"))]
        status = str(f.get("injury_status") or "")
        if len(status) <= 140:
            lines.append(status)
        return lines
    if decision.event_type == EventType.SUSPENSION:
        lines = [str(f.get("club_name"))]
        status = str(f.get("suspension_status") or "")
        if len(status) <= 140:
            lines.append(status)
        if f.get("suspension_length"):
            lines.append(f"Length: {f['suspension_length']}")
        return lines
    if decision.event_type == EventType.MANAGER:
        action = str(f.get("manager_action") or "").title()
        return [f"{action} — {f.get('club_name')}"]
    if decision.event_type == EventType.CONTRACT:
        lines = [str(f.get("club_name"))]
        if f.get("contract_length"):
            lines.append(f"Terms: {f['contract_length']}")
        return lines
    topic = str(f.get("statement_topic") or "")
    return [topic] if len(topic) <= 140 else []


def _ellipsize(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    value = " ".join(str(text or "").split())
    if draw.textbbox((0, 0), value, font=font)[2] <= max_width:
        return value
    while value and draw.textbbox((0, 0), value + "…", font=font)[2] > max_width:
        value = value[:-1]
    return value.rstrip() + "…"
