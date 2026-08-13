"""Premier League news verification engine v2."""

from .models import (
    Claim,
    DecisionType,
    EventStatus,
    EventType,
    GateResult,
    GateState,
    VerificationDecision,
)
from .runtime import RuntimeUnavailable, VerificationRuntime
from ..transfer_safety import validate_before_publish
from .reported_transfer_gate import (
    is_reported_transfer,
    validate_reported_transfer,
)


def _install_transfer_safety_boundary() -> None:
    """Add a final transfer safety check without creating an authority bypass."""
    from . import engine as _engine

    original = _engine.VerificationEngine.verify
    if getattr(original, "_transfer_safety_wrapped", False):
        return

    def guarded_verify(self, claims, *, now=None):
        decision = original(self, claims, now=now)
        if decision.event_type != EventType.TRANSFER:
            return decision

        story = decision.verified_facts or {}
        if is_reported_transfer(decision):
            check = validate_reported_transfer(decision, self.sources)
            verdict, reason = ("ALLOW", check.reason) if check.ok else ("REJECT", check.reason)
        else:
            verdict, reason = validate_before_publish(story, claims, event="TRANSFER")
        decision.gates.append(GateResult(
            "transfer_publication_safety",
            GateState.PASS if verdict == "ALLOW" else GateState.FAIL,
            reason,
        ))
        if decision.decision != DecisionType.PUBLISH:
            return decision
        if verdict != "ALLOW":
            decision.decision = DecisionType.REJECT
            decision.reasons.append(f"transfer_publication_safety:{reason}")
            decision.rendered_text = None
            return decision
        return decision

    guarded_verify._transfer_safety_wrapped = True
    _engine.VerificationEngine.verify = guarded_verify


def _install_premium_card_renderer() -> None:
    """Install the 4K renderer behind the same strict publication gates."""
    try:
        from . import card as _card
        from .premium_cards import render_verified_card
        from .official_transfer_gate import validate_official_transfer, log_skipped_unverified_transfer
        from .reported_transfer_gate import (
            is_reported_transfer as is_reported,
            validate_reported_transfer,
        )
        from .press_conference_gate import validate_official_press_conference, log_skipped_unverified_press_conference
    except Exception:
        return

    def guarded_render(decision, sources, output_path, *, fpl_data=None):
        if not decision.may_publish:
            raise ValueError("cannot render card for unverified decision")
        if decision.event_type == EventType.TRANSFER:
            check = (
                validate_reported_transfer(decision, sources)
                if is_reported(decision)
                else validate_official_transfer(decision, sources)
            )
            if not check.ok:
                log_skipped_unverified_transfer(decision, check.reason)
                from .card import UnverifiedTransferError
                raise UnverifiedTransferError(f"SKIPPED_UNVERIFIED_TRANSFER: {check.reason}")
        elif decision.event_type == EventType.PRESS_CONFERENCE:
            check = validate_official_press_conference(decision, sources)
            if not check.ok:
                log_skipped_unverified_press_conference(decision, check.reason)
                from .card import UnverifiedPressConferenceError
                raise UnverifiedPressConferenceError(f"SKIPPED_UNVERIFIED_PRESS_CONFERENCE: {check.reason}")
        return render_verified_card(decision, sources, output_path, fpl_data=fpl_data)

    _card.create_verified_card = guarded_render


_install_transfer_safety_boundary()
_install_premium_card_renderer()

__all__ = [
    "Claim",
    "DecisionType",
    "EventStatus",
    "VerificationDecision",
    "RuntimeUnavailable",
    "VerificationRuntime",
]
