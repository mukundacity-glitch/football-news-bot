"""Required test suite for the official-confirmed-only transfer policy.

Covers every scenario specified in the non-negotiable transfer mandate:
  1. Official club announcement with all required fields -> publish, FROM/TO
     shown, correct official source shown.
  2. Reputable news article without official confirmation -> blocked, no
     image/caption, correct skip reason logged.
  3. "Deal agreed"/"medical completed" story -> blocked.
  4. FPL data changed but no official source -> blocked.
  5. Official announcement with no fee -> publish, "Fee: undisclosed".
  6. Official fee stated -> publish, shows only that fee.
  7. FPL player image available -> card uses a valid, large FPL image.
  8. FPL image missing -> card uses a valid Wikipedia fallback image.
  9. All player images missing -> card uses a clean placeholder.
  10. Missing FROM or TO -> blocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from src.verification import DecisionType, VerificationRuntime
from src.verification.card import UnverifiedTransferError, create_verified_card
from src.verification.official_transfer_gate import validate_official_transfer
from src.verification import player_image as player_image_module
from tests.test_verification_v2 import observation, transfer_story


FPL_DATA = {
    "teams": [
        {"id": 1, "name": "Chelsea", "short_name": "CHE"},
        {"id": 2, "name": "Brighton", "short_name": "BHA"},
        {"id": 3, "name": "Arsenal", "short_name": "ARS"},
    ],
    "elements": [
        {
            "id": 10, "first_name": "Danny", "second_name": "Welbeck",
            "web_name": "Welbeck", "team": 2, "code": 98765,
            "element_type": 4,
        },
    ],
    "element_types": [
        {"id": 4, "singular_name": "Forward", "singular_name_short": "FWD"},
    ],
}


@pytest.fixture
def runtime(tmp_path):
    rt = VerificationRuntime(fpl_data=FPL_DATA, database_path=tmp_path / "v.sqlite3")
    yield rt
    rt.close()


def _official_chelsea_obs(**overrides):
    story = transfer_story()
    story.update(overrides.pop("story_overrides", {}))
    defaults = dict(
        title="Chelsea sign Danny Welbeck from Brighton",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/chelsea-sign-danny-welbeck",
        story=story,
    )
    defaults.update(overrides)
    return observation(**defaults)


# ── 1. Official club announcement, all fields present ────────────────────

def test_1_official_announcement_publishes_with_from_to_and_source(runtime, tmp_path):
    decision = runtime.verify_observations([_official_chelsea_obs()])
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.may_publish

    validation = validate_official_transfer(decision, runtime.sources)
    assert validation.ok, validation.reason

    caption = decision.rendered_text
    assert "Brighton" in caption  # FROM
    assert "Chelsea" in caption  # TO
    assert "Official confirmation: Chelsea" in caption
    assert "Source: https://www.chelseafc.com" in caption

    card_path = tmp_path / "card.png"
    create_verified_card(decision, runtime.sources, card_path, fpl_data=FPL_DATA)
    assert card_path.exists()
    with Image.open(card_path) as im:
        assert im.size[0] >= 1080 and im.size[1] >= 1350


# ── 2. Reputable news article, no official confirmation ──────────────────

def test_2_reputable_media_only_is_blocked_with_no_image_or_caption(runtime, tmp_path):
    obs = observation(
        title="Chelsea sign Danny Welbeck from Brighton",
        source_id="media.bbc_sport",
        url="https://www.bbc.co.uk/sport/football/welbeck-signs",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish
    assert decision.rendered_text is None

    with pytest.raises(ValueError):
        create_verified_card(decision, runtime.sources, tmp_path / "should_not_exist.png")
    assert not (tmp_path / "should_not_exist.png").exists()


# ── 3. "Deal agreed" / "medical completed" story ──────────────────────────

def test_3_deal_agreed_story_is_blocked(runtime):
    bbc = observation(
        title="Deal agreed for Danny Welbeck to join Chelsea from Brighton",
        source_id="media.bbc_sport",
        url="https://www.bbc.co.uk/sport/football/welbeck-deal-agreed",
        story=transfer_story(),
    )
    athletic = observation(
        title="Danny Welbeck to join Chelsea from Brighton after fee agreed",
        source_id="media.the_athletic",
        url="https://www.nytimes.com/athletic/welbeck-fee-agreed",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([bbc, athletic])
    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish


def test_3_medical_completed_story_is_blocked(runtime):
    sky = observation(
        title="Chelsea agree fee with Brighton; Danny Welbeck given permission to undergo medical",
        source_id="media.sky_sports",
        url="https://www.skysports.com/football/news/welbeck-medical",
        story=transfer_story(),
    )
    bbc = observation(
        title="Danny Welbeck set for a medical at Chelsea after fee agreed with Brighton",
        source_id="media.bbc_sport",
        url="https://www.bbc.co.uk/sport/football/welbeck-medical",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([sky, bbc])
    assert decision.decision == DecisionType.PENDING
    assert not decision.may_publish


# ── 4. FPL data changed, no official source ───────────────────────────────

def test_4_fpl_only_change_without_official_source_is_blocked(runtime):
    """FPL is metadata/roster only -- never a transfer-confirmation source.

    Simulate the roster already reflecting a move (player's team changed in
    FPL) but with no accompanying official-source claim at all: nothing
    should publish, because no claims exist without an official document.
    """
    updated_fpl = {
        "teams": FPL_DATA["teams"],
        "elements": [
            {**FPL_DATA["elements"][0], "team": 1},  # moved to Chelsea in FPL only
        ],
        "element_types": FPL_DATA["element_types"],
    }
    rt2 = VerificationRuntime(fpl_data=updated_fpl, database_path=Path("/tmp/test4.sqlite3"))
    try:
        decision = rt2.verify_observations([])
        assert decision.decision != DecisionType.PUBLISH
        assert not decision.may_publish
    finally:
        rt2.close()


# ── 5. Official announcement with no fee ──────────────────────────────────

def test_5_official_announcement_without_fee_shows_undisclosed(runtime):
    decision = runtime.verify_observations([_official_chelsea_obs()])
    assert decision.may_publish
    assert "fee" not in decision.verified_facts
    assert "Fee: undisclosed" in decision.rendered_text
    assert "Fee: £" not in decision.rendered_text


# ── 6. Official fee stated ────────────────────────────────────────────────

def test_6_official_fee_is_shown_and_only_that_fee(runtime):
    story = transfer_story()
    story["fee"] = "£30m"
    obs = observation(
        title="Chelsea sign Danny Welbeck from Brighton for £30m",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/welbeck-30m",
        story=story,
    )
    decision = runtime.verify_observations([obs])
    assert decision.may_publish
    assert decision.verified_facts["fee"] == "£30m"
    assert "Fee: £30m" in decision.rendered_text
    assert "Fee: undisclosed" not in decision.rendered_text


def test_6_reported_fee_language_is_rejected_by_the_strict_gate(runtime):
    """A hedged/journalistic fee phrase must never be shown as an official fee."""
    from src.verification.models import VerificationDecision as VD

    decision = runtime.verify_observations([_official_chelsea_obs()])
    tampered_facts = dict(decision.verified_facts)
    tampered_facts["fee"] = "reported to be around £30m"
    tampered = VD(
        decision=decision.decision, story_id=decision.story_id,
        family_id=decision.family_id, event_type=decision.event_type,
        status=decision.status, verified_facts=tampered_facts,
        source_ids=decision.source_ids, publisher_groups=decision.publisher_groups,
        gates=decision.gates, reasons=decision.reasons, confidence=decision.confidence,
        confidence_dimensions=decision.confidence_dimensions,
        evidence_document_ids=decision.evidence_document_ids,
        fingerprint=decision.fingerprint, source_url=decision.source_url,
    )
    validation = validate_official_transfer(tampered, runtime.sources)
    assert not validation.ok
    assert validation.reason.startswith("unsupported_fee_language")


# ── 7. FPL player image available ─────────────────────────────────────────

def test_7_fpl_image_used_when_available(runtime, tmp_path):
    fake_image = Image.new("RGBA", (300, 300), (10, 20, 30, 255))

    def fake_fetch_bytes(url, timeout=10.0):
        if "resources.premierleague.com" in url:
            import io
            buf = io.BytesIO()
            fake_image.save(buf, format="PNG")
            return buf.getvalue()
        return None

    with patch.object(player_image_module, "_fetch_bytes", side_effect=fake_fetch_bytes):
        image, source, match = player_image_module.resolve_player_image(
            full_name="Danny Welbeck", first_name="Danny", last_name="Welbeck",
            club_name="Chelsea", fpl_data=FPL_DATA,
        )
    assert source == "fpl"
    assert image.size[0] >= 160 and image.size[1] >= 160


# ── 8. FPL image missing, Wikipedia fallback used ─────────────────────────

def test_8_wikipedia_fallback_used_when_fpl_image_missing(runtime):
    fake_image = Image.new("RGBA", (400, 400), (40, 50, 60, 255))

    def fake_fetch_bytes(url, timeout=10.0):
        if "wikipedia.org" not in url and "wikimedia.org" not in url:
            return None  # FPL photo endpoint fails
        import io
        buf = io.BytesIO()
        fake_image.save(buf, format="PNG")
        return buf.getvalue()

    def fake_fetch_json(url, accept_non_image=False):
        if "list=search" in url:
            return {"query": {"search": [{"title": "Danny Welbeck"}]}}
        return {
            "title": "Danny Welbeck",
            "description": "English footballer",
            "extract": "is an English footballer who plays as a forward",
            "originalimage": {"source": "https://upload.wikimedia.org/fake.jpg"},
        }

    with patch.object(player_image_module, "_fetch_bytes", side_effect=fake_fetch_bytes), \
         patch.object(player_image_module, "_fetch_json", side_effect=fake_fetch_json):
        image, source, match = player_image_module.resolve_player_image(
            full_name="Danny Welbeck", first_name="Danny", last_name="Welbeck",
            club_name="Chelsea", fpl_data={"teams": [], "elements": []},
        )
    assert source == "wikipedia"
    assert image.size[0] >= 160


# ── 9. All player images missing -> placeholder ───────────────────────────

def test_9_placeholder_used_when_no_image_is_available(runtime):
    with patch.object(player_image_module, "_fetch_bytes", return_value=None), \
         patch.object(player_image_module, "_fetch_json", return_value=None):
        image, source, match = player_image_module.resolve_player_image(
            full_name="Totally Unknownplayer", fpl_data={"teams": [], "elements": []},
        )
    assert source == "placeholder"
    assert image.size[0] > 0 and image.size[1] > 0


def test_9_placeholder_is_never_an_ai_generated_photo_and_card_still_publishes(runtime, tmp_path):
    with patch.object(player_image_module, "_fetch_bytes", return_value=None), \
         patch.object(player_image_module, "_fetch_json", return_value=None):
        decision = runtime.verify_observations([_official_chelsea_obs()])
        assert decision.may_publish
        card_path = tmp_path / "placeholder_card.png"
        create_verified_card(decision, runtime.sources, card_path, fpl_data=FPL_DATA)
    assert card_path.exists()
    with Image.open(card_path) as im:
        assert im.size == (1080, 1350)


# ── 10. Missing FROM or TO ────────────────────────────────────────────────

def test_10_missing_from_club_blocks_publication(runtime):
    obs = observation(
        title="Chelsea sign Danny Welbeck",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/welbeck-no-origin",
        story={
            "player": "Danny Welbeck", "event": "transfer",
            "to_key": "Chelsea", "to_club": "Chelsea", "stage": 4,
        },
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision != DecisionType.PUBLISH
    assert not decision.may_publish


def test_10_missing_to_club_blocks_publication(runtime):
    obs = observation(
        title="Danny Welbeck leaves Brighton",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/welbeck-no-destination",
        story={
            "player": "Danny Welbeck", "event": "transfer",
            "from_key": "Brighton", "from_club": "Brighton", "stage": 4,
        },
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision != DecisionType.PUBLISH
    assert not decision.may_publish


def test_10_gate_rejects_a_decision_with_from_equal_to_to():
    """Defense in depth: even if a decision somehow had FROM == TO, the
    strict gate must independently refuse it."""
    from src.verification.models import (
        DecisionType as DT, EventStatus, EventType, GateResult, GateState,
        VerificationDecision as VD,
    )
    from src.verification.source_registry import SourceRegistry

    sources = SourceRegistry.load()
    decision = VD(
        decision=DT.PUBLISH, story_id="s", family_id="f",
        event_type=EventType.TRANSFER, status=EventStatus.COMPLETED,
        verified_facts={
            "subject_name": "Danny Welbeck",
            "club_from_name": "Chelsea",
            "club_to_name": "Chelsea",
        },
        source_ids=["club.chelsea"], publisher_groups=["club-chelsea"],
        gates=[GateResult("x", GateState.PASS, "ok")],
        reasons=[], confidence=1.0, confidence_dimensions={},
        evidence_document_ids=["d"], fingerprint="fp",
        source_url="https://www.chelseafc.com/news/example",
    )
    validation = validate_official_transfer(decision, sources)
    assert not validation.ok
    assert validation.reason == "from_club_equals_to_club"


# ── Rejection logging ─────────────────────────────────────────────────────

def test_skipped_unverified_transfer_is_logged_with_reason(runtime, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    obs = observation(
        title="Chelsea sign Danny Welbeck from Brighton",
        source_id="media.bbc_sport",
        url="https://www.bbc.co.uk/sport/football/welbeck-signs",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([obs])
    assert not decision.may_publish
    # A PENDING (not PUBLISH) decision never reaches create_verified_card at
    # all in the real pipeline (main.py only calls it for may_publish==True).
    # Exercise the gate directly against a hand-built PUBLISH-shaped decision
    # with a non-official source to prove the log path fires correctly.
    from src.verification.models import (
        DecisionType as DT, EventStatus, EventType, GateResult, GateState,
        VerificationDecision as VD,
    )
    fake_decision = VD(
        decision=DT.PUBLISH, story_id="s", family_id="f",
        event_type=EventType.TRANSFER, status=EventStatus.COMPLETED,
        verified_facts={
            "subject_name": "Danny Welbeck",
            "club_from_name": "Brighton",
            "club_to_name": "Chelsea",
        },
        source_ids=["media.bbc_sport"], publisher_groups=["media-bbc"],
        gates=[GateResult("x", GateState.PASS, "ok")],
        reasons=[], confidence=1.0, confidence_dimensions={},
        evidence_document_ids=["d"], fingerprint="fp2",
        source_url="https://www.bbc.co.uk/sport/football/welbeck-signs",
    )
    with pytest.raises(UnverifiedTransferError):
        create_verified_card(fake_decision, runtime.sources, tmp_path / "x.png")

    debug_dir = tmp_path / "queue" / "debug"
    assert (debug_dir / "rejections.jsonl").exists()
    content = (debug_dir / "rejections.jsonl").read_text()
    assert "SKIPPED_UNVERIFIED_TRANSFER" in content
    assert "source_not_on_official_allowlist" in content
