"""Premier League official press-conference roundup helpers.

This module is deliberately limited to the official PremierLeague.com press
roundup lane. It does not change the player-card renderer or any other news
category. The source article is treated as one combined roundup so the existing
approved three-column PRESS CONFERENCE graphic can display all of the article's
manager updates in one post.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlparse

PREMIER_LEAGUE_SOURCE_ID = "official.premier_league"
PREMIER_LEAGUE_DOMAIN = "premierleague.com"
PRESS_FEED_ID = "google.premier_league.press_conference"
PRESS_DEADLINE_MARGIN_MINUTES = 30

# PremierLeague.com roundup pages use headings such as:
#   Mikel Arteta (Arsenal)
#   Nuno Espirito Santo (Nottingham Forest)
# Keep this intentionally shape-based; the closed-world entity registry still
# decides whether the primary speaker is a real registered manager.
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
_GENERIC_NAMES = {
    "tv info",
    "broadcasters",
    "highlights available",
    "news",
    "close",
}


def _clean(value: object, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" .\t\r\n")
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip(" .;,|") + "…"


def _norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def is_premier_league_url(value: object) -> bool:
    """Return true only for the Premier League's own web domain."""
    try:
        host = (urlparse(str(value or "")).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return host == PREMIER_LEAGUE_DOMAIN or host.endswith("." + PREMIER_LEAGUE_DOMAIN)


def is_premier_league_press_item(item: Mapping[str, Any]) -> bool:
    """Identify the dedicated official feed/article lane without trusting text."""
    if str(item.get("feed_id") or "") == PRESS_FEED_ID:
        return True
    if str(item.get("source_id") or "") == PREMIER_LEAGUE_SOURCE_ID:
        return True
    return any(
        is_premier_league_url(item.get(key))
        for key in ("source_url", "publisher_url")
    )


def _speaker_entries(text: str) -> list[dict[str, Any]]:
    matches = list(_SPEAKER_RE.finditer(text or ""))
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, match in enumerate(matches):
        name = _clean(match.group("name"), 90)
        club = _clean(match.group("club"), 70)
        # The official page places a small "TV Info - Broadcasters" label
        # immediately before the first manager heading in its article text.
        # Strip only those known layout words; never guess or rewrite a real
        # person's name.
        name_parts = name.split()
        while (
            len(name_parts) > 2
            and name_parts[0].casefold() in {"tv", "info", "broadcasters"}
        ):
            name_parts.pop(0)
        name = " ".join(name_parts)
        if _norm(name) in _GENERIC_NAMES or len(name.split()) < 2:
            continue
        identity = (_norm(name), _norm(club))
        if identity in seen:
            continue
        seen.add(identity)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.end():end]
        quotes = [_clean(m.group(1), 330) for m in _QUOTE_RE.finditer(section)]
        quotes = list(dict.fromkeys(q for q in quotes if q))[:8]
        topics: list[str] = []
        cursor = 0
        for quote_match in _QUOTE_RE.finditer(section):
            prefix = section[cursor:quote_match.start()]
            topic_match = _TOPIC_RE.search(prefix)
            if topic_match:
                topic = _clean(topic_match.group(1), 130)
                if topic and topic not in topics:
                    topics.append(topic)
            cursor = quote_match.end()
        entries.append({
            "name": name,
            "club": club,
            "quotes": quotes,
            "topics": topics[:8],
        })
    return entries


def parse_premier_league_roundup(text: str) -> dict[str, Any]:
    """Extract only text present in an official PremierLeague.com article.

    The parser never invents a quote. If a page has no speaker heading and no
    quoted material, it returns an empty result and the normal verification
    pipeline remains fail-closed.
    """
    source_text = re.sub(r"\s+", " ", str(text or "")).strip()
    entries = _speaker_entries(source_text)
    if not entries:
        return {"entries": [], "primary": None}

    roundup: list[str] = []
    latest_news: list[str] = []
    key_quotes: list[str] = []
    manager_notes: list[str] = []
    for entry in entries[:18]:
        name = entry["name"]
        club = entry["club"]
        quotes = entry["quotes"]
        topics = entry["topics"]
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
    first_quote = (primary["quotes"] or [""])[0]
    first_topic = (primary["topics"] or [""])[0]
    return {
        "entries": entries[:18],
        "primary": {
            "name": primary["name"],
            "club": primary["club"],
            "quote_summary": first_quote,
            "quote_topic": first_topic or "Official press conference update",
        },
        # These lists map directly to fields already supported by the approved
        # renderer. They are facts extracted from the official article body.
        "latest_news": latest_news[:8],
        "key_quotes": key_quotes[:8],
        "manager_notes": manager_notes[:4],
        "roundup": roundup[:18],
    }


def project_roundup_story(
    story: dict[str, Any],
    source_item: Mapping[str, Any],
    *,
    resolve_staff: Optional[Callable[[str], Any]] = None,
    resolve_club_key: Optional[Callable[[str], Optional[str]]] = None,
) -> bool:
    """Put one combined official roundup into the existing V2 story envelope."""
    text = str(
        source_item.get("full_text")
        or source_item.get("text")
        or source_item.get("summary")
        or ""
    )
    parsed = parse_premier_league_roundup(text)
    primary = parsed.get("primary")
    if not primary:
        return False

    speaker_name = primary["name"]
    if resolve_staff:
        # Current managers may already exist in the provider snapshot. If they
        # do not, keep the exact official heading and let the existing
        # first-party entity-establishment rule validate it; do not discard a
        # genuine PremierLeague.com roundup merely because the snapshot lags.
        resolved = resolve_staff(speaker_name)
        if resolved is not None:
            speaker_name = str(getattr(resolved, "name", speaker_name))

    club_name = primary["club"]
    club_key = resolve_club_key(club_name) if resolve_club_key else None
    story.update({
        "event": "press_conference",
        "player": speaker_name,
        "display_name": speaker_name,
        "speaker_type": "manager",
        "to_club": club_name,
        "to_key": club_key,
        "quote_summary": primary["quote_summary"],
        "quote_topic": primary["quote_topic"],
        "latest_news": parsed.get("latest_news", []),
        "key_quotes": parsed.get("key_quotes", []),
        "manager_notes": parsed.get("manager_notes", []),
        "roundup": parsed.get("roundup", []),
        "_premier_league_press_roundup": True,
    })
    return True


def next_fpl_deadline(
    fpl_data: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """Return the next official FPL deadline from bootstrap-static."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
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


def press_deadline_target(
    fpl_data: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    margin_minutes: int = PRESS_DEADLINE_MARGIN_MINUTES,
) -> Optional[datetime]:
    deadline = next_fpl_deadline(fpl_data, now=now)
    return deadline - timedelta(minutes=margin_minutes) if deadline else None


def press_deadline_window_open(
    fpl_data: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    margin_minutes: int = PRESS_DEADLINE_MARGIN_MINUTES,
    window_minutes: int = 20,
) -> bool:
    """Check whether the target pre-deadline posting window is open."""
    now = now or datetime.now(timezone.utc)
    target = press_deadline_target(fpl_data, now=now, margin_minutes=margin_minutes)
    if target is None:
        return False
    deadline = target + timedelta(minutes=margin_minutes)
    return target <= now < min(deadline, target + timedelta(minutes=window_minutes))
