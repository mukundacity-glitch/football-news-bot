"""Read-only live diagnostics for transfer/injury/suspension discovery.

This script never creates an X client and never calls the posting path. It is
intended only to expose where a current source item is filtered before V2.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import main as bot
from src.verification import VerificationRuntime
from src.verification.ingestion import fetch_configured_news


def _age_hours(value: object) -> float | None:
    dt = bot._parse_rss_date(value)
    if dt is None:
        return None
    return round((datetime.now(timezone.utc) - dt).total_seconds() / 3600.0, 2)


def _fpl_document(item: dict) -> dict:
    enriched = dict(item)
    enriched.update({
        "title": item.get("text", ""),
        "summary": item.get("_fpl_pre_built", {}).get("body", ""),
        "source_url": "https://fantasy.premierleague.com/api/bootstrap-static/",
        "source_id": "official.fpl",
        "source_hint": "official.fpl",
        "source_handle": "officialfpl",
        "transport": "DIRECT_API",
        "configured_direct_feed": True,
        "declared_sport": "football",
        "feed_id": "official.fpl.bootstrap",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "structured_official": True,
            "competition_id": "competition:premier-league",
        },
    })
    return enriched


def main() -> int:
    fpl = bot.fetch_fpl_data()
    if not fpl:
        print("DIAG_ERROR: FPL bootstrap unavailable")
        return 1

    with tempfile.TemporaryDirectory(prefix="fpl-vortex-diag-") as td:
        runtime = VerificationRuntime(
            fpl_data=fpl,
            database_path=Path(td) / "verification.sqlite3",
        )
        try:
            state = bot.load_data()
            posted_ids = set(state.get("posted_ids", []))

            print("=== STRUCTURED OFFICIAL FPL AVAILABILITY ===")
            fpl_items = bot.fetch_fpl_injury_news(fpl)
            for raw in fpl_items:
                item = _fpl_document(raw)
                story = dict(item["_fpl_pre_built"])
                bot._v2_refine_candidate_hint(story, item, runtime)
                observation = {
                    "document": bot._v2_document_item(item),
                    "legacy_story": story,
                }
                decision = runtime.verify_observations([observation])
                row = {
                    "player": story.get("player"),
                    "event": story.get("event"),
                    "item_id": item.get("id"),
                    "already_in_posted_ids": item.get("id") in posted_ids,
                    "created_at": item.get("created_at"),
                    "age_hours": _age_hours(item.get("created_at")),
                    "chance": story.get("chance_of_playing"),
                    "availability_status": story.get("availability_status"),
                    "diagnosis": story.get("diagnosis"),
                    "decision": decision.decision.value,
                    "status": decision.status.value,
                    "failed_gates": [
                        gate.name for gate in decision.gates if gate.state.value != "PASS"
                    ],
                    "reasons": decision.reasons[:6],
                }
                print(json.dumps(row, ensure_ascii=False, sort_keys=True))

            print("=== FRESH FOTMOB CATEGORY ITEMS (<=72H) ===")
            news, health = fetch_configured_news(runtime)
            print(json.dumps({
                "health": {
                    "feeds_total": health.get("feeds_total"),
                    "feeds_failed": health.get("feeds_failed"),
                    "fotmob_items": health.get("fotmob_items"),
                    "fotmob_items_within_48h": health.get("fotmob_items_within_48h"),
                }
            }, sort_keys=True))
            for item in news:
                feed_id = str(item.get("feed_id") or "")
                if not (
                    feed_id.startswith("google.fotmob.")
                    or feed_id.startswith("fotmob.")
                    or item.get("source_id") == "media.fotmob"
                ):
                    continue
                age = _age_hours(item.get("created_at"))
                if age is None or age > 72:
                    continue
                try:
                    document = runtime.documents.from_item(bot._v2_document_item(item))
                    classified = runtime.extractor.classifier.classify(document, None)
                    source_verified = bool(document.source.verified)
                    resolved_source = document.source.profile_id
                    event = classified.event_type.value
                    category = classified.article_category.value
                    status = classified.status.value
                    certainty = classified.event_certainty
                except Exception as exc:  # diagnostic only
                    source_verified = False
                    resolved_source = f"ERROR:{type(exc).__name__}"
                    event = category = status = "ERROR"
                    certainty = 0.0
                row = {
                    "feed_id": feed_id,
                    "item_id": item.get("id"),
                    "already_in_posted_ids": item.get("id") in posted_ids,
                    "age_hours": age,
                    "title": item.get("title"),
                    "publisher_name": item.get("publisher_name"),
                    "publisher_url": item.get("publisher_url"),
                    "ingested_source_id": item.get("source_id"),
                    "resolved_source_id": resolved_source,
                    "source_verified": source_verified,
                    "event": event,
                    "article_category": category,
                    "status": status,
                    "event_certainty": certainty,
                }
                print(json.dumps(row, ensure_ascii=False, sort_keys=True))
        finally:
            runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
