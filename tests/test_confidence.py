"""
Confidence-engine test suite.

Locks the publish decision for every category in the spec:
  - legitimate PL transfers / injuries / suspensions / manager & coach moves POST,
  - journalists / companies / sponsors / media / stadiums / clubs / agents /
    directors / executives / junk fragments are SKIPPED,
  - the historical failure examples resolve correctly,
  - recall is preserved for new/foreign signings (obscure origin club).

Run with pytest OR standalone:  python tests/test_confidence.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.confidence import (evaluate, score_signals, validate_direction,
                            club_exists, AUTO_POST, REVIEW, SKIP)


def _story(**kw):
    kw.setdefault("raw_text", kw.get("headline", ""))
    return kw


# ── PURE SCORER ──────────────────────────────────────────────────────────

def test_score_thresholds():
    assert score_signals({"verified_player": True, "verified_clubs": True,
                          "event_verified": True, "direction_verified": True,
                          "official_source": True})["decision"] == AUTO_POST  # 90
    assert score_signals({"verified_player": True, "verified_clubs": True,
                          "event_verified": True, "direction_verified": True,
                          })["decision"] == REVIEW  # 75
    assert score_signals({"verified_player": True, "verified_clubs": True,
                          "event_verified": True})["decision"] == SKIP  # 60
    # Any hard entity reject buries the score no matter what else is true.
    r = score_signals({"verified_player": True, "verified_clubs": True,
                       "event_verified": True, "direction_verified": True,
                       "official_source": True, "multiple_sources": True,
                       "premier_league_relevant": True, "entity_penalty": -100})
    assert r["decision"] == SKIP and r["breakdown"]["entity_reject"] == -100


# ── MUST POST (recall) ───────────────────────────────────────────────────

def test_confirmed_pl_transfer_posts():
    r = evaluate(_story(player="Michael Svoboda", event="transfer",
                        from_club="Rapid Vienna", to_key="Brighton",
                        headline="Brighton sign Michael Svoboda from Rapid Vienna"),
                 player_verified=True, official_source=True, n_sources=2)
    assert r["decision"] == AUTO_POST, r


def test_foreign_and_efl_signings_post():
    for frm, to in (("Olympiacos", "Brighton"), ("AIK", "Brighton"),
                    ("Aston_Villa", "Sheffield Wednesday"),
                    ("Liverpool", "Bolton Wanderers")):
        r = evaluate(_story(player="Real Person", event="transfer",
                            from_club=frm, to_club=to,
                            headline=f"joins {to} from {frm}"),
                     player_verified=True, official_source=True, n_sources=1)
        assert r["decision"] == AUTO_POST, (frm, to, r)


def test_injury_and_suspension_post():
    for ev in ("injury", "suspension", "return_from_injury", "injury_update"):
        r = evaluate(_story(player="Alexander Isak", event=ev, to_key="Newcastle",
                            headline="Isak update at Newcastle"),
                     player_verified=True, official_source=True, n_sources=1)
        assert r["decision"] == AUTO_POST, (ev, r)


def test_contract_extension_posts():
    r = evaluate(_story(player="Michael Kayode", event="contract_extension",
                        to_key="Brentford",
                        headline="Michael Kayode signs new deal at Brentford"),
                 player_verified=True, official_source=True, n_sources=2)
    assert r["decision"] == AUTO_POST, r


def test_manager_and_coach_moves_post():
    for role_txt in ("Chelsea appoint Marcus Sorg as first team coach",
                     "Nottingham Forest name new head coach"):
        r = evaluate(_story(player="Marcus Sorg", event="manager", to_key="Chelsea",
                            staff_role="first team coach", headline=role_txt),
                     player_verified=False, official_source=True, n_sources=1)
        assert r["decision"] in (AUTO_POST, REVIEW), (role_txt, r)


def test_free_agent_signing_posts():
    r = evaluate(_story(player="Callum Wilson", event="free_transfer",
                        to_key="Brentford",
                        headline="Callum Wilson joins Brentford as a free agent"),
                 player_verified=True, official_source=True, n_sources=1)
    assert r["decision"] == AUTO_POST, r


# ── MUST NOT POST (precision) ────────────────────────────────────────────

def test_journalist_skipped():
    r = evaluate(_story(player="Florian Plettenberg", event="transfer",
                        to_key="Brighton", headline="Plettenberg reports talks"),
                 player_verified=True, official_source=True, n_sources=3)
    assert r["decision"] == SKIP and r["entity_type"] == "JOURNALIST", r


def test_company_sponsor_media_stadium_skipped():
    samples = [
        ("Lilley Plummer Risks", "COMPANY"),
        ("Boyle Sports", None),          # BRAND/MEDIA — either way SKIP
        ("Emirates", None),
        ("Sky Sports", "MEDIA"),
        ("Old Trafford", "STADIUM"),
        ("Real Madrid", "CLUB"),
    ]
    for name, etype in samples:
        r = evaluate(_story(player=name, event="transfer", to_key="Arsenal",
                            headline=f"{name} in the news"),
                     player_verified=True, official_source=True, n_sources=3)
        assert r["decision"] == SKIP, (name, r)
        if etype:
            assert r["entity_type"] == etype, (name, r["entity_type"])


def test_director_executive_agent_skipped():
    for name, txt in (("Luis Campos", "Luis Campos appointed as sporting director"),
                      ("Todd Boehly", "Chelsea chairman Todd Boehly"),
                      ("Jorge Mendes", "super agent Jorge Mendes")):
        r = evaluate(_story(player=name, event="manager", to_key="Chelsea",
                            headline=txt),
                     player_verified=False, official_source=True, n_sources=2)
        assert r["decision"] == SKIP, (name, r)


def test_junk_fragments_skipped():
    for name in ("Link Click", "Why Harry Kane", "From June", "Our Carabao Cup"):
        r = evaluate(_story(player=name, event="transfer", from_club="Leeds",
                            to_key="Brentford", headline="link click confirmed"),
                     player_verified=True, official_source=True, n_sources=2)
        assert r["decision"] == SKIP and r["entity_type"] == "UNKNOWN", (name, r)


def test_retired_or_no_origin_transfer_skipped():
    # Dennis Wise "confirmed transfer to Chelsea" — no origin club => no verified
    # direction => must not auto-post (the real-world false positive).
    r = evaluate(_story(player="Dennis Wise", event="transfer", to_key="Chelsea",
                        headline="Dennis Wise confirmed transfer to Chelsea"),
                 player_verified=True, official_source=False, n_sources=1)
    assert r["decision"] == SKIP, r
    assert validate_direction(_story(player="Dennis Wise", event="transfer",
                                     to_key="Chelsea"))[0] is False


def test_unverified_rumour_demoted_not_autoposted():
    # A single non-official source with a bare link — should not AUTO_POST.
    r = evaluate(_story(player="Some Player", event="transfer", to_key="Arsenal",
                        headline="linked with a move"),
                 player_verified=False, official_source=False, n_sources=1)
    assert r["decision"] != AUTO_POST, r


# ── DIRECTION + CLUB HELPERS ─────────────────────────────────────────────

def test_club_existence():
    assert club_exists("Arsenal") and club_exists("Sheffield Wednesday")
    assert club_exists("Olympiacos") and club_exists("Real Madrid")
    assert not club_exists("") and not club_exists("Link Click")


def test_direction_same_club_rejected():
    ok, why = validate_direction(_story(player="X", event="transfer",
                                        from_key="Arsenal", to_key="Arsenal"))
    assert ok is False and why == "from_equals_to"


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
