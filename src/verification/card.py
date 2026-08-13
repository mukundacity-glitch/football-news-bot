"""Fail-closed boundary while no player-card renderer is installed.

The previous renderer and every previous card design were removed at the
repository owner's request. Verification may continue to classify stories, but
nothing can produce a card or reach image-based publishing until a replacement
renderer is supplied, reviewed, and installed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .models import VerificationDecision
from .source_registry import SourceRegistry


class RendererNotInstalledError(RuntimeError):
    pass


class UnverifiedTransferError(RendererNotInstalledError):
    pass


class UnverifiedPressConferenceError(RendererNotInstalledError):
    pass


def create_verified_card(
    decision: VerificationDecision,
    sources: SourceRegistry,
    output_path: str | Path,
    *,
    fpl_data: Optional[dict] = None,
) -> str:
    raise RendererNotInstalledError(
        "all previous player-card renderers were removed; replacement not installed"
    )
