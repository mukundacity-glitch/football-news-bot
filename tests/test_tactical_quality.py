from __future__ import annotations

from datetime import datetime, timezone

from PIL import Image

from src import tactical_agent_v2
from src.rendering import tactical_enhanced
from src.rendering.tactical_enhanced import EnhancedTacticalGraphicRenderer
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


def test_renderer_uses_team_jersey_when_selected_player_image_is_unavailable(monkeypatch, tmp_path):
    logo = Image.new("RGBA", (220, 220), (255, 255, 255, 255))
    jersey = Image.new("RGBA", (900, 1120), (120, 120, 120, 255))

    def fake_remote(url: str, *, minimum: int = 100):
        if "players" in url:
            return None
        return logo.copy()

    monkeypatch.setattr(tactical_enhanced, "_remote_asset", fake_remote)
    monkeypatch.setattr(tactical_enhanced, "resolve_team_shirt", lambda *args, **kwargs: jersey.copy())
    monkeypatch.setattr(tactical_enhanced, "resolve_club_logo", lambda *args, **kwargs: logo.copy())

    post = {
        "heading": "TACTICAL WATCH",
        "topic_line": "Home Club vs Away Club",
        "thesis": "Watch whether Home Club create repeated pressure from set pieces",
        "explanation": "Repeated corners could keep Away Club defending restarts for longer spells.",
        "evidence": [
            {"label": "Recent corners", "value": "6.0 created per match"},
            {"label": "Opponent pressure", "value": "5.0 corners conceded per match"},
            {"label": "FPL angle", "value": "Set pieces can lift goal threat"},
        ],
        "diagram_label": "Set-piece pressure",
        "score_label": "Strong • 70/100",
        "status": "ANALYSIS",
        "source_label": "Verified data",
        "checked_local": "16 Sep 12:00 EDT",
        "assets": {
            "home_logo": {"team": "Home Club", "url": "https://media.api-sports.io/football/teams/1.png"},
            "away_logo": {"team": "Away Club", "url": "https://media.api-sports.io/football/teams/2.png"},
            "hero_player": {
                "kind": "player",
                "name": "Example Player",
                "url": "https://resources.premierleague.com/premierleague/photos/players/250x250/p1.png",
                "club_name": "Home Club",
            },
        },
    }
    output = tmp_path / "jersey-fallback.png"
    EnhancedTacticalGraphicRenderer().render(post, output)
    assert output.exists()
    with Image.open(output) as rendered:
        assert rendered.size == (3840, 2160)


def test_missing_provider_key_records_status_and_exits_cleanly(monkeypatch):
    captured = {}
    fixed_now = datetime(2026, 9, 16, 16, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(tactical_agent_v2.base, "utcnow", lambda: fixed_now)
    monkeypatch.setattr(tactical_agent_v2.base, "save_json", lambda path, value: captured.update(value))
    result = tactical_agent_v2._missing_provider_key("trial")
    assert result == 0
    assert captured["published"] is False
    assert captured["mode"] == "trial"
    assert captured["reason"] == "missing_structured_provider_key"
