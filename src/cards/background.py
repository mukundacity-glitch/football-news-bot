"""The four approved FPL VORTEX slide backgrounds.

The artwork in ``assets/frames/`` is the design. Each file already contains the
lion mark, the category banner, the Premier League crest and the whole
source/follow footer bar. The renderer's only job is to present that artwork at
the publishing canvas — it never redraws branding, so the code cannot drift away
from the approved slides.

    from src.cards import render_background
    render_background("TRANSFER", "out.png")

Content is composited later, and only inside :data:`CONTENT_BOX` — the clear
region between the header banner and the footer bar. Drawing outside it would
cover branding that is part of the artwork.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

from PIL import Image

# Publishing canvas. 4K UHD, 16:9 — the size every post ships at.
CANVAS: Tuple[int, int] = (3840, 2160)

FRAMES_DIR = Path("assets/frames")


@dataclass(frozen=True)
class Category:
    """One news category and the slide that carries it."""

    key: str
    frame: str
    banner: str
    accent: Tuple[int, int, int]

    @property
    def path(self) -> Path:
        return FRAMES_DIR / self.frame


# The four categories, each bound to its approved slide. Accents are sampled
# from the artwork's own banner so anything drawn later matches the slide
# instead of guessing at a brand colour.
CATEGORIES: Dict[str, Category] = {
    "TRANSFER": Category("TRANSFER", "transfer_bg.jpg", "TRANSFER NEWS", (0, 200, 83)),
    "INJURY": Category("INJURY", "injury_bg.jpg", "INJURY NEWS", (214, 32, 47)),
    "SUSPENSION": Category("SUSPENSION", "suspension_bg.jpg", "SUSPENSION NEWS", (240, 196, 46)),
    "PRESS_CONFERENCE": Category("PRESS_CONFERENCE", "press_conference_bg.jpg", "PRESS CONFERENCE", (0, 230, 90)),
}

# Clear region of the slide, as fractions of the canvas.
#
# Measured from the artwork rather than eyeballed: the header banner ends by
# 0.190 of the height on the tallest of the four (press conference), and the
# footer bar begins at 0.886 on the same one. Taking the worst case of each and
# adding a margin gives one box that is safe on all four slides, so a layout
# built once cannot collide with branding when the category changes.
CONTENT_BOX: Tuple[float, float, float, float] = (0.030, 0.205, 0.970, 0.870)


def content_box_px(canvas: Tuple[int, int] = CANVAS) -> Tuple[int, int, int, int]:
    """CONTENT_BOX in pixels for a given canvas: (left, top, right, bottom)."""
    width, height = canvas
    left, top, right, bottom = CONTENT_BOX
    return (round(left * width), round(top * height),
            round(right * width), round(bottom * height))


class MissingFrameError(FileNotFoundError):
    """The approved artwork for a category is not on disk."""


def _category(key: str) -> Category:
    try:
        return CATEGORIES[str(key).strip().upper().replace(" ", "_")]
    except KeyError:
        raise KeyError(
            f"unknown card category {key!r}; expected one of {sorted(CATEGORIES)}"
        ) from None


def load_background(category: str, canvas: Tuple[int, int] = CANVAS) -> Image.Image:
    """Return the approved slide for ``category``, sized to ``canvas``.

    The source art is about 2760x1546 — a hair off 16:9. It is resized to the
    canvas rather than cropped, because cropping to the exact ratio would eat
    into the header banner or the footer bar, and those are the branding. The
    resulting stretch is under 1.2% on the worst of the four and is not visible;
    losing part of the logo bar would be.

    Raises MissingFrameError rather than substituting a blank slide: a card with
    no branding is not a card, and silently publishing one is worse than
    publishing nothing.
    """
    cat = _category(category)
    if not cat.path.exists():
        raise MissingFrameError(
            f"approved background missing for {cat.key}: {cat.path}"
        )
    with Image.open(cat.path) as art:
        return art.convert("RGB").resize(canvas, Image.LANCZOS)


def render_background(
    category: str,
    output_path: str | Path,
    *,
    canvas: Tuple[int, int] = CANVAS,
) -> Path:
    """Write the approved slide for ``category`` to ``output_path``."""
    image = load_background(category, canvas)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, "PNG", optimize=True)
    return out
