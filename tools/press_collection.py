#!/usr/bin/env python3
"""Lightweight FPL-only press-conference collector.

The official FPL bootstrap API is the only approved source for this lane. The
current public bootstrap payload does not expose a manager press-conference
roundup, so this collector fails closed unless an explicit ``press_conferences``
array is supplied by the FPL payload. It never falls back to Google News,
PremierLeague.com, journalists, social feeds, or other media.
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "press_conference_collection.json"
FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
PRESS_FEED_ID = "official.fpl.news"
FPL_SOURCE_ID = "official.fpl"
USER_AGENT = "FPLVortexBot/2.0 (+https://github.com/mukundacity-glitch/football-news-bot)"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _request_json(url: str, timeout: int = 20) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def _parse_deadline(raw: object) -> Optional[datetime]:
    try:
        value = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _event_cycle(fpl_data: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    events: list[tuple[datetime, dict[str, Any]]] = []
    for event in fpl_data.get("events", []) or []:
        deadline = _parse_deadline(event.get("deadline_time"))
        if deadline is not None:
            events.append((deadline, event))
    events.sort(key=lambda pair: pair[0])
    next_index = next((index for index, (deadline, _event) in enumerate(events) if deadline > now), None)
    if next_index is None:
        return None
    deadline, event = events[next_index]
    previous_deadline = events[next_index - 1][0] if next_index > 0 else None
    event_id = str(event.get("id") or event.get("name") or deadline.isoformat())
    return {
        "event_id": event_id,
        "event_name": str(event.get("name") or f"Gameweek {event_id}"),
        "deadline": deadline,
        "previous_deadline": previous_deadline,
    }


def _explicit_fpl_press_items(fpl_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Read only an explicit FPL press-conference collection, if present.

    We intentionally do not reinterpret player ``news`` as a manager press
    conference. Injury/availability news is handled by the normal FPL lane.
    """
    raw_items = fpl_data.get("press_conferences")
    if not isinstance(raw_items, list):
        return []
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or raw.get("headline") or "").strip()
        text = str(raw.get("text") or raw.get("body") or raw.get("summary") or "").strip()
        if not title and not text:
            continue
        stable = "|".join((title, text, str(raw.get("published_at") or "")))
        items.append({
            "id": hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24],
            "title": title or text[:180],
            "link": FPL_BOOTSTRAP_URL,
            "published_at": raw.get("published_at"),
            "publisher_url": FPL_BOOTSTRAP_URL,
            "publisher_name": "Fantasy Premier League",
            "source_id": FPL_SOURCE_ID,
            "feed_id": PRESS_FEED_ID,
            "text": text or title,
        })
    return items


def merge_collection(previous: dict[str, Any] | None, *, cycle: dict[str, Any], discovered: list[dict[str, Any]], now: datetime) -> tuple[dict[str, Any], bool]:
    same_event = bool(previous and str(previous.get("event_id")) == str(cycle["event_id"]))
    existing = {
        str(item.get("id")): dict(item)
        for item in ((previous or {}).get("items", []) if same_event else [])
        if item.get("id")
    }
    changed = not same_event
    for item in discovered:
        item_id = str(item["id"])
        if item_id in existing:
            continue
        stored = dict(item)
        stored["first_seen"] = now.isoformat()
        existing[item_id] = stored
        changed = True
    ordered = list(existing.values())[-60:]
    state = {
        "schema_version": 1,
        "event_id": str(cycle["event_id"]),
        "event_name": cycle["event_name"],
        "previous_deadline": cycle["previous_deadline"].isoformat() if cycle.get("previous_deadline") else None,
        "deadline": cycle["deadline"].isoformat(),
        "items": ordered,
    }
    if changed:
        state["updated_at"] = now.isoformat()
    elif previous and previous.get("updated_at"):
        state["updated_at"] = previous["updated_at"]
    return state, changed


def main() -> int:
    now = utcnow()
    try:
        fpl_data = _request_json(FPL_BOOTSTRAP_URL)
    except Exception as exc:
        print(f"[PRESS-COLLECT] Official FPL fetch failed: {type(exc).__name__}: {exc}")
        return 0

    cycle = _event_cycle(fpl_data, now)
    if cycle is None:
        print("[PRESS-COLLECT] No future FPL deadline; nothing to collect.")
        return 0

    discovered = _explicit_fpl_press_items(fpl_data)
    if not discovered:
        print("[PRESS-COLLECT] FPL payload has no explicit press_conferences feed; failing closed.")

    previous = None
    if STATE_PATH.exists():
        try:
            previous = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            previous = None

    state, changed = merge_collection(previous, cycle=cycle, discovered=discovered, now=now)
    if changed or not STATE_PATH.exists():
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"[PRESS-COLLECT] {state['event_name']} deadline={state['deadline']} stored={len(state['items'])} new_state={'yes' if changed else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
