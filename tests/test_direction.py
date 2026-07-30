"""
Transfer-direction resolver tests.

Locks the historical DIRECTION failures (Swinkels, Stephenson) and foreign/EFL
club resolution the base PL-only parser could not do.

Run with pytest OR standalone:  python tests/test_direction.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.direction import resolve


def test_swinkels_direction_not_inverted():
    frm, fk, to, tk = resolve("Sil Swinkels joins Sheffield Wednesday from Aston Villa.")
    assert frm == "Aston Villa" and fk == "Aston_Villa"
    assert to == "Sheffield Wednesday"


def test_stephenson_direction():
    frm, fk, to, tk = resolve(
        "Luca Stephenson completes permanent move from Liverpool to Bolton Wanderers.")
    assert frm == "Liverpool"
    assert to == "Bolton Wanderers"


def test_foreign_origin_captured_even_if_unknown_club():
    frm, fk, to, tk = resolve("Brighton complete the signing of Michael Svoboda from Rapid Vienna.")
    assert frm == "Rapid Vienna"          # raw origin captured
    assert to == "Brighton" and tk == "Brighton"


def test_subject_signs_from_efl():
    frm, fk, to, tk = resolve("Brighton sign Pascal Struijk from Leeds United.")
    assert to == "Brighton" and tk == "Brighton"
    assert fk == "Leeds"


def test_signs_for_pattern():
    frm, fk, to, tk = resolve("Costinha signs for Brighton from Olympiacos.")
    assert to == "Brighton"
    assert frm == "Olympiacos"


def test_no_direction_when_no_clubs():
    assert resolve("Some vague transfer chatter with no clubs") == (None, None, None, None)


def test_origin_not_a_date_word():
    # "from June" must NOT be captured as an origin club.
    frm, fk, to, tk = resolve("Player set to return from June after injury at Arsenal")
    assert frm is None


# ── "agreed ... with <club>" / joint-agreement origin grammar ──────────────
# Locks the Manzambi false-direction bug: neither tweet used a literal "from
# <club>", so the origin (a club outside our lexicon) was silently dropped and
# a THIRD, merely-interested club got promoted into the from/to pair instead.

def test_agreed_fee_with_captures_unknown_origin():
    frm, fk, to, tk = resolve(
        "Newcastle agreed terms with Freiburg but waiting on player's green light to proceed.")
    assert frm == "Freiburg"


def test_agreed_fee_with_amount_and_conversion_aside():
    frm, fk, to, tk = resolve(
        "The Magpies have agreed a €60million (£51.2million) fee with Freiburg for the midfielder.")
    assert frm == "Freiburg"


def test_joint_agreement_names_buyer_then_seller():
    frm, fk, to, tk = resolve(
        "Newcastle and SC Freiburg have reached a full agreement over the transfer of the player.")
    assert frm == "SC Freiburg"
    assert to == "Newcastle" and tk == "Newcastle"


# ── Raw destination fallback for a foreign club named only in shorthand ────
# Locks the John Stones false-Arsenal-transfer incident (29 Jul 2026): the
# real destination was Inter Milan, reported in the source text only as bare
# "Inter" (standard journalism shorthand). "Inter" isn't in our lexicon
# (only the full "Inter Milan" is), so the destination slot was silently left
# empty by grammar resolution and the parser's positional fallback promoted
# Arsenal — a club mentioned only as rival "interest" — into the to_key slot
# instead. Any future club named only by its common short form should hit
# this same raw fallback, not just "Inter".

def test_raw_destination_captures_unlisted_club_shorthand():
    frm, fk, to, tk = resolve(
        "John Stones has verbally agreed to join Inter on a free transfer, "
        "according to Sky in Italy.")
    assert to == "Inter" and tk is None


def test_raw_destination_not_confused_by_rival_interest_club():
    frm, fk, to, tk = resolve(
        "Stones verbally agrees Inter move amid Arsenal, Chelsea interest. "
        "John Stones has verbally agreed to join Inter on a free transfer, "
        "according to Sky in Italy.")
    assert to == "Inter"
    assert to != "Arsenal"


# ── Regressions found while auditing posted_news.json (29 Jul 2026) ───────
# Trusting direction.resolve()'s destination more (the fix above) surfaced
# two pre-existing weaknesses in the grammar heuristics themselves, which
# had been silently masked before because build_story rarely trusted this
# module's destination. Both are locked here so they can't resurface.

def test_subject_sign_ignores_possessive_mention():
    # "Newcastle agree a deal for Monaco's Bamba ... to sign a long-term
    # contract" — the nearest club before "sign" is "Monaco", but Monaco
    # only appears in a possessive ("Monaco's Bamba", i.e. whose player he
    # currently is), not as the club doing the signing. Newcastle, named via
    # "agree ... deal for", is the real destination.
    frm, fk, to, tk = resolve(
        "Newcastle agree a deal for Monaco's Bamba. Midfielder Aladji Bamba "
        "is due to arrive on Thursday for a medical and to sign a long-term "
        "contract.")
    assert to != "Monaco"


def test_bare_to_not_confused_by_result_phrase():
    # "Champions League final loss to Paris Saint-Germain" is a match result,
    # not a transfer destination — the bare "to <club>" heuristic must not
    # fire on "loss to X" / "defeat to X" style phrasing.
    frm, fk, to, tk = resolve(
        "Arsenal's Champions League final loss to Paris Saint-Germain in May.")
    assert to != "Paris Saint Germain"


def test_bare_to_still_works_for_genuine_movement_phrasing():
    # Sanity check: the block list must not swallow legitimate bare "to"
    # movement grammar that doesn't already have its own verb pattern (e.g.
    # "transfer to X" — "transfer" isn't a _DEST_VERB verb, so this relies
    # entirely on the bare "to" fallback still working for real cases).
    frm, fk, to, tk = resolve("The transfer to Newcastle looks imminent.")
    assert to == "Newcastle" and tk == "Newcastle"


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
