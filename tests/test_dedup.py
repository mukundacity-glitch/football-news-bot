"""
Direction-agnostic dedup tests.

Locks the Johan Manzambi duplicate-posting incident (12 Jul 2026): two
different sources reported the same Freiburg -> Newcastle transfer, but a
misparse had one report's from/to reversed relative to the other. Because the
old story key was anchored on `to_key` alone, the two (contradictory!)
reports minted two different story keys and both got posted. The fix keys
transfer/loan stories on the UNORDERED club-pair, so a reversed report of the
same move is recognised as the same story instead of a new one.

These tests build `data` state entirely in memory — they must NEVER call
load_data()/save_data()/record_posted() against the real posted_news.json.

Run with pytest OR standalone:  python tests/test_dedup.py
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "twikit" not in sys.modules:
    tw = types.ModuleType("twikit"); tw.Client = object; sys.modules["twikit"] = tw
import main  # noqa: E402

main.init_club_data()

_RUMOUR_TEXT = (
    "Understand Aston Villa are among clubs attentive to Johan Manzambi "
    "situation with Newcastle. Newcastle agreed terms with Freiburg but "
    "waiting on player's green light to proceed. AVFC interested and "
    "attentive; it's up to the player."
)
# Same underlying move, direction reversed relative to how a naive positional
# parse would have read it: this text leads with "Newcastle waiting on ...
# a move to St James' Park" then mentions Aston Villa purely as a hijack risk.
_CONFIRMED_TEXT = (
    "Newcastle waiting on Johan Manzambi to approve a move to St James' Park. "
    "Deal ready to formalise, but caution until it's signed. Aston Villa also "
    "interested as reported last week. Newcastle are wary that Aston Villa "
    "may make a late move for Johan Manzambi. The Magpies have agreed a "
    "€60million fee with Freiburg for the Swiss midfielder."
)


def _fresh_data():
    return {"daily": {"date": "", "count": 0, "limit": 30}, "stories": {},
            "posted_ids": [], "pending": {}, "extracted": {},
            "posted_hashes": [], "posted_headlines": []}


def test_flipped_direction_reports_share_one_story_key():
    s1 = main.build_story(_RUMOUR_TEXT, None)
    s2 = main.build_story(_CONFIRMED_TEXT, None)
    key1 = main.reconcile_key(s1["player"], main.story_anchor(s1), s1["event"], {}, {}, {})
    key2 = main.reconcile_key(s2["player"], main.story_anchor(s2), s2["event"], {}, {}, {})
    assert key1 == key2, (key1, key2)


def test_flipped_direction_reports_share_one_content_hash():
    s1 = main.build_story(_RUMOUR_TEXT, None)
    s2 = main.build_story(_CONFIRMED_TEXT, None)
    assert main.content_hash(s1) == main.content_hash(s2)


def test_second_flipped_report_blocked_as_duplicate_after_first_posts():
    data = _fresh_data()
    s1 = main.build_story(_RUMOUR_TEXT, None)
    key1 = main.reconcile_key(s1["player"], main.story_anchor(s1), s1["event"],
                              {}, data["stories"], data["pending"])
    ok1, _ = main.should_post(data, key1, s1["stage"], s1["collapsed"])
    assert ok1 is True
    data["stories"][key1] = {
        "stage": s1["stage"], "player": s1["player"], "to_key": s1.get("to_key"),
        "event": s1["event"], "status": "active", "sources": ["FabrizioRomano"],
    }
    main.record_content_dedup({**s1, "headline": s1.get("headline")}, data)

    s2 = main.build_story(_CONFIRMED_TEXT, None)
    key2 = main.reconcile_key(s2["player"], main.story_anchor(s2), s2["event"],
                              {}, data["stories"], data["pending"])
    assert key2 == key1
    ok2, reason2 = main.should_post(data, key2, s2["stage"], s2["collapsed"])
    dup2, dreason2 = main.is_duplicate_content(s2, data)
    assert not (ok2 and not dup2), (
        f"second, direction-reversed report of the same move was NOT blocked "
        f"(should_post={ok2}/{reason2}, duplicate={dup2}/{dreason2})")


def test_genuinely_conflicting_clubs_flagged_as_contradiction_not_corroboration():
    # Two sources naming DIFFERENT destination clubs for the same player is a
    # real disagreement about the facts (not a direction flip about the same
    # pair) — must be held for review, never silently merged as if the
    # sources agree.
    s1 = main.build_story("Brighton sign Pascal Struijk from Leeds United.", None)
    s2 = main.build_story("Arsenal sign Pascal Struijk from Leeds United.", None)
    key1 = main.reconcile_key(s1["player"], main.story_anchor(s1), s1["event"], {}, {}, {})
    key2 = main.reconcile_key(s2["player"], main.story_anchor(s2), s2["event"], {}, {}, {})
    # These two do NOT share a key today (different club pairs) — the
    # contradiction path only triggers when they land under the SAME key.
    # Simulate that directly via the merge predicate used in scrape().
    new_to = main._norm_text(s2.get("to_key") or s2.get("to_club") or "")
    ex_to = main._norm_text(s1.get("to_key") or s1.get("to_club") or "")
    new_from = main._norm_text(s2.get("from_key") or s2.get("from_club") or "")
    ex_from = main._norm_text(s1.get("from_key") or s1.get("from_club") or "")
    contradicts = (
        bool(new_to and ex_to and new_to != ex_to and new_to != ex_from) or
        bool(new_from and ex_from and new_from != ex_from and new_from != ex_to)
    )
    assert contradicts is True


def test_different_players_keep_distinct_keys():
    s1 = main.build_story("Brighton sign Pascal Struijk from Leeds United.", None)
    s2 = main.build_story("Arsenal complete signing of Michael Svoboda from Rapid Vienna.", None)
    key1 = main.reconcile_key(s1["player"], main.story_anchor(s1), s1["event"], {}, {}, {})
    key2 = main.reconcile_key(s2["player"], main.story_anchor(s2), s2["event"], {}, {}, {})
    assert key1 != key2


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
