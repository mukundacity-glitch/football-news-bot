#!/usr/bin/env python3
"""Publish exactly one verified PremierLeague.com press roundup per FPL gameweek."""
from __future__ import annotations

import asyncio
import json
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import main as bot
from src.verification import VerificationRuntime
from src.verification.enrichment import enrich_official_item
from src.verification.press_conference_gate import validate_official_press_conference
from src.verification.press_roundup import (
    PREMIER_LEAGUE_SOURCE_ID,
    PRESS_FEED_ID,
    is_premier_league_url,
    project_roundup_story,
)
from tools.press_deadline_window import deadline_window_status, fetch_fpl_bootstrap

ROOT = Path(__file__).resolve().parents[1]
COLLECTION_PATH = ROOT / "data" / "press_conference_collection.json"
LOCK_PATH = ROOT / "data" / "press_conference_status.json"
RUN_STATUS_PATH = ROOT / "data" / "last_run_status.json"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else dict(default)
    except Exception:
        return dict(default)


def _next_event(fpl_data: dict[str, Any], now: datetime) -> Optional[dict[str, Any]]:
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for event in fpl_data.get("events", []) or []:
        try:
            deadline = datetime.fromisoformat(
                str(event.get("deadline_time") or "").replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            continue
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        deadline = deadline.astimezone(timezone.utc)
        if deadline > now:
            candidates.append((deadline, event))
    if not candidates:
        return None
    deadline, event = min(candidates, key=lambda pair: pair[0])
    return {
        "event_id": str(event.get("id") or event.get("name") or deadline.isoformat()),
        "event_name": str(event.get("name") or "Next Gameweek"),
        "deadline": deadline,
    }


def _article_item(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(raw.get("id") or ""),
        "title": str(raw.get("title") or ""),
        "summary": "",
        "text": str(raw.get("title") or ""),
        "source_url": raw.get("link"),
        "publisher_url": raw.get("publisher_url"),
        "publisher_name": raw.get("publisher_name"),
        "source_id": PREMIER_LEAGUE_SOURCE_ID,
        "source_hint": PREMIER_LEAGUE_SOURCE_ID,
        "username": "premierleague",
        "transport": "GOOGLE_NEWS",
        "configured_direct_feed": False,
        "declared_sport": "football",
        "feed_id": PRESS_FEED_ID,
        "created_at": raw.get("published_at"),
        "published_at": raw.get("published_at"),
        "fetched_at": utcnow().isoformat(),
        "metadata": {},
    }


def _candidate(
    raw: dict[str, Any],
    runtime: VerificationRuntime,
) -> Optional[tuple[int, str, dict[str, Any], dict[str, Any]]]:
    item = _article_item(raw)
    try:
        document = runtime.documents.from_item(item)
    except Exception:
        return None
    if (
        not document.source.verified
        or document.source.profile_id != PREMIER_LEAGUE_SOURCE_ID
    ):
        return None

    item = enrich_official_item(item, runtime)
    if not item.get("full_text") or not is_premier_league_url(item.get("source_url")):
        return None
    item.setdefault("metadata", {})["premier_league_press_roundup"] = True

    story: dict[str, Any] = {"event": "press_conference"}
    if not project_roundup_story(
        story,
        item,
        resolve_staff=runtime.entities.resolve_staff,
        resolve_club_key=bot.resolve_club_key,
    ):
        return None
    story["created_at"] = item.get("created_at")
    story["source_url"] = item.get("source_url")
    return (
        len(story.get("roundup") or []),
        str(item.get("published_at") or item.get("created_at") or ""),
        item,
        story,
    )


def _already_posted(lock: dict[str, Any], event_id: str) -> bool:
    return str(event_id) in {
        str(key) for key in (lock.get("posted_events") or {}).keys()
    }


def _mark_posted(
    lock: dict[str, Any],
    *,
    event: dict[str, Any],
    fingerprint: str,
    source_url: Optional[str],
) -> None:
    lock.setdefault("schema_version", 1)
    lock.setdefault("posted_events", {})[str(event["event_id"])] = {
        "event_name": event["event_name"],
        "deadline": event["deadline"].isoformat(),
        "fingerprint": fingerprint,
        "source_url": source_url,
        "posted_at": utcnow().isoformat(),
    }
    _write_json(LOCK_PATH, lock)


def _status(**values: Any) -> None:
    _write_json(
        RUN_STATUS_PATH,
        {
            "run_type": "press_conference_roundup",
            "updated_at": utcnow().isoformat(),
            **values,
        },
    )


async def run() -> int:
    now = utcnow()
    fpl_data = fetch_fpl_bootstrap()
    event = _next_event(fpl_data, now)
    if event is None:
        _status(run_exit="no_future_fpl_deadline", posted_count=0)
        return 0

    in_window, reason, _deadline, window_start = deadline_window_status(
        fpl_data,
        now=now,
        start_before_minutes=60,
        end_before_minutes=30,
    )
    if not in_window:
        _status(
            run_exit="outside_publication_window",
            posted_count=0,
            event_id=event["event_id"],
            window_reason=reason,
        )
        return 0

    lock = _load_json(LOCK_PATH, {"schema_version": 1, "posted_events": {}})
    if _already_posted(lock, event["event_id"]):
        _status(
            run_exit="gameweek_already_posted",
            posted_count=0,
            event_id=event["event_id"],
        )
        print(f"[PRESS] {event['event_name']} already posted; no repetition.")
        return 0

    collection = _load_json(COLLECTION_PATH, {})
    if str(collection.get("event_id") or "") != str(event["event_id"]):
        _status(
            run_exit="collection_not_ready",
            posted_count=0,
            event_id=event["event_id"],
        )
        return 0

    runtime = VerificationRuntime(fpl_data=fpl_data)
    bot._VERIFICATION_RUNTIME = runtime
    try:
        if not runtime.live_enabled:
            _status(run_exit="verification_shadow_mode", posted_count=0)
            return 0

        rollout = runtime.config.rollout_config
        rebuild_allowed = (
            os.getenv(rollout["database_rebuild_environment_variable"], "")
            == rollout["database_rebuild_required_value"]
        )
        if runtime.database_was_empty and not rebuild_allowed:
            _status(run_exit="verification_database_empty", posted_count=0)
            return 0

        candidates = [
            candidate
            for raw in collection.get("items", []) or []
            if (candidate := _candidate(raw, runtime)) is not None
        ]
        if not candidates:
            _status(
                run_exit="no_parseable_official_roundup",
                posted_count=0,
                collected_count=len(collection.get("items", []) or []),
            )
            return 0

        _count, _published, source_item, story = max(
            candidates, key=lambda row: (row[0], row[1])
        )
        decision = runtime.verify_observations([{
            "document": bot._v2_document_item(source_item),
            "legacy_story": dict(story),
        }])
        validation = validate_official_press_conference(decision, runtime.sources)
        if not decision.may_publish or not validation.ok:
            _status(
                run_exit="roundup_not_publishable",
                posted_count=0,
                decision=decision.decision.value,
                reasons=decision.reasons[:10],
                press_gate=validation.reason,
            )
            return 0

        if runtime.repository.has_publication_fingerprint(decision.fingerprint):
            _mark_posted(
                lock,
                event=event,
                fingerprint=decision.fingerprint,
                source_url=decision.source_url,
            )
            _status(
                run_exit="already_published_fingerprint",
                posted_count=0,
                fingerprint=decision.fingerprint,
            )
            return 0

        item = dict(story)
        bot._v2_project_verified_facts(item, decision)
        data = bot.load_data()

        if not bot.ENABLE_AUTOPOST:
            _status(run_exit="autopost_disabled", posted_count=0)
            return 0
        if bot.in_cooldown(data):
            _status(
                run_exit="x_cooldown_active",
                posted_count=0,
                cooldown_until=data.get("cooldown_until"),
            )
            return 0
        if not bot.check_daily_limit(data):
            _status(run_exit="daily_limit_reached", posted_count=0)
            return 0

        hour_cap = bot._positive_cap(bot.MAX_POSTS_PER_HOUR)
        if (
            hour_cap is not None
            and bot._recent_post_count(data, 3600) >= hour_cap
        ):
            _status(run_exit="hourly_limit_reached", posted_count=0)
            return 0

        draft = await bot.build_draft(item, data, fpl_data)
        if draft is None:
            _status(run_exit="press_graphic_build_failed", posted_count=0)
            return 0

        if not (bot.X_POST_AUTH_TOKEN and bot.X_POST_CT0_TOKEN):
            _status(run_exit="missing_posting_credentials", posted_count=0)
            return 0

        client = bot.Client("en-US")
        client.set_cookies({
            "auth_token": bot.X_POST_AUTH_TOKEN,
            "ct0": bot.X_POST_CT0_TOKEN,
        })
        jitter = random.randint(*bot.POST_JITTER_RANGE_S)
        print(f"[PRESS] Verified roundup ready; posting after {jitter}s pacing delay.")
        await asyncio.sleep(jitter)

        posted = await bot.post_item(client, draft, data)
        ledger_has_post = runtime.repository.has_publication_fingerprint(
            decision.fingerprint
        )
        if posted or ledger_has_post:
            _mark_posted(
                lock,
                event=event,
                fingerprint=decision.fingerprint,
                source_url=decision.source_url,
            )
            _status(
                run_exit="posted" if posted else "x_duplicate_recorded",
                posted_count=1 if posted else 0,
                event_id=event["event_id"],
                event_name=event["event_name"],
                deadline=event["deadline"].isoformat(),
                window_start=window_start.isoformat() if window_start else None,
                fingerprint=decision.fingerprint,
                roundup_count=len(decision.verified_facts.get("roundup") or []),
                source_url=decision.source_url,
            )
            print(f"[PRESS] {event['event_name']} locked after publication; no repeat.")
        else:
            _status(run_exit="post_not_completed", posted_count=0)
        return 0
    finally:
        bot._VERIFICATION_RUNTIME = None
        runtime.close()


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
