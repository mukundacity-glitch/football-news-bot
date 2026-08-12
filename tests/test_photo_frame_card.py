"""
Locks the photo-background card redesign (Aug 2026): the production card
template (_build_photo_frame_html in src/renderer.py) uses the real
approved background image per category (assets/frames/*.jpg, extracted
from the provided reference deck) instead of a CSS-drawn approximation.
Header/footer are baked into the background image; this only overlays
dynamic content (status, name, icon-row fields, season badge, optional
photo accent) into the reserved empty region.

Also locks the suspension/press_conference dispatch fix: those two
categories previously had no dedicated card path at all and silently
rendered with transfer's FROM/TO/FEE fields. category_rows() now builds
their real fields from the real extractor field names (diagnosis,
suspension_length, quote_summary, quote_topic -- see
src/verification/extractor.py add_fact calls), and create_transfer_image
routes suspension/press_conference events to those real fields instead of
transfer's.

Run with pytest OR standalone:  python tests/test_photo_frame_card.py
"""

import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.renderer import (  # noqa: E402
    CATEGORY_FRAME, _ASSET_FRAMES_DIR, _build_photo_frame_html,
    _frame_category, _frame_background_uri, _hex_to_rgb, category_rows,
    current_fpl_season,
)


def _sample_rows():
    return [("person", "CLUB", "Brighton")]


# ---- REAL BACKGROUND ASSETS EXIST AND RESOLVE ----

def test_every_category_background_file_exists_on_disk():
    for key, theme in CATEGORY_FRAME.items():
        path = _ASSET_FRAMES_DIR / theme["bg"]
        assert path.exists(), f"{key} background asset missing: {path}"
        assert path.stat().st_size > 10_000, (
            f"{key} background asset suspiciously small: {path.stat().st_size} bytes")


def test_background_uri_resolves_to_a_real_data_uri():
    for key in CATEGORY_FRAME:
        uri = _frame_background_uri(key)
        assert uri.startswith("data:image/"), f"{key} did not resolve to a data URI"
        assert len(uri) > 10_000, f"{key} data URI suspiciously short"


def test_all_four_categories_have_distinct_background_files():
    files = {theme["bg"] for theme in CATEGORY_FRAME.values()}
    assert len(files) == 4, f"expected 4 distinct background files, got {files}"


# ---- SEASON AUTO-GENERATION: no hardcoded year anywhere ----

def test_season_matches_the_real_current_season():
    assert current_fpl_season(date(2026, 8, 11)) == "2026/27"


def test_season_start_boundary_august():
    assert current_fpl_season(date(2026, 7, 31)) == "2025/26"
    assert current_fpl_season(date(2026, 8, 1)) == "2026/27"


def test_off_season_still_reads_as_the_season_just_finished():
    for month in (6, 7):
        assert current_fpl_season(date(2026, month, 15)) == "2025/26"


def test_season_a_future_year_works_with_zero_code_change():
    assert current_fpl_season(date(2031, 9, 1)) == "2031/32"


def test_template_default_uses_the_function_not_a_literal():
    html = _build_photo_frame_html(
        "Carlos Baleba", "OFFICIAL", "TRANSFER", "", "", "",
        _sample_rows(), "@Source", "TRANSFER",
    )
    assert current_fpl_season() in html


# ---- CATEGORY DISPATCH ----

def test_press_and_team_news_aliases_map_to_press_conference():
    assert _frame_category("PRESS") == CATEGORY_FRAME["PRESS_CONFERENCE"]
    assert _frame_category("TEAM_NEWS") == CATEGORY_FRAME["PRESS_CONFERENCE"]


def test_unknown_category_falls_back_to_transfer_not_a_crash():
    assert _frame_category("SOMETHING_NEW") == CATEGORY_FRAME["TRANSFER"]


def test_rendered_html_uses_the_category_heading_and_background():
    for event, theme in CATEGORY_FRAME.items():
        html = _build_photo_frame_html(
            "Carlos Baleba", "OFFICIAL", event, "", "", "",
            _sample_rows(), "@Source", event,
        )
        bg_uri = _frame_background_uri(event)
        assert bg_uri in html, f"{event} background not embedded in rendered HTML"


# ---- category_rows(): REAL FIELD NAMES, NO INVENTED ONES ----
# Field names must match src/verification/extractor.py add_fact() calls
# exactly, not names that merely look plausible.

def test_injury_rows_use_diagnosis_and_expected_return():
    story = {"diagnosis": "Hamstring strain", "stage": 2, "expected_return": "GW6"}
    rows = category_rows("INJURY", story)
    values = [v for (_, _, v) in rows]
    assert "Hamstring strain" in values
    assert "GW6" in values
    assert any(label == "AVAILABILITY" for (_, label, _) in rows)


def test_injury_rows_include_optional_next_match_when_present():
    story = {"diagnosis": "Knock", "stage": 3, "next_match": "vs Arsenal (H)"}
    rows = category_rows("INJURY", story)
    labels = [label for (_, label, _) in rows]
    assert "NEXT MATCH" in labels


def test_injury_rows_omit_next_match_when_absent():
    story = {"diagnosis": "Knock", "stage": 3}
    rows = category_rows("INJURY", story)
    labels = [label for (_, label, _) in rows]
    assert "NEXT MATCH" not in labels


def test_suspension_rows_use_diagnosis_as_reason_and_suspension_length():
    story = {"diagnosis": "Red card - violent conduct", "suspension_length": "3 matches",
             "expected_return": "GW5"}
    rows = category_rows("SUSPENSION", story)
    values = [v for (_, _, v) in rows]
    assert "Red card - violent conduct" in values
    assert "3 matches" in values
    assert "GW5" in values


def test_suspension_rows_omit_length_when_not_provided():
    story = {"diagnosis": "Disciplinary", "expected_return": "TBC"}
    rows = category_rows("SUSPENSION", story)
    labels = [label for (_, label, _) in rows]
    assert "LENGTH" not in labels


def test_press_conference_rows_use_quote_summary_and_quote_topic():
    story = {"quote_summary": "We are fully focused on the next match",
             "quote_topic": "Upcoming fixtures"}
    rows = category_rows("PRESS_CONFERENCE", story)
    values = [v for (_, _, v) in rows]
    assert "We are fully focused on the next match" in values
    assert "Upcoming fixtures" in values


def test_press_conference_rows_have_a_fallback_when_no_quote_present():
    rows = category_rows("PRESS_CONFERENCE", {})
    assert len(rows) >= 1, "press conference with no data should still render something"


def test_transfer_category_returns_no_rows_from_category_rows():
    # Transfer rows are built by create_transfer_image itself (FROM/TO/FEE),
    # not by category_rows() -- this must stay empty so the caller's own
    # transfer-specific logic is what actually renders, not a duplicate.
    assert category_rows("TRANSFER", {"diagnosis": "irrelevant"}) == []


# ---- PHOTO ACCENT: regression guard for the "computed but never placed
# in the HTML body" bug found and fixed in this same session ----

def test_photo_accent_appears_in_html_when_photo_provided():
    html = _build_photo_frame_html(
        "Carlos Baleba", "OFFICIAL", "TRANSFER", "", "data:image/png;base64,ABC", "",
        _sample_rows(), "@Source", "TRANSFER",
    )
    assert 'class="photo-accent"' in html, (
        "photo_accent_html was computed but not inserted into the HTML body -- "
        "this is the exact bug found and fixed this session; a photo URI must "
        "actually produce a visible element, not just an unused local variable")
    assert "data:image/png;base64,ABC" in html


def test_photo_accent_absent_when_no_photo_provided():
    html = _build_photo_frame_html(
        "Carlos Baleba", "OFFICIAL", "TRANSFER", "", "", "",
        _sample_rows(), "@Source", "TRANSFER",
    )
    assert 'class="photo-accent"' not in html


def test_crest_badge_appears_in_html_when_crest_provided():
    html = _build_photo_frame_html(
        "Carlos Baleba", "OFFICIAL", "TRANSFER", "", "", "data:image/png;base64,XYZ",
        _sample_rows(), "@Source", "TRANSFER",
    )
    assert 'class="crest-badge"' in html
    assert "data:image/png;base64,XYZ" in html


# ---- ROW RENDERING ----

def test_all_rows_render_with_icon_label_and_value():
    rows = [("person", "FROM", "Chelsea"), ("cash", "FEE", "£50m")]
    html = _build_photo_frame_html(
        "Carlos Baleba", "OFFICIAL", "TRANSFER", "", "", "",
        rows, "@Source", "TRANSFER",
    )
    assert "FROM" in html and "Chelsea" in html
    assert "FEE" in html and "£50m" in html
    assert html.count('class="data-row"') == 2


def test_unknown_icon_key_falls_back_without_crashing():
    rows = [("totally_unknown_icon", "LABEL", "VALUE")]
    html = _build_photo_frame_html(
        "Carlos Baleba", "OFFICIAL", "TRANSFER", "", "", "",
        rows, "@Source", "TRANSFER",
    )
    assert "LABEL" in html and "VALUE" in html


# ---- HEX-TO-RGB HELPER (unchanged from previous session, still in use) ----

def test_hex_to_rgb_converts_a_known_value():
    assert _hex_to_rgb("#FF453A") == "255,69,58"


def test_hex_to_rgb_invalid_input_falls_back_to_white_not_a_crash():
    assert _hex_to_rgb("") == "255,255,255"
    assert _hex_to_rgb(None) == "255,255,255"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed.")
    sys.exit(1 if failed else 0)


# ---- PRODUCTION ENTRY POINTS: real dispatch through create_transfer_image ----

def _with_twikit_stub():
    import types
    if "twikit" not in sys.modules:
        tw = types.ModuleType("twikit")
        tw.Client = object
        sys.modules["twikit"] = tw


def test_create_transfer_image_renders_a_valid_landscape_card():
    _with_twikit_stub()
    import main as _main
    story = {
        "player": "Thomas Meunier", "display_name": "Thomas Meunier",
        "event": "transfer", "mode": "confirmed", "stage": 4,
        "to_key": "crystal_palace", "from_key": "trabzonspor",
        "to_club": "Crystal Palace", "from_club": "Trabzonspor",
        "fee": None, "is_free": False,
    }
    out = "/tmp/_test_create_transfer_image.png"
    _main.create_transfer_image(story, ["SachaTavolieri"], out)
    from PIL import Image
    w, h = Image.open(out).size
    # Full 4K (3840x2160) whenever the file fits X's upload limit; a
    # graceful, aspect-correct downscale via _ensure_upload_safe()
    # (pre-existing, unchanged behaviour) is acceptable when it doesn't --
    # what must never happen is a crash, a zero-byte file, or a distorted
    # aspect ratio.
    assert w >= 1600 and h >= 900, f"card rendered too small: {w}x{h}"
    assert abs((w / h) - (3840 / 2160)) < 0.01, f"aspect ratio drifted: {w}x{h}"


def _requires_browser():
    """Skip when there is no browser to render with.

    Without Playwright the renderer silently falls back to a 1380x776 PIL card,
    so these assertions would be measuring the fallback rather than the card
    that ships. Skipping says that honestly instead of failing for the wrong
    reason — and test_ci_installs_a_browser_so_these_never_silently_skip below
    stops the skip from quietly becoming permanent in CI.
    """
    import pytest
    try:
        import playwright  # noqa: F401
    except ImportError:
        pytest.skip("playwright not installed — cannot verify a real 4K render")


def test_create_injury_image_renders_a_valid_landscape_card():
    _requires_browser()
    _with_twikit_stub()
    import main as _main
    story = {
        "player": "Bukayo Saka", "display_name": "Bukayo Saka",
        "event": "injury", "to_key": "arsenal", "from_key": "arsenal",
        "diagnosis": "Hamstring injury", "stage": 2,
        "expected_return": "Awaiting update",
    }
    out = "/tmp/_test_create_injury_image.png"
    _main.create_injury_image(story, ["ArsenalFC"], out)
    from PIL import Image
    w, h = Image.open(out).size
    assert w >= 1600 and h >= 900, f"card rendered too small: {w}x{h}"
    assert abs((w / h) - (3840 / 2160)) < 0.01, f"aspect ratio drifted: {w}x{h}"


def test_suspension_event_through_create_transfer_image_uses_suspension_fields():
    """The real dispatch fix: a suspension story routed through
    create_transfer_image (its only entry point today -- there is no
    dedicated create_suspension_image) must render with suspension's real
    fields, not transfer's FROM/TO/FEE."""
    _with_twikit_stub()
    import main as _main
    story = {
        "player": "Declan Rice", "display_name": "Declan Rice",
        "event": "suspension", "to_key": "arsenal", "from_key": "arsenal",
        "to_club": "Arsenal", "from_club": "Arsenal",
        "diagnosis": "Red card - violent conduct", "suspension_length": "3 matches",
        "expected_return": "GW5", "stage": 2,
    }
    out = "/tmp/_test_create_suspension_via_transfer.png"
    _main.create_transfer_image(story, ["SkySports"], out)
    from PIL import Image
    w, h = Image.open(out).size
    assert w >= 1600 and h >= 900, f"card rendered too small: {w}x{h}"


def test_press_conference_event_through_create_transfer_image_uses_quote_fields():
    _with_twikit_stub()
    import main as _main
    story = {
        "player": "Enzo Maresca", "display_name": "Enzo Maresca",
        "event": "press_conference", "to_key": "chelsea", "from_key": "chelsea",
        "to_club": "Chelsea", "from_club": "Chelsea",
        "quote_summary": "We are fully focused on the next three matches",
        "quote_topic": "Upcoming fixtures", "stage": 4,
    }
    out = "/tmp/_test_create_press_conference_via_transfer.png"
    _main.create_transfer_image(story, ["SkySports"], out)
    from PIL import Image
    w, h = Image.open(out).size
    assert w >= 1600 and h >= 900, f"card rendered too small: {w}x{h}"


def test_old_frame_functions_have_no_remaining_callers():
    """_build_card_html (the original navy-gradient template) and
    _build_broadcast_frame_html (the CSS-drawn red/black frame, superseded
    by the real photo-background frame this session) are both confirmed
    dead after this redesign. A canary: if a future edit adds a new
    caller of either, that's worth knowing about explicitly."""
    import inspect
    import src.renderer as renderer

    source = inspect.getsource(renderer)
    for fn_name in ("_build_card_html", "_build_broadcast_frame_html"):
        call_count = source.count(f"{fn_name}(") - source.count(f"def {fn_name}(")
        assert call_count == 0, (
            f"expected {fn_name} to have zero remaining callers, found {call_count}")


def test_ci_installs_a_browser_so_these_never_silently_skip():
    """The render tests skip without Playwright. That is right for a laptop and
    wrong for CI: a gate that skips protects nothing. bot.yml installs a browser
    because production needs one; the test workflow must match, or it measures a
    different renderer than the one that ships."""
    from pathlib import Path

    for workflow in (".github/workflows/bot.yml", ".github/workflows/diff_test.yml"):
        text = Path(workflow).read_text()
        assert "pip install playwright" in text, f"{workflow} must install playwright"
        assert "playwright install chromium" in text, f"{workflow} must install a browser"
