"""
Mandatory entity-safety test suite.

Covers every case the bot must never get wrong. Runs with pytest OR standalone:
    python tests/test_entity_guard.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.entity_guard import classify_entity, is_postable_player, is_staff_subject


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


def test_case5_coach_pako_ayestaran_not_a_player_transfer():
    # CASE 5: assistant head coach — staff, never a PLAYER transfer, but it IS
    # postable as staff/manager news (must not be discarded).
    text = "Aston Villa assistant head coach Pako Ayestaran has departed the club."
    cat, reason = classify_entity("Pako Ayestaran", text)
    assert cat == "STAFF", (cat, reason)
    assert is_postable_player("Pako Ayestaran", text, "transfer")[0] is False  # not a player move
    assert is_postable_player("Pako Ayestaran", text, "manager")[0] is True    # posts as staff/manager news
    # A generic coach caught purely by role context (name not pre-listed):
    ctx = "Brighton first team coach John Smithson leaves for a new role."
    assert classify_entity("John Smithson", ctx)[0] == "STAFF"


def test_staff_role_and_action_detection():
    from src.entity_guard import (staff_role_of, staff_action_of, is_staff_subject,
                                  classify_entity_detailed)
    dep = "Aston Villa assistant head coach Pako Ayestaran has departed the club."
    assert is_staff_subject("Pako Ayestaran", dep) is True
    assert staff_role_of("Pako Ayestaran", dep) == "assistant head coach"
    assert staff_action_of(dep) == "departure"
    # A COACH appointment is postable staff.
    appt = "Chelsea appoint Marcus Sorg as first team coach."
    assert staff_role_of("Marcus Sorg", appt) == "first team coach"
    assert staff_action_of(appt) == "appointment"
    # A DIRECTOR is NOT postable staff (spec: only PLAYER/COACH/MANAGER publish).
    dtxt = "Chelsea appoint Luis Campos as sporting director."
    assert classify_entity_detailed("Luis Campos", dtxt)[0] == "DIRECTOR"
    assert is_postable_player("Luis Campos", dtxt, "manager")[0] is False


def test_player_mentioned_with_coach_not_flipped():
    # A coach merely mentioned in a player's transfer must NOT flip the subject.
    text = "Chelsea complete the signing of Joao Pedro; head coach Maresca is delighted."
    assert is_staff_subject("Joao Pedro", text) is False
    assert classify_entity("Joao Pedro", text)[0] == "PLAYER"


def test_build_story_routes_staff_to_manager():
    # End-to-end: a staff (player-event) story is re-routed to manager/staff news.
    import sys, types
    if "twikit" not in sys.modules:
        tw = types.ModuleType("twikit"); tw.Client = object; sys.modules["twikit"] = tw
    import main
    main.init_club_data()
    s = main.build_story("Aston Villa assistant coach Pako Ayestaran has departed the club.", None)
    assert s["event"] == "manager", s["event"]          # re-routed off the player pipeline
    assert s.get("staff_role") == "assistant coach"
    assert s.get("staff_action") == "departure"


def test_company_suffix_heuristic():
    # Brand/company/outlet names caught by structural suffix even if not pre-listed.
    # All are hard-rejects; the exact bucket (COMPANY/BRAND/MEDIA) may vary.
    _REJECT = {"COMPANY", "BRAND", "SPONSOR", "MEDIA"}
    for name in ("Boyle Sports", "Northbridge Insurance", "Vertex Holdings"):
        assert classify_entity(name, "")[0] in _REJECT, name
        assert is_postable_player(name, "", "transfer")[0] is False, name


def test_junk_and_fragment_names_rejected():
    # RSS / scraper / social-noise fragments must never post as a player.
    from src.entity_guard import classify_entity_detailed
    for name in ("Link Click", "link click", "Why Harry Kane", "From June",
                 "Our Carabao Cup", "Should Mateus", "Watch Live", "RT Fabrizio"):
        assert classify_entity_detailed(name, "")[0] == "UNKNOWN", name
        assert is_postable_player(name, "", "transfer")[0] is False, name


def test_director_executive_agent_rejected():
    from src.entity_guard import classify_entity_detailed as C
    assert C("Luis Campos", "Luis Campos appointed as sporting director")[0] == "DIRECTOR"
    assert C("Todd Boehly", "Chelsea chairman Todd Boehly said")[0] == "EXECUTIVE"
    assert C("Jorge Mendes", "super agent Jorge Mendes is negotiating")[0] == "AGENT"
    for n, t in (("Luis Campos", "Luis Campos appointed as sporting director"),
                 ("Todd Boehly", "Chelsea chairman Todd Boehly"),
                 ("Jorge Mendes", "super agent Jorge Mendes")):
        assert is_postable_player(n, t, "transfer")[0] is False, n
        assert is_postable_player(n, t, "manager")[0] is False, n


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
