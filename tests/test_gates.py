"""
Recency + subject-identity gate tests.

Locks two rules the bot must never break:
  1. Never publish news older than 3 days (fail-closed on unknown dates).
  2. Never publish a non-player (coach/staff/executive) as a player transfer;
     always determine whether the subject is a player, manager or staff.

Run with pytest OR standalone:  python tests/test_gates.py
"""

import os
import sys
import types
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "twikit" not in sys.modules:
    tw = types.ModuleType("twikit"); tw.Client = object; sys.modules["twikit"] = tw
import main  # noqa: E402

main.init_club_data()

_NOW = datetime.now(timezone.utc)
def _date(**delta):
    return (_NOW - timedelta(**delta)).strftime("%a, %d %b %Y %H:%M:%S +0000")


# ── RECENCY ──────────────────────────────────────────────────────────────

def test_tweet_too_old_thresholds():
    assert main.tweet_too_old(_date(days=5)) is True
    assert main.tweet_too_old(_date(hours=6)) is False
    assert main.tweet_too_old(_date(days=2, hours=23)) is False


def test_unknown_date_fails_closed():
    # Cannot verify recency => must be treated as too old (never posted).
    assert main.tweet_too_old(None) is True
    assert main.tweet_too_old("not a date") is True


def test_stale_story_rejected_in_validate():
    s = main.build_story("Brighton sign Pascal Struijk from Leeds United", None)
    s["created_at"] = _date(days=5)
    ok, why = main.validate_story(s, None, sources=["FabrizioRomano"])
    assert ok is False and why == "older_than_3d", why


def test_recent_story_passes_recency():
    s = main.build_story("Brighton sign Pascal Struijk from Leeds United", None)
    s["created_at"] = _date(hours=4)
    ok, why = main.validate_story(s, None, sources=["FabrizioRomano"])
    assert ok is True, why


# ── SUBJECT IDENTITY (player vs staff) ───────────────────────────────────

def test_non_player_no_origin_rejected_as_transfer():
    # A coach announced by a club, filed as a player transfer, with no origin
    # club and not in FPL -> must NOT publish as a player transfer.
    s = main.build_story("Pascal De Maesschalck confirmed transfer to Arsenal", None)
    s["created_at"] = _date(hours=2)
    ok, why = main.validate_story(s, None, sources=["Arsenal"])
    assert ok is False and why == "unconfirmed_player_identity", why


def test_coach_with_role_cue_routes_to_staff():
    # Same person WITH a role cue -> correctly identified as staff and postable.
    s = main.build_story("Arsenal appoint Pascal De Maesschalck as goalkeeping coach", None)
    assert s["event"] == "manager"
    assert s.get("staff_role") == "goalkeeping coach"
    s["created_at"] = _date(hours=2)
    ok, why = main.validate_story(s, None, sources=["Arsenal"])
    assert ok is True, why


def test_real_signing_with_origin_still_posts():
    # Foreign signing with an extracted origin -> genuine player move, allowed.
    s = main.build_story("Brighton complete the signing of Michael Svoboda from Rapid Vienna", None)
    s["created_at"] = _date(hours=2)
    ok, why = main.validate_story(s, None, sources=["OfficialBHAFC"])
    assert ok is True, why


# ── MANAGER WORDING (linked = rumour, not confirmed) ─────────────────────

def test_manager_linked_is_rumour_not_confirmed():
    s = main.build_story("Ange Postecoglou linked with the Tottenham job", None)
    s["event"] = "manager"; s["player"] = "Ange Postecoglou"; s["to_key"] = "Spurs"
    s["stage"] = 1
    mode = main.classify_post(s, ["FabrizioRomano"])   # elite source
    assert mode != "confirmed", mode   # a bare "linked" must never be CONFIRMED


def test_manager_appointment_can_confirm():
    s = main.build_story("Chelsea appoint Marcus Sorg as first team coach", None)
    s["to_key"] = "Chelsea"
    mode = main.classify_post(s, ["ChelseaFC"])   # official source
    assert mode == "confirmed", mode


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
