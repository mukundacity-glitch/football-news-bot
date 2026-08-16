"""Runtime composition root for verification engine v2."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .config import VerificationConfig
from .documents import DocumentFactory, FeedRegistry
from .engine import VerificationEngine
from .entities import EntityRegistry
from .extractor import LegacyClaimAdapter
from .models import Claim, VerificationDecision
from .renderer import VerifiedPostRenderer
from .repository import VerificationRepository
from .source_registry import SourceRegistry


class RuntimeUnavailable(RuntimeError):
    pass


class VerificationRuntime:
    def __init__(
        self,
        *,
        fpl_data: Optional[dict],
        config_path: str | Path = "config/verification.json",
        sources_path: str | Path = "config/sources.json",
        feeds_path: str | Path = "config/feeds.json",
        database_path: Optional[str | Path] = None,
    ) -> None:
        try:
            self.config = VerificationConfig.load(config_path)
            self.sources = SourceRegistry.load(sources_path)
            self.feeds = FeedRegistry.load(feeds_path)
            self.entities = EntityRegistry.from_fpl(fpl_data)
            db_config = self.config.database_config
            override = database_path or os.getenv("VERIFICATION_DB_PATH")
            if override:
                db_config["path"] = str(override)
            # sqlite3.connect() CREATES a missing file, so a lost database is
            # indistinguishable from a healthy empty one once the repository is
            # open. The V2 store is persisted through actions/cache, whose keys
            # are evicted on age and total size — a restore miss would hand the
            # engine an empty story history, and every duplicate-suppression
            # gate (database_consistency, story_progression) would pass on news
            # already published. Record the fact here; the caller decides.
            self.database_was_empty = not Path(str(db_config["path"])).exists()
            self.repository = VerificationRepository.from_config(db_config)
            self.documents = DocumentFactory(self.sources)
            self.extractor = LegacyClaimAdapter(
                self.config, self.sources, self.entities
            )
            self.engine = VerificationEngine(
                self.config, self.sources, self.entities, self.repository
            )
            rendering = self.config.rendering_config
            self.renderer = VerifiedPostRenderer(
                self.sources,
                limit=int(rendering["x_character_limit"]),
                max_optional_fact_chars=int(rendering["max_optional_fact_chars"]),
            )
        except Exception as exc:
            if isinstance(exc, RuntimeUnavailable):
                raise
            raise RuntimeUnavailable(f"verification v2 unavailable: {exc}") from exc

    @property
    def live_enabled(self) -> bool:
        rollout = self.config.rollout_config
        variable = rollout["live_environment_variable"]
        required = rollout["live_required_value"]
        if rollout.get("shadow_mode_default", True):
            return os.getenv(variable, "") == required
        return os.getenv(variable, required) == required

    def claim_from_observation(self, observation: Mapping[str, Any]) -> Claim:
        document_item = dict(observation.get("document") or observation)
        legacy_story = dict(observation.get("legacy_story") or {})
        document = self.documents.from_item(document_item)
        return self.extractor.extract(document, legacy_story)

    def verify_observations(
        self, observations: Sequence[Mapping[str, Any]]
    ) -> VerificationDecision:
        claims = [self.claim_from_observation(obs) for obs in observations]
        decision = self.engine.verify(claims)
        if decision.may_publish:
            decision.rendered_text = self.renderer.render(decision)
        return decision

    def decision_integrity_ok(self, decision: VerificationDecision) -> bool:
        if not decision.may_publish:
            return False
        try:
            policy = self.config.event_policy(decision.event_type)
            expected = self.engine._fingerprint(
                decision.event_type,
                decision.status,
                decision.verified_facts,
                policy,
                authority_kind=decision.authority_kind,
                authority_source_ids=decision.authority_source_ids,
            )
        except Exception:
            return False
        return (
            expected == decision.fingerprint
            and self.repository.decision_exists(
                decision.story_id, decision.fingerprint, "PUBLISH"
            )
        )

    def close(self) -> None:
        self.repository.close()
