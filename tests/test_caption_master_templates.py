"""Exact future caption templates approved by the owner."""
from __future__ import annotations

from src.rendering.engine import MasterGraphicRenderer
from src.verification.models import (
    DecisionType,
    EventStatus,
    EventType,
    GateResult,
    GateState,
    VerificationDecision,
)
from src.verification.renderer import VerifiedPostRenderer, twitter_weight
from src.verification.source_registry import SourceRegistry


def decision(
    event,
    facts,
    *,
    status=EventStatus.OFFICIAL,
    source_ids=None,
    authority_kind="first_party_official",
):
    source_ids = source_ids or ["official.fpl"]
    return VerificationDecision(
        decision=DecisionType.PUBLISH,
        story_id="template",
        family_id="template",
        event_type=event,
        status=status,
        verified_facts=facts,
        source_ids=source_ids,
        publisher_groups=source_ids,
        gates=[GateResult("ok", GateState.PASS, "ok")],
        reasons=[],
        confidence=1.0,
        confidence_dimensions={},
        evidence_document_ids=["template"],
        fingerprint="template",
        source_url="https://example.com",
        authority_kind=authority_kind,
        authority_source_ids=source_ids,
    )


def render(value):
    text = VerifiedPostRenderer(SourceRegistry.load()).render(value)
    assert twitter_weight(text) <= 280
    assert "http" not in text
    assert "Verified by" not in text
    lines = text.splitlines()
    assert len(lines) == 6
    assert lines[3] == ""
    assert not any(line.startswith("#") for line in lines[:5])
    assert len(lines[5].split()) == 3
    assert all(tag.startswith("#") for tag in lines[5].split())
    return text


def test_reported_transfer_template_exact():
    text = render(
        decision(
            EventType.TRANSFER,
            {
                "subject_name": "Gerónimo Rulli",
                "club_from_name": "Marseille",
                "club_to_name": "Man City",
                "structured_source": "fotmob_transfer_table",
            },
            status=EventStatus.COMPLETED,
            source_ids=["media.fotmob"],
            authority_kind="structured_fotmob_reported_transfer",
        )
    )
    assert text == (
        "🚨 REPORTED TRANSFER — Gerónimo Rulli\n"
        "Marseille → Man City\n"
        "Status: COMPLETED\n"
        "\n"
        "Monitor for confirmation before making an FPL move.\n"
        "#FPL #TransferNews #ManCity"
    )


def test_official_transfer_uses_official_status_and_three_hashtags():
    text = render(
        decision(
            EventType.TRANSFER,
            {
                "subject_name": "Dynamic Player",
                "club_from_name": "Arsenal",
                "club_to_name": "Chelsea",
            },
            status=EventStatus.COMPLETED,
            source_ids=["club.chelsea"],
        )
    )
    assert "Status: OFFICIAL" in text
    assert "Arsenal → Chelsea" in text
    assert text.splitlines()[-1] == "#FPL #TransferNews #Chelsea"


def test_suspension_template_exact():
    text = render(
        decision(
            EventType.SUSPENSION,
            {
                "subject_name": "Dynamic Player",
                "club_name": "Arsenal",
                "suspension_status": "Red card",
            },
        )
    )
    assert text == (
        "⛔ SUSPENSION UPDATE — Dynamic Player\n"
        "Arsenal | Red card\n"
        "Status: SUSPENDED\n"
        "\n"
        "Check your squad before the deadline.\n"
        "#FPL #SuspensionNews #Arsenal"
    )


def test_injury_template_exact_for_unknown_return():
    text = render(
        decision(
            EventType.INJURY,
            {
                "subject_name": "Dynamic Player",
                "club_name": "Arsenal",
                "injury_status": "Knee injury - Unknown return date",
                "availability_status": "RETURNING",
            },
        )
    )
    assert text == (
        "🚑 INJURY UPDATE — Dynamic Player\n"
        "Arsenal | Knee injury – Unknown return date\n"
        "Status: OUT\n"
        "\n"
        "Monitor for more news if you own him.\n"
        "#FPL #InjuryNews #Arsenal"
    )


def test_real_returning_cue_stays_returning():
    text = render(
        decision(
            EventType.INJURY,
            {
                "subject_name": "Dynamic Player",
                "club_name": "Arsenal",
                "injury_status": "Back in training following recovery",
                "availability_status": "RETURNING",
            },
        )
    )
    assert "Status: RETURNING" in text


def test_press_template_exact():
    text = render(
        decision(
            EventType.PRESS_CONFERENCE,
            {
                "subject_name": "Mikel Arteta",
                "club_name": "Arsenal",
                "quote_summary": "The squad is ready for the season",
            },
        )
    )
    assert text == (
        "🎙️ PRESS CONFERENCE — Mikel Arteta\n"
        "Arsenal | The squad is ready for the season\n"
        "Status: CONFIRMED\n"
        "\n"
        "Use the update when planning your next FPL move.\n"
        "#FPL #PressConference #Arsenal"
    )


def test_position_abbreviations_expand_to_bright_display_categories():
    assert MasterGraphicRenderer._full_position("LW") == "MIDFIELDER"
    assert MasterGraphicRenderer._full_position("MID") == "MIDFIELDER"
    assert MasterGraphicRenderer._full_position("CB") == "DEFENDER"
    assert MasterGraphicRenderer._full_position("GK") == "GOALKEEPER"
    assert MasterGraphicRenderer._full_position("ST") == "FORWARD"


def test_extreme_verified_values_keep_fixed_shape_inside_normal_x_limit():
    text = render(
        decision(
            EventType.PRESS_CONFERENCE,
            {
                "subject_name": "A Manager With An Extremely Long Multi-Part Football Name",
                "club_name": "A Very Long Premier League Football Club Association Name",
                "quote_summary": (
                    "The medical team will make a final decision after training because "
                    "several players are progressing well but still require careful "
                    "assessment before the next Premier League fixture."
                ),
            },
        )
    )
    lines = text.splitlines()
    assert len(lines) == 6
    assert lines[2] == "Status: CONFIRMED"
    assert lines[3] == ""
    assert lines[5].startswith("#FPL #PressConference ")
    assert twitter_weight(text) <= 280
