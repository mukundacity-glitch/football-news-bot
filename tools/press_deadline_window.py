#!/usr/bin/env python3
"""Dependency-free GitHub Actions gate for the press-roundup deadline run."""
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

from src.fpl_deadline import deadline_target, next_fpl_deadline

FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
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
    margin_minutes: int = 30,
    window_minutes: int = 30,
) -> tuple[bool, str, Optional[datetime], Optional[datetime]]:
    """Return the pre-deadline state plus the official deadline and target."""
    now = now or utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    deadline = next_fpl_deadline(fpl_data, now=now)
    if deadline is None:
        return False, "no_future_fpl_deadline", None, None
    target = deadline_target(
        fpl_data, now=now, margin_minutes=margin_minutes,
    )
    if target is None:
        return False, "no_future_fpl_deadline", deadline, None
    # Use an explicit duration rather than deriving it from the margin; the
    # target and retry window are independent workflow controls.
    window_end = min(deadline, target + timedelta(minutes=window_minutes))
    if target <= now < window_end:
        return True, "inside_press_roundup_window", deadline, target
    if now < target:
        return False, f"too_early_target_{target.isoformat()}", deadline, target
    return False, f"outside_window_target_{target.isoformat()}", deadline, target


def github_output_check(*, margin_minutes: int, window_minutes: int) -> int:
    try:
        fpl_data = fetch_fpl_bootstrap()
        ok, reason, deadline, target = deadline_window_status(
            fpl_data,
            margin_minutes=margin_minutes,
            window_minutes=window_minutes,
        )
        print(f"should_run={'true' if ok else 'false'}")
        print(f"reason={reason}")
        if deadline:
            print(f"deadline={deadline.isoformat()}")
        if target:
            print(f"target_run={target.isoformat()}")
    except Exception as exc:
        reason = f"check_failed_{type(exc).__name__}:{exc}"
        print("should_run=false")
        print(f"reason={reason.replace(chr(10), ' ')[:180]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check the official FPL pre-deadline press-roundup window.",
    )
    parser.add_argument("--check-window", action="store_true")
    parser.add_argument("--margin-minutes", type=int, default=30)
    parser.add_argument("--window-minutes", type=int, default=30)
    args = parser.parse_args()
    if not args.check_window:
        parser.error("--check-window is required")
    return github_output_check(
        margin_minutes=args.margin_minutes,
        window_minutes=args.window_minutes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
