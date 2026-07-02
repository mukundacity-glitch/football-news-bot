"""
FPL VORTEX — Entity Safety Guard.

Deterministic gate that classifies the extracted "player" BEFORE any story can be
posted, so journalists, companies/sponsors, coaches/staff and stadiums can never be
published as footballers. No network, no heuristics beyond the loaded blocklists —
knowledge-based and testable.

Public API:
    classify_entity(name, text="") -> (category, reason)
        category in {"PLAYER","JOURNALIST","COMPANY","STAFF","STADIUM"}.
    is_postable_player(name, text="", event="transfer") -> (ok: bool, reason: str)
        The hard gate. Only real players (for non-manager events) pass.
"""

import json
import re
import unicodedata
from pathlib import Path

_DATA = Path(__file__).resolve().parent.parent / "data"


def _strip(s: str) -> str:
    """Lowercase, remove accents, collapse whitespace, drop punctuation."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _load(name, key, default):
    try:
        with open(_DATA / name, "r", encoding="utf-8") as f:
            return json.load(f).get(key, default)
    except Exception:
        return default


# Loaded once at import; small static files.
_JOURNALISTS = {_strip(n) for n in _load("journalists.json", "journalists", [])}
_COMPANIES = {_strip(n) for n in _load("companies_blacklist.json", "companies", [])}
_COMPANY_SUFFIX = {_strip(t) for t in _load("companies_blacklist.json", "suffix_tokens", [])}
_STADIUMS = {_strip(n) for n in _load("companies_blacklist.json", "stadiums", [])}
_STAFF = {_strip(n) for n in _load("staff_roles.json", "staff", [])}
_ROLE_CUES = [_strip(c) for c in _load("staff_roles.json", "role_cues", []) if c]
_DEPARTURE_CUES = [_strip(c) for c in _load("staff_roles.json", "departure_cues", []) if c]
_APPOINTMENT_CUES = [_strip(c) for c in _load("staff_roles.json", "appointment_cues", []) if c]

# Club knowledge base (foreign + EFL), for rejecting a club misparsed as a player.
_STOP = {"le", "la", "les", "de", "du", "des", "el", "los", "las", "al", "the",
         "and", "of", "fc", "cf", "sc", "ac", "1", "cd", "ud"}
_KNOWN_CLUBS = {_strip(c) for c in _load("clubs_extended.json", "known_clubs", [])}
_CLUB_TOKENS = {_strip(t) for t in _load("clubs_extended.json", "club_tokens", [])}
_KNOWN_CLUB_TOKENSETS = [
    frozenset(t for t in c.split() if t not in _STOP) for c in _KNOWN_CLUBS
]


def detect_club_entity(name_norm) -> bool:
    """True if the (normalized) name is actually a football club, not a person.

    Signals (any one): exact known-club match; a strong club-indicator token
    (Stade, Borussia, Olympiacos, Wanderers, ...); or >=2 significant tokens
    overlapping a known club (catches mangled parses like 'le paris saint')."""
    if not name_norm:
        return False
    toks = [t for t in name_norm.split() if t not in _STOP]
    if not toks:
        return False
    if name_norm in _KNOWN_CLUBS:
        return True
    if any(t in _CLUB_TOKENS for t in toks):
        return True
    tset = set(toks)
    return any(len(tset & cts) >= 2 for cts in _KNOWN_CLUB_TOKENSETS)


def _name_matches(name_norm, blockset) -> bool:
    """Exact normalized match, or same set of name tokens (order-insensitive)."""
    if name_norm in blockset:
        return True
    ntok = set(name_norm.split())
    if len(ntok) >= 2:
        for b in blockset:
            if set(b.split()) == ntok:
                return True
    return False


def _norm_keep_punct(s):
    """Lowercase + de-accent but KEEP , ; . - so clause breaks are visible."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9,;.\-\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def staff_role_of(name, text):
    """Return the staff role phrase (e.g. 'sporting director') if the subject is
    football staff, else None. The role title must be bound to THIS name — either
    directly before it ('sporting director Luis Campos') or in apposition with a
    connector ('Luis Campos, sporting director' / '... as sporting director'). A
    role that belongs to a DIFFERENT person (e.g. 'Joao Pedro; head coach Maresca')
    is not matched. Pure role-cue logic — no name list."""
    n = _strip(name)
    if _name_matches(n, _STAFF):
        return "staff"
    if not n:
        return None
    t = _norm_keep_punct(text)
    if not t:
        return None
    nm = re.escape(n)
    for cue in _ROLE_CUES:
        if not cue:
            continue
        c = re.escape(cue)
        # title immediately before the name
        if re.search(r"(?<![a-z])" + c + r"\s+" + nm + r"\b", t):
            return cue
        # apposition: name , | - | as | the  <role>   (NOT a ';' / '.' clause break)
        if re.search(nm + r"\s*(?:,|-|\bas\b|\bthe\b)\s*" + c + r"\b", t):
            return cue
    return None


def is_staff_subject(name, text="") -> bool:
    return staff_role_of(name, text) is not None


def staff_action_of(text):
    """Classify the staff move as 'departure' or 'appointment' (else None) so the
    news is worded accurately instead of a generic/incorrect claim."""
    t = _strip(text)
    if any(c in t for c in _DEPARTURE_CUES):
        return "departure"
    if any(c in t for c in _APPOINTMENT_CUES):
        return "appointment"
    return None


def classify_entity(name, text=""):
    """Return (category, reason). Order matters: most-specific rejections first."""
    name_norm = _strip(name)
    text_norm = _strip(text)
    if not name_norm:
        return "PLAYER", "empty_name"  # let downstream name-length checks handle it

    if detect_club_entity(name_norm):
        return "CLUB", "club_entity"

    if _name_matches(name_norm, _JOURNALISTS):
        return "JOURNALIST", "journalist_entity"

    if _name_matches(name_norm, _STADIUMS):
        return "STADIUM", "stadium_entity"

    if _name_matches(name_norm, _COMPANIES):
        return "COMPANY", "company_entity"
    if any(tok in _COMPANY_SUFFIX for tok in name_norm.split()):
        return "COMPANY", "company_entity"

    if staff_role_of(name, text):
        return "STAFF", "staff_entity"

    return "PLAYER", "player_ok"


def is_postable_player(name, text="", event="transfer"):
    """Hard gate. Returns (ok, reason).

    - Journalists / companies / stadiums are NEVER postable, for any event.
    - Staff/coaches are rejected for player events (transfer/loan/injury/contract);
      they are only legitimate for event == 'manager'.
    """
    category, reason = classify_entity(name, text)
    if category in ("JOURNALIST", "COMPANY", "STADIUM", "CLUB"):
        return False, reason
    if category == "STAFF" and event != "manager":
        return False, reason
    return True, "player_ok"
