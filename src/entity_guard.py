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


def _role_cue_near_name(name_norm, text_norm) -> bool:
    """True if a staff role phrase sits within ~60 chars of the name in the text."""
    if not name_norm or not text_norm:
        return False
    for m in re.finditer(re.escape(name_norm), text_norm):
        lo, hi = max(0, m.start() - 60), m.end() + 60
        window = text_norm[lo:hi]
        if any(cue and cue in window for cue in _ROLE_CUES):
            return True
    return False


def classify_entity(name, text=""):
    """Return (category, reason). Order matters: most-specific rejections first."""
    name_norm = _strip(name)
    text_norm = _strip(text)
    if not name_norm:
        return "PLAYER", "empty_name"  # let downstream name-length checks handle it

    if _name_matches(name_norm, _JOURNALISTS):
        return "JOURNALIST", "journalist_entity"

    if _name_matches(name_norm, _STADIUMS):
        return "STADIUM", "stadium_entity"

    if _name_matches(name_norm, _COMPANIES):
        return "COMPANY", "company_entity"
    if any(tok in _COMPANY_SUFFIX for tok in name_norm.split()):
        return "COMPANY", "company_entity"

    if _name_matches(name_norm, _STAFF):
        return "STAFF", "staff_entity"
    if _role_cue_near_name(name_norm, text_norm):
        return "STAFF", "staff_entity"

    return "PLAYER", "player_ok"


def is_postable_player(name, text="", event="transfer"):
    """Hard gate. Returns (ok, reason).

    - Journalists / companies / stadiums are NEVER postable, for any event.
    - Staff/coaches are rejected for player events (transfer/loan/injury/contract);
      they are only legitimate for event == 'manager'.
    """
    category, reason = classify_entity(name, text)
    if category in ("JOURNALIST", "COMPANY", "STADIUM"):
        return False, reason
    if category == "STAFF" and event != "manager":
        return False, reason
    return True, "player_ok"
