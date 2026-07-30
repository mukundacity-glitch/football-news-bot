"""
FPL VORTEX — Rejection Audit Log.

Durable, structured record of every story the pipeline refused to publish.
Required so a rejection can be debugged later from evidence, not memory or
guesswork: "why did we skip this?" and "did we skip something real?" must
both be answerable from disk, weeks after the run that made the call.

Every entry captures:
  - the ORIGINAL source text and account(s) — never paraphrased or summarised
  - every field the extractor produced (player/clubs/event/fee/stage/mode/...)
  - the entity classification (type + reason) for the extracted subject —
    so "why was this treated as a COMPETITION/COUNTRY/CLUB and not a PLAYER"
    is answerable without re-running the classifier by hand
  - the exact gate and reason string that rejected the story
  - the confidence score/breakdown, when one was computed at that gate

Two files, both best-effort (a logging failure must never block or crash the
pipeline it is auditing):
  - queue/debug/rejections.jsonl      append-only, full history, capped
  - queue/debug/rejected_latest.json  rolling snapshot of the most recent
                                       rejections, for quick inspection
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from src.entity_guard import classify_entity_detailed

_DEBUG_DIR = Path("queue/debug")
_JSONL_PATH = _DEBUG_DIR / "rejections.jsonl"
_LATEST_PATH = _DEBUG_DIR / "rejected_latest.json"
_MAX_JSONL_LINES = 5000
_MAX_LATEST = 300


def _extracted_fields(story: dict) -> dict:
    return {
        "player": story.get("player"),
        "event": story.get("event"),
        "from_club": story.get("from_club") or story.get("from_key"),
        "to_club": story.get("to_club") or story.get("to_key"),
        "fee": story.get("fee"),
        "contract": story.get("contract"),
        "stage": story.get("stage"),
        "mode": story.get("mode"),
        "collapsed": bool(story.get("collapsed")),
    }


def _read_latest() -> list:
    try:
        return json.loads(_LATEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _trim_jsonl() -> None:
    try:
        lines = _JSONL_PATH.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_JSONL_LINES:
            _JSONL_PATH.write_text(
                "\n".join(lines[-_MAX_JSONL_LINES:]) + "\n", encoding="utf-8")
    except FileNotFoundError:
        pass


def log_rejection(gate: str, story: dict, reason: str, *, sources=None,
                   confidence: dict = None, fpl_data=None) -> None:
    """Append one rejection record.

    gate: the stage that rejected the story, e.g. 'safety_gate',
          'validate_story', 'confidence', 'contradiction', 'verify_card_data'.
    reason: the exact machine-readable rejection reason string from that gate.
    confidence: the dict returned by confidence.evaluate()/score_confidence(),
          if one was computed before this rejection.

    Never raises — a logging failure must not block the pipeline it audits.
    """
    try:
        sources = list(sources or story.get("sources") or [])
        text = (story.get("raw_text") or story.get("text")
                or story.get("body") or "")
        player = story.get("player") or ""
        etype, ereason = classify_entity_detailed(player, text, fpl_data)

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "gate": gate,
            "reason": reason,
            "sources": sources,
            "original_text": text,
            "extracted": _extracted_fields(story),
            "entity": {"type": etype, "reason": ereason},
        }
        if confidence is not None:
            entry["confidence"] = {
                "score": confidence.get("score"),
                "decision": confidence.get("decision"),
                "breakdown": confidence.get("breakdown"),
                "signals": confidence.get("signals"),
            }

        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)

        with open(_JSONL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _trim_jsonl()

        latest = _read_latest()
        latest.append(entry)
        latest = latest[-_MAX_LATEST:]
        with open(_LATEST_PATH, "w", encoding="utf-8") as f:
            json.dump(latest, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[REJECTION-LOG] failed to record rejection (non-fatal): {e}")
