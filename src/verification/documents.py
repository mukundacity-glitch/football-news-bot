"""Feed configuration and immutable source-document construction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .config import ConfigurationError
from .models import RawDocument
from .source_registry import SourceRegistry


@dataclass(frozen=True)
class FeedDefinition:
    id: str
    url: str
    transport: str
    source_hint: Optional[str]
    declared_sport: Optional[str]
    max_entries: int


@dataclass(frozen=True)
class SocialFeedDefinition:
    id: str
    network: str
    handle: str
    source_id: str
    declared_sport: Optional[str]
    max_entries: int


class FeedRegistry:
    def __init__(
        self,
        feeds: Iterable[FeedDefinition],
        social_feeds: Iterable[SocialFeedDefinition],
        official_discovery: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.feeds = tuple(feeds)
        self.social_feeds = tuple(social_feeds)
        self.official_discovery = dict(official_discovery or {})
        ids = [f.id for f in self.feeds] + [f.id for f in self.social_feeds]
        if len(ids) != len(set(ids)):
            raise ConfigurationError("feed IDs must be unique")

    @classmethod
    def load(cls, path: str | Path = "config/feeds.json") -> "FeedRegistry":
        p = Path(path)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ConfigurationError(f"cannot load feed config {p}: {exc}") from exc
        if raw.get("schema_version") != 2:
            raise ConfigurationError("feed config schema_version must be 2")
        feeds = [
            FeedDefinition(
                id=item["id"],
                url=item["url"],
                transport=item["transport"],
                source_hint=item.get("source_hint"),
                declared_sport=item.get("declared_sport"),
                max_entries=int(item.get("max_entries", 20)),
            )
            for item in raw.get("feeds", [])
        ]
        social = [
            SocialFeedDefinition(
                id=item["id"],
                network=item["network"],
                handle=item["handle"],
                source_id=item["source_id"],
                declared_sport=item.get("declared_sport"),
                max_entries=int(item.get("max_entries", 30)),
            )
            for item in raw.get("social_feeds", [])
        ]
        return cls(feeds, social, raw.get("official_discovery"))


class DocumentFactory:
    def __init__(self, sources: SourceRegistry):
        self.sources = sources

    def from_item(self, item: Dict[str, Any]) -> RawDocument:
        title = str(item.get("title") or item.get("text") or "").strip()
        body = str(
            item.get("full_text") or item.get("summary") or item.get("body") or ""
        ).strip()
        if not body and item.get("text") and title != item.get("text"):
            body = str(item["text"])
        transport = str(item.get("transport") or "UNKNOWN").upper()
        identity = self.sources.resolve(
            url=item.get("source_url"),
            publisher_url=item.get("publisher_url"),
            handle=item.get("source_handle") or item.get("username"),
            source_hint=item.get("source_id") or item.get("source_hint"),
            transport=transport,
            configured_direct_feed=bool(item.get("configured_direct_feed")),
        )
        fetched_at = str(
            item.get("fetched_at") or datetime.now(timezone.utc).isoformat()
        )
        raw_id = "|".join(
            str(x or "")
            for x in (
                identity.profile_id,
                item.get("source_url"),
                item.get("published_at") or item.get("created_at"),
                title,
                body,
            )
        )
        document_id = str(item.get("document_id") or hashlib.sha256(raw_id.encode()).hexdigest())
        metadata = dict(item.get("metadata") or {})
        if item.get("publisher_name"):
            metadata["publisher_name"] = item["publisher_name"]
        if item.get("_fpl_pre_built"):
            metadata["structured_official"] = True
            metadata["structured_source"] = "FPL_BOOTSTRAP"
        return RawDocument(
            id=document_id,
            title=title,
            body=body,
            url=item.get("source_url"),
            published_at=item.get("published_at") or item.get("created_at"),
            fetched_at=fetched_at,
            source=identity,
            feed_id=item.get("feed_id"),
            declared_sport=item.get("declared_sport"),
            metadata=metadata,
        )
