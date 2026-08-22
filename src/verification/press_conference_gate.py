"""Official PremierLeague.com press-conference publication gate.

Press roundups use one authoritative source: the Premier League website. No
second-source confirmation is required for this lane. The source-domain check
remains so a media or journalist report cannot accidentally enter the official
roundup route.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from .models import DecisionType, EventStatus, EventType, VerificationDecision
from .source_registry import SourceRegistry, normalize_domain

PREMIER_LEAGUE_SOURCE_ID = "official.premier_league"
PREMIER_LEAGUE_DOMAIN = "premierleague.com"
_OFFICIAL_STATUSES = frozenset({EventStatus.OFFICIAL, EventStatus.COMPLETED})


@dataclass(frozen=True)
class OfficialPressConferenceValidation:
    ok: bool
    reason: str
    verified_at: Optional[str] = None

    def __bool__(self) -> bool:
        return self.ok


def _is_valid_url(url: Optional[str]) -> bool:
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_premier_league_domain(url: str) -> bool:
    domain = normalize_domain(url)
    return domain == PREMIER_LEAGUE_DOMAIN or domain.endswith("." + PREMIER_LEAGUE_DOMAIN)


def validate_official_press_conference(
    decision: VerificationDecision,
    sources: SourceRegistry,
    *,
    now: Optional[datetime] = None,
) -> OfficialPressConferenceValidation:
    """Validate one combined PremierLeague.com roundup.

    This deliberately does not inspect publisher counts or corroboration. One
    verified PremierLeague.com article is sufficient authority for this event
    type, while the URL, source identity, speaker, club and extracted roundup
    content are still required.
    """
    now = now or datetime.now(timezone.utc)
    if decision.event_type != EventType.PRESS_CONFERENCE:
        return OfficialPressConferenceValidation(False, "not_a_press_conference_event")
    if decision.decision != DecisionType.PUBLISH or not decision.may_publish:
        return OfficialPressConferenceValidation(False, "engine_did_not_authorize_publish")
    if decision.status not in _OFFICIAL_STATUSES:
        return OfficialPressConferenceValidation(False, f"status_not_official:{decision.status.value}")

    facts: Mapping[str, Any] = decision.verified_facts
    url = decision.source_url
    if not _is_valid_url(url):
        return OfficialPressConferenceValidation(False, "missing_or_invalid_premierleague_source_url")
    if not _is_premier_league_domain(url):
        return OfficialPressConferenceValidation(False, "source_url_is_not_premierleague.com")

    authority_ids = list(dict.fromkeys(decision.authority_source_ids or decision.source_ids))
    if PREMIER_LEAGUE_SOURCE_ID not in authority_ids:
        return OfficialPressConferenceValidation(False, "source_is_not_official_premier_league")
    profile = sources.get(PREMIER_LEAGUE_SOURCE_ID)
    if profile is None or not profile.is_official:
        return OfficialPressConferenceValidation(False, "official_premier_league_profile_missing")

    speaker = facts.get("subject_name")
    club = facts.get("club_name")
    quote_summary = facts.get("quote_summary")
    if not speaker or not str(speaker).strip():
        return OfficialPressConferenceValidation(False, "missing_speaker_full_name")
    if not club or not str(club).strip():
        return OfficialPressConferenceValidation(False, "missing_club")
    if not quote_summary or not str(quote_summary).strip():
        return OfficialPressConferenceValidation(False, "missing_quote_summary")
    if not isinstance(facts.get("key_quotes"), list) or not facts["key_quotes"]:
        return OfficialPressConferenceValidation(False, "missing_extracted_key_quotes")
    if not isinstance(facts.get("roundup"), list) or not facts["roundup"]:
        return OfficialPressConferenceValidation(False, "missing_extracted_roundup")

    return OfficialPressConferenceValidation(True, "official_premierleague_roundup", verified_at=now.isoformat())


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
