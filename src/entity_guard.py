"""
FPL VORTEX — Entity Safety Guard.

Deterministic classifier that decides WHAT KIND of entity an extracted "player"
is, BEFORE any story can be posted. It is knowledge- and logic-based (no network,
no per-failure name hacks) so it generalises and is fully testable.

CLOSED WORLD. The blacklists below (journalists, sponsors, stadiums, clubs,
competitions, junk fragments) only ever REJECT. They never promote a name to
PLAYER. The single route to PLAYER is resolving against the squad registry —
the live FPL roster plus vouched-for, expiring manual overrides (see
``src.squad_registry``). An unrecognised name is UNKNOWN, which is a hard
reject, no matter how official the source or how confident the wording.

Taxonomy (classify_entity_detailed):
    PLAYER                                  -> enters transfer/injury validation
    COACH / MANAGER / ASSISTANT_COACH       -> staff-event pipeline (postable)
    DIRECTOR / EXECUTIVE / AGENT             -> rejected (not a player, not a coach)
    JOURNALIST / MEDIA                       -> rejected
    COMPANY / BRAND / SPONSOR                -> rejected
    STADIUM / CLUB                           -> rejected
    COUNTRY / COMPETITION / LEAGUE            -> rejected (a national team,
                                                 tournament, or league is never a transfer subject)
    UNKNOWN                                  -> rejected (junk, RSS fragment, noise,
                                                 OR any name absent from the squad registry)

Hard invariant: an entity classified as COUNTRY or COMPETITION/LEAGUE can NEVER become
PLAYER. This check runs unconditionally, before the default PLAYER fallback, and is
NOT bypassable by source tier, confidence override, or confirmation language — a
"confirmed" or "official" tweet about a tournament is still not a transfer.

Public API (stable):
    classify_entity(name, text="")          -> (coarse_category, reason)
    classify_entity_detailed(name, text="") -> (entity_type, reason)
    is_postable_player(name, text="", event="transfer") -> (ok, reason)
    looks_like_junk_name(name)              -> bool
    staff_role_of / is_staff_subject / staff_action_of  (coach/manager roles only)
    detect_club_entity(name_norm)
"""

import json
import re
import unicodedata
from pathlib import Path
from src.constants import MANAGER_SURNAMES
from src.fpl_feed import find_player_in_fpl
from src import squad_registry

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


def _load_json(name, default):
    try:
        with open(_DATA / name, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ── Knowledge bases (loaded once at import; small static files) ───────────
_JOURNALISTS = {_strip(n) for n in _load("journalists.json", "journalists", [])}

_COMPANIES = {_strip(n) for n in _load("companies_blacklist.json", "companies", [])}
_COMPANY_SUFFIX = {_strip(t) for t in _load("companies_blacklist.json", "suffix_tokens", [])}

_SPONSORS = {_strip(n) for n in _load("sponsors_blacklist.json", "names", [])}
_SPONSOR_SUFFIX = {_strip(t) for t in _load("sponsors_blacklist.json", "suffix_tokens", [])}
_SPONSOR_CONTEXT = [_strip(c) for c in _load("sponsors_blacklist.json", "context_cues", []) if c]

_MEDIA = {_strip(n) for n in _load("media_blacklist.json", "names", [])}
_MEDIA_SUFFIX = {_strip(t) for t in _load("media_blacklist.json", "suffix_tokens", [])}

_STADIUMS = {_strip(n) for n in _load("companies_blacklist.json", "stadiums", [])}
_STADIUMS |= {_strip(n) for n in _load("stadiums_blacklist.json", "names", [])}
_STADIUM_SUFFIX = {_strip(t) for t in _load("stadiums_blacklist.json", "suffix_tokens", [])}

# Reject-role groups (agent/director/executive) — logic cues, no name lists.
_PROT = _load_json("protected_entities.json", {})
_REASONS = _PROT.get("reasons", {})
_ROLE_GROUPS = _PROT.get("role_cues", {})
def _cues(items):
    """Normalise + sort role cues LONGEST-first so the most specific role wins
    ('assistant head coach' before 'head coach', 'sporting director' before ...)."""
    return sorted({_strip(c) for c in items if c}, key=len, reverse=True)


_AGENT_CUES = _cues(_ROLE_GROUPS.get("AGENT", []))
_DIRECTOR_CUES = _cues(_ROLE_GROUPS.get("DIRECTOR", []))
_EXEC_CUES = _cues(_ROLE_GROUPS.get("EXECUTIVE", []))

# Postable football staff (coach / manager) — the ONLY non-player roles we publish.
_STAFF = {_strip(n) for n in _load("staff_roles.json", "staff", [])}
_ROLE_CUES = _cues(_load("staff_roles.json", "role_cues", []))
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

# Country / national-team knowledge base. Exact normalized match ONLY (never a
# substring) — a real surname that happens to contain a country-like fragment
# must never be caught by accident.
_COUNTRIES = {_strip(c) for c in _load("countries.json", "countries", [])}

# Competition / tournament / league knowledge base, for rejecting "France World
# Cup"-style misparses where a country and a tournament get glued into one
# capitalized fragment by the extractor. Mirrors the club-detection strategy:
# exact name match, OR >=2 significant-token overlap with a known competition,
# OR a strong single-token indicator ("world cup", "champions league", ...).
_KNOWN_COMPETITIONS = {_strip(c) for c in _load("competitions.json", "known_competitions", [])}
_COMPETITION_TOKENS = {_strip(t) for t in _load("competitions.json", "competition_tokens", [])}
_KNOWN_COMPETITION_TOKENSETS = [
    frozenset(t for t in c.split() if t not in _STOP) for c in _KNOWN_COMPETITIONS
]

# ── Name-quality (junk / RSS-fragment / social-noise) filter ──────────────
# Tokens that NEVER appear inside a real footballer's name — they come from RSS
# fragments, scraper artifacts and social-media chrome ("link click", "watch",
# "RT", "reaction"). Any one of these in the extracted name => not a person.
_ARTIFACT_TOKENS = {
    "link", "links", "click", "clicks", "reaction", "reactions", "retweet", "rt",
    "subscribe", "watch", "video", "videos", "podcast", "episode", "clip", "clips",
    "thread", "breaking", "exclusive", "livestream", "stream", "highlights",
    "gallery", "poll", "quiz", "comments", "share", "tap",
}
# Words that make a name junk ONLY when they LEAD it (a real name never starts
# with a function/interrogative word). Catches "why harry kane", "from june",
# "should mateus", "our carabao", "not anderson".
_JUNK_LEADERS = {
    "why", "how", "what", "when", "where", "who", "should", "could", "would",
    "will", "can", "is", "are", "was", "were", "do", "does", "did", "the", "a",
    "an", "this", "that", "these", "those", "our", "your", "their", "his", "her",
    "from", "not", "no", "yes", "more", "latest", "update", "news", "here", "via",
    "meanwhile", "also", "plus", "elsewhere", "separately", "and", "but", "so",
    "if", "then", "just", "still", "now", "read", "full", "watch",
}


def _looks_like_junk_name(raw_name) -> bool:
    """True if the extracted name is an RSS/scraper/social fragment, not a person."""
    n = _strip(raw_name)
    if not n:
        return True
    toks = n.split()
    if any(t in _ARTIFACT_TOKENS for t in toks):
        return True
    if toks and toks[0] in _JUNK_LEADERS:
        return True
    # A name that carries NO alphabetic token of length >= 2 is noise.
    if not any(t.isalpha() and len(t) >= 2 for t in toks):
        return True
    return False


# Public alias.
def looks_like_junk_name(name) -> bool:
    return _looks_like_junk_name(name)


def _reason(etype, fallback):
    return _REASONS.get(etype, fallback)


def detect_club_entity(name_norm) -> bool:
    """True if the (normalized) name is actually a football club, not a person."""
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


def detect_country_entity(name_norm) -> bool:
    """True if the (normalized) name IS a country/national-team name — exact
    match only (single or multi-word), never a substring. This deliberately
    does NOT use token-overlap like clubs/competitions: a country name is a
    single fixed phrase ('south korea', 'ivory coast'), not a family of
    variant club names, so exact equality is the correct and safest test."""
    if not name_norm:
        return False
    return name_norm in _COUNTRIES


def detect_competition_entity(name_norm) -> bool:
    """True if the normalized name is a tournament/league/cup competition,
    including a country-and-competition fragment glued together by extraction.
    Uses an exact match, significant token overlap, or a structural competition
    indicator from the configurable knowledge base."""
    if not name_norm:
        return False
    toks = [t for t in name_norm.split() if t not in _STOP]
    if not toks:
        return False
    if name_norm in _KNOWN_COMPETITIONS:
        return True
    if any(t in _COMPETITION_TOKENS for t in toks):
        return True
    tset = set(toks)
    return any(len(tset & cts) >= 2 for cts in _KNOWN_COMPETITION_TOKENSETS)


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


def _has_suffix_token(name_norm, suffixset) -> bool:
    """True if any token of the name is a structural non-person marker
    (Insurance, Airways, Stadium, Sports, ...). Safe: real people don't carry
    these tokens in their name."""
    return any(tok in suffixset for tok in name_norm.split())


def _norm_keep_punct(s):
    """Lowercase + de-accent but KEEP , ; . - so clause breaks are visible."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9,;.\-\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _role_bound_to_name(name_norm, text, cues):
    """Return the role cue if one is bound to THIS name — directly before it
    ('sporting director Luis Campos') or in apposition with a connector
    ('Luis Campos, sporting director' / '... as sporting director'). A role that
    belongs to a DIFFERENT person (across a ';'/'.' clause break) is NOT matched."""
    if not name_norm:
        return None
    t = _norm_keep_punct(text)
    if not t:
        return None
    nm = re.escape(name_norm)
    for cue in cues:
        if not cue:
            continue
        c = re.escape(cue)
        if re.search(r"(?<![a-z])" + c + r"\s+" + nm + r"\b", t):
            return cue
        if re.search(nm + r"\s*(?:,|-|\bas\b|\bthe\b)\s*" + c + r"\b", t):
            return cue
        # appointment phrasing: "<n> ... as <role>" within the SAME clause
        # (no '.'/';' break) — e.g. "Luis Campos appointed as sporting director".
        # "as" is followed by up to 4 filler words ("the", "new", "the club's",
        # "their next", ...) before the cue — "confirmed as THE NEW head coach"
        # / "unveiled as THEIR NEW manager" is the standard appointment
        # phrasing, not the exception, and matching only a bare "as <cue>"
        # missed it entirely.
        # Guard: skip if a DIFFERENT known manager surname sits between the name
        # and the role cue (e.g. "...manager Iraola wants to keep Van Dijk" must
        # NOT bind "manager" to Van Dijk).
        m = re.search(nm + r"\b([^.;]{0,40}?)\bas\s+(?:\w+\s+){0,4}?" + c + r"\b", t)
        # "as a free agent" is an employment STATUS ("out of contract"), never
        # the AGENT role ("Jorge Mendes, agent of ...") — the filler-word
        # tolerance above would otherwise match "agent" through "as a free
        # agent" and misclassify a normal out-of-contract signing.
        if m and cue == "agent" and re.search(r"\bfree\s+agent\b", m.group(0)):
            m = None
        if m:
            between = m.group(1)
            other_manager_named = any(
                re.search(r'(?<![a-z])' + re.escape(surname) + r'(?![a-z])', between)
                for surname in MANAGER_SURNAMES
            )
            if not other_manager_named:
                return cue
    return None


def staff_role_of(name, text, fpl_data=None):
    """Return the COACH/MANAGER role phrase bound to this name, else None.
    (Directors / executives / agents are handled separately and are NOT postable.)

    GUARD: a name already verified against the live FPL feed is a real,
    rostered PLAYER — never staff. This is checked FIRST, before any text-
    proximity matching, because proximity alone ("Garnacho ... Villa job")
    can accidentally brush against role-cue language that has nothing to do
    with the player being staff. Ground truth beats text pattern-matching.
    """
    if fpl_data and find_player_in_fpl(name, fpl_data):
        return None
    n = _strip(name)
    if _name_matches(n, _STAFF):
        return "staff"
    return _role_bound_to_name(n, text, _ROLE_CUES)


def is_staff_subject(name, text="", fpl_data=None) -> bool:
    return staff_role_of(name, text, fpl_data) is not None


def staff_action_of(text):
    """Classify the staff move as 'departure' or 'appointment' (else None)."""
    t = _strip(text)
    if any(c in t for c in _DEPARTURE_CUES):
        return "departure"
    if any(c in t for c in _APPOINTMENT_CUES):
        return "appointment"
    return None


def sponsorship_context(text) -> bool:
    """Soft signal: the text is framed as a sponsorship/commercial announcement."""
    t = _strip(text)
    return any(cue in t for cue in _SPONSOR_CONTEXT)


def classify_entity_detailed(name, text="", fpl_data=None, role_hint=None):
    """Return (entity_type, reason) using the full taxonomy. Order = most-specific
    rejections first, postable staff next, PLAYER last — EXCEPT a name already
    verified against the live FPL feed, which short-circuits to PLAYER
    immediately (step 0) and skips every other check. A verified player can
    never be reclassified as staff, director, agent, club, or anything else
    based on surrounding text, no matter what words sit near their name."""
    name_norm = _strip(name)
    if not name_norm:
        # A missing name must NEVER default to PLAYER — that is exactly the
        # kind of "guess when data is missing" this gate exists to prevent.
        # UNKNOWN is a hard reject, so an empty/unresolved subject can never
        # slip through the confidence engine's PLAYER-only override path.
        return "UNKNOWN", _reason("UNKNOWN", "unknown_entity")

    # 0. GROUND TRUTH FIRST: a name the live FPL feed confirms is a real,
    # rostered player is PLAYER, full stop. This must run before every other
    # check below, because those checks are text-proximity matchers with no
    # concept of identity — they will happily misclassify a real player as
    # staff/director/agent if the surrounding tweet text merely brushes past
    # a role-cue word near the name.
    if fpl_data and find_player_in_fpl(name, fpl_data):
        return "PLAYER", "fpl_verified_player"

    # 1. Junk / RSS fragment / social noise ("link click", "why harry kane", ...).
    if _looks_like_junk_name(name):
        return "UNKNOWN", _reason("UNKNOWN", "unknown_entity")

    # 1. Competition / tournament / league misparsed as a person — checked
    # before club detection because a compound country-and-competition fragment
    # can also brush a club token by coincidence. This stops a national-team
    # tournament reference from ever being posted as a transfer subject,
    # regardless of source tier or "confirmed"/"official" wording in the text.
    if detect_competition_entity(name_norm):
        return "COMPETITION", _reason("COMPETITION", "competition_entity")

    # 1. Country / national team misparsed as a person.
    if detect_country_entity(name_norm):
        return "COUNTRY", _reason("COUNTRY", "country_entity")

    # 1. Club misparsed as a person.
    if detect_club_entity(name_norm):
        return "CLUB", _reason("CLUB", "club_entity")

    # 2. Stadium / venue.
    if _name_matches(name_norm, _STADIUMS) or _has_suffix_token(name_norm, _STADIUM_SUFFIX):
        return "STADIUM", _reason("STADIUM", "stadium_entity")

    # 3. Journalist (named knowledge base — explicitly maintained).
    if _name_matches(name_norm, _JOURNALISTS):
        return "JOURNALIST", _reason("JOURNALIST", "journalist_entity")

    # 4. Media outlet / broadcaster (name or structural suffix).
    if _name_matches(name_norm, _MEDIA) or _has_suffix_token(name_norm, _MEDIA_SUFFIX):
        return "MEDIA", _reason("MEDIA", "media_entity")

    # 5. Sponsor / brand / company (known name, or structural suffix token).
    if _name_matches(name_norm, _SPONSORS):
        return "SPONSOR", _reason("SPONSOR", "sponsor_entity")
    if _has_suffix_token(name_norm, _SPONSOR_SUFFIX):
        return "BRAND", _reason("BRAND", "brand_entity")
    if _name_matches(name_norm, _COMPANIES) or _has_suffix_token(name_norm, _COMPANY_SUFFIX):
        return "COMPANY", _reason("COMPANY", "company_entity")

    # 6. Reject-roles bound to the name: agent / director / executive.
    if _role_bound_to_name(name_norm, text, _AGENT_CUES):
        return "AGENT", _reason("AGENT", "agent_entity")
    if _role_bound_to_name(name_norm, text, _DIRECTOR_CUES):
        return "DIRECTOR", _reason("DIRECTOR", "director_entity")
    if _role_bound_to_name(name_norm, text, _EXEC_CUES):
        return "EXECUTIVE", _reason("EXECUTIVE", "executive_entity")

    # 8. Postable football staff: coach / assistant coach / manager.
    #    ``role_hint`` carries a role the pipeline already bound to THIS subject
    #    upstream (parser ``staff_role``). It is evidence, not a guess, so it
    #    counts alongside a role cue found in the text here — a coach appointment
    #    whose announcement names the role in a different field than the one this
    #    classifier sees must not fall through to the player allowlist.
    role = staff_role_of(name, text, fpl_data) or (role_hint or None)
    if role:
        rl = role.lower()
        if "assistant" in rl:
            return "ASSISTANT_COACH", "assistant_coach"
        if "manager" in rl or "head coach" in rl:
            return "MANAGER", "manager_or_head_coach"
        return "COACH", "coach"

    # 9. CLOSED-WORLD ALLOWLIST — the final and only route to PLAYER.
    #
    # Everything above this line is a blacklist: it removes names we have
    # already learned are not players. A blacklist can never be complete, so it
    # must not be the thing that DECIDES a name is a player. It isn't. A name
    # reaches this point having merely survived the known-bad checks, and it
    # becomes a PLAYER only by resolving against the squad registry (the live
    # FPL roster plus vouched-for, expiring manual overrides).
    #
    # This is what stops the whole class of "Manchester United Website has
    # joined Man Utd" failures at the source: that phrase survives every
    # blacklist ever written, and resolves against no squad on earth.
    if squad_registry.is_known_player(name, fpl_data):
        return "PLAYER", "squad_registry_verified"

    return "UNKNOWN", "not_in_squad_registry"


# Coarse categories map (backward-compatible with the previous 6-way classifier).
_COARSE = {
    "PLAYER": "PLAYER",
    "COACH": "STAFF", "MANAGER": "STAFF", "ASSISTANT_COACH": "STAFF",
    "DIRECTOR": "DIRECTOR", "EXECUTIVE": "EXECUTIVE", "AGENT": "AGENT",
    "JOURNALIST": "JOURNALIST", "MEDIA": "MEDIA",
    "COMPANY": "COMPANY", "BRAND": "COMPANY", "SPONSOR": "COMPANY",
    "STADIUM": "STADIUM", "CLUB": "CLUB",
    "COUNTRY": "COUNTRY", "COMPETITION": "COMPETITION", "UNKNOWN": "UNKNOWN",
}

# Postable staff types → the staff-event pipeline (event == "manager").
_STAFF_TYPES = {"COACH", "MANAGER", "ASSISTANT_COACH"}
# Everything below is never a player and never a coach → always rejected.
_HARD_REJECT = {"JOURNALIST", "MEDIA", "COMPANY", "BRAND", "SPONSOR",
                "STADIUM", "CLUB", "AGENT", "DIRECTOR", "EXECUTIVE",
                "COUNTRY", "COMPETITION", "UNKNOWN"}


def classify_entity(name, text="", fpl_data=None):
    """Coarse (category, reason). STAFF covers postable coach/manager roles."""
    etype, reason = classify_entity_detailed(name, text, fpl_data)
    return _COARSE.get(etype, etype), reason


def is_postable_player(name, text="", event="transfer", fpl_data=None):
    """Hard gate. Returns (ok, reason).

    - Real players pass for player events.
    - Coach/manager/assistant pass ONLY for event == 'manager' (staff pipeline).
    - Journalists, media, companies, brands, sponsors, stadiums, clubs, agents,
      directors, executives and junk fragments are NEVER postable.
    """
    etype, reason = classify_entity_detailed(name, text, fpl_data)
    if etype in _HARD_REJECT:
        return False, reason
    if etype in _STAFF_TYPES and event != "manager":
        return False, "staff_entity"
    return True, "player_ok"
