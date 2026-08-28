from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tools import press_deadline_window as window


def _fpl_fixture(deadline: datetime) -> dict:
    return {
        "events": [{
            "id": 7,
            "name": "Gameweek 7",
            "deadline_time": deadline.isoformat().replace("+00:00", "Z"),
        }],
    }


def test_window_opens_30_minutes_before_official_fpl_deadline():
    deadline = datetime(2026, 9, 12, 13, 30, tzinfo=timezone.utc)
    fpl = _fpl_fixture(deadline)

    ok, reason, actual_deadline, target = window.deadline_window_status(
        fpl,
        now=deadline-timedelta(minutes=30),
        margin_minutes=30,
        window_minutes=30,
    )

    assert ok is True
    assert reason == "inside_press_roundup_window"
    assert actual_deadline == deadline
    assert target == deadline-timedelta(minutes=30)


def test_window_rejects_early_and_post_deadline_runs():
    deadline = datetime(2026, 9, 12, 13, 30, tzinfo=timezone.utc)
    fpl = _fpl_fixture(deadline)

    early = window.deadline_window_status(
        fpl, now=deadline-timedelta(minutes=31),
    )
    at_deadline = window.deadline_window_status(fpl, now=deadline)

    assert early[0] is False and early[1].startswith("too_early")
    assert at_deadline[0] is False


def test_window_uses_the_next_future_event_only():
    now = datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc)
    next_deadline = now+timedelta(minutes=30)
    fpl = {
        "events": [
            {"id": 6, "deadline_time": (now-timedelta(days=7)).isoformat()},
            {"id": 7, "deadline_time": next_deadline.isoformat()},
            {"id": 8, "deadline_time": (next_deadline+timedelta(days=7)).isoformat()},
        ],
    }

    ok, _reason, deadline, target = window.deadline_window_status(fpl, now=now)

    assert ok is True
    assert deadline == next_deadline
    assert target == now
