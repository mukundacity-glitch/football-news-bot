from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tools.press_collection import merge_collection


def _cycle(deadline: datetime) -> dict:
    return {
        "event_id": "3",
        "event_name": "Gameweek 3",
        "previous_deadline": deadline-timedelta(days=7),
        "deadline": deadline,
    }


def test_collection_deduplicates_same_press_item():
    deadline = datetime(2026, 9, 5, 17, 30, tzinfo=timezone.utc)
    now = deadline-timedelta(days=1)
    item = {
        "id": "same",
        "title": "Press conference roundup",
        "link": "https://news.google.com/example",
        "published_at": (now-timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        "publisher_url": "https://www.premierleague.com/",
        "publisher_name": "Premier League",
    }

    first, changed_first = merge_collection(
        None, cycle=_cycle(deadline), discovered=[item], now=now,
    )
    second, changed_second = merge_collection(
        first, cycle=_cycle(deadline), discovered=[item], now=now+timedelta(minutes=10),
    )

    assert changed_first is True
    assert changed_second is False
    assert len(second["items"]) == 1


def test_collection_resets_when_next_gameweek_changes():
    deadline = datetime(2026, 9, 5, 17, 30, tzinfo=timezone.utc)
    previous = {
        "event_id": "2",
        "event_name": "Gameweek 2",
        "deadline": (deadline-timedelta(days=7)).isoformat(),
        "items": [{"id": "old"}],
    }

    state, changed = merge_collection(
        previous,
        cycle=_cycle(deadline),
        discovered=[],
        now=deadline-timedelta(days=1),
    )

    assert changed is True
    assert state["event_id"] == "3"
    assert state["items"] == []
