"""Regression tests for the user-approved FPL VORTEX master renderer."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageStat

from src.rendering import CANVAS, MasterGraphicRenderer
from src.verification.card import create_verified_card
from src.verification.models import (
    DecisionType, EventStatus, EventType, GateResult, GateState,
    VerificationDecision,
)
from src.verification.source_registry import SourceRegistry


@pytest.fixture
def sources():
    return SourceRegistry.load()


def decision(event: EventType, facts: dict, *, status=EventStatus.OFFICIAL,
             source_ids=None, authority_kind="first_party_official"):
    source_ids = source_ids or ["official.fpl"]
    return VerificationDecision(
        decision=DecisionType.PUBLISH,
        story_id="preview-" + event.value,
        family_id="preview",
        event_type=event,
        status=status,
        verified_facts=facts,
        source_ids=list(source_ids),
        publisher_groups=list(source_ids),
        gates=[GateResult("authorized", GateState.PASS, "ok")],
        reasons=[], confidence=1.0, confidence_dimensions={},
        evidence_document_ids=["preview"], fingerprint="preview-" + event.value,
        source_url=(
            "https://www.chelseafc.com/en/news/example"
            if source_ids == ["club.chelsea"] else "https://fantasy.premierleague.com/"
        ),
        authority_kind=authority_kind,
        authority_source_ids=list(source_ids),
    )


@pytest.fixture
def no_network_assets(monkeypatch):
    import src.rendering.engine as engine
    monkeypatch.setattr(engine, "resolve_player_image", lambda *a, **k: (None, ""))
    monkeypatch.setattr(engine, "resolve_club_logo", lambda *a, **k: None)
    monkeypatch.setattr(engine, "resolve_player_metadata", lambda *a, **k: {})


@pytest.mark.parametrize("event,facts", [
    (EventType.INJURY, {
        "subject_name": "Dynamic Player", "club_name": "Arsenal",
        "injury_status": "Hamstring injury", "availability_status": "Out",
        "return_date": "Gameweek 5", "severity": "Moderate",
    }),
    (EventType.SUSPENSION, {
        "subject_name": "Dynamic Player", "club_name": "Chelsea",
        "suspension_status": "Red card", "suspension_length": "1 match",
        "matches_to_miss": "Gameweek 12", "return_date": "Gameweek 13",
    }),
    (EventType.PRESS_CONFERENCE, {
        "subject_name": "Dynamic Manager", "club_name": "Arsenal",
        "quote_topic": "Squad fitness", "quote_summary": "The squad is ready.",
    }),
])
def test_categories_render_4k_rgb(tmp_path, sources, no_network_assets, event, facts):
    path = tmp_path / f"{event.value}.png"
    MasterGraphicRenderer(sources).render(decision(event, facts), path)
    with Image.open(path) as image:
        assert image.size == CANVAS
        assert image.mode == "RGB"
        assert max(ImageStat.Stat(image.resize((96, 54))).stddev) > 8


def test_transfer_direction_and_dynamic_values_render(tmp_path, sources, no_network_assets):
    facts = {
        "subject_name": "Dynamic Player", "position": "Midfielder", "age": 23,
        "club_from_name": "Arsenal", "club_to_name": "Chelsea",
        "fee": "£25m", "contract_length": "5 years", "transfer_kind": "permanent",
    }
    path = tmp_path / "transfer.png"
    d = decision(EventType.TRANSFER, facts, status=EventStatus.COMPLETED,
                 source_ids=["club.chelsea"])
    create_verified_card(d, sources, path)
    assert path.exists() and path.stat().st_size > 100_000
    with Image.open(path) as image:
        assert image.size == (3840, 2160)


def test_long_values_fit_without_crashing(tmp_path, sources, no_network_assets):
    facts = {
        "subject_name": "A Very Long Dynamically Supplied Football Player Name",
        "club_from_name": "A Very Long Previous Football Club Name United",
        "club_to_name": "A Very Long Destination Football Club Association",
        "position": "Central Attacking Midfielder",
        "fee": "A dynamically supplied undisclosed performance-based package",
        "contract_length": "Until June 2032 with an optional extension",
        "transfer_kind": "permanent",
    }
    path = tmp_path / "long.png"
    MasterGraphicRenderer(sources).render(
        decision(EventType.TRANSFER, facts, status=EventStatus.COMPLETED,
                 source_ids=["media.fotmob"],
                 authority_kind="structured_fotmob_reported_transfer"),
        path,
    )
    assert path.exists()


def test_reference_and_brand_assets_exist():
    for name in ("injury", "press_conference", "suspension", "transfer"):
        path = Path(f"assets/reference/{name}_reference.png")
        assert path.exists()
        with Image.open(path) as image:
            assert image.size[0] / image.size[1] == pytest.approx(16/9, rel=0.01)
    for path in (
        Path("logo.png"), Path("assets/branding/fpl_lion.jpg"),
        Path("assets/branding/fpl_vortex_full.png"),
        Path("assets/branding/premier_league.png"),
        Path("assets/branding/stadium_texture.jpg"),
    ):
        assert path.exists()


def test_no_reference_player_is_hardcoded_in_renderer():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("src/rendering").glob("*.py")
    ).casefold()
    for forbidden in ("bukayo saka", "bruno fernandes", "pedri"):
        assert forbidden not in source


def test_transfer_logo_rule_coordinates_are_fixed():
    source = Path("src/rendering/engine.py").read_text(encoding="utf-8")
    # Left brand and right PL areas are explicit master-frame zones.
    assert "(55, 24, 320, 325)" in source
    assert "(3440, 28, 3745, 325)" in source
    assert "club_from_name" in source and "club_to_name" in source
