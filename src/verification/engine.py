"""Fail-closed publication decision engine.

No score, source reputation, keyword, or source count can bypass a failed critical
validator.  Weighted confidence is calculated for audit/ranking only after the
critical gates have been evaluated.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .config import VerificationConfig
from .consensus import FactConsensusEngine, ConsensusResult, normalize_fact
from .entities import EntityRegistry
from .models import (
    Claim,
    DecisionType,
    EntityType,
    EventStatus,
    EventType,
    GateResult,
    GateState,
    VerificationDecision,
)
from .reliability import SourceReliabilityModel
from .repository import RepositoryError, VerificationRepository
from .source_registry import SourceRegistry


class VerificationEngine:
    def __init__(
        self,
        config: VerificationConfig,
        sources: SourceRegistry,
        entities: EntityRegistry,
        repository: VerificationRepository,
    ) -> None:
        self.config = config
        self.sources = sources
        self.entities = entities
        self.repository = repository
        self.reliability = SourceReliabilityModel(config, sources, repository)
        self.consensus = FactConsensusEngine(config, self.reliability)
        self.repository.register_sources(sources.all())

    def verify(
        self,
        claims: Sequence[Claim],
        *,
        now: Optional[datetime] = None,
    ) -> VerificationDecision:
        now = now or datetime.now(timezone.utc)
        now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        for claim in claims:
            self.repository.record_claim(claim)

        event, event_claims, event_ambiguity = self._select_event(claims)
        if not event_ambiguity and event in self.config.publishable_categories:
            provisional_family, _ = self._story_ids(event, {}, event_claims)
            since = (
                now - timedelta(days=self.config.threshold("active_story_window_days"))
            ).isoformat()
            historical = self.repository.claims_for_family(
                provisional_family, observed_since=since
            )
            merged = {claim.id: claim for claim in [*historical, *claims]}
            claims = list(merged.values())
            event_claims = [c for c in claims if c.event_type == event]
        status = self._highest_status(event_claims)
        policy = self.config.event_policy(event) if event in self.config.publishable_categories else None
        official_claims = self._authoritative_claims(event_claims, event, status)
        authoritative = list(official_claims)
        confirmation_kind = "first_party_official" if official_claims else "none"
        if not authoritative and policy:
            authoritative, confirmation_kind = self._configured_nonofficial_confirmation(
                event_claims, event
            )
        if authoritative:
            status = self._highest_status(authoritative)
        consensus = (
            self.consensus.compare(event_claims, event, authoritative)
            if policy else self._empty_consensus()
        )
        confirmation_ready = bool(authoritative)
        if official_claims and (
            self.config.policy("require_corroboration_with_official")
            or not self.config.policy("official_first_party_sufficient")
        ):
            confirmation_ready = (
                len(consensus.independent_publishers)
                >= int(self.config.policy("minimum_independent_publishers"))
            )

        provisional_facts = consensus.agreed_facts
        family_id, story_id = self._story_ids(event, provisional_facts, event_claims)
        fingerprint = self._fingerprint(event, status, provisional_facts, policy)
        gates: List[GateResult] = []

        gates.append(GateResult(
            "configuration_health", GateState.PASS, "validated schema v2"
        ))
        gates.append(GateResult(
            "entity_registry_health",
            GateState.PASS if self.entities.healthy else GateState.WAIT,
            self.entities.health_reason,
        ))

        # Article/event gates evaluate all candidate claims, while publication
        # authority and mandatory facts evaluate only first-party claims.
        if event_ambiguity:
            gates.append(GateResult(
                "event_classification", GateState.FAIL,
                "conflicting or missing event classifications",
                value=0.0,
            ))
        else:
            event_certainty = max(
                (claim.event_certainty for claim in event_claims), default=0.0
            )
            state = (
                GateState.PASS
                if event in self.config.publishable_categories
                and event_certainty >= self.config.threshold("event_certainty_min")
                else GateState.FAIL
            )
            gates.append(GateResult(
                "event_classification", state,
                f"event={event.value}; certainty={event_certainty:.3f}",
                value=event_certainty,
            ))

        eligible_category_claims = [
            c for c in event_claims if c.article_category == event
        ]
        gates.append(GateResult(
            "article_category",
            GateState.PASS if eligible_category_claims else GateState.FAIL,
            "eligible news category" if eligible_category_claims else "rumour/opinion/analysis/preview/recap/interview/feature/editorial or unknown",
        ))

        valid_entity_claims = [
            c for c in event_claims
            if c.entity_certainty >= self.config.threshold("entity_certainty_min")
            and policy
            and c.subject_type.value in policy.allowed_subject_types
            and not any("relationship_conflict" in w for w in c.warnings)
        ]
        gates.append(GateResult(
            "entity_validation",
            GateState.PASS if valid_entity_claims else GateState.FAIL,
            "canonical entities and relationships validated" if valid_entity_claims else "mandatory person/club identity or relationship is unresolved",
            value=max((c.entity_certainty for c in event_claims), default=0.0),
        ))

        sport_valid = [
            c for c in event_claims
            if c.sport_confidence >= self.config.threshold("sport_confidence_min")
            and len(set(c.sport_signals)) >= int(self.config.threshold("sport_independent_signals_min"))
        ]
        gates.append(GateResult(
            "sport_validation",
            GateState.PASS if sport_valid else GateState.FAIL,
            (
                "positive football evidence from independent signal classes"
                if sport_valid else
                "football was not positively established"
            ),
            value=max((c.sport_confidence for c in event_claims), default=0.0),
        ))

        league_valid = any(c.league_relevant for c in event_claims)
        gates.append(GateResult(
            "league_validation",
            GateState.PASS if league_valid else GateState.FAIL,
            "Premier League ecosystem validated" if league_valid else "no active Premier League relationship",
            value=1.0 if league_valid else 0.0,
        ))

        provenance_state = GateState.PASS if authoritative else GateState.WAIT
        gates.append(GateResult(
            "source_provenance",
            provenance_state,
            "verified configured publisher identity" if authoritative else "no verified authoritative source yet",
        ))
        source_url_present = any(claim.document.url for claim in authoritative)
        require_url = bool(self.config.policy("require_source_url"))
        gates.append(GateResult(
            "source_url",
            GateState.PASS if (source_url_present or not require_url) else GateState.WAIT,
            "authoritative source URL captured" if source_url_present else "authoritative source URL missing",
        ))

        status_state = (
            GateState.PASS if confirmation_ready and status in self.config.publishable_statuses
            else GateState.WAIT
        )
        gates.append(GateResult(
            "event_status",
            status_state,
            f"status={status.value}; publishable={status in self.config.publishable_statuses}",
        ))

        official_state = GateState.PASS if confirmation_ready else GateState.WAIT
        confirmation_reason = {
            "first_party_official": "first-party official confirmation",
            "configured_here_we_go": "configured elite milestone with independent fact agreement",
            "configured_nonofficial": "configured multi-publisher confirmation",
            "configured_elite_medical": "elite source medical/deal-agreed transfer milestone",
            "configured_structured_fotmob": "structured FotMob completed transfer row",
            "none": "media/journalist evidence remains pending",
        }[confirmation_kind]
        if authoritative and not confirmation_ready:
            confirmation_reason = "official claim is awaiting configured independent corroboration"
        gates.append(GateResult(
            "official_confirmation",
            official_state,
            confirmation_reason,
            value=(1.0 if confirmation_kind == "first_party_official" and confirmation_ready
                   else 0.95 if confirmation_ready else 0.0),
        ))

        missing = []
        unsupported = []
        if policy:
            for field in policy.required_facts:
                if provisional_facts.get(field) in (None, ""):
                    missing.append(field)
                elif not any(field in claim.fact_support for claim in authoritative):
                    unsupported.append(field)
        gates.append(GateResult(
            "mandatory_facts",
            GateState.PASS if policy and not missing and not unsupported else GateState.WAIT,
            (
                "all mandatory facts grounded"
                if policy and not missing and not unsupported
                else f"missing={missing}; unsupported={unsupported}"
            ),
        ))

        gates.append(GateResult(
            "fact_consensus",
            GateState.PASS if not consensus.has_conflict else GateState.WAIT,
            "no credible fact conflicts" if not consensus.has_conflict else f"conflicts={consensus.conflict_fields}",
            value=consensus.fact_agreement,
        ))

        temporal_ok, temporal_reason, freshness = self._temporal(authoritative, now)
        gates.append(GateResult(
            "temporal_consistency",
            GateState.PASS if temporal_ok else GateState.WAIT,
            temporal_reason,
            value=1.0 if temporal_ok else 0.0,
        ))

        authority_reliabilities = [
            self.reliability.evaluate(c.source_id).score for c in authoritative
        ]
        source_reliability = min(authority_reliabilities, default=0.0)
        gates.append(GateResult(
            "source_reliability",
            GateState.PASS if source_reliability >= self.config.threshold("source_reliability_min") else GateState.WAIT,
            f"minimum authoritative reliability={source_reliability:.3f}",
            value=source_reliability,
        ))

        db_state, progression_state, db_reason = self._database_gates(
            family_id, fingerprint, event, status, provisional_facts, policy, now
        )
        gates.append(GateResult(
            "database_consistency", db_state, db_reason,
            value=1.0 if db_state == GateState.PASS else 0.0,
        ))
        gates.append(GateResult(
            "story_progression", progression_state, db_reason,
            value=1.0 if progression_state == GateState.PASS else 0.0,
        ))

        dimensions = {
            "source_reliability": source_reliability,
            "entity_certainty": max((c.entity_certainty for c in authoritative), default=0.0),
            "event_certainty": max((c.event_certainty for c in authoritative), default=0.0),
            "fact_agreement": consensus.fact_agreement,
            "temporal_consistency": 1.0 if temporal_ok else 0.0,
            "cross_source_agreement": consensus.cross_source_agreement,
            "official_confirmation": (
                1.0 if confirmation_kind == "first_party_official" and confirmation_ready
                else 0.95 if confirmation_ready else 0.0
            ),
            "league_validation": 1.0 if league_valid else 0.0,
            "sport_validation": max((c.sport_confidence for c in authoritative), default=0.0),
            "story_freshness": freshness,
            "database_consistency": 1.0 if db_state == GateState.PASS else 0.0,
        }
        confidence = weighted_geometric_mean(
            dimensions, self.config.confidence_weights
        )
        confidence_gate = GateResult(
            "overall_confidence",
            GateState.PASS if confidence >= self.config.threshold("overall_confidence_min") else GateState.WAIT,
            f"noncompensatory weighted confidence={confidence:.3f}",
            value=confidence,
        )
        gates.append(confidence_gate)

        non_database_failures = [
            g for g in gates
            if g.critical and g.state == GateState.FAIL
            and g.name not in {"database_consistency", "story_progression"}
        ]
        if non_database_failures:
            decision_type = DecisionType.REJECT
        elif progression_state == GateState.FAIL or db_state == GateState.FAIL:
            decision_type = DecisionType.DUPLICATE
        elif all(g.state == GateState.PASS for g in gates if g.critical):
            decision_type = DecisionType.PUBLISH
        else:
            decision_type = DecisionType.PENDING

        source_ids = list(dict.fromkeys(
            [*(c.source_id for c in authoritative), *(c.source_id for c in event_claims)]
        ))
        publisher_groups = list(dict.fromkeys(
            [*(c.publisher_group for c in authoritative), *(c.publisher_group for c in event_claims)]
        ))
        reasons = [
            f"{g.name}:{g.reason}" for g in gates if g.state != GateState.PASS
        ]
        source_url = next(
            (c.document.url for c in authoritative if c.document.url), None
        )
        decision = VerificationDecision(
            decision=decision_type,
            story_id=story_id,
            family_id=family_id,
            event_type=event,
            status=status,
            verified_facts=provisional_facts,
            source_ids=source_ids,
            publisher_groups=publisher_groups,
            gates=gates,
            reasons=reasons,
            confidence=confidence,
            confidence_dimensions=dimensions,
            evidence_document_ids=list(dict.fromkeys(c.document.id for c in event_claims)),
            fingerprint=fingerprint,
            source_url=source_url,
        )
        self.repository.upsert_story(decision, claims)
        self.repository.record_decision(decision)
        if official_claims:
            self._update_source_outcomes(decision, event_claims, official_claims, policy)
        return decision

    def _select_event(
        self, claims: Sequence[Claim]
    ) -> tuple[EventType, List[Claim], bool]:
        threshold = self.config.threshold("event_certainty_min")
        eligible = [
            c for c in claims
            if c.event_type in self.config.publishable_categories
            and c.event_certainty >= threshold
        ]
        events = {c.event_type for c in eligible}
        if len(events) != 1:
            return EventType.UNKNOWN, list(claims), True
        event = next(iter(events))
        return event, [c for c in claims if c.event_type == event], False

    def _highest_status(self, claims: Sequence[Claim]) -> EventStatus:
        return max(
            (c.status for c in claims),
            key=self.config.status_rank,
            default=EventStatus.UNKNOWN,
        )

    def _authoritative_claims(
        self,
        claims: Sequence[Claim],
        event: EventType,
        aggregate_status: EventStatus,
    ) -> List[Claim]:
        result = []
        for claim in claims:
            profile = self.sources.get(claim.source_id)
            if not profile or not profile.is_official:
                continue
            if not claim.document.source.verified:
                continue
            if event.value not in profile.allowed_events:
                continue
            if claim.article_category != event:
                continue
            if claim.status not in self.config.publishable_statuses:
                continue
            if not self._official_relation(profile, claim, event):
                continue
            result.append(claim)
        return result

    def _configured_nonofficial_confirmation(
        self, claims: Sequence[Claim], event: EventType
    ) -> tuple[List[Claim], str]:
        """Optional policies, disabled by default in the shipped config.

        HERE_WE_GO requires at least one source explicitly configured for that
        milestone plus independent high-status support. Generic non-official
        confirmation requires the separately configured publisher minimum.

        NON-NEGOTIABLE HARD GATE: a TRANSFER or PRESS_CONFERENCE can NEVER be
        published from this path, no matter what config/verification.json
        says. Only a first-party official source (club/league website,
        official league data feed, or a verified official club account
        explicitly stating the move is complete / hosting the press
        conference) may make either of these authoritative — see
        ``_authoritative_claims``. Journalists, "here we go" posts, deal-agreed
        reports, medical bulletins, structured third-party tables (e.g.
        FotMob), and media outlets merely quoting a press conference they
        attended are explicitly excluded from ever becoming publication
        authority for these two categories. This check is intentionally
        hardcoded, not config-driven, so a config edit alone can never reopen
        this hole.
        """
        threshold = self.config.threshold("source_reliability_min")
        eligible = []
        for claim in claims:
            profile = self.sources.get(claim.source_id)
            if not profile or profile.is_official or not claim.document.source.verified:
                continue
            if claim.article_category != event or not claim.league_relevant:
                continue
            if self.reliability.evaluate(claim.source_id).score < threshold:
                continue
            eligible.append(claim)

        by_group = {claim.publisher_group: claim for claim in eligible}
        independent = list(by_group.values())
        if self.config.policy("allow_here_we_go"):
            milestone = [
                claim for claim in independent
                if claim.status == EventStatus.HERE_WE_GO
                and "HERE_WE_GO" in self.sources.require(claim.source_id).allowed_milestones
            ]
            support = [
                claim for claim in independent
                if self.config.status_rank(claim.status)
                >= self.config.status_rank(EventStatus.AGREEMENT)
            ]
            if milestone and len(support) >= int(self.config.policy("minimum_here_we_go_publishers")):
                return support, "configured_here_we_go"

        # Structured FotMob transfer table: the user relies on this source for
        # complete same-day transfer coverage. FotMob can become publication
        # authority only for completed TRANSFER rows that arrived through the
        # structured table collector, and still must pass player identity,
        # from/to club grounding, PL relevance, freshness, reliability,
        # duplicate and conflict gates below.
        if event == EventType.TRANSFER and self.config.policy("allow_structured_fotmob_completed_transfers"):
            support = [
                claim for claim in independent
                if claim.source_id == "media.fotmob"
                and claim.status == EventStatus.COMPLETED
                and claim.document.metadata.get("structured_fotmob_transfer") is True
            ]
            if support:
                return support, "configured_structured_fotmob"

        # Optional fast transfer mode: allow elite sources to publish MEDICAL /
        # DEAL AGREED transfer milestones. This never labels the card/caption as
        # a completed transfer; renderer uses MEDICAL or DEAL AGREED based on
        # status. It is limited to TRANSFER and still requires PL relevance,
        # source URL, mandatory facts, freshness, and duplicate gates.
        #
        # "Elite" is enforced, not assumed: a source qualifies only if it is
        # explicitly configured for that milestone via allowed_milestones. The
        # reliability threshold alone is not a proxy for it — nearly every
        # configured publisher clears that bar, which would let a single
        # secondary outlet publish an unconfirmed transfer. The publisher
        # minimum matches the sibling paths for the same reason.
        if event == EventType.TRANSFER and self.config.policy("allow_elite_medical_transfer_confirmation"):
            allowed = set()
            for value in self.config.policy("elite_medical_transfer_statuses"):
                try:
                    allowed.add(EventStatus(value))
                except ValueError:
                    # An unrecognised status in config must not publish and
                    # must not abort verification — ignore it.
                    continue
            support = [
                claim for claim in independent
                if claim.status in allowed
                and claim.status.value
                in self.sources.require(claim.source_id).allowed_milestones
            ]
            minimum = int(self.config.policy("minimum_elite_medical_publishers"))
            if len(support) >= minimum:
                return support, "configured_elite_medical"

        if self.config.policy("allow_non_official_confirmation"):
            support = [
                claim for claim in independent
                if claim.status in {EventStatus.OFFICIAL, EventStatus.COMPLETED}
            ]
            if len(support) >= int(self.config.policy("minimum_independent_publishers")):
                return support, "configured_nonofficial"
        return [], "none"

    def _official_relation(self, profile: Any, claim: Claim, event: EventType) -> bool:
        policy = self.config.event_policy(event)
        relation_ids = {
            str(claim.facts.get(field))
            for field in policy.official_relation_fields
            if claim.facts.get(field)
        }
        if relation_ids & set(profile.official_for):
            return True
        if (
            "premier_league_ecosystem" in profile.official_scope
            and claim.league_relevant
            and claim.facts.get("competition_id") == self.entities.competition_id
        ):
            return True
        if "english_football" in profile.official_scope and claim.league_relevant:
            return True
        if "football" in profile.official_scope and claim.facts.get("competition_id"):
            return True
        return False

    def _temporal(
        self, authoritative: Sequence[Claim], now: datetime
    ) -> tuple[bool, str, float]:
        if not authoritative:
            return False, "awaiting authoritative timestamp", 0.0
        max_age = self.config.threshold("max_confirmation_age_hours")
        future_skew = timedelta(
            minutes=self.config.threshold("max_future_clock_skew_minutes")
        )
        ages = []
        for claim in authoritative:
            parsed = parse_timestamp(claim.document.published_at)
            if parsed is None:
                return False, "authoritative publication timestamp missing/unparseable", 0.0
            if parsed > now + future_skew:
                return False, "authoritative timestamp is in the future", 0.0
            age_hours = (now - parsed).total_seconds() / 3600
            if age_hours < 0:
                age_hours = 0.0
            if age_hours > max_age:
                return False, f"official confirmation is {age_hours:.1f}h old", 0.0
            event_time = parse_timestamp(claim.facts.get("event_time"))
            if event_time is not None:
                event_age = (now - event_time).total_seconds() / 3600
                if event_time > now + future_skew:
                    return False, "event timestamp is in the future", 0.0
                if event_age > self.config.threshold("max_event_age_hours"):
                    return False, f"event itself is {event_age:.1f}h old", 0.0
            ages.append(age_hours)
        newest = min(ages) if ages else max_age
        freshness = 1.0 if newest <= max_age / 2 else 0.95
        return True, f"fresh official confirmation ({newest:.1f}h)", freshness

    def _database_gates(
        self,
        family_id: str,
        fingerprint: str,
        event: EventType,
        status: EventStatus,
        facts: Mapping[str, Any],
        policy: Any,
        now: datetime,
    ) -> tuple[GateState, GateState, str]:
        if self.repository.has_publication_fingerprint(fingerprint):
            return GateState.FAIL, GateState.FAIL, "identical verified facts already published"
        previous = self.repository.latest_publication(family_id)
        if not previous:
            return GateState.PASS, GateState.PASS, "new verified story"

        previous_at = parse_timestamp(previous.get("published_at"))
        active_window = timedelta(
            days=self.config.threshold("active_story_window_days")
        )
        within_active = bool(previous_at and now - previous_at <= active_window)
        previous_facts = previous.get("facts", {})

        if not within_active:
            return GateState.PASS, GateState.PASS, "new verified story outside active window"

        if policy:
            conflict_fields = set(policy.conflict_fields)
            for field in conflict_fields:
                old = previous_facts.get(field)
                new = facts.get(field)
                if old not in (None, "") and new not in (None, "") and normalize_fact(old) != normalize_fact(new):
                    if field in policy.progressive_fields:
                        continue
                    return GateState.WAIT, GateState.WAIT, f"new facts conflict with recent publication on {field}"

        old_status = EventStatus(previous.get("event_status", "UNKNOWN"))
        newly_verified_fields, progressive_fields = self._progression_fields(
            policy, previous_facts, facts
        )
        if progressive_fields:
            fields = ", ".join(progressive_fields)
            return GateState.PASS, GateState.PASS, f"verified progression in {fields}"
        if newly_verified_fields:
            fields = ", ".join(newly_verified_fields)
            return GateState.PASS, GateState.PASS, f"new verified material facts: {fields}"
        if (
            not policy
            and self.config.status_rank(status) > self.config.status_rank(old_status)
        ):
            return GateState.PASS, GateState.PASS, "verified status progression"
        return GateState.FAIL, GateState.FAIL, "no new verified milestone or material fact"

    def _progression_fields(
        self,
        policy: Any,
        previous_facts: Mapping[str, Any],
        facts: Mapping[str, Any],
    ) -> tuple[list[str], list[str]]:
        if not policy:
            return [], []

        def _present(value: Any) -> bool:
            return value not in (None, "")

        progressive: list[str] = []
        new_material: list[str] = []
        progressive_set = set(policy.progressive_fields)
        material_set = set(policy.material_fields)
        ordered = list(dict.fromkeys([*policy.progressive_fields, *policy.material_fields]))
        for field in ordered:
            old = previous_facts.get(field)
            new = facts.get(field)
            old_present = _present(old)
            new_present = _present(new)
            if not new_present:
                continue
            changed = (not old_present) or normalize_fact(old) != normalize_fact(new)
            if not changed:
                continue
            if field in progressive_set:
                progressive.append(field)
            elif field in material_set and not old_present:
                new_material.append(field)
        return new_material, progressive

    def _story_ids(
        self,
        event: EventType,
        facts: Mapping[str, Any],
        claims: Sequence[Claim],
    ) -> tuple[str, str]:
        subject = facts.get("subject_id") or facts.get("club_id")
        if not subject:
            subject = next(
                (c.facts.get("subject_id") or c.facts.get("club_id") for c in claims if c.facts),
                "unknown",
            )
        family_raw = f"{event.value}|{subject}"
        if event == EventType.OFFICIAL_STATEMENT and facts.get("statement_topic"):
            # Separate official statements are separate stories; identical topics
            # still deduplicate via the normalized topic component.
            family_raw += "|" + normalize_fact(facts["statement_topic"])
        family_id = hashlib.sha256(family_raw.encode()).hexdigest()[:32]
        route = "|".join(str(facts.get(k) or "") for k in (
            "club_from_id", "club_to_id", "club_id", "manager_action"
        ))
        story_id = hashlib.sha256(f"{family_raw}|{route}".encode()).hexdigest()[:32]
        return family_id, story_id

    def _fingerprint(
        self,
        event: EventType,
        status: EventStatus,
        facts: Mapping[str, Any],
        policy: Any,
    ) -> str:
        fields = sorted(facts)
        payload = {
            "event": event.value,
            "status": status.value,
            "subject": facts.get("subject_id") or facts.get("club_id"),
            "facts": {field: normalize_fact(facts.get(field)) for field in fields},
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _update_source_outcomes(
        self,
        decision: VerificationDecision,
        claims: Sequence[Claim],
        authoritative: Sequence[Claim],
        policy: Any,
    ) -> None:
        if not policy or not authoritative:
            return
        authority = authoritative[0]
        outcome_weights = self.config.reliability_config["outcome_weights"]
        for claim in claims:
            profile = self.sources.get(claim.source_id)
            if not profile or profile.is_official or claim.source_id == authority.source_id:
                continue
            minimum = EventStatus(self.config.policy("conflict_minimum_status"))
            if self.config.status_rank(claim.status) < self.config.status_rank(minimum):
                continue
            overlap = [
                field for field in policy.conflict_fields
                if claim.facts.get(field) not in (None, "")
                and authority.facts.get(field) not in (None, "")
            ]
            if not overlap:
                continue
            agrees = all(
                normalize_fact(claim.facts[field]) == normalize_fact(authority.facts[field])
                for field in overlap
            )
            outcome = "officially_confirmed" if agrees else "contradicted"
            if not agrees:
                claim_time = parse_timestamp(claim.document.published_at)
                later_correction = False
                for newer in claims:
                    if newer.source_id != claim.source_id or newer.id == claim.id:
                        continue
                    newer_time = parse_timestamp(newer.document.published_at)
                    if claim_time is None or newer_time is None or newer_time <= claim_time:
                        continue
                    newer_overlap = [
                        field for field in policy.conflict_fields
                        if newer.facts.get(field) not in (None, "")
                        and authority.facts.get(field) not in (None, "")
                    ]
                    if newer_overlap and all(
                        normalize_fact(newer.facts[field])
                        == normalize_fact(authority.facts[field])
                        for field in newer_overlap
                    ):
                        later_correction = True
                        break
                if later_correction:
                    outcome = "corrected"
            weight = abs(float(outcome_weights[outcome]))
            self.repository.record_source_outcome(
                source_id=claim.source_id,
                story_id=decision.story_id,
                claim_id=claim.id,
                outcome=outcome,
                weight=weight,
                authority_document_id=authority.document.id,
            )

    @staticmethod
    def _empty_consensus() -> ConsensusResult:
        return ConsensusResult({}, 0.0, 0.0, (), (), {}, {})


def parse_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
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


def weighted_geometric_mean(
    dimensions: Mapping[str, float], weights: Mapping[str, float]
) -> float:
    if not weights:
        return 0.0
    total_weight = sum(float(w) for w in weights.values())
    if total_weight <= 0:
        return 0.0
    if any(float(dimensions.get(name, 0.0)) <= 0 for name in weights):
        return 0.0
    log_sum = sum(
        float(weights[name]) * math.log(min(1.0, max(1e-12, float(dimensions[name]))))
        for name in weights
    )
    return round(math.exp(log_sum / total_weight), 6)
