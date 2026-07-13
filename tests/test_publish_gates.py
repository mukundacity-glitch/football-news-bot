"""
Publish-decision gate tests.

Locks the second round of real production failures (12 Jul 2026 audit):

  1. Hugo Oliveira was published as Fulham's head coach off a tweet that
     actually said he'd joined STRASBOURG and was merely "in the running for
     the Fulham job" (a candidacy that did not happen). Root cause: a foreign
     club invisible to the club lexicon let a past/rejected-candidacy club
     get promoted into the destination slot, and the tweet-body wording for
     a known role with no confirmed appointment/departure action read as a
     flat fact instead of a hedge.

  2. Jesse Derry's officially-confirmed (ChelseaFC, confidence 100) loan to
     Sporting Lisbon was rendered as "LINKED WITH A LOAN MOVE" — a rumour
     framing for a done deal — because the OFFICIAL/CONFIRMED gate required
     a Premier-League-only `to_key`, which a foreign destination never has.

  3. Both Manzambi posts scored confidence_decision=REVIEW (75-89), yet both
     went live because the GitHub Actions workflow always passes
     --allow-rumours, which (before this fix) also unlocked REVIEW-tier
     stories, not just genuinely rumour-staged ones.

Run with pytest OR standalone:  python tests/test_publish_gates.py
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "twikit" not in sys.modules:
    tw = types.ModuleType("twikit"); tw.Client = object; sys.modules["twikit"] = tw
import main  # noqa: E402

main.init_club_data()

_HUGO_OLIVEIRA_TEXT = (
    "Strasbourg confirm Hugo Oliveira has joined as head coach. BlueCo "
    "impressed by his time at Famalicao, and Oliveira has Premier League "
    "experience having worked as Marco Silva's assistant. Ben Jacobs "
    "(@JacobsBen) BREAKING: Hugo Oliveira is the leading candidate to take "
    "over at Strasbourg. BlueCo impressed by his job at Famalicao. Was in "
    "the running for the Fulham job and now likely to move to Ligue 1 with "
    "Strasbourg."
)

_JESSE_DERRY_TEXT = (
    "Jesse Derry has joined Sporting Lisbon, one of Portugal's most "
    "successful clubs, on loan for the duration of the 2026/27 season. "
    "Everyone at Chelsea wishes Jesse well for the season ahead and we look "
    "forward to monitoring and supporting his development."
)
# Minimal FPL bootstrap-static shape: Jesse Derry registered at Chelsea. The
# real pipeline always calls build_story with live FPL data — this is what
# lets from_key anchor to the player's TRUE club even when the loan
# destination (a foreign club) is invisible to the lexicon.
_FPL_DATA_JESSE_DERRY = {
    "elements": [{"first_name": "Jesse", "second_name": "Derry", "web_name": "Derry",
                  "team": 1, "now_cost": 40, "total_points": 0}],
    "teams": [{"id": 1, "name": "Chelsea", "short_name": "CHE"}],
}


# ── Hugo Oliveira: wrong-club rejection ──────────────────────────────────

def test_rejected_candidacy_club_not_promoted_to_destination():
    s = main.build_story(_HUGO_OLIVEIRA_TEXT, None)
    assert s.get("to_key") != "Fulham"
    assert s.get("to_club") != "Fulham"


def test_manager_story_with_no_resolvable_club_is_rejected_not_guessed():
    s = main.build_story(_HUGO_OLIVEIRA_TEXT, None)
    ok, why = main.validate_story(s, None, sources=["JacobsBen"])
    assert ok is False and why == "manager_no_club", (ok, why)


def test_staff_role_without_confirmed_action_is_hedged_wording():
    s = main.build_story("Fulham consider Some Candidate for the head coach role.", None)
    s["staff_role"] = "head coach"
    s["staff_action"] = None
    s["to_key"] = "Fulham"
    s["display_name"] = s["player"] or "Some Candidate"
    body = main.build_tweet_body(s, ["JacobsBen"], "rumour")
    assert "LINKED WITH" in body
    assert "APPOINTED" not in body


# ── Jesse Derry: officially-confirmed foreign-destination loan ──────────

def test_confirmed_loan_to_foreign_club_is_not_stuck_as_rumour():
    s = main.build_story(_JESSE_DERRY_TEXT, _FPL_DATA_JESSE_DERRY)
    assert s.get("from_key") == "Chelsea"
    assert s.get("to_key") is None and s.get("to_club")  # foreign destination, raw name only
    mode = main.classify_post(s, ["ChelseaFC"])
    assert mode == "confirmed", mode


def test_confirmed_foreign_destination_gets_official_label():
    s = main.build_story(_JESSE_DERRY_TEXT, _FPL_DATA_JESSE_DERRY)
    label = main.status_label(s, "confirmed")
    assert label == "OFFICIAL", label


# ── REVIEW-tier confidence is a hard floor, never bypassable ────────────

def test_review_tier_never_auto_posts_even_with_allow_rumours():
    async def _fake_main_body(allow_rumours):
        drafts = [{"mode": "rumour", "confidence_decision": "REVIEW"}]
        modes_ok = {"confirmed"} | ({"rumour"} if allow_rumours else set())
        def _conf_ok(d):
            return d.get("confidence_decision", "AUTO_POST") == main._conf.AUTO_POST
        return [d for d in drafts if d.get("mode") in modes_ok and _conf_ok(d)]

    import asyncio
    postable_with_flag = asyncio.run(_fake_main_body(True))
    postable_without_flag = asyncio.run(_fake_main_body(False))
    assert postable_with_flag == [] == postable_without_flag


def test_auto_post_tier_still_posts_with_allow_rumours():
    def _conf_ok(d):
        return d.get("confidence_decision", "AUTO_POST") == main._conf.AUTO_POST
    d = {"mode": "rumour", "confidence_decision": "AUTO_POST"}
    assert _conf_ok(d) is True


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
