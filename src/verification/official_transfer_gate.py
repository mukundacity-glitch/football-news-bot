"""Strict, standalone pre-publish gate for TRANSFER decisions.

This module is deliberately independent of (and layered ON TOP OF) the
engine's own gates in ``engine.py``.  Two things must both agree before a
transfer graphic/caption is ever generated:

  1. ``VerificationEngine.verify()`` already refuses to mark a transfer
     ``PUBLISH`` unless a first-party official source confirmed it
     (see ``engine.py._configured_nonofficial_confirmation`` returning
     ``([], "none")`` unconditionally for every TRANSFER).
  2. This module re-checks every fact required by the non-negotiable policy
     from scratch, using only the verified decision object, and blocks
     publication if ANY required fact/URL/domain/consistency check fails.

The intent is defense-in-depth: even a bug or a future change in engine.py
cannot alone cause an unconfirmed transfer to reach a caption or a card,
because this second, narrower function has no dependency on engine internals
beyond the already-serialized ``VerificationDecision``.

If validation fails, callers MUST NOT generate an image or caption and MUST
log ``SKIPPED_UNVERIFIED_TRANSFER`` with the exact reason (see
``log_skipped_unverified_transfer`` below).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from .models import DecisionType, EventStatus, EventType, VerificationDecision
from .source_registry import SourceRegistry, normalize_domain

# A transfer's status may only ever be treated as "official_confirmed" (in
# this bot's vocabulary) when it is one of these two engine statuses. Every
# other status in EventStatus (RUMOUR, INTEREST, TALKS, NEGOTIATION, BID,
# AGREEMENT, MEDICAL, HERE_WE_GO, UNKNOWN) must be rejected here even if some
# future bug lets the engine mark them PUBLISH.
_OFFICIAL_CONFIRMED_STATUSES = frozenset({EventStatus.OFFICIAL, EventStatus.COMPLETED})

# Fee text must never carry hedged/journalistic language. If a fee value
# contains any of these tokens it did not come from an explicit official
# figure and must not be shown as one.
_FORBIDDEN_FEE_TOKENS = (
    "report", "reported", "estimate", "estimated", "believed", "understood",
    "rumour", "rumor", "close to", "in the region of", "around £", "circa",
    "expected", "could be", "said to be",
)


@dataclass(frozen=True)
class OfficialTransferValidation:
    ok: bool
    reason: str
    verified_at: Optional[str] = None

    def __bool__(self) -> bool:  # pragma: no cover - convenience
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
    """Return the strict official-only validation result for a TRANSFER decision.

    ALL of the following must hold, or publication is blocked:
      - event_type is TRANSFER
      - decision.decision == PUBLISH and every critical gate already PASSed
      - status is exactly OFFICIAL or COMPLETED ("official_confirmed")
      - an official source URL exists and is a well-formed http(s) URL
      - an official source name/profile exists
      - that source is on the official allowlist (SourceProfile.is_official)
        and its identity was verified via a controlled domain/handle, not a
        label or a search-query hint
      - player full name, from club, to club are all present
      - from club and to club are different
      - if a fee is present, it is not phrased as a journalist estimate/report
    """
    now = now or datetime.now(timezone.utc)

    if decision.event_type != EventType.TRANSFER:
        return OfficialTransferValidation(False, "not_a_transfer_event")

    if decision.decision != DecisionType.PUBLISH or not decision.may_publish:
        return OfficialTransferValidation(False, "engine_did_not_authorize_publish")

    if decision.status not in _OFFICIAL_CONFIRMED_STATUSES:
        return OfficialTransferValidation(
            False, f"status_not_official_confirmed:{decision.status.value}"
        )

    facts: Mapping[str, Any] = decision.verified_facts

    url = decision.source_url
    if not _is_valid_url(url):
        return OfficialTransferValidation(False, "missing_or_invalid_official_source_url")

    source_id = decision.source_ids[0] if decision.source_ids else None
    profile = sources.get(source_id) if source_id else None
    if profile is None or not profile.display_name:
        return OfficialTransferValidation(False, "missing_official_source_name")
    if not profile.is_official:
        return OfficialTransferValidation(False, "source_not_on_official_allowlist")

    domain = normalize_domain(url)
    domain_allowed = any(
        domain == normalize_domain(d) or domain.endswith("." + normalize_domain(d))
        for d in profile.domains
    )
    # A verified official social-media account (handle-based identity, no
    # controlled domain configured) is also acceptable, per policy section 4,
    # but ONLY because SourceRegistry.resolve() already required the claim's
    # identity to be resolved through that exact configured handle -- never a
    # label. profile.is_official above already guarantees allowlist
    # membership; this domain check adds a second, independent confirmation
    # for the common club-website case and is skipped only for handle-only
    # official profiles (no domains configured).
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
    """Durable, structured record of a transfer that was blocked at this gate.

    Reuses the existing rejection-log review queue (``queue/debug/...``) so a
    skipped transfer can be manually reviewed later without inventing a new
    storage mechanism. Never raises.
    """
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
    log_rejection(
        "SKIPPED_UNVERIFIED_TRANSFER",
        story,
        reason,
        sources=sources,
    )
