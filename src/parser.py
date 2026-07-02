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

    loan_signal = ("on loan" in tl) or bool(re.search(r"\bjoine?d?\b.*\bon loan\b", tl))
    
    # Priority classification
    if has_word(["suspended", "suspension", "banned", "ban", "red card", "sent off"], tl): event = "suspension"
    elif loan_signal: event = "loan"
    elif has_word(["injury", "injured", "ruled out", "scan", "hamstring", "surgery", "doubt"], tl): event = "injury"
    elif has_word(["sack", "appoint", "head coach", "manager"], tl): event = "manager"
    elif has_word(["new deal", "new contract", "signs new", "extension", "renew"], tl): event = "renewal"
    elif has_word(["stay", "staying", "no exit", "not for sale", "remain"], tl) and not has_word(["sign for", "joins", "move to"], tl): event = "stay"
    else: event = "transfer"

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
                # Resolve clubs and order them by FIRST appearance in the text (fallback only)
    club_pos = {}
    for alias in _SORTED_ALIASES:
        m = re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', tl)
        if m:
            k = CLUB_ALIASES[alias]
            if k not in club_pos or m.start() < club_pos[k]:
                club_pos[k] = m.start()
    clubs = sorted(club_pos, key=lambda k: club_pos[k])

    fpl_player_el = find_player_in_fpl(name, fpl_data) if name and fpl_data else None
    actual_current_club_key = fpl_team_key(fpl_player_el, fpl_data) if fpl_player_el else None

    # Directional cue detection: look for a preposition/verb immediately
    # before each club mention to determine from/to, instead of trusting
    # raw sentence order.
    FROM_CUES = [r"from", r"leaves?", r"departs?", r"exits?"]
    TO_CUES = [r"to", r"joins?", r"sign(?:s|ed)?\s+for", r"move\s+to",
               r"agree(?:s|d)?\s+(?:a\s+)?(?:deal\s+)?with", r"heading\s+to"]
    REJECT_CUES = [r"reject(?:ed|s)?\s+by", r"turn(?:ed|s)?\s+down\s+by",
                   r"pull(?:ed|s)?\s+out", r"walk(?:ed|s)?\s+away\s+from"]

    def _cue_before(alias_start, cues):
        window = tl[max(0, alias_start - 25):alias_start]
        return any(re.search(cue + r'\s*$', window) for cue in cues)

    cued_from, cued_to, cued_reject = None, None, set()
    for alias in _SORTED_ALIASES:
        m = re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', tl)
        if not m:
            continue
        k = CLUB_ALIASES[alias]
        if _cue_before(m.start(), REJECT_CUES):
            cued_reject.add(k)
        elif _cue_before(m.start(), FROM_CUES) and cued_from is None:
            cued_from = k
        elif _cue_before(m.start(), TO_CUES) and cued_to is None:
            cued_to = k

    from_key = cued_from or actual_current_club_key
    to_key = cued_to

    if to_key is None:
        candidates = [c for c in clubs if c != from_key and c not in cued_reject]
        if actual_current_club_key:
            if candidates:
                to_key = candidates[0]
        elif candidates:
            to_key = candidates[0]
            if from_key is None and len(candidates) > 1:
                from_key = candidates[1]


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
