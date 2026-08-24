"""Locks V2 update-aware deduplication across scheduled runs."""

from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "twikit" not in sys.modules:
    tw = types.ModuleType("twikit")
    tw.Client = object
    sys.modules["twikit"] = tw

import main  # noqa: E402


def _data() -> dict:
    return {
        "stories": {},
        "posted_ids": [],
        "posted_hashes": [],
        "posted_headlines": [],
        "posted_v2_fingerprints": [],
        "posted_v2_fact_signatures": [],
    }


def _verified_story(*, fingerprint: str, injury_status: str, headline: str) -> dict:
    return {
        "player": "Example Player",
        "event": "injury",
        "from_key": "Arsenal",
        "to_key": "Arsenal",
        "stage": 4,
        "headline": headline,
        "_v2_verified": True,
        "_v2_decision": {
            "fingerprint": fingerprint,
            "family_id": "family-example-player-injury",
            "event_type": "INJURY",
            "status": "OFFICIAL",
            "verified_facts": {
                "subject_id": "player.example",
                "subject_name": "Example Player",
                "club_id": "club.arsenal",
                "club_name": "Arsenal",
                "injury_status": injury_status,
            },
        },
    }


def test_same_verified_facts_from_another_source_are_still_duplicate():
    data = _data()
    first = _verified_story(
        fingerprint="authority-a",
        injury_status="Hamstring injury - ruled out",
        headline="Official update: Example Player is ruled out",
    )
    main.record_content_dedup(first, data)

    rediscovered = _verified_story(
        fingerprint="authority-b",
        injury_status="Hamstring injury - ruled out",
        headline="Club confirms Example Player remains unavailable",
    )
    duplicate, reason = main.is_duplicate_content(rediscovered, data)

    assert duplicate is True
    assert reason == "v2_verified_facts"


def test_material_same_player_update_is_not_blocked_by_legacy_stage_guard():
    data = _data()
    first = _verified_story(
        fingerprint="out-fingerprint",
        injury_status="Hamstring injury - ruled out",
        headline="Official update: Example Player is ruled out",
    )
    main.record_content_dedup(first, data)
    data["stories"]["v2_example"] = {
        "stage": 4,
        "player": "Example Player",
        "to_key": "Arsenal",
        "event": "injury",
        "status": "active",
    }

    returning = _verified_story(
        fingerprint="returning-fingerprint",
        injury_status="Returned to full training",
        headline="Official update: Example Player has returned to training",
    )
    duplicate, reason = main.is_duplicate_content(returning, data)

    assert duplicate is False, reason


def test_event_families_keep_suspension_and_press_news_out_of_transfer_dedup():
    assert main._event_family("loan") == "transfer"
    assert main._event_family("suspension") == "suspension"
    assert main._event_family("press_conference") == "press_conference"
    assert main._event_family("manager") == "manager"
    assert main.build_story_key("Example Player", "Arsenal", "suspension").endswith(
        "_suspension"
    )


def test_official_suspension_is_not_a_duplicate_of_a_prior_transfer():
    data = _data()
    data["stories"]["old_transfer"] = {
        "stage": 4,
        "player": "Example Player",
        "to_key": "Arsenal",
        "event": "transfer",
        "status": "active",
    }
    suspension = {
        "player": "Example Player",
        "event": "suspension",
        "from_key": "Arsenal",
        "stage": 4,
        "headline": "Example Player suspended for one match",
    }

    duplicate, reason = main.is_duplicate_content(suspension, data)
    assert duplicate is False, reason
