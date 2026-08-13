"""Configuration loading and validation for verification engine v2."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from .models import EventStatus, EventType


class ConfigurationError(RuntimeError):
    """Raised when safety configuration is missing or internally inconsistent."""


@dataclass(frozen=True)
class EventPolicy:
    required_facts: tuple[str, ...]
    allowed_subject_types: tuple[str, ...]
    conflict_fields: tuple[str, ...]
    material_fields: tuple[str, ...]
    official_relation_fields: tuple[str, ...]
    progressive_fields: tuple[str, ...]


class VerificationConfig:
    def __init__(self, raw: Mapping[str, Any], path: Path):
        self.raw = dict(raw)
        self.path = path
        self._validate()

    @classmethod
    def load(cls, path: str | Path = "config/verification.json") -> "VerificationConfig":
        p = Path(path)
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ConfigurationError(f"cannot load verification config {p}: {exc}") from exc
        return cls(raw, p)

    def _validate(self) -> None:
        required_sections = {
            "schema_version",
            "policy",
            "thresholds",
            "confidence",
            "source_reliability",
            "events",
            "status_order",
            "classification",
            "sport",
            "database",
            "rollout",
            "collection",
            "rendering",
        }
        missing = required_sections - set(self.raw)
        if missing:
            raise ConfigurationError(f"verification config missing sections: {sorted(missing)}")
        if self.raw["schema_version"] != 2:
            raise ConfigurationError("verification config schema_version must be 2")
        if self.policy("fail_closed") is not True:
            raise ConfigurationError("policy.fail_closed must be true")

        if self.policy("allow_reported_transfers") is True:
            # The human-readable config and the defense-in-depth hard gate must
            # describe exactly the same narrow lane; drift fails startup closed.
            from .reported_transfer_gate import (
                SINGLE_SOURCE_STATUSES,
                TWO_SOURCE_STATUSES,
                approved_source_ids,
            )
            configured_sources = set(self.policy("reported_transfer_approved_sources"))
            configured_single = set(self.policy("reported_transfer_single_source_statuses"))
            configured_double = set(self.policy("reported_transfer_two_source_statuses"))
            if configured_sources != set(approved_source_ids()):
                raise ConfigurationError("reported transfer source allowlist does not match hard gate")
            if configured_single != {status.value for status in SINGLE_SOURCE_STATUSES}:
                raise ConfigurationError("reported transfer one-source statuses do not match hard gate")
            if configured_double != {status.value for status in TWO_SOURCE_STATUSES}:
                raise ConfigurationError("reported transfer two-source statuses do not match hard gate")
        if self.policy("allow_structured_fotmob_completed_transfers") is True:
            fotmob_age = self.threshold("max_fotmob_transfer_age_hours")
            if fotmob_age <= 0 or fotmob_age > 48:
                raise ConfigurationError("structured FotMob transfer age must be within 48 hours")

        statuses = self.raw["status_order"]
        if len(statuses) != len(set(statuses)):
            raise ConfigurationError("status_order contains duplicates")
        unknown_statuses = set(statuses) - {s.value for s in EventStatus}
        if unknown_statuses:
            raise ConfigurationError(f"unknown statuses: {sorted(unknown_statuses)}")

        publishable = set(self.raw["policy"]["publishable_article_categories"])
        event_names = set(self.raw["events"])
        if publishable != event_names:
            raise ConfigurationError(
                "events must exactly match policy.publishable_article_categories"
            )
        unknown_events = publishable - {e.value for e in EventType}
        if unknown_events:
            raise ConfigurationError(f"unknown event categories: {sorted(unknown_events)}")

        for name, value in self.raw["thresholds"].items():
            if not isinstance(value, (int, float)):
                raise ConfigurationError(f"threshold {name} must be numeric")
            if any(token in name for token in ("confidence", "certainty", "reliability", "agreement")):
                if not 0.0 <= float(value) <= 1.0:
                    raise ConfigurationError(f"threshold {name} must be between 0 and 1")
            elif float(value) < 0:
                raise ConfigurationError(f"threshold {name} cannot be negative")
        weights = self.raw["confidence"].get("weights", {})
        if not weights or any(float(v) <= 0 for v in weights.values()):
            raise ConfigurationError("all confidence weights must be positive")

        classification = self.raw["classification"]
        for key in (
            "ineligible_article_patterns",
            "speculation_patterns",
            "negation_patterns",
            "event_patterns",
            "status_patterns",
            "fact_patterns",
        ):
            if key not in classification:
                raise ConfigurationError(f"classification.{key} is required")

    def policy(self, key: str) -> Any:
        try:
            return self.raw["policy"][key]
        except KeyError as exc:
            raise ConfigurationError(f"missing policy value: {key}") from exc

    def threshold(self, key: str) -> float:
        try:
            return float(self.raw["thresholds"][key])
        except KeyError as exc:
            raise ConfigurationError(f"missing threshold: {key}") from exc

    @property
    def publishable_categories(self) -> set[EventType]:
        return {
            EventType(value)
            for value in self.raw["policy"]["publishable_article_categories"]
        }

    @property
    def publishable_statuses(self) -> set[EventStatus]:
        values = set(self.raw["policy"]["publishable_statuses"])
        if self.policy("allow_here_we_go"):
            values.add(EventStatus.HERE_WE_GO.value)
        return {EventStatus(value) for value in values}

    def event_policy(self, event_type: EventType) -> EventPolicy:
        try:
            raw = self.raw["events"][event_type.value]
        except KeyError as exc:
            raise ConfigurationError(f"no event policy for {event_type.value}") from exc
        return EventPolicy(
            required_facts=tuple(raw["required_facts"]),
            allowed_subject_types=tuple(raw["allowed_subject_types"]),
            conflict_fields=tuple(raw["conflict_fields"]),
            material_fields=tuple(raw["material_fields"]),
            official_relation_fields=tuple(raw["official_relation_fields"]),
            progressive_fields=tuple(raw.get("progressive_fields", [])),
        )

    def status_rank(self, status: EventStatus) -> int:
        return self.raw["status_order"].index(status.value)

    @property
    def classification(self) -> Dict[str, Any]:
        return dict(self.raw["classification"])

    @property
    def confidence_weights(self) -> Dict[str, float]:
        return {
            str(k): float(v)
            for k, v in self.raw["confidence"]["weights"].items()
        }

    @property
    def sport_signal_weights(self) -> Dict[str, float]:
        weights = self.raw["sport"].get("signal_weights", {})
        if not weights:
            raise ConfigurationError("sport.signal_weights cannot be empty")
        return {str(k): float(v) for k, v in weights.items()}

    @property
    def reliability_config(self) -> Dict[str, Any]:
        return dict(self.raw["source_reliability"])

    @property
    def database_config(self) -> Dict[str, Any]:
        return dict(self.raw["database"])

    @property
    def rollout_config(self) -> Dict[str, Any]:
        return dict(self.raw["rollout"])

    @property
    def collection_config(self) -> Dict[str, Any]:
        return dict(self.raw["collection"])

    @property
    def rendering_config(self) -> Dict[str, Any]:
        return dict(self.raw["rendering"])
