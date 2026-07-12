"""
FPL VORTEX — Story Parsing and Extraction Engine.
Handles regex matching, categorization, and FPL-relevance safety gates.
"""

import re
from datetime import datetime, timezone
from src.fpl_feed import find_player_in_fpl, fpl_team_key
from src.constants import (
    FOOTBALL_KW, STAFF_BLOCK_KW, MANAGER_SURNAMES, CLUB_ALIASES, 
    POSITION_WORDS, NATIONALITY_ADJECTIVES, OFFICIAL_INJURY_ACCOUNTS
)

# Sort aliases by length descending
_SORTED_ALIASES = sorted(CLUB_ALIASES.keys(), key=len, reverse=True)

# Build fragments to avoid confusing common words with names
CLUB_WORD_FRAGMENTS = set()
SKIP = {"fc", "the", "de", "af", "sc", "if", "bk", "ac", "as", "vv", "rb", "al", "el", "cf", "sk", "fk", "and", "du", "us"}
for name in CLUB_ALIASES.keys():
    for word in re.split(r'[\s\-&]+', name):
        w = word.lower().strip("'")
        if w and w not in SKIP and len(w) >= 3:
            CLUB_WORD_FRAGMENTS.add(w)

def looks_like_club(name: str) -> bool:
    if not name: return False
    n = name.lower().strip()
    return n in CLUB_ALIASES or any(n == c or c in n for c in CLUB_ALIASES.keys() if len(c) >= 5)

def _clean_source_text(text: str) -> str:
    t = text or ""
    t = re.sub(r'\bRT\s+@\w+:?', ' ', t)
    t = re.sub(r'https?://\S+|www\.\S+', ' ', t)
    t = re.sub(r'[@#]', '', t)
    t = re.sub(r'["""]', '', t)
    return re.sub(r'\s+', ' ', t).strip()

# Phrasing that names a club as merely INTERESTED / a potential hijacker, not
# the confirmed party to a move — e.g. "Aston Villa also interested" or
# "Villa may make a late move" while the real story is Freiburg -> Newcastle.
# A club whose only mention(s) sit in this kind of context must not be
# promoted into the from/to slots by the positional heuristic below.
_INTEREST_ONLY_CUES = (
    "also interested", "also interest", "among clubs attentive", "attentive to",
    "in the race", "keen on", "keen to sign", "admirer", "monitoring",
    "eyeing", "chasing", "tracking", "alternative suitor", "rival interest",
    "hijack", "late move", "credited with interest", "touted as a",
    "touted as potential", "linked with interest", "also chasing", "also keen",
    "also monitoring", "also credited", "also tracking", "also linked",
    "rival to sign", "could rival",
)
# Strong deal-completion language that, if present in the SAME local context,
# overrides an interest-only cue (the club really is party to the move).
_STRONG_DEAL_CUES = (
    "agreed", "agreement", "deal with", "sign", "signs", "signed", "joins",
    "joined", "here we go", "confirmed", "official", "completed", "medical",
)


def _club_context_is_interest_only(pos: int, text: str) -> bool:
    """Scope the check to the CLAUSE (sentence) containing this position, not
    a fixed character window — a fixed window lets an unrelated verb in the
    previous/next sentence ("...deal signed. Aston Villa also interested...")
    leak in and wrongly treat the mention as more than just onlooker interest."""
    lo = pos
    while lo > 0 and text[lo - 1] not in ".;!?\n":
        lo -= 1
    hi = pos
    while hi < len(text) and text[hi] not in ".;!?\n":
        hi += 1
    ctx = text[lo:hi]
    if not any(c in ctx for c in _INTEREST_ONLY_CUES):
        return False
    return not any(c in ctx for c in _STRONG_DEAL_CUES)


def _cue_pos(words_list, text):
    """Earliest character position at which any word/phrase in words_list
    occurs, or None if none occur."""
    best = None
    for w in words_list:
        m = re.search(r'(?<![a-z])' + re.escape(w) + r'(?![a-z])', text)
        if m and (best is None or m.start() < best):
            best = m.start()
    return best


def _is_bad_name(low: str, event: str) -> bool:
    if event != "manager" and (low in MANAGER_SURNAMES or any(m in low for m in MANAGER_SURNAMES)): 
        return True
        
    FILLER = {"excl", "exclusive", "breaking", "official", "understand", "update", "deal", "medical", "source"}
    ROLE_WORDS = POSITION_WORDS.copy()
    for phrase in STAFF_BLOCK_KW:
        ROLE_WORDS.update([w for w in phrase.split() if len(w) > 3])
        
    words = low.split()
    if any(w in FILLER for w in words): return True
    if any(w in CLUB_WORD_FRAGMENTS for w in words): return True
    if any(w in NATIONALITY_ADJECTIVES for w in words): return True
    if any(w in ROLE_WORDS for w in words): return True
    if looks_like_club(low): return True
    return False

def extract_story_fallback(tweet_text: str, fpl_data=None) -> dict:
    cleaned = _clean_source_text(tweet_text)
    tl = cleaned.lower()

    def has_word(words_list, text):
        return any(re.search(r'(?<![a-z])' + re.escape(w) + r'(?![a-z])', text) for w in words_list)

    _loan_m = re.search(r"\bon loan\b", tl) or re.search(r"\bjoine?d?\b.*\bon loan\b", tl)

    # Classify by which category's cue occurs EARLIEST in the text, not by a
    # fixed category priority. Journalism tweets lead with the real news and
    # relegate side notes ("...remains focused on the World Cup despite being
    # currently injured") to a trailing clause — a fixed "injury beats
    # transfer" priority misreads that trailing aside as the headline.
    _event_positions = {
        "suspension": _cue_pos(["suspended", "suspension", "banned", "ban", "red card", "sent off"], tl),
        "loan": _loan_m.start() if _loan_m else None,
        "injury": _cue_pos(["injury", "injured", "ruled out", "scan", "hamstring", "surgery", "doubt"], tl),
        "manager": _cue_pos(["sack", "appoint", "head coach", "manager"], tl),
        "renewal": _cue_pos(["new deal", "new contract", "signs new", "extension", "renew"], tl),
        "transfer": _cue_pos(["transfer", "sign", "signs", "signed", "joins", "joined",
                              "deal", "medical", "here we go", "official", "confirmed",
                              "completed", "agreement", "agreed"], tl),
    }
    _stay_pos = _cue_pos(["stay", "staying", "no exit", "not for sale", "remain"], tl)
    if _stay_pos is not None and not has_word(["sign for", "joins", "move to"], tl):
        _event_positions["stay"] = _stay_pos

    _found_events = {k: v for k, v in _event_positions.items() if v is not None}
    event = min(_found_events, key=_found_events.get) if _found_events else "transfer"

    stage = 4 if has_word(["here we go", "official", "confirmed", "completed", "joins"], tl) else \
            2 if has_word(["agreement", "agreed", "advanced", "personal terms"], tl) else 1

    name = None
    for m in re.findall(r'\b([A-ZÀ-ÖØ-Þ][a-zà-ÿ]+(?:\s+(?:(?:van|de|da|dos|del|el|la|le|di|du|den|der|ten|ter|von|zu)\s+)?[A-ZÀ-ÖØ-Þ][a-zà-ÿ]+)+)\b', cleaned):
        if not _is_bad_name(m.lower(), event):
            name = m
            break
            
    if not name and fpl_data:
        for m in re.findall(r'\b([A-ZÀ-ÖØ-Þ][a-zà-ÿ]{2,})\b', cleaned):
            if _is_bad_name(m.lower(), event): continue
            if find_player_in_fpl(m, fpl_data):
                name = m
                break

    # Resolve clubs and order them by FIRST appearance in the text, so the
    # destination heuristic picks the club the tweet actually leads with.
    # Mentions that are only ever "also interested" / "monitoring" / "could
    # hijack" chatter about a THIRD club are excluded from this pool — that
    # club isn't a party to the move, so it must never be promoted into the
    # from/to slots just because of where it happens to sit in the text.
    club_pos = {}
    for alias in _SORTED_ALIASES:
        k = CLUB_ALIASES[alias]
        best = None
        for m in re.finditer(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', tl):
            if _club_context_is_interest_only(m.start(), tl):
                continue
            if best is None or m.start() < best:
                best = m.start()
        if best is not None and (k not in club_pos or best < club_pos[k]):
            club_pos[k] = best
    clubs = sorted(club_pos, key=lambda k: club_pos[k])

    fpl_player_el = find_player_in_fpl(name, fpl_data) if name and fpl_data else None
    actual_current_club_key = fpl_team_key(fpl_player_el, fpl_data) if fpl_player_el else None

    from_key = actual_current_club_key
    to_key = None

    if actual_current_club_key:
        other_clubs = [c for c in clubs if c != actual_current_club_key]
        if other_clubs:
            to_key = other_clubs[0]
    elif clubs:
        to_key = clubs[0]
        if len(clubs) > 1: from_key = clubs[1]

    is_collapsed = has_word(["collapsed", "called off", "rejected", "deal off"], tl)
    fee_match = re.search(r'([£€$]\d+(?:\.\d+)?\s*(?:m|k|million|billion))', cleaned, re.IGNORECASE)

    return {
        "is_football": True, "event": event,
        "is_real_move": event in ("transfer", "loan", "loan_option"),
        "player": name,
        "from_club": from_key.replace("_", " ") if from_key else None,
        "to_club": to_key.replace("_", " ") if to_key else None,
        "from_key": from_key, "to_key": to_key,
        "fee": fee_match.group(1).upper() if fee_match else None,
        "stage": stage, "collapsed": is_collapsed,
        "headline": name if name else "Transfer update",
        "body": tweet_text,
    }

def detect_historical(text: str) -> bool:
    tl = (text or "").lower()
    _FRESH_CUE = re.compile(r"\b(today|tonight|tomorrow|breaking|here we go|confirmed|official|now|signed)\b", re.I)
    _HISTORICAL_MARKERS = re.compile(r"\b(on this day|otd|\d+\s+years?\s+ago|throwback|#tbt|remember when)\b", re.I)
    
    if _HISTORICAL_MARKERS.search(tl) and not _FRESH_CUE.search(tl):
        return True
    return False

def passes_safety_gate(story, raw_text, fpl_data, sources=None, source_tier_func=None):
    """
    Core gatekeeper. Enforces rigorous checks for FPL relevance, ensuring 
    injuries and suspensions ONLY trigger for verified premier league athletes.
    """
    sources = sources or []
    tl = (raw_text or "").lower()
    
    if story.get("historical"): return False, "historical_news"
    if not story.get("player"): return False, "no_player"

    tiers = [source_tier_func(s) for s in sources] if source_tier_func else [0]
    
    pl_player = find_player_in_fpl(story["player"], fpl_data) is not None

    # INJURY & SUSPENSION SAFETY LOCK
    if story["event"] in ("injury", "suspension"):
        injury_source_ok = any(t in (1, 2) for t in tiers) or any((s or "").lower().lstrip("@") in OFFICIAL_INJURY_ACCOUNTS for s in sources)
        if not injury_source_ok: return False, "injury_source_not_approved"

        # Verified FPL player is ideal. A reliable source reporting on a player at a
        # resolved Premier League club is also allowed (e.g. a new signing not yet
        # in the FPL dataset) — keeps it PL-relevant without blocking real news.
        if pl_player: return True, f"ok_{story['event']}"
        if story.get("to_key") or story.get("from_key"): return True, f"ok_{story['event']}_pl_club"
        return False, f"{story['event']}_not_pl_player"

    # TRANSFER SAFETY LOCK
    pl_club = bool(story.get("to_key") or story.get("from_key"))
    if pl_player or pl_club:
        return True, "ok_pl_transfer"

    return False, "not_pl_relevant"
