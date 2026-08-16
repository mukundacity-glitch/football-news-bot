"""Fact-level consensus and contradiction detection."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence

from .config import EventPolicy, VerificationConfig
from .models import Claim, EventStatus, EventType
from .reliability import SourceReliabilityModel


@dataclass(frozen=True)
class ConsensusResult:
    conflict_fields: Dict[str, Dict[str, List[str]]]
    fact_agreement: float
    cross_source_agreement: float
    independent_publishers: tuple[str, ...]
    credible_claims: tuple[Claim, ...]
    agreed_facts: Dict[str, Any]
    fact_sources: Dict[str, List[str]]

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflict_fields)


class FactConsensusEngine:
    def __init__(
        self,
        config: VerificationConfig,
        reliability: SourceReliabilityModel,
    ) -> None:
        self.config = config
        self.reliability = reliability

    def compare(
        self,
        claims: Iterable[Claim],
        event_type: EventType,
        authoritative_claims: Sequence[Claim],
    ) -> ConsensusResult:
        policy = self.config.event_policy(event_type)
        min_reliability = self.config.threshold("conflict_source_reliability_min")
        min_status = EventStatus(self.config.policy("conflict_minimum_status"))
        min_rank = self.config.status_rank(min_status)

        eligible: List[Claim] = []
        for claim in claims:
            if claim.event_type != event_type or not claim.document.source.verified:
                continue
            if self.config.status_rank(claim.status) < min_rank:
                continue
            if self.reliability.evaluate(claim.source_id).score < min_reliability:
                continue
            eligible.append(claim)

        # One claim per publisher organization prevents syndicated duplicates or
        # repeated URLs from manufacturing independence.
        by_group: Dict[str, Claim] = {}
        for claim in eligible:
            existing = by_group.get(claim.publisher_group)
            if existing is None or self.config.status_rank(claim.status) > self.config.status_rank(existing.status):
                by_group[claim.publisher_group] = claim
        credible = list(by_group.values())

        conflicts: Dict[str, Dict[str, List[str]]] = {}
        for field in policy.conflict_fields:
            values: Dict[str, List[str]] = {}
            stated_claims = [
                claim for claim in credible
                if field in claim.facts and claim.facts[field] not in (None, "")
            ]
            stated_claims = self._same_progression_window(
                stated_claims, field, policy
            )
            for claim in stated_claims:
                normalized = normalize_fact(claim.facts[field])
                values.setdefault(normalized, []).append(claim.source_id)
            if len(values) > 1:
                conflicts[field] = values

        # Verified facts are authoritative facts only.  When more than one
        # authoritative source exists, a field is included only when all official
        # claims that state it agree. Missing optional fields are simply omitted.
        agreed_facts: Dict[str, Any] = {}
        fact_sources: Dict[str, List[str]] = {}
        all_fields = set().union(*(c.facts.keys() for c in authoritative_claims)) if authoritative_claims else set()
        for field in all_fields:
            stated = [c for c in authoritative_claims if c.facts.get(field) not in (None, "")]
            stated = self._same_progression_window(stated, field, policy)
            if not stated:
                continue
            values = {normalize_fact(c.facts[field]) for c in stated}
            if len(values) == 1:
                agreed_facts[field] = stated[0].facts[field]
                fact_sources[field] = list(dict.fromkeys(c.source_id for c in stated))

        comparable = agreeing = 0
        if authoritative_claims:
            authority = authoritative_claims[0]
            for claim in credible:
                if claim.source_id == authority.source_id:
                    continue
                overlap = set(policy.conflict_fields) & set(authority.facts) & set(claim.facts)
                for field in overlap:
                    comparable += 1
                    if normalize_fact(authority.facts[field]) == normalize_fact(claim.facts[field]):
                        agreeing += 1
        cross_agreement = 1.0 if comparable == 0 else agreeing / comparable
        fact_agreement = 0.0 if conflicts else 1.0

        return ConsensusResult(
            conflict_fields=conflicts,
            fact_agreement=round(fact_agreement, 6),
            cross_source_agreement=round(cross_agreement, 6),
            independent_publishers=tuple(sorted(by_group)),
            credible_claims=tuple(credible),
            agreed_facts=agreed_facts,
            fact_sources=fact_sources,
        )

    def _same_progression_window(
        self,
        claims: Sequence[Claim],
        field: str,
        policy: EventPolicy,
    ) -> List[Claim]:
        if field not in policy.progressive_fields or len(claims) < 2:
            return list(claims)
        dated = [(claim, _parse_time(claim.document.published_at)) for claim in claims]
        known = [dt for _, dt in dated if dt is not None]
        if not known:
            return list(claims)
        latest = max(known)
        window = timedelta(
            hours=self.config.threshold("progression_same_milestone_window_hours")
        )
        # Unknown timestamps remain in the comparison and therefore cannot be
        # used to hide a conflict. Older, clearly dated status is superseded.
        return [
            claim for claim, dt in dated
            if dt is None or latest - dt <= window
        ]


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        dt = parsedate_to_datetime(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def normalize_fact(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()
