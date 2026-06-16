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
import asyncio
import requests
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter
from pilmoji import Pilmoji
from twikit import Client

try:
    import anthropic
    _ANTHROPIC_OK = bool(os.getenv("ANTHROPIC_API_KEY"))
except Exception:
    _ANTHROPIC_OK = False

# ── SECRETS ──────────────────────────────────────────────────────────────────
X_POST_AUTH_TOKEN = os.getenv("X_POST_AUTH_TOKEN")
X_POST_CT0_TOKEN  = os.getenv("X_POST_CT0_TOKEN")
X_AUTH_TOKEN      = os.getenv("X_AUTH_TOKEN")   # read account (twikit reader)
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
    "FabrizioRomano", "David_Ornstein", "Plettigoal", "Santi_J_M",
    "sistoney67", "MatteoMoretto_", "AlfredoPedulla", "cfalk_news",
    "BenJacobs", "GianlucaDiMarzio",
    "_pauljoyce", "SamiMokbel1_DM", "JamesPearceLFC", "mcgrathmike",
    "SkySportsNews",
]

NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]

# Tier-1: their official word is trusted to post alone.
TOP_SOURCES = {"FabrizioRomano", "David_Ornstein"}

# ── LIGHT PRE-FILTER ONLY ────────────────────────────────────────────────────
FOOTBALL_KW = [
    "transfer", "sign", "deal", "fee", "bid", "loan", "contract", "agree",
    "medical", "official", "here we go", "talks", "joins", "move", "target",
    "injury", "injured", "ruled out", "scan", "hamstring", "surgery", "doubt",
    "sack", "appoint", "manager", "head coach", "stay", "return", "recall",
]

STAFF_BLOCK_KW = [
    "head of recruitment", "sporting director", "director of football",
    "technical director", "chief scout", "scouting", "ceo", "chairman",
    "owner", "president", "kit man", "head of football",
    "transfer chief", "negotiator", "team doctor", "physio", 
    "doctor", "medical", "medic", "surgeon", "physician", 
    "head of medical", "club doctor", "physiotherapist"
]

NATIONALITY_DESCRIPTORS = {
    "northern irishman", "irishman", "scotsman", "welshman", "englishman",
    "frenchman", "spaniard", "german", "italian", "portuguese", "brazilian",
    "argentinian", "dutch", "belgian", "norwegian", "danish", "swedish",
    "swiss", "austrian", "croatian", "senegalese", "ghanaian", "nigerian",
    "jamaican", "colombian", "uruguayan", "chilean", "mexican", "american",
    "japanese", "south korean", "australian", "canadian", "algerian",
    "moroccan", "ivorian", "ecuadorian", "paraguayan", "dutchman"
}

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
    "Arsenal": "3",       "Aston_Villa": "7",   "Bournemouth": "91",  "Brentford": "94",
    "Brighton": "36",     "Chelsea": "8",        "Crystal_Palace": "31","Everton": "11",
    "Fulham": "54",       "Ipswich": "40",       "Leicester": "13",    "Liverpool": "14",
    "Man_City": "43",     "Man_Utd": "1",        "Newcastle": "4",     "Nottm_Forest": "17",
    "Southampton": "20",  "Spurs": "6",          "West_Ham": "21",     "Wolves": "39",
}

CLUB_COLORS = {
    "Arsenal": (239, 1, 7),        "Aston_Villa": (103, 14, 54),   "Bournemouth": (181, 14, 18),
    "Brentford": (227, 6, 19),     "Brighton": (0, 87, 184),        "Chelsea": (3, 70, 148),
    "Crystal_Palace": (27, 69, 143),"Everton": (39, 68, 136),      "Fulham": (15, 15, 15),
    "Ipswich": (0, 0, 255),        "Leicester": (0, 83, 160),       "Liverpool": (200, 16, 46),
    "Man_City": (108, 173, 223),   "Man_Utd": (218, 41, 28),        "Newcastle": (15, 15, 15),
    "Nottm_Forest": (229, 50, 51), "Southampton": (215, 25, 32),    "Spurs": (17, 24, 38),
    "West_Ham": (122, 38, 58),     "Wolves": (253, 185, 19),
}

CLUB_HASHTAG_MAP = {
    "Arsenal": "#Arsenal",       "Aston_Villa": "#AVFC",    "Bournemouth": "#AFCB",
    "Brentford": "#Brentford",   "Brighton": "#BHAFC",      "Chelsea": "#Chelsea",
    "Crystal_Palace": "#CPFC",   "Everton": "#EFC",         "Fulham": "#FFC",
    "Ipswich": "#ITFC",          "Leicester": "#LCFC",      "Liverpool": "#LFC",
    "Man_City": "#MCFC",         "Man_Utd": "#MUFC",        "Newcastle": "#NUFC",
    "Nottm_Forest": "#NFFC",     "Southampton": "#SaintsFC","Spurs": "#THFC",
    "West_Ham": "#WHUFC",        "Wolves": "#Wolves",
}

def resolve_club_key(name: str):
    if not name:
        return None
    n = name.lower()
    for alias in _SORTED_ALIASES:
        if re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', n):
            return CLUB_ALIASES[alias]
    return None

# ── CLUBS_CACHE WIRING ───────────────────────────────────────────────────────
CLUB_NAME_SET  = set()
CLUB_HASHTAGS  = {}
PL_CLUB_NAMES  = set()

def init_club_data():
    global CLUB_NAME_SET, CLUB_HASHTAGS, PL_CLUB_NAMES
    try:
        d = get_club_data()
    except Exception as e:
        print(f"[CLUBS] get_club_data failed: {e}")
        return
    CLUB_HASHTAGS  = d.get("club_hashtags", {}) or {}
    PL_CLUB_NAMES  = set(d.get("pl_clubs", []) or [])
    CLUB_NAME_SET  = set(CLUB_HASHTAGS.keys()) | set((d.get("short_names", {}) or {}).keys())
    CLUB_NAME_SET |= set(CLUB_ALIASES.keys())

def looks_like_club(name: str) -> bool:
    if not name:
        return False
    n = name.lower().strip()
    if n in CLUB_NAME_SET or n in CLUB_ALIASES:
        return True
    return any(n == c or c in n for c in CLUB_NAME_SET if len(c) >= 5)

def hashtag_for(name_or_key: str):
    if not name_or_key:
        return None
    key = name_or_key
    if key in CLUB_HASHTAG_MAP:
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
    d.setdefault("daily",      fresh["daily"])
    d.setdefault("stories",    {})
    d.setdefault("posted_ids", [])
    d.setdefault("pending",    {})
    return d

def save_data(data: dict):
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

# ── STORY EXTRACTION ─────────────────────────────────────────────────────────
_EXTRACT_PROMPT = """You are a football transfer-desk editor. Read this reporter tweet and extract ONLY what it actually states. Do NOT invent, assume, or generalise. If the tweet is conditional (deadlines, options, "if X then Y"), capture that exactly.

CRITICAL DIRECTION RULE — read carefully:
- "from_club" = the club the player is CURRENTLY at / LEAVING (the selling club)
- "to_club"   = the club the player is GOING TO / JOINING (the buying/destination club)
- If a club is making a BID or SIGNING a player, that club is ALWAYS "to_club"
- The player's CURRENT club is ALWAYS "from_club"
- Example: "Man City bid to sign Anderson from Nottm Forest" → from_club=Nottm Forest, to_club=Man City
- Example: "Arsenal sign player from Aston Villa"            → from_club=Aston Villa,   to_club=Arsenal
- Example: "Barcelona want Real Madrid's Bellingham"         → from_club=Real Madrid,   to_club=Barcelona
- If only ONE club is mentioned (e.g. "Man City close to signing X") → to_club=Man City, from_club=null

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
"stage": 1,
"collapsed": true/false,
"headline": "<=10 word ENGLISH headline true to THIS exact story",
"body": "1-2 factual ENGLISH sentences summarising THIS tweet, no filler, no hype template, no invented facts",
"confidence": 0.0-1.0}}

Tweet:
\"\"\"{tweet}\"\"\""""

def extract_story_llm(tweet_text: str):
    if not _ANTHROPIC_OK:
        return None
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": _EXTRACT_PROMPT.format(tweet=tweet_text)}],
        )
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception as e:
        print(f"  [LLM] extraction failed, using fallback: {e}")
        return None

def extract_story_fallback(tweet_text: str) -> dict:
    tl = tweet_text.lower()
    clubs = []
    for alias in _SORTED_ALIASES:
        if re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', tl):
            k = CLUB_ALIASES[alias]
            if k not in clubs:
                clubs.append(k)

    if any(w in tl for w in ["injury", "injured", "ruled out", "scan", "hamstring", "surgery", "doubt", "knock"]):
        event = "injury"
    elif any(w in tl for w in ["appoint", "manager", "head coach", "sack"]):
        event = "manager"
    elif any(w in tl for w in ["loan"]):
        event = "loan"
    elif any(w in tl for w in ["stay", "remain", "not for sale"]):
        event = "stay"
    else:
        event = "transfer"

    stage = 4 if any(w in tl for w in ["here we go", "official", "confirmed", "completed", "medical", "joins"]) else \
            2 if any(w in tl for w in ["agreement", "agreed", "advanced", "personal terms"]) else 1

    FILLER = {"excl", "exclusive", "breaking", "official", "understand", "update",
              "here", "done", "deal", "medical", "nothing", "all", "source", "news"}

    name = None
    for m in re.findall(r'\b([A-Z][a-zà-ÿ]+(?:[-\' ][A-Z][a-zà-ÿ]+)+)\b', tweet_text):
        low = m.lower().strip()
        if looks_like_club(m):
            continue
        if any(w in FILLER for w in low.split()):
            continue
        if low in NATIONALITY_DESCRIPTORS or any(dem in low for dem in NATIONALITY_DESCRIPTORS):
            continue
        name = m
        break

    clean = re.sub(r'\s+', ' ', tweet_text).strip()

    from_key = None
    to_key   = None
    if clubs:
        from_match = None
        for alias in _SORTED_ALIASES:
            pattern = r'\bfrom\s+' + re.escape(alias) + r'\b'
            if re.search(pattern, tl):
                from_match = CLUB_ALIASES[alias]
                break
        if from_match and from_match in clubs:
            from_key  = from_match
            remaining = [c for c in clubs if c != from_key]
            to_key    = remaining[0] if remaining else None
        else:
            to_key   = clubs[0]
            from_key = clubs[-1] if len(clubs) >= 2 else None

    return {
        "is_football": True, "event": event, "is_real_move": event in ("transfer", "loan", "loan_option"),
        "player": name,
        "from_club": (from_key.replace("_", " ") if from_key else None),
        "to_club":   (to_key.replace("_",   " ") if to_key   else None),
        "from_key": from_key, "to_key": to_key,
        "fee": None, "contract": None, "conditional": None,
        "stage": stage, "collapsed": any(w in tl for w in ["collapsed", "off", "called off", "rejected"]),
        "headline": (name + " — update") if name else "Transfer update",
        "body": clean[:240], "confidence": 0.5,
    }

def build_story(tweet_text: str) -> dict:
    s = extract_story_llm(tweet_text) or extract_story_fallback(tweet_text)

    s["from_key"] = s.get("from_key") or resolve_club_key(s.get("from_club"))
    s["to_key"]   = s.get("to_key")   or resolve_club_key(s.get("to_club"))

    if s.get("player"):
        p_low = s["player"].lower().strip()
        if p_low in NATIONALITY_DESCRIPTORS or any(nd in p_low for nd in ["irishman", "englishman", "scotsman", "welshman", "frenchman", "dutchman", "spaniard"]):
            s["player"] = None

    if s.get("from_key") and s.get("to_key") and s.get("player"):
        _fpl = fetch_fpl_data()
        _el  = find_player_in_fpl(s["player"], _fpl)
        if _el:
            cur = fpl_team_key(_el, _fpl)
            if cur and cur == s["to_key"] and cur != s["from_key"]:
                s["from_key"], s["to_key"] = s["to_key"], s["from_key"]
                temp_club = s.get("from_club")
                s["from_club"] = s.get("to_club")
                s["to_club"] = temp_club

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
    if not data or not player_name:
        return None
    q      = player_name.lower().strip()
    tokens = [t for t in re.split(r'[\s\-]+', q) if t]
    if not tokens:
        return None
    for el in data.get("elements", []):
        web  = el["web_name"].lower()
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
    return el.get("now_cost", 0) >= 65 or el.get("total_points", 0) >= 90

def fpl_team_key(el, fpl_data):
    if not el or not fpl_data:
        return None
    for t in fpl_data.get("teams", []):
        if t.get("id") == el.get("team"):
            return resolve_club_key((t.get("name", "") + " " + t.get("short_name", "")).lower())
    return None

# ── DEDUP / PROGRESSION ──────────────────────────────────────────────────────
def build_story_key(player, club_key, event) -> str:
    p   = (player    or "unknown").lower().replace(" ", "_")
    c   = (club_key  or "unknown").lower()
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
        if not (story.get("to_key") or story.get("to_club")):
            return False, "manager_no_club"
        return True, "ok_manager"
    if story["event"] == "injury":
        if find_player_in_fpl(story["player"], fpl_data) is None:
            return False, "injury_player_not_in_fpl"
        return True, "ok_injury"
    pl_player = find_player_in_fpl(story["player"], fpl_data) is not None
    pl_club   = bool(story.get("to_key") or story.get("from_key"))
    if not pl_club:
        for nm in (story.get("to_club"), story.get("from_club")):
            if nm and nm.lower() in PL_CLUB_NAMES:
                pl_club = True
                break
    if not pl_player and not pl_club:
        return False, "not_fpl_relevant"
    return True, "ok"

def classify_post(story, sources):
    if story.get("collapsed"):
        return "confirmed"
    if story["event"] in ("manager", "injury", "stay", "renewal", "loan_option"):
        return "confirmed"
    tl     = story.get("body", "").lower() + " " + " ".join(story.get(k, "") or "" for k in ("headline",)).lower()
    strong = story["stage"] >= 4 or any(w in tl for w in STRONG_OFFICIAL)
    top_source = any(s in TOP_SOURCES for s in sources)
    multi  = len(set(sources)) >= 2
    if strong or multi or top_source:
        return "confirmed"
    if is_big_player(story["player"], fetch_fpl_data()) or story.get("confidence", 0) >= 0.7:
        return "rumour"
    return None

# ── TWEET TEXT ───────────────────────────────────────────────────────────────
def twitter_len(text: str) -> int:
    url_re  = re.compile(r'https?://\S+|www\.\S+')
    urls    = url_re.findall(text)
    stripped = url_re.sub("", text)
    weight  = 23 * len(urls)
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
    ev   = story["event"]
    base = "#TransferNews" if ev in ("transfer", "loan", "loan_option") else \
           "#ManagerNews"  if ev == "manager" else "#InjuryNews" if ev == "injury" else "#FootballNews"
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
    ev     = story["event"]
    prefix = "COLLAPSED" if story.get("collapsed") else EVENT_PREFIX.get(ev, "UPDATE")
    head   = story.get("headline") or "Update"
    lines  = [f"🚨 {prefix} | {head}", "", story.get("body") or ""]
    if story.get("conditional"):
        lines.append(f"\n📌 {story['conditional']}")
    details = []
    if story.get("fee"):
        details.append(f"💰 Fee: {story['fee']}")
    if story.get("contract"):
        details.append(f"📄 {story['contract']}")
    if details:
        lines.append("\n" + " | ".join(details))
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
    return " | ".join(bits)

# ── GRAPHICS ENGINE ──────────────────────────────────────────────────────────
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
    p    = Path(f"logos/{safe}.png")
    if not p.exists() and FPL_LOGO_IDS.get(safe):
        _download_asset(f"https://resources.premierleague.com/premierleague/badges/t{FPL_LOGO_IDS[safe]}.png", p)
    if p.exists():
        src = _safe_open_rgba(p)
        if src is not None:
            return _fit_contain(src, box, box)
    return None

def create_image(story, sources, filename, rumour=False):
    W, H = 1200, 675
    fpl  = fetch_fpl_data()
    player_el   = find_player_in_fpl(story.get("player"), fpl)
    player_name = (player_el["web_name"] if player_el else story.get("player")) or "PLAYER"
    to_key      = story.get("to_key")
    from_key    = story.get("from_key")
    ev          = story["event"]
    collapsed   = story.get("collapsed")
    GREEN       = (40, 210, 90)

    face_verified = False
    if player_el:
        cur = fpl_team_key(player_el, fpl)
        if cur is None:
            face_verified = True
        elif cur == from_key or cur == to_key:
            face_verified = True
        elif not from_key and not to_key:
            face_verified = True

    stats      = None
    player_img = Path("players/silhouette.png")
    if player_el:
        code   = player_el["code"]
        stats  = {"cost": f"£{player_el['now_cost']/10.0}m", "pts": str(player_el['total_points']),
                  "goals": str(player_el['goals_scored']),   "assists": str(player_el['assists'])}
        if face_verified:
            player_img = Path(f"players/{code}.png")
            if not player_img.exists():
                _download_asset(
                    f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{code}.png",
                    player_img)

    bg_color = CLUB_COLORS.get(to_key, (25, 29, 38))
    accent   = (255, 90, 0)  if ev in ("transfer", "loan", "loan_option") else \
               (0, 163, 255) if ev == "manager" else \
               (255, 0, 77)  if ev == "injury"  else (120, 200, 120)
    if collapsed:
        accent = (150, 80, 80)

    img  = Image.new("RGB", (W, H), (14, 16, 21))
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
            p_img  = _fit_contain(p_src, 460, 460)
            shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            sh     = p_img.split()[3].point(lambda a: int(a * 0.55))
            shadow.paste((0, 0, 0, 255), (W - 420 + 8, H - p_img.height - 6 + 8), sh)
            shadow = shadow.filter(ImageFilter.GaussianBlur(8))
            img.paste(shadow, (0, 0), shadow)
            img.paste(p_img,  (W - 420, H - p_img.height - 6), p_img)
            photo_ok = True

    if not photo_ok:
        ov  = Image.new("RGBA", (W, H), (0, 0, 0, 0)); od = ImageDraw.Draw(ov)
        cx, cyc, r = int(W * 0.78), int(H * 0.50), 150
        od.ellipse([cx - r, cyc - r, cx + r, cyc + r], fill=(0, 0, 0, 70))
        od.ellipse([cx - 52, cyc - 78, cx + 52, cyc + 26], fill=(255, 255, 255, 60))
        od.pieslice([cx - 95, cyc + 6, cx + 95, cyc + 210], 180, 360, fill=(255, 255, 255, 60))
        img.paste(ov, (0, 0), ov)
        ph  = get_premium_font(34, "Black"); lab = "NO PHOTO"
        lw  = od.textlength(lab, font=ph)
        ImageDraw.Draw(img).text((cx - lw/2, cyc + r - 6), lab, font=ph, fill=(255, 255, 255))

    TEXT_X    = 60
    TEXT_MAX_W = int(W * 0.62) - TEXT_X
    draw.rectangle([0, 0, W, 12], fill=accent)

    brand      = get_premium_font(60, "Black")
    sub        = get_premium_font(38, "Bold")
    crest_font = get_premium_font(34, "Black")

    draw.text((TEXT_X, 44), "FPL", font=brand, fill=(255, 255, 255))
    fpl_w = draw.textlength("FPL ", font=brand)
    draw.text((TEXT_X + fpl_w, 44), "VORTEX", font=brand, fill=accent)

    if rumour:
        badge_txt, badge_fill, badge_bg = "RUMOUR – NOT CONFIRMED", (255, 196, 0), (60, 45, 0)
    else:
        badge_txt  = ("COLLAPSED" if collapsed else EVENT_PREFIX.get(ev, "UPDATE"))
        badge_fill, badge_bg = accent, (25, 28, 38)
    bw = int(draw.textlength(badge_txt, font=sub))
    draw.rounded_rectangle([TEXT_X, 138, TEXT_X + bw + 52, 206], radius=14, fill=badge_bg)
    draw.text((TEXT_X + 26, 152), badge_txt, font=sub, fill=badge_fill)

    name_up = player_name.upper()
    nsize   = 78
    while nsize >= 40 and draw.textlength(name_up, font=get_premium_font(nsize, "Black")) > TEXT_MAX_W:
        nsize -= 3
    nf     = get_premium_font(nsize, "Black"); name_y = 236
    with Pilmoji(img) as pj:
        pj.text((TEXT_X, name_y), name_up, font=nf, fill=(255, 255, 255))
    nb          = draw.textbbox((0, 0), name_up, font=nf)
    name_bottom = name_y + (nb[3] - nb[1]) + 12

    CREST   = 132
    from_im = _load_crest(from_key, CREST); to_im = _load_crest(to_key, CREST)
    row_y   = name_bottom + 36; cy = row_y + CREST // 2; x = TEXT_X

    is_transfer = ev in ("transfer", "loan", "loan_option")

    if is_transfer and from_im is not None and to_im is not None:
        # Full FROM -> TO layout for transfers
        img.paste(from_im, (x, row_y + (CREST - from_im.height)//2), from_im)
        fn  = (story.get("from_club") or from_key or "").replace("_", " ").upper()
        fnw = draw.textlength(fn, font=crest_font)
        draw.text((x + (CREST - fnw)//2, row_y + CREST + 10), fn, font=crest_font, fill=(235, 235, 235))
        x += CREST + 30

        _draw_arrow(draw, x, cy - 14, 150, GREEN, thick=28); x += 180

        img.paste(to_im, (x, row_y + (CREST - to_im.height)//2), to_im)
        tn  = (story.get("to_club") or to_key or "").replace("_", " ").upper()
        tnw = draw.textlength(tn, font=crest_font)
        draw.text((x + (CREST - tnw)//2, row_y + CREST + 10), tn, font=crest_font, fill=(255, 255, 255))
    else:
        # Single logo mode (No arrow) for injuries, managers, or single-club updates
        single_im = to_im or from_im
        single_key = to_key if to_im else from_key
        single_club = story.get("to_club") if to_im else story.get("from_club")

        if single_im is not None:
            img.paste(single_im, (x, row_y + (CREST - single_im.height)//2), single_im)
            cn  = (single_club or single_key or "").replace("_", " ").upper()
            cnw = draw.textlength(cn, font=crest_font)
            draw.text((x + (CREST - cnw)//2, row_y + CREST + 10), cn, font=crest_font, fill=(255, 255, 255))

    detail = build_detail_line(story)
    if detail:
        with Pilmoji(img) as pj:
            pj.text((TEXT_X, row_y + CREST + 56), detail.upper()[:60], font=sub, fill=(160, 255, 120))

    draw.rectangle([0, H - 90, W, H - 12], fill=(20, 24, 33))
    draw.rectangle([0, H - 12, W, H], fill=accent)

    if stats:
        bar  = (f"FPL COST: {stats['cost']} | POINTS: {stats['pts']}"
                f" | GOALS: {stats['goals']} | ASSISTS: {stats['assists']}")
        fill = (255, 255, 255)
    else:
        src  = " · ".join(f"@{s}" for s in sources[:2])
        bar  = f"Source: {src} | @FPLVortex"; fill = (170, 180, 200)

    bsize = 30; bf = get_premium_font(bsize, "Bold")
    while bsize > 18 and draw.textlength(bar, font=bf) > (W - 120):
        bsize -= 1; bf = get_premium_font(bsize, "Bold")
    bbox = draw.textbbox((0, 0), bar, font=bf)
    by   = (H - 90) + (78 - (bbox[3]-bbox[1])) // 2 - bbox[1]
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
            out  = []
            for it in root.findall(".//item")[:8]:
                link, desc = it.find("link"), it.find("description")
                if link is None:
                    continue
                tid  = link.text.strip().split("/")[-1].split("#")[0]
                text = re.sub(r'<[^>]+>', '', desc.text).strip() if desc is not None and desc.text else ""
                if tid and text:
                    out.append({"id": tid, "text": text})
            if out:
                return out
        except Exception:
            continue
    return []

async def get_twikit_tweets(read_client, username, count=20, retries=2):
    if read_client is None:
        return []
    for attempt in range(retries):
        try:
            user   = await read_client.get_user_by_screen_name(username)
            tweets = await read_client.get_user_tweets(user.id, "Tweets", count=count)
            out    = []
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
    tweets = await get_twikit_tweets(read_client, username)
    if tweets:
        return tweets, "twikit"
    nit = get_nitter_tweets(username)
    return nit, ("nitter" if nit else "none")

async def scrape(data, read_client):
    fpl       = fetch_fpl_data()
    story_map = {}
    seen = skipped = 0

    for username in JOURNALISTS:
        try:
            tweets, src = await fetch_tweets(read_client, username)
        except Exception as e:
            print(f"  [READ] @{username} error: {e}")
            tweets, src = [], "error"

        if not tweets and src == "none":
            print(f"  [WARN] @{username}: ALL sources failed — X tokens may be expired or Nitter is down")
        else:
            print(f"  [READ] @{username}: {len(tweets)} tweets via {src}")

        for t in tweets:
            tid, text = t["id"], t["text"]
            if tid in data["posted_ids"]:
                continue
            if not any(k in text.lower() for k in FOOTBALL_KW):
                continue
            seen += 1
            story      = build_story(text)
            safe, why  = passes_safety_gate(story, text, fpl)
            if not safe:
                skipped += 1
                print(f"  ⏭️  skip ({why}): {text[:70]!r}")
                continue
            anchor     = story.get("to_key") or story.get("from_key") or "unknown"
            key        = build_story_key(story["player"], anchor, story["event"])
            ok, reason = should_post(data, key, story["stage"], story["collapsed"])
            if not ok:
                print(f"  ⏭️  skip ({reason}): {key}")
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
        print("  [WARN] ⚠️ Zero tweets fetched from ALL journalists. X auth tokens likely expired.")
    print(f"  [SCRAPE] {seen} football tweets seen, {skipped} skipped, {len(story_map)} candidate stories")

    ready = []
    for key, st in story_map.items():
        mode = classify_post(st, st["sources"])
        if mode is None:
            data["pending"][key] = {
                "sources": st["sources"], "player": st["player"],
                "to_key":  st.get("to_key"), "event": st["event"],
                "last_seen": datetime.now(timezone.utc).isoformat(),
            }
            continue
        st["rumour"] = (mode == "rumour")
        data["pending"].pop(key, None)
        ready.append(st)

    save_data(data)
    return sorted(ready, key=lambda x: -(1 if x["collapsed"] else x["stage"]))

# ── PUBLISH ──────────────────────────────────────────────────────────────────
async def post_item(client, item, data):
    rumour   = item.get("rumour", False)
    filename = "news_card.png"
    create_image(item, item["sources"], filename, rumour=rumour)
    media_id = await client.upload_media(filename, media_type="image/png")
    body     = trim_for_twitter(build_tweet_body(item, item["sources"], rumour), limit=278)
    await client.create_tweet(text=body, media_ids=[media_id])
    if os.path.exists(filename):
        os.remove(filename)
    data["posted_ids"].append(item["id"])
    data["stories"][item["key"]] = {
        "stage":        item["stage"],      "player":  item["player"],
        "to_key":       item.get("to_key"), "event":   item["event"],
        "status":       "collapsed" if item["collapsed"] else "active",
        "sources":      item["sources"],
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    increment_daily(data)
    save_data(data)
    move_to_posted(item)
    print(f"  ✅ Posted: {item['player']} — {item['event']} (stage {item['stage']})")

# ── MAIN ─────────────────────────────────────────────────────────────────────
async def main():
    print(f"\n[BOT] Run — {datetime.now(timezone.utc).isoformat()} (LLM={'on' if _ANTHROPIC_OK else 'off/fallback'})")
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

    client = Client("en-US")
    client.set_cookies({"auth_token": X_POST_AUTH_TOKEN, "ct0": X_POST_CT0_TOKEN})

    remaining = data["daily"]["limit"] - data["daily"]["count"]
    batch     = queue[:max(0, min(3, remaining))]

    for i, item in enumerate(batch):
        if i > 0:
            await asyncio.sleep(90)
        try:
            await post_item(client, item, data)
        except Exception as e:
            print(f"  ❌ Failed to post {item.get('player')}: {e}")

if __name__ == "__main__":
    asyncio.run(main())
