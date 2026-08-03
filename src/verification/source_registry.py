"""Configurable source identity and provenance resolution.

A search query for a journalist is not evidence that every result was written by
that journalist.  Likewise, a Google News label is not an official source.  This
module resolves the *actual publisher* from a controlled domain or direct social
handle and deliberately ignores query labels for aggregator transports.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Iterable, Optional
from urllib.parse import urlparse

from .config import ConfigurationError
from .models import SourceIdentity, SourceKind, SourceProfile


def normalize_handle(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9.]", "", (value or "").lower().lstrip("@"))


def normalize_domain(value: Optional[str]) -> str:
    if not value:
        return ""
    candidate = value.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    host = (urlparse(candidate).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


class SourceRegistry:
    def __init__(self, profiles: Iterable[SourceProfile], path: Path):
        self.path = path
        self._profiles: Dict[str, SourceProfile] = {}
        self._domains: Dict[str, str] = {}
        self._handles: Dict[str, str] = {}
        for profile in profiles:
            if profile.id in self._profiles:
                raise ConfigurationError(f"duplicate source id: {profile.id}")
            if not profile.publisher_group:
                raise ConfigurationError(f"source {profile.id} has no publisher_group")
            if not 0.0 <= profile.prior_mean <= 1.0 or profile.prior_strength <= 0:
                raise ConfigurationError(f"source {profile.id} has invalid reliability prior")
            if profile.is_official and not (profile.official_for or profile.official_scope):
                raise ConfigurationError(f"official source {profile.id} has no authority scope")
            self._profiles[profile.id] = profile
            for domain in profile.domains:
                d = normalize_domain(domain)
                previous = self._domains.get(d)
                if previous and previous != profile.id:
                    raise ConfigurationError(
                        f"domain {d} assigned to both {previous} and {profile.id}"
                    )
                self._domains[d] = profile.id
            for handle in profile.handles:
                h = normalize_handle(handle)
                previous = self._handles.get(h)
                if previous and previous != profile.id:
                    raise ConfigurationError(
                        f"handle {h} assigned to both {previous} and {profile.id}"
                    )
                self._handles[h] = profile.id
        if not self._profiles:
            raise ConfigurationError("source registry cannot be empty")

    @classmethod
    def load(cls, path: str | Path = "config/sources.json") -> "SourceRegistry":
        p = Path(path)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ConfigurationError(f"cannot load source registry {p}: {exc}") from exc
        if raw.get("schema_version") != 2:
            raise ConfigurationError("source registry schema_version must be 2")
        profiles = []
        for item in raw.get("sources", []):
            try:
                prior = item["prior"]
                profiles.append(
                    SourceProfile(
                        id=item["id"],
                        display_name=item["display_name"],
                        handles=tuple(item.get("handles", [])),
                        domains=tuple(item.get("domains", [])),
                        publisher_group=item["publisher_group"],
                        kind=SourceKind(item["kind"]),
                        declared_sports=tuple(s.lower() for s in item.get("declared_sports", [])),
                        official_for=tuple(item.get("official_for", [])),
                        official_scope=tuple(item.get("official_scope", [])),
                        allowed_events=tuple(item.get("allowed_events", [])),
                        allowed_milestones=tuple(item.get("allowed_milestones", [])),
                        prior_mean=float(prior["mean"]),
                        prior_strength=float(prior["strength"]),
                        search_enabled=bool(item.get("search_enabled", False)),
                    )
                )
            except Exception as exc:
                raise ConfigurationError(
                    f"invalid source profile {item.get('id', '<unknown>')}: {exc}"
                ) from exc
        return cls(profiles, p)

    def get(self, source_id: str) -> Optional[SourceProfile]:
        return self._profiles.get(source_id)

    def require(self, source_id: str) -> SourceProfile:
        profile = self.get(source_id)
        if profile is None:
            raise ConfigurationError(f"unknown source id: {source_id}")
        return profile

    def all(self) -> tuple[SourceProfile, ...]:
        return tuple(self._profiles.values())

    def profile_for_domain(self, domain_or_url: Optional[str]) -> Optional[SourceProfile]:
        domain = normalize_domain(domain_or_url)
        if not domain:
            return None
        matches = [
            (known, sid)
            for known, sid in self._domains.items()
            if domain == known or domain.endswith("." + known)
        ]
        if not matches:
            return None
        # Most-specific controlled domain wins.
        _, source_id = max(matches, key=lambda pair: len(pair[0]))
        return self._profiles[source_id]

    def profile_for_handle(self, handle: Optional[str]) -> Optional[SourceProfile]:
        source_id = self._handles.get(normalize_handle(handle))
        return self._profiles.get(source_id) if source_id else None

    @staticmethod
    def _identity(profile: SourceProfile, *, resolution: str,
                  domain: Optional[str] = None,
                  handle: Optional[str] = None) -> SourceIdentity:
        return SourceIdentity(
            profile_id=profile.id,
            publisher_group=profile.publisher_group,
            kind=profile.kind,
            verified=True,
            resolution=resolution,
            domain=normalize_domain(domain) or None,
            handle=normalize_handle(handle) or None,
        )

    def resolve(
        self,
        *,
        url: Optional[str] = None,
        publisher_url: Optional[str] = None,
        handle: Optional[str] = None,
        source_hint: Optional[str] = None,
        transport: str = "UNKNOWN",
        configured_direct_feed: bool = False,
    ) -> SourceIdentity:
        """Resolve source identity without trusting labels from aggregators.

        Resolution order:
        1. Publisher domain supplied by the aggregator (e.g. Google ``<source>``).
        2. Controlled domain of the canonical article URL.
        3. Direct social account handle.
        4. A configured source hint *only* for a direct feed whose URL is under
           configuration control.  Hints are never accepted for Google News.
        """
        transport_u = (transport or "UNKNOWN").upper()
        profile = self.profile_for_domain(publisher_url)
        if profile:
            return self._identity(
                profile, resolution="publisher_domain", domain=publisher_url
            )

        url_domain = normalize_domain(url)
        if url_domain and url_domain not in {"news.google.com", "google.com"}:
            profile = self.profile_for_domain(url_domain)
            if profile:
                return self._identity(
                    profile, resolution="article_domain", domain=url_domain
                )

        if transport_u in {"BLUESKY", "X", "TWITTER", "DIRECT_SOCIAL"}:
            profile = self.profile_for_handle(handle)
            if profile:
                return self._identity(
                    profile, resolution="direct_social_handle", handle=handle
                )

        if (
            configured_direct_feed
            and transport_u in {"DIRECT_RSS", "DIRECT_API"}
            and source_hint
        ):
            profile = self.get(source_hint)
            if profile:
                return self._identity(profile, resolution="configured_direct_feed")

        unknown_key = url_domain or normalize_domain(publisher_url) or normalize_handle(handle)
        if not unknown_key:
            unknown_key = "unresolved"
        return SourceIdentity(
            profile_id=f"unknown:{unknown_key}",
            publisher_group=f"unknown:{unknown_key}",
            kind=SourceKind.UNKNOWN,
            verified=False,
            resolution="unresolved",
            domain=url_domain or normalize_domain(publisher_url) or None,
            handle=normalize_handle(handle) or None,
        )
