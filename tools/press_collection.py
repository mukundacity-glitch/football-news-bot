#!/usr/bin/env python3
"""Lightweight press-conference link collector.

Dependency-free by design. It collects only the configured PremierLeague.com
press-conference feed for the next FPL gameweek and never posts anything.
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
FEEDS_PATH = ROOT / "config" / "feeds.json"
STATE_PATH = ROOT / "data" / "press_conference_collection.json"
FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
PRESS_FEED_ID = "google.premier_league.press_conference"
USER_AGENT = (
    "FPLVortexBot/2.0 "
    "(+https://github.com/mukundacity-glitch/football-news-bot)"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _request_bytes(url: str, timeout: int = 20) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"Accept": "*/*", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _parse_deadline(raw: object) -> Optional[datetime]:
    try:
        value = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_publication(raw: object) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
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

    next_index = next(
        (index for index, (deadline, _event) in enumerate(events) if deadline > now),
        None,
    )
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


def _press_feed() -> dict[str, Any]:
    payload = json.loads(FEEDS_PATH.read_text(encoding="utf-8"))
    for feed in payload.get("feeds", []) or []:
        if feed.get("id") == PRESS_FEED_ID:
            return feed
    raise RuntimeError(f"missing configured feed {PRESS_FEED_ID}")


def _is_premier_league_domain(value: object) -> bool:
    try:
        host = (urlparse(str(value or "")).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return host == "premierleague.com" or host.endswith(".premierleague.com")


def _rss_items(payload: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(payload)
    items: list[dict[str, Any]] = []
    for node in root.findall(".//item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or node.findtext("guid") or "").strip()
        published = (node.findtext("pubDate") or "").strip()
        source = node.find("source")
        publisher_url = (source.attrib.get("url") if source is not None else "") or ""
        publisher_name = ((source.text or "").strip() if source is not None else "")
        if not title or not link:
            continue
        if publisher_url and not _is_premier_league_domain(publisher_url):
            continue
        stable = "|".join((link, title, published, publisher_url))
        items.append({
            "id": hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24],
            "title": title,
            "link": link,
            "published_at": published or None,
            "publisher_url": publisher_url or None,
            "publisher_name": publisher_name or None,
        })
    return items


def merge_collection(
    previous: dict[str, Any] | None,
    *,
    cycle: dict[str, Any],
    discovered: list[dict[str, Any]],
    now: datetime,
) -> tuple[dict[str, Any], bool]:
    same_event = bool(
        previous
        and str(previous.get("event_id")) == str(cycle["event_id"])
    )
    existing = {
        str(item.get("id")): dict(item)
        for item in ((previous or {}).get("items", []) if same_event else [])
        if item.get("id")
    }

    changed = not same_event
    previous_deadline = cycle.get("previous_deadline")
    for item in discovered:
        published = _parse_publication(item.get("published_at"))
        if previous_deadline and published and published < previous_deadline:
            continue
        item_id = str(item["id"])
        if item_id in existing:
            continue
        stored = dict(item)
        stored["first_seen"] = now.isoformat()
        existing[item_id] = stored
        changed = True

    ordered = sorted(
        existing.values(),
        key=lambda item: (
            _parse_publication(item.get("published_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
            str(item.get("id") or ""),
        ),
        reverse=True,
    )[:60]

    state = {
        "schema_version": 1,
        "event_id": str(cycle["event_id"]),
        "event_name": cycle["event_name"],
        "previous_deadline": (
            cycle["previous_deadline"].isoformat()
            if cycle.get("previous_deadline") else None
        ),
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
    fpl_data = json.loads(_request_bytes(FPL_BOOTSTRAP_URL).decode("utf-8"))
    cycle = _event_cycle(fpl_data, now)
    if cycle is None:
        print("[PRESS-COLLECT] No future FPL deadline; nothing to collect.")
        return 0

    feed = _press_feed()
    discovered = _rss_items(_request_bytes(str(feed["url"])))
    previous = None
    if STATE_PATH.exists():
        try:
            previous = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            previous = None

    state, changed = merge_collection(
        previous,
        cycle=cycle,
        discovered=discovered,
        now=now,
    )
    if changed or not STATE_PATH.exists():
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    print(
        f"[PRESS-COLLECT] {state['event_name']} deadline={state['deadline']} "
        f"stored={len(state['items'])} new_state={'yes' if changed else 'no'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
