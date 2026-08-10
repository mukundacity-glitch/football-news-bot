"""Strict, standalone pre-publish gate for PRESS_CONFERENCE decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from .models import DecisionType, EventStatus, EventType, VerificationDecision
from .source_registry import SourceRegistry, normalize_domain

_OFFICIAL_CONFIRMED_STATUSES = frozenset({EventStatus.OFFICIAL, EventStatus.COMPLETED})
_NONOFFICIAL_SOURCE_PREFIXES = ("media.", "journalist.", "reporter.", "aggregator.")


@dataclass(frozen=True)
class OfficialPressConferenceValidation:
    ok: bool
    reason: str
    verified_at: Optional[str] = None

    def __bool__(self) -> bool:
        return self.ok


def _normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _is_valid_url(url: Optional[str]) -> bool:
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_official_press_conference(
    decision: VerificationDecision,
    sources: SourceRegistry,
    *,
    now: Optional[datetime] = None,
) -> OfficialPressConferenceValidation:
    now = now or datetime.now(timezone.utc)
    if decision.event_type != EventType.PRESS_CONFERENCE:
        return OfficialPressConferenceValidation(False, "not_a_press_conference_event")
    if decision.decision != DecisionType.PUBLISH or not decision.may_publish:
        return OfficialPressConferenceValidation(False, "engine_did_not_authorize_publish")
    if decision.status not in _OFFICIAL_CONFIRMED_STATUSES:
        return OfficialPressConferenceValidation(False, f"status_not_official_confirmed:{decision.status.value}")

    facts: Mapping[str, Any] = decision.verified_facts
    url = decision.source_url
    if not _is_valid_url(url):
        return OfficialPressConferenceValidation(False, "missing_or_invalid_official_source_url")

    source_id = decision.source_ids[0] if decision.source_ids else None
    profile = sources.get(source_id) if source_id else None
    if profile is None or not profile.display_name:
        return OfficialPressConferenceValidation(False, "missing_official_source_name")

    source_norm = str(source_id or "").strip().lower()
    if source_norm.startswith(_NONOFFICIAL_SOURCE_PREFIXES):
        return OfficialPressConferenceValidation(False, "nonfirstparty_source_cannot_authorize_press_conference")
    if not profile.is_official:
        return OfficialPressConferenceValidation(False, "source_not_on_official_allowlist")

    domain = normalize_domain(url)
    domain_allowed = any(
        domain == normalize_domain(d) or domain.endswith("." + normalize_domain(d))
        for d in profile.domains
    )
    if profile.domains and not domain_allowed:
        return OfficialPressConferenceValidation(False, "official_source_domain_not_on_allowlist")

    speaker = facts.get("subject_name")
    club = facts.get("club_name")
    quote_summary = facts.get("quote_summary")
    if not speaker or not str(speaker).strip():
        return OfficialPressConferenceValidation(False, "missing_speaker_full_name")
    if not club or not str(club).strip():
        return OfficialPressConferenceValidation(False, "missing_club")
    if not quote_summary or not str(quote_summary).strip():
        return OfficialPressConferenceValidation(False, "missing_quote_summary")

    return OfficialPressConferenceValidation(True, "ok", verified_at=now.isoformat())


def log_skipped_unverified_press_conference(
    decision: Optional[VerificationDecision],
    reason: str,
    *,
    raw_item: Optional[Mapping[str, Any]] = None,
) -> None:
    from src.rejection_log import log_rejection

    facts: Mapping[str, Any] = decision.verified_facts if decision else (raw_item or {})
    story = {
        "player": facts.get("subject_name") or (raw_item or {}).get("player"),
        "event": "press_conference",
        "from_club": facts.get("club_name"),
        "from_key": facts.get("club_id"),
        "quote_summary": facts.get("quote_summary"),
        "quote_topic": facts.get("quote_topic"),
        "stage": 4,
    }
    sources = list(decision.source_ids) if decision else []
    log_rejection("SKIPPED_UNVERIFIED_PRESS_CONFERENCE", story, reason, sources=sources)
