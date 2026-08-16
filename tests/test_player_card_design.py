"""Focused regression tests for the player-card-only visual redesign."""
from __future__ import annotations

from types import SimpleNamespace

from PIL import Image

import src.rendering.assets as assets
import src.rendering.engine as engine
from src.rendering.engine import Field, MasterGraphicRenderer, STYLES
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


def test_player_name_and_values_use_large_responsive_font_ranges(monkeypatch):
    calls: list[tuple[str, int, int]] = []
    original_fit_font = engine.fit_font

    def capture(draw, value, max_width, *, max_size, min_size, role="bold"):
        calls.append((str(value), max_size, min_size))
        return original_fit_font(
            draw, value, max_width, max_size=max_size, min_size=min_size, role=role,
        )

    monkeypatch.setattr(engine, "fit_font", capture)
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

    assert ("Large Type", 154, 84) in calls
    assert ("Ready", 104, 58) in calls
