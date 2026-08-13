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



_install_transfer_safety_boundary()

__all__ = [
    "Claim",
    "DecisionType",
    "EventStatus",
    "VerificationDecision",
    "RuntimeUnavailable",
    "VerificationRuntime",
]
