"""Rendering intentionally unavailable.

All previous player-card layouts, templates, image composition code, asset
lookups, and fallback graphics were removed at the owner's request. This module
contains only a fail-closed compatibility boundary plus the image-blank safety
check used by the posting pipeline. A new renderer must replace these stubs
before card publishing can be re-enabled.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageStat

from src.constants import CLUB_COLORS

CARD_OUTPUT_W = 3840
CARD_OUTPUT_H = 2160


class RendererNotInstalledError(RuntimeError):
    pass


def _removed(*args, **kwargs):
    raise RendererNotInstalledError(
        "player-card renderer removed; install and approve the replacement renderer"
    )


# Compatibility names imported by main.py. They deliberately cannot create an
# image, preventing any accidental fallback to an old design.
create_transfer_image = _removed
create_injury_image = _removed
create_verified_branded_card = _removed
_create_fallback_card = _removed


_COLOR_EMOJI_RGB = {
    "🔴": (237, 28, 36), "🟠": (255, 127, 0), "🟡": (255, 221, 0),
    "🟢": (0, 166, 81), "🔵": (0, 114, 206), "🟣": (145, 65, 172),
    "🟤": (127, 85, 57), "⚫": (30, 30, 30), "⚪": (245, 245, 245),
}


def club_color_emojis(club_key):
    """Text-caption utility only; contains no card-design behavior."""
    rgb = CLUB_COLORS.get(club_key)
    if not rgb:
        return "⚽"
    r, g, b = rgb
    primary = min(
        _COLOR_EMOJI_RGB,
        key=lambda emoji: sum(
            (value - target) ** 2
            for value, target in zip((r, g, b), _COLOR_EMOJI_RGB[emoji])
        ),
    )
    return primary if primary == "⚪" else f"{primary}⚪"


def image_is_blank(path, stddev_floor: float = 3.0) -> bool:
    """Fail closed when an image is missing, unreadable, or visually flat."""
    try:
        p = Path(path)
        if not p.exists() or p.stat().st_size < 1000:
            return True
        with Image.open(p) as image:
            stats = ImageStat.Stat(image.convert("RGB").resize((96, 54)))
        return max(stats.stddev or [0.0]) < float(stddev_floor)
    except Exception:
        return True
