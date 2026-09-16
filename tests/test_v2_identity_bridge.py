"""Regression tests for the fail-closed legacy-name to V2 FPL identity bridge."""

from src.verification.entities import EntityRegistry


def _registry(tmp_path):
    fpl = {
        "teams": [
            {"id": 1, "name": "Arsenal", "short_name": "ARS"},
            {"id": 2, "name": "Chelsea", "short_name": "CHE"},
        ],
        "elements": [
            {
                "id": 10,
                "first_name": "Gabriel",
                "second_name": "dos Santos Magalhães",
                "web_name": "Gabriel",
                "team": 1,
            },
            {
                "id": 11,
                "first_name": "Alex John",
                "second_name": "Smith",
                "web_name": "A. Smith",
                "team": 1,
            },
            {
                "id": 12,
                "first_name": "Alex Peter",
                "second_name": "Smith",
                "web_name": "B. Smith",
                "team": 2,
            },
        ],
    }
    return EntityRegistry.from_fpl(fpl, snapshot_path=tmp_path / "missing.json")


def test_compound_official_fpl_name_resolves_from_grounded_media_form(tmp_path):
    registry = _registry(tmp_path)

    player = registry.resolve_player("Gabriel Magalhães")

    assert player is not None
    assert player.id == "player:fpl:10"
    assert player.name == "Gabriel dos Santos Magalhães"
    assert player.validation_source == "official_fpl"
    assert player.confidence == 1.0


def test_identity_bridge_still_fails_closed_for_ambiguous_or_unknown_names(tmp_path):
    registry = _registry(tmp_path)

    assert registry.resolve_player("Alex Smith") is None
    assert registry.resolve_player("Premier League Website") is None
    assert registry.resolve_player("Gabriel dos") is None


def test_current_non_pl_destination_references_resolve_without_open_fallback(tmp_path):
    registry = _registry(tmp_path)

    for name in ("Southend United", "Forest Green Rovers"):
        club = registry.resolve_club(name)
        assert club is not None
        assert club.validation_source == "football_club_reference"
        assert club.confidence == 0.98
        assert club.active_premier_league is False

    assert registry.resolve_club("Completely Invented FC") is None
