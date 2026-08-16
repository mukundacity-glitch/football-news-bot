"""Strict publication gate for clearly-labelled tier-one transfer reports.

This lane is intentionally separate from official transfer confirmation:

* TALKS / NEGOTIATION / BID may publish from one approved tier-one publisher.
* AGREEMENT / MEDICAL / HERE_WE_GO require two independent approved publishers.
* INTEREST / RUMOUR never publish.
* OFFICIAL / COMPLETED normally require a first-party club, league, or
  governing-body source.
* The structured FotMob Premier League transfer table may publish COMPLETED as
  a clearly labelled third-party listing, never as official confirmation.

The distinction is carried on ``VerificationDecision.authority_kind`` so no
reported item can accidentally receive CONFIRMED wording or card treatment.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from .models import DecisionType, EventStatus, EventType, VerificationDecision
from .source_registry import SourceRegistry


AUTHORITY_KIND = "tier_one_reported_transfer"
FOTMOB_AUTHORITY_KIND = "structured_fotmob_reported_transfer"
FOTMOB_SOURCE_ID = "media.fotmob"
REPORTED_AUTHORITY_KINDS = frozenset({AUTHORITY_KIND, FOTMOB_AUTHORITY_KIND})

# User-approved, deliberately narrow list.  David Ornstein and The Athletic
# share one independence group so the same newsroom cannot count twice.
APPROVED_SOURCE_GROUPS = {
    "journalist.fabrizio_romano": "fabrizio-romano",
    "journalist.david_ornstein": "the-athletic",
    "media.the_athletic": "the-athletic",
    "media.bbc_sport": "bbc",
    "media.sky_sports": "sky-sports",
}

SINGLE_SOURCE_STATUSES = frozenset({
    EventStatus.TALKS,
    EventStatus.NEGOTIATION,
    EventStatus.BID,
})
TWO_SOURCE_STATUSES = frozenset({
    EventStatus.AGREEMENT,
    EventStatus.MEDICAL,
    EventStatus.HERE_WE_GO,
})
REPORTED_STATUSES = SINGLE_SOURCE_STATUSES | TWO_SOURCE_STATUSES


@dataclass(frozen=True)
class ReportedTransferValidation:
    ok: bool
    reason: str

    def __bool__(self) -> bool:
        return self.ok


def source_independence_group(source_id: str) -> Optional[str]:
    return APPROVED_SOURCE_GROUPS.get(str(source_id or "").strip())


def approved_source_ids() -> frozenset[str]:
    return frozenset(APPROVED_SOURCE_GROUPS)


def required_independent_publishers(status: EventStatus) -> int:
    if status in SINGLE_SOURCE_STATUSES:
        return 1
    if status in TWO_SOURCE_STATUSES:
        return 2
    return 10**9  # fail closed for every status outside the reported lane


def _valid_url(value: Optional[str]) -> bool:
    if not value or not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def is_reported_transfer(decision: VerificationDecision) -> bool:
    return (
        decision.event_type == EventType.TRANSFER
        and decision.authority_kind in REPORTED_AUTHORITY_KINDS
    )


def validate_reported_transfer(
    decision: VerificationDecision,
    sources: SourceRegistry,
) -> ReportedTransferValidation:
    """Validate a V2 decision before reported wording/card generation."""
    if decision.event_type != EventType.TRANSFER:
        return ReportedTransferValidation(False, "not_a_transfer_event")
    if decision.decision != DecisionType.PUBLISH or not decision.may_publish:
        return ReportedTransferValidation(False, "engine_did_not_authorize_publish")
    if decision.authority_kind not in REPORTED_AUTHORITY_KINDS:
        return ReportedTransferValidation(False, "wrong_authority_kind")
    if not _valid_url(decision.source_url):
        return ReportedTransferValidation(False, "missing_or_invalid_source_url")

    authority_ids = list(dict.fromkeys(decision.authority_source_ids))
    if not authority_ids:
        return ReportedTransferValidation(False, "missing_authority_sources")
    if any(sources.get(source_id) is None for source_id in authority_ids):
        return ReportedTransferValidation(False, "source_profile_missing")

    facts: Mapping[str, Any] = decision.verified_facts
    if decision.authority_kind == FOTMOB_AUTHORITY_KIND:
        if decision.status != EventStatus.COMPLETED:
            return ReportedTransferValidation(
                False, f"fotmob_status_not_completed:{decision.status.value}"
            )
        if authority_ids != [FOTMOB_SOURCE_ID]:
            return ReportedTransferValidation(False, "fotmob_authority_source_mismatch")
        if facts.get("structured_source") != "fotmob_transfer_table":
            return ReportedTransferValidation(False, "fotmob_structured_marker_missing")
        if not str(facts.get("provider_player_id") or "").isdigit():
            return ReportedTransferValidation(False, "fotmob_player_id_missing")
    else:
        if decision.status not in REPORTED_STATUSES:
            return ReportedTransferValidation(
                False, f"status_not_reportable:{decision.status.value}"
            )
        if any(source_id not in APPROVED_SOURCE_GROUPS for source_id in authority_ids):
            return ReportedTransferValidation(False, "source_not_on_tier_one_allowlist")
        groups = {
            source_independence_group(source_id)
            for source_id in authority_ids
            if source_independence_group(source_id)
        }
        needed = required_independent_publishers(decision.status)
        if len(groups) < needed:
            return ReportedTransferValidation(
                False, f"insufficient_independent_sources:{len(groups)}/{needed}"
            )
    player = facts.get("subject_name")
    origin = facts.get("club_from_name")
    destination = facts.get("club_to_name")
    if not player or not str(player).strip():
        return ReportedTransferValidation(False, "missing_player_full_name")
    if not origin or not str(origin).strip():
        return ReportedTransferValidation(False, "missing_from_club")
    if not destination or not str(destination).strip():
        return ReportedTransferValidation(False, "missing_to_club")
    if _norm(origin) == _norm(destination):
        return ReportedTransferValidation(False, "from_club_equals_to_club")

    return ReportedTransferValidation(True, "ok")


def reported_status_label(status: EventStatus, facts: Mapping[str, Any]) -> str:
    """Return a factual card/caption label without promoting the milestone."""
    if facts.get("structured_source") == "fotmob_transfer_table":
        kind = str(facts.get("transfer_kind") or "").strip().upper()
        return f"{kind} LISTED COMPLETED" if kind else "LISTED COMPLETED"
    detail = str(facts.get("reported_status_detail") or "").strip()
    if detail:
        return detail.upper()
    return {
        EventStatus.TALKS: "TALKS ACTIVE",
        EventStatus.NEGOTIATION: "NEGOTIATIONS ONGOING",
        EventStatus.BID: "BID REPORTED",
        EventStatus.AGREEMENT: "AGREEMENT REPORTED",
        EventStatus.MEDICAL: "MEDICAL REPORTED",
        EventStatus.HERE_WE_GO: "HERE WE GO REPORTED",
    }.get(status, "REPORTED UPDATE")
