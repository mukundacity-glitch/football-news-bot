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

    social_items, social_successes, social_failures = _fetch_bluesky(runtime, seen, fetched_at)
    items.extend(social_items)
    successes += social_successes
    failures.extend(social_failures)
    total = len(feed_definitions) + len(runtime.feeds.social_feeds)
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
