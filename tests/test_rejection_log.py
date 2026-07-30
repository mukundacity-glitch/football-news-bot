"""
Rejection audit log tests.

Verifies every rejection record captures what the spec requires for future
debugging: the original article text, the extracted fields, the entity
classification, the confidence breakdown (when available), and the exact
rejection reason — and that a logging failure can never raise.

Run with pytest OR standalone: python tests/test_rejection_log.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.rejection_log as rl


def _with_temp_debug_dir(fn):
    """Run fn() with the module's debug dir/paths redirected to a temp dir,
    then restore the originals — keeps this test from touching the real
    queue/debug/ directory."""
    orig_dir, orig_jsonl, orig_latest = rl._DEBUG_DIR, rl._JSONL_PATH, rl._LATEST_PATH
    with tempfile.TemporaryDirectory() as td:
        rl._DEBUG_DIR = Path(td)
        rl._JSONL_PATH = rl._DEBUG_DIR / "rejections.jsonl"
        rl._LATEST_PATH = rl._DEBUG_DIR / "rejected_latest.json"
        try:
            fn()
        finally:
            rl._DEBUG_DIR, rl._JSONL_PATH, rl._LATEST_PATH = orig_dir, orig_jsonl, orig_latest


def test_rejection_record_has_required_fields():
    def _run():
        story = {
            "player": "France World Cup", "event": "transfer",
            "from_key": "Crystal_Palace", "to_key": "Chelsea",
            "raw_text": "FRANCE WORLD CUP CONFIRMED TRANSFER", "fee": "£52M", "stage": 4,
        }
        conf = {"score": -25, "decision": "SKIP", "breakdown": {"entity_reject": -100},
                "signals": {"verified_player": False}}
        rl.log_rejection("validate_story", story, "missing_player",
                         sources=["ChelseaFC"], confidence=conf)

        lines = rl._JSONL_PATH.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])

        assert entry["gate"] == "validate_story"
        assert entry["reason"] == "missing_player"
        assert entry["sources"] == ["ChelseaFC"]
        assert entry["original_text"] == "FRANCE WORLD CUP CONFIRMED TRANSFER"
        assert entry["extracted"]["player"] == "France World Cup"
        assert entry["extracted"]["from_club"] == "Crystal_Palace"
        assert entry["extracted"]["to_club"] == "Chelsea"
        assert entry["extracted"]["fee"] == "£52M"
        assert entry["entity"]["type"] in ("COMPETITION", "COUNTRY")
        assert entry["confidence"]["decision"] == "SKIP"
        assert "timestamp" in entry

        latest = json.loads(rl._LATEST_PATH.read_text(encoding="utf-8"))
        assert len(latest) == 1
        assert latest[0]["reason"] == "missing_player"

    _with_temp_debug_dir(_run)


def test_multiple_rejections_append_not_overwrite():
    def _run():
        for i in range(3):
            rl.log_rejection("safety_gate", {"player": f"Junk {i}", "raw_text": "x"},
                             "not_pl_relevant", sources=["src"])
        lines = rl._JSONL_PATH.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        latest = json.loads(rl._LATEST_PATH.read_text(encoding="utf-8"))
        assert len(latest) == 3

    _with_temp_debug_dir(_run)


def test_logging_never_raises_on_bad_input():
    def _run():
        # Confidence dict missing keys, story missing everything — must not raise.
        rl.log_rejection("confidence", {}, "low_confidence_score_10", confidence={})
        rl.log_rejection("safety_gate", None or {}, "no_player")
    _with_temp_debug_dir(_run)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed.")
    sys.exit(1 if failed else 0)
