"""Grounded claim extraction for verification engine v2.

This module deliberately does not decide whether a story may publish.  It turns
one source document into one structured claim and records how every fact was
supported.  The publication policy is enforced later by ``engine.py``.

The existing regex parser is accepted only as a *candidate hint*.  A hinted event
or entity receives no credit unless it is independently grounded in this source
text, an official structured payload, an entity registry, or a verified
first-party source relationship.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .config import VerificationConfig
from .entities import EntityRegistry, normalize_entity_name
from .models import (
    Claim,
    EntityRecord,
    EntityType,
    EventStatus,
    EventType,
    EvidenceSupport,
    RawDocument,
)
from .source_registry import SourceRegistry
from src.entity_guard import classify_entity_detailed, _HARD_REJECT


_LEGACY_EVENT_MAP = {
    "transfer": EventType.TRANSFER,
    "player_transfer": EventType.TRANSFER,
    "loan": EventType.TRANSFER,
    "loan_option": EventType.TRANSFER,
    "free_transfer": EventType.TRANSFER,
    "release": EventType.TRANSFER,
    "injury": EventType.INJURY,
    "injury_update": EventType.INJURY,
    "return_from_injury": EventType.INJURY,
    "suspension": EventType.SUSPENSION,
    "manager": EventType.MANAGER,
    "managerial_change": EventType.MANAGER,
    "staff_appointment": EventType.MANAGER,
    "staff_departure": EventType.MANAGER,
    "renewal": EventType.CONTRACT,
    "stay": EventType.CONTRACT,
    "contract_extension": EventType.CONTRACT,
    "official_statement": EventType.OFFICIAL_STATEMENT,
}


def _norm_text(value: object) -> str:
    return normalize_entity_name(value)


def _compile_many(patterns: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


def _first_match(patterns: Sequence[re.Pattern[str]], text: str) -> Optional[re.Match[str]]:
    matches = [m for pattern in patterns if (m := pattern.search(text))]
    return min(matches, key=lambda m: m.start()) if matches else None


def _clause(text: str, start: int, end: int, max_chars: int = 260) -> str:
    left = max(text.rfind(".", 0, start), text.rfind(";", 0, start),
               text.rfind("\n", 0, start), text.rfind("!", 0, start),
               text.rfind("?", 0, start))
    right_candidates = [
        pos for mark in ".;\n!?"
        if (pos := text.find(mark, end)) >= 0
    ]
    right = min(right_candidates) if right_candidates else len(text)
    value = re.sub(r"\s+", " ", text[left + 1:right]).strip()
    if len(value) > max_chars:
        value = value[: max_chars - 1].rstrip() + "…"
    return value


def _safe_string(value: object) -> Optional[str]:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or None


@dataclass(frozen=True)
class Classification:
    event_type: EventType
    article_category: EventType
    status: EventStatus
    event_certainty: float
    status_evidence: Optional[str]
    warnings: tuple[str, ...]


class SemanticClassifier:
    """Config-driven event/category/status classifier with clause-level negation."""

    def __init__(self, config: VerificationConfig):
        self.config = config
        raw = config.classification
        self.primary_event_patterns = {
            EventType(name): _compile_many(patterns)
            for name, patterns in raw["event_patterns"].items()
        }
        self.event_patterns = dict(self.primary_event_patterns)
        self.status_patterns = {
            EventType(event): {
                EventStatus(status): _compile_many(patterns)
                for status, patterns in statuses.items()
            }
            for event, statuses in raw["status_patterns"].items()
        }
        self.ineligible_patterns = {
            EventType(name): _compile_many(patterns)
            for name, patterns in raw["ineligible_article_patterns"].items()
        }
        self.speculation_patterns = _compile_many(raw["speculation_patterns"])
        self.negation_patterns = _compile_many(raw["negation_patterns"])
        self.fact_patterns = {
            group: {
                value: _compile_many(patterns)
                for value, patterns in values.items()
            }
            for group, values in raw["fact_patterns"].items()
        }

    def classify(self, document: RawDocument, legacy_hint: Optional[str]) -> Classification:
        text = document.text
        title = document.title or ""
        hint = _LEGACY_EVENT_MAP.get((legacy_hint or "").lower(), EventType.UNKNOWN)
        warnings: List[str] = []

        title_events = {
            event for event, patterns in self.event_patterns.items()
            if _first_match(patterns, title)
        }
        all_events = {
            event for event, patterns in self.event_patterns.items()
            if _first_match(patterns, text)
        }
        primary_title_events = {
            event for event, patterns in self.primary_event_patterns.items()
            if _first_match(patterns, title)
        }

        if hint != EventType.UNKNOWN and hint in all_events:
            event = hint
            conflicting_primary = primary_title_events - {hint}
            if conflicting_primary and hint not in primary_title_events:
                # The headline's concrete event predicate conflicts with the
                # legacy hint; generic shared status words do not create a tie.
                event = EventType.UNKNOWN
                certainty = 0.0
                warnings.append("legacy_event_conflicts_with_headline")
            elif conflicting_primary:
                certainty = 0.80
                warnings.append("multiple_event_types_in_headline")
            else:
                certainty = 0.98
        elif len(title_events) == 1:
            event = next(iter(title_events))
            certainty = 0.97
            if hint not in {EventType.UNKNOWN, event}:
                warnings.append("legacy_event_overridden_by_headline")
        elif not title_events and len(all_events) == 1:
            event = next(iter(all_events))
            certainty = 0.95
        else:
            event = EventType.UNKNOWN
            certainty = 0.0
            if len(title_events) > 1 or len(all_events) > 1:
                warnings.append("ambiguous_event_type")
            else:
                warnings.append("event_not_grounded")

        status, status_evidence = self._status(event, text)

        category = event
        for ineligible, patterns in self.ineligible_patterns.items():
            if _first_match(patterns, title):
                category = ineligible
                warnings.append(f"ineligible_article_category:{ineligible.value}")
                break
        if event == EventType.UNKNOWN and category == EventType.UNKNOWN:
            category = EventType.UNKNOWN

        return Classification(
            event_type=event,
            article_category=category,
            status=status,
            event_certainty=certainty,
            status_evidence=status_evidence,
            warnings=tuple(warnings),
        )

    def _status(self, event: EventType, text: str) -> tuple[EventStatus, Optional[str]]:
        by_status = self.status_patterns.get(event, {})
        ranked = sorted(
            by_status,
            key=self.config.status_rank,
            reverse=True,
        )
        for status in ranked:
            match = _first_match(by_status[status], text)
            if not match:
                continue
            clause = _clause(text, match.start(), match.end())
            if _first_match(self.negation_patterns, clause):
                return EventStatus.UNKNOWN, clause
            if (
                self.config.status_rank(status)
                >= self.config.status_rank(EventStatus.AGREEMENT)
                and _first_match(self.speculation_patterns, clause)
            ):
                return EventStatus.RUMOUR, clause
            return status, clause
        return EventStatus.UNKNOWN, None

    def fact_value(self, group: str, text: str) -> tuple[Optional[str], Optional[str]]:
        for value, patterns in self.fact_patterns.get(group, {}).items():
            match = _first_match(patterns, text)
            if match:
                return value, _clause(text, match.start(), match.end())
        return None, None


class LegacyClaimAdapter:
    """Adapt one legacy candidate and one source document into a grounded claim."""

    def __init__(
        self,
        config: VerificationConfig,
        sources: SourceRegistry,
        entities: EntityRegistry,
    ) -> None:
        self.config = config
        self.sources = sources
        self.entities = entities
        self.classifier = SemanticClassifier(config)

    def extract(self, document: RawDocument, legacy_story: Mapping[str, Any]) -> Claim:
        classification = self.classifier.classify(
            document, _safe_string(legacy_story.get("event"))
        )
        event = classification.event_type
        profile = self.sources.get(document.source.profile_id)
        warnings = list(classification.warnings)
        facts: Dict[str, Any] = {}
        support: Dict[str, List[Dict[str, str]]] = {}

        def add_fact(
            key: str,
            value: Any,
            support_type: EvidenceSupport,
            evidence: str,
        ) -> None:
            if value is None or value == "":
                return
            facts[key] = value
            support.setdefault(key, []).append(
                {"type": support_type.value, "evidence": str(evidence)}
            )

        club_mentions = self.entities.find_mentions(
            document.text, {EntityType.CLUB}
        )
        from_club = self._resolve_club(
            legacy_story.get("from_key"), legacy_story.get("from_club")
        )
        to_club = self._resolve_club(
            legacy_story.get("to_key"), legacy_story.get("to_club")
        )
        if event == EventType.TRANSFER:
            grammatical_from, grammatical_to = self._dynamic_transfer_direction(
                document.text, club_mentions
            )
            if grammatical_from:
                if from_club and from_club.id != grammatical_from.id:
                    warnings.append("legacy_origin_overridden_by_grounded_grammar")
                from_club = grammatical_from
            if grammatical_to:
                if to_club and to_club.id != grammatical_to.id:
                    warnings.append("legacy_destination_overridden_by_grounded_grammar")
                to_club = grammatical_to

        # A verified official club account/domain is itself evidence of club
        # context for non-directional events. It is never enough to guess a
        # transfer destination because official selling clubs announce exits too.
        source_club = self._single_source_club(profile)
        if event in {
            EventType.INJURY,
            EventType.SUSPENSION,
            EventType.MANAGER,
            EventType.CONTRACT,
            EventType.OFFICIAL_STATEMENT,
        } and not (from_club or to_club):
            source_club = source_club if source_club and self.entities.get(source_club) else None
            if source_club:
                to_club = self.entities.get(source_club)
            elif len(club_mentions) == 1:
                to_club = club_mentions[0][1]

        subject_name = _safe_string(
            legacy_story.get("display_name") or legacy_story.get("player")
        )
        subject_type = self._expected_subject_type(event, legacy_story)
        subject = self._resolve_subject(subject_name, subject_type)
        if subject is None and subject_type == EntityType.PLAYER:
            player_mentions = self.entities.find_mentions(
                document.text, {EntityType.PLAYER}
            )
            if len(player_mentions) == 1:
                subject = player_mentions[0][1]
                subject_name = subject.name
                warnings.append("subject_recovered_from_dynamic_registry")
            elif len(player_mentions) > 1:
                warnings.append("multiple_registry_players_in_document")

        if subject is None and profile and profile.is_official:
            candidates = self._official_person_candidates(
                document.title, event, subject_type
            )
            if len(candidates) == 1:
                subject_name = candidates[0]
                warnings.append("subject_recovered_from_official_title")
            elif len(candidates) > 1:
                warnings.append("multiple_person_candidates_in_official_title")

        if subject_name and subject is None and self._may_establish_from_official(
            profile, event, classification.status, from_club, to_club, subject_name
        ):
            subject = self.entities.officially_established_person(subject_name, subject_type)
            if subject:
                warnings.append("entity_established_by_first_party_announcement")

        if subject:
            subject_span = self._ground_entity(document.text, subject, subject_name)
            if subject_span:
                add_fact("subject_id", subject.id, EvidenceSupport.TEXT_SPAN, subject_span)
                add_fact("subject_name", subject.name, EvidenceSupport.TEXT_SPAN, subject_span)
            else:
                warnings.append("subject_not_grounded_in_document")
        elif subject_name:
            warnings.append("subject_not_in_entity_registry")

        if event == EventType.TRANSFER:
            self._add_transfer_facts(
                document, legacy_story, profile, from_club, to_club,
                add_fact, warnings,
            )
        elif event in {EventType.INJURY, EventType.SUSPENSION, EventType.CONTRACT}:
            club = to_club or from_club
            if not club and subject and subject.club_id:
                club = self.entities.get(subject.club_id)
            self._add_single_club(club, document, profile, add_fact, warnings, subject)
        elif event == EventType.MANAGER:
            club = to_club or from_club
            self._add_single_club(club, document, profile, add_fact, warnings, subject)
        elif event == EventType.OFFICIAL_STATEMENT:
            club = to_club or from_club
            self._add_single_club(club, document, profile, add_fact, warnings, subject)

        if event == EventType.INJURY:
            injury_status = _safe_string(legacy_story.get("diagnosis"))
            evidence_type = EvidenceSupport.STRUCTURED_DATA if document.metadata.get("structured_official") else EvidenceSupport.TEXT_SPAN
            if injury_status and (
                document.metadata.get("structured_official")
                or _norm_text(injury_status) in _norm_text(document.text)
            ):
                add_fact("injury_status", injury_status, evidence_type, injury_status)
            elif classification.status_evidence:
                add_fact(
                    "injury_status", classification.status_evidence,
                    EvidenceSupport.TEXT_SPAN, classification.status_evidence,
                )
            self._add_optional_grounded(
                "return_date", legacy_story.get("expected_return"), document, add_fact
            )
        elif event == EventType.SUSPENSION:
            value = _safe_string(legacy_story.get("diagnosis")) or classification.status_evidence
            if value:
                add_fact(
                    "suspension_status", value,
                    EvidenceSupport.STRUCTURED_DATA if document.metadata.get("structured_official") else EvidenceSupport.TEXT_SPAN,
                    value,
                )
            self._add_optional_grounded(
                "suspension_length", legacy_story.get("suspension_length"), document, add_fact
            )
            self._add_optional_grounded(
                "return_date", legacy_story.get("expected_return"), document, add_fact
            )
        elif event == EventType.MANAGER:
            action = _safe_string(legacy_story.get("staff_action"))
            evidence = classification.status_evidence
            if action not in {"appointment", "departure"}:
                action, evidence = self.classifier.fact_value("manager_action", document.text)
            if action and evidence:
                add_fact("manager_action", action, EvidenceSupport.TEXT_SPAN, evidence)
            self._add_optional_grounded(
                "contract_length", legacy_story.get("contract"), document, add_fact
            )
        elif event == EventType.CONTRACT:
            if classification.status in {EventStatus.OFFICIAL, EventStatus.COMPLETED}:
                evidence = classification.status_evidence or document.title
                add_fact("contract_status", "extended", EvidenceSupport.TEXT_SPAN, evidence)
            self._add_optional_grounded(
                "contract_length", legacy_story.get("contract"), document, add_fact
            )
        elif event == EventType.OFFICIAL_STATEMENT:
            if document.title:
                add_fact(
                    "statement_topic", document.title.strip(),
                    EvidenceSupport.TEXT_SPAN, document.title.strip(),
                )

        event_time = legacy_story.get("event_time") or document.metadata.get("event_time")
        if event_time:
            if document.metadata.get("structured_official"):
                add_fact(
                    "event_time", str(event_time),
                    EvidenceSupport.STRUCTURED_DATA, str(event_time),
                )
            else:
                self._add_optional_grounded(
                    "event_time", event_time, document, add_fact
                )

        if self._league_relevant(subject, from_club, to_club, profile):
            add_fact(
                "competition_id", self.entities.competition_id,
                EvidenceSupport.ENTITY_REGISTRY, "active Premier League ecosystem",
            )

        entity_records = [x for x in (subject, from_club, to_club) if x]
        entity_certainty = min((x.confidence for x in entity_records), default=0.0)
        required_subject = event != EventType.OFFICIAL_STATEMENT
        if required_subject and not subject:
            entity_certainty = 0.0
        if event in self.config.publishable_categories and not any(
            x and x.entity_type == EntityType.CLUB for x in (from_club, to_club)
        ):
            entity_certainty = 0.0

        if subject and event in {EventType.INJURY, EventType.SUSPENSION, EventType.CONTRACT}:
            fact_club = facts.get("club_id")
            current = self.entities.player_current_club(subject.id)
            if current and fact_club and current != fact_club:
                warnings.append("subject_club_relationship_conflict")
                entity_certainty = 0.0

        sport_confidence, sport_signals = self._sport_confidence(
            document, profile, subject, from_club, to_club
        )
        league_relevant = self._league_relevant(subject, from_club, to_club, profile)

        status = classification.status
        if document.metadata.get("structured_official") and profile and profile.is_official:
            if event.value in profile.allowed_events:
                status = EventStatus.OFFICIAL

        claim_payload = "|".join(
            [document.id, event.value, status.value, str(sorted(facts.items()))]
        )
        claim_id = hashlib.sha256(claim_payload.encode("utf-8")).hexdigest()

        return Claim(
            id=claim_id,
            document=document,
            article_category=classification.article_category,
            event_type=event,
            status=status,
            facts=facts,
            fact_support=support,
            subject_type=subject.entity_type if subject else subject_type,
            entity_certainty=entity_certainty,
            event_certainty=classification.event_certainty,
            sport_confidence=sport_confidence,
            sport_signals=sport_signals,
            league_relevant=league_relevant,
            extraction_method="legacy_hint_plus_grounding_v2",
            warnings=warnings,
        )

    def _official_person_candidates(
        self,
        text: str,
        event: EventType,
        expected_type: EntityType,
    ) -> List[str]:
        """Find unambiguous person-shaped entities in an official headline.

        This is used only to establish a new entity from first-party evidence;
        it never runs for media or journalist claims.
        """
        pattern = re.compile(
            r"\b([A-ZÀ-ÖØ-Þ][a-zà-ÿ'’-]+(?:\s+(?:(?:van|de|da|dos|del|el|la|le|di|du|den|der|von)\s+)?"
            r"[A-ZÀ-ÖØ-Þ][a-zà-ÿ'’-]+)+)\b"
        )
        candidates: List[str] = []
        for candidate in pattern.findall(text or ""):
            if not document_name_is_person_like(candidate):
                continue
            entity_type, _ = classify_entity_detailed(candidate, text, None)
            if entity_type in _HARD_REJECT:
                continue
            if self.entities.resolve_club(candidate):
                continue
            if event == EventType.MANAGER and entity_type not in {
                "PLAYER", "MANAGER", "COACH", "ASSISTANT_COACH"
            }:
                continue
            candidates.append(candidate)
        return list(dict.fromkeys(candidates))

    def _dynamic_transfer_direction(
        self,
        text: str,
        mentions: Sequence[tuple[int, EntityRecord, str]],
    ) -> tuple[Optional[EntityRecord], Optional[EntityRecord]]:
        """Resolve direction from syntax over dynamically registered clubs."""
        normalized = _norm_text(text)
        origin = destination = None
        for position, club, alias in mentions:
            before = normalized[:position]
            after = normalized[position + len(alias):]
            if re.search(r"\bfrom\s+$", before[-40:]):
                origin = club
                continue
            if re.search(
                r"\b(?:join|joins|joined|joining|sign for|signs for|signed for|"
                r"move to|moves to|moved to|loan to|to)\s+$",
                before[-60:],
            ):
                destination = club
                continue
            if re.match(
                r"\s*(?:have |has )?(?:sign|signs|signed|announce|announces|"
                r"announced|complete|completes|completed)\b",
                after[:80],
            ):
                destination = club
        if destination and origin and destination.id == origin.id:
            return None, None
        return origin, destination

    def _resolve_club(self, *values: object) -> Optional[EntityRecord]:
        for value in values:
            if not value:
                continue
            candidates = [str(value), str(value).replace("_", " ")]
            for candidate in candidates:
                if record := self.entities.resolve_club(candidate):
                    return record
        return None

    def _expected_subject_type(
        self, event: EventType, story: Mapping[str, Any]
    ) -> EntityType:
        if event == EventType.MANAGER:
            role = str(story.get("staff_role") or "").lower()
            return EntityType.COACH if role and "manager" not in role and "head coach" not in role else EntityType.MANAGER
        if event == EventType.OFFICIAL_STATEMENT:
            return EntityType.CLUB
        return EntityType.PLAYER

    def _resolve_subject(
        self, name: Optional[str], expected: EntityType
    ) -> Optional[EntityRecord]:
        if not name:
            return None
        if expected == EntityType.PLAYER:
            return self.entities.resolve_player(name)
        if expected in {EntityType.MANAGER, EntityType.COACH}:
            return self.entities.resolve_staff(name)
        return self.entities.resolve(name, expected)

    def _ground_entity(
        self, text: str, record: EntityRecord, original: Optional[str]
    ) -> Optional[str]:
        normalized_text = f" {_norm_text(text)} "
        candidates = [original, record.name, *record.aliases]
        for candidate in candidates:
            normalized = _norm_text(candidate)
            if normalized and f" {normalized} " in normalized_text:
                return str(candidate)
        return None

    def _single_source_club(self, profile: Any) -> Optional[str]:
        if not profile or not profile.is_official:
            return None
        clubs = [entity_id for entity_id in profile.official_for if entity_id.startswith("club:")]
        resolved = list(dict.fromkeys(
            entity_id for entity_id in clubs if self.entities.get(entity_id)
        ))
        active = [entity_id for entity_id in resolved if self.entities.active_premier_league_club(entity_id)]
        if len(active) == 1:
            return active[0]
        return resolved[0] if len(resolved) == 1 else None

    def _source_relates_to_club(self, profile: Any, club: Optional[EntityRecord]) -> bool:
        if not profile or not profile.is_official or not club:
            return False
        if club.id in profile.official_for:
            return True
        return (
            "premier_league_ecosystem" in profile.official_scope
            and club.active_premier_league
        )

    def _may_establish_from_official(
        self,
        profile: Any,
        event: EventType,
        status: EventStatus,
        from_club: Optional[EntityRecord],
        to_club: Optional[EntityRecord],
        name: str,
    ) -> bool:
        if not profile or not profile.is_official or not document_name_is_person_like(name):
            return False
        entity_type, _ = classify_entity_detailed(name, name, None)
        if entity_type in _HARD_REJECT:
            return False
        if event.value not in profile.allowed_events:
            return False
        if status not in {EventStatus.OFFICIAL, EventStatus.COMPLETED}:
            return False
        if self._source_relates_to_club(profile, from_club) or self._source_relates_to_club(profile, to_club):
            return True
        return "premier_league_ecosystem" in profile.official_scope and bool(from_club or to_club)

    def _add_transfer_facts(
        self,
        document: RawDocument,
        story: Mapping[str, Any],
        profile: Any,
        from_club: Optional[EntityRecord],
        to_club: Optional[EntityRecord],
        add_fact: Any,
        warnings: List[str],
    ) -> None:
        if from_club:
            evidence = self._ground_entity(
                document.text, from_club,
                _safe_string(story.get("from_club") or story.get("from_key")),
            )
            if evidence:
                add_fact("club_from_id", from_club.id, EvidenceSupport.TEXT_SPAN, evidence)
                add_fact("club_from_name", from_club.name, EvidenceSupport.TEXT_SPAN, evidence)
        if to_club:
            evidence = self._ground_entity(
                document.text, to_club,
                _safe_string(story.get("to_club") or story.get("to_key")),
            )
            if evidence:
                add_fact("club_to_id", to_club.id, EvidenceSupport.TEXT_SPAN, evidence)
                add_fact("club_to_name", to_club.name, EvidenceSupport.TEXT_SPAN, evidence)
            elif self._source_relates_to_club(profile, to_club):
                add_fact("club_to_id", to_club.id, EvidenceSupport.SOURCE_RELATION, profile.id)
                add_fact("club_to_name", to_club.name, EvidenceSupport.SOURCE_RELATION, profile.id)
            else:
                warnings.append("destination_not_grounded_in_document")

        kind, evidence = self.classifier.fact_value("transfer_kind", document.text)
        legacy = str(story.get("event") or "").lower()
        if legacy in {"loan", "loan_option"} and kind is None:
            kind = "loan"
            evidence = "legacy loan event grounded by event classifier"
        if kind and evidence:
            add_fact("transfer_kind", kind, EvidenceSupport.TEXT_SPAN, evidence)

        self._add_optional_grounded("fee", story.get("fee"), document, add_fact)
        self._add_optional_grounded(
            "contract_length", story.get("contract"), document, add_fact
        )

    def _add_single_club(
        self,
        club: Optional[EntityRecord],
        document: RawDocument,
        profile: Any,
        add_fact: Any,
        warnings: List[str],
        subject: Optional[EntityRecord] = None,
    ) -> None:
        if not club:
            warnings.append("club_not_resolved")
            return
        evidence = self._ground_entity(document.text, club, club.name)
        if evidence:
            support_type = EvidenceSupport.TEXT_SPAN
            support_value = evidence
        elif self._source_relates_to_club(profile, club):
            support_type = EvidenceSupport.SOURCE_RELATION
            support_value = profile.id
        elif subject and self.entities.relationship_valid(subject.id, club.id):
            support_type = EvidenceSupport.ENTITY_REGISTRY
            support_value = "official current player-club registry"
        else:
            warnings.append("club_not_grounded_in_document")
            return
        add_fact("club_id", club.id, support_type, support_value)
        add_fact("club_name", club.name, support_type, support_value)

    def _add_optional_grounded(
        self, key: str, value: object, document: RawDocument, add_fact: Any
    ) -> None:
        text = _safe_string(value)
        if not text:
            return
        if _norm_text(text) in _norm_text(document.text):
            add_fact(key, text, EvidenceSupport.TEXT_SPAN, text)

    def _league_relevant(
        self,
        subject: Optional[EntityRecord],
        from_club: Optional[EntityRecord],
        to_club: Optional[EntityRecord],
        profile: Any,
    ) -> bool:
        if any(
            club and self.entities.active_premier_league_club(club.id)
            for club in (from_club, to_club)
        ):
            return True
        if subject and subject.club_id and self.entities.active_premier_league_club(subject.club_id):
            return True
        return bool(profile and "premier_league_ecosystem" in profile.official_scope)

    def _sport_confidence(
        self,
        document: RawDocument,
        profile: Any,
        subject: Optional[EntityRecord],
        from_club: Optional[EntityRecord],
        to_club: Optional[EntityRecord],
    ) -> tuple[float, List[str]]:
        signals: List[str] = []
        if (document.declared_sport or "").lower() == "football":
            signals.append("feed_metadata")
        if profile and "football" in profile.declared_sports:
            signals.append("source_scope")
        if subject and subject.sport == "football":
            signals.append("subject_registry")
        if any(club and club.sport == "football" for club in (from_club, to_club)):
            signals.append("club_registry")
        # Competition is an independent signal only when the document/source
        # explicitly establishes it. Do not derive it from the same club match
        # already counted above (that correlation caused SunRisers Leeds to look
        # like two separate football signals).
        if (
            document.metadata.get("competition_id") == self.entities.competition_id
            or (profile and "premier_league_ecosystem" in profile.official_scope)
        ):
            signals.append("competition_registry")
        if document.metadata.get("structured_official"):
            signals.append("structured_official_data")
        signals = list(dict.fromkeys(signals))
        weights = self.config.sport_signal_weights
        probability_not = 1.0
        for signal in signals:
            probability_not *= 1.0 - weights.get(signal, 0.0)
        return round(1.0 - probability_not, 6), signals


def document_name_is_person_like(name: str) -> bool:
    """Structural check used only after a verified official source is present."""
    normalized = normalize_entity_name(name)
    parts = normalized.split()
    if len(parts) < 2 or len(parts) > 6:
        return False
    return all(part.isalpha() and len(part) >= 2 for part in parts)
