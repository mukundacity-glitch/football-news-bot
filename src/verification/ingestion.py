"""Config-driven news ingestion with a strict FotMob source allowlist."""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

import feedparser
import requests

from .documents import FeedDefinition
from .runtime import VerificationRuntime


_TAGS = re.compile(r"<[^>]+>")
ALLOWED_NEWS_FEED_IDS = {"fotmob.premier_league.topnews"}
ALLOWED_NEWS_SOURCE_ID = "media.fotmob"


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
    """Return only the explicitly allowed FotMob news feed.

    Do not add club feeds, Google News discovery feeds, social feeds, or other
    media feeds here. FPL structured data is ingested separately by the FPL API
    workflow. Keeping this allowlist in code is defense-in-depth in case an old
    feed remains in config/feeds.json.
    """
    feeds = [
        feed
        for feed in runtime.feeds.feeds
        if feed.id in ALLOWED_NEWS_FEED_IDS
        and str(feed.source_hint or "") == ALLOWED_NEWS_SOURCE_ID
        and str(feed.transport or "").upper() == "DIRECT_RSS"
    ]
    if len(feeds) != 1:
        raise RuntimeError(
            "FotMob-only ingestion requires exactly one configured FotMob direct feed"
        )
    return feeds


def _fotmob_euro(value: object) -> str:
    """Format FotMob's euro-denominated numeric values without inventing precision."""
    try:
        amount = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if amount <= 0:
        return ""
    if amount >= 1_000_000:
        millions = f"{amount / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"€{millions}m"
    if amount >= 1_000:
        return f"€{round(amount / 1_000):,}k"
    return f"€{amount:,}"


def _fotmob_contract_until(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%b %Y")
    except Exception:
        return text[:10]


def _fotmob_row_kind(row: Dict[str, Any]) -> str:
    """Return the explicit FotMob transaction kind without guessing from clubs."""
    fee = row.get("fee") or {}
    fee_label = str(fee.get("feeText") or "").strip().lower()
    if "contract extension" in fee_label:
        return "contract_extension"
    if bool(row.get("onLoan")) or "loan" in fee_label:
        return "loan"
    if "free" in fee_label:
        return "free"
    return "permanent"


def _fotmob_transfer_text(row: Dict[str, Any]) -> str:
    name = str(row.get("name") or "").strip()
    from_club = str(row.get("fromClubFullName") or row.get("fromClub") or "").strip()
    to_club = str(row.get("toClubFullName") or row.get("toClub") or "").strip()
    kind = _fotmob_row_kind(row)
    fee = row.get("fee") or {}
    fee_value = _fotmob_euro(fee.get("value"))
    contract_until = _fotmob_contract_until(row.get("toDate"))
    market_value = _fotmob_euro(row.get("marketValue"))
    position = str((row.get("position") or {}).get("label") or "").strip()

    if kind == "contract_extension":
        club = to_club or from_club
        bits = [
            f"{name} extends the contract with {club}.",
            "FotMob listed the contract extension as completed.",
            "Deal type: contract extension.",
        ]
    elif kind == "loan":
        bits = [
            f"{name} has joined {to_club} from {from_club} on loan.",
            "FotMob listed the transfer as completed.",
            "Deal type: loan.",
        ]
    elif kind == "free":
        bits = [
            f"{name} has joined {to_club} from {from_club} on a free transfer.",
            "FotMob listed the transfer as completed.",
            "Deal type: free transfer.",
        ]
    else:
        bits = [
            f"{name} has joined {to_club} from {from_club}.",
            "FotMob listed the transfer as completed.",
            "Deal type: permanent transfer.",
        ]

    if fee_value and kind != "contract_extension":
        bits.append(f"Fee: {fee_value}.")
    if contract_until:
        bits.append(f"Contract until {contract_until}.")
    if market_value:
        bits.append(f"Market value: {market_value}.")
    if position:
        bits.append(f"Position: {position}.")
    return " ".join(bits)


def _fotmob_legacy_story(row: Dict[str, Any]) -> Dict[str, Any]:
    kind = _fotmob_row_kind(row)
    fee = row.get("fee") or {}
    fee_text = _fotmob_euro(fee.get("value"))
    is_extension = kind == "contract_extension"
    player_name = str(row.get("name") or "").strip()
    raw_from = str(row.get("fromClubFullName") or row.get("fromClub") or "").strip()
    raw_to = str(row.get("toClubFullName") or row.get("toClub") or "").strip()
    club = raw_to or raw_from
    text = _fotmob_transfer_text(row)

    if is_extension:
        event = "renewal"
        from_club = None
        to_club = club
        transfer_kind = None
    else:
        event = "loan" if kind == "loan" else "transfer"
        from_club = raw_from
        to_club = raw_to
        transfer_kind = kind

    return {
        "player": player_name,
        "event": event,
        "from_club": from_club,
        "to_club": to_club,
        "_structured_fotmob_transfer": not is_extension,
        "_structured_fotmob_contract_extension": is_extension,
        "_structured_transfer_group": (
            f"{player_name}|{raw_from}|{raw_to}" if not is_extension else None
        ),
        "fee": (fee_text or None) if not is_extension else None,
        "contract": _fotmob_contract_until(row.get("toDate")) or None,
        "market_value": _fotmob_euro(row.get("marketValue")) or None,
        "position": str((row.get("position") or {}).get("label") or "").strip() or None,
        "event_time": row.get("transferDate") or row.get("fromDate"),
        "transfer_kind": transfer_kind,
        "stage": 4,
        "collapsed": False,
        "historical": False,
        "headline": text,
        "raw_text": text,
        "sources": ["fotmob"],
    }


def _fetch_fotmob_transfers(
    runtime: VerificationRuntime,
    seen: set[str],
    fetched_at: str,
) -> tuple[List[Dict[str, Any]], int, List[Dict[str, str]]]:
    """Fetch the structured FotMob Premier League transfer table.

    The data is discovery input only. Existing downstream V2 verification,
    entity matching, status checks, freshness checks and deduplication remain
    mandatory before publication.
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
        ordered = sorted(
            transfers,
            key=lambda row: str(row.get("transferDate") or row.get("fromDate") or ""),
            reverse=True,
        )
        for row in ordered:
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
            legacy_story = _fotmob_legacy_story(row)
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
                "source_id": ALLOWED_NEWS_SOURCE_ID,
                "source_hint": ALLOWED_NEWS_SOURCE_ID,
                "source_handle": "fotmob",
                "username": "fotmob",
                "transport": "FOTMOB",
                "configured_direct_feed": False,
                "declared_sport": "football",
                "feed_id": feed_id,
                "fetched_at": fetched_at,
                "metadata": {
                    "structured_fotmob_transfer": bool(legacy_story.get("_structured_fotmob_transfer")),
                    "structured_fotmob_contract_extension": bool(legacy_story.get("_structured_fotmob_contract_extension")),
                    "fotmob_row": row,
                },
                "_legacy_story": legacy_story,
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
                    configured_direct_feed=True,
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
                    "configured_direct_feed": True,
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

    total = len(feed_definitions) + 1
    fotmob_recent = 0
    for item in fotmob_items:
        try:
            published = datetime.fromisoformat(str(item.get("created_at") or "").replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - published).total_seconds() <= 48 * 3600:
                fotmob_recent += 1
        except Exception:
            continue
    health = {
        "feeds_total": total,
        "feeds_succeeded": successes,
        "feeds_failed": len(failures),
        "fotmob_items": len(fotmob_items),
        "fotmob_items_within_48h": fotmob_recent,
        "fail_ratio": len(failures) / total if total else 1.0,
        "failures": failures,
        "at": fetched_at,
    }
    return items, health


def _media_url(entry: Any) -> Any:
    content = entry.get("media_content")
    if content:
        return content[0].get("url")
    return None
