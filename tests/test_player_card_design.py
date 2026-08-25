"""Focused regression tests for the player-card-only visual redesign."""
from __future__ import annotations

from types import SimpleNamespace

from PIL import Image

import src.rendering.assets as assets
import src.rendering.engine as engine
from src.rendering.engine import Field, MasterGraphicRenderer, STYLES
from src.rendering.layout import (
    CORE_TEXT_MIN, LABEL_TEXT_MIN, META_TEXT_MIN, Rect, preview_safe_font,
    stacked_rects,
)
from src.verification.models import EventType


def _fpl_data() -> dict:
    return {
        "teams": [{"id": 1, "code": 3, "name": "Arsenal", "short_name": "ARS"}],
        "elements": [{
            "id": 10, "code": 12345, "first_name": "Truth", "second_name": "Player",
            "web_name": "Player", "team": 1, "element_type": 3,
        }],
        "element_types": [{"id": 3, "singular_name": "Midfielder"}],
    }


def _portrait(color: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGBA", (300, 400), (*color, 255))


def test_player_image_uses_fpl_before_every_fallback(monkeypatch):
    calls: list[str] = []
    expected = _portrait((10, 220, 30))

    def download(url, _cache):
        calls.append(url)
        return expected if "resources.premierleague.com" in url else None

    monkeypatch.setattr(assets, "_download_image", download)
    monkeypatch.setattr(
        assets, "_wikipedia_image",
        lambda _subject: (_ for _ in ()).throw(AssertionError("Wikipedia must not run")),
    )

    image, source = assets.resolve_player_image("Truth Player", {}, fpl_data=_fpl_data())

    assert image is expected
    assert source == "FPL API"
    assert len(calls) == 1


def test_wikipedia_precedes_reliable_provider(monkeypatch):
    calls: list[str] = []
    expected = _portrait((40, 80, 220))

    def download(url, _cache):
        calls.append(url)
        return None

    monkeypatch.setattr(assets, "_download_image", download)
    monkeypatch.setattr(assets, "_wikipedia_image", lambda _subject: expected)

    image, source = assets.resolve_player_image(
        "Truth Player", {"provider_player_id": "9988"}, fpl_data=_fpl_data(),
    )

    assert image is expected
    assert source == "Wikipedia"
    assert not any("fotmob.com" in url for url in calls)


def test_reliable_provider_runs_after_wikipedia(monkeypatch):
    calls: list[str] = []
    expected = _portrait((220, 40, 160))

    def download(url, _cache):
        calls.append(url)
        if "fotmob.com/image_resources/playerimages/9988.png" in url:
            return expected
        return None

    monkeypatch.setattr(assets, "_download_image", download)
    monkeypatch.setattr(assets, "_wikipedia_image", lambda _subject: None)

    image, source = assets.resolve_player_image(
        "Truth Player", {"provider_player_id": "9988"}, fpl_data=_fpl_data(),
    )

    assert image is expected
    assert source == "Reliable provider"
    assert any("fotmob.com/image_resources/playerimages/9988.png" in url for url in calls)


def test_verified_team_shirt_is_the_final_image_fallback(monkeypatch):
    monkeypatch.setattr(assets, "_download_image", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(assets, "_wikipedia_image", lambda _subject: None)

    image, source = assets.resolve_player_image(
        "Truth Player",
        {"club_from_name": "Chelsea", "club_to_name": "Tottenham"},
        fpl_data=_fpl_data(),
    )

    assert source == "Team shirt fallback"
    assert image is not None and image.mode == "RGBA"
    assert image.getchannel("A").getbbox() is not None
    # The live FPL team is the identity anchor, not either supplied transfer club.
    assert assets._verified_shirt_club(
        "Truth Player", {"club_to_name": "Chelsea"}, _fpl_data(),
    )[0] == "Arsenal"


def test_verified_player_portrait_keeps_original_image_intact():
    portrait = Image.new("RGBA", (500, 500), (0, 0, 0, 0))
    portrait.paste((20, 180, 240, 255), (0, 0, 500, 220))
    portrait.paste((255, 210, 0, 255), (0, 220, 500, 500))

    safe = assets.identity_safe_portrait(portrait, "FPL API")

    assert safe.size == portrait.size
    assert safe.tobytes() == portrait.tobytes()


def test_verified_team_shirt_is_never_cropped_as_a_stale_portrait():
    shirt = Image.new("RGBA", (900, 1120), (80, 20, 120, 255))
    safe = assets.identity_safe_portrait(shirt, "Team shirt fallback")
    assert safe.size == shirt.size


def test_player_name_and_values_use_large_responsive_font_ranges(monkeypatch):
    calls: list[tuple[str, int, int]] = []
    original_fit_font = engine.fit_font
    original_fit_wrapped_text = engine.fit_wrapped_text

    def capture(draw, value, max_width, *, max_size, min_size, role="bold"):
        calls.append((str(value), max_size, min_size))
        return original_fit_font(
            draw, value, max_width, max_size=max_size, min_size=min_size, role=role,
        )

    monkeypatch.setattr(engine, "fit_font", capture)

    def capture_wrapped(
        draw, value, max_width, max_height, max_lines, *, max_size,
        min_size, role="bold", line_spacing=1.12,
    ):
        calls.append((str(value), max_size, min_size))
        return original_fit_wrapped_text(
            draw, value, max_width, max_height, max_lines,
            max_size=max_size, min_size=min_size, role=role,
            line_spacing=line_spacing,
        )

    monkeypatch.setattr(engine, "fit_wrapped_text", capture_wrapped)
    monkeypatch.setattr(engine, "resolve_player_metadata", lambda *_args, **_kwargs: {})
    renderer = MasterGraphicRenderer(None, fpl_data=_fpl_data())
    image = Image.new("RGB", (3840, 2160), (0, 0, 0))
    decision = SimpleNamespace(
        event_type=EventType.INJURY,
        verified_facts={"subject_name": "Large Type", "club_name": "Arsenal"},
    )

    renderer._player_heading(
        image, (200, 485, 2325, 750), decision, STYLES[EventType.INJURY],
    )
    renderer._draw_rows(
        image, (200, 790, 2325, 1014), [Field("STATUS", "Ready", "status")],
        STYLES[EventType.INJURY],
    )

    assert ("Large Type", 180, 96) in calls
    assert any(value == "Ready" and minimum == CORE_TEXT_MIN for value, _maximum, minimum in calls)


def test_phone_preview_font_floors_are_calculated_not_magic_tiny_sizes():
    assert preview_safe_font(13) == CORE_TEXT_MIN == 78
    assert preview_safe_font(11) == LABEL_TEXT_MIN == 66
    assert preview_safe_font(9) == META_TEXT_MIN == 54


def test_dynamic_rows_never_overlap_or_escape_their_panel():
    panel = Rect(200, 790, 2325, 1810)
    for count in range(1, 6):
        rows = [Rect(*value) for value in stacked_rects(panel.tuple(), count, gap=22, max_height=500)]
        assert len(rows) == count
        assert all(panel.contains(row) for row in rows)
        assert all(not left.overlaps(right) for left, right in zip(rows, rows[1:]))
