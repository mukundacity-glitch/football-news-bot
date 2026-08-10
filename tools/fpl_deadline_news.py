#!/usr/bin/env python3
"""Press-conference window poster for FPL Vortex.

This runner is intentionally narrow:
- it only posts verified PRESS_CONFERENCE items
- it only runs inside the 3-hour window before the next FPL deadline
- it posts every qualifying item, with no artificial top-N cap
- it performs a preview validation before each live post
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
FPL_HEADERS = {
    "User-Agent": "FPLVortexBot/2.0 (+https://github.com/mukundacity-glitch/football-news-bot)",
    "Accept": "application/json",
}
STATUS_FILE = Path("data/press_conference_status.json")
CARD_DIR = Path("queue/press_conference")
CARD_DIR.mkdir(parents=True, exist_ok=True)

WINDOW_HOURS = int(os.getenv("PRESS_CONFERENCE_WINDOW_HOURS", "3"))
ENABLE_AUTOPOST = os.getenv("ENABLE_AUTOPOST", "true").strip().lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() == "true"
X_AUTH_TOKEN = (os.getenv("X_POST_AUTH_TOKEN") or os.getenv("X_AUTH_TOKEN") or "").strip()
X_CT0_TOKEN = (os.getenv("X_POST_CT0_TOKEN") or os.getenv("X_CT0_TOKEN") or "").strip()

TWIKIT_SUCCESS_PARSE_KEYS = {
    "urls",
    "withheld_in_countries",
    "pinned_tweet_ids_str",
    "entities",
    "extended_entities",
    "card",
}

X_DUPLICATE_CODES = {"187"}
X_FLAG_CODES = {"226", "326", "334", "64", "261"}
X_RATELIMIT_CODES = {"429", "88"}
X_AUTH_CODES = {"32", "89", "99", "135", "215", "401"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fetch_json(url: str, *, headers: dict[str, str] | None = None, timeout: int = 20) -> dict[str, Any]:
    response = requests.get(url, headers=headers or {}, timeout=timeout)
    response.raise_for_status()
    return response.json()


def fetch_fpl_bootstrap() -> dict[str, Any]:
    return fetch_json(FPL_BOOTSTRAP_URL, headers=FPL_HEADERS)


def parse_dt(raw: str) -> datetime:
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class DeadlineEvent:
    event_id: int | str
    name: str
    deadline: datetime
    target_run: datetime


def find_next_event(fpl: dict[str, Any], now: datetime | None = None) -> DeadlineEvent | None:
    now = now or utcnow()
    candidates: list[DeadlineEvent] = []
    for ev in fpl.get("events", []) or []:
        raw_deadline = ev.get("deadline_time")
        if not raw_deadline:
            continue
        try:
            deadline = parse_dt(raw_deadline)
        except Exception:
            continue
        if deadline <= now:
            continue
        event_id = ev.get("id") or ev.get("name") or deadline.strftime("%Y%m%d%H%M")
        name = ev.get("name") or f"GW{event_id}"
        candidates.append(
            DeadlineEvent(
                event_id=event_id,
                name=name,
                deadline=deadline,
                target_run=deadline - timedelta(hours=WINDOW_HOURS),
            )
        )
    if not candidates:
        return None
    return min(candidates, key=lambda e: e.deadline)


def deadline_window_status(
    fpl: dict[str, Any],
    *,
    now: datetime | None = None,
    window_hours: int = WINDOW_HOURS,
) -> tuple[bool, str, DeadlineEvent | None]:
    now = now or utcnow()
    event = find_next_event(fpl, now)
    if event is None:
        return False, "no_future_fpl_deadline", None
    target = event.deadline - timedelta(hours=window_hours)
    if target <= now < event.deadline:
        return True, f"inside_{event.name}_press_conference_window", event
    if now < target:
        return False, f"too_early_for_{event.name}_target_{target.isoformat()}", event
    return False, f"outside_{event.name}_window_target_{target.isoformat()}", event


def write_status(payload: dict[str, Any]) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = dict(payload)
    data["updated_at"] = utcnow().isoformat()
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    tmp.replace(STATUS_FILE)


def classify_x_error(exc: Exception) -> str:
    text = str(exc).lower()
    cls = type(exc).__name__.lower()
    codes = set(re.findall(r'code[\"\']?\s*[:=]\s*[\"\']?(\d+)', text))
    if codes & X_DUPLICATE_CODES or "duplicate" in text or "duplicatetweet" in cls:
        return "duplicate"
    if codes & X_RATELIMIT_CODES or "toomanyrequests" in cls or "rate limit" in text:
        return "rate_limited"
    if codes & X_FLAG_CODES or "automated" in text or "spam" in text or "locked" in text or "suspend" in text:
        return "flagged"
    if codes & X_AUTH_CODES or "could not authenticate" in text or "unauthorized" in cls or "invalid or expired" in text:
        return "auth"
    return "transient"


async def post_to_x(text: str, image_path: Path) -> str:
    from twikit import Client

    if not X_AUTH_TOKEN or not X_CT0_TOKEN:
        raise RuntimeError("missing X_POST_AUTH_TOKEN / X_POST_CT0_TOKEN")
    client = Client("en-US")
    client.set_cookies({"auth_token": X_AUTH_TOKEN, "ct0": X_CT0_TOKEN})
    try:
        media_id = await client.upload_media(str(image_path), media_type="image/png")
        await client.create_tweet(text=text, media_ids=[media_id])
    except KeyError as ke:
        key = str(ke).strip("'\"")
        if key in TWIKIT_SUCCESS_PARSE_KEYS:
            print(f"[WARN] twikit KeyError({ke}) after create_tweet — treating as posted.")
            return "posted_keyerror_success"
        raise
    return "posted"


def preview_caption(caption: str) -> None:
    lines = [line for line in (caption or "").splitlines() if line.strip()]
    if len(lines) > 4:
        raise RuntimeError(f"caption exceeds 4 lines: {len(lines)}")
    if "http" in caption.lower() or "source:" in caption.lower():
        raise RuntimeError("caption contains a URL or source line")
    if "#premierleague" not in caption.lower():
        raise RuntimeError("caption is missing #PremierLeague")


def item_slug(decision) -> str:
    subject = decision.verified_facts.get("subject_name") or decision.verified_facts.get("club_name") or "press"
    subject = re.sub(r"[^A-Za-z0-9]+", "_", str(subject)).strip("_").lower() or "press"
    fingerprint = getattr(decision, "fingerprint", "")[:12]
    return f"{subject}_{fingerprint}" if fingerprint else subject


def build_observation(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "document": item,
        "legacy_story": dict(item.get("_legacy_story") or {}),
    }


def augment_press_conference_queries(runtime) -> None:
    """Add press-conference search terms without needing a config file edit."""
    official = runtime.feeds.official_discovery
    if not official.get("enabled"):
        return
    terms = list(official.get("query_terms") or [])
    extras = [
        "press conference",
        "pre-match press conference",
        "post-match press conference",
        "team news",
        "news conference",
        "manager press conference",
    ]
    changed = False
    for term in extras:
        if term not in terms:
            terms.append(term)
            changed = True
    if changed:
        official["query_terms"] = terms


async def run(*, dry_run: bool = False, force_window: bool = False) -> int:
    from src.verification import EventType, VerificationRuntime
    from src.verification.card import create_verified_card
    from src.verification.ingestion import fetch_configured_news

    status: dict[str, Any] = {"run_type": "press_conference_window", "dry_run": dry_run, "force_window": force_window}
    fpl_data = fetch_fpl_bootstrap()
    now = utcnow()
    window_ok, reason, event = deadline_window_status(fpl_data, now=now)

    status.update({"window_ok": window_ok, "window_reason": reason})
    if event:
        status.update(
            {
                "event_id": event.event_id,
                "event_name": event.name,
                "deadline": event.deadline.isoformat(),
                "target_run": event.target_run.isoformat(),
            }
        )
    if not force_window and not window_ok:
        print(f"[PRESS-CONFERENCE] No-op: {reason}")
        write_status(status | {"run_exit": "outside_window"})
        return 0
    if not event:
        print("[PRESS-CONFERENCE] No-op: no future FPL deadline found.")
        write_status(status | {"run_exit": "no_deadline"})
        return 0

    runtime = VerificationRuntime(fpl_data=fpl_data)
    try:
        augment_press_conference_queries(runtime)
        items, report = fetch_configured_news(runtime)
        if report.get("failures"):
            print(f"[PRESS-CONFERENCE] feed issues: {report['failures'][:3]}")

        candidates: list[tuple[dict[str, Any], Any]] = []
        for item in items:
            try:
                decision = runtime.verify_observations([build_observation(item)])
            except Exception as exc:
                print(f"[PRESS-CONFERENCE] verify failed: {type(exc).__name__}: {exc}")
                continue
            if decision.event_type != EventType.PRESS_CONFERENCE:
                continue
            if not decision.may_publish:
                continue
            candidates.append((item, decision))

        status["candidate_count"] = len(candidates)
        if not candidates:
            print("[PRESS-CONFERENCE] No verified press-conference items in this window.")
            write_status(status | {"run_exit": "no_verified_press_conference"})
            return 0

        posted = 0
        for item, decision in candidates:
            if runtime.repository.has_publication_fingerprint(decision.fingerprint):
                print(f"[PRESS-CONFERENCE] Duplicate skipped: {decision.verified_facts.get('subject_name') or decision.story_id}")
                continue

            caption = decision.rendered_text or ""
            preview_caption(caption)

            slug = item_slug(decision)
            image_path = CARD_DIR / f"{slug}.png"
            create_verified_card(decision, runtime.sources, image_path, fpl_data=fpl_data)

            if not image_path.exists() or image_path.stat().st_size < 1000:
                print(f"[PRESS-CONFERENCE] card generation failed for {slug}")
                continue

            print("[PRESS-CONFERENCE] Preview caption:\n" + caption)

            if dry_run or not ENABLE_AUTOPOST:
                reason_live = "dry_run" if dry_run else "autopost_disabled"
                print(f"[PRESS-CONFERENCE] Not posting ({reason_live}).")
                status.setdefault("dry_run_items", []).append(
                    {
                        "story_id": decision.story_id,
                        "subject": decision.verified_facts.get("subject_name"),
                        "event_type": decision.event_type.value,
                    }
                )
                continue

            try:
                result = await post_to_x(caption, image_path)
            except Exception as exc:
                kind = classify_x_error(exc)
                if kind == "duplicate":
                    print("[PRESS-CONFERENCE] X duplicate returned; marking published.")
                    try:
                        runtime.repository.mark_published(decision)
                    except Exception:
                        pass
                    continue
                if kind in {"flagged", "rate_limited"}:
                    write_status(status | {"run_exit": f"x_{kind}", "error": str(exc)[:300]})
                    return 2
                if kind == "auth":
                    write_status(status | {"run_exit": "x_auth_expired", "error": str(exc)[:300]})
                    return 2
                write_status(status | {"run_exit": "x_transient_error", "error": str(exc)[:300]})
                return 1

            try:
                runtime.repository.mark_published(decision)
            except Exception as exc:
                print(f"[PRESS-CONFERENCE] publication ledger warning: {exc}")
            posted += 1
            print(f"[PRESS-CONFERENCE] ✅ POSTED {decision.verified_facts.get('subject_name') or decision.story_id} ({result}).")

        status.update({"posted_count": posted, "matched_count": len(candidates)})
        write_status(status | {"run_exit": "posted" if posted else "no_post"})
        return 0
    finally:
        runtime.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Post verified press-conference news only inside the 3-hour pre-deadline window.")
    parser.add_argument("--dry-run", action="store_true", help="Preview and validate, but do not post.")
    parser.add_argument("--force-window", action="store_true", help="Manual testing only: ignore the window gate.")
    parser.add_argument("--check-window", action="store_true", help="Print GitHub output variables only.")
    args = parser.parse_args()

    if args.check_window:
        fpl_data = fetch_fpl_bootstrap()
        ok, reason, event = deadline_window_status(fpl_data)
        print(f"should_run={'true' if ok else 'false'}")
        print(f"reason={reason}")
        if event:
            print(f"event_id={event.event_id}")
            print(f"deadline={event.deadline.isoformat()}")
            print(f"target_run={event.target_run.isoformat()}")
        return 0

    return asyncio.run(run(dry_run=args.dry_run, force_window=args.force_window))


if __name__ == "__main__":
    raise SystemExit(main())