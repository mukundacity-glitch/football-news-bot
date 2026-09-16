"""Quality-gated runner for Premier League tactical intelligence.

Official FPL evidence can run through the user's FPL-VORTEX-AUTO Day 1 source
without a paid football-data key. API-Football remains optional enrichment.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from src import fpl_vortex_day1 as day1
from src import tactical_intelligence as base
from src.rendering.tactical_enhanced import EnhancedTacticalGraphicRenderer
from src.tactical_quality import TREND_SCORE, enhance_candidate
from src.twikit_runtime import apply_twikit_transaction_patch

_ORIGINAL_REQUEST_JSON = base.request_json
_ORIGINAL_OFFICIAL_SNAPSHOT = base.official_snapshot
_ORIGINAL_BUILD_WATCH = base.build_watch
_ORIGINAL_BUILD_REVIEW = base.build_review
_ORIGINAL_CHOOSE_WATCH = base.choose_watch
_ORIGINAL_CHOOSE_REVIEW = base.choose_review
_ORIGINAL_LOAD_CONFIG = base.load_config
_BOOTSTRAP: dict[str, Any] = {}


def _request_json(url: str, *, headers=None, params=None):
    if day1.available():
        return day1.request_json(
            url,
            headers=headers,
            params=params,
            fallback=_ORIGINAL_REQUEST_JSON,
        )
    return _ORIGINAL_REQUEST_JSON(url, headers=headers, params=params)


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
    return enhance_candidate(candidate, _BOOTSTRAP, provider_row, provider_get=base.provider_get)


def _build_review(*args, **kwargs):
    candidate = _ORIGINAL_BUILD_REVIEW(*args, **kwargs)
    provider_row = args[1] if len(args) > 1 else kwargs.get("provider_row") or {}
    return enhance_candidate(candidate, _BOOTSTRAP, provider_row, provider_get=base.provider_get)


def _day1_choose_watch(fixtures, teams, ownership, config, now, tz, official_sources):
    return day1.choose_watch(
        fixtures,
        teams,
        ownership,
        config,
        now,
        tz,
        official_sources,
        bootstrap=_BOOTSTRAP,
    )


def _day1_choose_review(fixtures, teams, ownership, config, now, tz, state, official_sources):
    return day1.choose_review(
        fixtures,
        teams,
        ownership,
        config,
        now,
        tz,
        state,
        official_sources,
        bootstrap=_BOOTSTRAP,
    )


def _load_config() -> dict[str, Any]:
    config = dict(_ORIGINAL_LOAD_CONFIG())
    # The score is a ranking layer after evidence gates. A 45-59 story may only
    # publish as Tactical Trend when it is the strongest verified option.
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


def _trim_words(value: object, max_chars: int) -> str:
    """Trim on a word boundary without adding characters outside the budget."""
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    if max_chars <= 0:
        return ""
    clipped = text[:max_chars].rsplit(" ", 1)[0].strip()
    return clipped or text[:max_chars].strip()


def _x_caption(candidate: Any, *, trial: bool = False) -> str:
    """Create an intentionally conservative X caption.

    X applies its own weighted-length rules. The graphic already contains the
    full explanation and evidence, so captions stay well below the nominal
    280-character ceiling instead of relying on raw Python slicing at 280.
    """
    post = candidate.post
    heading = " ".join(str(post.get("heading") or "TACTICAL WATCH").split())
    topic = " ".join(str(post.get("topic_line") or "").split())
    thesis = " ".join(str(post.get("thesis") or "").split())
    tags = "#PremierLeague #FPL"
    marker = "Format trial" if trial else ""
    limit = 220 if trial else 235

    def compose(current_topic: str, current_thesis: str) -> str:
        parts = [heading, current_topic, "", current_thesis, "", tags]
        if marker:
            parts.extend(["", marker])
        return "\n".join(parts).strip()

    text = compose(topic, thesis)
    if len(text) > limit:
        topic = f"{candidate.fixture.home} vs {candidate.fixture.away}"
        text = compose(topic, thesis)

    if len(text) > limit:
        without_thesis = compose(topic, "")
        thesis_budget = max(24, limit - len(without_thesis) - 1)
        thesis = _trim_words(thesis, thesis_budget)
        text = compose(topic, thesis)

    # Final defensive cap; normal inputs should already fit on word boundaries.
    return text[:limit].rstrip()


def _install() -> None:
    # Use the same Twikit transaction compatibility behavior as the established
    # news publisher before any media upload is attempted.
    apply_twikit_transaction_patch()

    # Official FPL requests use the user's Day 1 retry/request layer whenever
    # that checked-out source is available.
    base.request_json = _request_json
    base.official_snapshot = _official_snapshot
    base.load_config = _load_config
    base.posted_today = _posted_today
    base.caption = _x_caption
    base.TacticalGraphicRenderer = EnhancedTacticalGraphicRenderer

    if base.provider_key():
        # Rich licensed provider path: retain the existing deeper match analysis.
        base.build_watch = _build_watch
        base.build_review = _build_review
        base.choose_watch = _ORIGINAL_CHOOSE_WATCH
        base.choose_review = _ORIGINAL_CHOOSE_REVIEW
    elif day1.available():
        # Free path: Official FPL + fixture-specific player summaries from the
        # Day 1 source. Claims are intentionally narrower than provider-backed
        # possession/shot/corner analysis.
        base.build_watch = _ORIGINAL_BUILD_WATCH
        base.build_review = _ORIGINAL_BUILD_REVIEW
        base.choose_watch = _day1_choose_watch
        base.choose_review = _day1_choose_review


def _trial_config(config: Mapping[str, Any]) -> dict[str, Any]:
    trial = dict(config)
    trial["watch_fixture_min_hours"] = 0
    trial["watch_fixture_max_hours"] = max(120, int(config.get("watch_fixture_max_hours", 36)))
    trial["minimum_publish_score"] = int(config.get("minimum_trend_score", TREND_SCORE))
    return trial


def _missing_day1_source(mode: str) -> int:
    status = {
        "checked_at": base.iso(base.utcnow()),
        "mode": mode,
        "published": False,
        "reason": "missing_fpl_vortex_day1_source",
        "expected_path": str(day1.DAY1_PATH),
        "optional_enrichment": "API_FOOTBALL_KEY or APIFOOTBALL_KEY",
    }
    base.save_json(base.RUN_STATUS_PATH, status)
    print(json.dumps(status, indent=2))
    return 0


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
        "data_path": "structured_provider" if base.provider_key() else "fpl_vortex_day1",
    }

    bootstrap, fixtures_response, teams, fixtures, ownership = _official_snapshot()
    official_sources = [
        base.source_record("official_bootstrap", bootstrap),
        base.source_record("official_fixtures", fixtures_response),
    ]
    candidate = base.choose_watch(fixtures, teams, ownership, config, now, tz, official_sources)
    if candidate is None:
        status["reason"] = "no_verified_trial_candidate"
        base.save_json(base.STATE_PATH, state)
        base.save_json(base.RUN_STATUS_PATH, status)
        print(json.dumps(status, indent=2))
        return 0

    threshold = int(config["minimum_publish_score"])
    if candidate.priority_score < threshold:
        status.update({"reason": "trial_priority_below_threshold", "priority_score": candidate.priority_score})
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
        trial_caption = _x_caption(candidate, trial=True)
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
    effective_mode = "trial" if args.mode == "trial" or trial_env else args.mode

    if not base.provider_key() and not day1.available():
        return _missing_day1_source(effective_mode)
    if effective_mode == "trial":
        return run_trial(dry_run=dry_run)
    return base.run(args.mode, dry_run=dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
