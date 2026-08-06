"""Simple verified-facts-only image card for X posts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from PIL import Image, ImageDraw, ImageFont

from .models import EventType, VerificationDecision
from .source_registry import SourceRegistry


_BG = (9, 16, 35)
_PANEL = (18, 29, 58)
_WHITE = (247, 249, 255)
_MUTED = (173, 188, 216)
_GREEN = (46, 204, 113)
_BLUE = (73, 141, 255)


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    names = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
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
) -> str:
    if not decision.may_publish:
        raise ValueError("cannot render card for unverified decision")
    facts = dict(decision.verified_facts)
    facts["_event_status"] = decision.status.value

    # Use the established FPL VORTEX player-card treatment whenever Playwright
    # is available: player image, club crest, logo, channel name, and official
    # source handle in the footer.  The adapter receives only V2 verified facts.
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

    # Keep the production V2 card fact-only, while restoring the FPL VORTEX
    # identity that was present on the original player-card design.  The local
    # logo is deliberately embedded in every image so a repost/crop still has
    # an unambiguous channel mark.
    _draw_brand(draw, image)

    event_label = {
        EventType.TRANSFER: (
            "MEDICAL / DEAL AGREED"
            if facts.get("_event_status") in {"MEDICAL", "AGREEMENT", "HERE_WE_GO"}
            else "CONFIRMED TRANSFER"
        ),
        EventType.INJURY: "OFFICIAL INJURY UPDATE",
        EventType.SUSPENSION: "OFFICIAL SUSPENSION",
        EventType.MANAGER: "OFFICIAL MANAGER UPDATE",
        EventType.CONTRACT: "OFFICIAL CONTRACT EXTENSION",
        EventType.OFFICIAL_STATEMENT: "OFFICIAL CLUB STATEMENT",
    }[decision.event_type]
    draw.text((90, 85), event_label, font=_font(34, True), fill=_GREEN)

    subject = str(
        facts.get("subject_name") or facts.get("club_name") or "Official update"
    )
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
    if decision.event_type == EventType.TRANSFER:
        destination = f.get("club_to_name")
        origin = f.get("club_from_name")
        route = f"{origin} → {destination}" if origin else f"Joined {destination}"
        lines = [route]
        if f.get("transfer_kind"):
            lines.append(f"Move type: {str(f['transfer_kind']).title()}")
        if f.get("fee"):
            lines.append(f"Fee: {f['fee']}")
        if f.get("contract_length"):
            lines.append(f"Contract: {f['contract_length']}")
        return lines
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
