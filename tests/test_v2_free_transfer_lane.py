from __future__ import annotations

from datetime import datetime, timezone

import main
from src.verification import VerificationRuntime
from src.verification.models import DecisionType


def _fpl():
    return {
        "teams": [
            {"id": 1, "name": "Chelsea", "short_name": "CHE"},
            {"id": 2, "name": "Brighton", "short_name": "BHA"},
        ],
        "elements": [{
            "id": 10, "first_name": "Example", "second_name": "Player",
            "web_name": "Player", "team": 1,
        }],
    }


def _obs(text, *, source_id="club.chelsea", free_hint=False):
    now = datetime.now(timezone.utc).isoformat()
    story = {
        "player": "Example Player", "display_name": "Example Player",
        "event": "transfer",
        "from_club": "Chelsea", "from_key": "Chelsea",
        "to_club": "Chelsea", "to_key": "Chelsea",
    }
    if free_hint:
        story["transfer_kind"] = "free"
    domain = "chelseafc.com" if source_id == "club.chelsea" else "bbc.com"
    return {
        "document": {
            "id": "free-transfer-" + str(abs(hash((text, source_id)))),
            "document_id": "free-transfer-" + str(abs(hash((text, source_id)))),
            "title": text, "summary": text, "text": text,
            "source_url": f"https://www.{domain}/football/{abs(hash(text))}",
            "source_id": source_id, "source_hint": source_id,
            "username": "chelseafc" if source_id == "club.chelsea" else "BBC_Sport",
            "transport": "DIRECT_RSS", "configured_direct_feed": True,
            "declared_sport": "football", "created_at": now,
            "published_at": now, "feed_id": "test.free.transfer",
        },
        "legacy_story": story,
    }


def test_official_grounded_free_transfer_can_publish_without_fake_origin(tmp_path):
    runtime = VerificationRuntime(fpl_data=_fpl(), database_path=tmp_path / "free.sqlite3")
    try:
        decision = runtime.verify_observations([_obs(
            "Chelsea have signed Example Player on a free transfer."
        )])
        assert decision.decision == DecisionType.PUBLISH, decision.reasons
        assert decision.may_publish
        assert decision.verified_facts["club_to_name"] == "Chelsea"
        assert decision.verified_facts["transfer_kind"] == "free"
        assert "club_from_id" not in decision.verified_facts
        assert "club_from_name" not in decision.verified_facts
        assert "Free Agent → Chelsea" in decision.rendered_text
        assert "Deal — Free Transfer" in decision.rendered_text
        safety = [g for g in decision.gates if g.name == "transfer_publication_safety"]
        assert safety and safety[-1].state.value == "PASS"
    finally:
        runtime.close()


def test_non_free_same_origin_destination_still_fails_closed(tmp_path):
    runtime = VerificationRuntime(fpl_data=_fpl(), database_path=tmp_path / "same.sqlite3")
    try:
        decision = runtime.verify_observations([_obs(
            "Chelsea have signed Example Player from Chelsea."
        )])
        assert not decision.may_publish
        assert decision.decision == DecisionType.REJECT
        assert any(g.name == "entity_validation" and g.state.value == "FAIL" for g in decision.gates)
    finally:
        runtime.close()


def test_legacy_free_hint_without_source_cue_cannot_bypass_origin_gate(tmp_path):
    runtime = VerificationRuntime(fpl_data=_fpl(), database_path=tmp_path / "hint.sqlite3")
    try:
        decision = runtime.verify_observations([_obs(
            "Chelsea have signed Example Player.", free_hint=True
        )])
        assert not decision.may_publish
        assert decision.decision != DecisionType.PUBLISH
        assert decision.verified_facts.get("transfer_kind") != "free"
    finally:
        runtime.close()


def test_media_free_transfer_remains_unconfirmed_and_cannot_publish(tmp_path):
    runtime = VerificationRuntime(fpl_data=_fpl(), database_path=tmp_path / "media.sqlite3")
    try:
        decision = runtime.verify_observations([_obs(
            "Chelsea have signed Example Player on a free transfer.",
            source_id="media.bbc_sport",
        )])
        assert not decision.may_publish
        assert decision.decision != DecisionType.PUBLISH
        assert any(g.name == "official_confirmation" and g.state.value != "PASS" for g in decision.gates)
    finally:
        runtime.close()


def test_projection_clears_unverified_legacy_origin_for_verified_free_move(tmp_path):
    runtime = VerificationRuntime(fpl_data=_fpl(), database_path=tmp_path / "projection.sqlite3")
    try:
        decision = runtime.verify_observations([_obs(
            "Chelsea have signed Example Player on a free transfer."
        )])
        assert decision.may_publish
        item = {
            "player": "Example Player", "event": "transfer",
            "from_club": "Chelsea", "from_key": "Chelsea",
            "to_club": "Chelsea", "to_key": "Chelsea",
        }
        main._v2_project_verified_facts(item, decision)
        assert item["is_free"] is True
        assert item["from_club"] is None
        assert item["from_key"] is None
        assert item["to_key"].lower() == "chelsea"
    finally:
        runtime.close()
