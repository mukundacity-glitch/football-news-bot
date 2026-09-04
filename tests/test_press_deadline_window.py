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


def test_window_opens_60_minutes_before_and_ends_30_minutes_before_deadline():
    deadline = datetime(2026, 9, 12, 13, 30, tzinfo=timezone.utc)
    fpl = _fpl_fixture(deadline)

    at_start = window.deadline_window_status(
        fpl,
        now=deadline-timedelta(minutes=60),
    )
    inside = window.deadline_window_status(
        fpl,
        now=deadline-timedelta(minutes=45),
    )
    at_end = window.deadline_window_status(
        fpl,
        now=deadline-timedelta(minutes=30),
    )

    assert at_start[0] is True
    assert at_start[1] == "inside_press_publication_window"
    assert at_start[2] == deadline
    assert at_start[3] == deadline-timedelta(minutes=60)
    assert inside[0] is True
    assert at_end[0] is False


def test_window_rejects_early_and_late_runs():
    deadline = datetime(2026, 9, 12, 13, 30, tzinfo=timezone.utc)
    fpl = _fpl_fixture(deadline)

    early = window.deadline_window_status(
        fpl, now=deadline-timedelta(minutes=61),
    )
    late = window.deadline_window_status(
        fpl, now=deadline-timedelta(minutes=29),
    )

    assert early[0] is False and early[1].startswith("too_early")
    assert late[0] is False and late[1].startswith("outside_window")


def test_window_uses_the_next_future_event_only():
    now = datetime(2026, 9, 12, 12, 30, tzinfo=timezone.utc)
    next_deadline = now+timedelta(minutes=60)
    fpl = {
        "events": [
            {"id": 6, "deadline_time": (now-timedelta(days=7)).isoformat()},
            {"id": 7, "deadline_time": next_deadline.isoformat()},
            {"id": 8, "deadline_time": (next_deadline+timedelta(days=7)).isoformat()},
        ],
    }

    ok, _reason, deadline, window_start = window.deadline_window_status(fpl, now=now)

    assert ok is True
    assert deadline == next_deadline
    assert window_start == now


def test_invalid_window_configuration_is_rejected():
    deadline = datetime(2026, 9, 12, 13, 30, tzinfo=timezone.utc)
    fpl = _fpl_fixture(deadline)

    try:
        window.deadline_window_status(
            fpl,
            now=deadline-timedelta(minutes=45),
            start_before_minutes=30,
            end_before_minutes=60,
        )
    except ValueError as exc:
        assert "start_before_minutes" in str(exc)
    else:
        raise AssertionError("invalid publication window must raise ValueError")
