"""Dynamic, outcome-backed source reliability model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from .config import VerificationConfig
from .repository import VerificationRepository
from .source_registry import SourceRegistry


@dataclass(frozen=True)
class ReliabilityResult:
    source_id: str
    score: float
    factors: Dict[str, float]
    resolved_history: float


class SourceReliabilityModel:
    """Calculate reliability from configured priors and official outcomes.

    Unknown/unresolved stories never improve or damage a source.  Outcomes are
    learned only when a later first-party official claim provides ground truth.
    This avoids circularly training the model from its own publication decisions.
    """

    def __init__(
        self,
        config: VerificationConfig,
        registry: SourceRegistry,
        repository: VerificationRepository,
    ) -> None:
        self.config = config
        self.registry = registry
        self.repository = repository

    def evaluate(self, source_id: str) -> ReliabilityResult:
        rcfg = self.config.reliability_config
        profile = self.registry.get(source_id)
        prior_mean = (
            profile.prior_mean if profile else float(rcfg["default_prior_mean"])
        )
        prior_strength = (
            profile.prior_strength if profile else float(rcfg["default_prior_strength"])
        )
        stats = self.repository.source_outcome_stats(source_id)
        confirmed = max(0.0, stats.get("officially_confirmed", 0.0))
        contradicted = max(0.0, stats.get("contradicted", 0.0))
        corrected = max(0.0, stats.get("corrected", 0.0))
        resolved = confirmed + contradicted + corrected

        posterior_accuracy = (
            prior_mean * prior_strength + confirmed
        ) / max(1e-9, prior_strength + resolved)

        # Rate factors are Bayesian-smoothed by the same source prior so a
        # single incident cannot swing a source from 0 to 1 or vice versa.
        denominator = prior_strength + resolved
        false_positive_rate = contradicted / max(1e-9, denominator)
        correction_rate = corrected / max(1e-9, denominator)
        # Official publishers receive the full official-status factor. Known
        # non-official publishers retain their configured prior; unknown sources
        # receive zero for this factor. Official authority is checked separately
        # by the policy engine and can never be manufactured by this score.
        official_status = (
            1.0 if profile and profile.is_official
            else prior_mean if profile
            else 0.0
        )
        full_count = float(rcfg["verification_history_full_count"])
        verification_history = min(
            1.0,
            (prior_strength + resolved) / max(1.0, full_count),
        )

        factors = {
            "historical_accuracy": _clamp(posterior_accuracy),
            "official_status": official_status,
            "false_positive_rate": _clamp(1.0 - false_positive_rate),
            "correction_rate": _clamp(1.0 - correction_rate),
            "verification_history": _clamp(verification_history),
        }
        weights = {
            name: float(weight)
            for name, weight in rcfg["factor_weights"].items()
        }
        total_weight = sum(weights.values())
        if total_weight <= 0:
            score = 0.0
        else:
            score = sum(factors[name] * weights[name] for name in weights) / total_weight
        return ReliabilityResult(
            source_id=source_id,
            score=round(_clamp(score), 6),
            factors={k: round(v, 6) for k, v in factors.items()},
            resolved_history=resolved,
        )


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
