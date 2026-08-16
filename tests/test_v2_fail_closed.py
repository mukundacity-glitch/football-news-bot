"""Failure-mode tests: missing/corrupt authority can never fall through to X."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

import main
from src.verification.models import (
    DecisionType, EventStatus, EventType, GateResult, GateState,
    VerificationDecision,
)
from src.verification import VerificationRuntime
from src.verification.repository import RepositoryError, VerificationRepository


class NeverCallClient:
    async def upload_media(self, *args, **kwargs):
        raise AssertionError("X client must not be called")

    async def create_tweet(self, *args, **kwargs):
        raise AssertionError("X client must not be called")


def test_corrupt_sqlite_does_not_start_with_empty_history(tmp_path):
    path = tmp_path / "verification.sqlite3"
    path.write_bytes(b"not a sqlite database")
    with pytest.raises(RepositoryError):
        VerificationRepository(path)


def test_corrupt_legacy_state_raises_instead_of_forgetting_dedup(monkeypatch, tmp_path):
    path = tmp_path / "posted_news.json"
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(main, "POSTED_FILE", path)
    with pytest.raises(main.StateCorruptionError):
        main.load_data()


def test_post_item_rejects_every_legacy_item_before_x(monkeypatch):
    monkeypatch.setattr(main, "_VERIFICATION_RUNTIME", object())
    result = asyncio.run(main.post_item(
        NeverCallClient(),
        {"player": "Candidate", "_v2_verified": False},
        {"posted_ids": [], "stories": {}, "daily": {"count": 0}},
    ))
    assert result is False


def test_tampered_v2_gate_is_rejected_before_x(monkeypatch):
    decision = VerificationDecision(
        decision=DecisionType.PUBLISH,
        story_id="story", family_id="family",
        event_type=EventType.TRANSFER,
        status=EventStatus.COMPLETED,
        verified_facts={"subject_name": "A B", "club_to_name": "Club"},
        source_ids=["club.example"], publisher_groups=["club-example"],
        gates=[GateResult("entity_validation", GateState.FAIL, "tampered")],
        reasons=[], confidence=1.0, confidence_dimensions={},
        evidence_document_ids=["doc"], fingerprint="fingerprint",
    )
    monkeypatch.setattr(main, "_VERIFICATION_RUNTIME", object())
    result = asyncio.run(main.post_item(
        NeverCallClient(),
        {"player": "A B", "_v2_verified": True, "_v2_decision": decision.to_dict()},
        {"posted_ids": [], "stories": {}, "daily": {"count": 0}},
    ))
    assert result is False


def test_tampered_verified_facts_fail_fingerprint_check(monkeypatch, tmp_path):
    fpl = {
        "teams": [
            {"id": 1, "name": "Chelsea", "short_name": "CHE"},
            {"id": 2, "name": "Brighton", "short_name": "BHA"},
        ],
        "elements": [{
            "id": 10, "first_name": "Danny", "second_name": "Welbeck",
            "web_name": "Welbeck", "team": 2,
        }],
    }
    runtime = VerificationRuntime(
        fpl_data=fpl, database_path=tmp_path / "verification.sqlite3"
    )
    now = datetime.now(timezone.utc).isoformat()
    decision = runtime.verify_observations([{
        "document": {
            "title": "Chelsea sign Danny Welbeck from Brighton",
            "summary": "Chelsea sign Danny Welbeck from Brighton",
            "source_url": "https://www.chelseafc.com/en/news/article/welbeck",
            "source_id": "club.chelsea", "source_hint": "club.chelsea",
            "transport": "DIRECT_RSS", "configured_direct_feed": True,
            "declared_sport": "football", "created_at": now,
        },
        "legacy_story": {
            "player": "Danny Welbeck", "event": "transfer",
            "from_club": "Brighton", "to_club": "Chelsea", "stage": 4,
        },
    }])
    assert decision.may_publish
    tampered = decision.to_dict()
    tampered["verified_facts"]["club_to_name"] = "Arsenal"
    monkeypatch.setattr(main, "_VERIFICATION_RUNTIME", runtime)
    result = asyncio.run(main.post_item(
        NeverCallClient(),
        {"player": "Danny Welbeck", "_v2_verified": True, "_v2_decision": tampered},
        {"posted_ids": [], "stories": {}, "daily": {"count": 0}},
    ))
    assert result is False

    authority_tampered = VerificationDecision.from_dict(decision.to_dict())
    authority_tampered.authority_source_ids = ["club.arsenal"]
    assert not runtime.decision_integrity_ok(authority_tampered)

    label_tampered = VerificationDecision.from_dict(decision.to_dict())
    label_tampered.authority_kind = "tier_one_reported_transfer"
    assert not runtime.decision_integrity_ok(label_tampered)
    runtime.close()
