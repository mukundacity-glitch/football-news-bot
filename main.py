"""
FPL VORTEX — Transfer / Injury / Manager news bot for X.

Design goals:
  1. THE BOT READS THE STORY. An extraction step turns each raw tweet into an
     accurate, structured story — real from/to clubs, fees, deadlines and
     conditions. No fixed templates inventing a "joins" sentence.
  2. CORRECT CLUB / KIT. The player photo is only ever used when the name match
     is strict AND the player's real FPL club lines up with the story, so we
     never paste a different player's photo onto the card. Crest + colours come
     from the real destination club.
  3. NOTHING TRUE IS SILENTLY DROPPED. Official/confirmed reports post even from
     a single mid-tier source; loan/stay/renewal stories post with accurate
     framing instead of being skipped or mislabelled.

Extraction uses Anthropic Haiku when ANTHROPIC_API_KEY is set; otherwise it
falls back to a truthful summary built from the tweet text itself. Each tweet is
sent to the LLM at most ONCE (results are cached in posted_news.json), keeping
API cost and latency low across repeated runs.
"""
from clubs_cache import get_club_data
import os
import re
import json
import asyncio
import unicodedata 
import requests
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter
from pilmoji import Pilmoji
from twikit import Client

try:
    import google.generativeai as genai
    _GEMINI_OK = bool(os.getenv("GEMINI_API_KEY"))
    if _GEMINI_OK:
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
except Exception:
    _GEMINI_OK = False

# ── SECRETS ──────────────────────────────────────────────────────────────────
X_POST_AUTH_TOKEN = os.getenv("X_POST_AUTH_TOKEN")
X_POST_CT0_TOKEN  = os.getenv("X_POST_CT0_TOKEN")
X_AUTH_TOKEN      = os.getenv("X_AUTH_TOKEN")      # read account (twikit reader)
X_CT0_TOKEN       = os.getenv("X_CT0_TOKEN")
FOOTBALL_API_KEY  = os.getenv("FOOTBALL_API_KEY")

# ── PATHS ────────────────────────────────────────────────────────────────────
POSTED_FILE = Path("posted_news.json")
PENDING_DIR = Path("queue/pending")
POSTED_DIR  = Path("queue/posted")
for d in (PENDING_DIR, POSTED_DIR, Path("logos"), Path("players")):
    d.mkdir(parents=True, exist_ok=True)

# ── JOURNALISTS ──────────────────────────────────────────────────────────────
# NOTE: handles below are the CORRECT current X usernames. A wrong handle
# silently returns zero tweets, so these are kept accurate on purpose.
JOURNALISTS = [
    # Tier-1 global transfer journalists
    "FabrizioRomano", "David_Ornstein",
    # European transfer reporters
    "Plettigoal", "MatteoMoretto", "AlfredoPedulla", "DiMarzio",
    # PL-focused reporters & club beat writers
    "JacobsBen", "sistoney67", "_pauljoyce", "JamesPearceLFC",
    "mcgrathmike", "SkySportsNews",
]
NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]
# Tier-1: their official word is trusted to post alone.
TOP_SOURCES = {"FabrizioRomano", "David_Ornstein"}

# ── LIGHT PRE-FILTER ONLY (decides "is this worth extracting?", nothing else) ──
FOOTBALL_KW = [
    "transfer", "sign", "deal", "fee", "bid", "loan", "contract", "agree",
    "medical", "official", "here we go", "talks", "joins", "move", "target",
    "injury", "injured", "ruled out", "scan", "hamstring", "surgery", "doubt",
    "sack", "appoint", "manager", "head coach", "stay", "return", "recall",
]
# Off-pitch people we never post as a "transfer".
STAFF_BLOCK_KW = [
    "head of recruitment", "sporting director", "director of football",
    "technical director", "chief scout", "scouting", "ceo", "chairman",
    "owner", "president", "physio", "kit man", "head of football",
    "transfer chief", "negotiator",
]

# ── CLUB MAPS ────────────────────────────────────────────────────────────────
CLUB_ALIASES = {
    "arsenal": "Arsenal",
    "aston villa": "Aston_Villa", "villa": "Aston_Villa",
    "bournemouth": "Bournemouth",
    "brentford": "Brentford",
    "brighton": "Brighton",
    "chelsea": "Chelsea",
    "crystal palace": "Crystal_Palace", "palace": "Crystal_Palace",
    "everton": "Everton",
    "fulham": "Fulham",
    "ipswich": "Ipswich", "ipswich town": "Ipswich",
    "leicester": "Leicester", "leicester city": "Leicester",
    "liverpool": "Liverpool",
    "manchester city": "Man_City", "man city": "Man_City",
    "manchester united": "Man_Utd", "man united": "Man_Utd", "man utd": "Man_Utd",
    "newcastle": "Newcastle", "newcastle united": "Newcastle",
    "nottingham forest": "Nottm_Forest", "nott'm forest": "Nottm_Forest", "forest": "Nottm_Forest",
    "southampton": "Southampton",
    "tottenham": "Spurs", "spurs": "Spurs", "tottenham hotspur": "Spurs",
    "west ham": "West_Ham", "west ham united": "West_Ham",
    "wolves": "Wolves", "wolverhampton": "Wolves",
}
_SORTED_ALIASES = sorted(CLUB_ALIASES.keys(), key=len, reverse=True)
# Auto-built at startup from CLUB_ALIASES + clubs_cache — no hardcoding needed
CLUB_WORD_FRAGMENTS: set = set()

# Only these three small lists genuinely have no auto source in your stack
NATIONALITY_ADJECTIVES = {
    "english", "french", "german", "spanish", "italian", "portuguese", "dutch",
    "brazilian", "argentinian", "belgian", "croatian", "danish", "swedish",
    "norwegian", "scottish", "welsh", "irish", "austrian", "swiss", "polish",
    "ukrainian", "turkish", "greek", "serbian", "canadian", "american", "mexican",
    "japanese", "korean", "senegalese", "nigerian", "ghanaian", "moroccan",
    "egyptian", "cameroonian", "colombian", "uruguayan", "chilean", "australian",
    "algerian", "tunisian", "ivorian", "congolese", "zambiani",
}
POSITION_WORDS = {
    "goalkeeper", "defender", "midfielder", "striker", "winger",
    "forward", "keeper", "playmaker", "captain", "international",
}

FPL_LOGO_IDS = {
    "Arsenal": "3", "Aston_Villa": "7", "Bournemouth": "91", "Brentford": "94",
    "Brighton": "36", "Chelsea": "8", "Crystal_Palace": "31", "Everton": "11",
    "Fulham": "54", "Ipswich": "40", "Leicester": "13", "Liverpool": "14",
    "Man_City": "43", "Man_Utd": "1", "Newcastle": "4", "Nottm_Forest": "17",
    "Southampton": "20", "Spurs": "6", "West_Ham": "21", "Wolves": "39",
}
CLUB_COLORS = {
    "Arsenal": (239, 1, 7), "Aston_Villa": (103, 14, 54), "Bournemouth": (181, 14, 18),
    "Brentford": (227, 6, 19), "Brighton": (0, 87, 184), "Chelsea": (3, 70, 148),
    "Crystal_Palace": (27, 69, 143), "Everton": (39, 68, 136), "Fulham": (15, 15, 15),
    "Ipswich": (0, 0, 255), "Leicester": (0, 83, 160), "Liverpool": (200, 16, 46),
    "Man_City": (108, 173, 223), "Man_Utd": (218, 41, 28), "Newcastle": (15, 15, 15),
    "Nottm_Forest": (229, 50, 51), "Southampton": (215, 25, 32), "Spurs": (17, 24, 38),
    "West_Ham": (122, 38, 58), "Wolves": (253, 185, 19),
}
CLUB_HASHTAG_MAP = {
    "Arsenal": "#Arsenal", "Aston_Villa": "#AVFC", "Bournemouth": "#AFCB",
    "Brentford": "#Brentford", "Brighton": "#BHAFC", "Chelsea": "#Chelsea",
    "Crystal_Palace": "#CPFC", "Everton": "#EFC", "Fulham": "#FFC",
    "Ipswich": "#ITFC", "Leicester": "#LCFC", "Liverpool": "#LFC",
    "Man_City": "#MCFC", "Man_Utd": "#MUFC", "Newcastle": "#NUFC",
    "Nottm_Forest": "#NFFC", "Southampton": "#SaintsFC", "Spurs": "#THFC",
    "West_Ham": "#WHUFC", "Wolves": "#Wolves",
}

def resolve_club_key(name: str):
    """Map any club name string to our PL key, or None if it's not a PL club
    (e.g. Barcelona). None => no crest, which is correct, not a fake one."""
    if not name:
        return None
    n = name.lower()
    for alias in _SORTED_ALIASES:
        if re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', n):
            return CLUB_ALIASES[alias]
    return None

# Club whitelist scope, per product decision: Bundesliga, La Liga, and
# Champions League regulars (UCL is on the "always relevant" competition
# list, so its usual elite participants count even when not in Bundesliga/
# La Liga, e.g. PSG, Inter, Milan, Juventus).
BUNDESLIGA_BIG_CLUBS = {
    "bayern munich", "bayern", "borussia dortmund", "dortmund",
    "bayer leverkusen", "leverkusen", "rb leipzig",
}
LA_LIGA_BIG_CLUBS = {
    "real madrid", "barcelona", "atletico madrid", "atletico de madrid",
}
UCL_REGULAR_CLUBS = {
    "psg", "paris saint-germain", "paris saint germain",
    "inter milan", "inter", "ac milan", "milan", "juventus", "napoli", "roma",
}
BIG_CLUBS_NON_PL = BUNDESLIGA_BIG_CLUBS | LA_LIGA_BIG_CLUBS | UCL_REGULAR_CLUBS

def is_big_club_name(name: str) -> bool:
    if not name:
        return False
    n = name.lower().strip()
    return any(n == c or c in n for c in BIG_CLUBS_NON_PL)

def is_bundesliga_or_laliga_club(name: str) -> bool:
    """Stricter check used for injuries/managers: Bundesliga or La Liga only,
    no UCL-only clubs like PSG/Inter/Milan that aren't in those two leagues."""
    if not name:
        return False
    n = name.lower().strip()
    return any(n == c or c in n for c in (BUNDESLIGA_BIG_CLUBS | LA_LIGA_BIG_CLUBS))

# Globally recognizable stars who are unlikely to ever be FPL-eligible (so
# is_big_player()'s FPL-cost/points check can't catch them). Surname-keyed,
# lowercase. Keep this to genuinely A-list names — it's a deliberate
# whitelist, not a general celebrity filter.
BIG_NAMES_NON_FPL = {
    "mbappe", "mbappé", "vinicius", "vinícius", "bellingham", "rodrygo",
    "haaland", "lewandowski", "messi", "neymar", "ronaldo", "modric", "kroos",
    "benzema", "pedri", "gavi", "yamal", "kane", "musiala", "wirtz", "kvaratskhelia",
}

def is_big_name_player(name: str) -> bool:
    if not name:
        return False
    n = name.lower().strip()
    return any(part in BIG_NAMES_NON_FPL for part in re.split(r'[\s\-]+', n))

# ── CLUBS_CACHE WIRING (all leagues, not just PL) ────────────────────────────
CLUB_NAME_SET = set()        # every known club name/alias, lowercased
CLUB_HASHTAGS = {}           # name/alias -> hashtag (all leagues)
PL_CLUB_NAMES = set()        # PL club names/aliases, lowercased

def init_club_data():
    global CLUB_NAME_SET, CLUB_HASHTAGS, PL_CLUB_NAMES
    try:
        d = get_club_data()
    except Exception as e:
        print(f"[CLUBS] get_club_data failed: {e}")
        return
    CLUB_HASHTAGS = d.get("club_hashtags", {}) or {}
    PL_CLUB_NAMES = set(d.get("pl_clubs", []) or [])
    CLUB_NAME_SET = set(CLUB_HASHTAGS.keys()) | set((d.get("short_names", {}) or {}).keys())
    CLUB_NAME_SET |= set(CLUB_ALIASES.keys())
    _build_club_word_fragments()

def _build_club_word_fragments():
    SKIP = {"fc", "the", "de", "af", "sc", "if", "bk", "ac", "as", "vv",
            "rb", "al", "el", "cf", "sk", "fk", "and", "du", "us"}
    for name in (set(CLUB_ALIASES.keys()) | CLUB_NAME_SET):
        for word in re.split(r'[\s\-&]+', name):
            w = word.lower().strip("'")
            if w and w not in SKIP and len(w) >= 3:
                CLUB_WORD_FRAGMENTS.add(w)

# Auto-built from FPL bootstrap nationality field
COUNTRY_NAMES: set = set()

def _build_country_block(fpl_data):
    global COUNTRY_NAMES
    if not fpl_data:
        return
    for el in fpl_data.get("elements", []):
        nat = (el.get("nationality") or "").lower().strip()
        if nat:
            COUNTRY_NAMES.add(nat)

def looks_like_club(name: str) -> bool:
    """True if a candidate 'player' string is actually a known club (any league)."""
    if not name:
        return False
    n = name.lower().strip()
    if n in CLUB_NAME_SET or n in CLUB_ALIASES:
        return True
    return any(n == c or c in n for c in CLUB_NAME_SET if len(c) >= 5)

def hashtag_for(name_or_key: str):
    """Hashtag for any club (PL via our clean map, others via clubs_cache)."""
    if not name_or_key:
        return None
    key = name_or_key
    if key in CLUB_HASHTAG_MAP:                 # already a PL key e.g. 'Man_Utd'
        return CLUB_HASHTAG_MAP[key]
    n = name_or_key.replace("_", " ").lower()
    return CLUB_HASHTAG_MAP.get(resolve_club_key(n) or "", CLUB_HASHTAGS.get(n))

# ── STATE ────────────────────────────────────────────────────────────────────
def load_data() -> dict:
    fresh = {"daily": {"date": "", "count": 0, "limit": 17}, "stories": {}, "posted_ids": []}
    if POSTED_FILE.exists():
        try:
            with open(POSTED_FILE) as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[STATE] posted_news.json unreadable ({e}); starting fresh.")
            d = fresh
    else:
        d = fresh
    d.setdefault("daily", fresh["daily"])
    d.setdefault("stories", {})
    d.setdefault("posted_ids", [])
    d.setdefault("pending", {})
    d.setdefault("extracted", {})     # tweet_id -> story, so we never re-call the LLM
    return d

def save_data(data: dict):
    # atomic write: tmp file + rename, so a crash mid-write never corrupts state
    tmp = POSTED_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(POSTED_FILE)

def check_daily_limit(data: dict) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if data["daily"]["date"] != today:
        data["daily"] = {"date": today, "count": 0, "limit": 17}
    return data["daily"]["count"] < data["daily"]["limit"]

def increment_daily(data: dict):
    data["daily"]["count"] += 1

# ── STORY EXTRACTION (the brain) ─────────────────────────────────────────────
_EXTRACT_PROMPT = """You are a football transfer-desk editor. Read this reporter tweet and extract ONLY what it actually states. Do NOT invent, assume, or generalise. If the tweet is conditional (deadlines, options, "if X then Y"), capture that exactly.

ACCURACY RULES (critical):
- Every field must come from THIS tweet only. Never mix in other players, clubs, fees or stories.
- from_club is the player's CURRENT/SELLING club; to_club is the DESTINATION/BUYING club. Do not reverse them.
- from_club and to_club must be DIFFERENT clubs. If only one club is clearly named, set the other to null.
- Do not output placeholder text (e.g. "Player Name", "Example", "xxx", "TBD"). Use null when unknown.
- If the tweet is about a manager/coach appointment, set event=manager, not transfer.
- If the tweet is about an injury, set event=injury and leave transfer fields null.

LANGUAGE: The tweet may be in Spanish, Italian, Portuguese, French or German. ALL output text fields (headline, body, conditional, club and player names) MUST be in natural English. Translate everything. Never output non-English or mixed-language text. Use the player's and club's common English names.

Return STRICT JSON only, no markdown, no prose:
{{"is_football": true/false,
 "event": "transfer|loan|loan_option|stay|renewal|injury|manager|collapse|other",
 "is_real_move": true/false,
 "player": "full name or null",
 "from_club": "selling/current club full name or null",
 "to_club": "destination club full name or null",
 "fee": "e.g. £30m or null",
 "contract": "e.g. until 2028 or null",
 "conditional": "one short ENGLISH sentence describing any deadline/condition, else null",
 "fpl_impact": "one short ENGLISH sentence on the Fantasy Premier League angle (e.g. 'Expected to start immediately, a viable £5.5m midfield option.'), else null",
 "diagnosis": "for injury events only: the injury type in 1-4 words (e.g. 'Hamstring strain'), else null",
 "expected_return": "for injury events only: expected return / timeline in a few words (e.g. 'Out 3-4 weeks', 'Awaiting scans', 'Doubtful for weekend'), else null",
 "next_match": "for injury events only: the next/affected fixture if stated (e.g. 'Arsenal vs Liverpool'), else null",
 "stage": 1,            // 1=rumour/talks 2=agreement/advanced 3=signed 4=official/confirmed (or for injury: 1=concern 2=scan 3=ruled out 4=fit again)
 "collapsed": true/false,
 "headline": "<=10 word ENGLISH headline true to THIS exact story",
 "body": "1-2 factual ENGLISH sentences summarising THIS tweet, no filler, no hype template, no invented facts",
 "confidence": 0.0-1.0}}

Tweet:
\"\"\"{tweet}\"\"\""""

def extract_story_llm(tweet_text: str):
    if not _GEMINI_OK:
        return None
    try:
        model = genai.GenerativeModel("gemini-2.0-flash")
        resp = model.generate_content(_EXTRACT_PROMPT.format(tweet=tweet_text))
        raw = resp.text
        return json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception as e:
        print(f"  [LLM] extraction failed, using fallback: {e}")
        return None

def extract_story_fallback(tweet_text: str, fpl_data=None) -> dict:
    """No-LLM path: still TRUTHFUL — uses the tweet's own words as the body
    instead of a fabricated template. Crude club/stage guesses only."""
    tl = tweet_text.lower()

    def has_word(words_list, text):
        # whole-word match so 'unofficial' never triggers 'official'
        return any(re.search(r'\b' + re.escape(w) + r'\b', text) for w in words_list)

    clubs = []
    for alias in _SORTED_ALIASES:
        if re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', tl):
            k = CLUB_ALIASES[alias]
            if k not in clubs:
                clubs.append(k)

    if has_word(["injury", "injured", "ruled out", "scan", "hamstring", "surgery", "doubt", "knock"], tl):
        event = "injury"
    elif (has_word(["appoint", "sack", "part company"], tl) or
          (has_word(["manager", "head coach"], tl) and
           not has_word(["signing", "sign", "joins", "fee", "transfer", "bid"], tl))):
        event = "manager"
    elif has_word(["loan"], tl):
        event = "loan"
    elif has_word(["stay", "remain", "not for sale"], tl):
        event = "stay"
    else:
        event = "transfer"

    stage = 4 if has_word(["here we go", "official", "confirmed", "completed", "medical", "joins"], tl) else \
            2 if has_word(["agreement", "agreed", "advanced", "personal terms"], tl) else 1

    FILLER = {
        "excl", "exclusive", "breaking", "official", "understand", "understands",
        "update", "here", "done", "deal", "medical", "nothing", "all", "source",
        "news", "report", "reports", "told", "says", "said", "claim", "claims",
        "today", "tonight", "tomorrow", "now", "latest", "just", "also",
        "full", "free", "new", "big", "top", "key", "real", "transfer",
        "window", "deadline", "fee", "bid", "offer", "loan", "agree", "agreed",
        "talks", "interest", "signed", "signing", "joins", "joined", "move",
        "permanent", "option", "clause", "release", "extension", "premier",
        "league", "champions", "europa", "conference", "sport", "press",
    }

    # Auto-derive role words from your existing STAFF_BLOCK_KW
    ROLE_WORDS = set()
    for phrase in STAFF_BLOCK_KW:
        for word in phrase.split():
            if len(word) > 3:
                ROLE_WORDS.add(word)
    ROLE_WORDS |= POSITION_WORDS   # merge in the small hardcoded position list

    def _is_bad_name(low: str) -> bool:
        words = low.split()
        if any(w in FILLER for w in words):
            return True
        if any(w in CLUB_WORD_FRAGMENTS for w in words):
            return True
        if any(w in COUNTRY_NAMES for w in words):
            return True
        if any(w in NATIONALITY_ADJECTIVES for w in words):
            return True
        if any(w in ROLE_WORDS for w in words):
            return True
        if looks_like_club(low):
            return True
        return False

    name = None

    # Pass 1 — multi-word with Dutch/Spanish/Portuguese connectors
    # e.g. "Jan Paul van Hecke", "Virgil van Dijk", "Bruno Fernandes"
    for m in re.findall(
        r'\b([A-Z][a-zà-ÿ]+(?:\s+(?:(?:van|de|da|dos|del|el|la|le|di|du|den|der|ten|ter|von|zu)\s+)?[A-Z][a-zà-ÿ]+)+)\b',
        tweet_text
    ):
        if not _is_bad_name(m.lower()):
            name = m
            break

    # Pass 2 — plain two-capitalised-word spans (catches what pass 1 misses)
    if not name:
        for m in re.findall(r'\b([A-Z][a-zà-ÿ]+(?:[-\' ][A-Z][a-zà-ÿ]+)+)\b', tweet_text):
            if not _is_bad_name(m.lower()):
                name = m
                break

    # Pass 3 — single surname, must match FPL web_name exactly
    if not name and fpl_data:
        for m in re.findall(r'\b([A-Z][a-zà-ÿ]{2,})\b', tweet_text):
            if _is_bad_name(m.lower()):
                continue
            if find_player_in_fpl(m, fpl_data):
                name = m
                break

    # Pass 4 — single surname matching global star list
    if not name:
        for m in re.findall(r'\b([A-Z][a-zà-ÿ]{2,})\b', tweet_text):
            if _is_bad_name(m.lower()):
                continue
            if is_big_name_player(m):
                name = m
                break
    clean = re.sub(r'\s+', ' ', tweet_text).strip()

    # Determine from/to direction. Only trust it when the tweet clearly anchors
    # direction with "from X" / "to Y" / "joins Y" / "leaves X". Multi-club or
    # unanchored tweets are marked direction_confident=False so the card avoids
    # a misleading FROM→TO arrow.
    from_key = None
    to_key = None
    direction_confident = False

    def _alias_after(keyword):
        for alias in _SORTED_ALIASES:
            if re.search(r'\b' + keyword + r'\s+(?:the\s+)?' + re.escape(alias) + r'\b', tl):
                return CLUB_ALIASES[alias]
        return None

    from_anchor = _alias_after("from") or _alias_after("leaves") or _alias_after("leaving")
    to_anchor = (_alias_after("to") or _alias_after("joins") or _alias_after("join")
                 or _alias_after("sign for") or _alias_after("signs for") or _alias_after("moves to")
                 or _alias_after("set to join") or _alias_after("close to joining"))

    if from_anchor and to_anchor and from_anchor != to_anchor:
        from_key, to_key = from_anchor, to_anchor
        direction_confident = True
    elif from_anchor and len(clubs) == 2:
        from_key = from_anchor
        other = [c for c in clubs if c != from_anchor]
        to_key = other[0] if other else None
        direction_confident = bool(to_key)
    elif to_anchor and len(clubs) == 2:
        to_key = to_anchor
        other = [c for c in clubs if c != to_anchor]
        from_key = other[0] if other else None
        direction_confident = bool(from_key)
    elif len(clubs) == 1:
        # single club named, no clear direction → treat as "linked with" club
        to_key = clubs[0]
        direction_confident = False
    else:
        # ambiguous (0 or 3+ clubs, no clean anchor) → don't assert a direction
        to_key = clubs[0] if clubs else None
        direction_confident = False

    is_collapsed = has_word(["collapsed", "called off", "rejected", "deal off"], tl)

    return {
        "is_football": True, "event": event,
        "is_real_move": event in ("transfer", "loan", "loan_option"),
        "player": name,
        "from_club": (from_key.replace("_", " ") if from_key else None),
        "to_club": (to_key.replace("_", " ") if to_key else None),
        "from_key": from_key, "to_key": to_key,
        "fee": None, "contract": None, "conditional": None, "fpl_impact": None,
        "stage": stage, "collapsed": is_collapsed,
        "direction_confident": direction_confident,
        "headline": name if name else "Transfer update",
        "body": clean[:240], "confidence": 0.5,
    }

def build_story(tweet_text: str, fpl_data=None) -> dict:
    s = extract_story_llm(tweet_text) or extract_story_fallback(tweet_text, fpl_data)
    s["from_key"] = s.get("from_key") or resolve_club_key(s.get("from_club"))
    s["to_key"]   = s.get("to_key") or resolve_club_key(s.get("to_club"))
    try:
        s["stage"] = max(1, min(4, int(s.get("stage", 1))))
    except Exception:
        s["stage"] = 1
    s["collapsed"] = bool(s.get("collapsed"))

    # Direction confidence: verify the from/to against the tweet text itself.
    # The card only shows a FROM→TO arrow when we can ANCHOR the direction in
    # the words "from X" / "to Y" / "joins Y" etc. This prevents reversed-club
    # cards on multi-club or vaguely-worded tweets, regardless of LLM/fallback.
    if "direction_confident" not in s:
        tl = tweet_text.lower()
        fk, tk = s.get("from_key"), s.get("to_key")
        fc = (s.get("from_club") or "").lower()
        tc = (s.get("to_club") or "").lower()

        def _named_after(keyword, key, club):
            # does "keyword <club>" appear, for either the PL alias or raw name?
            cands = []
            if club:
                cands.append(club)
            if key:
                cands.append(key.replace("_", " ").lower())
            return any(re.search(r'\b' + keyword + r'\s+(?:the\s+)?' + re.escape(c) + r'\b', tl)
                       for c in cands if c)

        from_ok = _named_after("from", fk, fc) or _named_after("leaves", fk, fc)
        to_ok = (_named_after("to", tk, tc) or _named_after("joins", tk, tc)
                 or _named_after("join", tk, tc) or _named_after("sign for", tk, tc)
                 or _named_after("moves to", tk, tc))
        s["direction_confident"] = bool((from_ok and to_ok) or
                                        (from_ok and not (s.get("to_key") and s.get("from_key"))) or
                                        (to_ok and from_ok))
        # require BOTH ends anchored when both clubs are present
        if s.get("from_key") and s.get("to_key"):
            s["direction_confident"] = bool(from_ok and to_ok)
    return s

# ── FPL DATA ─────────────────────────────────────────────────────────────────
def fetch_fpl_data():
    cache = Path("fpl_cache.json")
    if cache.exists() and (datetime.now().timestamp() - cache.stat().st_mtime < 86400):
        with open(cache) as f:
            return json.load(f)
    try:
        req = urllib.request.Request(
            "https://fantasy.premierleague.com/api/bootstrap-static/",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            with open(cache, "w") as f:
                json.dump(data, f)
            _build_country_block(data)
            return data
    except Exception:
        return None

def _norm(s: str) -> str:
    """Lowercase + strip accents so 'Koné' matches 'Kone', 'Múñoz' matches 'Munoz'."""
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return s.lower().strip()


def find_player_in_fpl(player_name, data):
    """STRICT match, accent-insensitive. Returns an element only when the name clearly lines up."""
    if not data or not player_name:
        return None
    q = _norm(player_name)
    tokens = [t for t in re.split(r'[\s\-]+', q) if t]
    if not tokens:
        return None
    for el in data.get("elements", []):
        web = _norm(el["web_name"])
        full = _norm(el["first_name"] + " " + el["second_name"])
        if q == full or q == web:
            return el
        if len(tokens) >= 2 and all(
                re.search(r'(?<![a-z])' + re.escape(t) + r'(?![a-z])', full) for t in tokens):
            return el
        if len(tokens) == 1 and tokens[0] == web:
            return el
    return None


def is_big_player(player, fpl_data) -> bool:
    el = find_player_in_fpl(player, fpl_data)
    if not el:
        return False
    return el.get("now_cost", 0) >= 65 or el.get("total_points", 0) >= 90  # £6.5m or 90+ pts


def fpl_team_key(el, fpl_data):
    """The PL key (e.g. 'Man_Utd') of an FPL element's current club, or None."""
    if not el or not fpl_data:
        return None
    for t in fpl_data.get("teams", []):
        if t.get("id") == el.get("team"):
            return resolve_club_key((t.get("name", "") + " " + t.get("short_name", "")).lower())
    return None

# ── DEDUP / PROGRESSION ──────────────────────────────────────────────────────
def build_story_key(player, club_key, event) -> str:
    p = (player or "unknown").lower().replace(" ", "_")
    c = (club_key or "unknown").lower()
    fam = "injury" if event == "injury" else "manager" if event == "manager" else "transfer"
    return f"{p}_{c}_{fam}"

def should_post(data, key, new_stage, collapsed):
    existing = data["stories"].get(key)
    if collapsed:
        if not existing or existing["status"] == "active":
            return True, "collapse"
        return False, "already_collapsed"
    if not existing:
        return True, "new"
    if existing["status"] == "collapsed":
        return False, "story_collapsed"
    if new_stage <= existing["stage"]:
        return False, "no_progression"
    return True, "progression"

# ── SAFETY + POST MODE ───────────────────────────────────────────────────────
STRONG_OFFICIAL = ["here we go", "official", "confirmed", "completed", "done deal",
                   "sealed", "unveiled", "joins", "joined", "signs", "signed", "medical"]

def passes_safety_gate(story, raw_text, fpl_data):
    """Reject only things we should never post. Loan/stay/renewal are allowed —
    they just get accurate framing downstream."""
    tl = raw_text.lower()
    NON_NEWS_KW = ["documentary", "amazon prime", "netflix", "man of the match",
                   "potm", "player of the month", "kit launch", "new kit", "sponsor",
                   "anniversary", "birthday", "wins the", "award", "fifa the best",
                   "ballon d'or", "merch", "video game", "ea sports"]
    if any(k in tl for k in NON_NEWS_KW):
        return False, "off_topic_content"
    if not story.get("is_football"):
        return False, "not_football"
    if story.get("confidence", 0) < 0.45:
        return False, "low_confidence"
    if any(re.search(r'(?<![a-z])' + re.escape(w) + r'(?![a-z])', tl) for w in STAFF_BLOCK_KW):
        return False, "staff_or_offpitch"
    if not story.get("player"):
        return False, "no_player"
    if story["event"] == "manager":
        to_key = story.get("to_key")
        to_club = story.get("to_club")
        pl_club = bool(to_key) or (to_club and to_club.lower() in PL_CLUB_NAMES)
        bl_club = is_bundesliga_or_laliga_club(to_club)
        if not (pl_club or bl_club):
            return False, "manager_no_club"
        return True, "ok_manager"
    if story["event"] == "injury":
        pl_player = find_player_in_fpl(story["player"], fpl_data) is not None
        # to_club/to_key are always null for injury events (the extraction
        # prompt leaves transfer fields blank here), so there's no structured
        # club field to check. There's also no Bundesliga/La Liga player
        # database to verify against, so fall back to scanning the raw tweet
        # text for a Bundesliga/La Liga club name.
        bl_club_in_text = any(c in tl for c in (BUNDESLIGA_BIG_CLUBS | LA_LIGA_BIG_CLUBS))
        if pl_player or bl_club_in_text:
            return True, "ok_injury"
        return False, "injury_not_pl_bundesliga_laliga"
    pl_player = find_player_in_fpl(story["player"], fpl_data) is not None
    pl_club = bool(story.get("to_key") or story.get("from_key"))
    if not pl_club:
        for nm in (story.get("to_club"), story.get("from_club")):
            if nm and nm.lower() in PL_CLUB_NAMES:
                pl_club = True
                break
    if pl_player or pl_club:
        return True, "ok"
    # Not a PL player/club — still post if it's a big enough story for an FPL
    # audience to care about: a recognizable star, or a move to/from an elite
    # European club (Real Madrid, Bayern, PSG, etc.).
    big_player = is_big_player(story["player"], fpl_data) or is_big_name_player(story["player"])
    big_club = is_big_club_name(story.get("to_club")) or is_big_club_name(story.get("from_club"))
    if big_player or big_club:
        return True, "ok_big_name"
    return False, "not_fpl_relevant"

def classify_post(story, sources):
    """'confirmed' -> post as fact | 'rumour' -> labelled unconfirmed | None -> hold."""
    if story.get("collapsed"):
        return "confirmed"
    if story["event"] in ("manager", "injury", "stay", "renewal", "loan_option"):
        return "confirmed"
    tl = story.get("body", "").lower() + " " + (story.get("headline", "") or "").lower()
    strong = story["stage"] >= 4 or any(re.search(r'\b' + re.escape(w) + r'\b', tl) for w in STRONG_OFFICIAL)
    top_source = any(s in TOP_SOURCES for s in sources)
    multi = len(set(sources)) >= 2
    if strong or multi or top_source:
        return "confirmed"
    if is_big_player(story["player"], fetch_fpl_data()) or story.get("confidence", 0) >= 0.7:
        return "rumour"
    return None

# ── TWEET TEXT ───────────────────────────────────────────────────────────────
def twitter_len(text: str) -> int:
    url_re = re.compile(r'https?://\S+|www\.\S+')
    urls = url_re.findall(text)
    stripped = url_re.sub("", text)
    weight = 23 * len(urls)
    for ch in stripped:
        o = ord(ch)
        weight += 1 if (o <= 0x10FF or 0x2000 <= o <= 0x200D or 0x2010 <= o <= 0x201F or 0x2032 <= o <= 0x2037) else 2
    return weight

def trim_for_twitter(body: str, limit: int = 278) -> str:
    if twitter_len(body) <= limit:
        return body
    parts = body.rsplit("\n\n", 1)
    if len(parts) == 2 and parts[1].strip().startswith("#"):
        head, tags = parts[0], parts[1].split()
        while tags and twitter_len(head + "\n\n" + " ".join(tags)) > limit:
            tags.pop()
        cand = head + ("\n\n" + " ".join(tags) if tags else "")
        if twitter_len(cand) <= limit:
            return cand
        body = head
    out = ""
    for ch in body:
        if twitter_len(out + ch) > limit - 1:
            break
        out += ch
    return out.rstrip() + "…"

EVENT_PREFIX = {
    "transfer": "TRANSFER", "loan": "LOAN", "loan_option": "LOAN OPTION",
    "stay": "STAYING PUT", "renewal": "NEW DEAL", "injury": "INJURY",
    "manager": "MANAGER", "collapse": "COLLAPSED", "other": "UPDATE",
}

def build_hashtags(story):
    ev = story["event"]
    base = "#TransferNews" if ev in ("transfer", "loan", "loan_option") else \
           "#ManagerNews" if ev == "manager" else "#InjuryNews" if ev == "injury" else "#FootballNews"
    tags = [base, "#Football"]
    for key, name in ((story.get("to_key"), story.get("to_club")),
                      (story.get("from_key"), story.get("from_club"))):
        ht = hashtag_for(key) or hashtag_for(name)
        if ht and ht not in tags:
            tags.append(ht)
    if (story.get("to_key") or story.get("from_key")) and "#PremierLeague" not in tags:
        tags.append("#PremierLeague")
    return " ".join(tags[:5]) + " #FPL"

def build_tweet_body(story, sources, rumour: bool) -> str:
    ev = story["event"]
    prefix = "COLLAPSED" if story.get("collapsed") else EVENT_PREFIX.get(ev, "UPDATE")
    head = story.get("headline") or story.get("player") or "Update"
    lines = [f"🚨 {prefix} | {head}", "", story.get("body") or ""]
    if story.get("conditional"):
        lines.append(f"\n📌 {story['conditional']}")
    if story.get("fpl_impact"):
        lines.append(f"\n🎯 FPL: {story['fpl_impact']}")
    details = []
    if story.get("fee"):
        details.append(f"💰 Fee: {story['fee']}")
    if story.get("contract"):
        details.append(f"📄 {story['contract']}")
    if details:
        lines.append("\n" + "  |  ".join(details))
    body = "\n".join(p for p in lines if p is not None)
    if rumour:
        body = "⚠️ RUMOUR (UNCONFIRMED)\n" + body
    body += "\n\n" + build_hashtags(story)
    return body

def build_detail_line(story) -> str:
    bits = []
    if story.get("fee"):
        bits.append(f"💰 {story['fee']}")
    if story.get("contract"):
        bits.append(f"⏱️ {story['contract']}")
    if story.get("conditional"):
        bits.append(story["conditional"])
    return "  |  ".join(bits)

# ── GRAPHICS ENGINE ──────────────────────────────────────────────────────────
_FONT_CACHE = {}
# Readable, widely-available fallbacks. DejaVu/Liberation render crisp at the
# sizes used below, and the Pilmoji path handles colour emoji separately.
_FALLBACK_FONTS = {
    "Black": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
    "Bold":  ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
}

def _load_fallback(size, weight):
    for path in _FALLBACK_FONTS.get(weight, _FALLBACK_FONTS["Bold"]):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()

def get_premium_font(size, weight="Bold"):
    key = (weight, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    fp = f"Montserrat-{weight}.ttf"
    if not os.path.exists(fp):
        try:
            url = f"https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-{weight}.ttf"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r, open(fp, "wb") as out:
                out.write(r.read())
        except Exception:
            f = _load_fallback(size, weight); _FONT_CACHE[key] = f; return f
    try:
        f = ImageFont.truetype(fp, size)
    except Exception:
        f = _load_fallback(size, weight)
    _FONT_CACHE[key] = f
    return f

def _download_asset(url, dest: Path) -> bool:
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                return False
            data = resp.read()
        if not data:
            return False
        with open(tmp, "wb") as f:
            f.write(data)
        tmp.replace(dest)
        return True
    except Exception:
        try:
            tmp.exists() and tmp.unlink()
        except Exception:
            pass
        return False

def _safe_open_rgba(path: Path):
    try:
        im = Image.open(path); im.load(); return im.convert("RGBA")
    except Exception:
        try:
            path.unlink()
        except Exception:
            pass
        return None

def _fit_contain(im, w, h):
    return ImageOps.contain(im, (w, h), Image.Resampling.LANCZOS)

def _draw_arrow(d, x, y, w, color, thick=24):
    head = int(thick * 2.2); cy = y + thick // 2
    d.rounded_rectangle([x, y, x + w - head, y + thick], radius=thick // 2, fill=color)
    d.polygon([(x + w - head, cy - thick), (x + w, cy), (x + w - head, cy + thick)], fill=color)

def _draw_text_shadow(draw, xy, text, font, fill, shadow=(0, 0, 0), offset=2):
    """Draw text with a subtle shadow for readability over busy backgrounds."""
    x, y = xy
    draw.text((x + offset, y + offset), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=fill)

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2190-\u21FF\u2B00-\u2BFF\uFE0F]")

def _safe_emoji_text(img, xy, text, font, fill):
    """Render text with colour emoji via Pilmoji, but NEVER crash the card if
    the emoji source can't be fetched (e.g. no network). Falls back to plain
    text with emoji stripped, so a post always gets an image."""
    try:
        with Pilmoji(img) as pj:
            pj.text(xy, text, font=font, fill=fill)
    except Exception:
        plain = _EMOJI_RE.sub("", text).strip()
        ImageDraw.Draw(img).text(xy, plain, font=font, fill=fill)

def _photo_verified(player_el, fpl, from_key, to_key) -> bool:
    """STRICT: only trust the FPL photo when the player's actual FPL club
    EXACTLY matches a club named in the story (from or to). If the player's club
    can't be determined, or doesn't match, we do NOT show the photo — a
    silhouette is shown instead. This prevents pasting the wrong player's face
    (e.g. a Southampton photo on a West Ham move)."""
    if not player_el:
        return False
    cur = fpl_team_key(player_el, fpl)
    if cur is None:
        return False
    return cur == from_key or cur == to_key

def _load_crest(club_key, box=132):
    if not club_key:
        return None
    safe = club_key.replace(" ", "_").replace("'", "")
    p = Path(f"logos/{safe}.png")
    if not p.exists() and FPL_LOGO_IDS.get(safe):
        _download_asset(f"https://resources.premierleague.com/premierleague/badges/t{FPL_LOGO_IDS[safe]}.png", p)
    if p.exists():
        src = _safe_open_rgba(p)
        if src is not None:
            return _fit_contain(src, box, box)
    return None

def _injury_rows(story):
    """Build the (label, value) rows for the injury panel, in display order.
    Falls back to sensible defaults so the panel never looks empty."""
    rows = []
    if story.get("diagnosis"):
        rows.append(("DIAGNOSIS", story["diagnosis"]))
    if story.get("next_match"):
        rows.append(("MATCH", story["next_match"]))
    # availability: derive a short status from stage if not explicit
    stage = story.get("stage", 1)
    avail = {4: "Available / fit again", 3: "Ruled out", 2: "Doubt", 1: "To be assessed"}.get(stage)
    rows.append(("AVAILABILITY", avail))
    rows.append(("TIMELINE", story.get("expected_return") or "Awaiting update"))
    return rows[:4]

def create_injury_image(story, sources, filename):
    """Injury card in the style of the reference: full-bleed player photo on the
    right with a red wash, large serif name + medical cross top-left, red INJURY
    label, and a dark rounded panel of red-label detail rows. Degrades to a
    silhouette + flat red background when no verified photo is available."""
    W, H = 1380, 776
    fpl = fetch_fpl_data()
    player_el = find_player_in_fpl(story.get("player"), fpl)
    # Prefer the full name (e.g. "Bukayo Saka") so it stacks across two lines
    # like the reference card; fall back to the story's player or web_name.
    if player_el:
        full = f"{player_el.get('first_name','')} {player_el.get('second_name','')}".strip()
        player_name = full or player_el.get("web_name") or story.get("player") or "PLAYER"
    else:
        player_name = story.get("player") or "PLAYER"
    club_key = story.get("to_key") or story.get("from_key") or fpl_team_key(player_el, fpl)

    RED = (206, 22, 30)
    RED_BRIGHT = (227, 30, 36)
    WHITE = (255, 255, 255)
    DARK = (12, 14, 18)

    # face verified the same way as the main card
    # Strict: only show the photo if the player's FPL club matches the story club.
    face_verified = _photo_verified(player_el, fpl, club_key, club_key)

    img = Image.new("RGB", (W, H), DARK)

    # ── Right side: player photo, full-height, bleeding off the right edge ──
    photo_ok = False
    if player_el and face_verified:
        code = player_el["code"]
        pth = Path(f"players/{code}.png")
        if not pth.exists():
            _download_asset(f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{code}.png", pth)
        src = _safe_open_rgba(pth)
        if src is not None:
            # scale photo to roughly full card height, anchored bottom-right
            ph = ImageOps.contain(src, (int(W * 0.55), H), Image.Resampling.LANCZOS)
            img.paste(ph, (W - ph.width, H - ph.height), ph)
            photo_ok = True

    if not photo_ok:
        sil = _safe_open_rgba(Path("players/silhouette.png"))
        if sil is not None:
            sil = ImageOps.contain(sil, (int(W * 0.5), H), Image.Resampling.LANCZOS)
            img.paste(sil, (W - sil.width - 30, H - sil.height), sil)

    # ── Red atmospheric wash: dark on the left for text, red over the photo ──
    wash = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    wd = ImageDraw.Draw(wash)
    for x in range(W):
        # left → mostly opaque dark; right → translucent red tint
        if x < W * 0.45:
            a = 235
            wd.line([(x, 0), (x, H)], fill=(DARK[0], DARK[1], DARK[2], a))
        else:
            t = (x - W * 0.45) / (W * 0.55)
            a = int(150 * (1 - t))           # red fades toward the right edge
            wd.line([(x, 0), (x, H)], fill=(RED[0], RED[1], RED[2], max(40, a)))
    img.paste(wash, (0, 0), wash)
    # bottom + top darkening for legibility
    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(H):
        if y > H * 0.7:
            a = int(160 * ((y - H * 0.7) / (H * 0.3)))
            gd.line([(0, y), (W, y)], fill=(0, 0, 0, a))
    img.paste(grad, (0, 0), grad)

    draw = ImageDraw.Draw(img, "RGBA")

    # ── Club crest, top-right ──
    crest = _load_crest(club_key, 150)
    if crest is not None:
        img.paste(crest, (W - crest.width - 45, 40), crest)

    # ── Name (big, stacked, serif-style display) + medical cross ──
    TEXT_X = 55
    name_words = player_name.upper().split()
    if len(name_words) >= 2:
        line1, line2 = name_words[0], " ".join(name_words[1:])
    else:
        line1, line2 = player_name.upper(), ""
    nf = get_premium_font(118, "Black")
    # shrink if a single line is too wide for the left 60%
    maxw = int(W * 0.6)
    while draw.textlength(max(line1, line2, key=len), font=nf) > maxw and nf.size > 64:
        nf = get_premium_font(nf.size - 4, "Black")
    y = 70
    _draw_text_shadow(draw, (TEXT_X, y), line1, nf, WHITE, offset=3)
    name_h = nf.size + 6
    if line2:
        _draw_text_shadow(draw, (TEXT_X, y + name_h), line2, nf, WHITE, offset=3)
        name_bottom = y + name_h * 2
    else:
        name_bottom = y + name_h

    # medical cross to the right of the name block
    cross_cx = TEXT_X + int(draw.textlength(line1, font=nf)) + 120
    cross_cy = y + name_h
    arm = 70; thick = 46
    for box in ([cross_cx - thick // 2, cross_cy - arm, cross_cx + thick // 2, cross_cy + arm],
                [cross_cx - arm, cross_cy - thick // 2, cross_cx + arm, cross_cy + thick // 2]):
        draw.rounded_rectangle(box, radius=10, fill=RED_BRIGHT)

    # ── Red INJURY label under the name ──
    label_font = get_premium_font(52, "Black")
    draw.text((TEXT_X, name_bottom + 8), "INJURY", font=label_font, fill=RED_BRIGHT)
    panel_top = name_bottom + 8 + 64 + 18

    # ── Dark rounded translucent detail panel ──
    rows = _injury_rows(story)
    row_label_f = get_premium_font(34, "Black")
    row_val_f = get_premium_font(34, "Bold")
    head_f = get_premium_font(30, "Black")
    panel_w = int(W * 0.56)
    row_h = 64
    panel_h = 34 + 40 + len(rows) * row_h + 24
    panel = Image.new("RGBA", (panel_w, panel_h), (0, 0, 0, 0))
    ImageDraw.Draw(panel).rounded_rectangle([0, 0, panel_w, panel_h], radius=28, fill=(10, 12, 16, 222))
    img.paste(panel, (TEXT_X, panel_top), panel)
    pd = ImageDraw.Draw(img, "RGBA")

    px = TEXT_X + 34
    py = panel_top + 26
    pd.text((px, py), "INJURY", font=head_f, fill=RED_BRIGHT)
    py += 50
    for label, value in rows:
        # bullet
        pd.ellipse([px, py + 18, px + 12, py + 30], fill=WHITE)
        lx = px + 28
        pd.text((lx, py), f"{label}:", font=row_label_f, fill=RED_BRIGHT)
        lw = pd.textlength(f"{label}: ", font=row_label_f)
        val = value or ""
        # truncate overly long values to fit the panel
        while pd.textlength(val, font=row_val_f) > (panel_w - (lx - TEXT_X) - lw - 30) and len(val) > 4:
            val = val[:-2]
        if val != (value or ""):
            val = val.rstrip() + "…"
        pd.text((lx + lw, py), val, font=row_val_f, fill=WHITE)
        py += row_h

    # ── Bottom source strip ──
    src = "  ·  ".join(f"@{s}" for s in sources[:2])
    sf = get_premium_font(22, "Bold")
    sw = draw.textlength(f"Source: {src}  |  @FPLVortex", font=sf)
    draw.text((W - sw - 40, H - 40), f"Source: {src}  |  @FPLVortex", font=sf, fill=(220, 220, 220))

    img.save(filename)

def _draw_diagonal_accents(img, accent, gold=(212, 175, 55)):
    """Subtle diagonal corner stripes like the reference, on a dark bg."""
    W, H = img.size
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for i, x0 in enumerate(range(-40, 180, 36)):
        col = gold if i % 2 == 0 else accent
        d.polygon([(x0, 0), (x0 + 22, 0), (x0 + 22 - 90, H), (x0 - 90, H)], fill=col + (60,))
    for i, x0 in enumerate(range(W - 180, W + 40, 36)):
        col = gold if i % 2 == 0 else accent
        d.polygon([(x0, 0), (x0 + 22, 0), (x0 + 22 + 90, H), (x0 + 90, H)], fill=col + (60,))
    img.paste(ov, (0, 0), ov)

def create_transfer_image(story, sources, filename, collapsed=False):
    """FUT-style transfer card: dark navy bg with diagonal accents, big red
    heading, a light panel with PLAYER NAME / FROM..TO crests / PRICE / CONTRACT,
    and a framed player portrait on the right. Used for transfers, loans and
    collapsed deals (collapsed flips the heading + accent to red/grey)."""
    W, H = 1380, 776
    fpl = fetch_fpl_data()
    player_el = find_player_in_fpl(story.get("player"), fpl)
    player_name = (player_el["web_name"] if player_el else story.get("player")) or "PLAYER NAME"
    to_key = story.get("to_key")
    from_key = story.get("from_key")

    NAVY = (11, 18, 32)
    GOLD = (212, 175, 55)
    RED = (227, 30, 36)
    accent = (120, 30, 34) if collapsed else (30, 55, 110)
    head_col = (200, 30, 34) if collapsed else RED

    # Strict: only show the photo if the player's FPL club matches a story club.
    face_verified = _photo_verified(player_el, fpl, from_key, to_key)

    img = Image.new("RGB", (W, H), NAVY)
    # vertical sheen
    sheen = Image.new("L", (1, H), 0)
    for y in range(H):
        sheen.putpixel((0, y), int(30 * (1 - abs(y - H/2) / (H/2))))
    img.paste(Image.new("RGB", (W, H), (28, 40, 70)), (0, 0),
              sheen.resize((W, H)))
    _draw_diagonal_accents(img, accent, GOLD)
    draw = ImageDraw.Draw(img, "RGBA")

    # ── Red heading, top-left ──
    head_font = get_premium_font(72, "Black")
    heading = ["TRANSFER", "COLLAPSED"] if collapsed else ["TRANSFER", "UPDATE"]
    hy = 36
    for ln in heading:
        _draw_text_shadow(draw, (55, hy), ln, head_font, head_col, offset=3)
        hy += 78

    # ── Right: framed player portrait panel ──
    PANEL_X, PANEL_Y, PANEL_W, PANEL_H = W - 470, 40, 430, H - 80
    draw.rounded_rectangle([PANEL_X, PANEL_Y, PANEL_X + PANEL_W, PANEL_Y + PANEL_H],
                           radius=28, fill=(18, 28, 48), outline=GOLD, width=4)
    # inner stadium-ish gradient
    inner = Image.new("RGB", (PANEL_W - 24, PANEL_H - 24), (24, 36, 60))
    ig = ImageDraw.Draw(inner)
    for y in range(inner.height):
        c = int(20 + 30 * (y / inner.height))
        ig.line([(0, y), (inner.width, y)], fill=(c, c + 6, c + 16))
    img.paste(inner, (PANEL_X + 12, PANEL_Y + 12))
    portrait_ok = False
    if player_el and face_verified:
        code = player_el["code"]
        pth = Path(f"players/{code}.png")
        if not pth.exists():
            _download_asset(f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{code}.png", pth)
        src = _safe_open_rgba(pth)
        if src is not None:
            ph = ImageOps.contain(src, (PANEL_W - 40, PANEL_H - 40), Image.Resampling.LANCZOS)
            img.paste(ph, (PANEL_X + (PANEL_W - ph.width)//2, PANEL_Y + PANEL_H - ph.height - 12), ph)
            portrait_ok = True
    if not portrait_ok:
        sil = _safe_open_rgba(Path("players/silhouette.png"))
        if sil is not None:
            sil = ImageOps.contain(sil, (PANEL_W - 80, PANEL_H - 80), Image.Resampling.LANCZOS)
            img.paste(sil, (PANEL_X + (PANEL_W - sil.width)//2, PANEL_Y + PANEL_H - sil.height - 12), sil)

    # ── Left: light FUT-style detail panel ──
    LP_X, LP_Y, LP_W, LP_H = 45, 205, W - 470 - 45 - 40, H - 205 - 60
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([LP_X+8, LP_Y+10, LP_X+LP_W+8, LP_Y+LP_H+10],
                                             radius=30, fill=(0, 0, 0, 120))
    img.paste(shadow, (0, 0), shadow)
    # panel gradient (light silver)
    panel = Image.new("RGB", (LP_W, LP_H), (236, 238, 242))
    pg = ImageDraw.Draw(panel)
    for y in range(LP_H):
        c = int(246 - 28 * (y / LP_H))
        pg.line([(0, y), (LP_W, y)], fill=(c, c, c + 4))
    mask = Image.new("L", (LP_W, LP_H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, LP_W, LP_H], radius=30, fill=255)
    img.paste(panel, (LP_X, LP_Y), mask)
    # gold border
    draw.rounded_rectangle([LP_X, LP_Y, LP_X + LP_W, LP_Y + LP_H], radius=30, outline=GOLD, width=4)

    INK = (15, 17, 22)
    px = LP_X + 36
    # eyebrow
    eb = get_premium_font(24, "Bold")
    ebw = draw.textlength("TRANSFER UPDATE", font=eb)
    draw.text((LP_X + (LP_W - ebw)//2, LP_Y + 16), "TRANSFER UPDATE", font=eb, fill=(90, 95, 105))

    # PLAYER NAME (auto-fit)
    name_up = player_name.upper()
    nf = get_premium_font(72, "Black")
    while draw.textlength(name_up, font=nf) > (LP_W - 72) and nf.size > 36:
        nf = get_premium_font(nf.size - 3, "Black")
    name_y = LP_Y + 56
    draw.text((px, name_y), name_up, font=nf, fill=INK)
    y = name_y + nf.size + 18

    # Club row. Only show a directional FROM→TO when we're confident of the
    # direction; otherwise show a neutral "LINKED WITH [club]" to avoid the
    # reversed-clubs problem on ambiguous/multi-club tweets.
    direction_ok = bool(story.get("direction_confident")) and bool(from_key) and bool(to_key)
    fn = (story.get("from_club") or (from_key or "").replace("_", " ")).upper()
    tn = (story.get("to_club") or (to_key or "").replace("_", " ")).upper()

    if direction_ok:
        from_im = _load_crest(from_key, 64); to_im = _load_crest(to_key, 64)

        def _row_width(font, crest_px):
            w = font.getlength("FROM ")
            w += (crest_px + 8) if from_im is not None else 0
            w += font.getlength(fn + " TO ")
            w += (crest_px + 8) if to_im is not None else 0
            w += font.getlength(tn)
            return w

        row_f = get_premium_font(36, "Black")
        crest_px = 64
        while _row_width(row_f, crest_px) > (LP_W - 72) and row_f.size > 20:
            row_f = get_premium_font(row_f.size - 2, "Black")
            crest_px = max(40, int(row_f.size * 1.6))
        fi = _load_crest(from_key, crest_px); ti = _load_crest(to_key, crest_px)
        crest_top_off = max(0, (row_f.size - crest_px) // 2)

        cx = px
        draw.text((cx, y + 14), "FROM ", font=row_f, fill=INK); cx += row_f.getlength("FROM ")
        if fi is not None:
            img.paste(fi, (int(cx), y + 14 + crest_top_off), fi); cx += crest_px + 8
        draw.text((cx, y + 14), fn + " ", font=row_f, fill=INK); cx += row_f.getlength(fn + " ")
        draw.text((cx, y + 14), "TO ", font=row_f, fill=INK); cx += row_f.getlength("TO ")
        if ti is not None:
            img.paste(ti, (int(cx), y + 14 + crest_top_off), ti); cx += crest_px + 8
        draw.text((cx, y + 14), tn, font=row_f, fill=(20, 30, 90))
    else:
        # neutral: show the one club we're sure is involved (destination/link)
        link_key = to_key or from_key
        link_name = (story.get("to_club") or story.get("from_club")
                     or (link_key or "").replace("_", " ")).upper()
        link_im = _load_crest(link_key, 64)
        row_f = get_premium_font(36, "Black")
        label = "LINKED WITH "
        while (row_f.getlength(label) + 72 + row_f.getlength(link_name)) > (LP_W - 72) and row_f.size > 22:
            row_f = get_premium_font(row_f.size - 2, "Black")
        cx = px
        draw.text((cx, y + 14), label, font=row_f, fill=INK); cx += row_f.getlength(label)
        if link_im is not None:
            img.paste(link_im, (int(cx), y + 14), link_im); cx += 72
        draw.text((cx, y + 14), link_name, font=row_f, fill=(20, 30, 90))
    y += 96

    # divider
    draw.line([(px, y), (LP_X + LP_W - 36, y)], fill=(190, 165, 90), width=3)
    y += 22

    # PRICE row
    detail_f = get_premium_font(40, "Black")
    val_f = get_premium_font(38, "Bold")
    fee = story.get("fee") or "Undisclosed"
    _safe_emoji_text(img, (px, y), "💰", get_premium_font(40, "Bold"), GOLD)
    draw.text((px + 56, y), "PRICE: ", font=detail_f, fill=INK)
    pw = draw.textlength("PRICE: ", font=detail_f)
    draw.text((px + 56 + pw, y + 2), str(fee), font=val_f, fill=(20, 30, 90))
    y += 64

    # CONTRACT row
    _safe_emoji_text(img, (px, y), "📄", get_premium_font(40, "Bold"), GOLD)
    draw.text((px + 56, y), "CONTRACT: ", font=detail_f, fill=INK)
    cw = draw.textlength("CONTRACT: ", font=detail_f)
    contract = story.get("contract") or "Details to follow"
    cf = val_f
    while draw.textlength(str(contract), font=cf) > (LP_W - 36 - (px + 56 + cw - LP_X)) and cf.size > 22:
        cf = get_premium_font(cf.size - 2, "Bold")
    draw.text((px + 56 + cw, y + 2), str(contract), font=cf, fill=(20, 30, 90))
    y += 64

    # optional conditional line
    if story.get("conditional"):
        cond_f = get_premium_font(26, "Bold")
        cond = story["conditional"]
        while draw.textlength(cond, font=cond_f) > (LP_W - 72) and len(cond) > 8:
            cond = cond[:-2]
        if cond != story["conditional"]:
            cond = cond.rstrip() + "…"
        draw.text((px, y), cond, font=cond_f, fill=(120, 60, 30))

    # source strip bottom-left under panel
    sf = get_premium_font(22, "Bold")
    src = "  ·  ".join(f"@{s}" for s in sources[:2])
    draw.text((55, H - 34), f"Source: {src}  |  @FPLVortex", font=sf, fill=(200, 205, 215))

    img.save(filename)

def validate_story(story):
    """Enforce accuracy/consistency rules before a card is rendered.
    Returns (ok: bool, reason: str). On failure the caller should skip the post
    rather than render something wrong. This catches LLM/extraction mistakes:
    reversed clubs, duplicate from==to, manager data on a transfer, placeholder
    text, and empty required fields."""
    ev = story.get("event")
    player = (story.get("player") or "").strip()
    if not player:
        return False, "missing_player"

    # placeholder / template leftovers must never reach a card
    PLACEHOLDERS = ("player name", "example", "xxx", "[", "]", "tbd", "to follow",
                    "lorem", "duration & details", "updated heading", "from club", "to club")
    blob = " ".join(str(story.get(k, "") or "") for k in
                    ("player", "headline", "body", "from_club", "to_club", "fee",
                     "contract", "conditional", "diagnosis", "expected_return")).lower()
    for ph in PLACEHOLDERS:
        if ph in blob:
            return False, f"placeholder_text:{ph!r}"

    # the player name must not actually be a club (extraction slip)
    if looks_like_club(player):
        return False, "player_is_club"

    if ev in ("transfer", "loan", "loan_option"):
        fk = story.get("from_key"); tk = story.get("to_key")
        fc = (story.get("from_club") or "").strip().lower()
        tc = (story.get("to_club") or "").strip().lower()
        # duplicate clubs: from == to is incoherent
        if (fk and tk and fk == tk) or (fc and tc and fc == tc):
            return False, "from_equals_to"
        # a transfer card needs at least a destination to be meaningful
        if not (tk or story.get("to_club") or fk or story.get("from_club")):
            return False, "no_clubs"
        # manager/injury vocabulary leaking into a transfer story
        leak = (story.get("body", "") + " " + story.get("headline", "")).lower()
        if re.search(r'\b(head coach|sacked|appointed as manager|hamstring|ruled out for)\b', leak):
            return False, "event_data_mismatch"

    if ev == "manager" and not (story.get("to_key") or story.get("to_club")):
        return False, "manager_no_club"

    return True, "ok"

def create_image(story, sources, filename, rumour=False):
    # Injury stories get the dedicated red medical-card layout (unless it's an
    # unconfirmed rumour, which keeps the standard rumour styling).
    if story.get("event") == "injury" and not rumour and not story.get("collapsed"):
        try:
            create_injury_image(story, sources, filename)
            return
        except Exception as e:
            print(f"  [IMG] injury card failed, using standard card: {e}")
    # Transfers, loans and collapsed deals get the FUT-style card (rumours keep
    # the standard styling so the unconfirmed look is preserved).
    if (story.get("collapsed") or story.get("event") in ("transfer", "loan", "loan_option")) and not rumour:
        try:
            create_transfer_image(story, sources, filename, collapsed=bool(story.get("collapsed")))
            return
        except Exception as e:
            print(f"  [IMG] transfer card failed, using standard card: {e}")
    W, H = 1200, 675
    fpl = fetch_fpl_data()
    player_el = find_player_in_fpl(story.get("player"), fpl)
    player_name = (player_el["web_name"] if player_el else story.get("player")) or "PLAYER"
    to_key = story.get("to_key")
    from_key = story.get("from_key")
    ev = story["event"]
    collapsed = story.get("collapsed")
    GREEN = (40, 210, 90)

    # Strict: only show the photo if the player's FPL club matches a story club.
    face_verified = _photo_verified(player_el, fpl, from_key, to_key)

    stats = None
    player_img = Path("players/silhouette.png")
    if player_el:
        code = player_el["code"]
        stats = {"cost": f"£{player_el['now_cost']/10.0}m", "pts": str(player_el['total_points']),
                 "goals": str(player_el['goals_scored']), "assists": str(player_el['assists'])}
        if face_verified:
            player_img = Path(f"players/{code}.png")
            if not player_img.exists():
                _download_asset(f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{code}.png", player_img)

    bg_color = CLUB_COLORS.get(to_key, (25, 29, 38))
    accent = (255, 90, 0) if ev in ("transfer", "loan", "loan_option") else \
             (0, 163, 255) if ev == "manager" else (255, 0, 77) if ev == "injury" else (120, 200, 120)
    if collapsed:
        accent = (150, 80, 80)

    img = Image.new("RGB", (W, H), (14, 16, 21))
    draw = ImageDraw.Draw(img)
    draw.polygon([(W*0.52, 0), (W, 0), (W, H), (W*0.42, H)], fill=bg_color)

    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shade).polygon([(W*0.52, 0), (W, 0), (W, H), (W*0.42, H)], fill=(0, 0, 0, 70))
    grad = Image.new("L", (1, H), 0)
    for y in range(H):
        grad.putpixel((0, y), int(110 * (y / H)))
    grad = grad.resize((W, H))
    img.paste(shade, (0, 0), Image.composite(shade.split()[3], Image.new("L", (W, H), 0), grad))

    photo_ok = False
    if player_el and face_verified and player_img.exists():
        p_src = _safe_open_rgba(player_img)
        if p_src is not None:
            p_img = _fit_contain(p_src, 460, 460)
            shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            sh = p_img.split()[3].point(lambda a: int(a * 0.55))
            shadow.paste((0, 0, 0, 255), (W - 420 + 8, H - p_img.height - 6 + 8), sh)
            shadow = shadow.filter(ImageFilter.GaussianBlur(8))
            img.paste(shadow, (0, 0), shadow)
            img.paste(p_img, (W - 420, H - p_img.height - 6), p_img)
            photo_ok = True

    if not photo_ok:
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0)); od = ImageDraw.Draw(ov)
        cx, cyc, r = int(W * 0.78), int(H * 0.50), 150
        od.ellipse([cx - r, cyc - r, cx + r, cyc + r], fill=(0, 0, 0, 70))
        od.ellipse([cx - 52, cyc - 78, cx + 52, cyc + 26], fill=(255, 255, 255, 60))
        od.pieslice([cx - 95, cyc + 6, cx + 95, cyc + 210], 180, 360, fill=(255, 255, 255, 60))
        img.paste(ov, (0, 0), ov)
        ph = get_premium_font(34, "Black"); lab = "NO PHOTO"
        lw = od.textlength(lab, font=ph)
        ImageDraw.Draw(img).text((cx - lw/2, cyc + r - 6), lab, font=ph, fill=(255, 255, 255))

    TEXT_X = 60
    TEXT_MAX_W = int(W * 0.62) - TEXT_X
    draw.rectangle([0, 0, W, 12], fill=accent)

    brand = get_premium_font(60, "Black"); sub = get_premium_font(38, "Bold")
    crest_font = get_premium_font(34, "Black")
    draw.text((TEXT_X, 44), "FPL", font=brand, fill=(255, 255, 255))
    fpl_w = draw.textlength("FPL ", font=brand)
    draw.text((TEXT_X + fpl_w, 44), "VORTEX", font=brand, fill=accent)

    if rumour:
        badge_txt, badge_fill, badge_bg = "RUMOUR – NOT CONFIRMED", (255, 196, 0), (60, 45, 0)
    else:
        badge_txt = ("COLLAPSED" if collapsed else EVENT_PREFIX.get(ev, "UPDATE"))
        badge_fill, badge_bg = accent, (25, 28, 38)
    bw = int(draw.textlength(badge_txt, font=sub))
    draw.rounded_rectangle([TEXT_X, 138, TEXT_X + bw + 52, 206], radius=14, fill=badge_bg)
    draw.text((TEXT_X + 26, 152), badge_txt, font=sub, fill=badge_fill)

    # Player name — large, shadowed for readability, auto-shrinks to fit width.
    name_up = player_name.upper()
    nsize = 78
    while nsize >= 40 and draw.textlength(name_up, font=get_premium_font(nsize, "Black")) > TEXT_MAX_W:
        nsize -= 3
    nf = get_premium_font(nsize, "Black"); name_y = 236
    _draw_text_shadow(draw, (TEXT_X, name_y), name_up, nf, (255, 255, 255), offset=3)
    nb = draw.textbbox((0, 0), name_up, font=nf)
    name_bottom = name_y + (nb[3] - nb[1]) + 12

    CREST = 132
    from_im = _load_crest(from_key, CREST); to_im = _load_crest(to_key, CREST)
    row_y = name_bottom + 36; cy = row_y + CREST // 2; x = TEXT_X
    if from_im is not None:
        img.paste(from_im, (x, row_y + (CREST - from_im.height)//2), from_im)
        fn = (story.get("from_club") or from_key or "").replace("_", " ").upper()
        fnw = draw.textlength(fn, font=crest_font)
        _draw_text_shadow(draw, (x + (CREST - fnw)//2, row_y + CREST + 10), fn, crest_font, (235, 235, 235))
        x += CREST + 30
    if (from_im is not None) or (to_im is not None):
        _draw_arrow(draw, x, cy - 14, 150, GREEN, thick=28); x += 180
    if to_im is not None:
        img.paste(to_im, (x, row_y + (CREST - to_im.height)//2), to_im)
        tn = (story.get("to_club") or to_key or "").replace("_", " ").upper()
        tnw = draw.textlength(tn, font=crest_font)
        _draw_text_shadow(draw, (x + (CREST - tnw)//2, row_y + CREST + 10), tn, crest_font, (255, 255, 255))

    detail = build_detail_line(story)
    if detail:
        _safe_emoji_text(img, (TEXT_X, row_y + CREST + 56), detail.upper()[:60], sub, (160, 255, 120))

    # Bottom info bar
    draw.rectangle([0, H - 90, W, H - 12], fill=(20, 24, 33))
    draw.rectangle([0, H - 12, W, H], fill=accent)
    if stats:
        bar = (f"FPL COST: {stats['cost']}  |  POINTS: {stats['pts']}"
               f"  |  GOALS: {stats['goals']}  |  ASSISTS: {stats['assists']}")
        fill = (255, 255, 255)
    else:
        src = " · ".join(f"@{s}" for s in sources[:2])
        bar = f"Source: {src}  |  @FPLVortex"; fill = (190, 200, 220)
    bsize = 30; bf = get_premium_font(bsize, "Bold")
    while bsize > 18 and draw.textlength(bar, font=bf) > (W - 120):
        bsize -= 1; bf = get_premium_font(bsize, "Bold")
    bbox = draw.textbbox((0, 0), bar, font=bf)
    by = (H - 90) + (78 - (bbox[3]-bbox[1])) // 2 - bbox[1]
    draw.text((60, by), bar, font=bf, fill=fill)

    img.save(filename)

# ── QUEUE FILES ──────────────────────────────────────────────────────────────
def _slug(item):
    return re.sub(r'[^a-z0-9_]', '', item["key"]) + f"_s{item['stage']}"

def save_pending(item):
    with open(PENDING_DIR / f"{_slug(item)}.json", "w") as f:
        json.dump(item, f, indent=2, default=str)

def move_to_posted(item):
    src, dst = PENDING_DIR / f"{_slug(item)}.json", POSTED_DIR / f"{_slug(item)}.json"
    if src.exists():
        src.rename(dst)
    else:
        with open(dst, "w") as f:
            json.dump(item, f, indent=2, default=str)

# ── SCRAPER ──────────────────────────────────────────────────────────────────
def get_nitter_tweets(username):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; RSS reader)"}
    for inst in NITTER_INSTANCES:
        try:
            r = requests.get(f"{inst}/{username}/rss", headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            out = []
            for it in root.findall(".//item")[:8]:
                link, desc = it.find("link"), it.find("description")
                if link is None:
                    continue
                tid = link.text.strip().split("/")[-1].split("#")[0]
                text = re.sub(r'<[^>]+>', '', desc.text).strip() if desc is not None and desc.text else ""
                if tid and text:
                    out.append({"id": tid, "text": text})
            if out:
                return out
        except Exception:
            continue
    return []

async def get_twikit_tweets(read_client, username, count=20, retries=2):
    """Read latest tweets straight from X via twikit (primary source)."""
    if read_client is None:
        return []
    for attempt in range(retries):
        try:
            user = await read_client.get_user_by_screen_name(username)
            tweets = await read_client.get_user_tweets(user.id, "Tweets", count=count)
            out = []
            for t in tweets:
                txt = getattr(t, "full_text", None) or getattr(t, "text", "") or ""
                tid = str(getattr(t, "id", "") or "")
                if tid and txt:
                    out.append({"id": tid, "text": txt})
            return out
        except Exception as e:
            if attempt + 1 < retries:
                await asyncio.sleep(3 * (attempt + 1))
            else:
                print(f"  [READ] twikit failed for @{username}: {e}")
    return []

async def fetch_tweets(read_client, username):
    """twikit first; Nitter only as a fallback. Never raise — return []."""
    tweets = await get_twikit_tweets(read_client, username)
    if tweets:
        return tweets, "twikit"
    nit = get_nitter_tweets(username)
    return nit, ("nitter" if nit else "none")

async def scrape(data, read_client):
    fpl = fetch_fpl_data()
    story_map = {}
    seen = skipped = 0
    for username in JOURNALISTS:
        try:
            tweets, src = await fetch_tweets(read_client, username)
        except Exception as e:
            print(f"  [READ] @{username} error: {e}")
            tweets, src = [], "error"
        if not tweets and src in ("none", "error"):
            print(f"  [WARN] @{username}: ALL sources failed — X tokens may be expired or Nitter is down")
        else:
            print(f"  [READ] @{username}: {len(tweets)} tweets via {src}")

        for t in tweets:
            tid, text = t["id"], t["text"]
            if tid in data["posted_ids"]:
                continue
            if not any(k in text.lower() for k in FOOTBALL_KW):     # cheap pre-filter
                continue
            seen += 1

            # Extraction cache: each tweet hits the LLM at most ONCE, ever.
            if tid in data["extracted"]:
                story = dict(data["extracted"][tid])
            else:
                story = build_story(text, fpl)
                data["extracted"][tid] = dict(story)

            safe, why = passes_safety_gate(story, text, fpl)
            if not safe:
                skipped += 1
                print(f"    ⏭️  skip ({why}): {text[:70]!r}")
                continue

            valid, vwhy = validate_story(story)
            if not valid:
                skipped += 1
                print(f"    🚫 invalid ({vwhy}): {text[:70]!r}")
                continue

            anchor = story.get("to_key") or story.get("from_key") or "unknown"
            key = build_story_key(story["player"], anchor, story["event"])
            ok, reason = should_post(data, key, story["stage"], story["collapsed"])
            if not ok:
                print(f"    ⏭️  skip ({reason}): {key}")
                continue

            if key in story_map:
                ex = story_map[key]
                if username not in ex["sources"]:
                    ex["sources"].append(username)
                if story["stage"] > ex["stage"]:
                    ex.update({k: story[k] for k in story})
                    ex["sources"] = list(dict.fromkeys(ex["sources"]))
            else:
                prior = data.get("pending", {}).get(key, {}).get("sources", [])
                story.update({
                    "id": tid, "key": key, "text": text,
                    "sources": list(dict.fromkeys(prior + [username])), "reason": reason,
                })
                story_map[key] = story
        await asyncio.sleep(1)

    total_fetched = seen + skipped
    if total_fetched == 0:
        print("  [WARN] ⚠️ Zero football tweets from ALL journalists. X auth tokens likely "
              "expired — update X_AUTH_TOKEN and X_CT0_TOKEN secrets.")
    print(f"  [SCRAPE] {seen} football tweets seen, {skipped} skipped, {len(story_map)} candidate stories")

    ready = []
    for key, st in story_map.items():
        mode = classify_post(st, st["sources"])
        if mode is None:
            data["pending"][key] = {
                "sources": st["sources"], "player": st["player"],
                "to_key": st.get("to_key"), "event": st["event"],
                "last_seen": datetime.now(timezone.utc).isoformat(),
            }
            continue
        st["rumour"] = (mode == "rumour")
        data["pending"].pop(key, None)
        ready.append(st)

    # keep state files from growing unbounded
    if len(data["extracted"]) > 600:
        for k in list(data["extracted"].keys())[:-600]:
            del data["extracted"][k]
    if len(data["posted_ids"]) > 1500:
        data["posted_ids"] = data["posted_ids"][-1500:]

    save_data(data)
    return sorted(ready, key=lambda x: -(1 if x["collapsed"] else x["stage"]))

# ── PUBLISH ──────────────────────────────────────────────────────────────────
async def post_item(client, item, data):
    # Final accuracy gate: never render/post a story with reversed clubs,
    # duplicates, placeholder text, or mismatched event data.
    valid, why = validate_story(item)
    if not valid:
        print(f"  🚫 VALIDATION FAILED ({why}) — not posting: {item.get('player')!r}")
        # mark the tweet seen so we don't keep re-processing the bad story
        if item.get("id") and item["id"] not in data["posted_ids"]:
            data["posted_ids"].append(item["id"])
        save_data(data)
        return
    rumour = item.get("rumour", False)
    filename = "news_card.png"
    create_image(item, item["sources"], filename, rumour=rumour)
    media_id = await client.upload_media(filename, media_type="image/png")
    body = trim_for_twitter(build_tweet_body(item, item["sources"], rumour), limit=278)
    await client.create_tweet(text=body, media_ids=[media_id])
    if os.path.exists(filename):
        os.remove(filename)
    data["posted_ids"].append(item["id"])
    data["stories"][item["key"]] = {
        "stage": item["stage"], "player": item["player"],
        "to_key": item.get("to_key"), "event": item["event"],
        "status": "collapsed" if item["collapsed"] else "active",
        "sources": item["sources"], "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    increment_daily(data)
    save_data(data)
    move_to_posted(item)
    print(f"  ✅ Posted: {item['player']} — {item['event']} (stage {item['stage']})")

# ── MAIN ─────────────────────────────────────────────────────────────────────
async def main():
    print(f"\n[BOT] Run — {datetime.now(timezone.utc).isoformat()}  (LLM={'Gemini' if _GEMINI_OK else 'off/fallback'})")
    init_club_data()
    data = load_data()
    if not check_daily_limit(data):
        print("[BOT] Daily limit reached.")
        return

    read_client = None
    if X_AUTH_TOKEN and X_CT0_TOKEN:
        try:
            read_client = Client("en-US")
            read_client.set_cookies({"auth_token": X_AUTH_TOKEN, "ct0": X_CT0_TOKEN})
        except Exception as e:
            print(f"[READ] could not init twikit read client: {e}")
            read_client = None
    else:
        print("[READ] no read cookies set — using Nitter fallback only.")

    queue = await scrape(data, read_client)
    if not queue:
        print("[BOT] Quiet run. No new stories found.")
        return
    for item in queue:
        save_pending(item)

    if not (X_POST_AUTH_TOKEN and X_POST_CT0_TOKEN):
        print("[BOT] No post cookies set — cannot post. Set X_POST_AUTH_TOKEN and X_POST_CT0_TOKEN.")
        return

    client = Client("en-US")
    client.set_cookies({"auth_token": X_POST_AUTH_TOKEN, "ct0": X_POST_CT0_TOKEN})
    remaining = data["daily"]["limit"] - data["daily"]["count"]
    batch = queue[:max(0, min(3, remaining))]
    for i, item in enumerate(batch):
        try:
            await post_item(client, item, data)
        except Exception as e:
            print(f"  [ERROR] {item['key']} (attempt 1): {e} — retrying once")
            try:
                await asyncio.sleep(10)
                await post_item(client, item, data)
            except Exception as e2:
                print(f"  [ERROR] {item['key']} (attempt 2): {e2} — skipping")
        if i < len(batch) - 1:
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
