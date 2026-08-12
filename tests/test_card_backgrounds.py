"""The four approved slide backgrounds.

The artwork is the design: lion mark, category banner, Premier League crest and
the whole footer bar are baked into each file. These tests exist so the code
cannot quietly stop using it, or start drawing over it.
"""

from pathlib import Path

import pytest
from PIL import Image

from src.cards import (
    CANVAS,
    CATEGORIES,
    CONTENT_BOX,
    MissingFrameError,
    content_box_px,
    load_background,
    render_background,
)

ALL = sorted(CATEGORIES)


def test_exactly_the_four_news_categories_exist():
    assert ALL == ["INJURY", "PRESS_CONFERENCE", "SUSPENSION", "TRANSFER"]


@pytest.mark.parametrize("key", ALL)
def test_every_category_has_its_artwork_on_disk(key):
    """A missing frame is a broken card, not a smaller one."""
    assert CATEGORIES[key].path.exists(), f"{key}: {CATEGORIES[key].path} missing"


@pytest.mark.parametrize("key", ALL)
def test_background_renders_at_the_publishing_canvas(key, tmp_path):
    out = render_background(key, tmp_path / f"{key}.png")
    assert Image.open(out).size == CANVAS == (3840, 2160)


@pytest.mark.parametrize("key", ALL)
def test_background_is_the_approved_artwork_not_a_blank(key):
    """Guards against a fallback that quietly ships an empty slide: the real
    art has a bright banner up top and a bright footer bar at the bottom."""
    image = load_background(key).convert("L")
    width, height = image.size
    px = image.load()

    def band_mean(y0, y1):
        pixels = [px[x, y] for y in range(y0, y1, 8) for x in range(0, width, 32)]
        return sum(pixels) / len(pixels)

    assert band_mean(0, int(height * 0.10)) > 20, f"{key}: header band looks empty"
    assert band_mean(int(height * 0.92), height) > 20, f"{key}: footer band looks empty"


def test_unknown_category_is_rejected_by_name():
    with pytest.raises(KeyError) as exc:
        load_background("SOMETHING_ELSE")
    assert "SOMETHING_ELSE" in str(exc.value)


@pytest.mark.parametrize("given", ["transfer", "Transfer", " TRANSFER ", "press conference"])
def test_category_lookup_is_forgiving_about_case_and_spacing(given):
    assert load_background(given).size == CANVAS


def test_missing_artwork_raises_rather_than_substituting_a_blank(monkeypatch, tmp_path):
    """Publishing an unbranded card is worse than publishing nothing."""
    from src.cards import background as bg

    monkeypatch.setattr(bg, "FRAMES_DIR", tmp_path)
    with pytest.raises(MissingFrameError):
        bg.load_background("TRANSFER")


# ── the safe content region ─────────────────────────────────────────────

def test_content_box_clears_the_header_and_the_footer_on_every_slide():
    """One box has to be safe on all four slides, or a layout built once will
    collide with branding as soon as the category changes. Measured from the
    artwork: the deepest header ends at 0.190 of the height (press conference)
    and the highest footer starts at 0.886 (also press conference)."""
    _, top, _, bottom = CONTENT_BOX
    assert top >= 0.190, "content would overlap the header banner"
    assert bottom <= 0.886, "content would overlap the footer bar"
    assert top < bottom


def test_content_box_in_pixels_is_inside_the_canvas():
    left, top, right, bottom = content_box_px()
    width, height = CANVAS
    assert 0 < left < right < width
    assert 0 < top < bottom < height
    # Enough room to be worth laying out in.
    assert (right - left) > width * 0.85
    assert (bottom - top) > height * 0.60


@pytest.mark.parametrize("key", ALL)
def test_measured_header_and_footer_still_match_the_artwork(key):
    """If someone swaps a frame for one with a taller banner, CONTENT_BOX stops
    being safe. This measures the real file rather than trusting the constant."""
    import statistics

    image = load_background(key).convert("L")
    width, height = image.size
    px = image.load()
    rows = [statistics.mean(px[x, y] for x in range(0, width, 24)) for y in range(height)]

    header_bottom = max((y for y in range(int(height * 0.25)) if rows[y] > 55), default=0)
    _, top_frac, _, bottom_frac = CONTENT_BOX
    assert header_bottom / height <= top_frac, (
        f"{key}: banner reaches {header_bottom / height:.3f}, past CONTENT_BOX top {top_frac}"
    )

    column = [statistics.mean(px[x, y] for x in range(int(width * 0.62), int(width * 0.72), 6))
              for y in range(height)]
    footer_top = next((y for y in range(int(height * 0.80), height) if column[y] > 40), height)
    assert footer_top / height >= bottom_frac, (
        f"{key}: footer starts at {footer_top / height:.3f}, above CONTENT_BOX bottom {bottom_frac}"
    )
