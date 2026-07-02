"""
Mandatory entity-safety test suite.

Covers every case the bot must never get wrong. Runs with pytest OR standalone:
    python tests/test_entity_guard.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.entity_guard import classify_entity, is_postable_player


# ── MUST REJECT ──────────────────────────────────────────────────────────

def test_case3_company_lilley_plummer_rejected():
    # CASE 3: insurance brokerage, not a footballer.
    for name in ("Lilley Plummer", "Lilley Plummer Risks", "LILLEY PLUMMER RISKS"):
        cat, reason = classify_entity(name, "West Ham announce new partnership with " + name)
        assert cat == "COMPANY", (name, cat)
        ok, r = is_postable_player(name, "", "transfer")
        assert ok is False and r == "company_entity", (name, ok, r)


def test_case4_journalist_plettenberg_rejected():
    # CASE 4: Sky Sport journalist, never a transfer subject.
    ok, r = is_postable_player("Florian Plettenberg", "Florian Plettenberg reports Brighton are in talks", "transfer")
    assert ok is False and r == "journalist_entity", (ok, r)
    # Protected journalists must all fail, in any event.
    for j in ("Fabrizio Romano", "David Ornstein", "Ben Jacobs", "James Pearce"):
        assert is_postable_player(j, "", "transfer")[0] is False, j


def test_case5_coach_pako_ayestaran_rejected_as_player():
    # CASE 5: assistant head coach — staff, not a player transfer.
    text = "Aston Villa assistant head coach Pako Ayestaran has departed the club."
    cat, reason = classify_entity("Pako Ayestaran", text)
    assert cat == "STAFF", (cat, reason)
    assert is_postable_player("Pako Ayestaran", text, "renewal")[0] is False
    assert is_postable_player("Pako Ayestaran", text, "transfer")[0] is False
    # A generic coach caught purely by role context (name not pre-listed):
    ctx = "Brighton first team coach John Smithson leaves for a new role."
    assert classify_entity("John Smithson", ctx)[0] == "STAFF"


def test_company_suffix_heuristic():
    # Sponsor/brand names caught by suffix even if not pre-listed.
    for name in ("Boyle Sports", "Northbridge Insurance", "Vertex Holdings"):
        assert classify_entity(name, "")[0] == "COMPANY", name


def test_stadiums_rejected():
    for s in ("Old Trafford", "Villa Park", "Emirates Stadium"):
        assert is_postable_player(s, "", "transfer")[0] is False, s


def test_club_as_player_rejected():
    # From the real run: foreign clubs misparsed as players.
    for name in ("Le Paris Saint", "Stade Rennais", "Paris Saint-Germain",
                 "Olympiacos", "Borussia Dortmund", "Sheffield Wednesday",
                 "Bolton Wanderers", "Real Madrid"):
        cat, reason = classify_entity(name, "")
        assert cat == "CLUB", (name, cat, reason)
        assert is_postable_player(name, "", "transfer")[0] is False, name


def test_players_with_clublike_tokens_still_pass():
    # Real players whose names share a single token with a club must NOT be caught.
    for name in ("Milan Djuric", "David Villa", "Roma Cadette", "Sergio Ramos",
                 "Michael Svoboda", "Costinha", "Zadok Yohanna", "Pascal Struijk",
                 "Sil Swinkels", "Luca Stephenson"):
        assert classify_entity(name, "")[0] == "PLAYER", name


# ── MUST ALLOW (real players, incl. non-FPL new/foreign signings) ────────

def test_cases_6_to_10_real_players_allowed():
    allowed = {
        "Michael Svoboda": "Brighton complete the signing of Michael Svoboda.",
        "Jeremy Sarmiento": "Jeremy Sarmiento joins Middlesbrough from Brighton.",
        "Pascal Struijk": "Pascal Struijk completes his move from Leeds to Brighton.",
        "Costinha": "Costinha signs for Brighton from Olympiacos.",
        "Zadok Yohanna": "Zadok Yohanna moves from AIK to Brighton.",
    }
    for name, text in allowed.items():
        cat, reason = classify_entity(name, text)
        assert cat == "PLAYER", (name, cat, reason)
        assert is_postable_player(name, text, "transfer")[0] is True, name


def test_direction_cases_are_still_real_players():
    # CASES 1 & 2 are DIRECTION errors, not entity errors: the subjects ARE real
    # players and must pass the entity gate. (Correcting the transfer DIRECTION is a
    # separate, not-yet-implemented phase — see notes in the PR.)
    for name, text in (
        ("Sil Swinkels", "Sil Swinkels joins Sheffield Wednesday from Aston Villa on a permanent deal."),
        ("Luca Stephenson", "Luca Stephenson completes permanent move from Liverpool to Bolton Wanderers."),
    ):
        assert classify_entity(name, text)[0] == "PLAYER", name
        assert is_postable_player(name, text, "transfer")[0] is True, name


def test_ordinary_fpl_player_allowed():
    for name in ("Bukayo Saka", "Declan Rice", "Reece James", "Erling Haaland"):
        assert is_postable_player(name, "", "transfer")[0] is True, name


# ── standalone runner ────────────────────────────────────────────────────
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
