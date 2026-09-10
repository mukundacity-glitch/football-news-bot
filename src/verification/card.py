"""Strict entry point for the approved FPL VORTEX image renderer."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Optional

from src.rendering import MasterGraphicRenderer
from .models import EventType, VerificationDecision
from .official_transfer_gate import validate_official_transfer
from .presentation import injury_graphic_facts
from .press_conference_gate import validate_official_press_conference
from .reported_transfer_gate import is_reported_transfer, validate_reported_transfer
from .source_registry import SourceRegistry


class RenderingAuthorizationError(RuntimeError):
    pass


class UnverifiedTransferError(RenderingAuthorizationError):
    pass


class UnverifiedPressConferenceError(RenderingAuthorizationError):
    pass


def create_verified_card(
    decision: VerificationDecision,
    sources: SourceRegistry,
    output_path: str | Path,
    *,
    fpl_data: Optional[dict] = None,
) -> str:
    if not decision.may_publish:
        raise RenderingAuthorizationError("cannot render an unauthorized decision")

    if decision.event_type == EventType.TRANSFER:
        validation = (
            validate_reported_transfer(decision, sources)
            if is_reported_transfer(decision)
            else validate_official_transfer(decision, sources)
        )
        if not validation.ok:
            raise UnverifiedTransferError(validation.reason)
    elif decision.event_type == EventType.PRESS_CONFERENCE:
        validation = validate_official_press_conference(decision, sources)
        if not validation.ok:
            raise UnverifiedPressConferenceError(validation.reason)

    # Keep the verification decision/fingerprint immutable.  The renderer gets a
    # presentation-only copy so the card cannot disagree with the fixed post text.
    render_decision = decision
    if decision.event_type == EventType.INJURY:
        render_decision = replace(
            decision,
            verified_facts=injury_graphic_facts(decision.verified_facts),
        )

    return MasterGraphicRenderer(sources, fpl_data=fpl_data).render(
        render_decision, output_path
    )
