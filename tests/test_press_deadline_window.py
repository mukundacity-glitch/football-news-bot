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


def test_window_opens_90_minutes_before_and_ends_60_minutes_before_deadline():
    deadline = datetime(2026, 9, 12, 13, 30, tzinfo=timezone.utc)
    fpl = _fpl_fixture(deadline)

    at_start = window.deadline_window_status(
        fpl,
        now=deadline-timedelta(minutes=90),
    )
    inside = window.deadline_window_status(
        fpl,
        now=deadline-timedelta(minutes=75),
    )
    at_end = window.deadline_window_status(
        fpl,
        now=deadline-timedelta(minutes=60),
    )

    assert at_start[0] is True
    assert at_start[1] == "inside_press_publication_window"
    assert at_start[2] == deadline
    assert at_start[3] == deadline-timedelta(minutes=90)
    assert inside[0] is True
    assert at_end[0] is False


def test_window_rejects_early_and_late_runs():
    deadline = datetime(2026, 9, 12, 13, 30, tzinfo=timezone.utc)
    fpl = _fpl_fixture(deadline)

    early = window.deadline_window_status(
        fpl, now=deadline-timedelta(minutes=91),
    )
    late = window.deadline_window_status(
        fpl, now=deadline-timedelta(minutes=59),
    )

    assert early[0] is False and early[1].startswith("too_early")
    assert late[0] is False and late[1].startswith("outside_window")


def test_window_uses_the_next_future_event_only():
    now = datetime(2026, 9, 12, 12, 30, tzinfo=timezone.utc)
    next_deadline = now+timedelta(minutes=90)
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
            now=deadline-timedelta(minutes=75),
            start_before_minutes=60,
            end_before_minutes=90,
        )
    except ValueError as exc:
        assert "start_before_minutes" in str(exc)
    else:
        raise AssertionError("invalid publication window must raise ValueError")


def test_noon_kickoff_starts_at_nine_and_finishes_by_nine_thirty():
    from zoneinfo import ZoneInfo
    local_timezone = ZoneInfo("America/New_York")
    kickoff = datetime(2026, 10, 10, 12, tzinfo=local_timezone)
    deadline = (kickoff-timedelta(minutes=90)).astimezone(timezone.utc)
    fpl = _fpl_fixture(deadline)
    fixtures = [
        {"event": 8, "kickoff_time": "2026-10-09T12:00:00Z"},
        {"event": 7, "kickoff_time": None},
        {"event": 7, "kickoff_time": (kickoff+timedelta(hours=2)).isoformat()},
        {"event": 7, "kickoff_time": kickoff.isoformat()},
    ]
    assert window.first_gameweek_kickoff(fpl, fixtures, deadline) == kickoff
    at_start = kickoff-timedelta(hours=3)
    at_cutoff = kickoff-timedelta(hours=2, minutes=30)
    assert at_start.hour == 9 and at_start.minute == 0
    assert at_cutoff.hour == 9 and at_cutoff.minute == 30
    assert window.deadline_window_status(fpl, now=at_start)[0] is True
    assert window.deadline_window_status(fpl, now=at_cutoff)[0] is False


def test_fixture_fetch_failure_fails_the_workflow_gate(monkeypatch, capsys):
    monkeypatch.setattr(window, "fetch_fpl_bootstrap", lambda: {"events": []})
    def fail_fetch():
        raise ValueError("fixture data unavailable")
    monkeypatch.setattr(window, "fetch_fpl_fixtures", fail_fetch)
    assert window.github_output_check(start_before_minutes=90, end_before_minutes=60) == 1
    assert "should_run=false" in capsys.readouterr().out


def test_conflicting_kickoff_and_deadline_stop_publication(monkeypatch, capsys):
    deadline = datetime.now(timezone.utc)+timedelta(minutes=75)
    monkeypatch.setattr(window, "fetch_fpl_bootstrap", lambda: _fpl_fixture(deadline))
    monkeypatch.setattr(window, "fetch_fpl_fixtures", lambda: [
        {"event": 7, "kickoff_time": (deadline+timedelta(minutes=120)).isoformat()},
    ])
    assert window.github_output_check(start_before_minutes=90, end_before_minutes=60) == 1
    output = capsys.readouterr().out
    assert "should_run=false" in output and "should_run=true" not in output
