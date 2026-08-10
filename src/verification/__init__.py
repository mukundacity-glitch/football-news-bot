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
    """Add a final transfer safety check without creating an authority bypass.

    The V2 engine decides whether a story is publishable. This boundary is
    defense-in-depth only: it may reject an already-authorized transfer, but it
    can NEVER promote PENDING/REJECTED media or journalist claims to PUBLISH.
    That is the critical distinction between validation and authorization.
    """
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
        safety_gate = GateResult(
            "transfer_publication_safety",
            GateState.PASS if verdict == "ALLOW" else GateState.FAIL,
            reason,
        )
        decision.gates.append(safety_gate)

        # Never turn a non-publishable claim into a publishable one here.
        # Rumours, speculation, media reports, HERE WE GO, agreements, medicals,
        # bids, talks and other non-official claims remain PENDING/REJECTED.
        if decision.decision != DecisionType.PUBLISH:
            if verdict != "ALLOW" and decision.decision == DecisionType.PUBLISH:
                decision.decision = DecisionType.REJECT
            return decision

        # A transfer already authorized by V2 must independently pass the final
        # completion/source/destination gate. If it does not, fail closed.
        if verdict != "ALLOW":
            decision.decision = DecisionType.REJECT
            decision.reasons.append(f"transfer_publication_safety:{reason}")
            decision.rendered_text = None
            return decision

        return decision

    guarded_verify._transfer_safety_wrapped = True
    _engine.VerificationEngine.verify = guarded_verify


_install_transfer_safety_boundary()

__all__ = [
    "Claim",
    "DecisionType",
    "EventStatus",
    "EventType",
    "VerificationDecision",
    "RuntimeUnavailable",
    "VerificationRuntime",
]
