from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.verification.models import (
    DecisionType,
    EventStatus,
    EventType,
    GateResult,
    GateState,
    VerificationDecision,
)
from src.verification.press_conference_gate import validate_official_press_conference
from src.verification.press_roundup import (
    FPL_SOURCE_ID,
    PRESS_FEED_ID,
    is_fpl_url,
    parse_premier_league_roundup,
    press_deadline_target,
    press_deadline_window_open,
    project_roundup_story,
)
from src.verification.source_registry import SourceRegistry
from src.verification.documents import FeedRegistry


ROUNDUP_TEXT = """
All the key quotes from EVERY manager's press conference.
TV Info - Broadcasters Mikel Arteta (Arsenal)
On the squad: “The squad is ready and we want to start strongly.”
On fitness: “The players have trained well and are available.”
Pep Guardiola (Man City)
On the season: “We want a strong start and a positive performance.”
Arne Slot (Liverpool)
On the team: “We will focus on our identity.”
"""

TWENTY_MANAGERS = [
    "Alex Adams", "Ben Brown", "Carl Clark", "David Davies", "Evan Evans",
    "Frank Foster", "Gareth Green", "Harry Harris", "Ivan Ingram", "Jack Jones",
    "Kevin King", "Liam Lewis", "Martin Moore", "Nathan North", "Oscar Owens",
    "Peter Price", "Quentin Quinn", "Robert Reed", "Samuel Stone", "Thomas Taylor",
]


def test_only_fpl_is_used_for_press_provenance():
    feeds = FeedRegistry.load("config/feeds.json")
    assert [feed.id for feed in feeds.feeds] == ["fotmob.premier_league.topnews"]
    assert not feeds.social_feeds
    assert feeds.official_discovery.get("enabled") is False
    assert FPL_SOURCE_ID == "official.fpl"
    assert PRESS_FEED_ID == "official.fpl.news"
    assert is_fpl_url("https://fantasy.premierleague.com/api/bootstrap-static/") is True
    assert is_fpl_url("https://www.premierleague.com/en/news/123") is False


def test_fpl_press_item_requires_official_fpl_provenance():
    assert project_roundup_story(
        {},
        {
            "source_id": FPL_SOURCE_ID,
            "feed_id": PRESS_FEED_ID,
            "source_url": "https://fantasy.premierleague.com/api/bootstrap-static/",
            "full_text": ROUNDUP_TEXT,
        },
        resolve_staff=lambda _name: None,
        resolve_club_key=lambda club: club.lower(),
    ) is True

    assert project_roundup_story(
        {},
        {
            "source_id": "official.premier_league",
            "feed_id": "official.premier_league.press",
            "source_url": "https://www.premierleague.com/en/news/123",
            "full_text": ROUNDUP_TEXT,
        },
    ) is False


def test_official_roundup_extracts_all_sections_for_existing_graphic_fields():
    parsed = parse_premier_league_roundup(ROUNDUP_TEXT)

    assert parsed["primary"] == {
        "name": "Mikel Arteta",
        "club": "Arsenal",
        "quote_summary": "The squad is ready and we want to start strongly",
        "quote_topic": "the squad",
    }
    assert len(parsed["entries"]) == 3
    assert len(parsed["roundup"]) == 3
    assert parsed["latest_news"]
    assert parsed["key_quotes"]
    assert parsed["manager_notes"]
    assert "Pep Guardiola" in " ".join(parsed["roundup"])
    assert "Arne Slot" in " ".join(parsed["roundup"])


def test_official_roundup_keeps_all_twenty_manager_sections():
    text = "\n".join(
        f"{name} (Club {index})\n"
        f"On team news: \u201cVerified squad update number {index} for the next match.\u201d"
        for index, name in enumerate(TWENTY_MANAGERS, start=1)
    )

    parsed = parse_premier_league_roundup(text)

    assert len(parsed["entries"]) == 20
    assert len(parsed["roundup"]) == 20
    assert "Thomas Taylor" in parsed["roundup"][-1]


def test_roundup_without_speaker_or_quotes_fails_closed():
    parsed = parse_premier_league_roundup("Press conference update without a named speaker.")
    assert parsed == {"entries": [], "primary": None}


def test_project_roundup_keeps_exact_official_speaker_when_snapshot_lags():
    story = {}
    source = {
        "source_id": FPL_SOURCE_ID,
        "feed_id": PRESS_FEED_ID,
        "source_url": "https://fantasy.premierleague.com/api/bootstrap-static/",
        "full_text": ROUNDUP_TEXT,
    }
    assert project_roundup_story(
        story,
        source,
        resolve_staff=lambda _name: None,
        resolve_club_key=lambda club: club.lower(),
    ) is True
    assert story["player"] == "Mikel Arteta"
    assert story["event"] == "press_conference"
    assert story["roundup"]
    assert story["key_quotes"]


def test_deadline_target_is_30_minutes_before_fpl_lock():
    deadline = datetime(2026, 8, 28, 13, 30, tzinfo=timezone.utc)
    fpl = {"events": [{"id": 1, "name": "GW1", "deadline_time": deadline.isoformat()}]}

    assert press_deadline_target(fpl, now=deadline - timedelta(hours=2)) == deadline - timedelta(minutes=30)
    assert press_deadline_window_open(
        fpl,
        now=deadline - timedelta(minutes=30),
        window_minutes=20,
    ) is True
    assert press_deadline_window_open(
        fpl,
        now=deadline - timedelta(minutes=31),
        window_minutes=20,
    ) is False


def _decision(url: str) -> VerificationDecision:
    return VerificationDecision(
        decision=DecisionType.PUBLISH,
        story_id="press-roundup-test",
        family_id="press-roundup-family",
        event_type=EventType.PRESS_CONFERENCE,
        status=EventStatus.OFFICIAL,
        verified_facts={
            "subject_id": "staff:mikel-arteta",
            "subject_name": "Mikel Arteta",
            "club_id": "club:arsenal",
            "club_name": "Arsenal",
            "quote_summary": "The squad is ready",
            "key_quotes": ["Mikel Arteta: The squad is ready"],
            "roundup": ["Arsenal — Mikel Arteta: The squad is ready"],
        },
        source_ids=[FPL_SOURCE_ID],
        publisher_groups=["premier-league"],
        gates=[GateResult("test", GateState.PASS, "ok")],
        reasons=[],
        confidence=1.0,
        confidence_dimensions={},
        evidence_document_ids=["fpl-press-1"],
        fingerprint="test-fingerprint",
        source_url=url,
        authority_kind="first_party_official",
        authority_source_ids=[FPL_SOURCE_ID],
    )


def test_one_fpl_source_is_sufficient_without_second_confirmation():
    sources = SourceRegistry.load("config/sources.json")
    result = validate_official_press_conference(
        _decision("https://fantasy.premierleague.com/api/bootstrap-static/"),
        sources,
    )
    assert result.ok is True
    assert result.reason == "official_fpl_press_roundup"


def test_non_fpl_url_is_rejected():
    sources = SourceRegistry.load("config/sources.json")
    result = validate_official_press_conference(
        _decision("https://www.bbc.com/sport/football/123456"),
        sources,
    )
    assert result.ok is False
    assert result.reason == "source_url_is_not_fpl"
