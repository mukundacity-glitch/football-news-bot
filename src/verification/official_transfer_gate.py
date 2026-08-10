"""Strict, standalone pre-publish gate for TRANSFER decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from .models import DecisionType, EventStatus, EventType, VerificationDecision
from .source_registry import SourceRegistry, normalize_domain

_OFFICIAL_CONFIRMED_STATUSES = frozenset({EventStatus.OFFICIAL, EventStatus.COMPLETED})
_FORBIDDEN_FEE_TOKENS = (
    "report", "reported", "estimate", "estimated", "believed", "understood",
    "rumour", "rumor", "close to", "in the region of", "around £", "circa",
    "expected", "could be", "said to be",
)
_NONOFFICIAL_SOURCE_PREFIXES = ("media.", "journalist.", "reporter.", "aggregator.")


@dataclass(frozen=True)
class OfficialTransferValidation:
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


def validate_official_transfer(
    decision: VerificationDecision,
    sources: SourceRegistry,
    *,
    now: Optional[datetime] = None,
) -> OfficialTransferValidation:
    """Strictly validate a completed, first-party official transfer."""
    now = now or datetime.now(timezone.utc)
    if decision.event_type != EventType.TRANSFER:
        return OfficialTransferValidation(False, "not_a_transfer_event")
    if decision.decision != DecisionType.PUBLISH or not decision.may_publish:
        return OfficialTransferValidation(False, "engine_did_not_authorize_publish")
    if decision.status not in _OFFICIAL_CONFIRMED_STATUSES:
        return OfficialTransferValidation(False, f"status_not_official_confirmed:{decision.status.value}")

    facts: Mapping[str, Any] = decision.verified_facts
    url = decision.source_url
    if not _is_valid_url(url):
        return OfficialTransferValidation(False, "missing_or_invalid_official_source_url")

    source_id = decision.source_ids[0] if decision.source_ids else None
    profile = sources.get(source_id) if source_id else None
    if profile is None or not profile.display_name:
        return OfficialTransferValidation(False, "missing_official_source_name")

    source_norm = str(source_id or "").strip().lower()
    if source_norm.startswith(_NONOFFICIAL_SOURCE_PREFIXES):
        # Preserve the public gate contract: media/journalist sources are
        # rejected as non-official, while the namespace check is defense in depth.
        return OfficialTransferValidation(False, "source_not_on_official_allowlist")
    if not profile.is_official:
        return OfficialTransferValidation(False, "source_not_on_official_allowlist")

    domain = normalize_domain(url)
    domain_allowed = any(
        domain == normalize_domain(d) or domain.endswith("." + normalize_domain(d))
        for d in profile.domains
    )
    if profile.domains and not domain_allowed:
        return OfficialTransferValidation(False, "official_source_domain_not_on_allowlist")

    player = facts.get("subject_name")
    from_club = facts.get("club_from_name")
    to_club = facts.get("club_to_name")
    if not player or not str(player).strip():
        return OfficialTransferValidation(False, "missing_player_full_name")
    if not to_club or not str(to_club).strip():
        return OfficialTransferValidation(False, "missing_to_club")
    if not from_club or not str(from_club).strip():
        return OfficialTransferValidation(False, "missing_from_club")
    if _normalize(from_club) == _normalize(to_club):
        return OfficialTransferValidation(False, "from_club_equals_to_club")

    fee = facts.get("fee")
    if fee:
        lowered = _normalize(fee)
        if any(token in lowered for token in _FORBIDDEN_FEE_TOKENS):
            return OfficialTransferValidation(False, f"unsupported_fee_language:{fee!r}")

    return OfficialTransferValidation(True, "ok", verified_at=now.isoformat())


def log_skipped_unverified_transfer(
    decision: Optional[VerificationDecision],
    reason: str,
    *,
    raw_item: Optional[Mapping[str, Any]] = None,
) -> None:
    from src.rejection_log import log_rejection

    facts: Mapping[str, Any] = decision.verified_facts if decision else (raw_item or {})
    story = {
        "player": facts.get("subject_name") or (raw_item or {}).get("player"),
        "event": "transfer",
        "from_club": facts.get("club_from_name"),
        "from_key": facts.get("club_from_id"),
        "to_club": facts.get("club_to_name"),
        "to_key": facts.get("club_to_id"),
        "fee": facts.get("fee"),
        "contract": facts.get("contract_length"),
        "stage": 4,
    }
    sources = list(decision.source_ids) if decision else []
    log_rejection("SKIPPED_UNVERIFIED_TRANSFER", story, reason, sources=sources)
