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
        verdict, reason = validate_before_publish(story, claims, event="TRANSFER")
        decision.gates.append(GateResult(
            "transfer_publication_safety",
            GateState.PASS if verdict == "ALLOW" else GateState.FAIL,
            reason,
        ))

        # This boundary is rejection-only. It is forbidden to promote a
        # PENDING/REJECTED journalist or media claim to PUBLISH.
        if decision.decision != DecisionType.PUBLISH:
            return decision

        # A transfer already authorized by V2 must independently pass the final
        # completion/source/destination gate. Otherwise fail closed.
        if verdict != "ALLOW":
            decision.decision = DecisionType.REJECT
            decision.reasons.append(f"transfer_publication_safety:{reason}")
            decision.rendered_text = None
            return decision

        return decision

    guarded_verify._transfer_safety_wrapped = True
    _engine.VerificationEngine.verify = guarded_verify


def _install_premium_card_renderer() -> None:
    """Replace the legacy V2 card surface with the single 4K design system."""
    try:
        from . import card as _card
        from .premium_cards import render_verified_card
        _card.create_verified_card = render_verified_card
    except Exception:
        # Renderer availability must not weaken publication safety. If the
        # premium renderer cannot load, normal callers will fail closed.
        pass


_install_transfer_safety_boundary()
_install_premium_card_renderer()

__all__ = [
    "Claim",
    "DecisionType",
    "EventStatus",
    "EventType",
    "VerificationDecision",
    "RuntimeUnavailable",
    "VerificationRuntime",
]
