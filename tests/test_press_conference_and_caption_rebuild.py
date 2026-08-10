"""Tests for this rebuild's three changes:

1. A new PRESS_CONFERENCE publishable category, official-confirmed-only --
   same non-negotiable bar as TRANSFER (only a first-party official source
   may make it authoritative; a media outlet quoting the same presser is not
   enough).
2. The 4-line, URL-free caption format used across every category (the
   account has no X Premium long-post allowance).
3. The generic "ambiguous trigger phrase" classification fix: an event
   pattern like "ruled out" (which fires on both a disallowed VAR goal and a
   genuine injury) now requires real corroborating evidence in the document
   before it is accepted, instead of publishing on the bare trigger phrase.
"""

from __future__ import annotations

import pytest
from PIL import Image

from src.verification import DecisionType, VerificationRuntime
from src.verification.card import (
    UnverifiedPressConferenceError,
    UnverifiedTransferError,
    create_verified_card,
)
from src.verification.extractor import SemanticClassifier
from src.verification.config import VerificationConfig
from src.verification.models import EventType
from src.verification.press_conference_gate import validate_official_press_conference
from tests.test_verification_v2 import now_iso, observation, transfer_story


FPL_DATA = {
    "teams": [
        {"id": 1, "name": "Chelsea", "short_name": "CHE"},
        {"id": 2, "name": "Brighton", "short_name": "BHA"},
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


def _press_conference_story(**overrides):
    story = {
        "player": "Enzo Maresca", "event": "press_conference",
        "to_key": "Chelsea", "to_club": "Chelsea",
        "speaker_type": "manager",
        "quote_summary": "Confident in the squad this season",
        "quote_topic": "Season outlook",
        "stage": 4,
    }
    story.update(overrides)
    return story


def _official_press_conference_obs(**overrides):
    defaults = dict(
        title="Enzo Maresca press conference on new signings",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/maresca-press-conference",
        story=_press_conference_story(),
    )
    defaults.update(overrides)
    obs = observation(**defaults)
    obs["document"]["summary"] = (
        "Chelsea head coach Enzo Maresca spoke to reporters at his pre-match "
        'press conference: "Confident in the squad this season."'
    )
    return obs


# ── PRESS_CONFERENCE: official source publishes ───────────────────────────

def test_official_press_conference_publishes(runtime, tmp_path):
    decision = runtime.verify_observations([_official_press_conference_obs()])
    assert decision.decision == DecisionType.PUBLISH, decision.reasons
    assert decision.may_publish
    assert decision.event_type == EventType.PRESS_CONFERENCE

    validation = validate_official_press_conference(decision, runtime.sources)
    assert validation.ok, validation.reason

    caption = decision.rendered_text
    assert "Enzo Maresca" in caption
    assert "Chelsea" in caption
    assert "Confident in the squad this season" in caption
    assert "http" not in caption
    assert "Source:" not in caption
    assert len(caption.splitlines()) <= 4

    card_path = tmp_path / "press.png"
    create_verified_card(decision, runtime.sources, card_path, fpl_data=FPL_DATA)
    assert card_path.exists()
    with Image.open(card_path) as im:
        assert im.size == (3840, 2160)


# ── PRESS_CONFERENCE: media outlet alone can never publish it ─────────────

def test_media_outlet_quoting_the_same_press_conference_is_blocked(runtime):
    """A journalist who was literally in the room is still not official."""
    obs = observation(
        title="Maresca press conference: manager confident in the squad",
        source_id="media.bbc_sport",
        url="https://www.bbc.co.uk/sport/football/maresca-press-conference",
        story=_press_conference_story(),
    )
    obs["document"]["summary"] = (
        "Chelsea head coach Enzo Maresca told reporters at his press "
        'conference: "Confident in the squad this season."'
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision != DecisionType.PUBLISH
    assert not decision.may_publish


def test_two_media_outlets_agreeing_still_cannot_publish_a_press_conference(runtime):
    """Even independent corroboration from two outlets is not official."""
    bbc = observation(
        title="Maresca press conference: manager confident in the squad",
        source_id="media.bbc_sport",
        url="https://www.bbc.co.uk/sport/football/maresca-press-conference",
        story=_press_conference_story(),
    )
    bbc["document"]["summary"] = (
        'Enzo Maresca told reporters at his press conference: "Confident in '
        'the squad this season."'
    )
    sky = observation(
        title="Maresca press conference: manager confident in the squad",
        source_id="media.sky_sports",
        url="https://www.skysports.com/football/news/maresca-press-conference",
        story=_press_conference_story(),
    )
    sky["document"]["summary"] = (
        'Enzo Maresca told reporters at his press conference: "Confident in '
        'the squad this season."'
    )
    decision = runtime.verify_observations([bbc, sky])
    assert decision.decision != DecisionType.PUBLISH
    assert not decision.may_publish


# ── PRESS_CONFERENCE: strict gate blocks missing required facts ───────────

def test_press_conference_gate_blocks_missing_quote_summary(runtime):
    from datetime import datetime, timezone
    from src.verification.models import (
        DecisionType as DT, EventStatus, GateResult, GateState,
        VerificationDecision as VD,
    )

    decision = VD(
        decision=DT.PUBLISH, story_id="s", family_id="f",
        event_type=EventType.PRESS_CONFERENCE, status=EventStatus.OFFICIAL,
        verified_facts={"subject_name": "Enzo Maresca", "club_name": "Chelsea"},
        source_ids=["club.chelsea"], publisher_groups=["club-chelsea"],
        gates=[GateResult("x", GateState.PASS, "ok")],
        reasons=[], confidence=1.0, confidence_dimensions={},
        evidence_document_ids=["d"], fingerprint="fp",
        source_url="https://www.chelseafc.com/news/example",
    )
    validation = validate_official_press_conference(decision, runtime.sources)
    assert not validation.ok
    assert validation.reason == "missing_quote_summary"


def test_create_verified_card_raises_for_unverified_press_conference(runtime, tmp_path):
    from src.verification.models import (
        DecisionType as DT, EventStatus, GateResult, GateState,
        VerificationDecision as VD,
    )

    fake_decision = VD(
        decision=DT.PUBLISH, story_id="s", family_id="f",
        event_type=EventType.PRESS_CONFERENCE, status=EventStatus.COMPLETED,
        verified_facts={
            "subject_name": "Enzo Maresca", "club_name": "Chelsea",
            "quote_summary": "Confident in the squad",
        },
        source_ids=["media.bbc_sport"], publisher_groups=["media-bbc"],
        gates=[GateResult("x", GateState.PASS, "ok")],
        reasons=[], confidence=1.0, confidence_dimensions={},
        evidence_document_ids=["d"], fingerprint="fp2",
        source_url="https://www.bbc.co.uk/sport/football/maresca",
    )
    with pytest.raises(UnverifiedPressConferenceError):
        create_verified_card(fake_decision, runtime.sources, tmp_path / "x.png")


# ── Caption format: 4 lines, no URL, across every category ────────────────

def test_transfer_caption_has_no_url_and_stays_within_four_lines(runtime):
    obs = observation(
        title="Chelsea sign Danny Welbeck from Brighton",
        source_id="club.chelsea",
        url="https://www.chelseafc.com/en/news/article/chelsea-sign-danny-welbeck",
        story=transfer_story(),
    )
    decision = runtime.verify_observations([obs])
    assert decision.may_publish
    caption = decision.rendered_text
    assert "http" not in caption
    assert "Source:" not in caption
    assert "Official confirmation:" not in caption
    assert len(caption.splitlines()) <= 4


def test_injury_caption_has_no_url_and_stays_within_four_lines(runtime):
    obs = observation(
        title="Danny Welbeck: Hamstring injury - ruled out",
        source_id="official.fpl",
        url="https://fantasy.premierleague.com/api/bootstrap-static/",
        story={
            "player": "Danny Welbeck", "event": "injury", "from_key": "Brighton",
            "from_club": "Brighton", "diagnosis": "Hamstring injury - ruled out",
            "stage": 3,
        },
        transport="DIRECT_API", structured=True,
    )
    decision = runtime.verify_observations([obs])
    assert decision.may_publish
    caption = decision.rendered_text
    assert "http" not in caption
    assert "Source:" not in caption
    assert len(caption.splitlines()) <= 4


# ── Ambiguous trigger phrase: "ruled out" needs real injury evidence ──────

def test_disallowed_goal_ruled_out_is_not_misclassified_as_injury():
    """Regression test for the exact false post this rebuild fixes.

    A disallowed-goal ("goal ruled out") match on the bare "ruled out"
    trigger phrase must never be classified as an injury -- it has none of
    the corroborating medical/fitness language a real injury story carries.
    """
    cfg = VerificationConfig.load()
    clf = SemanticClassifier(cfg)

    class FakeDoc:
        def __init__(self, title, body):
            self.title = title
            self.body = body

        @property
        def text(self):
            return f"{self.title}. {self.body}"

    doc = FakeDoc(
        "Meunier then had a goal ruled out",
        "Meunier then had a goal ruled out deep into first-half stoppage time "
        "after heading home Xhaka's cross, with the referee adjudging the "
        "Belgian to have pushed his marker",
    )
    result = clf.classify(doc, None)
    assert result.event_type == EventType.UNKNOWN
    assert any("uncorroborated_event_type" in w for w in result.warnings)


def test_genuine_injury_ruled_out_still_classifies_correctly():
    """The fix must not create false negatives on real injury stories."""
    cfg = VerificationConfig.load()
    clf = SemanticClassifier(cfg)

    class FakeDoc:
        def __init__(self, title, body):
            self.title = title
            self.body = body

        @property
        def text(self):
            return f"{self.title}. {self.body}"

    doc = FakeDoc(
        "Trai Hume ruled out",
        "Trai Hume has been ruled out with a hamstring injury, Sunderland "
        "have confirmed.",
    )
    result = clf.classify(doc, None)
    assert result.event_type == EventType.INJURY


def test_disallowed_goal_scenario_never_reaches_publish_end_to_end(runtime):
    """The same false-post scenario, run through the full pipeline."""
    obs = observation(
        title="Meunier then had a goal ruled out",
        source_id="official.fpl",
        url="https://fantasy.premierleague.com/api/bootstrap-static/",
        story={
            "player": "Danny Welbeck", "event": "injury", "from_key": "Brighton",
            "from_club": "Brighton",
            "diagnosis": (
                "Meunier then had a goal ruled out deep into first-half "
                "stoppage time after heading home Xhaka's cross, with the "
                "referee adjudging the Belgian to have pushed his marker"
            ),
            "stage": 3,
        },
        transport="DIRECT_API", structured=True,
    )
    obs["document"]["summary"] = (
        "Meunier then had a goal ruled out deep into first-half stoppage "
        "time after heading home Xhaka's cross, with the referee adjudging "
        "the Belgian to have pushed his marker"
    )
    decision = runtime.verify_observations([obs])
    assert decision.decision != DecisionType.PUBLISH
    assert not decision.may_publish
