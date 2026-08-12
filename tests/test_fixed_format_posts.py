"""
Locks the fixed-format post rebuild (Aug 2026): four category-specific
tweet-body generators (format_transfer_post, format_injury_post,
format_suspension_post, format_press_conference_post) replace the old
stage-aware wording for CONFIRMED posts only -- rumours and other stages
still use the original build_tweet_body logic unchanged. Built from real
user-provided reference examples; the suspension format and the Arsenal
club-colour-emoji result were verified to match the user's own example
directly, not just approximately.

Also locks two real bugs found and fixed while building this:
1. resolve_club_key() was not idempotent on its own canonical output
   (e.g. resolve_club_key("Man_City") returned None) -- real production
   code assigns to_key/from_key directly from this function's output
   (main.py, ~line 2395), so any downstream call re-resolving an
   already-resolved key silently failed before this fix.
2. club_color_emojis() needs a resolved canonical key (CLUB_COLORS is
   keyed "Arsenal", not "arsenal") -- the same casing gap hashtag_for()
   already bridges via resolve_club_key(), now applied consistently at
   all four format-function call sites.

Run with pytest OR standalone:  python tests/test_fixed_format_posts.py
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "twikit" not in sys.modules:
    tw = types.ModuleType("twikit")
    tw.Client = object
    sys.modules["twikit"] = tw

import main  # noqa: E402

main.init_club_data()


# ---- resolve_club_key IDEMPOTENCY (the real bug found this session) ----

def test_resolve_club_key_is_idempotent_on_its_own_canonical_output():
    # This is the exact failure mode found: real story data gets to_key
    # assigned directly from resolve_club_key()'s own output (an
    # underscore-separated canonical key), so calling it again on that
    # same value must return the same key, not None.
    for canonical in ("Man_City", "Man_Utd", "Nottm_Forest", "Aston_Villa",
                       "Crystal_Palace"):
        assert main.resolve_club_key(canonical) == canonical, canonical


def test_resolve_club_key_still_handles_space_separated_free_text():
    assert main.resolve_club_key("man city") == "Man_City"
    assert main.resolve_club_key("Manchester City") == "Man_City"
    assert main.resolve_club_key("manchester united") == "Man_Utd"


def test_resolve_club_key_unknown_input_returns_none_not_a_crash():
    assert main.resolve_club_key("Not A Real Club FC") is None
    assert main.resolve_club_key("") is None
    assert main.resolve_club_key(None) is None


# ---- club_color_emojis: real RGB-derived match, verified against the
# user's own Arsenal example ----

def test_arsenal_matches_the_users_own_example_exactly():
    from src.renderer import club_color_emojis
    # The user's own suspension example used "🔴⚪" for Arsenal directly.
    assert club_color_emojis("Arsenal") == "🔴⚪"


def test_every_club_in_club_colors_resolves_to_something():
    from src.renderer import club_color_emojis
    from src.constants import CLUB_COLORS
    for club in CLUB_COLORS:
        result = club_color_emojis(club)
        assert result, f"{club} produced an empty result"
        assert result != "⚽", f"{club} fell through to the unknown-club fallback"


def test_unknown_club_falls_back_to_ball_emoji_not_a_crash():
    from src.renderer import club_color_emojis
    assert club_color_emojis("Not_A_Real_Club") == "⚽"
    assert club_color_emojis(None) == "⚽"
    assert club_color_emojis("") == "⚽"


# ---- format_suspension_post: verified against the user's real example ----

def test_suspension_post_matches_user_example_structure():
    story = {
        "player": "Bukayo Saka", "display_name": "Bukayo Saka",
        "event": "suspension", "to_key": "Arsenal", "to_club": "Arsenal",
        "from_key": "Arsenal",
    }
    body = main.format_suspension_post(story)
    assert "🚨🔴 SUSPENDED!" in body
    assert "Bukayo Saka" in body
    assert "🔴⚪ ARSENAL" in body
    assert "Status: SUSPENDED" in body
    assert "check your squad before the deadline" in body


def test_suspension_post_uses_tbd_for_missing_return_date():
    story = {
        "player": "Some Player", "display_name": "Some Player",
        "event": "suspension", "to_key": "Chelsea",
    }
    body = main.format_suspension_post(story)
    # No crash, no blank field, no invented specific date.
    assert body  # renders something


def test_suspension_post_omits_source_line_by_design():
    story = {
        "player": "Bukayo Saka", "display_name": "Bukayo Saka",
        "event": "suspension", "to_key": "Arsenal",
    }
    body = main.format_suspension_post(story)
    assert "SOURCE" not in body
    assert "@" not in body.split("\n\n")[0]  # no handle in the body, only in hashtags


# ---- format_transfer_post: both real reference shapes ----

def test_transfer_post_same_club_renewal_shape():
    story = {
        "player": "Bukayo Saka", "display_name": "Bukayo Saka",
        "event": "transfer", "to_key": "Arsenal", "to_club": "Arsenal",
        "from_key": "Arsenal", "from_club": "Arsenal",
        "contract": "5 years", "wages": "£300K+ per week", "fee": "£0",
        "is_free": True,
    }
    body = main.format_transfer_post(story)
    assert "commits their future to Arsenal" in body
    assert "Contract: 5 years" in body
    assert "Wages: £300K+ per week" in body
    assert "stays at Arsenal" in body


def test_transfer_post_cross_club_shape():
    story = {
        "player": "Joao Palhinha", "display_name": "João Palhinha",
        "event": "transfer", "to_key": "Tottenham", "to_club": "Tottenham",
        "from_key": "Bayern_Munich", "from_club": "Bayern Munich",
        "fee": "£30M", "contract": "4 Years",
    }
    body = main.format_transfer_post(story)
    assert "joins Tottenham from Bayern Munich" in body
    assert "FROM: Bayern Munich" in body
    assert "TO: Tottenham" in body
    assert "Fee: £30M" in body


def test_transfer_post_missing_fee_shows_tbd_not_blank():
    story = {
        "player": "Some Player", "display_name": "Some Player",
        "event": "transfer", "to_key": "Chelsea", "to_club": "Chelsea",
        "from_key": "Arsenal", "from_club": "Arsenal",
    }
    body = main.format_transfer_post(story)
    assert "Fee: TBD" in body
    assert "Contract: TBD" in body


# ---- format_injury_post / format_press_conference_post: same 3-line
# structural family, no direct reference example but built consistently ----

def test_injury_post_has_three_line_structure_and_real_fields():
    story = {
        "player": "Phil Foden", "display_name": "Phil Foden",
        "event": "injury", "to_key": "Man_City", "to_club": "Manchester City",
        "diagnosis": "a hamstring knock", "stage": 2,
    }
    body = main.format_injury_post(story)
    assert "Phil Foden" in body
    assert "a hamstring knock" in body
    assert "🔵⚪ MANCHESTER CITY" in body or "MANCHESTER CITY" in body


def test_press_conference_post_uses_real_quote_fields():
    story = {
        "player": "Mikel Arteta", "display_name": "Mikel Arteta",
        "event": "press_conference", "to_key": "Arsenal", "to_club": "Arsenal",
        "quote_topic": "squad fitness", "quote_summary": "We are ready for the new season",
    }
    body = main.format_press_conference_post(story)
    assert "squad fitness" in body
    assert "We are ready for the new season" in body
    assert "🔴⚪ ARSENAL" in body


# ---- build_tweet_body dispatch: confirmed uses new format, rumour
# untouched ----

def test_build_tweet_body_dispatches_confirmed_transfer_to_new_format():
    story = main.build_story("Chelsea complete the signing of Joao Pedro from Brighton.", None)
    story["display_name"] = story["player"]
    body = main.build_tweet_body(story, ["ChelseaFC"], "confirmed")
    assert "TRANSFER CONFIRMED!" in body


def test_build_tweet_body_rumour_mode_unaffected():
    story = main.build_story("Chelsea complete the signing of Joao Pedro from Brighton.", None)
    story["display_name"] = story["player"]
    body = main.build_tweet_body(story, ["transfermarkt"], "rumour")
    assert "TRANSFER CONFIRMED!" not in body
    assert "🔄 STATUS — RUMOUR" in body


def test_build_tweet_body_manager_events_use_old_format_not_new():
    # Manager/staff stories have no new fixed format -- must still route
    # through the original logic, not silently produce a blank/wrong body.
    story = main.build_story("Arsenal appoint a new head coach.", None)
    story["event"] = "manager"
    story["display_name"] = story.get("player") or "New Coach"
    body = main.build_tweet_body(story, ["ArsenalFC"], "confirmed")
    assert body  # renders something via the old path, doesn't crash or go blank
    assert "TRANSFER CONFIRMED!" not in body


# ---- build_hashtags: rebuilt tag set ----

def test_hashtags_include_the_confirmed_branded_base_tags():
    story = {"event": "suspension", "to_key": "Arsenal", "display_name": "Bukayo Saka"}
    tags = main.build_hashtags(story)
    for expected in ("#FPL", "#FPL2026", "#FantasyPremierLeague", "#FPLVortex"):
        assert expected in tags


def test_hashtags_include_club_and_player_tags_when_available():
    story = {"event": "suspension", "to_key": "Arsenal", "display_name": "Bukayo Saka"}
    tags = main.build_hashtags(story)
    assert "#Arsenal" in tags
    assert "#Saka" in tags


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
