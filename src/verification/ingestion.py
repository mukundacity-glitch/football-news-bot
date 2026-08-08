"""Config-driven RSS and Bluesky ingestion with publisher provenance metadata."""

from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import feedparser
import requests

from .documents import FeedDefinition, FeedRegistry
from .runtime import VerificationRuntime


_TAGS = re.compile(r"<[^>]+>")


def _clean_html(value: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub(" ", html.unescape(value or ""))).strip()


def _publisher(entry: Any) -> tuple[str, str]:
    source = entry.get("source") or {}
    if isinstance(source, dict):
        return source.get("href", "") or "", source.get("title", "") or ""
    return getattr(source, "href", "") or "", getattr(source, "title", "") or ""


def _legacy_source_name(runtime: VerificationRuntime, identity: Any) -> str:
    profile = runtime.sources.get(identity.profile_id)
    if profile:
        return profile.handles[0] if profile.handles else profile.id
    return identity.profile_id


def _all_feed_definitions(runtime: VerificationRuntime) -> List[FeedDefinition]:
    feeds = list(runtime.feeds.feeds)
    cfg = runtime.feeds.official_discovery
    if not cfg.get("enabled"):
        return feeds
    domains = sorted({
        domain
        for profile in runtime.sources.all()
        if profile.kind.value == "OFFICIAL_CLUB"
        for domain in profile.domains
    })
    chunk_size = max(1, int(cfg["domains_per_query"]))
    terms = " OR ".join(f'"{term}"' if " " in term else term for term in cfg["query_terms"])
    lookback = int(cfg["lookback_days"])
    for index in range(0, len(domains), chunk_size):
        chunk = domains[index:index + chunk_size]
        sites = " OR ".join(f"site:{domain}" for domain in chunk)
        query = f"({sites}) ({terms}) when:{lookback}d"
        url = (
            "https://news.google.com/rss/search?q="
            + urllib.parse.quote_plus(query)
            + "&hl=en-GB&gl=GB&ceid=GB:en"
        )
        feeds.append(FeedDefinition(
            id=f"official-clubs.discovery.{index // chunk_size + 1}",
            url=url,
            transport="GOOGLE_NEWS",
            source_hint=None,
            declared_sport="football",
            max_entries=int(cfg["max_entries"]),
        ))
    return feeds


def _fotmob_transfer_text(row: Dict[str, Any]) -> str:
    name = str(row.get("name") or "").strip()
    from_club = str(row.get("fromClubFullName") or row.get("fromClub") or "").strip()
    to_club = str(row.get("toClubFullName") or row.get("toClub") or "").strip()
    transfer_type = (row.get("transferType") or {}).get("text") or ""
    fee = row.get("fee") or {}
    fee_text = str(fee.get("feeText") or "").strip()
    until = str(row.get("toDate") or "").strip()
    if row.get("onLoan"):
        lead = f"{name} has joined {to_club} from {from_club} on loan."
    elif fee_text and "free" in fee_text.lower():
        lead = f"{name} has joined {to_club} from {from_club} on a free transfer."
    else:
        lead = f"{name} has joined {to_club} from {from_club}."
    bits = [lead, "FotMob listed the transfer as completed."]
    if fee_text:
        bits.append(f"Fee: {fee_text}.")
    if until:
        bits.append(f"Contract until {until[:10]}.")
    return " ".join(bits)


def _fotmob_legacy_story(row: Dict[str, Any]) -> Dict[str, Any]:
    fee = row.get("fee") or {}
    fee_text = str(fee.get("feeText") or "").strip()
    if not fee_text and fee.get("value"):
        try:
            fee_text = f"€{int(fee['value']):,}"
        except Exception:
            fee_text = str(fee.get("value"))
    transfer_kind = "loan" if row.get("onLoan") else "free" if "free" in fee_text.lower() else "permanent"
    event = "loan" if row.get("onLoan") else "transfer"
    player_name = str(row.get("name") or "").strip()
    from_club = str(row.get("fromClubFullName") or row.get("fromClub") or "").strip()
    to_club = str(row.get("toClubFullName") or row.get("toClub") or "").strip()
    return {
        "player": player_name,
        "event": event,
        "from_club": from_club,
        "to_club": to_club,
        "_structured_fotmob_transfer": True,
        "_structured_transfer_group": f"{player_name}|{from_club}|{to_club}",
        "fee": fee_text or None,
        "contract": str(row.get("toDate") or "").strip()[:10] or None,
        "transfer_kind": transfer_kind,
        "stage": 4,
        "collapsed": False,
        "historical": False,
        "headline": _fotmob_transfer_text(row),
        "raw_text": _fotmob_transfer_text(row),
        "sources": ["fotmob"],
    }


def _fetch_fotmob_transfers(
    runtime: VerificationRuntime,
    seen: set[str],
    fetched_at: str,
) -> tuple[List[Dict[str, Any]], int, List[Dict[str, str]]]:
    """Fetch FotMob Premier League transfer table as discovery input.

    FotMob is not allowed to invent a post by itself unless the downstream V2
    gates can ground player + clubs + PL relevance and the engine accepts the
    structured FotMob transfer source. This only adds candidate coverage for the
    transfer table the user sees in the app.
    """
    feed_id = "fotmob.premier_league.transfers"
    url = "https://www.fotmob.com/leagues/47/transfers/premier-league?season=2026%2F2027"
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FPLVortexBot/2.0)"},
            timeout=float(runtime.config.collection_config["feed_timeout_seconds"]),
        )
        response.raise_for_status()
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            response.text,
            re.S,
        )
        if not match:
            raise RuntimeError("FotMob page missing __NEXT_DATA__")
        payload = json.loads(html.unescape(match.group(1)))
        transfers = (
            payload.get("props", {})
            .get("pageProps", {})
            .get("transfers", {})
            .get("data", [])
        )
        items: List[Dict[str, Any]] = []
        for row in transfers[:80]:
            name = str(row.get("name") or "").strip()
            to_club = str(row.get("toClubFullName") or row.get("toClub") or "").strip()
            from_club = str(row.get("fromClubFullName") or row.get("fromClub") or "").strip()
            if not name or not to_club:
                continue
            raw_id = f"fotmob|{row.get('playerId')}|{from_club}|{to_club}|{row.get('transferDate') or row.get('fromDate')}"
            item_id = "fotmob_" + hashlib.sha256(raw_id.encode()).hexdigest()[:20]
            if item_id in seen:
                continue
            seen.add(item_id)
            text = _fotmob_transfer_text(row)
            created = row.get("transferDate") or row.get("fromDate")
            items.append({
                "id": item_id,
                "document_id": item_id,
                "title": text,
                "summary": "",
                "text": text,
                "media_url": None,
                "created_at": created,
                "published_at": created,
                "source_url": url,
                "publisher_url": "https://www.fotmob.com/",
                "publisher_name": "FotMob",
                "source_id": "media.fotmob",
                "source_hint": "media.fotmob",
                "source_handle": "fotmob",
                "username": "fotmob",
                "transport": "FOTMOB",
                "configured_direct_feed": False,
                "declared_sport": "football",
                "feed_id": feed_id,
                "fetched_at": fetched_at,
                "metadata": {"structured_fotmob_transfer": True, "fotmob_row": row},
                "_legacy_story": _fotmob_legacy_story(row),
            })
        return items, 1, []
    except Exception as exc:
        return [], 0, [{"feed_id": feed_id, "error": str(exc)[:300]}]


def fetch_configured_news(
    runtime: VerificationRuntime,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    failures: List[Dict[str, str]] = []
    successes = 0
    fetched_at = datetime.now(timezone.utc).isoformat()

    feed_definitions = _all_feed_definitions(runtime)
    for feed_def in feed_definitions:
        try:
            response = requests.get(
                feed_def.url,
                headers={"User-Agent": "FPLVortexBot/2.0"},
                timeout=float(runtime.config.collection_config["feed_timeout_seconds"]),
            )
            response.raise_for_status()
            parsed = feedparser.parse(response.content)
            status = int(getattr(parsed, "status", 200) or 200)
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")
            entries = list(parsed.entries or [])[: feed_def.max_entries]
            if not entries and getattr(parsed, "bozo", False):
                raise RuntimeError(str(getattr(parsed, "bozo_exception", "feed parse failed")))
            successes += 1
            for entry in entries:
                title = str(entry.get("title") or "").strip()
                summary = _clean_html(entry.get("summary", ""))
                article_url = entry.get("link") or entry.get("id")
                publisher_url, publisher_name = _publisher(entry)
                raw_id = article_url or f"{title}|{entry.get('published') or entry.get('updated')}"
                item_id = "rss_" + hashlib.sha256(str(raw_id).encode()).hexdigest()[:20]
                if item_id in seen:
                    continue
                seen.add(item_id)
                identity = runtime.sources.resolve(
                    url=article_url,
                    publisher_url=publisher_url,
                    source_hint=feed_def.source_hint,
                    transport=feed_def.transport,
                    configured_direct_feed=feed_def.transport.upper() == "DIRECT_RSS",
                )
                text = title if not summary or summary == title else f"{title}. {summary}"
                items.append({
                    "id": item_id,
                    "document_id": item_id,
                    "title": title,
                    "summary": summary,
                    "text": text,
                    "media_url": _media_url(entry),
                    "created_at": entry.get("published") or entry.get("updated"),
                    "published_at": entry.get("published") or entry.get("updated"),
                    "source_url": article_url if str(article_url or "").startswith("http") else None,
                    "publisher_url": publisher_url or None,
                    "publisher_name": publisher_name or None,
                    "source_id": identity.profile_id if identity.verified else feed_def.source_hint,
                    "source_hint": feed_def.source_hint,
                    "username": _legacy_source_name(runtime, identity),
                    "transport": feed_def.transport,
                    "configured_direct_feed": feed_def.transport.upper() == "DIRECT_RSS",
                    "declared_sport": feed_def.declared_sport,
                    "feed_id": feed_def.id,
                    "fetched_at": fetched_at,
                })
        except Exception as exc:
            failures.append({"feed_id": feed_def.id, "error": str(exc)[:300]})

    fotmob_items, fotmob_successes, fotmob_failures = _fetch_fotmob_transfers(runtime, seen, fetched_at)
    items.extend(fotmob_items)
    successes += fotmob_successes
    failures.extend(fotmob_failures)

    social_items, social_successes, social_failures = _fetch_bluesky(runtime, seen, fetched_at)
    items.extend(social_items)
    successes += social_successes
    failures.extend(social_failures)
    total = len(feed_definitions) + len(runtime.feeds.social_feeds) + 1
    health = {
        "feeds_total": total,
        "feeds_succeeded": successes,
        "feeds_failed": len(failures),
        "fail_ratio": len(failures) / total if total else 1.0,
        "failures": failures,
        "at": fetched_at,
    }
    return items, health


def _fetch_bluesky(
    runtime: VerificationRuntime,
    seen: set[str],
    fetched_at: str,
) -> tuple[List[Dict[str, Any]], int, List[Dict[str, str]]]:
    items: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []
    successes = 0
    for feed_def in runtime.feeds.social_feeds:
        try:
            url = (
                "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
                f"?actor={urllib.parse.quote(feed_def.handle)}"
                f"&limit={feed_def.max_entries}&filter=posts_no_replies"
            )
            request = urllib.request.Request(
                url, headers={"User-Agent": "FPLVortexBot/2.0"}
            )
            with urllib.request.urlopen(request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
            successes += 1
            for feed_item in (payload.get("feed") or [])[: feed_def.max_entries]:
                post = feed_item.get("post", {})
                record = post.get("record", {})
                text = str(record.get("text") or "").strip()
                if not text:
                    continue
                uri = post.get("uri") or text
                item_id = "bsky_" + hashlib.sha256(uri.encode()).hexdigest()[:20]
                if item_id in seen:
                    continue
                seen.add(item_id)
                match = re.match(r"at://([^/]+)/[^/]+/([^/]+)$", post.get("uri", ""))
                source_url = (
                    f"https://bsky.app/profile/{match.group(1)}/post/{match.group(2)}"
                    if match else None
                )
                profile = runtime.sources.require(feed_def.source_id)
                items.append({
                    "id": item_id,
                    "document_id": item_id,
                    "title": text,
                    "summary": "",
                    "text": text,
                    "media_url": None,
                    "created_at": record.get("createdAt") or post.get("indexedAt"),
                    "published_at": record.get("createdAt") or post.get("indexedAt"),
                    "source_url": source_url,
                    "source_id": profile.id,
                    "source_hint": profile.id,
                    "source_handle": feed_def.handle,
                    "username": profile.handles[0] if profile.handles else profile.id,
                    "transport": "BLUESKY",
                    "configured_direct_feed": False,
                    "declared_sport": feed_def.declared_sport,
                    "feed_id": feed_def.id,
                    "fetched_at": fetched_at,
                })
        except Exception as exc:
            failures.append({"feed_id": feed_def.id, "error": str(exc)[:300]})
    return items, successes, failures


def _media_url(entry: Any) -> Any:
    content = entry.get("media_content")
    if content:
        return content[0].get("url")
    return None
