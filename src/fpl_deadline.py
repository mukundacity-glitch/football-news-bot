"""Shared official-FPL deadline timing helpers.

This module is intentionally dependency-free so a GitHub Actions window check
can run before the full bot environment is installed.  Production and tests use
the same functions; the deadline workflow therefore cannot drift away from the
press-roundup pipeline's timing rules.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

DEFAULT_MARGIN_MINUTES = 30


def next_fpl_deadline(
    fpl_data: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """Return the next official FPL lock time in UTC."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    deadlines: list[datetime] = []
    for event in fpl_data.get("events", []) or []:
        raw = event.get("deadline_time")
        if not raw:
            continue
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            value = value.astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        if value > now:
            deadlines.append(value)
    return min(deadlines) if deadlines else None


def deadline_target(
    fpl_data: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    margin_minutes: int = DEFAULT_MARGIN_MINUTES,
) -> Optional[datetime]:
    """Return the desired run time before the next official FPL lock."""
    deadline = next_fpl_deadline(fpl_data, now=now)
    return deadline - timedelta(minutes=margin_minutes) if deadline else None


def deadline_window_open(
    fpl_data: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    margin_minutes: int = DEFAULT_MARGIN_MINUTES,
    window_minutes: int = 30,
) -> bool:
    """Return true from the target time until the configured pre-lock window ends."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    target = deadline_target(
        fpl_data, now=now, margin_minutes=margin_minutes,
    )
    if target is None:
        return False
    deadline = target + timedelta(minutes=margin_minutes)
    return target <= now < min(deadline, target + timedelta(minutes=window_minutes))
