"""
Fee / contract / loan / manager-appointment extraction tests.

Locks a real user-reported failure (13 Jul 2026): correct stories were posting
with "PRICE — TBD" / "CONTRACT — TBD" even though the source text stated a
fee and contract length outright, because:
  1. The fee regex only matched a single amount, not a RANGE ("between
     €25-30 million", "€25m and €30m") — extremely common wording.
  2. Nothing extracted contract length at all ("a three-year contract"),
     so the field was always empty.
  3. "has officially confirmed his loan move..." lost the loan-vs-transfer
     classification race to the generic word "confirmed", which appears
     earlier in the sentence than "loan".
  4. "confirmed as THE NEW head coach" (a filler word between "as" and the
     role cue) wasn't recognised as staff at all, so a manager appointment
     was mis-parsed as a player transfer between two "clubs".

Run with pytest OR standalone:  python tests/test_field_extraction.py
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "twikit" not in sys.modules:
    tw = types.ModuleType("twikit"); tw.Client = object; sys.modules["twikit"] = tw
import main  # noqa: E402

main.init_club_data()


# ── Fee range extraction ──────────────────────────────────────────────────

def test_fee_range_with_dash():
    from src.parser import _extract_fee
    fee = _extract_fee("a potential fee between €25-30 million if targets are met")
    assert fee == "€25-30 M", fee


def test_fee_range_with_and_and_unit_on_both_sides():
    from src.parser import _extract_fee
    fee = _extract_fee("a fee between €25m and €30m if add-ons are met")
    assert fee is not None and "25" in fee and "30" in fee


def test_fee_single_amount_still_works():
    from src.parser import _extract_fee
    assert _extract_fee("a straightforward £30 million deal") == "£30 M"


def test_fee_none_when_absent():
    from src.parser import _extract_fee
    assert _extract_fee("no numbers here at all") is None


# ── Contract-duration extraction ─────────────────────────────────────────

def test_contract_years_word_form():
    from src.parser import _extract_contract
    assert _extract_contract("Rose signed a three-year contract in April 2026") == "3-Year Deal"


def test_contract_digit_form():
    from src.parser import _extract_contract
    assert _extract_contract("signs a 4-year deal at the club") == "4-Year Deal"


def test_contract_long_term():
    from src.parser import _extract_contract
    assert _extract_contract("agreed a long-term contract with the club") == "Long-Term Deal"


def test_contract_none_when_absent():
    from src.parser import _extract_contract
    assert _extract_contract("no term mentioned here") is None


# ── Loan vs transfer classification (Gruda) ──────────────────────────────

_GRUDA_TEXT = (
    "Brajan Gruda has officially confirmed his loan move from Brighton and "
    "Hove Albion to RB Leipzig for the 2026/27 season. The loan includes a "
    "conditional obligation to buy, with a potential fee between €25-30 "
    "million if specific performance targets are met."
)


def test_loan_wins_even_when_confirmed_appears_earlier():
    s = main.build_story(_GRUDA_TEXT, None)
    assert s["event"] == "loan", s["event"]


def test_loan_card_shows_extracted_fee_not_tbd():
    s = main.build_story(_GRUDA_TEXT, None)
    s["display_name"] = s["player"]
    body = main.build_tweet_body(s, ["FabrizioRomano"], "confirmed")
    assert "PRICE — TBD" not in body
    assert "25-30" in body


# ── Manager appointment via "confirmed as THE NEW <role>" (Marco Rose) ──

_ROSE_TEXT = (
    "Marco Rose has been officially confirmed as the new head coach of AFC "
    "Bournemouth. Rose signed a three-year contract in April 2026, "
    "officially taking over from Andoni Iraola at the start of the 2026/27 "
    "season."
)


def test_confirmed_as_the_new_role_recognised_as_staff():
    s = main.build_story(_ROSE_TEXT, None)
    assert s["event"] == "manager", s["event"]
    assert s.get("staff_role") == "head coach"
    assert s.get("staff_action") == "appointment"


def test_manager_appointment_card_shows_extracted_contract_not_tbd():
    s = main.build_story(_ROSE_TEXT, None)
    s["display_name"] = s["player"]
    mode = main.classify_post(s, ["David_Ornstein"])
    body = main.build_tweet_body(s, ["David_Ornstein"], mode)
    assert "CONTRACT — 3-Year Deal" in body
    assert "TBD" not in body


def test_free_agent_still_not_misread_as_agent_role():
    # Regression guard: the "as <role>" filler-word tolerance added for the
    # Marco Rose fix must not let "as a free agent" (an employment status)
    # match the AGENT role cue through the same pattern.
    from src.entity_guard import classify_entity_detailed
    etype, _ = classify_entity_detailed(
        "Callum Wilson", "Callum Wilson joins Brentford as a free agent")
    assert etype == "PLAYER", etype


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
