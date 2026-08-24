from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

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
    PREMIER_LEAGUE_SOURCE_ID,
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


def test_official_premier_league_press_feed_is_configured():
    feeds = FeedRegistry.load("config/feeds.json")
    press = next(feed for feed in feeds.feeds if feed.id == "google.premier_league.press_conference")
    query = parse_qs(urlparse(press.url).query)["q"][0]
    assert query.startswith("site:premierleague.com/en/news (")
    assert '"press conference"' in query
    assert query.endswith(") when:2d")
    assert press.source_hint == PREMIER_LEAGUE_SOURCE_ID


def test_all_official_premier_league_searches_group_terms_and_limit_lookback():
    feeds = FeedRegistry.load("config/feeds.json")
    official = {
        feed.id: parse_qs(urlparse(feed.url).query)["q"][0]
        for feed in feeds.feeds
        if feed.id.startswith("google.premier_league.")
    }
    assert set(official) == {
        "google.premier_league.transfers",
        "google.premier_league.availability",
        "google.premier_league.press_conference",
    }
    for query in official.values():
        assert query.startswith("site:premierleague.com/en/news (")
        assert query.endswith(") when:2d")


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


def test_roundup_without_speaker_or_quotes_fails_closed():
    parsed = parse_premier_league_roundup("Press conference update without a named speaker.")
    assert parsed == {"entries": [], "primary": None}


def test_project_roundup_keeps_exact_official_speaker_when_snapshot_lags():
    story = {}
    source = {
        "feed_id": "google.premierleague.press_conference",
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
        source_ids=[PREMIER_LEAGUE_SOURCE_ID],
        publisher_groups=["premier-league"],
        gates=[GateResult("test", GateState.PASS, "ok")],
        reasons=[],
        confidence=1.0,
        confidence_dimensions={},
        evidence_document_ids=["official-press-1"],
        fingerprint="test-fingerprint",
        source_url=url,
        authority_kind="first_party_official",
        authority_source_ids=[PREMIER_LEAGUE_SOURCE_ID],
    )


def test_one_premierleague_source_is_sufficient_without_second_confirmation():
    sources = SourceRegistry.load("config/sources.json")
    result = validate_official_press_conference(
        _decision("https://www.premierleague.com/en/news/1234567"),
        sources,
    )
    assert result.ok is True
    assert result.reason == "official_premierleague_roundup"


def test_non_premierleague_url_is_rejected():
    sources = SourceRegistry.load("config/sources.json")
    result = validate_official_press_conference(
        _decision("https://www.bbc.com/sport/football/123456"),
        sources,
    )
    assert result.ok is False
    assert result.reason == "source_url_is_not_premierleague.com"
