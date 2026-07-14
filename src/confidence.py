"""
FPL VORTEX — Confidence Engine.

Turns the verified signals produced upstream (entity type, FPL/player verification,
club resolution, transfer direction, source tier, PL relevance) into a single
numeric score and a publish decision. This is the composition layer the spec asks
for — it does NOT re-implement extraction; it SCORES what the pipeline already
verified, so precision and recall are tunable in one place.

Scoring (additive):
    verified_player          +25
    verified_clubs           +20
    event_verified           +15
    direction_verified       +15
    official_source          +15
    multiple_sources         +10
    premier_league_relevant  +10
    <any hard entity reject> -100   (journalist/company/brand/sponsor/media/
                                      stadium/club/agent/director/executive/unknown)

Decision:
    score >= 90   -> AUTO_POST
    75 <= score<90-> REVIEW   (queue, don't auto-publish)
    score < 75    -> SKIP

Pure + testable: score_signals() takes a dict of booleans; evaluate() builds that
dict from a story + a few facts the caller already knows (FPL match, source tiers).
"""

import json
import re
import unicodedata
from pathlib import Path

from src.entity_guard import classify_entity_detailed, _HARD_REJECT

_DATA = Path(__file__).resolve().parent.parent / "data"

# ── Weights & thresholds (single source of truth) ─────────────────────────
WEIGHTS = {
    "verified_player": 25,
    "verified_clubs": 20,
    "event_verified": 15,
    "direction_verified": 15,
    "official_source": 15,
    "elite_source": 5,
    "multiple_sources": 10,
    "premier_league_relevant": 10,
}
ENTITY_REJECT_PENALTY = -100
AUTO_POST_THRESHOLD = 90
REVIEW_THRESHOLD = 75

AUTO_POST, REVIEW, SKIP = "AUTO_POST", "REVIEW", "SKIP"

# Events the bot supports, and which ones are directional transfers.
SUPPORTED_EVENTS = {
    "player_transfer", "transfer", "loan", "loan_option", "contract_extension",
    "renewal", "stay", "injury", "injury_update", "suspension",
    "return_from_injury", "staff_appointment", "staff_departure",
    "managerial_change", "manager", "retirement", "release", "free_transfer",
    "academy_promotion",
}
_DIRECTIONAL = {"player_transfer", "transfer", "loan", "loan_option"}
_FREE_MOVE = {"free_transfer", "release"}


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ── Club knowledge (for club-existence + direction validation) ────────────
def _load_clubs():
    try:
        c = json.load(open(_DATA / "clubs_cache.json", encoding="utf-8"))
    except Exception:
        return set(), set()
    pl = set()
    for key, aliases in c.get("premier_league", {}).items():
        pl.add(_norm(key))
        pl.update(_norm(a) for a in aliases)
    efl = {_norm(x) for x in c.get("efl_and_foreign", [])}
    return pl, efl


_PL_CLUBS, _OTHER_CLUBS = _load_clubs()


def club_exists(name) -> bool:
    """True if a club name/key resolves to a known PL, EFL or foreign club."""
    n = _norm(name)
    if not n:
        return False
    if n in _PL_CLUBS or n in _OTHER_CLUBS:
        return True
    # token-overlap fallback for lightly-mangled names
    toks = set(n.split())
    if not toks:
        return False
    for known in _PL_CLUBS | _OTHER_CLUBS:
        kt = set(known.split())
        if kt and (toks == kt or len(toks & kt) >= 2):
            return True
    return False


def is_pl_club(name) -> bool:
    n = _norm(name)
    if n in _PL_CLUBS:
        return True
    toks = set(n.split())
    return any(toks == set(k.split()) for k in _PL_CLUBS if k)


# ── Direction validation ──────────────────────────────────────────────────
def validate_direction(story) -> tuple:
    """(ok, reason). A transfer needs a verified direction:
      - both origin AND destination resolve to real clubs, and differ; OR
      - a free/release move with a verified destination only.
    Non-directional events (injury/contract/manager/etc.) are 'ok' as long as the
    one relevant club is known. This is what rejects "Dennis Wise -> Chelsea"
    (no origin club) while passing "Rapid Vienna -> Brighton"."""
    ev = story.get("event")
    frm = story.get("from_key") or story.get("from_club")
    to = story.get("to_key") or story.get("to_club")

    if ev in _DIRECTIONAL:
        t_ok = club_exists(to)
        origin_present = bool(_norm(frm))
        if t_ok and origin_present:
            if _norm(frm) == _norm(to):
                return False, "from_equals_to"
            # Destination is a known club and an origin was extracted. We trust the
            # extracted origin even if it's an obscure foreign club not in our
            # cache (recall for foreign signings). The strict both-clubs-known
            # check only gates the verified_clubs bonus, not direction itself.
            return True, ("direction_verified" if club_exists(frm)
                          else "direction_verified_foreign_origin")
        if t_ok and not origin_present:
            # Destination-only is valid ONLY for an explicit free/release move —
            # otherwise there is no verified direction (kills "<retired name> -> PL club").
            blob = _norm(" ".join(str(story.get(k, "")) for k in
                                  ("raw_text", "body", "headline")))
            if any(c in blob for c in ("free agent", "free transfer", "released",
                                       "out of contract", "on a free")):
                return True, "free_move_destination_only"
            return False, "origin_unverified"
        return False, "destination_unverified"

    if ev in _FREE_MOVE:
        return (True, "free_move") if club_exists(to) else (False, "no_destination_club")

    # Injury / suspension / contract / manager / return: one known club suffices.
    if club_exists(to) or club_exists(frm):
        return True, "single_club_ok"
    return False, "no_club"


def clubs_verified(story) -> bool:
    """Both relevant clubs (or the single relevant one) resolve to real clubs."""
    ev = story.get("event")
    frm = story.get("from_key") or story.get("from_club")
    to = story.get("to_key") or story.get("to_club")
    if ev in _DIRECTIONAL:
        if club_exists(frm) and club_exists(to):
            return True
        # free/release destination-only counts as clubs-verified
        return validate_direction(story)[0]
    return club_exists(to) or club_exists(frm)


# ── Scoring ───────────────────────────────────────────────────────────────
def score_signals(signals: dict) -> dict:
    """Pure scorer. signals: bool flags matching WEIGHTS keys, plus optional
    'entity_penalty' (int, e.g. -100) and 'entity_type'/'entity_reason'."""
    breakdown = {}
    total = 0
    for key, weight in WEIGHTS.items():
        if signals.get(key):
            breakdown[key] = weight
            total += weight
    penalty = int(signals.get("entity_penalty", 0) or 0)
    if penalty:
        breakdown["entity_reject"] = penalty
        total += penalty
    decision = (AUTO_POST if total >= AUTO_POST_THRESHOLD
                else REVIEW if total >= REVIEW_THRESHOLD
                else SKIP)
    return {
        "score": total,
        "decision": decision,
        "breakdown": breakdown,
        "entity_type": signals.get("entity_type", "PLAYER"),
        "entity_reason": signals.get("entity_reason", "player_ok"),
    }


def evaluate(story, *, player_verified=False, official_source=False,
             elite_source=False, n_sources=0, pl_relevant=None) -> dict:
    """Build the signal dict from a story + caller-known facts, then score it.

    Args:
        player_verified: FPL/trusted-DB match OR a reliable-source-reported signing.
        official_source: an official club / league / tier-1 source is present.
        n_sources:       number of distinct sources.
        pl_relevant:     override PL relevance; if None, derived from clubs.
    """
    # Clause-breaking separator so role-binding regexes can't match ACROSS field
    # boundaries (e.g. "...free agent" + "Callum Wilson..." must not read as
    # "agent Callum Wilson"). The '.' is a hard clause break in the binder.
    text = " . ".join(str(story.get(k, "") or "") for k in
                      ("raw_text", "body", "headline"))
    name = story.get("player") or ""
    etype, ereason = classify_entity_detailed(name, text)

    ev = story.get("event")
    dir_ok, dir_reason = validate_direction(story)
    to = story.get("to_key") or story.get("to_club")
    frm = story.get("from_key") or story.get("from_club")
    if pl_relevant is None:
        pl_relevant = is_pl_club(to) or is_pl_club(frm)

    signals = {
        "verified_player": bool(player_verified) and etype == "PLAYER",
        "verified_clubs": clubs_verified(story),
        "event_verified": ev in SUPPORTED_EVENTS,
        "direction_verified": dir_ok,
        "official_source": bool(official_source),
        "elite_source": bool(elite_source),
        "multiple_sources": (n_sources or 0) >= 2,
        "premier_league_relevant": bool(pl_relevant),
        "entity_type": etype,
        "entity_reason": ereason,
        "entity_penalty": ENTITY_REJECT_PENALTY if etype in _HARD_REJECT else 0,
    }
    # Staff (coach/manager) can't be a "verified_player"; give them player credit
    # for the staff pipeline so a real coach move can still clear REVIEW/AUTO.
    if etype in {"COACH", "MANAGER", "ASSISTANT_COACH"}:
        signals["verified_player"] = True
        signals["direction_verified"] = dir_ok or club_exists(to) or club_exists(frm)

    result = score_signals(signals)
    result["direction_reason"] = dir_reason
    result["signals"] = {k: signals[k] for k in WEIGHTS}
    return result


def decision_log_line(story, result) -> str:
    """Compact, greppable one-line audit record for each decision."""
    return (
        "[CONFIDENCE] "
        f"decision={result['decision']} score={result['score']} "
        f"entity={result['entity_type']} event={story.get('event')} "
        f"player={story.get('player')!r} "
        f"from={story.get('from_key') or story.get('from_club')!r} "
        f"to={story.get('to_key') or story.get('to_club')!r} "
        f"signals={result.get('signals')} reason={result['entity_reason']}"
    )
