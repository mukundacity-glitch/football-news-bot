"""Regression coverage for the fixture card pre-flight."""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import main


class _Profile:
    id = "journalist.fabrizio_romano"


class _Registry:
    def profile_for_handle(self, handle):
        return _Profile()


class _Renderer:
    decisions = []

    def __init__(self, sources, *, fpl_data=None):
        self.sources = sources
        self.fpl_data = fpl_data

    def render(self, decision, output_path):
        self.decisions.append(decision)
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"rendered-card" * 200)
        return str(path)


def _accepted_story():
    return {
        "player": "Dynamic Player",
        "display_name": "Dynamic Player",
        "event": "transfer",
        "from_key": "Arsenal",
        "to_key": "Chelsea",
        "from_club": "Arsenal",
        "to_club": "Chelsea",
        "stage": 4,
        "collapsed": False,
    }


def test_fixture_preflight_uses_the_approved_renderer(monkeypatch, tmp_path):
    fixture = tmp_path / "tweets.json"
    fixture.write_text(json.dumps([{
        "id": "fixture-1",
        "source": "FabrizioRomano",
        "text": "fixture text",
    }]), encoding="utf-8")

    _Renderer.decisions.clear()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "init_club_data", lambda: None)
    monkeypatch.setattr(main, "fetch_fpl_data", lambda: {})
    monkeypatch.setattr(main.V2SourceRegistry, "load", lambda: _Registry())
    monkeypatch.setattr(main, "MasterGraphicRenderer", _Renderer)
    monkeypatch.setattr(main, "build_story", lambda text, fpl: _accepted_story())
    monkeypatch.setattr(main, "passes_safety_gate", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(main, "validate_story", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(main, "is_duplicate_content", lambda *a, **k: (False, ""))
    monkeypatch.setattr(main, "verify_card_data", lambda *a, **k: (True, "ok", []))
    monkeypatch.setattr(main, "record_content_dedup", lambda *a, **k: None)
    monkeypatch.setattr(main, "image_is_blank", lambda path: False)

    result = asyncio.run(main.run_dry_run(str(fixture), runs=1))

    assert result == 0
    assert (tmp_path / "queue" / "dryrun" / "dynamic_player_chelsea_transfer.png").exists()
    assert len(_Renderer.decisions) == 1
    assert _Renderer.decisions[0].may_publish is True
    assert _Renderer.decisions[0].event_type.value == "TRANSFER"


def test_fixture_preflight_no_longer_calls_removed_renderer_stubs():
    source = inspect.getsource(main.run_dry_run)
    assert "create_transfer_image(" not in source
    assert "create_verified_branded_card(" not in source


def test_missing_fixture_returns_failure_status(tmp_path):
    result = asyncio.run(main.run_dry_run(str(tmp_path / "missing.json"), runs=1))
    assert result == 1
