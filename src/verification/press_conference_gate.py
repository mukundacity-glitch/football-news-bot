"""Strict, standalone pre-publish gate for PRESS_CONFERENCE decisions.

Mirrors ``official_transfer_gate.py`` exactly: this is defense-in-depth,
layered ON TOP OF the engine's own hardcoded refusal to treat any
non-official source as authoritative for PRESS_CONFERENCE (see
``engine.py._configured_nonofficial_confirmation``). Even a bug or future
change there cannot alone cause an unconfirmed quote to reach a caption or a
card, because this module re-checks every fact from scratch using only the
already-serialized ``VerificationDecision``.

If validation fails, callers MUST NOT generate an image or caption and MUST
log ``SKIPPED_UNVERIFIED_PRESS_CONFERENCE`` with the exact reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from .models import DecisionType, EventStatus, EventType, VerificationDecision
from .source_registry import SourceRegistry, normalize_domain

# Same official-confirmed bar as transfers: only these two engine statuses
# may ever be treated as "official_confirmed" for a press conference quote.
_OFFICIAL_CONFIRMED_STATUSES = frozenset({EventStatus.OFFICIAL, EventStatus.COMPLETED})


@dataclass(frozen=True)
class OfficialPressConferenceValidation:
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


def validate_official_press_conference(
    decision: VerificationDecision,
    sources: SourceRegistry,
    *,
    now: Optional[datetime] = None,
) -> OfficialPressConferenceValidation:
    """Return the strict official-only validation result for a PRESS_CONFERENCE decision.

    ALL of the following must hold, or publication is blocked:
      - event_type is PRESS_CONFERENCE
      - decision.decision == PUBLISH and every critical gate already PASSed
      - status is exactly OFFICIAL or COMPLETED ("official_confirmed")
      - an official source URL exists and is a well-formed http(s) URL
      - an official source name/profile exists, is on the official allowlist,
        and (when the profile declares controlled domains) the source URL's
        domain matches the allowlist -- exactly the same check as transfers,
        so a journalist merely quoting the same press conference can never
        qualify even if they were physically in the room
      - speaker (subject) full name, club, and a short verified quote summary
        are all present
    """
    now = now or datetime.now(timezone.utc)

    if decision.event_type != EventType.PRESS_CONFERENCE:
        return OfficialPressConferenceValidation(False, "not_a_press_conference_event")

    if decision.decision != DecisionType.PUBLISH or not decision.may_publish:
        return OfficialPressConferenceValidation(False, "engine_did_not_authorize_publish")

    if decision.status not in _OFFICIAL_CONFIRMED_STATUSES:
        return OfficialPressConferenceValidation(
            False, f"status_not_official_confirmed:{decision.status.value}"
        )

    facts: Mapping[str, Any] = decision.verified_facts

    url = decision.source_url
    if not _is_valid_url(url):
        return OfficialPressConferenceValidation(False, "missing_or_invalid_official_source_url")

    source_id = decision.source_ids[0] if decision.source_ids else None
    profile = sources.get(source_id) if source_id else None
    if profile is None or not profile.display_name:
        return OfficialPressConferenceValidation(False, "missing_official_source_name")
    if not profile.is_official:
        return OfficialPressConferenceValidation(False, "source_not_on_official_allowlist")

    domain = normalize_domain(url)
    domain_allowed = any(
        domain == normalize_domain(d) or domain.endswith("." + normalize_domain(d))
        for d in profile.domains
    )
    if profile.domains and not domain_allowed:
        return OfficialPressConferenceValidation(
            False, "official_source_domain_not_on_allowlist"
        )

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
    """Durable, structured record of a press-conference post that was blocked.

    Reuses the existing rejection-log review queue, exactly like the transfer
    gate. Never raises.
    """
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
    log_rejection(
        "SKIPPED_UNVERIFIED_PRESS_CONFERENCE",
        story,
        reason,
        sources=sources,
    )
