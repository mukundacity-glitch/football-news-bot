"""
FPL VORTEX — Story Parsing and Extraction Engine.
Handles regex matching, categorization, and FPL-relevance safety gates.
"""

import re
from datetime import datetime, timezone
from src.fpl_feed import find_player_in_fpl, fpl_team_key
from src.constants import (
    FOOTBALL_KW, STAFF_BLOCK_KW, MANAGER_SURNAMES, CLUB_ALIASES,
    POSITION_WORDS, NATIONALITY_ADJECTIVES, OFFICIAL_INJURY_ACCOUNTS,
    STRONG_OFFICIAL_CUES
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

# Phrasing that names a club as merely INTERESTED / a potential hijacker, or
# as a PAST/REJECTED candidate, not the confirmed party to a move — e.g.
# "Aston Villa also interested" (still-active rival interest) or "was in the
# running for the Fulham job" (a candidacy that did NOT happen — the real
# move went elsewhere, e.g. Hugo Oliveira -> Strasbourg, not Fulham). A club
# whose only mention(s) sit in this kind of context must not be promoted
# into the from/to slots by the positional heuristic below.
_INTEREST_ONLY_CUES = (
    # still-active interest / hijack risk. Deliberately specific phrases only
    # — a bare "monitoring"/"tracking" is too generic (a club routinely
    # "monitors the development" of its OWN player after a loan/exit, which
    # is not transfer speculation about a third club at all).
    "also interested", "also interest", "among clubs attentive", "attentive to",
    "in the race", "keen on", "keen to sign", "admirer",
    "monitoring the situation", "monitoring developments", "eyeing a move",
    "eyeing a swoop", "chasing a move", "tracking the situation",
    "keeping tabs on", "alternative suitor", "rival interest",
    "hijack", "late move", "credited with interest", "touted as a",
    "touted as potential", "linked with interest", "also chasing", "also keen",
    "also monitoring", "also credited", "also tracking", "also linked",
    "rival to sign", "could rival",
    # past / rejected candidacy — the subject did NOT end up at this club
    "was in the running", "in the running for", "in contention for",
    "were in contention", "was a candidate for", "one of the candidates",
    "in the frame for", "was considered for", "was interviewed for",
    "had been linked with", "was among the candidates", "lost out on",
    "passed over for", "shortlisted for", "missed out on", "was not appointed",
    "did not get the job", "turned down the",
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


# Fee extraction: a RANGE ("a potential fee between €25-30 million",
# "£20m-£25m") is extremely common in real reporting (add-ons, performance-
# based obligations to buy) and was previously invisible — only a single
# amount matched, so any range-quoted fee silently fell back to "TBD" even
# though the number was right there in the text. Range pattern is tried
# first (more specific); the single-amount pattern is the fallback.
# Alternatives ordered LONGEST-first (million/billion before m/k). Regex
# alternation is first-match-wins, not longest-match-wins — with the
# abbreviation listed first, "million" partially matched as just "m" and
# silently truncated every fee of this shape (e.g. "£30-32 million" ->
# "£30-32 M"), which is why the fee field showed "Undisclosed" even when
# the number was right there in the tweet. General ordering rule, not
# specific to any one fee amount — fixes every past and future case of
# the same shape.
_FEE_RANGE_RE = re.compile(
    r'([£€$]\s?\d+(?:\.\d+)?\s*(?:million|billion|m|k)?\s*(?:-|–|—|to|and)\s*'
    r'(?:[£€$]\s?)?\d+(?:\.\d+)?\s*(?:million|billion|m|k))', re.IGNORECASE)
_FEE_SINGLE_RE = re.compile(r'([£€$]\s?\d+(?:\.\d+)?\s*(?:million|billion|m|k))', re.IGNORECASE)


def _extract_fee(text):
    m = _FEE_RANGE_RE.search(text) or _FEE_SINGLE_RE.search(text)
    if not m:
        return None
    # Normalise the unit word to the compact card form ("£30 MILLION" -> "£30 M")
    fee = m.group(1).upper()
    fee = re.sub(r'\s*MILLION\b', ' M', fee)
    fee = re.sub(r'\s*BILLION\b', ' B', fee)
    return re.sub(r'\s+', ' ', fee).strip()


# Contract-duration extraction. Nothing previously populated this field at
# all — "signs a three-year contract", "new deal until 2029" always fell
# back to "TBD" on the card even though the tweet stated it outright.
_CONTRACT_WORD_NUM = {"one": "1", "two": "2", "three": "3", "four": "4",
                      "five": "5", "six": "6", "seven": "7", "eight": "8"}
_CONTRACT_YEARS_RE = re.compile(
    r'\b(one|two|three|four|five|six|seven|eight|\d)[\s-]year\s*(?:contract|deal)\b',
    re.IGNORECASE)
_CONTRACT_UNTIL_RE = re.compile(
    r'\b(?:contract|deal)\s+(?:runs?\s+)?(?:until|through|to)\s+'
    r'((?:[A-Za-z]+\s+)?20\d{2})', re.IGNORECASE)
_CONTRACT_LONGTERM_RE = re.compile(r'\blong[\s-]term\s+(?:contract|deal)\b', re.IGNORECASE)


def _extract_contract(text):
    m = _CONTRACT_YEARS_RE.search(text)
    if m:
        n = _CONTRACT_WORD_NUM.get(m.group(1).lower(), m.group(1))
        return f"{n}-Year Deal"
    m = _CONTRACT_UNTIL_RE.search(text)
    if m:
        return f"Until {m.group(1).title()}"
    if _CONTRACT_LONGTERM_RE.search(text):
        return "Long-Term Deal"
    return None


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

    # "permanent" keyword is an explicit override: a tweet that says "permanent
    # transfer" or "permanent deal" must never be classified as a loan even if
    # the word "loan" appears elsewhere in the same text.
    _is_permanent = bool(re.search(r'\b(permanent\s+(?:transfer|deal|signing|move)|on\s+a\s+permanent\s+basis)\b', tl))
    # LOAN is a sub-type of "transfer" — loan words are folded into the transfer
    # position bucket, then the event is re-labelled "loan" afterward. When
    # "permanent" is explicitly stated, suppress the loan words entirely.
    _LOAN_WORDS = [] if _is_permanent else ["loan", "loans"]
    _loan_anywhere = not _is_permanent and has_word(["loan", "loans"], tl)

    # Classify by which category's cue occurs EARLIEST in the text, not by a
    # fixed category priority. Journalism tweets lead with the real news and
    # relegate side notes ("...remains focused on the World Cup despite being
    # currently injured") to a trailing clause — a fixed "injury beats
    # transfer" priority misreads that trailing aside as the headline.
    _event_positions = {
        "suspension": _cue_pos(["suspended", "suspension", "banned", "ban", "red card", "sent off"], tl),
        "injury": _cue_pos(["injury", "injured", "ruled out", "scan", "hamstring", "surgery", "doubt"], tl),
        "manager": _cue_pos(["sack", "appoint", "head coach", "manager"], tl),
        "renewal": _cue_pos(["new deal", "new contract", "signs new", "extension", "renew"], tl),
        "transfer": _cue_pos(["transfer", "sign", "signs", "signed", "joins", "joined",
                              "deal", "medical", "here we go", "official", "confirmed",
                              "completed", "agreement", "agreed"] + _LOAN_WORDS, tl),
    }
    _stay_pos = _cue_pos(["stay", "staying", "no exit", "not for sale", "remain"], tl)
    if _stay_pos is not None and not has_word(["sign for", "joins", "move to"], tl):
        _event_positions["stay"] = _stay_pos

    _found_events = {k: v for k, v in _event_positions.items() if v is not None}
    event = min(_found_events, key=_found_events.get) if _found_events else "transfer"
    if event == "transfer" and _loan_anywhere:
        event = "loan"

    stage = 4 if has_word(STRONG_OFFICIAL_CUES, tl) else \
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
        # from_key is NOT assigned positionally for non-FPL players.
        # direction.resolve() in build_story() handles from_club via grammar
        # ("from X", "agreed fee with X"). Positional second-club caused the
        # Manu Kone "FROM BRIGHTON" false post: Brighton was a comparison club
        # in the article, not Kone's actual club (AS Roma).

    is_collapsed = has_word(["collapsed", "called off", "rejected", "deal off"], tl)

    return {
        "is_football": True, "event": event,
        "is_real_move": event in ("transfer", "loan", "loan_option"),
        "player": name,
        "from_club": from_key.replace("_", " ") if from_key else None,
        "to_club": to_key.replace("_", " ") if to_key else None,
        "from_key": from_key, "to_key": to_key,
        "fee": _extract_fee(cleaned),
        "contract": _extract_contract(cleaned),
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
    if not (pl_player or pl_club):
        return False, "not_pl_relevant"

    # NEGATIVE OUTCOME GATE: if the article explicitly says the deal/bid was
    # rejected, collapsed, or ruled out, never publish it as a transfer.
    # These phrases signal the OPPOSITE of a completed move — posting them
    # as transfer news is factually wrong and damages credibility.
    if story.get("event") in ("transfer", "loan", "loan_option"):
        _NEG_SIGNALS = (
            "bid rejected", "rejected bid", "rejected a bid",
            "bid turned down", "turned down the bid",
            "bid knocked back", "knocked back the bid",
            "not for sale", "refuses to sell", "refused to sell",
            "rejected move", "rejects move", "rejects a move",
            "deal collapsed", "deal has collapsed",
            "falls through", "fell through", "has fallen through",
            "pulled out", "pulls out",
            "no deal agreed", "deal off", "no fee agreed",
            "failed to agree", "unable to agree",
            "turned down a bid", "turned down an offer",
        )
        if any(s in tl for s in _NEG_SIGNALS):
            return False, "negative_outcome_detected"

    return True, "ok_pl_transfer"
