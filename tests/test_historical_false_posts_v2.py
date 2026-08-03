"""Backtest V2 against false posts preserved in the production audit corpus."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.verification import DecisionType, VerificationRuntime


_FALSE_POSTS = [
    "queue/posted/ben_duckett_leeds_transfer_s1.json",
    "queue/posted/france_world_cup_chelsea_crystal_palace_transfer_s1.json",
]


@pytest.fixture
def runtime(tmp_path):
    fpl = {
        "teams": [
            {"id": 1, "name": "Chelsea", "short_name": "CHE"},
            {"id": 2, "name": "Crystal Palace", "short_name": "CRY"},
            {"id": 3, "name": "Leeds", "short_name": "LEE"},
            {"id": 4, "name": "Brighton", "short_name": "BHA"},
        ],
        "elements": [
            {"id": 10, "first_name": "Real", "second_name": "Footballer", "web_name": "Footballer", "team": 4},
        ],
    }
    rt = VerificationRuntime(fpl_data=fpl, database_path=tmp_path / "v2.sqlite3")
    yield rt
    rt.close()


@pytest.mark.parametrize("path", _FALSE_POSTS)
def test_historical_false_post_cannot_publish_under_v2(runtime, path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    url = payload.get("source_url")
    if "ben_duckett" in path:
        source_id = "media.sky_sports"
        declared_sport = None
    else:
        source_id = "media.bbc_sport"
        url = url or "https://www.bbc.co.uk/sport/football/false-historical-item"
        declared_sport = "football"
    observation = {
        "document": {
            "title": payload.get("text") or payload.get("raw_text"),
            "summary": payload.get("body", ""),
            "source_url": url,
            "source_id": source_id,
            "source_hint": source_id,
            "transport": "DIRECT_RSS",
            "configured_direct_feed": True,
            "declared_sport": declared_sport,
            "created_at": payload.get("created_at"),
        },
        "legacy_story": payload,
    }
    decision = runtime.verify_observations([observation])
    assert decision.decision == DecisionType.REJECT
    assert not decision.may_publish
