"""Typed records used by the fail-closed verification pipeline.

The legacy scraper is allowed to be noisy: it only creates candidates.  These
records are the boundary between candidate discovery and publication authority.
Only :class:`VerificationDecision` with ``decision == PUBLISH`` may reach X.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value


class EventType(StrEnum):
    TRANSFER = "TRANSFER"
    INJURY = "INJURY"
    SUSPENSION = "SUSPENSION"
    PRESS_CONFERENCE = "PRESS_CONFERENCE"
    MANAGER = "MANAGER"
    CONTRACT = "CONTRACT"
    OFFICIAL_STATEMENT = "OFFICIAL_STATEMENT"
    RUMOUR = "RUMOUR"
    OPINION = "OPINION"
    ANALYSIS = "ANALYSIS"
    PREVIEW = "PREVIEW"
    RECAP = "RECAP"
    INTERVIEW = "INTERVIEW"
    FEATURE = "FEATURE"
    EDITORIAL = "EDITORIAL"
    UNKNOWN = "UNKNOWN"


class EventStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    RUMOUR = "RUMOUR"
    INTEREST = "INTEREST"
    TALKS = "TALKS"
    NEGOTIATION = "NEGOTIATION"
    BID = "BID"
    AGREEMENT = "AGREEMENT"
    MEDICAL = "MEDICAL"
    HERE_WE_GO = "HERE_WE_GO"
    OFFICIAL = "OFFICIAL"
    COMPLETED = "COMPLETED"


class EntityType(StrEnum):
    PLAYER = "PLAYER"
    MANAGER = "MANAGER"
    COACH = "COACH"
    CLUB = "CLUB"
    COMPETITION = "COMPETITION"
    ORGANIZATION = "ORGANIZATION"
    UNKNOWN = "UNKNOWN"


class SourceKind(StrEnum):
    OFFICIAL_CLUB = "OFFICIAL_CLUB"
    OFFICIAL_LEAGUE = "OFFICIAL_LEAGUE"
    OFFICIAL_DATA = "OFFICIAL_DATA"
    GOVERNING_BODY = "GOVERNING_BODY"
    JOURNALIST = "JOURNALIST"
    SPECIALIST = "SPECIALIST"
    MEDIA = "MEDIA"
    AGGREGATOR = "AGGREGATOR"
    UNKNOWN = "UNKNOWN"


class DecisionType(StrEnum):
    PUBLISH = "PUBLISH"
    PENDING = "PENDING"
    REJECT = "REJECT"
    DUPLICATE = "DUPLICATE"


class GateState(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    WAIT = "WAIT"


class EvidenceSupport(StrEnum):
    TEXT_SPAN = "TEXT_SPAN"
    STRUCTURED_DATA = "STRUCTURED_DATA"
    ENTITY_REGISTRY = "ENTITY_REGISTRY"
    SOURCE_RELATION = "SOURCE_RELATION"


@dataclass(frozen=True)
class SourceProfile:
    id: str
    display_name: str
    handles: tuple[str, ...]
    domains: tuple[str, ...]
    publisher_group: str
    kind: SourceKind
    declared_sports: tuple[str, ...]
    official_for: tuple[str, ...]
    official_scope: tuple[str, ...]
    allowed_events: tuple[str, ...]
    allowed_milestones: tuple[str, ...]
    prior_mean: float
    prior_strength: float
    search_enabled: bool = False

    @property
    def is_official(self) -> bool:
        return self.kind in {
            SourceKind.OFFICIAL_CLUB,
            SourceKind.OFFICIAL_LEAGUE,
            SourceKind.OFFICIAL_DATA,
            SourceKind.GOVERNING_BODY,
        }


@dataclass(frozen=True)
class SourceIdentity:
    """Resolved publisher identity for one document.

    ``verified`` means the identity came from a domain/handle controlled by the
    configured publisher, not from a search query label or an article mentioning
    that publisher.  A display name is never sufficient provenance.
    """

    profile_id: str
    publisher_group: str
    kind: SourceKind
    verified: bool
    resolution: str
    domain: Optional[str] = None
    handle: Optional[str] = None


@dataclass
class RawDocument:
    id: str
    title: str
    body: str
    url: Optional[str]
    published_at: Optional[str]
    fetched_at: str
    source: SourceIdentity
    feed_id: Optional[str] = None
    declared_sport: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        title = (self.title or "").strip()
        body = (self.body or "").strip()
        if not body or body == title:
            return title
        return f"{title}. {body}"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["source"]["kind"] = self.source.kind.value
        return d


@dataclass(frozen=True)
class EntityRecord:
    id: str
    name: str
    entity_type: EntityType
    sport: str
    confidence: float
    aliases: tuple[str, ...] = ()
    club_id: Optional[str] = None
    active_premier_league: bool = False
    validation_source: str = "registry"


@dataclass
class Claim:
    id: str
    document: RawDocument
    article_category: EventType
    event_type: EventType
    status: EventStatus
    facts: Dict[str, Any]
    fact_support: Dict[str, List[Dict[str, str]]]
    subject_type: EntityType
    entity_certainty: float
    event_certainty: float
    sport_confidence: float
    sport_signals: List[str]
    league_relevant: bool
    extraction_method: str
    warnings: List[str] = field(default_factory=list)

    @property
    def source_id(self) -> str:
        return self.document.source.profile_id

    @property
    def publisher_group(self) -> str:
        return self.document.source.publisher_group

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["article_category"] = self.article_category.value
        d["event_type"] = self.event_type.value
        d["status"] = self.status.value
        d["subject_type"] = self.subject_type.value
        d["document"]["source"]["kind"] = self.document.source.kind.value
        return d


@dataclass(frozen=True)
class GateResult:
    name: str
    state: GateState
    reason: str
    critical: bool = True
    value: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d


@dataclass
class VerificationDecision:
    decision: DecisionType
    story_id: str
    family_id: str
    event_type: EventType
    status: EventStatus
    verified_facts: Dict[str, Any]
    source_ids: List[str]
    publisher_groups: List[str]
    gates: List[GateResult]
    reasons: List[str]
    confidence: float
    confidence_dimensions: Dict[str, float]
    evidence_document_ids: List[str]
    fingerprint: str
    source_url: Optional[str] = None
    rendered_text: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def may_publish(self) -> bool:
        return self.decision == DecisionType.PUBLISH and all(
            gate.state == GateState.PASS for gate in self.gates if gate.critical
        )

    def gate(self, name: str) -> Optional[GateResult]:
        return next((g for g in self.gates if g.name == name), None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.value,
            "story_id": self.story_id,
            "family_id": self.family_id,
            "event_type": self.event_type.value,
            "status": self.status.value,
            "verified_facts": self.verified_facts,
            "source_ids": self.source_ids,
            "publisher_groups": self.publisher_groups,
            "gates": [g.to_dict() for g in self.gates],
            "reasons": self.reasons,
            "confidence": self.confidence,
            "confidence_dimensions": self.confidence_dimensions,
            "evidence_document_ids": self.evidence_document_ids,
            "fingerprint": self.fingerprint,
            "source_url": self.source_url,
            "rendered_text": self.rendered_text,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "VerificationDecision":
        return cls(
            decision=DecisionType(raw["decision"]),
            story_id=raw["story_id"],
            family_id=raw["family_id"],
            event_type=EventType(raw["event_type"]),
            status=EventStatus(raw["status"]),
            verified_facts=dict(raw.get("verified_facts") or {}),
            source_ids=list(raw.get("source_ids") or []),
            publisher_groups=list(raw.get("publisher_groups") or []),
            gates=[
                GateResult(
                    name=g["name"],
                    state=GateState(g["state"]),
                    reason=g["reason"],
                    critical=bool(g.get("critical", True)),
                    value=g.get("value"),
                )
                for g in raw.get("gates", [])
            ],
            reasons=list(raw.get("reasons") or []),
            confidence=float(raw.get("confidence", 0.0)),
            confidence_dimensions=dict(raw.get("confidence_dimensions") or {}),
            evidence_document_ids=list(raw.get("evidence_document_ids") or []),
            fingerprint=raw["fingerprint"],
            source_url=raw.get("source_url"),
            rendered_text=raw.get("rendered_text"),
            created_at=raw.get("created_at") or utc_now_iso(),
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
