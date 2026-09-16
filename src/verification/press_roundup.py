"""FotMob-only press-conference roundup helpers."""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlparse

from src.fpl_deadline import (
    DEFAULT_MARGIN_MINUTES,
    deadline_target,
    deadline_window_open,
    next_fpl_deadline as _shared_next_fpl_deadline,
)

FOTMOB_SOURCE_ID = "media.fotmob"
FOTMOB_DOMAIN = "fotmob.com"
PRESS_FEED_ID = "fotmob.premier_league.topnews"
PRESS_DEADLINE_MARGIN_MINUTES = DEFAULT_MARGIN_MINUTES
MAX_PREMIER_LEAGUE_ROUNDUP_ENTRIES = 20

_NAME_TOKEN = r"[A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+"
_SPEAKER_RE = re.compile(
    rf"(?P<name>{_NAME_TOKEN}(?:\s+{_NAME_TOKEN}){{1,5}})"
    rf"\s*\(\s*(?P<club>[^()\n]{{2,70}}?)\s*\)",
)
_QUOTE_RE = re.compile(r"[\"“](.{20,900}?)[\"”]", re.DOTALL)
_TOPIC_RE = re.compile(
    r"\b(?:On|Regarding|Asked about|When asked about)\s+(.{3,180}?):\s*$",
    re.IGNORECASE,
)
_GENERIC_NAMES = {"tv info", "broadcasters", "highlights available", "news", "close"}


def _clean(value: object, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" .\t\r\n")
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip(" .;,|") + "…"


def _norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def is_fotmob_url(value: object) -> bool:
    try:
        host = (urlparse(str(value or "")).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return host == FOTMOB_DOMAIN or host.endswith("." + FOTMOB_DOMAIN)


def is_premier_league_press_item(item: Mapping[str, Any]) -> bool:
    """Accept only the approved FotMob Premier League news lane."""
    if str(item.get("feed_id") or "") == PRESS_FEED_ID:
        return str(item.get("source_id") or FOTMOB_SOURCE_ID) == FOTMOB_SOURCE_ID
    if str(item.get("source_id") or "") != FOTMOB_SOURCE_ID:
        return False
    return any(is_fotmob_url(item.get(key)) for key in ("source_url", "publisher_url"))


def _speaker_entries(text: str) -> list[dict[str, Any]]:
    matches = list(_SPEAKER_RE.finditer(text or ""))
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, match in enumerate(matches):
        name = _clean(match.group("name"), 90)
        club = _clean(match.group("club"), 70)
        parts = name.split()
        while len(parts) > 2 and parts[0].casefold() in {"tv", "info", "broadcasters"}:
            parts.pop(0)
        name = " ".join(parts)
        identity = (_norm(name), _norm(club))
        if _norm(name) in _GENERIC_NAMES or len(parts) < 2 or identity in seen:
            continue
        seen.add(identity)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.end():end]
        quotes = list(dict.fromkeys(_clean(m.group(1), 330) for m in _QUOTE_RE.finditer(section)))[:8]
        topics: list[str] = []
        cursor = 0
        for quote_match in _QUOTE_RE.finditer(section):
            topic_match = _TOPIC_RE.search(section[cursor:quote_match.start()])
            if topic_match:
                topic = _clean(topic_match.group(1), 130)
                if topic and topic not in topics:
                    topics.append(topic)
            cursor = quote_match.end()
        entries.append({"name": name, "club": club, "quotes": quotes, "topics": topics[:8]})
    return entries


def parse_premier_league_roundup(text: str) -> dict[str, Any]:
    source_text = re.sub(r"\s+", " ", str(text or "")).strip()
    entries = _speaker_entries(source_text)
    if not entries:
        return {"entries": [], "primary": None}
    roundup: list[str] = []
    latest_news: list[str] = []
    key_quotes: list[str] = []
    manager_notes: list[str] = []
    for entry in entries[:MAX_PREMIER_LEAGUE_ROUNDUP_ENTRIES]:
        name, club, quotes, topics = entry["name"], entry["club"], entry["quotes"], entry["topics"]
        if topics:
            latest_news.append(_clean(f"{club}: {topics[0]}", 180))
            manager_notes.append(_clean(f"{club}: {topics[0]}", 180))
        elif quotes:
            latest_news.append(_clean(f"{club}: {quotes[0]}", 180))
        if quotes:
            key_quotes.extend(_clean(f"{name}: {quote}", 300) for quote in quotes[:2])
            roundup.append(_clean(f"{club} — {name}: “{quotes[0]}”", 245))
        else:
            roundup.append(_clean(f"{club} — {name}: Press conference update", 180))
    primary = entries[0]
    return {
        "entries": entries[:MAX_PREMIER_LEAGUE_ROUNDUP_ENTRIES],
        "primary": {"name": primary["name"], "club": primary["club"], "quote_summary": (primary["quotes"] or [""])[0], "quote_topic": (primary["topics"] or [""])[0] or "Press conference update"},
        "latest_news": latest_news[:8], "key_quotes": key_quotes[:8],
        "manager_notes": manager_notes[:4], "roundup": roundup[:MAX_PREMIER_LEAGUE_ROUNDUP_ENTRIES],
    }


def project_roundup_story(story: dict[str, Any], source_item: Mapping[str, Any], *, resolve_staff: Optional[Callable[[str], Any]] = None, resolve_club_key: Optional[Callable[[str], Optional[str]]] = None) -> bool:
    if not is_premier_league_press_item(source_item):
        return False
    text = str(source_item.get("full_text") or source_item.get("text") or source_item.get("summary") or "")
    primary = parse_premier_league_roundup(text).get("primary")
    if not primary:
        return False
    speaker_name = primary["name"]
    if resolve_staff:
        resolved = resolve_staff(speaker_name)
        if resolved is not None:
            speaker_name = str(getattr(resolved, "name", speaker_name))
    club_name = primary["club"]
    story.update({
        "event": "press_conference", "player": speaker_name, "display_name": speaker_name,
        "speaker_type": "manager", "to_club": club_name,
        "to_key": resolve_club_key(club_name) if resolve_club_key else None,
        "quote_summary": primary["quote_summary"], "quote_topic": primary["quote_topic"],
        "latest_news": parse_premier_league_roundup(text).get("latest_news", []),
        "key_quotes": parse_premier_league_roundup(text).get("key_quotes", []),
        "manager_notes": parse_premier_league_roundup(text).get("manager_notes", []),
        "roundup": parse_premier_league_roundup(text).get("roundup", []),
        "_premier_league_press_roundup": True,
    })
    return True


def next_fpl_deadline(fpl_data: Mapping[str, Any], *, now=None):
    return _shared_next_fpl_deadline(fpl_data, now=now)


def press_deadline_target(fpl_data: Mapping[str, Any], *, now=None, margin_minutes: int = PRESS_DEADLINE_MARGIN_MINUTES):
    return deadline_target(fpl_data, now=now, margin_minutes=margin_minutes)


def press_deadline_window_open(fpl_data: Mapping[str, Any], *, now=None, margin_minutes: int = PRESS_DEADLINE_MARGIN_MINUTES, window_minutes: int = 30) -> bool:
    return deadline_window_open(fpl_data, now=now, margin_minutes=margin_minutes, window_minutes=window_minutes)
