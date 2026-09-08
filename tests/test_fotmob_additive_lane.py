from __future__ import annotations

from datetime import datetime, timezone

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


def test_fotmob_completed_transfer_can_publish_without_second_source(runtime):
    decision = runtime.verify_observations([_obs(
        title="Danny Welbeck has joined Chelsea from Brighton.",
        source_id="media.fotmob",
        url="https://www.fotmob.com/news/welbeck-joins-chelsea",
        story=_transfer_story(),
    )])

    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.authority_kind == "trusted_fotmob_news"
    assert decision.authority_source_ids == ["media.fotmob"]
    assert "REPORTED TRANSFER" in decision.rendered_text
    assert "OFFICIAL TRANSFER" not in decision.rendered_text


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
