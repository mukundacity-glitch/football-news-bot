"""Candidate evidence collector for verification engine v2.

This module finds potentially relevant documents.  It does **not** confirm a
story.  Every returned document is independently re-extracted and evaluated by
the fail-closed verification engine, which compares canonical facts.
"""

from __future__ import annotations

import calendar
import re
import time
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Sequence

import feedparser
import requests

from src.verification.entities import EntityRegistry
from src.verification.source_registry import SourceRegistry


GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=en-GB&gl=GB&ceid=GB:en"
_UA = {"User-Agent": "Mozilla/5.0 (compatible; FPLVortexBot/2.0; +https://x.com/FPLVortex)"}
HTTP_TIMEOUT_S = 12
MAX_AGE_DAYS = 3
MAX_ENTRIES_PER_QUERY = 25
_GENERIC_CLUB_WORDS = {
    "united", "city", "town", "club", "albion", "hotspur", "forest",
    "the", "and", "real", "athletic",
}
_EVENT_CANDIDATE_TERMS = {
    "injury": ("injur", "ruled out", "out for", "scan", "surgery", "fitness", "sidelined", "return"),
    "suspension": ("suspend", "ban", "red card", "sent off"),
    "manager": ("manager", "head coach", "appoint", "sack", "in charge"),
    "renewal": ("contract", "new deal", "extend", "renew", "stay"),
    "stay": ("contract", "new deal", "extend", "renew", "stay", "remain"),
}
_TRANSFER_CANDIDATE_TERMS = (
    "transfer", "sign", "deal", "join", "move", "bid", "fee", "medical",
    "agree", "loan", "talks", "switch", "exit",
)
_CONTRADICTION_SIGNALS = (
    "bid rejected", "rejected bid", "not for sale", "deal collapsed",
    "falls through", "fell through", "pulled out", "no deal", "deal off",
    "failed to agree", "stays at", "remains at", "extends contract",
)


def _norm_handle(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _surname(name: str) -> str:
    parts = [p for p in re.split(r"[\s\-']+", (name or "").strip()) if p]
    if not parts:
        return ""
    return parts[-1].lower() if len(parts[-1]) >= 3 else " ".join(parts).lower()


def _club_tokens(story: Dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for field in ("to_club", "from_club", "to_key", "from_key"):
        for token in re.split(r"[\s_\-]+", str(story.get(field) or "").lower()):
            if len(token) >= 4 and token not in _GENERIC_CLUB_WORDS:
                tokens.add(token)
    return tokens


def matches_story(text: str, story: Dict[str, Any]) -> bool:
    """Broad candidate-retrieval match, never a verification decision."""
    lowered = " " + (text or "").lower() + " "
    surname = _surname(story.get("player"))
    if not surname or not re.search(r"(?<![a-z])" + re.escape(surname) + r"(?![a-z])", lowered):
        return False
    if any(re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", lowered) for t in _club_tokens(story)):
        return True
    terms = _EVENT_CANDIDATE_TERMS.get(story.get("event"), _TRANSFER_CANDIDATE_TERMS)
    return any(term in lowered for term in terms)


def _contradicts_story(text: str, story: Dict[str, Any]) -> bool:
    lowered = " " + (text or "").lower() + " "
    surname = _surname(story.get("player"))
    if not surname or not re.search(r"(?<![a-z])" + re.escape(surname) + r"(?![a-z])", lowered):
        return False
    return any(signal in lowered for signal in _CONTRADICTION_SIGNALS)


def _too_old(entry: Any) -> bool:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return False  # v2 temporal gate will refuse authoritative unknown dates
    return (time.time() - calendar.timegm(parsed)) > MAX_AGE_DAYS * 86400


def _entry_publisher_url(entry: Any) -> str:
    source = entry.get("source") or {}
    return (
        source.get("href", "") if isinstance(source, dict)
        else getattr(source, "href", "")
    ) or ""


def _entry_publisher_name(entry: Any) -> str:
    source = entry.get("source") or {}
    return (
        source.get("title", "") if isinstance(source, dict)
        else getattr(source, "title", "")
    ) or ""


def _google_news(query: str, log: List[str]) -> List[Any]:
    url = GOOGLE_NEWS_RSS.format(q=urllib.parse.quote(query))
    try:
        response = requests.get(url, headers=_UA, timeout=HTTP_TIMEOUT_S)
        response.raise_for_status()
    except Exception as exc:
        log.append(f"Google News unavailable for {query!r}: {exc}")
        return []
    return list(feedparser.parse(response.content).entries or [])[:MAX_ENTRIES_PER_QUERY]


def _entry_evidence(entry: Any, profile_id: str) -> Dict[str, Any]:
    return {
        "title": entry.get("title", ""),
        "summary": re.sub(r"<[^>]+>", " ", entry.get("summary", "") or "").strip(),
        "source_url": entry.get("link"),
        "publisher_url": _entry_publisher_url(entry),
        "publisher_name": _entry_publisher_name(entry),
        "source_id": profile_id,
        "source_hint": None,
        "transport": "GOOGLE_NEWS",
        "configured_direct_feed": False,
        "declared_sport": None,
        "created_at": entry.get("published") or entry.get("updated"),
        "feed_id": "cross_verifier.google_news",
    }


def _official_profiles_for_story(
    story: Dict[str, Any],
    source_registry: SourceRegistry,
    entity_registry: Optional[EntityRegistry],
) -> List[Any]:
    club_ids = set()
    if entity_registry:
        for field in ("to_key", "to_club", "from_key", "from_club"):
            value = story.get(field)
            if value and (record := entity_registry.resolve_club(str(value).replace("_", " "))):
                club_ids.add(record.id)
    profiles = []
    for profile in source_registry.all():
        if not profile.is_official or not profile.domains:
            continue
        if club_ids & set(profile.official_for):
            profiles.append(profile)
        elif "premier_league_ecosystem" in profile.official_scope and club_ids:
            profiles.append(profile)
    return profiles


# ── Cross-verification read health ──────────────────────────────────────
# The X searches below are the bot's only route to journalist corroboration.
# When every one of them fails, stories stall at PENDING with no visible cause:
# the run summary reports RSS/Bluesky feed health, which covers a completely
# different set of sources and stays green throughout. That gap once hid a total
# X outage for 20 hours behind the message "sources read OK".
#
# Counting attempts here lets the summary tell the truth. Per-process and reset
# at the start of each run, because the bot is one run per process.
_X_SEARCH_STATS: Dict[str, Any] = {"attempted": 0, "failed": 0, "errors": []}


def reset_x_search_health() -> None:
    _X_SEARCH_STATS.update({"attempted": 0, "failed": 0, "errors": []})


def x_search_health() -> Dict[str, Any]:
    """Attempted/failed X searches this run, plus the distinct errors seen.

    ``fail_ratio`` is None when nothing was attempted — that is "no data", not
    "all healthy", and the caller must not read it as success.
    """
    attempted = _X_SEARCH_STATS["attempted"]
    return {
        "attempted": attempted,
        "failed": _X_SEARCH_STATS["failed"],
        "fail_ratio": (_X_SEARCH_STATS["failed"] / attempted) if attempted else None,
        "errors": list(_X_SEARCH_STATS["errors"]),
    }


async def _x_journalist_evidence(
    read_client: Any,
    story: Dict[str, Any],
    source_registry: SourceRegistry,
    log: List[str],
) -> List[Dict[str, Any]]:
    if read_client is None:
        return []
    surname = _surname(story.get("player"))
    if not surname:
        return []
    evidence = []
    for profile in (p for p in source_registry.all() if p.search_enabled):
        if not profile.handles:
            continue
        handle = profile.handles[0]
        _X_SEARCH_STATS["attempted"] += 1
        try:
            results = await read_client.search_tweet(f"from:{handle} {surname}", "Latest")
        except Exception as exc:
            _X_SEARCH_STATS["failed"] += 1
            detail = f"{type(exc).__name__}: {str(exc)[:120]}"
            if detail not in _X_SEARCH_STATS["errors"]:
                _X_SEARCH_STATS["errors"].append(detail)
            log.append(f"X @{handle}: search failed ({exc})")
            continue
        for tweet in list(results or [])[:10]:
            text = getattr(tweet, "text", "") or getattr(tweet, "full_text", "") or ""
            if not matches_story(text, story):
                continue
            tweet_id = getattr(tweet, "id", None)
            evidence.append({
                "title": text,
                "summary": "",
                "source_url": f"https://x.com/{handle}/status/{tweet_id}" if tweet_id else None,
                "source_handle": handle,
                "source_id": profile.id,
                "transport": "X",
                "configured_direct_feed": False,
                "declared_sport": "football" if "football" in profile.declared_sports else None,
                "created_at": str(getattr(tweet, "created_at", "") or "") or None,
                "feed_id": "cross_verifier.x",
            })
            log.append(f"X @{handle}: candidate evidence found")
            break
    return evidence


async def cross_verify(
    story: Dict[str, Any],
    known_sources: Sequence[str] = (),
    read_client: Any = None,
    *,
    source_registry: Optional[SourceRegistry] = None,
    entity_registry: Optional[EntityRegistry] = None,
) -> Dict[str, Any]:
    """Collect independently attributable documents for a candidate story."""
    source_registry = source_registry or SourceRegistry.load()
    log: List[str] = []
    evidence: List[Dict[str, Any]] = []
    handles: List[str] = []
    known = {_norm_handle(source) for source in known_sources}
    groups = set()
    contradiction_groups = set()
    official_confirmed = False
    player = str(story.get("player") or "").strip()
    if not player:
        return {
            "handles": [], "evidence": [], "official_confirmed": False,
            "n_independent": 0, "contradicted": False,
            "n_contradictions": 0, "log": ["no subject to search"],
        }

    def accept_entry(entry: Any) -> None:
        nonlocal official_confirmed
        publisher_url = _entry_publisher_url(entry)
        profile = source_registry.profile_for_domain(publisher_url)
        if not profile:
            return
        title = entry.get("title", "")
        if _contradicts_story(title, story):
            contradiction_groups.add(profile.publisher_group)
            log.append(f"candidate contradiction ({profile.display_name}): {title[:90]}")
            return
        if not matches_story(title, story):
            return
        if profile.publisher_group in groups:
            return
        groups.add(profile.publisher_group)
        evidence.append(_entry_evidence(entry, profile.id))
        handle = profile.handles[0] if profile.handles else profile.id
        if _norm_handle(handle) not in known:
            known.add(_norm_handle(handle))
            handles.append(handle)
        official_confirmed = official_confirmed or profile.is_official
        log.append(f"candidate evidence ({profile.display_name}): {title[:90]}")

    for entry in _google_news(f'"{player}" football when:{MAX_AGE_DAYS}d', log):
        if not _too_old(entry):
            accept_entry(entry)

    # Search only domains configured as official for a club in this claim. The
    # final engine still requires exact fact extraction and source relationship.
    if not official_confirmed:
        surname = _surname(player)
        for profile in _official_profiles_for_story(story, source_registry, entity_registry):
            for domain in profile.domains:
                for entry in _google_news(f"site:{domain} {surname} when:{MAX_AGE_DAYS}d", log):
                    if not _too_old(entry):
                        accept_entry(entry)
                if official_confirmed:
                    break
            if official_confirmed:
                break

    x_evidence = await _x_journalist_evidence(read_client, story, source_registry, log)
    for item in x_evidence:
        profile = source_registry.get(item["source_id"])
        if profile and profile.publisher_group not in groups:
            groups.add(profile.publisher_group)
            evidence.append(item)
            handle = profile.handles[0] if profile.handles else profile.id
            if _norm_handle(handle) not in known:
                handles.append(handle)

    return {
        "handles": handles,
        "evidence": evidence,
        "official_confirmed": official_confirmed,
        "n_independent": len(groups),
        "contradicted": len(contradiction_groups) >= 2,
        "n_contradictions": len(contradiction_groups),
        "log": log,
    }
