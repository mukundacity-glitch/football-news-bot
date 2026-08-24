"""Exact future caption templates approved by the owner."""
from __future__ import annotations

from src.rendering.engine import MasterGraphicRenderer
from src.verification.models import (
    DecisionType, EventStatus, EventType, GateResult, GateState,
    VerificationDecision,
)
from src.verification.renderer import VerifiedPostRenderer, twitter_weight
from src.verification.source_registry import SourceRegistry


def decision(event, facts, *, status=EventStatus.OFFICIAL,
             source_ids=None, authority_kind="first_party_official"):
    source_ids = source_ids or ["official.fpl"]
    return VerificationDecision(
        decision=DecisionType.PUBLISH, story_id="template", family_id="template",
        event_type=event, status=status, verified_facts=facts,
        source_ids=source_ids, publisher_groups=source_ids,
        gates=[GateResult("ok", GateState.PASS, "ok")], reasons=[], confidence=1.0,
        confidence_dimensions={}, evidence_document_ids=["template"],
        fingerprint="template", source_url="https://example.com",
        authority_kind=authority_kind, authority_source_ids=source_ids,
    )


def render(value):
    text = VerifiedPostRenderer(SourceRegistry.load()).render(value)
    assert twitter_weight(text) <= 280
    assert "http" not in text
    lines = text.splitlines()
    assert len(lines) == 6
    assert all(lines[index].startswith("#") for index in (4, 5))
    assert all(not lines[index].startswith("#") for index in range(4))
    return text


def test_reported_transfer_template_exact():
    text = render(decision(
        EventType.TRANSFER,
        {"subject_name": "Gerónimo Rulli", "club_from_name": "Marseille",
         "club_to_name": "Man City", "structured_source": "fotmob_transfer_table"},
        status=EventStatus.COMPLETED, source_ids=["media.fotmob"],
        authority_kind="structured_fotmob_reported_transfer",
    ))
    assert text == (
        "🚨 REPORTED TRANSFER — Gerónimo Rulli\n"
        "Marseille → Man City\n"
        "Verified by — FotMob\n"
        "STATUS — COMPLETED\n"
        "#TransferNews #PremierLeague #FPL\n"
        "#ManCity #GeronimoRulli #FPLNews #FPLVortex"
    )


def test_official_transfer_uses_official_status():
    text = render(decision(
        EventType.TRANSFER,
        {"subject_name": "Dynamic Player", "club_from_name": "Arsenal",
         "club_to_name": "Chelsea"},
        status=EventStatus.COMPLETED, source_ids=["club.chelsea"],
    ))
    assert "STATUS — OFFICIAL" in text
    assert "Arsenal → Chelsea" in text


def test_suspension_template_exact():
    text = render(decision(
        EventType.SUSPENSION,
        {"subject_name": "Dynamic Player", "club_name": "Arsenal",
         "suspension_status": "Red card"},
    ))
    assert text == (
        "⛔ SUSPENSION UPDATE — Dynamic Player\n"
        "Arsenal — Red card\n"
        "Verified by — Official FPL\n"
        "STATUS — SUSPENDED\n"
        "#SuspensionNews #PremierLeague #FPL\n"
        "#Arsenal #DynamicPlayer #FPLNews #FPLVortex"
    )


def test_injury_template_exact():
    text = render(decision(
        EventType.INJURY,
        {"subject_name": "Dynamic Player", "club_name": "Arsenal",
         "injury_status": "Hamstring injury", "availability_status": "OUT"},
    ))
    assert text == (
        "🚑 INJURY UPDATE — Dynamic Player\n"
        "Arsenal — Hamstring injury\n"
        "Verified by — Official FPL\n"
        "STATUS — OUT\n"
        "#InjuryNews #PremierLeague #FPL\n"
        "#Arsenal #DynamicPlayer #FPLNews #FPLVortex"
    )


def test_press_template_exact():
    text = render(decision(
        EventType.PRESS_CONFERENCE,
        {"subject_name": "Mikel Arteta", "club_name": "Arsenal",
         "quote_summary": "The squad is ready for the season"},
    ))
    assert text == (
        "🎙️ PREMIER LEAGUE PRESS UPDATE\n"
        "Mikel Arteta — Arsenal\n"
        "Key update — The squad is ready for the season\n"
        "STATUS — CONFIRMED\n"
        "#PressConference #PremierLeague #FPL\n"
        "#Arsenal #MikelArteta #FPLNews #FPLVortex"
    )


def test_position_abbreviations_expand_to_bright_display_categories():
    assert MasterGraphicRenderer._full_position("LW") == "MIDFIELDER"
    assert MasterGraphicRenderer._full_position("MID") == "MIDFIELDER"
    assert MasterGraphicRenderer._full_position("CB") == "DEFENDER"
    assert MasterGraphicRenderer._full_position("GK") == "GOALKEEPER"
    assert MasterGraphicRenderer._full_position("ST") == "FORWARD"


def test_extreme_verified_values_keep_six_lines_inside_normal_x_limit():
    text = render(decision(
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
    ))
    assert len(text.splitlines()) == 6
    assert twitter_weight(text) <= 280
    assert text.splitlines()[3] == "STATUS — CONFIRMED"
    assert text.splitlines()[4].startswith("#PressConference")
