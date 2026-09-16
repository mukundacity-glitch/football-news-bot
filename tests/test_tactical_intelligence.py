from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.tactical_intelligence import (
    Fixture,
    fit_thesis,
    local_time_due,
    normalize_name,
    post_key,
    priority_score,
    Candidate,
)


def _fixture() -> Fixture:
    return Fixture(
        fixture_id=123,
        home_id=1,
        away_id=2,
        home="Home Club",
        away="Away Club",
        kickoff=datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc),
        finished=False,
        home_score=None,
        away_score=None,
    )


def test_normalize_name_is_stable_for_provider_matching():
    assert normalize_name("A.F.C. Example") == normalize_name("AFC Example")


def test_fit_thesis_always_respects_editorial_word_limit():
    short = fit_thesis("Watch the midfield gap")
    long = fit_thesis("one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen")
    assert 8 <= len(short.split()) <= 14
    assert 8 <= len(long.split()) <= 14


def test_local_time_due_uses_configured_timezone_and_window():
    config = {
        "audience_timezone": "America/New_York",
        "watch_local_time": "09:00",
        "schedule_window_minutes": 20,
    }
    at_target = datetime(2026, 9, 16, 13, 5, tzinfo=timezone.utc)
    outside = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)
    assert local_time_due(at_target, config, "watch_local_time") is True
    assert local_time_due(outside, config, "watch_local_time") is False


def test_priority_score_is_bounded_and_uses_dynamic_signals():
    fixture = _fixture()
    teams = {1: {"position": 1}, 2: {"position": 2}}
    ownership = {1: 100.0, 2: 50.0, 3: 25.0}
    score = priority_score(
        fixture,
        teams,
        ownership,
        novelty=0.8,
        now=datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc),
        review=False,
    )
    assert 0 <= score <= 100
    assert score >= 45


def test_post_key_deduplicates_same_fixture_story():
    fixture = _fixture()
    post = {"thesis": "Watch whether the midfield battle creates a repeatable tactical edge"}
    first = Candidate("watch", fixture, post, 70, [])
    second = Candidate("watch", fixture, dict(post), 70, [])
    assert post_key(first) == post_key(second)


def test_post_key_changes_when_story_changes():
    fixture = _fixture()
    first = Candidate("watch", fixture, {"thesis": "Watch whether the midfield battle creates a repeatable tactical edge"}, 70, [])
    second = Candidate("watch", fixture, {"thesis": "Watch whether repeated set pieces create a different tactical edge"}, 70, [])
    assert post_key(first) != post_key(second)
