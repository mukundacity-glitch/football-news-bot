"""Quality-gated runner for Premier League tactical intelligence.

This layer keeps the proven verification/data pipeline in tactical_intelligence,
then adds editorial quality gates, dynamic player/team visuals, a jersey fallback,
and an explicit one-time format-trial path.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from src import tactical_intelligence as base
from src.rendering.tactical_enhanced import EnhancedTacticalGraphicRenderer
from src.tactical_quality import TREND_SCORE, enhance_candidate

_ORIGINAL_OFFICIAL_SNAPSHOT = base.official_snapshot
_ORIGINAL_BUILD_WATCH = base.build_watch
_ORIGINAL_BUILD_REVIEW = base.build_review
_ORIGINAL_LOAD_CONFIG = base.load_config
_ORIGINAL_POSTED_TODAY = base.posted_today
_BOOTSTRAP: dict[str, Any] = {}


def _official_snapshot():
    result = _ORIGINAL_OFFICIAL_SNAPSHOT()
    bootstrap = result[0].data if getattr(result[0], "data", None) else {}
    _BOOTSTRAP.clear()
    if isinstance(bootstrap, dict):
        _BOOTSTRAP.update(bootstrap)
    return result


def _build_watch(*args, **kwargs):
    candidate = _ORIGINAL_BUILD_WATCH(*args, **kwargs)
    provider_row = args[1] if len(args) > 1 else kwargs.get("provider_row") or {}
    return enhance_candidate(
        candidate,
        _BOOTSTRAP,
        provider_row,
        provider_get=base.provider_get,
    )


def _build_review(*args, **kwargs):
    candidate = _ORIGINAL_BUILD_REVIEW(*args, **kwargs)
    provider_row = args[1] if len(args) > 1 else kwargs.get("provider_row") or {}
    return enhance_candidate(
        candidate,
        _BOOTSTRAP,
        provider_row,
        provider_get=base.provider_get,
    )


def _load_config() -> dict[str, Any]:
    config = dict(_ORIGINAL_LOAD_CONFIG())
    # 75+ = priority, 60-74 = strong, 45-59 = Tactical Trend. Because the
    # chooser already selects the highest-scoring verified story, a Trend is
    # published only when no stronger qualifying story exists.
    config["minimum_publish_score"] = int(config.get("minimum_trend_score", TREND_SCORE))
    return config


def _posted_today(state: Mapping[str, Any], now, tz, mode: str | None = None) -> int:
    """Trial posts are real X posts but do not consume either daily editorial slot."""
    rows = state.get("posts", []) if isinstance(state, Mapping) else []
    local_day = now.astimezone(tz).date().isoformat()
    return sum(
        1
        for row in rows
        if row.get("local_day") == local_day
        and row.get("published")
        and not row.get("trial")
        and (mode is None or row.get("mode") == mode)
    )


def _install() -> None:
    base.official_snapshot = _official_snapshot
    base.build_watch = _build_watch
    base.build_review = _build_review
    base.load_config = _load_config
    base.posted_today = _posted_today
    base.TacticalGraphicRenderer = EnhancedTacticalGraphicRenderer


def _trial_config(config: Mapping[str, Any]) -> dict[str, Any]:
    trial = dict(config)
    # A format trial should not depend on the normal 09:00 window. It still uses
    # live verified fixtures and the same scoring/quality gates.
    trial["watch_fixture_min_hours"] = 0
    trial["watch_fixture_max_hours"] = max(120, int(config.get("watch_fixture_max_hours", 36)))
    trial["minimum_publish_score"] = int(config.get("minimum_trend_score", TREND_SCORE))
    return trial


def run_trial(*, dry_run: bool = False) -> int:
    config = _trial_config(_load_config())
    now = base.utcnow()
    tz = ZoneInfo(str(config["audience_timezone"]))
    state = base.load_json(base.STATE_PATH, {"posts": [], "full_time_seen": {}})
    status: dict[str, Any] = {
        "checked_at": base.iso(now),
        "mode": "trial",
        "published": False,
        "reason": "",
    }

    bootstrap, fixtures_response, teams, fixtures, ownership = _official_snapshot()
    official_sources = [
        base.source_record("official_bootstrap", bootstrap),
        base.source_record("official_fixtures", fixtures_response),
    ]
    candidate = base.choose_watch(
        fixtures,
        teams,
        ownership,
        config,
        now,
        tz,
        official_sources,
    )
    if candidate is None:
        status["reason"] = "no_verified_trial_candidate"
        base.save_json(base.STATE_PATH, state)
        base.save_json(base.RUN_STATUS_PATH, status)
        print(json.dumps(status, indent=2))
        return 0

    threshold = int(config["minimum_publish_score"])
    if candidate.priority_score < threshold:
        status.update({
            "reason": "trial_priority_below_threshold",
            "priority_score": candidate.priority_score,
        })
        base.save_json(base.STATE_PATH, state)
        base.save_json(base.RUN_STATUS_PATH, status)
        print(json.dumps(status, indent=2))
        return 0

    base.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output = base.OUTPUT_DIR / f"{now.astimezone(tz):%Y%m%d-%H%M}-trial-{candidate.fixture.fixture_id}.png"
    EnhancedTacticalGraphicRenderer().render(candidate.post, output)

    manual_review = bool(config.get("manual_review")) or os.getenv("TACTICAL_MANUAL_REVIEW", "").casefold() == "true"
    autopost = os.getenv("ENABLE_TACTICAL_AUTOPOST", "true").casefold() == "true"
    published = False
    delivery_url = ""
    if not dry_run and not manual_review and autopost:
        trial_caption = base.caption(candidate)
        suffix = "\n\nFormat trial"
        if len(trial_caption) + len(suffix) <= 280:
            trial_caption += suffix
        delivery_url = asyncio.run(base.publish_x(str(output), trial_caption))
        published = True

    row = {
        "key": f"trial-{base.post_key(candidate)}-{now:%Y%m%d%H%M}",
        "mode": "trial",
        "trial": True,
        "fixture_id": candidate.fixture.fixture_id,
        "teams": [candidate.fixture.home, candidate.fixture.away],
        "priority_score": candidate.priority_score,
        "priority_tier": candidate.post.get("priority_tier"),
        "heading": candidate.post["heading"],
        "thesis": candidate.post["thesis"],
        "status": candidate.post["status"],
        "local_day": now.astimezone(tz).date().isoformat(),
        "created_at": base.iso(now),
        "published": published,
        "delivery_url": delivery_url,
        "image": str(output),
        "sources": candidate.source_records,
    }
    state.setdefault("posts", []).append(row)
    state["posts"] = state["posts"][-250:]
    status.update({
        "published": published,
        "reason": "trial_published" if published else "trial_draft_created",
        "priority_score": candidate.priority_score,
        "candidate": row,
    })
    base.save_json(base.STATE_PATH, state)
    base.save_json(base.RUN_STATUS_PATH, status)
    print(json.dumps(status, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["auto", "watch", "review", "trial"], default="auto")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    env_dry = os.getenv("TACTICAL_DRY_RUN", "").casefold() == "true"
    dry_run = args.dry_run or env_dry

    _install()
    trial_env = os.getenv("TACTICAL_TRIAL_POST", "").casefold() == "true"
    if args.mode == "trial" or trial_env:
        return run_trial(dry_run=dry_run)
    return base.run(args.mode, dry_run=dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
