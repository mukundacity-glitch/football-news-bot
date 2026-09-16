from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src import fpl_vortex_day1 as day1
from src import tactical_intelligence as base


def _fixture(*, finished: bool = False) -> base.Fixture:
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    kickoff = now - timedelta(hours=3) if finished else now + timedelta(hours=24)
    return base.Fixture(
        fixture_id=321,
        home_id=1,
        away_id=2,
        home="Home Club",
        away="Away Club",
        kickoff=kickoff,
        finished=finished,
        home_score=2 if finished else None,
        away_score=1 if finished else None,
    )


def _bootstrap() -> dict:
    return {
        "elements": [
            {
                "id": 22,
                "team": 1,
                "code": 222,
                "first_name": "Example",
                "second_name": "Player",
                "web_name": "Example",
                "status": "a",
                "selected_by_percent": "18.0",
                "form": "6.0",
                "minutes": 360,
                "expected_goal_involvements_per_90": "0.65",
                "penalties_order": 1,
            }
        ]
    }


def _summary(fixture_id: int = 321) -> base.Retrieved:
    rows = [
        {
            "fixture": fixture_id - 2,
            "round": 2,
            "minutes": 82,
            "expected_goal_involvements": "0.30",
            "threat": "42",
            "creativity": "35",
            "total_points": 5,
        },
        {
            "fixture": fixture_id - 1,
            "round": 3,
            "minutes": 90,
            "expected_goal_involvements": "0.40",
            "threat": "51",
            "creativity": "40",
            "total_points": 7,
        },
        {
            "fixture": fixture_id,
            "round": 4,
            "minutes": 88,
            "expected_goal_involvements": "0.55",
            "threat": "60",
            "creativity": "46",
            "total_points": 8,
        },
    ]
    return base.Retrieved(
        data={"history": rows},
        url=f"https://fantasy.premierleague.com/api/element-summary/22/",
        checked_at="2026-09-16T12:00:00+00:00",
        digest="abc123",
    )


def test_day1_request_adapter_uses_checked_out_request_layer(monkeypatch):
    seen = {}

    def fake_getter(url: str):
        seen["url"] = url
        return {"elements": [{"id": 1}]}

    monkeypatch.setattr(day1, "_day1_getter", lambda: fake_getter)
    result = day1.request_json("https://fantasy.premierleague.com/api/bootstrap-static/")
    assert seen["url"].endswith("/bootstrap-static/")
    assert result.data["elements"][0]["id"] == 1
    assert len(result.digest) == 64


def test_free_watch_builds_from_official_fpl_player_history(monkeypatch):
    monkeypatch.setattr(day1, "_summary", lambda player_id: _summary())
    fixture = _fixture()
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    candidate = day1._build_watch_candidate(
        fixture,
        {1: {}, 2: {}},
        {1: 100.0, 2: 50.0},
        _bootstrap(),
        now,
        ZoneInfo("America/New_York"),
        [],
    )
    assert candidate is not None
    assert candidate.post["source_label"] == "Official FPL via FPL Vortex Day 1"
    assert candidate.post["hero_player_id"] == 22
    assert candidate.post["assets"]["hero_player"]["name"] == "Example Player"
    assert candidate.priority_score >= 45


def test_free_review_uses_fixture_specific_fpl_history(monkeypatch):
    monkeypatch.setattr(day1, "_summary", lambda player_id: _summary(321))
    fixture = _fixture(finished=True)
    now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    candidate = day1._build_review_candidate(
        fixture,
        {1: {}, 2: {}},
        {1: 100.0, 2: 50.0},
        _bootstrap(),
        now,
        ZoneInfo("America/New_York"),
        [],
    )
    assert candidate is not None
    assert candidate.post["topic_line"] == "Home Club 2-1 Away Club"
    assert candidate.post["hero_player_id"] == 22
    assert candidate.post["diagram_focus"] in {"set_piece", "player_role"}
    assert any("0.55 xGI" in card["value"] for card in candidate.post["evidence"])
