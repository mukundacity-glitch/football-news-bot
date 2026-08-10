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

        if verdict != "ALLOW":
            decision.decision = DecisionType.REJECT
            decision.reasons.append(f"transfer_publication_safety:{reason}")
            decision.rendered_text = None
            return decision

        # The normal V2 engine intentionally defaults to first-party-only
        # transfer authority. This wrapper permits the explicitly configured
        # trusted-source policy ONLY when every other critical gate already
        # passes and the final safety module independently found explicit
        # completed-transfer evidence. It never promotes HERE_WE_GO, MEDICAL,
        # AGREEMENT, BID, TALKS, or any speculative milestone.
        non_source_failures = [
            g for g in decision.gates
            if g.critical
            and g.state != GateState.PASS
            and g.name not in {"source_provenance", "official_confirmation"}
        ]
        approved_trusted = any(
            source_id in {
                "journalist.fabrizio_romano",
                "journalist.david_ornstein",
                "media.bbc_sport",
                "media.sky_sports",
                "media.the_athletic",
                "media.espn",
            }
            for source_id in decision.source_ids
        )
        official_source = any(
            getattr(self.sources.get(source_id), "is_official", False)
            for source_id in decision.source_ids
        )

        if official_source:
            return decision

        if not approved_trusted or non_source_failures:
            decision.decision = DecisionType.REJECT
            decision.reasons.append(
                "transfer_publication_safety:trusted_source_did_not_clear_all_other_gates"
            )
            decision.rendered_text = None
            return decision

        # Re-score the decision with the explicit trusted-source confirmation
        # dimension at 0.95. This is intentionally lower than first-party 1.0,
        # but still requires every other critical validator to pass.
        try:
            dims = dict(decision.confidence_dimensions)
            dims["official_confirmation"] = 0.95
            confidence = _engine.weighted_geometric_mean(
                dims, self.config.confidence_weights
            )
            decision.confidence_dimensions = dims
            decision.confidence = confidence
            confidence_gate = decision.gate("overall_confidence")
            if confidence_gate is not None:
                decision.gates.remove(confidence_gate)
                decision.gates.append(GateResult(
                    "overall_confidence",
                    GateState.PASS if confidence >= self.config.threshold("overall_confidence_min") else GateState.FAIL,
                    f"approved trusted-source completion confidence={confidence:.3f}",
                    value=confidence,
                ))
            if confidence < self.config.threshold("overall_confidence_min"):
                decision.decision = DecisionType.REJECT
                decision.reasons.append("transfer_publication_safety:confidence_below_threshold")
                decision.rendered_text = None
                return decision

            for gate_name in ("source_provenance", "official_confirmation"):
                gate = decision.gate(gate_name)
                if gate is not None:
                    decision.gates.remove(gate)
                    decision.gates.append(GateResult(
                        gate_name,
                        GateState.PASS,
                        "approved trusted source + explicit completed-transfer evidence",
                        value=0.95,
                    ))

            decision.decision = DecisionType.PUBLISH
            decision.reasons = [r for r in decision.reasons if "source_provenance:" not in r]
            # Persist the promoted decision so post_item()'s integrity check
            # cannot publish a decision that exists only in memory.
            self.repository.upsert_story(decision, claims)
            self.repository.record_decision(decision)
            decision.rendered_text = self.renderer.render(decision)
            return decision
        except Exception as exc:
            decision.decision = DecisionType.REJECT
            decision.reasons.append(f"transfer_publication_safety:promotion_error:{exc}")
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
    "VerificationDecision",
]
