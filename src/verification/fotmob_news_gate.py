"""Isolated trusted-news gate for FotMob Premier League reporting.

This module is deliberately narrow. It does not change verification rules for
any other publisher and it never authorizes press conferences, previews,
opinions, recaps, or generic football news.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Sequence

from .models import Claim, EventStatus, EventType


FOTMOB_SOURCE_ID = "media.fotmob"
FOTMOB_NEWS_AUTHORITY_KIND = "trusted_fotmob_news"
FOTMOB_NEWS_MAX_AGE_HOURS = 24.0

_FOTMOB_TRANSFER_STATUSES = frozenset({
    EventStatus.TALKS,
    EventStatus.NEGOTIATION,
    EventStatus.BID,
    EventStatus.AGREEMENT,
    EventStatus.MEDICAL,
    EventStatus.HERE_WE_GO,
    EventStatus.COMPLETED,
})
_FOTMOB_AVAILABILITY_STATUSES = frozenset({
    EventStatus.OFFICIAL,
    EventStatus.COMPLETED,
})


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        try:
            parsed = parsedate_to_datetime(text)
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _allowed_statuses(event: EventType) -> frozenset[EventStatus]:
    if event == EventType.TRANSFER:
        return _FOTMOB_TRANSFER_STATUSES
    if event in {EventType.INJURY, EventType.SUSPENSION}:
        return _FOTMOB_AVAILABILITY_STATUSES
    return frozenset()


def select_trusted_fotmob_claim(
    claims: Sequence[Claim],
    event: EventType,
    *,
    now: datetime | None = None,
) -> Claim | None:
    """Return one fresh, verified FotMob PL claim or None.

    Entity, league, event classification, mandatory facts, conflicts, dedup and
    confidence are still enforced by the normal V2 engine after this selection.
    """
    allowed = _allowed_statuses(event)
    if not allowed:
        return None

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    eligible: list[tuple[datetime, Claim]] = []
    for claim in claims:
        if claim.source_id != FOTMOB_SOURCE_ID:
            continue
        if not claim.document.source.verified:
            continue
        if claim.document.metadata.get("structured_fotmob_transfer") is True:
            continue
        if claim.article_category != event or not claim.league_relevant:
            continue
        if claim.status not in allowed:
            continue
        published = _parse_timestamp(claim.document.published_at)
        if published is None:
            continue
        age_hours = (current - published).total_seconds() / 3600.0
        if age_hours < -0.25 or age_hours > FOTMOB_NEWS_MAX_AGE_HOURS:
            continue
        eligible.append((published, claim))

    if not eligible:
        return None
    eligible.sort(key=lambda item: item[0], reverse=True)
    return eligible[0][1]
