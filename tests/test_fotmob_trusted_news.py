from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.verification import DecisionType, VerificationRuntime


@pytest.fixture
def fpl_data():
    return {
        "teams": [
            {"id": 1, "name": "Chelsea", "short_name": "CHE"},
            {"id": 2, "name": "Brighton", "short_name": "BHA"},
        ],
        "elements": [
            {
                "id": 10,
                "first_name": "Danny",
                "second_name": "Welbeck",
                "web_name": "Welbeck",
                "team": 2,
            },
        ],
    }


@pytest.fixture
def runtime(tmp_path, fpl_data):
    rt = VerificationRuntime(
        fpl_data=fpl_data,
        database_path=tmp_path / "verification.sqlite3",
    )
    yield rt
    rt.close()


def _obs(title, story, *, hours_old=0):
    published = (
        datetime.now(timezone.utc)-timedelta(hours=hours_old)
    ).isoformat()
    return {
        "document": {
            "title": title,
            "summary": title,
            "source_url": "https://www.fotmob.com/news/example-premier-league",
            "publisher_url": "https://www.fotmob.com/",
            "publisher_name": "FotMob",
            "source_id": "media.fotmob",
            "source_hint": "media.fotmob",
            "source_handle": "fotmob",
            "transport": "DIRECT_RSS",
            "configured_direct_feed": True,
            "declared_sport": "football",
            "created_at": published,
            "published_at": published,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {},
        },
        "legacy_story": story,
    }


def test_trusted_fotmob_injury_can_publish_alone(runtime):
    decision = runtime.verify_observations([_obs(
        "Danny Welbeck ruled out with a hamstring injury and will miss the next match.",
        {
            "player": "Danny Welbeck",
            "event": "injury",
            "from_club": "Brighton",
            "from_key": "Brighton",
            "diagnosis": "ruled out with a hamstring injury",
            "stage": 3,
        },
    )])

    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.authority_kind == "trusted_fotmob_news"
    assert "INJURY UPDATE" in decision.rendered_text
    assert "STATUS — OUT" in decision.rendered_text


def test_trusted_fotmob_suspension_can_publish_alone(runtime):
    decision = runtime.verify_observations([_obs(
        "Danny Welbeck has been suspended and will serve a one-match ban.",
        {
            "player": "Danny Welbeck",
            "event": "suspension",
            "from_club": "Brighton",
            "from_key": "Brighton",
            "diagnosis": "has been suspended",
            "suspension_length": "one-match ban",
            "stage": 3,
        },
    )])

    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.authority_kind == "trusted_fotmob_news"
    assert "SUSPENSION UPDATE" in decision.rendered_text


def test_fotmob_transfer_rumour_does_not_use_fast_lane(runtime):
    decision = runtime.verify_observations([_obs(
        "Chelsea interested in signing Danny Welbeck from Brighton.",
        {
            "player": "Danny Welbeck",
            "event": "transfer",
            "from_club": "Brighton",
            "from_key": "Brighton",
            "to_club": "Chelsea",
            "to_key": "Chelsea",
            "stage": 1,
        },
    )])

    assert decision.decision != DecisionType.PUBLISH
    assert decision.authority_kind == "none"


def test_trusted_fotmob_news_older_than_24_hours_is_held(runtime):
    decision = runtime.verify_observations([_obs(
        "Danny Welbeck ruled out with a hamstring injury and will miss the next match.",
        {
            "player": "Danny Welbeck",
            "event": "injury",
            "from_club": "Brighton",
            "from_key": "Brighton",
            "diagnosis": "ruled out with a hamstring injury",
            "stage": 3,
        },
        hours_old=25,
    )])

    assert decision.decision != DecisionType.PUBLISH
    assert decision.authority_kind == "trusted_fotmob_news"
    assert any("FotMob report is" in reason for reason in decision.reasons)
