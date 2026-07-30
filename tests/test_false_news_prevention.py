"""
False-news prevention: country/competition entities must NEVER become a
transfer subject (30 Jul 2026 audit).

Real false post that reached publish:
  "TRANSFER - FRANCE WORLD CUP CONFIRMED PERMANENT TRANSFER FROM CRYSTAL
   PALACE TO CHELSEA. FEE — £52M. STAGE — IN PROGRESS"

"France World Cup" is not a footballer — it is a country name and a
tournament name glued together by the capitalized-word-sequence extractor,
which had no concept of COUNTRY or COMPETITION as entity types at all. The
fix is NOT a name-specific patch: entity_guard.py's taxonomy gained two new
hard-reject categories (COUNTRY, COMPETITION) backed by knowledge bases
(data/countries.json, data/competitions.json), wired in at THREE independent
layers so no single miss lets a non-person subject through:

  1. Extraction (src/parser.py `_is_bad_name`) — a country/competition
     candidate is rejected before it's ever assigned to `player`, so
     extraction tries the next capitalized run in the text instead.
  2. Entity classification (src/entity_guard.py `classify_entity_detailed`)
     — COUNTRY/COMPETITION are hard-reject types, checked before the
     PLAYER default, independent of source tier or wording.
  3. Confidence scoring (src/confidence.py `evaluate`) — the
     "elite_source_confirmed_language" override (which previously bypassed
     the additive score for any story with a trusted source + "confirmed"/
     "official" wording) now requires BOTH etype == PLAYER and a zero
     entity_penalty, so an entity-hard-rejected story can never be
     auto-posted purely on source tier + wording, regardless of how the
     reject happened.

A missing player name (because the only candidate was a country/tournament
fragment) is discarded outright — `validate_story` requires a player and
`classify_entity_detailed("")` is UNKNOWN (hard reject), never a silent
PLAYER default.

Run with pytest OR standalone: python tests/test_false_news_prevention.py
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "twikit" not in sys.modules:
    tw = types.ModuleType("twikit"); tw.Client = object; sys.modules["twikit"] = tw
import main  # noqa: E402

from src.entity_guard import classify_entity_detailed, is_postable_player
from src.confidence import evaluate as conf_evaluate

main.init_club_data()

_FALSE_POST_TEXT = (
    "TRANSFER - FRANCE WORLD CUP CONFIRMED PERMANENT TRANSFER FROM CRYSTAL "
    "PALACE TO CHELSEA. FEE — £52M. STAGE — IN PROGRESS"
)


# ── The exact reported false post, end to end ────────────────────────────

def test_france_world_cup_never_extracted_as_player():
    s = main.build_story(_FALSE_POST_TEXT, None)
    assert s.get("player") != "France World Cup"
    # No other candidate name exists in the text, so extraction must yield
    # nothing rather than guess — discarding is the correct outcome.
    assert s.get("player") is None


def test_france_world_cup_rejected_by_validate_story():
    s = main.build_story(_FALSE_POST_TEXT, None)
    ok, why = main.validate_story(s, None, sources=["ChelseaFC"])
    assert ok is False
    assert why == "missing_player"


def test_france_world_cup_never_auto_posts_via_confidence_override():
    # Even bypassing extraction (as if some other code path had set the
    # subject directly), the entity gate + hardened override must still
    # block it — source tier and "confirmed" wording must never override
    # an entity-hard-reject.
    story = {
        "player": "France World Cup", "event": "transfer",
        "from_key": "Crystal_Palace", "to_key": "Chelsea",
        "raw_text": _FALSE_POST_TEXT, "body": _FALSE_POST_TEXT,
        "headline": "France World Cup", "stage": 4,
    }
    result = conf_evaluate(story, player_verified=True, official_source=True,
                          n_sources=2)
    assert result["decision"] != "AUTO_POST", result
    assert result["entity_type"] in ("COMPETITION", "COUNTRY")


# ── Entity classification: COUNTRY / COMPETITION taxonomy ────────────────

def test_competition_names_classified_and_rejected():
    for name in ("France World Cup", "Premier League", "Champions League",
                 "Europa League", "Copa America", "AFCON", "FA Cup",
                 "England Euros", "Bundesliga"):
        etype, reason = classify_entity_detailed(name, "")
        assert etype == "COMPETITION", (name, etype, reason)
        ok, r = is_postable_player(name, "", "transfer")
        assert ok is False, name


def test_country_names_classified_and_rejected():
    for name in ("Brazil", "Argentina", "South Korea", "Ivory Coast",
                 "Saudi Arabia", "Nigeria"):
        etype, reason = classify_entity_detailed(name, "")
        assert etype == "COUNTRY", (name, etype, reason)
        ok, r = is_postable_player(name, "", "transfer")
        assert ok is False, name


def test_real_players_not_caught_by_country_or_competition_checks():
    # Sanity: the new hard-reject categories must not false-positive on real
    # players whose names merely share a token with a country/competition.
    for name in ("Bukayo Saka", "Declan Rice", "Erling Haaland",
                 "Cole Palmer", "Kylian Mbappe"):
        etype, reason = classify_entity_detailed(name, "")
        assert etype == "PLAYER", (name, etype, reason)


# ── Generalization: the same shape of bug, different country/competition ─

def test_various_country_plus_competition_fragments_all_discarded():
    cases = [
        "BREAKING: Brazil Copa America confirmed here we go to Manchester "
        "United from Liverpool. Fee 40M",
        "OFFICIAL: England Euros here we go, Arsenal complete signing from "
        "Chelsea",
        "Confirmed: Premier League here we go move from Everton to Fulham "
        "completed",
        "Champions League official confirmed transfer joins Newcastle from "
        "Southampton",
        "Ivory Coast AFCON here we go signs for West Ham from Wolves",
    ]
    for text in cases:
        s = main.build_story(text, None)
        ok, why = main.validate_story(s, None, sources=["SkySportsNews"])
        assert ok is False, (text, s.get("player"))
        assert why == "missing_player", (text, why)


# ── Missing-name fails closed, never defaults to PLAYER ──────────────────

def test_empty_player_name_is_unknown_not_player():
    etype, reason = classify_entity_detailed("", "")
    assert etype == "UNKNOWN", (etype, reason)
    ok, r = is_postable_player("", "", "transfer")
    assert ok is False


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
