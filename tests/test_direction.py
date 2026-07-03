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
