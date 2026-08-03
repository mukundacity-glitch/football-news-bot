"""Tests that the live main pipeline accepts only V2-authorized items."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import main
from src.verification import VerificationRuntime


def _fpl():
    return {
        "teams": [
            {"id": 1, "name": "Chelsea", "short_name": "CHE"},
            {"id": 2, "name": "Brighton", "short_name": "BHA"},
            {"id": 3, "name": "Leeds", "short_name": "LEE"},
        ],
        "elements": [
            {"id": 10, "first_name": "Danny", "second_name": "Welbeck", "web_name": "Welbeck", "team": 2},
        ],
    }


def _data():
    return {
        "daily": {"date": "", "count": 0, "limit": 15},
        "stories": {}, "posted_ids": [], "pending": {}, "pending_v2": {},
        "extracted": {}, "posted_hashes": [], "posted_headlines": [],
    }


async def _no_cross_verify(*args, **kwargs):
    return {"evidence": [], "log": []}


def test_main_scrape_returns_only_v2_verified_official_claim(monkeypatch, tmp_path):
    fpl = _fpl()
    runtime = VerificationRuntime(fpl_data=fpl, database_path=tmp_path / "v2.sqlite3")
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "id": "official-transfer-1",
        "document_id": "official-transfer-1",
        "title": "Chelsea sign Danny Welbeck from Brighton",
        "summary": "Chelsea sign Danny Welbeck from Brighton",
        "text": "Chelsea sign Danny Welbeck from Brighton",
        "source_url": "https://www.chelseafc.com/en/news/article/welbeck-signs",
        "source_id": "club.chelsea",
        "source_hint": "club.chelsea",
        "username": "chelseafc",
        "transport": "DIRECT_RSS",
        "configured_direct_feed": True,
        "declared_sport": "football",
        "created_at": now,
        "published_at": now,
        "feed_id": "test.chelsea",
    }
    monkeypatch.setattr(main, "fetch_configured_news", lambda rt: ([item], {
        "feeds_total": 1, "feeds_succeeded": 1, "feeds_failed": 0,
        "fail_ratio": 0.0, "failures": [], "at": now,
    }))
    monkeypatch.setattr(main, "fetch_fpl_injury_news", lambda fpl: [])
    monkeypatch.setattr(main, "cross_verify", _no_cross_verify)
    monkeypatch.setattr(main, "enrich_official_item", lambda item, runtime: item)
    monkeypatch.setattr(main, "save_data", lambda data: None)
    queue = asyncio.run(main.scrape(_data(), fpl=fpl, verification_runtime=runtime))
    assert len(queue) == 1
    assert queue[0]["_v2_verified"] is True
    assert queue[0]["_v2_decision"]["decision"] == "PUBLISH"
    runtime.close()


def test_main_scrape_never_queues_cross_sport_candidate(monkeypatch, tmp_path):
    fpl = _fpl()
    runtime = VerificationRuntime(fpl_data=fpl, database_path=tmp_path / "v2.sqlite3")
    now = datetime.now(timezone.utc).isoformat()
    text = (
        "Rockets stay top of Hundred table as Duckett stars. Ben Duckett hit a "
        "half-century as Trent Rockets beat SunRisers Leeds."
    )
    item = {
        "id": "cricket-1", "document_id": "cricket-1",
        "title": "Rockets stay top of Hundred table as Duckett stars",
        "summary": text, "text": text,
        "source_url": "https://www.skysports.com/cricket/news/example",
        "source_id": "media.sky_sports", "source_hint": "media.sky_sports",
        "username": "skysports", "transport": "DIRECT_RSS",
        "configured_direct_feed": True, "declared_sport": None,
        "created_at": now, "published_at": now, "feed_id": "test.sky",
    }
    monkeypatch.setattr(main, "fetch_configured_news", lambda rt: ([item], {
        "feeds_total": 1, "feeds_succeeded": 1, "feeds_failed": 0,
        "fail_ratio": 0.0, "failures": [], "at": now,
    }))
    monkeypatch.setattr(main, "fetch_fpl_injury_news", lambda fpl: [])
    monkeypatch.setattr(main, "cross_verify", _no_cross_verify)
    monkeypatch.setattr(main, "enrich_official_item", lambda item, runtime: item)
    monkeypatch.setattr(main, "save_data", lambda data: None)
    queue = asyncio.run(main.scrape(_data(), fpl=fpl, verification_runtime=runtime))
    assert queue == []
    runtime.close()
