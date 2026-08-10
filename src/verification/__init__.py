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

# The legacy verifier and V2 engine are intentionally kept intact, but the
# publication boundary gets one additional non-bypassable transfer gate.  It
# can only turn a candidate into REJECT; it never upgrades a decision.
from ..transfer_safety import validate_before_publish


def _install_transfer_safety_boundary() -> None:
    from . import engine as _engine

    original = _engine.VerificationEngine.verify
    if getattr(original, "_transfer_safety_wrapped", False):
        return

    def guarded_verify(self, claims, *, now=None):
        decision = original(self, claims, now=now)
        if decision.event_type != EventType.TRANSFER:
            return decision

        story = decision.verified_facts or {}
        # The gate is deliberately evaluated against the complete claim set,
        # including conflicting evidence, so a later official-looking phrase
        # cannot hide an earlier contradiction.
        verdict, reason = validate_before_publish(story, claims, event="TRANSFER")
        gate = GateResult(
            "transfer_publication_safety",
            GateState.PASS if verdict == "ALLOW" else GateState.FAIL,
            reason,
        )
        decision.gates.append(gate)
        if verdict != "ALLOW":
            decision.decision = DecisionType.REJECT
            decision.reasons.append(f"transfer_publication_safety:{reason}")
            decision.rendered_text = None
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
