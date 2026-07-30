"""
Transfer-accuracy improvement pass (30 Jul 2026): broaden confirmed-transfer
recall, add a settle-time gate before non-elite "confirmed" posts go live,
and require a source + link on every transfer post — without touching
injury classification/wording anywhere (verified explicitly below).

Run with pytest OR standalone: python tests/test_transfer_accuracy_improvements.py
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


# ── Broadened confirmed-transfer recall (TRANSFER_CONFIRM_CUES) ─────────

def test_new_confirmation_wordings_classify_as_confirmed():
    cases = [
        "Chelsea announces the signing of Cole Palmer from Manchester City.",
        "Arsenal completes the signing of Viktor Gyokeres from Sporting.",
        "Newcastle unveiling their new signing Anthony Elanga today.",
        "It has been announced that West Ham have signed Jarrod Bowen.",
    ]
    for text in cases:
        s = main.build_story(text, None)
        mode = main.classify_post(s, ["ChelseaFC"])
        assert mode == "confirmed", (text, mode)


def test_deal_type_and_agreement_wording_not_added_as_official():
    # "permanent transfer" / "free transfer" (deal type) and "agreement
    # reached" (this codebase's distinct AGREED tier) were deliberately left
    # out of the confirmation wordings — verify they don't silently leak in.
    from src.constants import STRONG_OFFICIAL_CUES, TRANSFER_CONFIRM_CUES
    for phrase in ("permanent transfer", "free transfer", "agreement reached"):
        assert phrase not in STRONG_OFFICIAL_CUES
        assert phrase not in TRANSFER_CONFIRM_CUES


def test_cues_removed_after_failing_historical_backtest_stay_removed():
    # "finalised"/"finalized" and "contract until" were tried and dropped
    # after backtesting against real historical queue/ data surfaced actual
    # false positives (future-tense "to be finalised", an ancillary fee
    # being finalised rather than the deal itself, and a player's contract
    # at his CURRENT club being cited, not evidence of a new one). Lock that
    # decision in so a future edit can't silently re-add them.
    from src.constants import TRANSFER_CONFIRM_CUES
    for phrase in ("finalised", "finalized", "contract until"):
        assert phrase not in TRANSFER_CONFIRM_CUES

    # The exact real (anonymized-none, verbatim) historical false-positive
    # cases that justified removing them — must classify as NOT settled by
    # strong wording alone.
    from src.constants import STRONG_OFFICIAL_CUES
    import re as _re
    def _has_word(words, text):
        return any(_re.search(r'(?<![a-z])' + _re.escape(w) + r'(?![a-z])', text) for w in words)

    doku_text = ("breaking: manchester city agree on new deal for jeremy doku. "
                 "verbal agreement in place with details to be finalised soon, "
                 "five year deal and salary increased for belgian winger.")
    bergvall_text = ("tottenham reject offer from newcastle for lucas bergvall. "
                      "bergvall is under contract until june 2031 but has informed "
                      "the club of his preference to seek an opportunity elsewhere.")
    for text in (doku_text, bergvall_text):
        assert not _has_word(STRONG_OFFICIAL_CUES + TRANSFER_CONFIRM_CUES, text), text


# ── Injury pipeline is untouched by the recall broadening ───────────────

def test_new_confirmation_wordings_never_change_injury_stage_or_event():
    # These sentences pair a genuine, fresh, ONGOING injury with one of the
    # new confirmation words — before this fix, a shared stage-cue list
    # bumped `stage` to 4 (="FIT AGAIN" in _avail_text), which is wrong for
    # a player who is still out. TRANSFER_CONFIRM_CUES is scoped so this can
    # never happen; STRONG_OFFICIAL_CUES (which injury's stage reads) is
    # completely unchanged from before this pass.
    cases = [
        "Arsenal announced Saka is out for six weeks with a hamstring problem.",
        "Saka completes his recovery from injury and returns to training this week.",
        "Saka's contract until 2027 is unaffected by his hamstring injury, "
        "which will keep him out for six weeks.",
    ]
    for text in cases:
        s = main.build_story(text, None)
        assert s["event"] == "injury", (text, s["event"])
        assert s["stage"] == 1, (text, s["stage"])  # NOT bumped to 4


def test_strong_official_cues_constant_unchanged():
    from src.constants import STRONG_OFFICIAL_CUES
    assert STRONG_OFFICIAL_CUES == [
        "here we go", "official", "confirmed", "completed", "done deal",
        "sealed", "unveiled", "joins", "joined", "signs", "signed", "medical",
    ]


# ── Settle-time gate ──────────────────────────────────────────────────────

def _story(sources, minutes_ago, event="transfer"):
    s = main.build_story("Chelsea complete the signing of a new midfielder from Brentford.", None)
    s["event"] = event
    s["sources"] = sources
    s["first_seen"] = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return s


def test_non_elite_source_must_settle_before_posting():
    assert main.settle_time_ok(_story(["transfermarkt"], 2)) is False
    assert main.settle_time_ok(_story(["transfermarkt"], main.MIN_CONFIRM_SETTLE_MINUTES + 1)) is True


def test_official_and_elite_sources_bypass_settle_time():
    assert main.settle_time_ok(_story(["ChelseaFC"], 0)) is True       # tier 1 official
    assert main.settle_time_ok(_story(["FabrizioRomano"], 0)) is True  # tier 2 elite
    assert main.settle_time_ok(_story(["skysports"], 0)) is True       # tier 2 elite


def test_settle_time_gate_does_not_apply_to_non_transfer_events():
    for ev in ("injury", "suspension", "manager", "renewal"):
        assert main.settle_time_ok(_story(["transfermarkt"], 0, event=ev)) is True


def test_unparseable_first_seen_fails_closed_not_settled():
    s = _story(["transfermarkt"], 0)
    s["first_seen"] = "not-a-timestamp"
    assert main.settle_time_ok(s) is False


# ── Source + link on every transfer post ──────────────────────────────────

def test_confirmed_post_includes_source_and_link():
    s = main.build_story("Chelsea complete the signing of Joao Pedro from Brighton for £30 million. Here we go!", None)
    s["display_name"] = s["player"]
    s["source_url"] = "https://x.com/FabrizioRomano/status/123"
    mode = main.classify_post(s, ["FabrizioRomano"])
    body = main.build_tweet_body(s, ["FabrizioRomano"], mode)
    assert "SOURCE" in body
    assert "@FabrizioRomano" in body
    assert "https://x.com/FabrizioRomano/status/123" in body


def test_post_without_url_still_shows_source_handle_only():
    s = main.build_story("Chelsea complete the signing of Joao Pedro from Brighton.", None)
    s["display_name"] = s["player"]
    s["source_url"] = None
    body = main.build_tweet_body(s, ["transfermarkt"], "confirmed")
    assert "SOURCE — @transfermarkt" in body
    assert "http" not in body.split("SOURCE")[1].split("\n")[0]


def test_explicit_confirmed_rumour_status_label_present():
    s = main.build_story("Chelsea complete the signing of Joao Pedro from Brighton.", None)
    s["display_name"] = s["player"]
    confirmed_body = main.build_tweet_body(s, ["ChelseaFC"], "confirmed")
    rumour_body = main.build_tweet_body(s, ["transfermarkt"], "rumour")
    assert "✅ STATUS — CONFIRMED" in confirmed_body
    assert "🔄 STATUS — RUMOUR" in rumour_body


# ── Dead-code cleanup: removed names really are gone ──────────────────────

def test_dead_stub_functions_removed():
    for name in ("is_big_name_player", "is_big_club_name", "BIG_NAMES_NON_FPL", "COUNTRY_NAMES"):
        assert not hasattr(main, name), name


def test_fpl_team_key_no_longer_shadowed_locally():
    # main.py used to define its own fpl_team_key/is_big_player that silently
    # shadowed the canonical src.fpl_feed versions it also imported. Now it
    # uses the imported (canonical, accent-safe) implementation directly.
    from src import fpl_feed
    assert main.fpl_team_key is fpl_feed.fpl_team_key


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
