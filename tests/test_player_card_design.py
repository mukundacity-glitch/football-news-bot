"""Focused regression tests for the FPL-only graphic asset source."""
from __future__ import annotations

from types import SimpleNamespace

from PIL import Image

import src.rendering.assets as assets
import src.rendering.engine as engine
from src.rendering.engine import Field, MasterGraphicRenderer, STYLES
from src.rendering.layout import (
    CORE_TEXT_MIN,
    LABEL_TEXT_MIN,
    META_TEXT_MIN,
    Rect,
    preview_safe_font,
    stacked_rects,
)
from src.verification.models import EventType


def _fpl_data() -> dict:
    return {
        "teams": [{"id": 1, "code": 3, "name": "Arsenal", "short_name": "ARS"}],
        "elements": [{
            "id": 10,
            "code": 12345,
            "first_name": "Truth",
            "second_name": "Player",
            "web_name": "Player",
            "team": 1,
            "element_type": 3,
        }],
        "element_types": [{"id": 3, "singular_name": "Midfielder"}],
    }


def _portrait(color: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGBA", (300, 400), (*color, 255))


def test_verified_player_uses_fpl_portrait_even_when_current_club_is_known(monkeypatch):
    expected = _portrait((40, 80, 220))
    calls: list[str] = []

    def download(url, _cache):
        calls.append(url)
        return expected

    monkeypatch.setattr(assets, "_download_image", download)

    image, source = assets.resolve_player_image("Truth Player", {}, fpl_data=_fpl_data())

    assert image is expected
    assert source == "FPL API"
    assert calls == ["https://resources.premierleague.com/premierleague/photos/players/250x250/p12345.png"]


def test_player_image_does_not_fallback_to_wikipedia_or_fotmob(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(assets, "_download_image", lambda url, _cache: calls.append(url) or None)
    monkeypatch.setattr(
        assets,
        "fetch_fpl_data",
        lambda: _fpl_data(),
    )

    image, source = assets.resolve_player_image("Truth Player", {}, fpl_data=_fpl_data())

    assert image is None
    assert source == ""
    assert all("wikipedia.org" not in url and "fotmob.com" not in url for url in calls)


def test_missing_fpl_player_identity_fails_closed(monkeypatch):
    monkeypatch.setattr(assets, "_download_image", lambda *_args, **_kwargs: None)
    image, source = assets.resolve_player_image("Unknown Player", {}, fpl_data=_fpl_data())
    assert image is None
    assert source == ""


def test_club_logo_uses_fpl_team_code_only(monkeypatch):
    calls: list[str] = []
    expected = _portrait((220, 40, 160))

    def download(url, _cache):
        calls.append(url)
        return expected

    monkeypatch.setattr(assets, "_download_image", download)

    image = assets.resolve_club_logo(
        "Arsenal",
        provider_id="9999",
        fpl_data=_fpl_data(),
    )

    assert image is expected
    assert calls == ["https://resources.premierleague.com/premierleague/badges/100/t3.png"]
    assert not any("fotmob.com" in url for url in calls)


def test_club_logo_missing_fpl_team_fails_closed(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(assets, "_download_image", lambda url, _cache: calls.append(url) or None)
    image = assets.resolve_club_logo("Unknown FC", provider_id="9999", fpl_data=_fpl_data())
    assert image is None
    assert calls == []


def test_identity_safe_portrait_keeps_verified_fpl_asset_intact():
    portrait = Image.new("RGBA", (500, 500), (0, 0, 0, 0))
    portrait.paste((20, 180, 240, 255), (0, 0, 500, 220))
    portrait.paste((255, 210, 0, 255), (0, 220, 500, 500))
    safe = assets.identity_safe_portrait(portrait, "FPL API")
    assert safe.size == portrait.size
    assert safe.tobytes() == portrait.tobytes()


def test_verified_shirt_club_is_anchored_to_fpl_player_team():
    assert assets._verified_shirt_club("Truth Player", {"club_to_name": "Chelsea"}, _fpl_data())[0] == "Arsenal"


def test_player_name_and_values_use_large_responsive_font_ranges(monkeypatch):
    calls: list[tuple[str, int, int]] = []
    original_fit_font = engine.fit_font
    original_fit_wrapped_text = engine.fit_wrapped_text

    def capture(draw, value, max_width, *, max_size, min_size, role="bold"):
        calls.append((str(value), max_size, min_size))
        return original_fit_font(draw, value, max_width, max_size=max_size, min_size=min_size, role=role)

    monkeypatch.setattr(engine, "fit_font", capture)

    def capture_wrapped(draw, value, max_width, max_height, max_lines, *, max_size, min_size, role="bold", line_spacing=1.12):
        calls.append((str(value), max_size, min_size))
        return original_fit_wrapped_text(
            draw, value, max_width, max_height, max_lines,
            max_size=max_size, min_size=min_size, role=role, line_spacing=line_spacing,
        )

    monkeypatch.setattr(engine, "fit_wrapped_text", capture_wrapped)
    monkeypatch.setattr(engine, "resolve_player_metadata", lambda *_args, **_kwargs: {})
    renderer = MasterGraphicRenderer(None, fpl_data=_fpl_data())
    image = Image.new("RGB", (3840, 2160), (0, 0, 0))
    decision = SimpleNamespace(event_type=EventType.INJURY, verified_facts={"subject_name": "Large Type", "club_name": "Arsenal"})

    renderer._player_heading(image, (200, 485, 2325, 750), decision, STYLES[EventType.INJURY])
    renderer._draw_rows(image, (200, 790, 2325, 1014), [Field("STATUS", "Ready", "status")], STYLES[EventType.INJURY])

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
