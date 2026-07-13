"""
Parser extraction-accuracy tests.

Locks two real production failures behind the Johan Manzambi false-news
incident (12 Jul 2026): a rival club merely "interested" in a player got
promoted into the from/to slots by pure word-order, and an incidental
"...remains focused on the World Cup despite being currently injured" aside
got mis-classified as the whole story's event instead of the actual
transfer news leading the tweet.

Run with pytest OR standalone:  python tests/test_parser.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.parser import extract_story_fallback


# ── interest-only club exclusion ────────────────────────────────────────
# A club mentioned only as "also interested" / "in the race" / "hijack"
# chatter is not a party to the move and must never be promoted into the
# from/to slots just because of where it sits in the text.

def test_rival_interest_club_excluded_from_direction():
    text = ("Understand Aston Villa are among clubs attentive to Johan Manzambi "
            "situation with Newcastle. Newcastle agreed terms with Freiburg but "
            "waiting on player's green light to proceed. AVFC interested and "
            "attentive; it's up to the player.")
    s = extract_story_fallback(text, None)
    assert s["to_key"] != "Aston_Villa", s["to_key"]
    assert s["from_key"] != "Aston_Villa", s["from_key"]


def test_hijack_language_excludes_rival_club():
    text = ("Newcastle are wary that Aston Villa may make a late move for Johan "
            "Manzambi, talkSPORT understands. The Magpies have agreed a "
            "€60million fee with Freiburg for the Swiss midfielder.")
    s = extract_story_fallback(text, None)
    assert s["to_key"] != "Aston_Villa"
    assert s["from_key"] != "Aston_Villa"


def test_genuine_second_club_still_captured_when_not_interest_only():
    # Sanity check: the exclusion is scoped to interest-only language, not
    # "any second club mentioned" — a real move between two known PL clubs
    # must still resolve normally.
    s = extract_story_fallback("Brighton sign Pascal Struijk from Leeds United.", None)
    assert {s["from_key"], s["to_key"]} == {"Leeds", "Brighton"}


# ── earliest-cue event classification ────────────────────────────────────
# The event is whichever cue occurs EARLIEST in the text, not whichever
# category wins a fixed priority order — a trailing "currently injured" aside
# must not outrank a leading transfer-agreement headline.

def test_trailing_injury_aside_does_not_override_leading_transfer_news():
    text = ("EXCLUSIVE | Newcastle and SC Freiburg have reached a full agreement "
            "over the transfer of Johan Manzambi worth €60m. All agreed between "
            "the clubs. NUFC are now finalising the final details of the "
            "agreement with Manzambi. Aston Villa were also in the race "
            "following Amadou Onana's ACL injury, but Newcastle are the clear "
            "favourites. Manzambi, who is currently injured, remains fully "
            "focused on the World Cup.")
    s = extract_story_fallback(text, None)
    assert s["event"] == "transfer", s["event"]


def test_leading_injury_news_still_classified_as_injury():
    text = "Bukayo Saka ruled out for six weeks with a hamstring injury sustained in training."
    s = extract_story_fallback(text, None)
    assert s["event"] == "injury", s["event"]


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
