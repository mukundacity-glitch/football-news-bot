"""
FPL VORTEX — Transfer / Injury / Manager news bot for X.

Design goals (fixing the old version):
  1. THE BOT READS THE STORY. An extraction step turns each raw tweet into an
     accurate, structured story — real from/to clubs, fees, deadlines and
     conditions (e.g. "Barcelona must pay the £30m option by Thursday or he
     returns to Man Utd"). No more fixed templates inventing a "joins" sentence.
  2. CORRECT CLUB / KIT. The player photo is only ever used when the name match
     is strict, so we never paste a different (e.g. Aston Villa) player's photo
     onto the card. Crest + colours come from the real destination club.
  3. NOTHING TRUE IS SILENTLY DROPPED. Official/confirmed reports post even from
     a single mid-tier source; loan/stay/renewal stories post with accurate
     framing instead of being skipped or mislabelled.

Extraction uses Anthropic Haiku when ANTHROPIC_API_KEY is set; otherwise it
falls back to a truthful summary built from the tweet text itself.
"""
from clubs_cache import get_club_data
import os
import re
import json
import time
import asyncio
import requests
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter
from pilmoji import Pilmoji
from twikit import Client

# ── LLM (Google Gemini, free tier) ───────────────────────────────────────────
# Uses the CURRENT unified SDK: pip install google-genai  (the old
# google-generativeai package is deprecated). Key from aistudio.google.com,
# stored as GEMINI_API_KEY. If unset, the bot uses the truthful fallback.
GEMINI_MODEL = "gemini-2.0-flash"   # free tier; change to a newer Flash if you like
try:
    from google import genai
    _GEMINI_OK = bool(os.getenv("GEMINI_API_KEY"))
    _genai_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY")) if _GEMINI_OK else None
except Exception:
    _GEMINI_OK = False
    _genai_client = None

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
JOURNALISTS = [
    # Tier-1 global transfer journalists
    "FabrizioRomano", "David_Ornstein",
    # European transfer reporters
    "Plettigoal", "MatteoMoretto", "AlfredoPedulla", "DiMarzio",
    # PL-focused reporters
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

# ── CLUBS_CACHE WIRING (all leagues, not just PL) ────────────────────────────
# Populated once at startup from get_club_data(). Lets us (a) recognise every
# club name across Europe so a club is never mistaken for the player, (b) tag
# non-PL clubs correctly, and (c) gate the feed to FPL-relevant news.
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
    # plus our own PL aliases, for safety
    CLUB_NAME_SET |= set(CLUB_ALIASES.keys())

def looks_like_club(name: str) -> bool:
    """True if a candidate 'player' string is actually a known club (any league)."""
    if not name:
        return False
    n = name.lower().strip()
    if n in CLUB_NAME_SET or n in CLUB_ALIASES:
        return True
    # token check: 'real madrid', 'fc barcelona', etc.
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
 "stage": 1,            // 1=rumour/talks 2=agreement/advanced 3=signed 4=official/confirmed (or for injury: 1=concern 2=scan 3=ruled out 4=fit again)
 "collapsed": true/false,
 "headline": "<=10 word ENGLISH headline true to THIS exact story",
 "body": "1-2 factual ENGLISH sentences summarising THIS tweet, no filler, no hype template, no invented facts",
 "confidence": 0.0-1.0}}

Tweet:
\"\"\"{tweet}\"\"\""""

def extract_story_llm(tweet_text: str):
    if not _GEMINI_OK or _genai_client is None:
        return None
    for attempt in range(3):
        try:
            resp = _genai_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=_EXTRACT_PROMPT.format(tweet=tweet_text),
            )
            raw = resp.text or ""
            return json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        except Exception as e:
            m = str(e).lower()
            if any(s in m for s in ("429", "rate", "quota", "resource_exhausted", "exhausted")):
                time.sleep(6 * (attempt + 1))     # free-tier rate limit → back off + retry
                continue
            print(f"  [LLM] Gemini failed, using fallback: {e}")
            return None
    print("  [LLM] Gemini rate-limited after retries, using fallback")
    return None

def extract_story_fallback(tweet_text: str) -> dict:
    """No-LLM path: still TRUTHFUL — uses the tweet's own words as the body
    instead of a fabricated template. Crude club/stage guesses only."""
    tl = tweet_text.lower()
    clubs = []
    for alias in _SORTED_ALIASES:
        if re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', tl):
            k = CLUB_ALIASES[alias]
            if k not in clubs:
                clubs.append(k)
    #def has_word(words, text):
        return any(re.search(r'\b' + re.escape(w) + r'\b', text) for w in words)

    if has_word(["injury", "injured", "ruled out", "scan", "hamstring", "surgery", "doubt", "knock"], tl):
        event = "injury"
    elif (has_word(["appoint", "sack", "part company"], tl) or 
         (has_word(["manager", "head coach"], tl) and not has_word(["signing", "sign", "joins", "fee", "transfer", "bid"], tl))):
        event = "manager"
    elif has_word(["loan"], tl):
        event = "loan"
    elif has_word(["stay", "remain", "not for sale"], tl):
        event = "stay"
    else:
        event = "transfer"
        
    stage = 4 if has_word(["here we go", "official", "confirmed", "completed", "medical", "joins"], tl) else \
            2 if has_word(["agreement", "agreed", "advanced", "personal terms"], tl) else 1
    # player: first capitalised 2+ token name that is NOT a club (any league)
    # and not a header/filler word. Excludes "Real Madrid", "Excl", "Nothing"…
    FILLER = {"excl", "exclusive", "breaking", "official", "understand", "update",
              "here", "done", "deal", "medical", "nothing", "all", "source", "news"}
    name = None
    for m in re.findall(r'\b([A-Z][a-zà-ÿ]+(?:[-\' ][A-Z][a-zà-ÿ]+)+)\b', tweet_text):
        low = m.lower()
        if looks_like_club(m):
            continue
        if any(w in FILLER for w in low.split()):
            continue
        name = m
        break
    clean = re.sub(r'\s+', ' ', tweet_text).strip()
    to_key = clubs[-1] if clubs else None
    from_key = clubs[0] if len(clubs) >= 2 else None
    return {
        "is_football": True, "event": event, "is_real_move": event in ("transfer", "loan", "loan_option"),
        "player": name,
        "from_club": (from_key.replace("_", " ") if from_key else None),
        "to_club": (to_key.replace("_", " ") if to_key else None),
        "from_key": from_key, "to_key": to_key,
        "fee": None, "contract": None, "conditional": None,
        "stage": stage, "collapsed": any(re.search(r'\b' + w + r'\b', tl) for w in ["collapsed", "called off", "rejected", "deal off"]),
        "headline": (name + " — update") if name else "Transfer update",
        "body": clean[:240], "confidence": 0.5,
    }

def build_story(tweet_text: str) -> dict:
    s = extract_story_llm(tweet_text) or extract_story_fallback(tweet_text)
    # normalise + resolve PL keys (don't overwrite keys the fallback already set)
    s["from_key"] = s.get("from_key") or resolve_club_key(s.get("from_club"))
    s["to_key"]   = s.get("to_key") or resolve_club_key(s.get("to_club"))
    try:
        s["stage"] = max(1, min(4, int(s.get("stage", 1))))
    except Exception:
        s["stage"] = 1
    s["collapsed"] = bool(s.get("collapsed"))
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
            return data
    except Exception:
        return None

def find_player_in_fpl(player_name, data):
    """STRICT match. Returns an element only when the name clearly lines up, so
    we never put a different player's photo/kit on the card."""
    if not data or not player_name:
        return None
    q = player_name.lower().strip()
    tokens = [t for t in re.split(r'[\s\-]+', q) if t]
    if not tokens:
        return None
    for el in data.get("elements", []):
        web = el["web_name"].lower()
        full = (el["first_name"] + " " + el["second_name"]).lower()
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
    # group loan/stay/renewal/loan_option under "transfer" family so the same
    # player doesn't get two separate active stories for related events
    fam = "injury" if event == "injury" else "manager" if event == "manager" else "transfer"
    return f"{p}_{c}_{fam}"

def should_post(data, key, new_stage, collapsed):
    existing = data["stories"].get(key)
    if collapsed:
        if existing and existing["status"] == "active":
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
    # off-topic content that names clubs but isn't transfer/injury/manager news
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
        if not (story.get("to_key") or story.get("to_club")):
            return False, "manager_no_club"
        return True, "ok_manager"
    if story["event"] == "injury":
        # injuries must be a real PL (FPL) player to be on-topic
        if find_player_in_fpl(story["player"], fpl_data) is None:
            return False, "injury_player_not_in_fpl"
        return True, "ok_injury"
    # transfer / loan / stay / renewal — must be FPL-relevant:
    #   a current FPL player, OR a Premier League club on either side.
    pl_player = find_player_in_fpl(story["player"], fpl_data) is not None
    pl_club = bool(story.get("to_key") or story.get("from_key"))
    if not pl_club:
        # also check clubs_cache PL names for clubs we render no crest for
        for nm in (story.get("to_club"), story.get("from_club")):
            if nm and nm.lower() in PL_CLUB_NAMES:
                pl_club = True
                break
    if not pl_player and not pl_club:
        return False, "not_fpl_relevant"   # e.g. Heidenheim<->Hoffenheim, Real/Real
    return True, "ok"

def classify_post(story, sources):
    """'confirmed' -> post as fact | 'rumour' -> labelled unconfirmed | None -> hold."""
    if story.get("collapsed"):
        return "confirmed"
    if story["event"] in ("manager", "injury", "stay", "renewal", "loan_option"):
        return "confirmed"  # factual reports; framed accurately, not as "joins"
    tl = story.get("body", "").lower() + " " + " ".join(story.get(k, "") or "" for k in ("headline",)).lower()
    strong = story["stage"] >= 4 or any(re.search(r'\b' + w + r'\b', tl) for w in STRONG_OFFICIAL)
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

SEO_HASHTAGS = ["#FPL", "#FantasyPL", "#PremierLeague", "#FootballTwitter"]

def build_seo_hashtags(story, max_tags=3):
    """Short, search-friendly tag set for card captions — separate from the
    longer club/event tag block build_hashtags() already produces for the tweet body."""
    tags = []
    ev = story.get("event")
    if ev in ("transfer", "loan", "loan_option"):
        tags.append("#TransferNews")
    elif ev == "injury":
        tags.append("#InjuryUpdate")
    elif ev == "manager":
        tags.append("#FootballManager")
    for key, name in ((story.get("to_key"), story.get("to_club")),
                       (story.get("from_key"), story.get("from_club"))):
        ht = hashtag_for(key) or hashtag_for(name)
        if ht and ht not in tags:
            tags.append(ht)
            break
    tags += [t for t in SEO_HASHTAGS if t not in tags]
    return " ".join(tags[:max_tags])

def build_hashtags(story):
    ev = story["event"]
    base = "#TransferNews" if ev in ("transfer", "loan", "loan_option") else \
           "#ManagerNews" if ev == "manager" else "#InjuryNews" if ev == "injury" else "#FootballNews"
    tags = [base, "#Football"]
    # PL side via clean map; non-PL side (Barcelona, Real Madrid…) via clubs_cache
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

    # 1. Headline Format (Only removes "- update" if it is at the end of the phrase)
    raw_head = story.get("headline") or story.get("player") or "Transfer"
    head = re.sub(r'(?i)[\s\-—]*update\s*$', '', raw_head).strip()
    
    lines = [f"🚨 {prefix} | {head}", ""]

    # 2. Post Content (Description) - Force strip all URLs (pic.x.com, t.co, etc.)
    raw_body = story.get("body") or ""
    clean_body = re.sub(r'https?://\S+|www\.\S+|pic\.x\.com/\S+', '', raw_body).strip()
    
    if clean_body:
        lines.append(f"🚨 {clean_body}")

    if story.get("conditional"):
        lines.append(f"📌 {story['conditional']}")

    # 3. Stacked format for Fee and Contract
    details = []
    if story.get("fee"):
        details.append(f"💰 Fee: {story['fee']}")
    if story.get("contract"):
        details.append(f"📝 Contract: {story['contract']}")

    if details:
        lines.append("\n".join(details))

    body = "\n\n".join(p for p in lines if p.strip() != "")
    
    if rumour:
        body = "⚠️ RUMOUR (UNCONFIRMED)\n" + body

    # 4. Append the full Hashtags at the very end
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
PALETTES = {
    'INJURY':              {'accent': (220, 38, 38),  'text': (255, 255, 255), 'bg': (18, 0, 0),   'swoosh': (180, 20, 20)},
    'TRANSFER_ONGOING':     {'accent': (200, 160, 40), 'text': (200, 160, 40), 'bg': (10, 20, 35),  'swoosh': (200, 160, 40)},
    'TRANSFER_CONFIRMED':   {'accent': (255, 215, 0),  'text': (255, 255, 255), 'bg': (15, 15, 15), 'swoosh': (255, 215, 0)},
    'TRANSFER_RUMOUR':      {'accent': (255, 196, 0),  'text': (255, 196, 0),  'bg': (40, 30, 0),   'swoosh': (200, 150, 0)},
    'TRANSFER_COLLAPSED':   {'accent': (153, 27, 40),  'text': (220, 220, 220), 'bg': (25, 25, 25), 'swoosh': (100, 15, 20)},
    'MANAGER_UPDATE':       {'accent': (192, 192, 192), 'text': (255, 255, 255), 'bg': (0, 10, 30), 'swoosh': (150, 150, 170)},
}
_FONT_CACHE = {}
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

def _classify_post_type(story, rumour):
    if story.get("collapsed"):
        return "TRANSFER_COLLAPSED"
    if rumour:
        return "TRANSFER_RUMOUR"
    ev = story.get("event")
    if ev == "injury":
        return "INJURY"
    if ev == "manager":
        return "MANAGER_UPDATE"
    if ev in ("transfer", "loan", "loan_option") and story.get("stage", 1) >= 4:
        return "TRANSFER_CONFIRMED"
    return "TRANSFER_ONGOING"

def create_image(story, sources, filename, rumour=False):
    W, H = 1200, 675
    fpl = fetch_fpl_data()
    player_el = find_player_in_fpl(story.get("player"), fpl)
    to_key, from_key = story.get("to_key"), story.get("from_key")

    face_verified = False
    if player_el:
        cur = fpl_team_key(player_el, fpl)
        if cur is None or cur == from_key or cur == to_key or (not from_key and not to_key):
            face_verified = True

    post_type = _classify_post_type(story, rumour)
    
    # Use premium transfer-news colors
    theme = {
        'bg': (10, 20, 35),           # Dark navy background
        'card_bg': (15, 25, 45),      # Contrast card
        'text': (255, 255, 255),      # White primary text
        'accent': (255, 215, 0),      # Gold accents
        'alert': (220, 38, 38),       # Red alert icon base
        'fee': (34, 197, 94),         # Green for fee
        'contract': (59, 130, 246)    # Blue for contract
    }

    club_key = to_key or from_key
    player_name = (player_el["web_name"] if player_el else story.get("player")) or "UPDATE"
    seo_tags = build_seo_hashtags(story)
    
    # Map FPL element type to Position String
    pos_str = "PLAYER"
    if player_el:
        type_map = {1: "GOALKEEPER", 2: "DEFENDER", 3: "MIDFIELDER", 4: "FORWARD"}
        pos_str = type_map.get(player_el.get("element_type"), "PLAYER")
    elif post_type == "MANAGER_UPDATE":
        pos_str = "HEAD COACH"

    img = Image.new("RGB", (W, H), theme['bg'])
    draw = ImageDraw.Draw(img, "RGBA")

    # Layout Setup: Left = 0 to 600 | Right = 600 to 1200
    draw.rectangle([600, 0, W, H], fill=theme['card_bg'])

    # === LEFT SIDE: IMAGE, POS, LOGO ===
    p_img_loaded = False
    if player_el and face_verified:
        code = player_el["code"]
        player_img = Path(f"players/{code}.png")
        if not player_img.exists():
            _download_asset(f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{code}.png", player_img)
        if player_img.exists():
            p_src = _safe_open_rgba(player_img)
            if p_src is not None:
                p_img = _fit_contain(p_src, 600, 600)
                img.paste(p_img, (0, H - p_img.height), p_img)
                p_img_loaded = True

    if not p_img_loaded:
        sil = _safe_open_rgba(Path("players/silhouette.png"))
        if sil is not None:
            sil = _fit_contain(sil, 500, 500)
            img.paste(sil, (50, H - sil.height), sil)

    # Left Side Typography
    draw.text((40, 40), player_name.upper(), font=get_premium_font(54, "Black"), fill=theme['text'])
    draw.text((40, 100), pos_str, font=get_premium_font(28, "Bold"), fill=theme['accent'])

    # Left Side Logo Placement
    crest = _load_crest(club_key, 120)
    if crest is not None:
        img.paste(crest, (440, 40), crest)

    # Center Separator Line
    draw.line([(600, 0), (600, H)], fill=theme['accent'], width=4)

    # === RIGHT SIDE: INFO CARD ===
    TEXT_X = 640
    head_text = "🚨 TRANSFER NEWS" if not story.get("collapsed") else "🚨 COLLAPSED DEAL"
    if post_type == "INJURY": head_text = "🚨 INJURY UPDATE"
    elif post_type == "MANAGER_UPDATE": head_text = "🚨 MANAGER UPDATE"

    with Pilmoji(img) as pj:
        pj.text((TEXT_X, 50), head_text, font=get_premium_font(36, "Black"), fill=theme['accent'])

    body_font = get_premium_font(24, "Bold")
    y = 130
    sstatus_icon = "❌" if story.get("collapsed") else ("⚠️" if rumour else "✅")
    raw_details = story.get("body") or ""
    details = re.sub(r'https?://\S+|www\.\S+|pic\.x\.com/\S+', '', raw_details).strip()

    with Pilmoji(img) as pj:
        words, wrapped, cur = details.split(), [], ""
        for w in words:
            if pj.getsize(cur + w, font=body_font)[0] <= 500:
                cur += w + " "
            else:
                wrapped.append(cur)
                cur = w + " "
        wrapped.append(cur)

        pj.text((TEXT_X, y), f"{status_icon} {wrapped[0]}", font=body_font, fill=theme['text'])
        y += 35
        for wl in wrapped[1:]:
            if wl.strip():
                pj.text((TEXT_X + 40, y), wl, font=body_font, fill=theme['text'])
                y += 35
        y += 40

        if story.get("fee"):
            pj.text((TEXT_X, y), f"💰 Fee: {story['fee']}", font=get_premium_font(26, "Black"), fill=theme['fee'])
            y += 45
        if story.get("contract"):
            pj.text((TEXT_X, y), f"📝 Contract: {story['contract']}", font=get_premium_font(26, "Black"), fill=theme['contract'])
            y += 45
        if story.get("conditional"):
            pj.text((TEXT_X, y), f"📌 {story['conditional']}", font=body_font, fill=theme['accent'])

    # Branding & Watermark
    brand_font = get_premium_font(18, "Bold")
    draw.text((TEXT_X, H - 40), "FPL_VORTEX", font=brand_font, fill=(150, 150, 150))

    # SEO Tags / Source
    src = " · ".join(f"@{s}" for s in sources[:2])
    sw = draw.textlength(src, font=brand_font)
    draw.text((W - sw - 40, H - 40), src, font=brand_font, fill=(150, 150, 150))

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
    """Read latest tweets straight from X via twikit (primary source).
    Returns [] on failure so the caller can fall back to Nitter."""
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
                await asyncio.sleep(3 * (attempt + 1))     # backoff then retry
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
        print(f"  [READ] @{username}: {len(tweets)} tweets via {src}")
        for t in tweets:
            tid, text = t["id"], t["text"]
            if tid in data["posted_ids"]:
                continue
            if not any(k in text.lower() for k in FOOTBALL_KW):     # cheap pre-filter
                continue
            seen += 1

            # Extraction cache: each tweet is sent to the LLM ONCE, ever.
            # Re-runs reuse the stored result → stays inside Gemini's free quota.
            if tid in data["extracted"]:
                story = dict(data["extracted"][tid])
            else:
                story = build_story(text)                            # READ the story (LLM if new)
                data["extracted"][tid] = dict(story)
                if _GEMINI_OK:
                    time.sleep(4)        # ~15 RPM free tier → 4s gap keeps us safely under
            safe, why = passes_safety_gate(story, text, fpl)
            if not safe:
                skipped += 1
                print(f"    ⏭️  skip ({why}): {text[:70]!r}")
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
                    ex.update({k: story[k] for k in story})  # keep most advanced
                    ex["sources"] = list(dict.fromkeys(ex["sources"]))
            else:
                prior = data.get("pending", {}).get(key, {}).get("sources", [])
                story.update({
                    "id": tid, "key": key, "text": text,
                    "sources": list(dict.fromkeys(prior + [username])), "reason": reason,
                })
                story_map[key] = story
        await asyncio.sleep(1)

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
    init_club_data()             # load all-league club universe + hashtags + PL set
    data = load_data()
    if not check_daily_limit(data):
        print("[BOT] Daily limit reached.")
        return

    # READ client (twikit) — primary source, replaces Nitter. Falls back to
    # Nitter automatically if cookies are missing or X reading fails.
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
