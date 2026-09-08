from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.verification import DecisionType, VerificationRuntime
from src.verification.documents import FeedRegistry


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


def _obs(*, title, source_id, url, story, structured=False):
    now = datetime.now(timezone.utc).isoformat()
    return {
        "document": {
            "title": title,
            "summary": title,
            "source_url": url,
            "publisher_url": "https://www.fotmob.com/" if source_id == "media.fotmob" else url,
            "publisher_name": "FotMob" if source_id == "media.fotmob" else "Source",
            "source_id": source_id,
            "source_hint": source_id,
            "source_handle": "fotmob" if source_id == "media.fotmob" else "",
            "transport": "DIRECT_RSS",
            "configured_direct_feed": True,
            "declared_sport": "football",
            "created_at": now,
            "published_at": now,
            "metadata": {"structured_official": structured},
        },
        "legacy_story": story,
    }


def _transfer_story():
    return {
        "player": "Danny Welbeck",
        "event": "transfer",
        "from_club": "Brighton",
        "from_key": "Brighton",
        "to_club": "Chelsea",
        "to_key": "Chelsea",
        "stage": 4,
    }


def test_direct_fotmob_feed_replaces_the_duplicate_breaking_query():
    feeds = FeedRegistry.load().feeds
    direct = [feed for feed in feeds if feed.id == "fotmob.premier_league.topnews"]

    assert len(direct) == 1
    assert direct[0].url == "https://www.fotmob.com/topnews/feed?format=rss"
    assert direct[0].transport == "DIRECT_RSS"
    assert direct[0].source_hint == "media.fotmob"
    assert not any(
        feed.id == "google.fotmob.premier_league.breaking_topics"
        for feed in feeds
    )


def test_same_unstructured_completion_from_other_media_stays_pending(runtime):
    decision = runtime.verify_observations([_obs(
        title="Danny Welbeck has joined Chelsea from Brighton.",
        source_id="media.bbc_sport",
        url="https://www.bbc.co.uk/sport/football/welbeck-chelsea",
        story=_transfer_story(),
    )])

    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish
    assert decision.authority_kind == "none"


def test_existing_official_fpl_injury_lane_is_unchanged(runtime):
    text = "Danny Welbeck: Hamstring injury - Expected back 15 September"
    decision = runtime.verify_observations([_obs(
        title=text,
        source_id="official.fpl",
        url="https://fantasy.premierleague.com/api/bootstrap-static/",
        story={
            "player": "Danny Welbeck",
            "event": "injury",
            "from_club": "Brighton",
            "from_key": "Brighton",
            "diagnosis": "Hamstring injury - Expected back 15 September",
            "stage": 3,
        },
        structured=True,
    )])

    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.authority_kind == "first_party_official"
    assert "INJURY UPDATE" in decision.rendered_text
