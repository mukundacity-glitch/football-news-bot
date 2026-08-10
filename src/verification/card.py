"""Verified-facts-only image cards for X posts.

Every publishable category (TRANSFER, INJURY, SUSPENSION, PRESS_CONFERENCE)
renders through the same production 3840x2160 (16:9) Playwright pipeline in
``src/renderer.py`` (``create_verified_branded_card``), so a viewer reads a
consistent visual identity no matter which category they see. TRANSFER and
PRESS_CONFERENCE are additionally official-confirmed-only: ``create_verified_card``
never generates either graphic unless the relevant strict gate passes.

If the Playwright renderer is unavailable, a local PIL fallback below keeps
the same 3840x2160 canvas and general layout so a degraded render still looks
like the same product, not a different card.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

from .models import EventType, VerificationDecision
from .official_transfer_gate import (
    log_skipped_unverified_transfer,
    validate_official_transfer,
)
from .press_conference_gate import (
    log_skipped_unverified_press_conference,
    validate_official_press_conference,
)
from .source_registry import SourceRegistry


_BG = (0, 0, 0)
_PANEL = (18, 29, 58)
_WHITE = (247, 249, 255)
_MUTED = (154, 163, 178)
_GREEN = (0, 230, 118)
_BLUE = (73, 141, 255)

# Every card renders at 4K UHD, 16:9 -- one consistent canvas across every
# publishable category (TRANSFER, INJURY, SUSPENSION, PRESS_CONFERENCE).
CARD_SIZE = (3840, 2160)


class UnverifiedTransferError(RuntimeError):
    """Raised when a caller tries to render a transfer card that failed the
    strict official-only validation gate. Callers must never fall back to a
    softer card in this case -- the correct action is to skip the post."""


class UnverifiedPressConferenceError(RuntimeError):
    """Raised when a caller tries to render a press-conference card that
    failed the strict official-only validation gate. Same rule as transfers:
    skip the post, never render a softer/partial card."""


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
        return _create_branded_card(decision, sources, output_path)

    if decision.event_type == EventType.PRESS_CONFERENCE:
        validation = validate_official_press_conference(decision, sources)
        if not validation.ok:
            log_skipped_unverified_press_conference(decision, validation.reason)
            raise UnverifiedPressConferenceError(
                f"SKIPPED_UNVERIFIED_PRESS_CONFERENCE: {validation.reason}"
            )
        return _create_branded_card(decision, sources, output_path)

    return _create_branded_card(decision, sources, output_path)


def _create_branded_card(
    decision: VerificationDecision,
    sources: SourceRegistry,
    output_path: str | Path,
) -> str:
    facts = dict(decision.verified_facts)
    facts["_event_status"] = decision.status.value

    # Use the established FPL VORTEX production card treatment whenever
    # Playwright is available: player image, club crest(s), logo, channel
    # name, and official source handle in the footer, at 3840x2160. The
    # adapter receives only V2 verified facts -- it never sees raw article
    # text and cannot invent a value the decision does not carry.
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

    return _create_fallback_card(decision, sources, output_path)


# ── PIL EMERGENCY FALLBACK (3840x2160, same aspect as production) ────────

def _create_fallback_card(
    decision: VerificationDecision,
    sources: SourceRegistry,
    output_path: str | Path,
) -> str:
    width, height = CARD_SIZE
    image = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(image)
    scale = width / 1200  # legacy layout was authored at 1200-wide; scale up uniformly

    def s(value: float) -> int:
        return round(value * scale)

    draw.rounded_rectangle((s(50), s(45), width - s(50), height - s(45)), radius=s(28), fill=_PANEL)
    draw.rectangle((s(50), s(45), width - s(50), s(55)), fill=_GREEN)

    _draw_brand(draw, image, s)

    event_label = {
        EventType.TRANSFER: "TRANSFER CONFIRMED",
        EventType.INJURY: "OFFICIAL INJURY UPDATE",
        EventType.SUSPENSION: "OFFICIAL SUSPENSION",
        EventType.PRESS_CONFERENCE: "PRESS CONFERENCE",
        EventType.MANAGER: "OFFICIAL MANAGER UPDATE",
        EventType.CONTRACT: "OFFICIAL CONTRACT EXTENSION",
        EventType.OFFICIAL_STATEMENT: "OFFICIAL CLUB STATEMENT",
    }.get(decision.event_type, "OFFICIAL UPDATE")
    draw.text((s(90), s(85)), event_label, font=_font(s(34), True), fill=_GREEN)

    facts = decision.verified_facts
    subject = str(facts.get("subject_name") or facts.get("club_name") or "Official update")
    subject = _ellipsize(draw, subject, _font(s(62), True), width - s(200))
    draw.text((s(90), s(150)), subject, font=_font(s(62), True), fill=_WHITE)

    lines = _fact_lines(decision)
    y = s(255)
    for line in lines[:4]:
        line = _ellipsize(draw, line, _font(s(31)), width - s(200))
        draw.text((s(92), y), line, font=_font(s(31)), fill=_WHITE)
        y += s(58)

    source_id = decision.source_ids[0] if decision.source_ids else ""
    profile = sources.get(source_id)
    source_name = profile.display_name if profile else source_id
    draw.text((s(90), height - s(120)), f"Verified source: {source_name}",
              font=_font(s(25)), fill=_MUTED)
    draw.text((width - s(295), height - s(120)), "FPL VORTEX",
              font=_font(s(25), True), fill=_BLUE)

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)
    return str(path)


def _draw_brand(draw: ImageDraw.ImageDraw, image: Image.Image, s) -> None:
    """Draw the channel logo and name on every verified card.

    Branding is local-only: it cannot introduce an external fetch or change any
    verified football fact shown in the card.
    """
    logo_path = Path("Logo.png")
    if logo_path.exists():
        try:
            logo = Image.open(logo_path).convert("RGBA")
            box = s(68)
            logo.thumbnail((box, box), Image.Resampling.LANCZOS)
            x, y = image.width - s(130), s(65)
            image.alpha_composite(logo, (x, y)) if image.mode == "RGBA" else image.paste(
                logo, (x, y), logo
            )
        except Exception:
            # A missing/corrupt decorative asset must never block an official
            # news post; the channel name below remains visible.
            pass
    wordmark = "FPL VORTEX"
    font = _font(s(24), True)
    width = draw.textbbox((0, 0), wordmark, font=font)[2]
    draw.text((image.width - s(155) - width, s(92)), wordmark, font=font, fill=_WHITE)


def _fact_lines(decision: VerificationDecision) -> List[str]:
    f = decision.verified_facts
    if decision.event_type == EventType.TRANSFER:
        origin = str(f.get("club_from_name") or "")
        destination = str(f.get("club_to_name") or "")
        lines = [f"{origin} → {destination}" if origin else f"Joined {destination}"]
        fee = f.get("fee")
        lines.append(f"Fee: {fee}" if fee else "Fee: undisclosed")
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
    if decision.event_type == EventType.PRESS_CONFERENCE:
        lines = [str(f.get("club_name"))]
        quote = str(f.get("quote_summary") or "")
        if len(quote) <= 140:
            lines.append(quote)
        if f.get("quote_topic"):
            lines.append(str(f["quote_topic"]))
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
