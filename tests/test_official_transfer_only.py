"""Required tests for official-confirmed and tier-one-reported transfers.

Covers the strict split authority policy:
  1. Official club announcement with all required fields -> CONFIRMED.
  2. A lone non-official completion claim remains blocked.
  3. Two approved independent sources may publish AGREEMENT/MEDICAL as
     REPORTED, never CONFIRMED, using the same 4K card.
  4. FPL data changed but no source evidence -> blocked.
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
    # Source citation is on the card image, never the caption text.
    assert "http" not in caption
    assert "Source:" not in caption
    assert len(caption.splitlines()) <= 4

    card_path = tmp_path / "card.png"
    create_verified_card(decision, runtime.sources, card_path, fpl_data=FPL_DATA)
    assert card_path.exists()
    with Image.open(card_path) as im:
        assert im.size == (3840, 2160)


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

def test_3_deal_agreed_story_is_reported_not_confirmed(runtime):
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
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.may_publish
    assert decision.authority_kind == "tier_one_reported_transfer"
    assert "REPORTED" in decision.rendered_text
    assert "CONFIRMED" not in decision.rendered_text


def test_3_reported_transfer_uses_same_4k_card_and_source_only_on_card(runtime, tmp_path):
    bbc = observation(
        title="Deal agreed for Danny Welbeck to join Chelsea from Brighton",
        source_id="media.bbc_sport",
        url="https://www.bbc.co.uk/sport/football/welbeck-deal-agreed",
        story=transfer_story(),
    )
    sky = observation(
        title="Danny Welbeck to join Chelsea from Brighton after fee agreed",
        source_id="media.sky_sports",
        url="https://www.skysports.com/football/news/welbeck-fee-agreed",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([bbc, sky])
    assert decision.may_publish, decision.reasons
    assert "Source:" not in decision.rendered_text
    assert "http" not in decision.rendered_text

    card_path = tmp_path / "reported-card.png"
    with patch("src.verification.premium_cards._load_player_image", return_value=None):
        create_verified_card(decision, runtime.sources, card_path, fpl_data=FPL_DATA)
    with Image.open(card_path) as image:
        assert image.size == (3840, 2160)


def test_3_medical_completed_story_is_reported_not_confirmed(runtime):
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
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.may_publish
    assert decision.authority_kind == "tier_one_reported_transfer"
    assert "REPORTED" in decision.rendered_text
    assert "CONFIRMED" not in decision.rendered_text


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
        # Every category, including TRANSFER, renders at the single 4K 16:9
        # canvas used across the whole product (see src/verification/card.py).
        assert im.size == (3840, 2160)


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


# ── 11. A club mentioned near an unrelated player's paragraph must never
#        override the trusted, previously-verified destination for THIS
#        player. Regression test for a real incident: a Sunderland signing
#        (correctly tracked with to_key="Sunderland") was posted as joining
#        "Crystal Palace" instead, because the grammar-direction parser
#        scanned the whole article for any club near transfer-verb language
#        with no check that the sentence was actually about that player.
#        Unit-tested directly against the parser (rather than through the
#        full pipeline) so the test isolates the actual mechanism that
#        broke, without depending on unrelated event-classification gates.

def test_11_grammar_parser_ignores_club_far_from_subject_player(runtime):
    from src.verification.entities import EntityType
    from src.verification.extractor import LegacyClaimAdapter

    adapter = LegacyClaimAdapter(runtime.config, runtime.sources, runtime.entities)

    # Realistic shape of the real incident: one paragraph genuinely about
    # Welbeck joining Chelsea, followed by an unrelated later paragraph
    # about Arsenal, separated by real distance (not just a period).
    text = (
        "Chelsea sign Danny Welbeck from Brighton in a deal announced this "
        "morning, ending speculation over his next club after a strong "
        "second half of last season. The move has been broadly welcomed "
        "by supporters, who have been calling for fresh legs in that "
        "position since the turn of the year, and the club's official "
        "statement confirmed terms had been agreed by both parties "
        "following a medical completed earlier in the week. "
        "In entirely separate news reported later the same day, Arsenal "
        "have completed the signing of a youth-team defender from a "
        "regional academy fixture, unconnected to today's other transfer "
        "activity across the rest of the Premier League."
    )
    club_mentions = runtime.entities.find_mentions(text, {EntityType.CLUB})
    player_mentions = runtime.entities.find_mentions(text, {EntityType.PLAYER})
    welbeck_positions = [pos for pos, ent, _ in player_mentions if "welbeck" in ent.name.lower()]
    assert welbeck_positions, "fixture is missing Danny Welbeck in the entity registry"

    # Without subject scoping (old behaviour): both clubs are visible to the
    # parser and either could be picked up as a destination.
    _, destination_unscoped = adapter._dynamic_transfer_direction(text, club_mentions)

    # With subject scoping (the fix): only club mentions near Welbeck's own
    # name are considered, so Arsenal -- mentioned only near a different,
    # unrelated sentence -- must never be returned as his destination.
    _, destination_scoped = adapter._dynamic_transfer_direction(
        text, club_mentions, subject_mentions=welbeck_positions
    )
    assert destination_scoped is not None
    assert destination_scoped.name == "Chelsea"
    assert destination_scoped.name != "Arsenal"


def test_11_genuine_conflicting_grammar_blocks_rather_than_silently_overrides(runtime, tmp_path):
    # Same player, but this time the ONLY club mentioned near transfer verbs
    # in the text genuinely conflicts with the trusted to_key (simulates a
    # wrong/stale source, or two different transfers sharing a name). This
    # must NOT silently publish with the newer, less-trusted destination --
    # it should be treated as an unresolved conflict and blocked.
    story = transfer_story(player="Danny Welbeck", destination="Chelsea")
    conflicting_title = "Danny Welbeck has signed for Arsenal in a shock move."
    obs = observation(
        title=conflicting_title,
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/danny-welbeck-arsenal",
        story=story,
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision != DecisionType.PUBLISH, (
        "a genuine from/to conflict between the trusted story and a fresh "
        "grammatical read was silently resolved instead of blocking"
    )


# ── 12. Subject resolution failing outright must not fall back to an
# unguarded whole-article club scan (the real root cause behind a second,
# later wrong-club incident: a Spurs player shown joining Crystal Palace/
# Everton, after the first incident's proximity-guard fix was already
# live -- because that guard only fires when subject_positions is
# non-None, and subject resolution failing outright left it None) ────────

def test_12_unresolvable_subject_never_reaches_the_unguarded_scan(runtime):
    """Direct test of _guarded_transfer_direction() -- the actual method
    the fix lives in -- rather than through the full verify_observations()
    pipeline, which blocks an unresolved subject for its own unrelated
    reasons (missing mandatory facts) before ever reaching this code.
    subject_positions=None (the real signature for both "name didn't
    resolve at all" and "resolved but no nearby mention in this
    document") must return (None, None), never fall through to
    _dynamic_transfer_direction()'s own unguarded scan.
    """
    from src.verification.extractor import LegacyClaimAdapter

    adapter = LegacyClaimAdapter(runtime.config, runtime.sources, runtime.entities)
    text = (
        "Chelsea have completed a transfer this afternoon, the club "
        "confirmed in an official statement. In separate news, Arsenal "
        "have been strongly linked with several targets ahead of the "
        "coming window, though nothing has been confirmed on that front."
    )
    from src.verification.entities import EntityType
    club_mentions = adapter.entities.find_mentions(text, {EntityType.CLUB})

    origin, destination = adapter._guarded_transfer_direction(
        text, club_mentions, subject_positions=None
    )
    assert origin is None and destination is None, (
        f"expected a fail-closed (None, None) when subject_positions is "
        f"None, got origin={origin!r} destination={destination!r} -- this "
        f"is the exact unguarded fallback that caused two real wrong-club "
        f"incidents")


def test_12_guarded_direction_still_works_normally_when_subject_positions_given(runtime):
    """The wrapper must not break the working, already-tested case
    (test_11): when subject_positions IS available, it should behave
    identically to calling _dynamic_transfer_direction() directly.
    """
    from src.verification.extractor import LegacyClaimAdapter
    from src.verification.entities import EntityType

    adapter = LegacyClaimAdapter(runtime.config, runtime.sources, runtime.entities)
    # Same text as test_11 (proven to place Arsenal outside the 400-char
    # proximity window) -- reused rather than a shortened variant, since a
    # shorter separation risks accidentally falling back inside the
    # window and testing nothing real.
    text = (
        "Chelsea sign Danny Welbeck from Brighton in a deal announced this "
        "morning, ending speculation over his next club after a strong "
        "second half of last season. The move has been broadly welcomed "
        "by supporters, who have been calling for fresh legs in that "
        "position since the turn of the year, and the club's official "
        "statement confirmed terms had been agreed by both parties "
        "following a medical completed earlier in the week. "
        "In entirely separate news reported later the same day, Arsenal "
        "have completed the signing of a youth-team defender from a "
        "regional academy fixture, unconnected to today's other transfer "
        "activity across the rest of the Premier League."
    )
    club_mentions = adapter.entities.find_mentions(text, {EntityType.CLUB})
    player_mentions = adapter.entities.find_mentions(text, {EntityType.PLAYER})
    welbeck_positions = [pos for pos, ent, _ in player_mentions if "welbeck" in ent.name.lower()]
    assert welbeck_positions, "fixture is missing Danny Welbeck in the entity registry"

    origin, destination = adapter._guarded_transfer_direction(
        text, club_mentions, subject_positions=welbeck_positions
    )
    assert destination is not None
    assert destination.name == "Chelsea"
    assert destination.name != "Arsenal"


def test_12_full_pipeline_still_blocks_an_unresolvable_player_for_its_own_reason(runtime):
    """Defense-in-depth check at the full-pipeline level: an unresolvable
    player name must never publish, regardless of which specific gate
    catches it. This does not prove the attribution fix on its own (see
    the two tests above for that) -- it confirms the outer pipeline still
    fails closed end-to-end.
    """
    story = transfer_story(player="Zzqxarian Nonexistentplayer", destination="Chelsea")
    obs = observation(
        title="Chelsea complete a transfer, Arsenal also active in the market",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/example-transfer",
        story=story,
        structured=True,
    )
    obs["document"]["summary"] = (
        "Chelsea have completed a transfer this afternoon, the club "
        "confirmed in an official statement. In separate news, Arsenal "
        "have been strongly linked with several targets ahead of the "
        "coming window, though nothing has been confirmed on that front."
    )
    decision = runtime.verify_observations([obs])
    assert not decision.may_publish
    assert decision.rendered_text is None
