from __future__ import annotations

from src.verification.models import (
    DecisionType,
    EventStatus,
    EventType,
    GateResult,
    GateState,
    VerificationDecision,
)
from src.verification.presentation import (
    injury_display_parts,
    injury_display_status,
    injury_graphic_facts,
)


def _decision(facts: dict) -> VerificationDecision:
    return VerificationDecision(
        decision=DecisionType.PUBLISH,
        story_id="injury-presentation",
        family_id="injury-presentation",
        event_type=EventType.INJURY,
        status=EventStatus.OFFICIAL,
        verified_facts=facts,
        source_ids=["official.fpl"],
        publisher_groups=["official.fpl"],
        gates=[GateResult("ok", GateState.PASS, "ok")],
        reasons=[],
        confidence=1.0,
        confidence_dimensions={},
        evidence_document_ids=["official-fpl"],
        fingerprint="unchanged-fingerprint",
        authority_kind="first_party_official",
        authority_source_ids=["official.fpl"],
    )


def test_unknown_return_text_cannot_present_as_returning():
    facts = {
        "subject_name": "Dynamic Player",
        "club_name": "Sunderland",
        "injury_status": "Knee injury - Unknown return date",
        "availability_status": "RETURNING",
    }

    assert injury_display_status(facts) == "OUT"
    assert injury_display_parts(facts) == ("Knee injury", "Unknown return date")


def test_positive_returning_evidence_stays_returning():
    facts = {
        "injury_status": "Back in training following recovery",
        "availability_status": "RETURNING",
    }
    assert injury_display_status(facts) == "RETURNING"


def test_graphic_projection_uses_same_status_and_does_not_repeat_unknown_return():
    facts = {
        "subject_name": "Dynamic Player",
        "club_name": "Sunderland",
        "injury_status": "Knee injury",
        "return_date": "Unknown return date",
        "availability_status": "RETURNING",
    }

    projected = injury_graphic_facts(facts)

    assert projected["availability_status"] == "OUT"
    assert projected["injury_status"] == "Knee injury - Unknown return date"
    assert "return_date" not in projected
    assert facts["availability_status"] == "RETURNING"
    assert facts["return_date"] == "Unknown return date"


def test_card_projection_keeps_original_verified_decision_immutable(monkeypatch, tmp_path):
    import src.verification.card as card

    original_facts = {
        "subject_name": "Dynamic Player",
        "club_name": "Sunderland",
        "injury_status": "Knee injury - Unknown return date",
        "availability_status": "RETURNING",
    }
    decision = _decision(original_facts.copy())
    captured = {}

    class Renderer:
        def __init__(self, sources, *, fpl_data=None):
            captured["sources"] = sources
            captured["fpl_data"] = fpl_data

        def render(self, value, output_path):
            captured["decision"] = value
            return str(output_path)

    monkeypatch.setattr(card, "MasterGraphicRenderer", Renderer)
    output = tmp_path / "injury.png"

    assert card.create_verified_card(decision, object(), output) == str(output)
    assert captured["decision"] is not decision
    assert captured["decision"].verified_facts["availability_status"] == "OUT"
    assert decision.verified_facts == original_facts
    assert decision.fingerprint == "unchanged-fingerprint"
