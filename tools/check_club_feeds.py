#!/usr/bin/env python3
"""Probe official club websites for a working news RSS/Atom feed.

WHY THIS EXISTS
---------------
config/sources.json already carries 25 OFFICIAL_CLUB source profiles, each with
its domain and a high reliability prior, and config/verification.json already
sets ``official_first_party_sufficient: true`` — a club announcing its own news
needs no second source. But config/feeds.json contains no club feeds at all, so
none of that trust has anything to act on. Every "official" item the bot sees
today arrives indirectly, via a Google News site-search.

Club RSS paths are not standardised and are not documented anywhere central, so
the only honest way to build the feed list is to ask each site. Guessing 25 URLs
and committing them would give the bot 25 failing feeds — the failure mode this
script exists to avoid.

Run it where the network is open (GitHub Actions), read the report, and wire in
only what actually answered.

    python tools/check_club_feeds.py                 # probe every club
    python tools/check_club_feeds.py --club arsenal  # probe one
    python tools/check_club_feeds.py --json out.json # machine-readable report

Exit code is always 0: an unreachable club site is information, not a build
failure.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SOURCES = Path("config/sources.json")
FEEDS = Path("config/feeds.json")

# Paths clubs actually use. Ordered most-common first so the usual case costs one
# request. Kept generic on purpose — a per-club hardcoded list would rot the
# moment a club redesigns its site, and this script exists to notice that.
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
    "/api/incrowd/getnewlistinformation?count=20",  # common club CMS feed
)

UA = "Mozilla/5.0 (compatible; FPLVortexFeedCheck/1.0; +https://github.com/)"
TIMEOUT = 12


def official_clubs(sources_path: Path = SOURCES) -> list[dict]:
    """Every OFFICIAL_CLUB profile, so the probe list is derived from the trust
    configuration rather than maintained separately and allowed to drift."""
    data = json.loads(sources_path.read_text(encoding="utf-8"))
    return [p for p in data.get("sources", []) if p.get("kind") == "OFFICIAL_CLUB"]


def _fetch(url: str) -> tuple[int, bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status, resp.read(200_000), resp.headers.get("Content-Type", "")


def _looks_like_feed(body: bytes, content_type: str) -> bool:
    """Cheap structural check before paying for a full parse."""
    head = body[:2000].lower()
    if b"<rss" in head or b"<feed" in head or b"<rdf:rdf" in head:
        return True
    return "xml" in (content_type or "").lower() and b"<item" in body[:20_000].lower()


def _parse_entries(body: bytes) -> tuple[int, str | None]:
    """Return (entry_count, newest_entry_iso). feedparser is already a dependency."""
    try:
        import feedparser
    except ImportError:  # pragma: no cover - feedparser is in requirements.txt
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


def probe_club(profile: dict) -> dict:
    """Try each candidate path against the club's domain; stop at the first that
    is a real, non-empty, dated feed."""
    club_id = profile["id"]
    domains = profile.get("domains") or []
    attempts: list[dict] = []

    for domain in domains:
        for path in CANDIDATE_PATHS:
            url = f"https://www.{domain}{path}" if not domain.startswith("www.") else f"https://{domain}{path}"
            record = {"url": url}
            try:
                status, body, ctype = _fetch(url)
                record["status"] = status
                if status == 200 and _looks_like_feed(body, ctype):
                    count, newest = _parse_entries(body)
                    record.update(entries=count, newest=newest)
                    # A feed with no dated entries cannot be freshness-checked,
                    # and the bot rejects undated items anyway — so it is no use.
                    if count > 0 and newest:
                        attempts.append(record)
                        return {
                            "club": club_id, "ok": True, "feed_url": url,
                            "entries": count, "newest": newest, "attempts": attempts,
                        }
                    record["reason"] = "feed parsed but has no dated entries"
                elif status == 200:
                    record["reason"] = "200 but body is not a feed (probably HTML)"
            except urllib.error.HTTPError as exc:
                record.update(status=exc.code, reason="http error")
            except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                record.update(status=None, reason=f"unreachable: {str(exc)[:60]}")
            except Exception as exc:  # noqa: BLE001 - a probe must never crash the report
                record.update(status=None, reason=f"{type(exc).__name__}: {str(exc)[:60]}")
            attempts.append(record)

    return {"club": club_id, "ok": False, "feed_url": None, "attempts": attempts}


def feed_entry(result: dict, profile: dict) -> dict:
    """Build the config/feeds.json entry for a club that answered.

    source_hint is the club's existing profile id, so the feed inherits the
    OFFICIAL_CLUB trust already configured for it — that is what lets a club
    announcement publish without a second source.
    """
    return {
        "id": f"official.{result['club'].replace('club.', '')}",
        "url": result["feed_url"],
        "transport": "DIRECT_RSS",
        "source_hint": profile["id"],
        "declared_sport": "football",
        "max_entries": 15,
    }


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--club", help="probe only clubs whose id contains this string")
    ap.add_argument("--json", dest="json_out", help="write the full report here")
    args = ap.parse_args(list(argv) if argv is not None else None)

    profiles = official_clubs()
    if args.club:
        needle = args.club.lower()
        profiles = [p for p in profiles if needle in p["id"].lower()]
    if not profiles:
        print("No matching OFFICIAL_CLUB profiles in config/sources.json.")
        return 0

    print(f"Probing {len(profiles)} official club site(s) for a news feed…\n")
    results, ready = [], []
    for profile in profiles:
        result = probe_club(profile)
        results.append(result)
        if result["ok"]:
            ready.append(feed_entry(result, profile))
            print(f"  OK    {result['club']:34s} {result['entries']:3d} entries, "
                  f"newest {result['newest'][:16]}\n        {result['feed_url']}")
        else:
            tried = len(result["attempts"])
            last = (result["attempts"][-1].get("reason") if result["attempts"] else "no domains")
            print(f"  MISS  {result['club']:34s} {tried} path(s) tried — {last}")

    ok = sum(1 for r in results if r["ok"])
    print(f"\n{ok}/{len(results)} club(s) have a usable feed.")

    if ready:
        print("\nAdd these to config/feeds.json under \"feeds\":\n")
        print(json.dumps(ready, indent=2))

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "clubs_probed": len(results),
        "clubs_ok": ok,
        "ready_feeds": ready,
        "results": results,
    }
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nFull report written to {args.json_out}")

    # Always 0 — a club site being down is information, not a build failure.
    return 0


if __name__ == "__main__":
    sys.exit(main())
