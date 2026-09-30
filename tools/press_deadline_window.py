#!/usr/bin/env python3
"""Dependency-free publication-window gate for the press-conference roundup."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.fpl_deadline import next_fpl_deadline

FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
FPL_FIXTURES_URL = "https://fantasy.premierleague.com/api/fixtures/"
START_BEFORE_DEADLINE_MINUTES = 90
FINISH_BEFORE_DEADLINE_MINUTES = 60
FPL_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "FPLVortexBot/2.0 "
        "(+https://github.com/mukundacity-glitch/football-news-bot)"
    ),
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fetch_fpl_bootstrap(timeout: int = 20) -> dict[str, Any]:
    request = urllib.request.Request(FPL_BOOTSTRAP_URL, headers=FPL_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise ValueError("official FPL payload is missing events")
    return payload


def deadline_window_status(
    fpl_data: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    start_before_minutes: int = START_BEFORE_DEADLINE_MINUTES,
    end_before_minutes: int = FINISH_BEFORE_DEADLINE_MINUTES,
) -> tuple[bool, str, Optional[datetime], Optional[datetime]]:
    """Return whether now is inside the press publication window.

    Start three hours before kickoff and finish two and a half hours before it,
    using the official FPL deadline (90 minutes before the first match).
    """
    if start_before_minutes <= end_before_minutes:
        raise ValueError("start_before_minutes must be greater than end_before_minutes")
    if end_before_minutes < 0:
        raise ValueError("end_before_minutes cannot be negative")

    now = now or utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    deadline = next_fpl_deadline(fpl_data, now=now)
    if deadline is None:
        return False, "no_future_fpl_deadline", None, None

    window_start = deadline - timedelta(minutes=start_before_minutes)
    window_end = deadline - timedelta(minutes=end_before_minutes)
    if window_start <= now < window_end:
        return True, "inside_press_publication_window", deadline, window_start
    if now < window_start:
        return False, f"too_early_window_starts_{window_start.isoformat()}", deadline, window_start
    return False, f"outside_window_ended_{window_end.isoformat()}", deadline, window_start


def fetch_fpl_fixtures(timeout: int = 20) -> list[dict[str, Any]]:
    request = urllib.request.Request(FPL_FIXTURES_URL, headers=FPL_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("official FPL payload is missing fixtures")
    return payload


def first_gameweek_kickoff(
    fpl_data: Mapping[str, Any], fixtures: list[dict[str, Any]], deadline: datetime,
) -> datetime:
    event_ids: set[str] = set()
    for event in fpl_data.get("events", []):
        try:
            event_deadline = datetime.fromisoformat(str(event.get("deadline_time", "")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if event_deadline.tzinfo is None:
            event_deadline = event_deadline.replace(tzinfo=timezone.utc)
        if event_deadline == deadline:
            event_ids.add(str(event.get("id")))
    kickoffs: list[datetime] = []
    for fixture in fixtures:
        if str(fixture.get("event")) not in event_ids or not fixture.get("kickoff_time"):
            continue
        kickoff = datetime.fromisoformat(str(fixture["kickoff_time"]).replace("Z", "+00:00"))
        if kickoff.tzinfo is None:
            raise ValueError("fixture kickoff has no timezone")
        kickoffs.append(kickoff.astimezone(timezone.utc))
    if not kickoffs:
        raise ValueError("next gameweek has no confirmed fixture kickoff")
    return min(kickoffs)


def github_output_check(
    *,
    start_before_minutes: int,
    end_before_minutes: int,
) -> int:
    try:
        fpl_data = fetch_fpl_bootstrap()
        fixtures = fetch_fpl_fixtures()
        ok, reason, deadline, window_start = deadline_window_status(
            fpl_data,
            start_before_minutes=start_before_minutes,
            end_before_minutes=end_before_minutes,
        )
        kickoff = first_gameweek_kickoff(fpl_data, fixtures, deadline) if deadline else None
        if kickoff and kickoff-timedelta(minutes=90) != deadline:
            raise ValueError("first kickoff and official FPL deadline disagree; refusing guessed timing")
        print(f"should_run={'true' if ok else 'false'}")
        print(f"reason={reason}")
        if deadline:
            print(f"first_kickoff={kickoff.isoformat()}")
            print(f"kickoff_calculated_deadline={(kickoff-timedelta(minutes=90)).isoformat()}")
            print(f"deadline={deadline.isoformat()}")
            print(
                "window_end="
                + (deadline - timedelta(minutes=end_before_minutes)).isoformat()
            )
        if window_start:
            print(f"window_start={window_start.isoformat()}")
    except Exception as exc:
        reason = f"check_failed_{type(exc).__name__}:{exc}"
        print("should_run=false")
        print(f"reason={reason.replace(chr(10), ' ')[:180]}")
        print(reason, file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check the official FPL press-conference publication window.",
    )
    parser.add_argument("--check-window", action="store_true")
    parser.add_argument("--start-before-minutes", type=int, default=START_BEFORE_DEADLINE_MINUTES)
    parser.add_argument("--end-before-minutes", type=int, default=FINISH_BEFORE_DEADLINE_MINUTES)
    args = parser.parse_args()
    if not args.check_window:
        parser.error("--check-window is required")
    return github_output_check(
        start_before_minutes=args.start_before_minutes,
        end_before_minutes=args.end_before_minutes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
