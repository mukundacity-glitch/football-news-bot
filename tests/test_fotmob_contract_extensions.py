from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.verification.ingestion import _fotmob_legacy_story, _fotmob_transfer_text
from src.verification.models import DecisionType, EventType
from src.verification.runtime import VerificationRuntime


def _now_iso(hours_ago: float = 1.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _row(*, fee_text: str = "Contract extension", to_date: str | None = "2032-06-30T00:00:00Z"):
    return {
        "playerId": 10,
        "name": "Danny Welbeck",
        "fromClubId": 10204,
        "fromClub": "Brighton",
        "fromClubFullName": "Brighton",
        "toClubId": 10204,
        "toClub": "Brighton",
        "toClubFullName": "Brighton",
        "transferDate": _now_iso(),
        "fromDate": _now_iso(),
        "toDate": to_date,
        "fee": {"feeText": fee_text, "value": 0},
        "marketValue": 48000000,
        "position": {"label": "ST"},
        "onLoan": False,
    }


def _obs(row):
    story = _fotmob_legacy_story(row)
    text = _fotmob_transfer_text(row)
    return {
        "document": {
            "id": "fotmob-contract-test",
            "document_id": "fotmob-contract-test",
            "title": text,
            "summary": "",
            "text": text,
            "source_url": "https://www.fotmob.com/leagues/47/transfers/premier-league?season=2026%2F2027",
            "publisher_url": "https://www.fotmob.com/",
            "publisher_name": "FotMob",
            "source_id": "media.fotmob",
            "source_hint": "media.fotmob",
            "source_handle": "fotmob",
            "transport": "FOTMOB",
            "configured_direct_feed": False,
            "declared_sport": "football",
            "created_at": row["transferDate"],
            "published_at": row["transferDate"],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "feed_id": "fotmob.premier_league.transfers",
            "metadata": {
                "structured_fotmob_transfer": bool(story.get("_structured_fotmob_transfer")),
                "structured_fotmob_contract_extension": bool(
                    story.get("_structured_fotmob_contract_extension")
                ),
                "fotmob_row": row,
            },
        },
        "legacy_story": story,
    }


@pytest.fixture
def runtime(tmp_path):
    fpl = {
        "teams": [{"id": 1, "name": "Brighton", "short_name": "BHA"}],
        "elements": [{
            "id": 10,
            "first_name": "Danny",
            "second_name": "Welbeck",
            "web_name": "Welbeck",
            "team": 1,
        }],
    }
    rt = VerificationRuntime(
        fpl_data=fpl, database_path=tmp_path / "verification.sqlite3"
    )
    yield rt
    rt.close()


def test_explicit_fotmob_contract_extension_routes_to_contract_not_transfer(runtime):
    row = _row()
    story = _fotmob_legacy_story(row)
    assert story["event"] == "renewal"
    assert story["_structured_fotmob_contract_extension"] is True
    assert story["_structured_fotmob_transfer"] is False
    assert story["from_club"] is None
    assert story["to_club"] == "Brighton"
    assert story["contract"] == "Jun 2032"
    assert "extends the contract with Brighton" in story["raw_text"]

    decision = runtime.verify_observations([_obs(row)])
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.may_publish
    assert decision.event_type == EventType.CONTRACT
    assert decision.authority_kind == "structured_fotmob_contract_extension"
    assert decision.authority_source_ids == ["media.fotmob"]
    assert decision.verified_facts["club_name"] == "Brighton"
    assert decision.verified_facts["contract_status"] == "extended"
    assert decision.verified_facts["contract_length"] == "Jun 2032"
    assert decision.verified_facts["provider_player_id"] == "10"
    assert decision.verified_facts["provider_to_club_id"] == "10204"
    assert "CONTRACT UPDATE" in decision.rendered_text


def test_same_club_row_is_not_guessed_as_extension_without_explicit_label(runtime):
    row = _row(fee_text="Permanent transfer")
    story = _fotmob_legacy_story(row)
    assert story["event"] == "transfer"
    assert story["_structured_fotmob_contract_extension"] is False
    assert story["_structured_fotmob_transfer"] is True

    decision = runtime.verify_observations([_obs(row)])
    assert decision.decision != DecisionType.PUBLISH
    assert not decision.may_publish
    assert decision.event_type == EventType.TRANSFER


def test_structured_fotmob_extension_without_contract_end_date_fails_closed(runtime):
    row = _row(to_date=None)
    decision = runtime.verify_observations([_obs(row)])
    assert decision.decision != DecisionType.PUBLISH
    assert not decision.may_publish
    mandatory = decision.gate("mandatory_facts")
    assert mandatory.state.value != "PASS"
    assert "contract_length" in mandatory.reason


def test_unstructured_fotmob_contract_story_cannot_use_structured_authority(runtime):
    row = _row()
    obs = _obs(row)
    obs["document"]["metadata"] = {}
    obs["legacy_story"]["_structured_fotmob_contract_extension"] = False
    obs["legacy_story"]["_structured_fotmob_transfer"] = False
    decision = runtime.verify_observations([obs])
    assert decision.decision != DecisionType.PUBLISH
    assert not decision.may_publish
    assert decision.authority_kind != "structured_fotmob_contract_extension"


def test_stale_structured_fotmob_contract_extension_does_not_backfill(runtime):
    row = _row()
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    row["transferDate"] = old
    row["fromDate"] = old
    decision = runtime.verify_observations([_obs(row)])
    assert decision.decision != DecisionType.PUBLISH
    assert not decision.may_publish
    temporal = decision.gate("temporal_consistency")
    assert temporal.state.value != "PASS"
