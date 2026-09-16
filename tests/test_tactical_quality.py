from __future__ import annotations

from datetime import datetime, timezone

from src.tactical_intelligence import Candidate, Fixture
from src.tactical_quality import score_tier, story_quality_gate, visual_assets


def _fixture() -> Fixture:
    return Fixture(
        fixture_id=321,
        home_id=1,
        away_id=2,
        home="Home Club",
        away="Away Club",
        kickoff=datetime(2026, 9, 19, 14, 0, tzinfo=timezone.utc),
        finished=False,
        home_score=None,
        away_score=None,
    )


def _candidate(*, score: int = 70, mode: str = "watch", focus: str = "set_piece") -> Candidate:
    return Candidate(
        mode,
        _fixture(),
        {
            "thesis": "Watch whether Home Club create repeated pressure from set pieces",
            "diagram_focus": focus,
            "evidence": [
                {"label": "Recent corners", "value": "6.0 created per match"},
                {"label": "Opponent pressure", "value": "5.0 corners conceded per match"},
            ],
        },
        score,
        [],
    )


def test_priority_tiers_match_editorial_policy():
    assert score_tier(91) == "PRIORITY"
    assert score_tier(75) == "PRIORITY"
    assert score_tier(74) == "STRONG"
    assert score_tier(60) == "STRONG"
    assert score_tier(59) == "TREND"
    assert score_tier(45) == "TREND"
    assert score_tier(44) == "REJECT"


def test_generic_single_stat_review_is_rejected():
    candidate = _candidate(mode="review", focus="pressure")
    allowed, reason = story_quality_gate(candidate)
    assert allowed is False
    assert reason == "generic_single_stat_review"


def test_verified_multi_evidence_watch_passes_quality_gate():
    candidate = _candidate(mode="watch", focus="set_piece")
    allowed, reason = story_quality_gate(candidate)
    assert allowed is True
    assert reason == "verified_tactical_pattern"


def test_missing_player_image_metadata_falls_back_to_verified_team_jersey():
    candidate = _candidate()
    provider_row = {
        "teams": {
            "home": {"name": "Home Club", "logo": "https://media.api-sports.io/football/teams/1.png"},
            "away": {"name": "Away Club", "logo": "https://media.api-sports.io/football/teams/2.png"},
        }
    }
    bootstrap = {"elements": []}
    assets = visual_assets(candidate, bootstrap, provider_row)
    assert assets["home_logo"]["team"] == "Home Club"
    assert assets["away_logo"]["team"] == "Away Club"
    assert assets["hero_player"]["kind"] == "team_shirt"
    assert assets["hero_player"]["club_name"] == "Home Club"


def test_team_logo_urls_are_optional_because_renderer_has_verified_logo_fallback():
    candidate = _candidate()
    provider_row = {"teams": {"home": {"name": "Home Club"}, "away": {"name": "Away Club"}}}
    assets = visual_assets(candidate, {"elements": []}, provider_row)
    assert assets["home_logo"]["url"] == ""
    assert assets["away_logo"]["url"] == ""
    assert assets["hero_player"]["kind"] == "team_shirt"
