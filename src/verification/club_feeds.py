"""Resolve official club news feeds automatically, and keep them working.

WHY
---
config/sources.json carries an OFFICIAL_CLUB profile for every Premier League
club, each with its domain and a ~0.99 reliability prior, and
config/verification.json sets ``official_first_party_sufficient: true`` — a club
announcing its own news needs no second source. But config/feeds.json listed no
club feeds, so that trust had nothing to act on and every "official" item
reached the bot second-hand through a Google News site-search.

Club RSS paths are not standardised, are documented nowhere central, and change
without notice when a club redesigns its site. A hand-maintained list of 25 URLs
would be wrong the week it was written and quietly rot afterwards. So the list
is discovered instead: the bot asks each club's site where its feed is, caches
the answer, and re-checks it periodically.

HOW IT STAYS CHEAP
------------------
Probing 25 clubs across every candidate path would cost hundreds of requests and
minutes of runtime. Instead each run resolves at most ``probe_budget`` clubs —
the ones never tried, then the ones checked longest ago. The registry fills over
the first few runs and then only refreshes what has gone stale, so the steady
state is roughly zero extra requests per run.

The cache is committed alongside the other bot state, so a fresh GitHub Actions
runner starts with the feeds already known.

FAILURE POSTURE
---------------
A club that cannot be resolved is recorded with its reason and retried later. It
never raises, and it never blocks the run: fewer sources is a smaller bot, not a
broken one.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

CACHE_PATH = Path("data/club_feeds.json")

# Paths clubs actually use, most common first so the usual case costs one
# request. Deliberately generic — a per-club hardcoded list would defeat the
# point of discovering them.
CANDIDATE_PATHS = (
    "/rss",
    "/rss.xml",
    "/feed",
    "/feed.xml",
    "/news/rss",
    "/news/rss.xml",
    "/news/feed",
    "/rss-feed",
    "/feeds/news.xml",
)

UA = "Mozilla/5.0 (compatible; FPLVortexBot/2.0; +https://github.com/)"
TIMEOUT = 10

# How long a resolved feed is trusted before it is re-checked, and how long a
# failed club waits before being retried. Failures retry sooner because a club
# site being briefly down is common and cheap to recheck.
FRESH_FOR = timedelta(days=7)
RETRY_FAILED_AFTER = timedelta(days=1)


@dataclass(frozen=True)
class ResolvedClubFeed:
    club_id: str
    url: str
    entries: int
    newest: Optional[str]
    checked_at: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def official_club_profiles(sources: Any) -> list[Any]:
    """Every OFFICIAL_CLUB profile from the live source registry.

    Derived from the trust configuration rather than a second list, so the two
    cannot drift apart.
    """
    out = []
    for profile in sources.all():
        if getattr(profile, "kind", "") == "OFFICIAL_CLUB" and getattr(profile, "domains", None):
            out.append(profile)
    return out


def _fetch(url: str) -> tuple[int, bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status, resp.read(200_000), resp.headers.get("Content-Type", "")


def _looks_like_feed(body: bytes, content_type: str) -> bool:
    head = body[:2000].lower()
    if b"<rss" in head or b"<feed" in head or b"<rdf:rdf" in head:
        return True
    return "xml" in (content_type or "").lower() and b"<item" in body[:20_000].lower()


def _dated_entries(body: bytes) -> tuple[int, Optional[str]]:
    """(count, newest_iso). A feed with no dated entries is unusable: it cannot
    be freshness-checked and the pipeline rejects undated items anyway."""
    try:
        import feedparser
    except ImportError:
        return 0, None
    parsed = feedparser.parse(body)
    entries = parsed.get("entries") or []
    newest = None
    for entry in entries:
        struct = entry.get("published_parsed") or entry.get("updated_parsed")
        if not struct:
            continue
        stamp = datetime(*struct[:6], tzinfo=timezone.utc)
        if newest is None or stamp > newest:
            newest = stamp
    return len(entries), newest.isoformat() if newest else None


def probe_club(profile: Any) -> dict:
    """Find this club's feed, or record why not. Never raises."""
    club_id = profile.id
    for domain in (getattr(profile, "domains", None) or []):
        base = domain if domain.startswith("www.") else f"www.{domain}"
        for path in CANDIDATE_PATHS:
            url = f"https://{base}{path}"
            try:
                status, body, ctype = _fetch(url)
            except (urllib.error.HTTPError, urllib.error.URLError,
                    socket.timeout, TimeoutError):
                continue
            except Exception:  # noqa: BLE001 — discovery must never break a run
                continue
            if status != 200 or not _looks_like_feed(body, ctype):
                continue
            count, newest = _dated_entries(body)
            if count > 0 and newest:
                return {
                    "club_id": club_id, "ok": True, "url": url,
                    "entries": count, "newest": newest,
                    "checked_at": _now().isoformat(),
                }
    return {
        "club_id": club_id, "ok": False, "url": None,
        "reason": "no candidate path returned a dated feed",
        "checked_at": _now().isoformat(),
    }


def load_cache(path: Path = CACHE_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "clubs": {}}
    if not isinstance(data.get("clubs"), dict):
        return {"schema_version": 1, "clubs": {}}
    return data


def save_cache(cache: dict, path: Path = CACHE_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # An unwritable cache costs a re-probe next run; it is not fatal.


def _needs_probe(entry: Optional[dict], now: datetime) -> bool:
    if not entry:
        return True
    checked = _parse_ts(entry.get("checked_at"))
    if checked is None:
        return True
    age = now - checked
    return age > (FRESH_FOR if entry.get("ok") else RETRY_FAILED_AFTER)


def resolve_club_feeds(
    sources: Any,
    *,
    cache_path: Path = CACHE_PATH,
    probe_budget: int = 4,
    now: Optional[datetime] = None,
) -> list[ResolvedClubFeed]:
    """Return every known-good club feed, refreshing a few entries per run.

    ``probe_budget`` bounds the network cost: the clubs never tried come first,
    then the ones checked longest ago. With 25 clubs and a budget of 4, the
    registry is complete within about seven runs and afterwards only refreshes
    what has expired.
    """
    now = now or _now()
    cache = load_cache(cache_path)
    clubs = cache.setdefault("clubs", {})
    profiles = official_club_profiles(sources)

    stale = [p for p in profiles if _needs_probe(clubs.get(p.id), now)]
    # Never-tried clubs first, then oldest check first, so coverage grows fast
    # and no club is starved.
    stale.sort(key=lambda p: (
        clubs.get(p.id) is not None,
        (_parse_ts((clubs.get(p.id) or {}).get("checked_at")) or datetime.min.replace(tzinfo=timezone.utc)),
    ))

    probed = 0
    for profile in stale:
        if probed >= probe_budget:
            break
        clubs[profile.id] = probe_club(profile)
        probed += 1

    if probed:
        cache["updated_at"] = now.isoformat()
        save_cache(cache, cache_path)

    resolved = []
    for club_id, entry in sorted(clubs.items()):
        if entry.get("ok") and entry.get("url"):
            resolved.append(ResolvedClubFeed(
                club_id=club_id,
                url=entry["url"],
                entries=int(entry.get("entries") or 0),
                newest=entry.get("newest"),
                checked_at=entry.get("checked_at") or "",
            ))
    return resolved


def as_feed_definitions(resolved: Iterable[ResolvedClubFeed]) -> list[Any]:
    """Wrap resolved feeds as FeedDefinitions the ingestion layer can read.

    ``source_hint`` is the club's own profile id, so each feed inherits the
    OFFICIAL_CLUB trust already configured for it — that is what lets a club
    announcement publish without waiting for a second source.
    """
    from .documents import FeedDefinition

    return [
        FeedDefinition(
            id=f"official.{feed.club_id.replace('club.', '')}",
            url=feed.url,
            transport="DIRECT_RSS",
            source_hint=feed.club_id,
            declared_sport="football",
            max_entries=15,
        )
        for feed in resolved
    ]


def summary(resolved: list[ResolvedClubFeed], sources: Any) -> str:
    total = len(official_club_profiles(sources))
    return f"{len(resolved)}/{total} official club feeds resolved"
