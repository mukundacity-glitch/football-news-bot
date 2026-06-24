"""
FPL VORTEX — Football news automation.

What this build does:
  - Scrapes trusted journalist/club accounts.
  - Extracts ONE accurate story per tweet (LLM with a truthful fallback).
  - Strips RT/@handles/URLs/raw repost text; writes an original short summary.
  - Classifies as OFFICIAL / TRANSFER / RUMOUR / INJURY / LOAN / CONTRACT /
    MANAGER using a strict source rule (official OR >= 2 trusted reporters).
  - Renders a clean card: "FPL VORTEX" wordmark top-left, FROM:/TO: text rows
    (NO arrows), club crests, relevant hashtags only.
  - DEFAULT IS LIVE: it AUTO-POSTS confirmed/rumour stories to X, capped per
    run, per hour and per day. Every post is guaranteed an image card.

Pass --draft-only to only build cards into queue/pending/ (no posting), or
--dry-run to test the whole pipeline offline.
"""

from clubs_cache import get_club_data
import os
import re
import json
import hashlib
import difflib
import random
import argparse
import asyncio
import requests
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageChops
from pilmoji import Pilmoji

# ── TWIKIT PATCH (inline) ────────────────────────────────────────────────
try:
    _tx_mod = __import__(
        "twikit.x_client_transaction.transaction", fromlist=["ClientTransaction"]
    )
except Exception as e:
    _tx_mod = None
    print(f"[PATCH] twikit transaction module not found, skipping patch: {e}")

if _tx_mod is not None:
    _tx_mod.ON_DEMAND_FILE_REGEX = re.compile(
        r""",(\d+):["']ondemand\.s["']""", flags=(re.VERBOSE | re.MULTILINE)
    )
    _tx_mod.ON_DEMAND_HASH_PATTERN = r',{}:"([0-9a-f]+)"'
    _tx_mod.INDICES_REGEX = re.compile(
        r"""(\(\w{1,2}\[(\d{1,2})\],\s*16\))+""", flags=(re.VERBOSE | re.MULTILINE)
    )

    async def _patched_get_indices(self, home_page_response, session, headers):
        key_byte_indices = []
        response = self.validate_response(home_page_response) or self.home_page_response
        response_str = str(response)

        on_demand_file = _tx_mod.ON_DEMAND_FILE_REGEX.search(response_str)
        if on_demand_file:
            on_demand_file_index = on_demand_file.group(1)
            hash_regex = re.compile(
                _tx_mod.ON_DEMAND_HASH_PATTERN.format(on_demand_file_index)
            )
            hash_match = hash_regex.search(response_str)
            if hash_match:
                filename = hash_match.group(1)
                on_demand_file_url = (
                    "https://abs.twimg.com/responsive-web/client-web/"
                    f"ondemand.s.{filename}a.js"
                )
                on_demand_file_response = await session.request(
                    method="GET", url=on_demand_file_url, headers=headers
                )
                key_byte_indices_match = _tx_mod.INDICES_REGEX.finditer(
                    str(on_demand_file_response.text)
                )
                for item in key_byte_indices_match:
                    key_byte_indices.append(item.group(2))

        if not key_byte_indices:
            raise Exception("Couldn't get KEY_BYTE indices")
        key_byte_indices = list(map(int, key_byte_indices))
        return key_byte_indices[0], key_byte_indices[1:]

    _tx_mod.ClientTransaction.get_indices = _patched_get_indices
    print("[PATCH] twikit ClientTransaction.get_indices patched (issue #408 workaround).")
# ── END TWIKIT PATCH ─────────────────────────────────────────────────────

from twikit import Client

# ── GEMINI (google-genai SDK) ────────────────────────────────────────────
GEMINI_MODEL_CHAIN = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash",
]
GEMINI_TIMEOUT_S = 20

try:
    from google import genai as _genai
    from google.genai import types as _genai_types
    _GEMINI_KEY = os.getenv("GEMINI_API_KEY")
    _gemini_client = _genai.Client(api_key=_GEMINI_KEY) if _GEMINI_KEY else None
    _GEMINI_OK = _gemini_client is not None
except Exception as _e:
    print(f"[GEMINI] google-genai SDK unavailable, using regex fallback only: {_e}")
    _gemini_client = None
    _GEMINI_OK = False
    _genai_types = None

_GEMINI_LAST_MODEL = None

# ── GROQ (fallback when Gemini quota exhausted) ──────────────────────────
GROQ_TIMEOUT_S = 20
GROQ_MODEL = "llama-3.3-70b-versatile"

try:
    import urllib.request as _urllib_req
    _GROQ_KEY = os.getenv("GROQ_API_KEY")
    _GROQ_OK = bool(_GROQ_KEY)
except Exception:
    _GROQ_KEY = None
    _GROQ_OK = False

def _groq_generate(prompt: str):
    """Call Groq API with Llama 3.3 70B. Returns (text, model_id) or (None, None)."""
    if not _GROQ_OK or not _GROQ_KEY:
        return None, None
    try:
        payload = json.dumps({
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 1000,
        }).encode("utf-8")
        req = _urllib_req.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {_GROQ_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with _urllib_req.urlopen(req, timeout=GROQ_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"]
        if text:
            print(f"  [GROQ] using model: {GROQ_MODEL}")
            return text, GROQ_MODEL
        return None, None
    except Exception as e:
        msg = str(e)
        short = (msg[:120] + "…") if len(msg) > 120 else msg
        print(f"  [GROQ] failed ({short}) — falling back to regex")
        return None, None
X_AUTH_TOKEN = (os.getenv("X_AUTH_TOKEN") or "").strip()
X_CT0_TOKEN = (os.getenv("X_CT0_TOKEN") or "").strip()
X_POST_AUTH_TOKEN = (os.getenv("X_POST_AUTH_TOKEN") or "").strip()
X_POST_CT0_TOKEN = (os.getenv("X_POST_CT0_TOKEN") or "").strip()
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

# ── PATHS ────────────────────────────────────────────────────────────────
# Bump _LOGIC_VER whenever extraction/classification logic changes. Cached
# stories stamped with an older version are re-extracted, so a logic upgrade
# never re-posts stories the new rules would reject (e.g. stale "transfer"
# extractions of what is now correctly an "other"/non-news event).
_LOGIC_VER = 3
POSTED_FILE = Path("posted_news.json")
PENDING_DIR = Path("queue/pending")
POSTED_DIR = Path("queue/posted")
for d in (PENDING_DIR, POSTED_DIR, Path("logos"), Path("players")):
    d.mkdir(parents=True, exist_ok=True)

# ── CHANNEL BRANDING ─────────────────────────────────────────────────────
CHANNEL_NAME = "FPL VORTEX"
CHANNEL_HANDLE = "@FPLVortex"

# ── JOURNALISTS ──────────────────────────────────────────────────────────
JOURNALISTS = [
    # Elite tier — "here we go" / confirmed news
    "FabrizioRomano", "David_Ornstein", "_pauljoyce", "sistoney67",
    "SamiMokbel_BBC", "JacobsBen", "JamesPearceLFC", "SachaTavolieri",
    "Plettigoal", "MatteoMoretto", "AlfredoPedulla", "DiMarzio",
    # Trusted media
    "SkySportsNews", "BBCSport", "TheAthleticFC", "guardian_sport",
    "lequipe", "marca", "diarioas", "kicker",
    "alex_crook", "AlexCrabb31", "Transferzone00",
    # PL official accounts (injury/squad news)
    "premierleague", "OfficialFPL", "PremierInjuries",
    "Arsenal", "AVFCOfficial", "ManCity", "LFC", "ChelseaFC",
    "ManUtd", "SpursOfficial", "NUFC", "NFFC", "Everton",
    "WestHam", "CPFC", "OfficialBHAFC", "Wolves", "BrentfordFC",
    "FulhamFC", "AFCBournemouth", "lcfc",
]
NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]

# ── SOURCE TIERS ─────────────────────────────────────────────────────────
OFFICIAL_ACCOUNTS = {
    "premierleague", "officialfpl", "fpl", "uefa", "fifacom", "fifaworldcup",
    "arsenal", "avfcofficial", "afcbournemouth", "brentfordfc",
    "officialbhafc", "chelseafc", "cpfc", "everton", "fulhamfc",
    "lcfc", "liverpoolfc", "lfc", "mancity", "manutd", "newcastle_nufc", "nufc",
    "nffc", "southamptonfc", "spursofficial", "westham", "wolves",
}
OFFICIAL_INJURY_ACCOUNTS = OFFICIAL_ACCOUNTS | {"officialfpl", "fpl", "premierleague", "premierinjuries"}
ELITE_TRUSTED = {
    "fabrizioromano", "david_ornstein", "_pauljoyce", "sistoney67",
    "samimokbel_bbc", "jacobsben", "jamespearcelfc", "sachatavolieri",
    "plettigoal", "matteomoretto", "alfredopedulla", "dimarzio",
}
TRUSTED_REPORTERS = ELITE_TRUSTED
TRUSTED_MEDIA = {
    "skysportsnews", "skysports", "bbcsport", "theathleticfc", "theathletic",
    "guardian_sport", "lequipe", "marca", "diarioas", "as", "kicker",
    "alex_crook", "alexcrabb31",
}
# Aggregators / rumour pages: deliberately left in NO tier (tier 0). Their
# items only reach a post when corroborated by a trusted reporter, matching the
# "aggregators require tier-1 confirmation" rule. (e.g. Transferzone00)

def source_tier(handle: str) -> int:
    h = (handle or "").lower().lstrip("@")
    if h in OFFICIAL_ACCOUNTS: return 1
    if h in ELITE_TRUSTED: return 2
    if h in TRUSTED_MEDIA: return 3
    return 0

# ── LIGHT PRE-FILTER ─────────────────────────────────────────────────────
FOOTBALL_KW = [
    "transfer", "sign", "deal", "fee", "bid", "loan", "contract", "agree",
    "medical", "official", "here we go", "talks", "joins", "move", "target",
    "injury", "injured", "ruled out", "scan", "hamstring", "surgery", "doubt",
    "sack", "appoint", "manager", "head coach", "stay", "return", "recall",
    "suspended", "suspension", "banned", "red card", "sent off",
]
STAFF_BLOCK_KW = [
    "head of recruitment", "sporting director", "director of football",
    "technical director", "chief scout", "scouting", "ceo", "chairman",
    "owner", "president", "physio", "kit man", "head of football",
    "transfer chief", "negotiator",
]

# ── CLUB MAPS ────────────────────────────────────────────────────────────
CLUB_ALIASES = {
    "arsenal": "Arsenal", "aston villa": "Aston_Villa", "villa": "Aston_Villa",
    "bournemouth": "Bournemouth", "brentford": "Brentford", "brighton": "Brighton",
    "chelsea": "Chelsea", "crystal palace": "Crystal_Palace", "palace": "Crystal_Palace",
    "everton": "Everton", "fulham": "Fulham", "ipswich": "Ipswich", "ipswich town": "Ipswich",
    "leicester": "Leicester", "leicester city": "Leicester", "liverpool": "Liverpool",
    "manchester city": "Man_City", "man city": "Man_City", "manchester united": "Man_Utd",
    "man united": "Man_Utd", "man utd": "Man_Utd", "newcastle": "Newcastle",
    "newcastle united": "Newcastle", "nottingham forest": "Nottm_Forest",
    "nott'm forest": "Nottm_Forest", "forest": "Nottm_Forest", "southampton": "Southampton",
    "tottenham": "Spurs", "spurs": "Spurs", "tottenham hotspur": "Spurs",
    "west ham": "West_Ham", "west ham united": "West_Ham", "wolves": "Wolves",
    "wolverhampton": "Wolves",
}
_SORTED_ALIASES = sorted(CLUB_ALIASES.keys(), key=len, reverse=True)
CLUB_WORD_FRAGMENTS: set = set()

NATIONALITY_ADJECTIVES = {
    "english", "french", "german", "spanish", "italian", "portuguese", "dutch",
    "brazilian", "argentinian", "belgian", "croatian", "danish", "swedish",
    "norwegian", "scottish", "welsh", "irish", "austrian", "swiss", "polish",
    "ukrainian", "turkish", "greek", "serbian", "canadian", "american", "mexican",
    "japanese", "korean", "senegalese", "nigerian", "ghanaian", "moroccan",
    "egyptian", "cameroonian", "colombian", "uruguayan", "chilean", "australian",
    "algerian", "tunisian", "ivorian", "congolese", "zambian",
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
PL_CLUB_NAMES = {a for a in CLUB_ALIASES}

BUNDESLIGA_BIG_CLUBS = {"bayern", "bayern munich", "borussia dortmund", "dortmund",
                        "leipzig", "rb leipzig", "leverkusen", "bayer leverkusen"}
LA_LIGA_BIG_CLUBS = {"real madrid", "barcelona", "atletico madrid", "atletico",
                     "sevilla", "villarreal", "real sociedad", "athletic bilbao"}
ELITE_EURO_CLUBS = (BUNDESLIGA_BIG_CLUBS | LA_LIGA_BIG_CLUBS |
                    {"psg", "paris saint-germain", "juventus", "inter", "ac milan",
                     "milan", "napoli", "roma", "benfica", "porto", "ajax"})

def resolve_club_key(name: str):
    if not name: return None
    n = name.lower()
    for alias in _SORTED_ALIASES:
        if re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', n):
            return CLUB_ALIASES[alias]
    return None

BIG_CLUBS_NON_PL = ELITE_EURO_CLUBS

def is_big_club_name(name: str) -> bool:
    if not name: return False
    n = name.lower().strip()
    return any(n == c or c in n for c in BIG_CLUBS_NON_PL)

def is_bundesliga_or_laliga_club(name: str) -> bool:
    if not name: return False
    n = name.lower().strip()
    return any(n == c or c in n for c in (BUNDESLIGA_BIG_CLUBS | LA_LIGA_BIG_CLUBS))

BIG_NAMES_NON_FPL = {
    "mbappe", "mbappé", "vinicius", "vinícius", "bellingham", "rodrygo",
    "haaland", "lewandowski", "messi", "neymar", "ronaldo", "modric", "kroos",
    "benzema", "pedri", "gavi", "yamal", "kane", "musiala", "wirtz", "kvaratskhelia",
}
MANAGER_SURNAMES = {
    "de zerbi", "zerbi", "guardiola", "arteta", "klopp", "slot", "postecoglou",
    "ten hag", "amorim", "emery", "howe", "maresca", "iraola", "frank",
    "nuno", "moyes", "dyche", "hurzeler", "glasner", "ancelotti", "xabi alonso",
    "alonso", "flick", "simeone", "mourinho", "conte", "tuchel", "nagelsmann",
    "neil", "o'neil", "mcinnes", "wilder", "edwards", "robinson", "silva",
    "kompany", "lopetegui", "obi",
}

def is_big_name_player(name: str) -> bool:
    if not name: return False
    n = name.lower().strip()
    return any(part in BIG_NAMES_NON_FPL for part in re.split(r'[\s\-]+', n))

# ── CLUBS_CACHE WIRING ───────────────────────────────────────────────────
CLUB_NAME_SET = set()
CLUB_HASHTAGS = {}

def init_club_data():
    global CLUB_NAME_SET, CLUB_HASHTAGS, PL_CLUB_NAMES
    try:
        d = get_club_data()
    except Exception as e:
        print(f"[CLUBS] get_club_data failed: {e}")
        return
    CLUB_HASHTAGS = d.get("club_hashtags", {}) or {}
    PL_CLUB_NAMES = set(d.get("pl_clubs", []) or []) | {a for a in CLUB_ALIASES}
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

COUNTRY_NAMES: set = set()

def _build_country_block(fpl_data):
    global COUNTRY_NAMES
    if not fpl_data: return
    for el in fpl_data.get("elements", []):
        nat = (el.get("nationality") or "").lower().strip()
        if nat: COUNTRY_NAMES.add(nat)

def looks_like_club(name: str) -> bool:
    if not name: return False
    n = name.lower().strip()
    if n in CLUB_NAME_SET or n in CLUB_ALIASES: return True
    return any(n == c or c in n for c in CLUB_NAME_SET if len(c) >= 5)

def hashtag_for(name_or_key: str):
    if not name_or_key: return None
    if name_or_key in CLUB_HASHTAG_MAP: return CLUB_HASHTAG_MAP[name_or_key]
    n = name_or_key.replace("_", " ").lower()
    return CLUB_HASHTAG_MAP.get(resolve_club_key(n) or "", CLUB_HASHTAGS.get(n))

# ── STATE ────────────────────────────────────────────────────────────────
def load_data() -> dict:
    fresh = {"daily": {"date": "", "count": 0, "limit": 17}, "stories": {}, "posted_ids": []}
    if POSTED_FILE.exists():
        try:
            with open(POSTED_FILE) as f: d = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[STATE] posted_news.json unreadable ({e}); starting fresh.")
            d = fresh
    else: d = fresh
    d.setdefault("daily", fresh["daily"])
    d.setdefault("stories", {})
    d.setdefault("posted_ids", [])
    d.setdefault("pending", {})
    d.setdefault("extracted", {})
    d.setdefault("posted_hashes", [])
    d.setdefault("posted_headlines", [])
    d.setdefault("posted_player_events", [])
    d.setdefault("posted_collapses", [])
    return d

def save_data(data: dict):
    tmp = POSTED_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f: json.dump(data, f, indent=2)
    tmp.replace(POSTED_FILE)

def check_daily_limit(data: dict) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if data["daily"]["date"] != today:
        data["daily"] = {"date": today, "count": 0, "limit": 17}
    return data["daily"]["count"] < data["daily"]["limit"]

def increment_daily(data: dict):
    data["daily"]["count"] += 1

# ── STORY EXTRACTION ─────────────────────────────────────────────────────
_EXTRACT_PROMPT = """You are a football transfer-desk editor. Read this reporter tweet and extract ONLY what it actually states. Do NOT invent, assume, or generalise.

ACCURACY RULES (critical):
- Every field must come from THIS tweet only. Never mix in other players, clubs, fees or stories.
- from_club is the player's CURRENT/SELLING club; to_club is the DESTINATION/BUYING club. Do not reverse them.
- from_club and to_club must be DIFFERENT clubs. If only one is clearly named, set the other to null.
- If the tweet says a player is STAYING / not moving / signing a new deal at his current club, set event="renewal" or "stay" and DO NOT name a different destination club.
- Never output placeholder text. Use null when unknown.
- If the tweet is a manager/coach appointment, event="manager".
- If the tweet is an injury, event="injury" and leave transfer fields null.
- body must be ONE original English sentence summarising the news. Never copy the tweet wording. Never include "RT", @handles, or links.

LANGUAGE: Translate everything to natural English.

Return STRICT JSON only:
{{"is_football": true/false,
 "event": "transfer|loan|loan_option|stay|renewal|injury|manager|collapse|other",
 "is_real_move": true/false,
 "player": "full name or null",
 "from_club": "selling/current club or null",
 "to_club": "destination club or null",
 "fee": "e.g. £30m or null",
 "contract": "e.g. until 2028 or null",
 "conditional": "one short English sentence on any deadline/condition, else null",
 "fpl_impact": "one short English FPL angle, else null",
 "diagnosis": "injury only: 1-4 words, else null",
 "expected_return": "injury only: timeline, else null",
 "next_match": "injury only: affected fixture, else null",
 "stage": 1,
 "collapsed": true/false,
 "headline": "<=10 word English headline true to THIS story",
 "body": "ONE original factual English sentence. No RT, no @handles, no links.",
 "confidence": 0.0-1.0}}
Tweet:
\"\"\"{tweet}\"\"\""""

def _clean_source_text(text: str) -> str:
    t = text or ""
    t = re.sub(r'\bRT\s+@\w+:?', ' ', t)
    t = re.sub(r'https?://\S+|www\.\S+', ' ', t)
    t = re.sub(r'(?<!\w)@\w+', ' ', t)
    t = re.sub(r'#\w+', ' ', t)
    t = re.sub(r'[“”"]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t

def _gemini_generate(prompt: str):
    global _GEMINI_LAST_MODEL
    if not _GEMINI_OK or _gemini_client is None: return None, None
    cfg = None
    if _genai_types is not None:
        try:
            cfg = _genai_types.GenerateContentConfig(
                temperature=0.2,
                http_options=_genai_types.HttpOptions(timeout=GEMINI_TIMEOUT_S * 1000),
            )
        except Exception:
            cfg = None
    for model_id in GEMINI_MODEL_CHAIN:
        try:
            resp = _gemini_client.models.generate_content(
                model=model_id, contents=prompt, config=cfg)
            text = getattr(resp, "text", None)
            if text:
                if _GEMINI_LAST_MODEL != model_id:
                    print(f"  [GEMINI] using model: {model_id}")
                _GEMINI_LAST_MODEL = model_id
                return text, model_id
            print(f"  [GEMINI] {model_id} returned empty — trying next")
        except Exception as e:
            msg = str(e)
            short = (msg[:120] + "…") if len(msg) > 120 else msg
            print(f"  [GEMINI] {model_id} failed ({short}) — trying next")
            continue
    print("  [GEMINI] ALL models failed — using regex fallback")
    return None, None

def extract_story_llm(tweet_text: str):
    prompt = _EXTRACT_PROMPT.format(tweet=tweet_text)
    # 1. Try Gemini chain first
    if _GEMINI_OK:
        text, _model = _gemini_generate(prompt)
        if text:
            try:
                return json.loads(text[text.find("{"): text.rfind("}") + 1])
            except Exception as e:
                print(f"  [LLM] Gemini JSON parse failed: {e}")
    # 2. Gemini exhausted/failed — try Groq
    if _GROQ_OK:
        text, _model = _groq_generate(prompt)
        if text:
            try:
                return json.loads(text[text.find("{"): text.rfind("}") + 1])
            except Exception as e:
                print(f"  [LLM] Groq JSON parse failed: {e}")
    # 3. Both failed — regex fallback handles it
    return None

_FALLBACK_BANNED_SOLO = {
    "neil", "silva", "alonso", "robinson", "edwards", "wilder", "obi",
    "kompany", "nuno", "frank", "howe", "moyes", "dyche", "emery", "conte",
    "tuchel", "klopp", "arteta", "slot", "amorim", "maresca", "iraola",
}

def _is_safe_fallback_name(name: str) -> bool:
    if not name: return False
    tokens = [t for t in re.split(r"[\s\-']+", name.strip()) if t]
    if len(tokens) < 2: return False
    low = name.lower()
    if low in _FALLBACK_BANNED_SOLO: return False
    if any(m in low for m in MANAGER_SURNAMES): return False
    return True

def extract_story_fallback(tweet_text: str, fpl_data=None) -> dict:
    cleaned = _clean_source_text(tweet_text)
    tl = cleaned.lower()

    def has_word(words_list, text):
        return any(re.search(r'(?<![a-z])' + re.escape(w) + r'(?![a-z])', text) for w in words_list)

    # An on-loan signal must win over injury wording. Recycled lines like
    # "X ruled out ... has joined Y on loan" otherwise misfire as injuries.
    loan_signal = ("on loan" in tl) or bool(re.search(r"\bjoine?d?\b.*\bon loan\b", tl))
    # A real transfer needs a TRANSFER ACTION word — not just a name + a club.
    # This stops press quotes / match reaction ("Rice urges England positivity")
    # being defaulted into a TRANSFER card. No default-to-transfer.
    transfer_signal = has_word(
        ["transfer", "sign", "signs", "signed", "signing", "joins", "joined",
         "join", "deal", "fee", "bid", "offer", "move", "moves", "moving",
         "medical", "here we go", "talks", "target", "targets", "pursue",
         "pursuing", "interested", "interest", "wants", "agree", "agreed",
         "agreement", "personal terms", "close to", "set to join", "swoop",
         "approach", "linked", "chase", "chasing", "snap up", "capture"], tl)
    if has_word(["suspended", "suspension", "banned", "ban", "red card", "sent off"], tl): event = "suspension"
    elif loan_signal: event = "loan"
    elif has_word(["injury", "injured", "ruled out", "scan", "hamstring", "surgery", "doubt"], tl): event = "injury"
    elif has_word(["sack", "appoint", "head coach", "manager"], tl): event = "manager"
    elif has_word(["new deal", "new contract", "signs new", "extension", "renew"], tl): event = "renewal"
    elif has_word(["stay", "staying", "no exit", "not for sale", "remain"], tl) and not has_word(["sign for", "joins", "move to"], tl): event = "stay"
    elif has_word(["loan"], tl): event = "loan"
    elif transfer_signal: event = "transfer"
    else: event = "other"   # no event signal at all -> not news, will be skipped

    stage = 4 if has_word(["here we go", "official", "confirmed", "completed", "joins"], tl) else \
        2 if has_word(["agreement", "agreed", "advanced", "personal terms"], tl) else 1

    FILLER = {"excl", "exclusive", "breaking", "official", "understand", "understands",
              "update", "here", "done", "deal", "medical", "nothing", "all", "source",
              "news", "report", "reports", "told", "says", "said", "claim", "claims",
              "today", "tonight", "tomorrow", "now", "latest", "just", "also",
              "meanwhile", "plus", "however", "elsewhere", "separately",
              "full", "free", "new", "big", "top", "key", "real", "transfer",
              "window", "deadline", "fee", "bid", "offer", "loan", "agree", "agreed",
              "talks", "interest", "signed", "signing", "joins", "joined", "move",
              "permanent", "option", "clause", "release", "extension", "premier",
              "league", "champions", "europa", "conference", "sport", "press"}
    ROLE_WORDS = set()
    for phrase in STAFF_BLOCK_KW:
        for word in phrase.split():
            if len(word) > 3: ROLE_WORDS.add(word)
    ROLE_WORDS |= POSITION_WORDS

    def _is_bad_name(low: str) -> bool:
        if event != "manager" and (low in MANAGER_SURNAMES or any(m in low for m in MANAGER_SURNAMES)): return True
        words = low.split()
        if any(w in FILLER for w in words): return True
        if any(w in CLUB_WORD_FRAGMENTS for w in words): return True
        if any(w in COUNTRY_NAMES for w in words): return True
        if any(w in NATIONALITY_ADJECTIVES for w in words): return True
        if any(w in ROLE_WORDS for w in words): return True
        if looks_like_club(low): return True
        return False

    name = None
    for m in re.findall(r'\b([A-Z][a-zà-ÿ]+(?:\s+(?:(?:van|de|da|dos|del|el|la|le|di|du|den|der|ten|ter|von|zu)\s+)?[A-Z][a-zà-ÿ]+)+)\b', cleaned):
        if not _is_bad_name(m.lower()):
            name = m
            break
    if not name:
        for m in re.findall(r'\b([A-Z][a-zà-ÿ]+(?:[-\' ][A-Z][a-zà-ÿ]+)+)\b', cleaned):
            if not _is_bad_name(m.lower()):
                name = m
                break
    if not name and fpl_data:
        for m in re.findall(r'\b([A-Z][a-zà-ÿ]{2,})\b', cleaned):
            if _is_bad_name(m.lower()): continue
            if find_player_in_fpl(m, fpl_data):
                name = m
                break
    if not name:
        for m in re.findall(r'\b([A-Z][a-zà-ÿ]{2,})\b', cleaned):
            if _is_bad_name(m.lower()): continue
            if is_big_name_player(m):
                name = m
                break

    if name and not _is_safe_fallback_name(name):
        name = None

    clubs = []
    for alias in _SORTED_ALIASES:
        if re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', tl):
            k = CLUB_ALIASES[alias]
            if k not in clubs: clubs.append(k)

    def _alias_after(keyword):
        for alias in _SORTED_ALIASES:
            if re.search(r'\b' + keyword + r'\s+(?:the\s+)?' + re.escape(alias) + r'\b', tl):
                return CLUB_ALIASES[alias]
        return None

    from_anchor = _alias_after("from") or _alias_after("leaves") or _alias_after("leaving")
    to_anchor = (_alias_after("to") or _alias_after("joins") or _alias_after("join")
                 or _alias_after("sign for") or _alias_after("signs for") or _alias_after("moves to")
                 or _alias_after("set to join") or _alias_after("close to joining"))

    from_key = to_key = None
    direction_confident = False
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
    elif to_anchor and len(clubs) <= 1:
        to_key = to_anchor
        direction_confident = True
    elif from_anchor and len(clubs) <= 1:
        from_key = from_anchor
        direction_confident = True
    elif len(clubs) == 1:
        if event in ("stay", "renewal"):
            from_key = clubs[0]
            direction_confident = True
        else:
            to_key = from_key = None
            direction_confident = False
    else:
        to_key = clubs[0] if (len(clubs) == 1) else None
        direction_confident = False

    # Collapse vs rejected-approach are DIFFERENT:
    #  - "deal collapsed / called off / move off" = something ADVANCED then died.
    #  - "offer/bid rejected / club rebuff/reject" = an approach that NEVER
    #    advanced; the player is effectively STAYING. That is a non-move, not a
    #    collapsed transfer, and should not get a dramatic "DEAL OFF" card.
    deal_collapsed = has_word(["collapsed", "called off", "deal off", "move off",
                               "deal collapses", "transfer collapses", "off the table"], tl)
    offer_rejected = bool(re.search(
        r"\b(reject|rejects|rejected|rebuff|rebuffs|rebuffed|turn(s|ed)? down|"
        r"knock(s|ed)? back|snub(s|bed)?)\b", tl)) and bool(re.search(
        r"\b(offer|bid|approach|proposal)\b", tl))

    is_collapsed = deal_collapsed
    # A pure rejected-offer with no prior advanced stage = the player stays put.
    if offer_rejected and not deal_collapsed:
        event = "stay"
        is_collapsed = False

    if event in ("stay", "renewal"):
        if to_key and not from_key:
            from_key, to_key = to_key, None
        to_key = None
        is_collapsed = False

    if is_collapsed and to_key and not from_anchor:
        to_key = None

    summary = _summarise(name, event, from_key, to_key, stage, is_collapsed)

    # CONFIDENCE IS EARNED. Per the "post with safe fallback, skip only when
    # unusable" policy: a story with a clearly identified player AND a meaningful
    # club signal is postable even if other signals are weak — the safe-fallback
    # image/caption handle imperfect detail. Bare guesses (no real player, or no
    # club at all) stay below the gate and are skipped. No per-story rules.
    has_real_player = bool(name and len(re.split(r"[\s\-']+", name.strip())) >= 2)
    # "meaningful club signal" = a resolved PL key OR any named club detected.
    has_club_signal = bool(from_key or to_key or from_anchor or to_anchor or clubs)
    strong_move = bool(re.search(
        r"\b(here we go|official|confirmed|completed|sign(s|ed)?|joins?|joined|"
        r"medical|done deal|agree(s|d)?|personal terms)\b", tl))
    strong_injury = has_word(["ruled out", "injury", "injured", "hamstring",
                              "surgery", "scan", "suspended", "suspension",
                              "banned", "red card", "sent off"], tl)

    conf = 0.30  # base: a bare guess is not trusted
    if has_real_player and has_club_signal:
        conf = 0.50          # postable floor: real player + real club signal
    if strong_move or strong_injury: conf += 0.15
    if (from_key or to_key):         conf += 0.10  # resolved PL club = stronger
    if direction_confident:          conf += 0.05
    conf = round(min(conf, 0.85), 2)  # regex never claims LLM-level certainty

    return {
        "is_football": True, "event": event,
        "is_real_move": event in ("transfer", "loan", "loan_option"),
        "player": name,
        "from_club": (from_key.replace("_", " ") if from_key else None),
        "to_club": (to_key.replace("_", " ") if to_key else None),
        "from_key": from_key, "to_key": to_key,
        "fee": None, "contract": None, "conditional": None, "fpl_impact": None,
        "stage": stage, "collapsed": is_collapsed,
        "headline": name if name else "Transfer update",
        "body": summary, "confidence": conf,
        "direction_confident": direction_confident,
        "from_fallback": True,
    }

def _summarise(name, event, from_key, to_key, stage, collapsed):
    who = name or "The player"
    fc = from_key.replace("_", " ") if from_key else None
    tc = to_key.replace("_", " ") if to_key else None
    if event == "injury": return f"{who} is being assessed and the club is monitoring the situation."
    if event == "suspension": return f"{who} faces a suspension and is set to miss upcoming action."
    if event == "manager": return f"{who} is linked with a managerial move{f' to {tc}' if tc else ''}."
    if event in ("renewal", "stay"):
        base = f"{who} is set to stay" + (f" at {fc or tc}" if (fc or tc) else "")
        return base + "; no exit is planned at this stage."
    if collapsed: return f"A reported move for {who}{f' to {tc}' if tc else ''} has broken down."
    verb = {1: "is being linked with", 2: "is in advanced talks over", 3: "is close to", 4: "is set to complete"}[stage]
    if tc and fc: return f"{who} {verb} a move from {fc} to {tc}."
    if tc: return f"{who} {verb} a move to {tc}."
    return f"{who} {verb} a transfer."

_VIDEO_MARKERS = re.compile(
    r'(youtu\.be|youtube\.com|/video|watch\?v=|\bfull video\b|\bwatch:?\b|'
    r'\blive\s*stream\b|\bpodcast\b|\bepisode\b|\bclip\b|🎥|▶️|📺)', re.I)
_CLAIM_MARKERS = re.compile(
    r'\b(agree[d]?|sign[ed|ing]*|join[s|ed|ing]*|move[s|d]*|deal|bid|offer|'
    r'medical|here we go|loan|contract|talks|fee|ruled out|injur|suspend|'
    r'set to|close to|advanced|personal terms|confirmed|official)\b', re.I)

def looks_like_video_post(tweet_text: str) -> bool:
    return bool(_VIDEO_MARKERS.search(tweet_text or ""))

def has_written_claim(tweet_text: str) -> bool:
    cleaned = _clean_source_text(tweet_text)
    return bool(_CLAIM_MARKERS.search(cleaned)) and len(cleaned.split()) >= 5

# ── HISTORICAL (OLD-NEWS) DETECTOR ───────────────────────────────────────
# Stops decades-old "on this day" / anniversary items posting as if they were
# breaking news (e.g. "Bergkamp completed his transfer to Arsenal in 1995").
# Default: such items are DETECTED and BLOCKED. Flip ALLOW_HISTORICAL_POSTS to
# True to instead post them, tagged HISTORICAL on the card and in the text.
ALLOW_HISTORICAL_POSTS = False

_HISTORICAL_MARKERS = re.compile(
    r"\b(on this day|on this date|this day in|otd|\d+\s+years?\s+(ago|on)|"
    r"years?\s+ago|anniversary|throwback|#tbt|flashback|remember when|"
    r"back in (the\s+)?(19|20)\d\d|years on|on this very day)\b", re.I)

# A "fresh news" cue means the tweet is reporting something happening NOW.
# If present, the recycled-status heuristic below is overridden so genuine
# current developments (a player recovering, a new loan announced today) post.
_FRESH_CUE = re.compile(
    r"\b(today|tonight|tomorrow|breaking|here we go|confirmed|just (in|now)|"
    r"official|now|set to|close to|agreed|agree|recovered|back in training|"
    r"returns? to training|fit again|stepped up|ruled fit|available again|"
    r"new deal|signs new|signed new|extension)\b", re.I)

# Recycled status lines (e.g. OfficialFPL re-surfacing "X has joined Y on loan
# for the rest of the season") have no fresh-news anchor. These are the lines
# that produced the stale Redmond / Young injury-card bug.
_RECYCLED_STATUS = re.compile(
    r"\b(has|have)\s+joined\b.*\bon loan\b|"
    r"\bon loan\b.*\b(rest of the|until end of|for the season)\b|"
    r"\bfor the rest of the (season|campaign)\b", re.I)

def detect_historical(text: str) -> bool:
    """True when a tweet is OLD / recycled news, not a current development.

    We do NOT care how old the player's situation is — only whether THIS tweet
    reports something happening now. "Injured a year ago, recovered today" is
    fresh (passes); "on this day in 2019 he joined on loan" is recycled (blocked).
    """
    t = text or ""
    tl = t.lower()
    has_fresh = bool(_FRESH_CUE.search(tl))
    # Soft markers ("on this day", "X years ago", "anniversary") usually mean
    # reminiscing — BUT "injured a year ago, recovered TODAY" is real news.
    # So a soft marker only blocks when there is NO fresh-news cue.
    if _HISTORICAL_MARKERS.search(tl) and not has_fresh:
        return True
    # Any 1900s year in a football tweet = historical, full stop (hard).
    if re.search(r"\b19\d\d\b", t):
        return True
    # A 2000s year >= 2 seasons old, stated as the TIME OF THE EVENT
    # ("in/back in/during/on 2018"), = old news (hard). "until 2030" /
    # "since 2022" are NOT matched, so future terms / ongoing context are safe.
    cur = datetime.now(timezone.utc).year
    for y in re.findall(r"\b(?:in|back in|during|on)\s+(20\d\d)\b", tl):
        if int(y) <= cur - 2:
            return True
    # Recycled season-status lines with NO fresh cue = stale. With a fresh cue
    # (recovered / today / confirmed / new deal) they are real news and pass.
    if _RECYCLED_STATUS.search(tl) and not has_fresh:
        return True
    return False


def build_story(tweet_text: str, fpl_data=None) -> dict:
    llm = extract_story_llm(tweet_text)
    s = llm or extract_story_fallback(tweet_text, fpl_data)
    if llm is not None:
        s["from_fallback"] = False
        s.setdefault("direction_confident", True)
    s["body"] = _clean_source_text(s.get("body") or "")
    s["from_key"] = s.get("from_key") or resolve_club_key(s.get("from_club"))
    s["to_key"] = s.get("to_key") or resolve_club_key(s.get("to_club"))
    try: s["stage"] = max(1, min(4, int(s.get("stage", 1))))
    except Exception: s["stage"] = 1
    s["collapsed"] = bool(s.get("collapsed"))
    s["historical"] = detect_historical(tweet_text)

    if looks_like_video_post(tweet_text):
        s["from_video"] = True
        s["has_written_claim"] = has_written_claim(tweet_text)
        if s["stage"] > 2: s["stage"] = 2
    else:
        s["from_video"] = False
        s["has_written_claim"] = True

    if len(s["body"].split()) < 4:
        s["body"] = _summarise(s.get("player"), s.get("event"),
                               s.get("from_key"), s.get("to_key"),
                               s.get("stage"), s.get("collapsed"))
    return s

# ── FPL DATA ─────────────────────────────────────────────────────────────
def fetch_fpl_data():
    cache = Path("fpl_cache.json")
    if cache.exists() and (datetime.now().timestamp() - cache.stat().st_mtime < 86400):
        with open(cache) as f: return json.load(f)
    try:
        req = urllib.request.Request(
            "https://fantasy.premierleague.com/api/bootstrap-static/",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        with open(cache, "w") as f: json.dump(data, f)
        _build_country_block(data)
        return data
    except Exception:
        return None

def find_player_in_fpl(player_name, data):
    if not data or not player_name: return None
    q = player_name.lower().strip()
    tokens = [t for t in re.split(r'[\s\-]+', q) if t]
    if not tokens: return None
    for el in data.get("elements", []):
        web = el["web_name"].lower()
        full = (el["first_name"] + " " + el["second_name"]).lower()
        if q == full or q == web: return el
        if len(tokens) >= 2 and all(
                re.search(r'(?<![a-z])' + re.escape(t) + r'(?![a-z])', full) for t in tokens):
            return el
        if len(tokens) == 1 and tokens[0] == web: return el
    return None

def is_big_player(player, fpl_data) -> bool:
    el = find_player_in_fpl(player, fpl_data)
    if not el: return False
    return el.get("now_cost", 0) >= 65 or el.get("total_points", 0) >= 90

def fpl_team_key(el, fpl_data):
    if not el or not fpl_data: return None
    for t in fpl_data.get("teams", []):
        if t.get("id") == el.get("team"):
            return resolve_club_key((t.get("name", "") + " " + t.get("short_name", "")).lower())
    return None

# ── DEDUP / PROGRESSION ──────────────────────────────────────────────────
def _norm_text(s: str) -> str:
    s = (s or "").lower()
    s = _EMOJI_RE.sub(" ", s) if "_EMOJI_RE" in globals() else s
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def content_hash(story: dict) -> str:
    # NOTE: headline is deliberately EXCLUDED. The same player+event+direction
    # reported by different journalists with different wording is the SAME news
    # and must dedupe to the same hash. Genuine updates (stage progression or a
    # collapse) are allowed separately by should_post() via the stage/status
    # system, not by this hash. Including the headline here was the bug that let
    # the same Álvarez story post 3x with slightly different phrasings.
    parts = [
        _event_family(story.get("event")),
        _norm_text(story.get("player")),
        _norm_text(story.get("from_key") or story.get("from_club")),
        _norm_text(story.get("to_key") or story.get("to_club")),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

def is_duplicate_content(story: dict, data: dict, threshold: float = 0.82):
    pe_key = f"{_norm_text(story.get('player'))}|{_event_family(story.get('event'))}"
    # A collapse is a genuine status change, so it is NOT blocked by the normal
    # "already posted" rule — BUT it must still only post ONCE. We track posted
    # collapses separately: the first collapse for a story posts, repeats of the
    # same collapse (same player+event) are blocked. This stops the C. Jones
    # "DEAL OFF" card reposting every run.
    if story.get("collapsed"):
        if pe_key.strip("|") and pe_key in data.get("posted_collapses", []):
            return True, "collapse_already_posted"
        return False, ""
    h = content_hash(story)
    if h in data.get("posted_hashes", []): return True, "content_hash"
    # Secondary guard: same player + same event family already posted recently,
    # regardless of how the club was extracted or worded. Catches the case where
    # one journalist says "to Barcelona", another "Barça", another leaves it
    # blank — all the SAME story. Genuine progression is allowed by should_post().
    if pe_key.strip("|") and pe_key in data.get("posted_player_events", []):
        return True, "same_player_event"
    head = _norm_text(story.get("headline") or story.get("player"))
    if head:
        for prev in data.get("posted_headlines", []):
            if difflib.SequenceMatcher(None, head, prev).ratio() >= threshold:
                return True, f"fuzzy_headline>={threshold:.2f}"
    return False, ""

def record_content_dedup(story: dict, data: dict):
    h = content_hash(story)
    if h not in data.setdefault("posted_hashes", []):
        data["posted_hashes"].append(h)
    head = _norm_text(story.get("headline") or story.get("player"))
    if head and head not in data.setdefault("posted_headlines", []):
        data["posted_headlines"].append(head)
    pe_key = f"{_norm_text(story.get('player'))}|{_event_family(story.get('event'))}"
    if pe_key.strip("|") and pe_key not in data.setdefault("posted_player_events", []):
        data["posted_player_events"].append(pe_key)
    if story.get("collapsed") and pe_key.strip("|"):
        if pe_key not in data.setdefault("posted_collapses", []):
            data["posted_collapses"].append(pe_key)
    if len(data["posted_hashes"]) > 2000: data["posted_hashes"] = data["posted_hashes"][-2000:]
    if len(data["posted_headlines"]) > 2000: data["posted_headlines"] = data["posted_headlines"][-2000:]
    if len(data.get("posted_player_events", [])) > 2000: data["posted_player_events"] = data["posted_player_events"][-2000:]
    if len(data.get("posted_collapses", [])) > 2000: data["posted_collapses"] = data["posted_collapses"][-2000:]

def build_story_key(player, club_key, event) -> str:
    p = (player or "unknown").lower().replace(" ", "_")
    c = (club_key or "unknown").lower()
    fam = "injury" if event == "injury" else "manager" if event == "manager" else "transfer"
    return f"{p}_{c}_{fam}"

def _event_family(event):
    return "injury" if event == "injury" else "manager" if event == "manager" else "transfer"

def reconcile_key(player, anchor, event, *maps):
    p = (player or "unknown").lower().replace(" ", "_")
    fam = _event_family(event)
    natural = build_story_key(player, anchor, event)
    natural_is_unknown = natural.endswith(f"_unknown_{fam}")
    prefix = f"{p}_"
    suffix = f"_{fam}"
    candidates = set()
    for mp in maps:
        if not mp: continue
        for k in mp.keys():
            if k.startswith(prefix) and k.endswith(suffix):
                candidates.add(k)
    candidates.discard(natural)
    real_club_keys = [k for k in candidates if not k.endswith(f"_unknown{suffix}")]
    if natural_is_unknown and real_club_keys: return sorted(real_club_keys)[0]
    if not natural_is_unknown: return natural
    if candidates: return sorted(candidates)[0]
    return natural

def absorb_unknown_variant(player, event, canonical_key, *maps):
    p = (player or "unknown").lower().replace(" ", "_")
    fam = _event_family(event)
    unknown_key = f"{p}_unknown_{fam}"
    if unknown_key == canonical_key: return None
    for mp in maps:
        if mp and unknown_key in mp: return unknown_key
    return None

def should_post(data, key, new_stage, collapsed):
    existing = data["stories"].get(key)
    if collapsed:
        if not existing or existing["status"] == "active": return True, "collapse"
        return False, "already_collapsed"
    if not existing: return True, "new"
    if existing["status"] == "collapsed": return False, "story_collapsed"
    if new_stage <= existing["stage"]: return False, "no_progression"
    return True, "progression"

# ── SAFETY + ACCURACY GATES ──────────────────────────────────────────────
STRONG_OFFICIAL = ["here we go", "official", "confirmed", "completed", "done deal",
                   "sealed", "unveiled", "joins", "joined", "signs", "signed", "medical"]

def detect_mixed_story(story, raw_text) -> str:
    text = (raw_text or "")
    tl = text.lower()
    player = (story.get("player") or "").lower()
    ev = story.get("event")
    if ev != "manager" and player and (player in MANAGER_SURNAMES or any(m in player for m in MANAGER_SURNAMES)):
        return "player_is_manager"
    if ev in ("transfer", "loan", "loan_option", "stay", "renewal"):
        manager_in_text = any(re.search(r'(?<![a-z])' + re.escape(m) + r'(?![a-z])', tl) for m in MANAGER_SURNAMES)
        subject_is_manager = any(m in player for m in MANAGER_SURNAMES)
        if manager_in_text and not subject_is_manager: return "manager_and_player_mixed"
    clauses = re.split(r'[.;]|\bmeanwhile\b|\balso\b|\bplus\b|\belsewhere\b|\bseparately\b', tl)
    clubbed_clauses = 0
    for c in clauses:
        if any(re.search(r'(?<![a-z])' + re.escape(a) + r'(?![a-z])', c) for a in _SORTED_ALIASES):
            clubbed_clauses += 1
    if clubbed_clauses >= 4: return "multiple_stories_suspected"
    if ev in ("transfer", "loan", "loan_option"):
        if re.search(r'\bno\s+(move|exit|transfer|deal)\b', tl) or \
           re.search(r'\b(not?|never)\s+(?:moving|leaving|for sale)\b', tl):
            dests = sum(1 for a in _SORTED_ALIASES if re.search(r'(?<![a-z])' + re.escape(a) + r'(?![a-z])', tl))
            if dests >= 1: return "negated_move_misread_as_transfer"
    name_candidates = set()
    for mm in re.findall(r'\b([A-Z][a-zà-ÿ]+(?:\s+[A-Z][a-zà-ÿ]+){1,2})\b', text):
        low = mm.lower()
        if any(m in low for m in MANAGER_SURNAMES): continue
        if looks_like_club(low): continue
        name_candidates.add(low)
    distinct = set()
    for n in sorted(name_candidates, key=len, reverse=True):
        if not any(n != o and n in o for o in distinct): distinct.add(n)
    if len(distinct) >= 2:
        coordinated = bool(re.search(r'\b[A-Z][a-zà-ÿ]+ [A-Z][a-zà-ÿ]+\s+(?:and|&)\s+[A-Z][a-zà-ÿ]+ [A-Z][a-zà-ÿ]+', text))
        if coordinated or len(distinct) >= 3: return "multiple_players_suspected"
    return ""

def player_already_at_club(story, fpl_data) -> bool:
    if story.get("event") not in ("transfer", "loan", "loan_option"): return False
    el = find_player_in_fpl(story.get("player"), fpl_data)
    cur = fpl_team_key(el, fpl_data)
    to_key = story.get("to_key")
    if el is None or cur is None: return False
    return bool(cur and to_key and cur == to_key)

def passes_safety_gate(story, raw_text, fpl_data, sources=None):
    sources = sources or []
    tl = (raw_text or "").lower()
    NON_NEWS_KW = ["documentary", "amazon prime", "netflix", "man of the match",
                   "potm", "player of the month", "kit launch", "new kit", "sponsor",
                   "anniversary", "birthday", "wins the", "award", "fifa the best",
                   "ballon d'or", "merch", "video game", "ea sports"]
    if any(k in tl for k in NON_NEWS_KW): return False, "off_topic_content"

    # OPINION / INTERVIEW / QUOTE GUARD — blocks "Mbappé praises Olise",
    # "Messi feels good, not focused on age", punditry and reaction tweets that
    # mention a player but report NO transfer/injury/suspension/manager EVENT.
    # These were being forced into bogus TRANSFER RUMOUR cards by the regex
    # fallback. A tweet is opinion/quote noise if it contains an opinion verb
    # AND lacks any hard move/availability signal.
    OPINION_MARKERS = [
        "praise", "praises", "praised", "hails", "hailed", "admits", "admit",
        "feels", "feeling", "insists", "believes", "reacts", "reaction",
        "speaks", "speaks out", "responds", "slams", "criticis", "warns",
        "says he", "happy at", "loves", "enjoying", "not focused", "focused on",
        "wants to win", "dreams of", "favourite", "best in the world",
        "interview", "exclusive interview", "opens up", "reveals he",
        "physically good", "in great shape", "confident", "motivated",
    ]
    HARD_NEWS_SIGNAL = [
        "sign", "signing", "signed", "joins", "joined", "join", "deal", "fee",
        "bid", "transfer", "medical", "here we go", "loan", "contract",
        "agreement", "agreed", "personal terms", "move to", "set to join",
        "close to", "ruled out", "injury", "injured", "hamstring", "scan",
        "surgery", "suspended", "suspension", "banned", "red card", "sent off",
        "sack", "appoint", "head coach", "new manager", "completed",
    ]
    has_opinion = any(k in tl for k in OPINION_MARKERS)
    has_hard_news = any(k in tl for k in HARD_NEWS_SIGNAL)
    if has_opinion and not has_hard_news:
        return False, "opinion_or_quote_no_event"

    if not story.get("is_football"): return False, "not_football"
    # No recognizable event (not transfer/injury/suspension/manager/loan/etc.)
    # means this isn't postable news — e.g. a press quote or match reaction that
    # merely mentions a player and a team. Skip rather than force a card.
    if story.get("event") == "other": return False, "no_event_type"
    if tweet_too_old(story.get("created_at")): return False, f"older_than_{MAX_TWEET_AGE_DAYS}d"
    if story.get("historical") and not ALLOW_HISTORICAL_POSTS: return False, "historical_news"
    if story.get("confidence", 0) < 0.45: return False, "low_confidence"
    if any(re.search(r'(?<![a-z])' + re.escape(w) + r'(?![a-z])', tl) for w in STAFF_BLOCK_KW): return False, "staff_or_offpitch"
    if not story.get("player"): return False, "no_player"
    mixed = detect_mixed_story(story, raw_text)
    if mixed: return False, f"mixed_story:{mixed}"
    if story.get("from_video") and not story.get("has_written_claim"): return False, "video_no_written_claim"
    if player_already_at_club(story, fpl_data): return False, "already_at_destination"

    if story["event"] == "manager":
        to_key = story.get("to_key")
        to_club = story.get("to_club")
        pl_club = bool(to_key) or (to_club and to_club.lower() in PL_CLUB_NAMES)
        if not (pl_club or is_bundesliga_or_laliga_club(to_club)): return False, "manager_no_club"
        if story.get("from_fallback"):
            appoint_cue = re.search(
                r"\b(appoint|appointed|new (head coach|manager|boss)|"
                r"set to (become|take over|be appointed)|sacked|"
                r"named (as )?(head coach|manager)|takes over|"
                r"agree(s|d)? to (become|join)|done deal)\b", tl)
            if not appoint_cue: return False, "manager_no_appointment_cue"
        return True, "ok_manager"

    if story["event"] == "injury":
        tiers = [source_tier(s) for s in sources]
        injury_source_ok = any(t in (1, 2) for t in tiers) or \
            any((s or "").lower().lstrip("@") in OFFICIAL_INJURY_ACCOUNTS for s in sources)
        if not injury_source_ok: return False, "injury_source_not_approved"
        pl_player = find_player_in_fpl(story["player"], fpl_data) is not None
        bl_club_in_text = any(c in tl for c in (BUNDESLIGA_BIG_CLUBS | LA_LIGA_BIG_CLUBS))
        if pl_player or bl_club_in_text: return True, "ok_injury"
        return False, "injury_not_pl_bundesliga_laliga"

    pl_player = find_player_in_fpl(story["player"], fpl_data) is not None
    pl_club = bool(story.get("to_key") or story.get("from_key"))
    if not pl_club:
        for nm in (story.get("to_club"), story.get("from_club")):
            if nm and nm.lower() in PL_CLUB_NAMES:
                pl_club = True
                break
    if pl_player or pl_club: return True, "ok"
    big_player = is_big_player(story["player"], fpl_data) or is_big_name_player(story["player"])
    big_club = is_big_club_name(story.get("to_club")) or is_big_club_name(story.get("from_club"))
    if big_player or big_club: return True, "ok_big_name"
    # UNLOCK: elite source BUT the named club must be a PL or elite European
    # club — not just ANY club. This is what stops obscure lower-league moves
    # (e.g. Séverin Nioule → Sporting Charleroi) being posted just because a
    # tier-2 reporter mentioned them. FPL relevance requires a relevant club.
    tiers = [source_tier(s) for s in (sources or [])]
    elite_source = any(t == 2 for t in tiers)
    relevant_club = False
    for nm in (story.get("to_club"), story.get("from_club")):
        if nm and (nm.lower() in PL_CLUB_NAMES or is_big_club_name(nm)
                   or is_bundesliga_or_laliga_club(nm)):
            relevant_club = True
            break
    if bool(story.get("to_key") or story.get("from_key")):
        relevant_club = True  # any resolved key is a PL club
    if elite_source and relevant_club: return True, "ok_elite_source"
    return False, "not_fpl_relevant"

def classify_post(story, sources):
    if story.get("collapsed"): return "rumour"
    tiers = [source_tier(s) for s in sources]
    has_official = 1 in tiers
    n_elite = sum(1 for t in tiers if t == 2)
    has_media = 3 in tiers
    tl = (story.get("body", "") + " " + (story.get("headline", "") or "")).lower()
    strong_words = story["stage"] >= 4 or any(re.search(r'\b' + re.escape(w) + r'\b', tl) for w in STRONG_OFFICIAL)

    if story["event"] == "injury":
        if has_official or n_elite >= 1: return "confirmed"
        return None

    trusted_strong = strong_words and (has_official or n_elite >= 1)
    video_only = story.get("from_video") and not has_official
    if (has_official or trusted_strong or n_elite >= 2) and not video_only: return "confirmed"
    if n_elite >= 1: return "rumour"
    if n_elite >= 1: return "rumour"
    if has_media: return "rumour"
    return None

def validate_story(story, fpl_data=None):
    ev = story.get("event")
    player = (story.get("player") or "").strip()
    if not player: return False, "missing_player"
    _ptokens = [t for t in re.split(r"[\s\-']+", player) if t]
    _plow = player.lower()
    if ev != "manager" and (_plow in MANAGER_SURNAMES or any(m in _plow for m in MANAGER_SURNAMES)): return False, "player_is_manager_name"
    if ev == "manager" and len(_ptokens) < 2: return False, "manager_name_single_token"
    if ev in ("transfer", "loan", "loan_option", "injury", "suspension", "renewal", "stay") and len(_ptokens) < 2: return False, "player_name_single_token"
    if re.search(r"\b(under|u\d{1,2}|u-\d{1,2})$", _plow): return False, "player_name_truncated_fragment"

    PLACEHOLDERS = ("player name", "example", "xxx", "[", "]", "tbd", "to follow",
                    "lorem", "duration & details", "updated heading", "from club", "to club")
    blob = " ".join(str(story.get(k, "") or "") for k in
                    ("player", "headline", "body", "from_club", "to_club", "fee",
                     "contract", "conditional", "diagnosis", "expected_return")).lower()
    for ph in PLACEHOLDERS:
        if ph in blob: return False, f"placeholder_text:{ph!r}"
    if looks_like_club(player): return False, "player_is_club"
    if re.search(r'\bRT\s+@|@\w+|https?://', story.get("body", "")): return False, "raw_source_text_in_body"
    if player_already_at_club(story, fpl_data): return False, "already_at_destination"
    if ev in ("transfer", "loan", "loan_option"):
        fk = story.get("from_key"); tk = story.get("to_key")
        fc = (story.get("from_club") or "").strip().lower()
        tc = (story.get("to_club") or "").strip().lower()
        if (fk and tk and fk == tk) or (fc and tc and fc == tc): return False, "from_equals_to"
        if not (tk or story.get("to_club") or fk or story.get("from_club")): return False, "no_clubs"
        if story.get("from_fallback") and not story.get("direction_confident"): return False, "direction_unconfident"
        leak = (story.get("body", "") + " " + story.get("headline", "")).lower()
        if re.search(r'\b(head coach|sacked|appointed as manager|hamstring|ruled out for)\b', leak): return False, "event_data_mismatch"
    if ev == "manager" and not (story.get("to_key") or story.get("to_club")): return False, "manager_no_club"

    # ENTITY COHERENCE (generalized, no name rules): if the detected player
    # resolves in FPL, his REAL current club should be consistent with the
    # story. For a transfer, his current club should be the from-side (or the
    # to-side if already moved). If FPL says he plays for a club that is NEITHER
    # the from nor the to club, the player or a club was mis-detected — skip
    # rather than post a wrong pairing. Only applies when we have hard FPL data
    # AND a resolved opposing club, so it never fires on legitimately unknown data.
    if ev in ("transfer", "loan", "loan_option") and fpl_data is not None:
        el = find_player_in_fpl(player, fpl_data)
        cur = fpl_team_key(el, fpl_data) if el else None
        fk = story.get("from_key"); tk = story.get("to_key")
        if cur and (fk or tk) and cur != fk and cur != tk:
            # The player's real club matches neither side of the reported move.
            return False, "player_club_mismatch"
    return True, "ok"

# ── LABELS ───────────────────────────────────────────────────────────────
APPROVED_LABELS = {
    "TRANSFER", "RUMOUR", "INJURY", "SUSPENSION", "CONTRACT EXTENSION",
    "LOAN", "MANAGER NEWS", "OFFICIAL", "HISTORICAL",
}
EVENT_PREFIX = {
    "transfer": "TRANSFER", "loan": "LOAN", "loan_option": "LOAN",
    "renewal": "CONTRACT EXTENSION", "stay": "CONTRACT EXTENSION",
    "injury": "INJURY", "suspension": "SUSPENSION", "manager": "MANAGER NEWS",
    "collapse": "TRANSFER",
}

def status_label(story, mode):
    if story.get("historical"): return "HISTORICAL"
    if story.get("collapsed"): return "RUMOUR"
    if mode == "rumour": return "RUMOUR"
    ev = story.get("event")
    tl = (story.get("body", "") + " " + (story.get("headline", "") or "")).lower()
    if ev in ("transfer", "loan", "loan_option") and (
            story.get("stage", 1) >= 4 or any(w in tl for w in ("official", "here we go", "completed", "confirmed"))):
        return "OFFICIAL"
    label = EVENT_PREFIX.get(ev)
    return label if label in APPROVED_LABELS else None

# ── HASHTAGS ─────────────────────────────────────────────────────────────
BASE_TAGS = ["#FPLVortex"]

def build_hashtags(story):
    """SEO hashtags, ordered most-relevant first, capped at 4 so the caption
    stays short for non-premium X. Order: club tag(s) first (highest search
    value), then the event-type tag, then #PremierLeague, then brand. Generic
    #FootballNews is only used when nothing more specific applies."""
    ev = story.get("event")
    club_tags = []
    for key, name in ((story.get("to_key"), story.get("to_club")),
                      (story.get("from_key"), story.get("from_club"))):
        ht = hashtag_for(key) or hashtag_for(name)
        if ht and ht not in club_tags:
            club_tags.append(ht)

    if ev in ("injury", "suspension"):
        event_tag = "#InjuryNews"
    elif ev in ("transfer", "loan", "loan_option", "renewal", "stay"):
        event_tag = "#Transfers"
    elif ev == "manager":
        event_tag = "#PremierLeague"  # manager moves index better under the league tag
    else:
        event_tag = "#FootballNews"

    is_pl = bool(story.get("to_key") or story.get("from_key"))
    ordered = []
    # 1) club tags (best search value), 2) event tag, 3) league tag, 4) brand
    for t in club_tags:
        if t not in ordered: ordered.append(t)
    if event_tag not in ordered: ordered.append(event_tag)
    if is_pl and "#PremierLeague" not in ordered: ordered.append("#PremierLeague")
    if "#FPLVortex" not in ordered: ordered.append("#FPLVortex")
    return " ".join(ordered[:4])

# ── TWEET TEXT ───────────────────────────────────────────────────────────
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
    if twitter_len(body) <= limit: return body
    parts = body.rsplit("\n\n", 1)
    if len(parts) == 2 and parts[1].strip().startswith("#"):
        head, tags = parts[0], parts[1].split()
        while tags and twitter_len(head + "\n\n" + " ".join(tags)) > limit: tags.pop()
        cand = head + ("\n\n" + " ".join(tags) if tags else "")
        if twitter_len(cand) <= limit: return cand
        body = head
    out = ""
    for ch in body:
        if twitter_len(out + ch) > limit - 1: break
        out += ch
    return out.rstrip() + "…"

def build_tweet_body(story, sources, mode) -> str:
    label = status_label(story, mode)
    head = story.get("headline") or story.get("player") or "Update"
    # Concise, non-redundant prefix. The old "Unconfirmed report RUMOUR | ..."
    # repeated itself; a single clear tag per story type reads cleaner and
    # leaves more room within X's non-premium character limit.
    ev = story.get("event")
    if story.get("collapsed"):
        prefix = "DEAL OFF"
    elif mode == "rumour":
        prefix = "RUMOUR"
    elif label and label != "RUMOUR":
        prefix = label            # OFFICIAL / INJURY / SUSPENSION / LOAN / etc.
    else:
        prefix = (EVENT_PREFIX.get(ev) or "UPDATE")
    first_line = f"{prefix} | {head}"
    body = first_line + "\n\n" + build_hashtags(story)
    return body

def build_detail_line(story) -> str:
    bits = []
    if story.get("fee"): bits.append(story["fee"])
    if story.get("contract"): bits.append(story["contract"])
    if story.get("conditional"): bits.append(story["conditional"])
    return "  |  ".join(bits)

# ── GRAPHICS ENGINE ──────────────────────────────────────────────────────
_FONT_CACHE = {}
_FALLBACK_FONTS = {
    "Black": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
    "Bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
}

def _load_fallback(size, weight):
    for path in _FALLBACK_FONTS.get(weight, _FALLBACK_FONTS["Bold"]):
        if os.path.exists(path):
            try: return ImageFont.truetype(path, size)
            except Exception: continue
    return ImageFont.load_default()

def get_premium_font(size, weight="Bold"):
    key = (weight, size)
    if key in _FONT_CACHE: return _FONT_CACHE[key]
    fp = f"Montserrat-{weight}.ttf"
    if not os.path.exists(fp):
        try:
            url = f"https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-{weight}.ttf"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r, open(fp, "wb") as out:
                out.write(r.read())
        except Exception:
            f = _load_fallback(size, weight)
            _FONT_CACHE[key] = f
            return f
    try: f = ImageFont.truetype(fp, size)
    except Exception: f = _load_fallback(size, weight)
    _FONT_CACHE[key] = f
    return f

def _download_asset(url, dest: Path) -> bool:
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200: return False
            data = resp.read()
            if not data: return False
        with open(tmp, "wb") as f: f.write(data)
        tmp.replace(dest)
        return True
    except Exception:
        try: tmp.exists() and tmp.unlink()
        except Exception: pass
        return False

def _safe_open_rgba(path: Path):
    try:
        im = Image.open(path)
        im.load()
        return im.convert("RGBA")
    except Exception:
        try: path.unlink()
        except Exception: pass
        return None

def _fit_contain(im, w, h):
    return ImageOps.contain(im, (w, h), Image.Resampling.LANCZOS)

def _draw_text_shadow(draw, xy, text, font, fill, shadow=(0, 0, 0), offset=2):
    x, y = xy
    draw.text((x + offset, y + offset), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=fill)

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2190-\u21FF\u2B00-\u2BFF\uFE0F]")

def _safe_emoji_text(img, xy, text, font, fill):
    try:
        with Pilmoji(img) as pj:
            pj.text(xy, text, font=font, fill=fill)
    except Exception:
        plain = _EMOJI_RE.sub("", text).strip()
        ImageDraw.Draw(img).text(xy, plain, font=font, fill=fill)

def _photo_verified(player_el, fpl, from_key, to_key) -> bool:
    if not player_el: return False
    cur = fpl_team_key(player_el, fpl)
    if cur is None: return False
    return cur == from_key or cur == to_key

def _load_crest(club_key, box=120):
    if not club_key: return None
    safe = club_key.replace(" ", "_").replace("'", "")
    p = Path(f"logos/{safe}.png")
    if not p.exists() and FPL_LOGO_IDS.get(safe):
        _download_asset(f"https://resources.premierleague.com/premierleague/badges/t{FPL_LOGO_IDS[safe]}.png", p)
    if p.exists():
        src = _safe_open_rgba(p)
        if src is not None: return _fit_contain(src, box, box)
    return None

def _draw_diagonal_accents(img, accent, gold=(212, 175, 55)):
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

_LOGO_CACHE = {"img": None, "loaded": False}

def _load_brand_logo(box=72):
    """Load the channel logo from the repo if present. Tries common filename/
    extension variants so a small naming mismatch doesn't break branding.
    Returns an RGBA image fitted into a `box`x`box` square, or None."""
    if _LOGO_CACHE["loaded"]:
        src = _LOGO_CACHE["img"]
        return _fit_contain(src, box, box) if src is not None else None
    _LOGO_CACHE["loaded"] = True
    for name in ("logo.png", "Logo.png", "logo.jpg", "Logo.jpg",
                 "logo.jpeg", "Logo.jpeg", "assets/logo.png", "assets/logo.jpg"):
        p = Path(name)
        if p.exists():
            src = _safe_open_rgba(p)
            if src is not None:
                _LOGO_CACHE["img"] = src
                return _fit_contain(src, box, box)
    _LOGO_CACHE["img"] = None
    return None

def _draw_wordmark(draw, xy, img=None):
    """Brand lockup: logo (if available) on the left, then 'FPL VORTEX' text.
    Falls back to text-only when no logo file is present, so it never breaks."""
    x, y = xy
    logo = _load_brand_logo(box=64) if img is not None else None
    if logo is not None:
        # vertically centre the logo against the ~46px cap height of the text
        ly = y - (logo.height - 46) // 2
        img.paste(logo, (x, max(0, ly)), logo)
        x += logo.width + 16
    f = get_premium_font(46, "Black")
    _draw_text_shadow(draw, (x, y), "FPL", f, (255, 255, 255), offset=2)
    fpl_w = draw.textlength("FPL ", font=f)
    _draw_text_shadow(draw, (x + fpl_w, y), "VORTEX", f, (84, 224, 124), offset=2)

def _draw_right_visual_fallback(img, draw, W, H, story):
    """Right-side visual when no verifiable player photo exists.
    Priority: big destination/origin club crest -> glowing V emblem.
    Crests come from the FPL badge API (PL clubs only); non-PL clubs fall
    through to the emblem rather than show a wrong/placeholder badge."""
    crest_key = story.get("to_key") or story.get("from_key")
    big_crest = _load_crest(crest_key, box=420) if crest_key else None
    if big_crest is not None:
        zone_left = 820
        zone_cx = zone_left + (W - zone_left) // 2
        zone_cy = (H - 90) // 2 + 40
        for r in range(int(big_crest.width * 0.62), 20, -60):
            draw.ellipse([zone_cx - r, zone_cy - r, zone_cx + r, zone_cy + r],
                         outline=(255, 255, 255, 12), width=2)
        img.paste(big_crest,
                  (zone_cx - big_crest.width // 2, zone_cy - big_crest.height // 2),
                  big_crest)
        return
    cx, cy = W - 320, H // 2 - 20
    for r in range(220, 20, -50):
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255, 15), width=2)
    f_emblem = get_premium_font(160, "Black")
    draw.text((cx - 60, cy - 100), "V", font=f_emblem, fill=(84, 224, 124, 50))

def _create_fallback_card(story, sources, filename):
    W, H = 1200, 675
    img = Image.new("RGB", (W, H), (11, 18, 32))
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rectangle([0, 0, W, 12], fill=(212, 175, 55))
    draw.rectangle([0, H - 12, W, H], fill=(212, 175, 55))
    _draw_wordmark(draw, (60, 48))
    lf = get_premium_font(40, "Bold")
    label = "BREAKING NEWS"
    draw.rounded_rectangle([60, 130, 60 + draw.textlength(label, font=lf) + 44, 192],
                           radius=12, fill=(210, 30, 34))
    _draw_text_shadow(draw, (60 + 22, 138), label, lf, (255, 255, 255), offset=2)
    head = (story.get("headline") or story.get("player") or "Football update").upper()
    hf = get_premium_font(64, "Black")
    words, line, y = head.split(), "", 250
    for w in words:
        test = (line + " " + w).strip()
        if draw.textlength(test, font=hf) > W - 120 and line:
            _draw_text_shadow(draw, (60, y), line, hf, (255, 255, 255), offset=3)
            y += 78
            line = w
        else: line = test
    if line: _draw_text_shadow(draw, (60, y), line, hf, (255, 255, 255), offset=3)
    src = " · ".join(f"@{s}" for s in (sources or [])[:2]) or CHANNEL_HANDLE
    draw.rectangle([0, H - 78, W, H - 12], fill=(20, 24, 33))
    bf = get_premium_font(30, "Bold")
    draw.text((60, H - 64), f"Source: {src}  |  {CHANNEL_HANDLE}", font=bf, fill=(190, 200, 220))
    img.save(filename)

# ── UNIFIED PLAYER CARD (Doku-style, image LEFT) ─────────────────────────
_LBL_RED = (227, 30, 36)
_LBL_GREEN = (84, 224, 124)

def _draw_player_silhouette(img, draw, cx, top, box_w, box_h):
    """Built-in head-and-shoulders silhouette IMAGE used only when neither an
    FPL headshot nor a journalist photo is available. Keeps a player-shaped
    visual in the exact image-slot position so the panel is never empty and the
    card never looks broken — no text/emblem fallback."""
    size = min(box_w, box_h)
    # soft rounded backdrop inside the slot
    pad = int(size * 0.06)
    draw.rounded_rectangle([cx - box_w // 2 + pad, top + pad,
                            cx + box_w // 2 - pad, top + box_h - pad],
                           radius=int(size * 0.10), fill=(26, 38, 64, 255))
    fill = (150, 168, 196, 255)
    # head
    head_r = int(size * 0.16)
    head_cy = top + int(box_h * 0.30)
    draw.ellipse([cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r], fill=fill)
    # shoulders dome (ellipse rising from the slot bottom)
    sw = int(size * 0.36)
    sh_top = top + int(box_h * 0.50)
    sh_bot = top + box_h - pad
    draw.ellipse([cx - sw, sh_top, cx + sw, sh_bot], fill=fill)

def _label_lines(story, mode):
    """Two-line badge: TOP = category (red), BOTTOM = status (green).
    Injury / suspension / historical render as a single red line.
    NOTE: 'manager' is interpreted here as MANAGER / NEWS; adjust if you
    meant the (garbled) 'Transfer STAFF' wording for staff/official moves."""
    ev = story.get("event")
    if story.get("historical"):
        return ("HISTORICAL", _LBL_RED, None, None)
    if story.get("collapsed"):
        return ("TRANSFER", _LBL_RED, "OFF", _LBL_GREEN)
    if ev == "injury":
        return ("INJURY", _LBL_RED, None, None)
    if ev == "suspension":
        return ("SUSPENSION", _LBL_RED, None, None)
    if ev == "manager":
        return ("MANAGER", _LBL_RED, "NEWS", _LBL_GREEN)
    if ev in ("renewal", "stay"):
        return ("CONTRACT", _LBL_RED, "EXTENSION", _LBL_GREEN)
    top = "LOAN" if ev in ("loan", "loan_option") else "TRANSFER"
    if mode == "rumour":
        return (top, _LBL_RED, "RUMOUR", _LBL_GREEN)
    tl = (story.get("body", "") + " " + (story.get("headline", "") or "")).lower()
    if story.get("stage", 1) >= 4 or any(
            w in tl for w in ("official", "here we go", "completed", "confirmed")):
        return (top, _LBL_RED, "OFFICIAL", _LBL_GREEN)
    return (top, _LBL_RED, "CONFIRMED", _LBL_GREEN)


def _pretty_club(s):
    """Title-case lowercase club strings but preserve acronyms (PSG, MCFC)
    and already-cased names (Real Madrid, Man City)."""
    s = (s or "").strip()
    return s.title() if s.islower() else s

def create_player_card(story, sources, filename, mode="confirmed"):
    """One card for EVERY post type. RIGHT = framed (crest on top + headshot
    below), Doku-style. LEFT = two-line red/green label, name, type info.

    Framed image slot, in priority order:
      1. FPL 250x250 head-and-shoulders mugshot (current PL players only)
      2. the source tweet's own image (journalist photo) if no FPL mugshot
      3. a clean emblem so the layout never breaks
    """
    W, H = 1380, 776
    fpl = fetch_fpl_data()
    player_el = find_player_in_fpl(story.get("player"), fpl)
    player_name = (player_el["web_name"] if player_el else story.get("player")) or "PLAYER"
    to_key = story.get("to_key"); from_key = story.get("from_key")
    ev = story.get("event", "transfer"); collapsed = bool(story.get("collapsed"))

    NAVY = (11, 18, 32); GOLD = (212, 175, 55)
    # Club-colored accent (option c): use the destination (then origin) club's
    # brand colour so each card feels team-specific. Collapsed deals stay red.
    # Falls back to the default blue when no club colour is known.
    club_accent = CLUB_COLORS.get(to_key) or CLUB_COLORS.get(from_key)
    if collapsed:
        accent = (120, 30, 34)
    elif club_accent:
        # darken slightly so white text stays readable over the accent stripes
        accent = tuple(max(0, int(c * 0.72)) for c in club_accent)
    else:
        accent = (30, 55, 110)
    img = Image.new("RGB", (W, H), NAVY)
    sheen = Image.new("L", (1, H), 0)
    for yy in range(H):
        sheen.putpixel((0, yy), int(30 * (1 - abs(yy - H / 2) / (H / 2))))
    img.paste(Image.new("RGB", (W, H), (28, 40, 70)), (0, 0), sheen.resize((W, H)))
    _draw_diagonal_accents(img, accent, GOLD)
    draw = ImageDraw.Draw(img, "RGBA")

    # ---------- RIGHT: crest badge (top) + LARGE photo filling the panel ----
    FX0, FY0, FX1, FY1 = 840, 150, 1320, H - 112
    draw.rounded_rectangle([FX0, FY0, FX1, FY1], radius=26,
                           fill=(17, 26, 44), outline=(255, 255, 255, 32), width=3)
    fcx = (FX0 + FX1) // 2

    # team crest at the TOP of the image area (kept big + clear)
    crest_key = to_key or from_key
    crest = _load_crest(crest_key, box=118) if crest_key else None
    crest_top = FY0 + 20
    crest_bottom = crest_top + (crest.height if crest is not None else 96)
    if crest is not None:
        img.paste(crest, (fcx - crest.width // 2, crest_top), crest)

    # LARGE portrait photo box that fills the rest of the panel
    PB_W = 372
    pbx0 = fcx - PB_W // 2
    pby0 = crest_bottom + 14
    pby1 = FY1 - 18
    pbx1 = pbx0 + PB_W
    pb_h = pby1 - pby0

    LEGENDS = {"harry kane": "78830"}
    legend_pid = LEGENDS.get(player_name.lower())
    photo = None

    # MISLEADING-KIT GUARD (logic, not hardcoding): the FPL photo shows the
    # player in his CURRENT club's kit. For an unconfirmed rumour where his
    # current club is NOT part of this story, that kit can imply a move that
    # hasn't happened (e.g. Fernandes in a Southampton shirt on a Man Utd card).
    # So we only use the player photo when it can't mislead:
    #   - the move is CONFIRMED/OFFICIAL (mode != rumour, not collapsed), OR
    #   - the player's current club matches a club in THIS story (verified).
    # Otherwise we skip it and fall back to a neutral destination-crest visual.
    confirmed = (mode != "rumour") and not collapsed
    photo_safe = confirmed or _photo_verified(player_el, fpl, from_key, to_key)
    legend_ok = bool(legend_pid)  # curated legends are always correct

    if (player_el and photo_safe) or legend_ok:
        pid = legend_pid or (player_el.get("code") if player_el else None)
        if pid:
            pp = Path(f"players/{pid}.png")
            if not pp.exists():
                _download_asset(f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{pid}.png", pp)
            photo = _safe_open_rgba(pp)
    # Tweet's own image is a safe, real photo of the event — use if no FPL photo.
    if photo is None and story.get("media_url"):
        tp = Path(f"players/tweet_{story.get('id')}.jpg")
        if not tp.exists():
            _download_asset(story["media_url"], tp)
        photo = _safe_open_rgba(tp)

    if photo is not None:
        # COVER-fit: fill the box edge-to-edge, center-crop with a slight upward
        # bias so faces are kept; round the corners to match the panel.
        filled = ImageOps.fit(photo, (PB_W, pb_h), Image.Resampling.LANCZOS,
                              centering=(0.5, 0.38)).convert("RGBA")
        round_mask = Image.new("L", (PB_W, pb_h), 0)
        ImageDraw.Draw(round_mask).rounded_rectangle([0, 0, PB_W, pb_h], radius=20, fill=255)
        paste_mask = ImageChops.multiply(filled.getchannel("A"), round_mask)
        img.paste(filled, (pbx0, pby0), paste_mask)
    else:
        # NEUTRAL FALLBACK: prefer a large destination/origin crest (never
        # misleading) centred in the photo panel; only if no crest is available
        # do we draw the generic player silhouette.
        big_crest = _load_crest(to_key or from_key, box=min(PB_W, pb_h) - 30)
        if big_crest is not None:
            img.paste(big_crest,
                      (fcx - big_crest.width // 2, pby0 + (pb_h - big_crest.height) // 2),
                      big_crest)
        else:
            _draw_player_silhouette(img, draw, fcx, pby0, PB_W, pb_h)

    # ---------- LEFT: wordmark, label, name, info ----------
    LX = 70
    _draw_wordmark(draw, (LX, 54), img=img)
    top_text, top_col, bot_text, bot_col = _label_lines(story, mode)
    lf = get_premium_font(66, "Black"); ly = 152
    _draw_text_shadow(draw, (LX, ly), top_text, lf, top_col, offset=2)
    lb = draw.textbbox((0, 0), top_text, font=lf); lh = lb[3] - lb[1]
    if bot_text:
        ly2 = ly + lh + 18
        _draw_text_shadow(draw, (LX, ly2), bot_text, lf, bot_col, offset=2)
        name_y = ly2 + lh + 40
    else:
        name_y = ly + lh + 40
    name_up = player_name.upper()
    NAME_MAX_W = FX0 - LX - 40
    nsize = 86; nf = get_premium_font(nsize, "Black")
    while draw.textlength(name_up, font=nf) > NAME_MAX_W and nsize > 44:
        nsize -= 3; nf = get_premium_font(nsize, "Black")
    _draw_text_shadow(draw, (LX, name_y), name_up, nf, (255, 255, 255), offset=3)
    nb = draw.textbbox((0, 0), name_up, font=nf)
    y = name_y + (nb[3] - nb[1]) + 34
    info_f = get_premium_font(38, "Bold"); sub_f = get_premium_font(27, "Bold")
    INFO_MAX_X = FX0 - 30  # never draw past the image panel's left edge

    def _info_row(label, value, col=(255, 255, 255)):
        nonlocal y
        if not value: return
        value = str(value)
        _draw_text_shadow(draw, (LX, y + 4), label, sub_f, (150, 165, 195))
        vx = LX + draw.textlength(label + "   ", font=sub_f)
        avail = max(120, INFO_MAX_X - vx)
        # shrink value font until even the longest single word fits the slot
        vsize, vf = 38, info_f
        while vsize > 22 and max(
                (draw.textlength(w, font=vf) for w in value.split()), default=0) > avail:
            vsize -= 2
            vf = get_premium_font(vsize, "Bold")
        # wrap onto lines within `avail`
        words, line, lines = value.split(), "", []
        for w in words:
            test = (line + " " + w).strip()
            if draw.textlength(test, font=vf) > avail and line:
                lines.append(line); line = w
            else:
                line = test
        if line: lines.append(line)
        truncated = len(lines) > 3
        lines = lines[:3]  # cap height; merge the rest into an ellipsis
        if truncated and lines:
            lines[-1] = lines[-1].rstrip() + "…"
        bb = draw.textbbox((0, 0), "Ag", font=vf)
        lh = (bb[3] - bb[1]) + 6
        for i, ln in enumerate(lines):
            _draw_text_shadow(draw, (vx, y + i * lh), ln, vf, col)
        y += max(60, len(lines) * lh + 8)
    if ev == "injury":
        stage = story.get("stage", 1)
        avail = {4: "Fit again", 3: "Ruled out", 2: "Doubt", 1: "Being assessed"}.get(stage, "Being assessed")
        _info_row("STATUS", avail); _info_row("DIAGNOSIS", story.get("diagnosis"))
        _info_row("RETURN", story.get("expected_return"))
    elif ev == "manager":
        tc = story.get("to_club") or (to_key or "").replace("_", " ")
        _info_row("CLUB", _pretty_club(tc) if tc else None)
    else:
        if collapsed:
            _info_row("DEAL", "Collapsed", col=(255, 120, 120))
        else:
            fc = story.get("from_club") or (from_key or "").replace("_", " ")
            tc = story.get("to_club") or (to_key or "").replace("_", " ")
            _info_row("FROM", _pretty_club(fc) if fc else None)
            _info_row("TO", _pretty_club(tc) if tc else None)
        _info_row("PRICE", story.get("fee")); _info_row("CONTRACT", story.get("contract"))

    # ---------- source bar ----------
    draw.rectangle([0, H - 90, W, H - 12], fill=(20, 24, 33))
    draw.rectangle([0, H - 12, W, H], fill=accent)
    src = " · ".join(f"@{s}" for s in sources[:2])
    bar = f"Source: {src}  |  {CHANNEL_HANDLE}"
    # Date string in the footer's own colour (gold), right-aligned. Uses the
    # tweet's publish date when known, otherwise the date the card is made.
    date_str = _card_date_str(story.get("created_at"))
    df = get_premium_font(30, "Bold")
    dw = draw.textlength(date_str, font=df)
    bsize = 34; bf = get_premium_font(bsize, "Bold")
    # leave room for the date on the right so the two never collide
    avail_w = (W - 120) - dw - 30
    while bsize > 24 and draw.textlength(bar, font=bf) > avail_w:
        bsize -= 1; bf = get_premium_font(bsize, "Bold")
    bb = draw.textbbox((0, 0), bar, font=bf)
    by = (H - 90) + (78 - (bb[3] - bb[1])) // 2 - bb[1]
    draw.text((60, by), bar, font=bf, fill=(190, 200, 220))
    db = draw.textbbox((0, 0), date_str, font=df)
    dy = (H - 90) + (78 - (db[3] - db[1])) // 2 - db[1]
    draw.text((W - 60 - dw, dy), date_str, font=df, fill=(212, 175, 55))
    img.save(filename)


def create_image(story, sources, filename, rumour=False):
    """Unified entry point. Every post type now renders the same clean
    Doku-style card via create_player_card; the BREAKING NEWS template is the
    last-resort guarantee so a post is never image-less."""
    mode = "rumour" if rumour else "confirmed"
    def _ok():
        return os.path.exists(filename) and os.path.getsize(filename) >= 1000
    try:
        create_player_card(story, sources, filename, mode=mode)
        if _ok():
            return
        print("  [IMG] player card produced no valid file — using fallback template")
    except Exception as e:
        print(f"  [IMG] player card raised ({e}) — using fallback template")
    try:
        _create_fallback_card(story, sources, filename)
        if _ok():
            print("  [IMG] fallback BREAKING NEWS card used.")
            return
    except Exception as e:
        print(f"  [IMG] fallback card ALSO failed: {e}")

# ── QUEUE FILES ──────────────────────────────────────────────────────────
def _slug(item):
    return re.sub(r'[^a-z0-9_]', '', item["key"]) + f"_s{item['stage']}"

def save_draft(item, body, image_path):
    draft = dict(item)
    draft["draft_caption"] = body
    draft["draft_image"] = str(image_path) if image_path else None
    draft["drafted_at"] = datetime.now(timezone.utc).isoformat()
    with open(PENDING_DIR / f"{_slug(item)}.json", "w") as f:
        json.dump(draft, f, indent=2, default=str)

def move_to_posted(item):
    src = PENDING_DIR / f"{_slug(item)}.json"
    dst = POSTED_DIR / f"{_slug(item)}.json"
    try:
        if src.exists(): src.rename(dst)
        else:
            with open(dst, "w") as f: json.dump(item, f, indent=2, default=str)
    except Exception as e:
        print(f"  [QUEUE] could not archive draft: {e}")

def record_posted(item, data):
    if item.get("id") and item["id"] not in data["posted_ids"]:
        data["posted_ids"].append(item["id"])
    data["stories"][item["key"]] = {
        "stage": item["stage"], "player": item["player"],
        "to_key": item.get("to_key"), "event": item["event"],
        "status": "collapsed" if item.get("collapsed") else "active",
        "sources": item["sources"], "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    record_content_dedup(item, data)
    increment_daily(data)
    save_data(data)
    move_to_posted(item)

_TWIKIT_SUCCESS_PARSE_KEYS = {
    "urls", "withheld_in_countries", "pinned_tweet_ids_str",
    "entities", "extended_entities", "card",
}

async def post_item(post_client, item, data):
    valid, why = validate_story(item, fetch_fpl_data())
    if not valid:
        print(f"  POST BLOCKED ({why}): {item.get('player')!r}")
        if item.get("id") and item["id"] not in data["posted_ids"]:
            data["posted_ids"].append(item["id"]); save_data(data)
        return False
    dup, dreason = is_duplicate_content(item, data)
    if dup:
        print(f"  POST BLOCKED (duplicate:{dreason}): {item.get('player')!r}")
        if item.get("id") and item["id"] not in data["posted_ids"]:
            data["posted_ids"].append(item["id"]); save_data(data)
        return False
    image_path = item.get("draft_image") or str(PENDING_DIR / f"{_slug(item)}.png")
    caption = item.get("draft_caption") or trim_for_twitter(
        build_tweet_body(item, item["sources"], item.get("mode", "confirmed")), limit=278)

    # GUARANTEE AN IMAGE — never drop or block a post for a missing card.
    def _img_ok():
        return os.path.exists(image_path) and os.path.getsize(image_path) >= 1000
    if not _img_ok():
        print(f"  [IMG] post-time card missing — regenerating: {item.get('player')!r}")
        try:
            create_image(item, item["sources"], image_path, rumour=(item.get("mode") == "rumour"))
        except Exception as e:
            print(f"  [IMG] regeneration raised: {e}")
    if not _img_ok():
        print(f"  [IMG] forcing BREAKING NEWS fallback card: {item.get('player')!r}")
        try:
            _create_fallback_card(item, item["sources"], image_path)
        except Exception as e:
            print(f"  [IMG] forced fallback card failed: {e}")
    if not _img_ok():
        print(f"  POST BLOCKED (no image could be produced): {item.get('player')!r}")
        return False

    media_id = await post_client.upload_media(image_path, media_type="image/png")
    posted_live = False
    try:
        await post_client.create_tweet(text=caption, media_ids=[media_id])
        posted_live = True
    except KeyError as ke:
        key = str(ke).strip("'\"")
        if key in _TWIKIT_SUCCESS_PARSE_KEYS:
            print(f"  [WARN] twikit KeyError({ke}) after create_tweet — "
                  f"tweet is live; recording as posted to prevent duplicate.")
            posted_live = True
        else: raise
    if posted_live:
        record_posted(item, data)
        print(f"  \u2705 POSTED [{status_label(item, item.get('mode'))}]: "
              f"{item['player']} — {item['event']} (stage {item['stage']})")
        return True
    return False

# ── SCRAPER ──────────────────────────────────────────────────────────────
# Maximum age, in days, for a tweet to be eligible to post. Tweets whose
# publish timestamp is older than this are skipped outright (hard cutoff),
# independent of the recycled-news wording filter.
MAX_TWEET_AGE_DAYS = 3

def _parse_tweet_date(raw):
    """Parse a tweet timestamp from twikit (created_at) or Nitter RSS (pubDate)
    into an aware UTC datetime. Returns None if it can't be parsed."""
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    s = str(raw).strip()
    # Twitter/X style: "Wed Oct 10 20:19:24 +0000 2018"
    for fmt in ("%a %b %d %H:%M:%S %z %Y",
                "%a, %d %b %Y %H:%M:%S %z",      # RFC822 / Nitter pubDate
                "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S%z"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    try:  # last resort: ISO 8601
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

def tweet_too_old(created_at, max_days=MAX_TWEET_AGE_DAYS):
    """True if the tweet's timestamp is older than max_days. Unknown/unparseable
    dates return False (do NOT block) so a missing timestamp never silently
    kills every post — the recycled-news filter is the backstop in that case."""
    dt = _parse_tweet_date(created_at)
    if dt is None:
        return False
    age = datetime.now(timezone.utc) - dt
    return age.total_seconds() > max_days * 86400

def _card_date_str(created_at):
    """Footer date label. Uses the tweet's publish date when known, else today.
    Format: '21 Jun 2026'."""
    dt = _parse_tweet_date(created_at) or datetime.now(timezone.utc)
    return dt.strftime("%d %b %Y")

def get_nitter_tweets(username):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; RSS reader)"}
    for inst in NITTER_INSTANCES:
        try:
            r = requests.get(f"{inst}/{username}/rss", headers=headers, timeout=10)
            if r.status_code != 200: continue
            root = ET.fromstring(r.content)
            out = []
            for it in root.findall(".//item")[:8]:
                link, desc = it.find("link"), it.find("description")
                if link is None: continue
                tid = link.text.strip().split("/")[-1].split("#")[0]
                desc_text = desc.text if desc is not None and desc.text else ""
                text = re.sub(r'<[^>]+>', '', desc_text).strip()

                pub = it.find("pubDate")
                created_at = pub.text.strip() if pub is not None and pub.text else None

                # Extract image from Nitter HTML
                media_url = None
                img_match = re.search(r'<img[^>]+src="([^">]+)"', desc_text)
                if img_match:
                    media_url = img_match.group(1)
                    if media_url.startswith("/"):
                        media_url = f"{inst}{media_url}"
                
                if tid and text: out.append({"id": tid, "text": text, "media_url": media_url, "created_at": created_at})
            if out: return out
        except Exception: continue
    return []

async def get_twikit_tweets(read_client, username, count=20, retries=2):
    if read_client is None: return []
    for attempt in range(retries):
        try:
            user = await read_client.get_user_by_screen_name(username)
            tweets = await read_client.get_user_tweets(user.id, "Tweets", count=count)
            out = []
            for t in tweets:
                txt = getattr(t, "full_text", None) or getattr(t, "text", "") or ""
                tid = str(getattr(t, "id", "") or "")
                created_at = getattr(t, "created_at", None) or getattr(t, "created_at_datetime", None)

                # Safely extract image from Twikit
                media_url = None
                if hasattr(t, "media") and t.media:
                    for m in t.media:
                        m_type = getattr(m, "type", None) or (m.get("type") if isinstance(m, dict) else None)
                        if m_type == "photo":
                            media_url = getattr(m, "media_url_https", None) or (m.get("media_url_https") if isinstance(m, dict) else None)
                            if media_url: break
                            
                if tid and txt: out.append({"id": tid, "text": txt, "media_url": media_url, "created_at": created_at})
            return out
        except Exception as e:
            if attempt + 1 < retries: await asyncio.sleep(3 * (attempt + 1))
            else: print(f"  [READ] twikit failed for @{username}: {e}")
    return []

async def fetch_tweets(read_client, username):
    tweets = await get_twikit_tweets(read_client, username)
    if tweets: return tweets, "twikit"
    nit = get_nitter_tweets(username)
    return nit, ("nitter" if nit else "none")

async def scrape(data, read_client):
    fpl = fetch_fpl_data()
    story_map = {}
    seen = skipped = 0
    accounts_total = len(JOURNALISTS)
    accounts_failed = 0
    for username in JOURNALISTS:
        try: tweets, src = await fetch_tweets(read_client, username)
        except Exception as e:
            print(f"  [READ] @{username} error: {e}")
            tweets, src = [], "error"
        if not tweets and src in ("none", "error"):
            accounts_failed += 1
            print(f"  [WARN] @{username}: ALL sources failed — X tokens may be expired or Nitter is down")
        else:
            print(f"  [READ] @{username}: {len(tweets)} tweets via {src}")
        for t in tweets:
            tid, text = t["id"], t["text"]
            if tid in data["posted_ids"]: continue
            if not any(k in text.lower() for k in FOOTBALL_KW): continue
            if tweet_too_old(t.get("created_at")):
                skipped += 1
                print(f"   skip (older_than_{MAX_TWEET_AGE_DAYS}d): {text[:70]!r}")
                continue
            seen += 1
            # Cache reuse: only trust a cached extraction if it was produced by
            # the CURRENT logic. Old cached entries (e.g. pre-"other"-event code)
            # could re-post stories the new gate would reject, so we stamp a
            # version and re-extract anything older. Cheap insurance against
            # stale-cache reposts after a logic upgrade.
            cached = data["extracted"].get(tid)
            if cached and cached.get("_logic_ver") == _LOGIC_VER:
                story = dict(cached)
            else:
                story = build_story(text, fpl)
                story["media_url"] = t.get("media_url")
                story["created_at"] = t.get("created_at")
                story["_logic_ver"] = _LOGIC_VER
                data["extracted"][tid] = dict(story)
            safe, why = passes_safety_gate(story, text, fpl, sources=[username])
            if not safe:
                skipped += 1
                print(f"   skip ({why}): {text[:70]!r}")
                continue
            valid, vwhy = validate_story(story, fpl)
            if not valid:
                skipped += 1
                print(f"   invalid ({vwhy}): {text[:70]!r}")
                continue
            anchor = story.get("to_key") or story.get("from_key") or "unknown"
            key = reconcile_key(story["player"], anchor, story["event"],
                                story_map, data.get("stories", {}), data.get("pending", {}))
            ok, reason = should_post(data, key, story["stage"], story["collapsed"])
            if not ok:
                print(f"   skip ({reason}): {key}")
                continue
            if key in story_map:
                ex = story_map[key]
                if username not in ex["sources"]: ex["sources"].append(username)
                if story["stage"] > ex["stage"]: ex.update({k: story[k] for k in story})
                ex["sources"] = list(dict.fromkeys(ex["sources"]))
            else:
                prior = data.get("pending", {}).get(key, {}).get("sources", [])
                unk = absorb_unknown_variant(story["player"], story["event"], key,
                                             story_map, data.get("pending", {}))
                if unk and unk in story_map:
                    prior = list(dict.fromkeys(prior + story_map[unk].get("sources", [])))
                    del story_map[unk]
                elif unk and unk in data.get("pending", {}):
                    prior = list(dict.fromkeys(prior + data["pending"][unk].get("sources", [])))
                    data["pending"].pop(unk, None)
                story.update({
                    "id": tid, "key": key, "text": text,
                    "sources": list(dict.fromkeys(prior + [username])), "reason": reason,
                })
                story_map[key] = story
        await asyncio.sleep(1)
    fail_ratio = accounts_failed / accounts_total if accounts_total else 1.0
    if accounts_failed:
        print(f"  [READ-HEALTH] {accounts_failed}/{accounts_total} accounts returned nothing "
              f"({fail_ratio:.0%}). Low volume may be a READ problem, not a quiet news day.")
    if fail_ratio >= 0.5:
        print("  [READ-HEALTH] WARNING: more than half of sources failed. "
              "Check X_AUTH_TOKEN / X_CT0_TOKEN and Nitter availability before "
              "assuming there is no news.")
    if (seen + skipped) == 0:
        print("  [WARN] Zero football tweets from ALL journalists. X auth tokens "
              "likely expired — update X_AUTH_TOKEN and X_CT0_TOKEN secrets.")
    print(f"  [SCRAPE] {seen} football tweets seen, {skipped} skipped, {len(story_map)} candidate stories")
    data["last_read_health"] = {
        "accounts_total": accounts_total,
        "accounts_failed": accounts_failed,
        "fail_ratio": round(fail_ratio, 3),
        "at": datetime.now(timezone.utc).isoformat(),
    }
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
        st["mode"] = mode
        st["rumour"] = (mode == "rumour")
        data["pending"].pop(key, None)
        ready.append(st)
    if len(data["extracted"]) > 600:
        for k in list(data["extracted"].keys())[:-600]: del data["extracted"][k]
    if len(data["posted_ids"]) > 1500: data["posted_ids"] = data["posted_ids"][-1500:]
    save_data(data)
    return sorted(ready, key=lambda x: -(1 if x["collapsed"] else x["stage"]))

# ── DRAFT BUILDER (NO POSTING) ───────────────────────────────────────────
def build_draft(item, data, fpl):
    valid, why = validate_story(item, fpl)
    if not valid:
        print(f"  VALIDATION FAILED ({why}) — not drafting: {item.get('player')!r}")
        if item.get("id") and item["id"] not in data["posted_ids"]:
            data["posted_ids"].append(item["id"])
        return None
    mode = item.get("mode", "rumour")
    rumour = (mode == "rumour")
    label = status_label(item, mode)
    if label is None or label not in APPROVED_LABELS:
        print(f"  HELD (no approved label for event={item.get('event')!r}): {item.get('player')!r}")
        if item.get("id") and item["id"] not in data["posted_ids"]:
            data["posted_ids"].append(item["id"])
        return None
    image_path = PENDING_DIR / f"{_slug(item)}.png"
    try:
        create_image(item, item["sources"], str(image_path), rumour=rumour)
        if not image_path.exists() or image_path.stat().st_size < 1000:
            raise RuntimeError("image missing or empty")
    except Exception as e:
        print(f"  [IMG] generation FAILED ({e}) — draft skipped: {item.get('player')!r}")
        return None
    body = trim_for_twitter(build_tweet_body(item, item["sources"], mode), limit=278)
    save_draft(item, body, image_path)
    item["draft_caption"] = body
    item["draft_image"] = str(image_path)
    print(f"  DRAFT READY [{label}]: {item['player']} — {item['event']} "
          f"(stage {item['stage']}, {len(item['sources'])} src) -> {image_path.name}")
    return item

# ── MAIN ─────────────────────────────────────────────────────────────────
AUTOPOST_MODES = {"confirmed", "rumour"}
MAX_POSTS_PER_RUN = 3
MAX_POSTS_PER_HOUR = 2
POST_JITTER_RANGE_S = (0, 15)

EVENT_PRIORITY = {
    "injury": 0, "suspension": 1, "transfer": 2, 
    "loan": 2, "loan_option": 2, "manager": 3, "renewal": 4, "stay": 4,
}

def _recent_post_count(data, within_seconds):
    now = datetime.now(timezone.utc)
    n = 0
    for st in data.get("stories", {}).values():
        ts = st.get("last_updated")
        if not ts: continue
        try: t = datetime.fromisoformat(ts)
        except Exception: continue
        if (now - t).total_seconds() <= within_seconds: n += 1
    return n

async def run_dry_run(fixtures_path="fixtures/tweets.json", runs=1):
    print(f"\n[DRY-RUN] Using fixtures: {fixtures_path} (x{runs} pass(es))")
    init_club_data()
    fpl = fetch_fpl_data()
    fx = Path(fixtures_path)
    if not fx.exists():
        print(f"[DRY-RUN] FIXTURE FILE NOT FOUND: {fixtures_path}")
        return
    try: fixtures = json.loads(fx.read_text())
    except Exception as e:
        print(f"[DRY-RUN] could not parse fixtures: {e}")
        return
    data = {"daily": {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                      "count": 0, "limit": 17},
            "stories": {}, "posted_ids": [], "pending": {}, "extracted": {},
            "posted_hashes": [], "posted_headlines": []}
    total_accepted = total_dup_blocked = total_img_ok = total_img_fail = 0
    dryrun_dir = Path("queue/dryrun")
    dryrun_dir.mkdir(parents=True, exist_ok=True)

    for run_i in range(1, runs + 1):
        print(f"\n[DRY-RUN] ===== PASS {run_i}/{runs} =====")
        accepted_this_pass = 0
        for fxt in fixtures:
            username = fxt.get("source", "FabrizioRomano")
            text = fxt.get("text", "")
            tid = str(fxt.get("id") or hashlib.sha256(text.encode()).hexdigest()[:16])
            story = build_story(text, fpl)
            story["media_url"] = fxt.get("media_url")
            story["created_at"] = fxt.get("created_at")
            safe, why = passes_safety_gate(story, text, fpl, sources=[username])
            if not safe:
                print(f"  [DRY] skip ({why}): {text[:60]!r}")
                continue
            valid, vwhy = validate_story(story, fpl)
            if not valid:
                print(f"  [DRY] invalid ({vwhy}): {text[:60]!r}")
                continue
            dup, dreason = is_duplicate_content(story, data)
            if dup:
                total_dup_blocked += 1
                print(f"  [DRY] DUPLICATE BLOCKED ({dreason}): {story.get('player')!r}")
                continue
            story.update({"id": tid, "key": build_story_key(
                story["player"], story.get("to_key") or story.get("from_key") or "unknown",
                story["event"]), "sources": [username], "mode": "rumour"})
            img_path = dryrun_dir / f"{re.sub(r'[^a-z0-9_]', '', story['key'])}.png"
            try:
                create_image(story, story["sources"], str(img_path), rumour=(story["mode"] == "rumour"))
                if img_path.exists() and img_path.stat().st_size >= 1000: total_img_ok += 1
                else:
                    total_img_fail += 1
                    print(f"  [DRY] IMAGE FAILED to produce valid file: {story['key']}")
            except Exception as e:
                total_img_fail += 1
                print(f"  [DRY] IMAGE EXCEPTION: {e}")
            record_content_dedup(story, data)
            data["stories"][story["key"]] = {
                "stage": story.get("stage", 1), "player": story["player"],
                "event": story["event"], "status": "active",
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            accepted_this_pass += 1
            total_accepted += 1
            print(f"  [DRY] ACCEPTED: {story['player']} — {story['event']} -> {img_path.name}")
        print(f"[DRY-RUN] pass {run_i}: {accepted_this_pass} new accepted")

    print("\n[DRY-RUN] ================ SUMMARY ================")
    print(f"  Fixtures processed : {len(fixtures)} x {runs} pass(es)")
    print(f"  Unique accepted    : {total_accepted}")
    print(f"  Duplicates blocked : {total_dup_blocked}  (should be > 0 if runs>1)")
    print(f"  Images OK (>=1KB)  : {total_img_ok}")
    print(f"  Images FAILED      : {total_img_fail}  (MUST be 0)")
    print(f"  Daily cap          : {data['daily']['limit']}")
    est = min(total_accepted, data['daily']['limit'])
    print(f"  Est. posts/day     : ~{est} (capped at {data['daily']['limit']}; "
          f"1/run × 30-min cron, hour cap {MAX_POSTS_PER_HOUR})")
    print(f"  Gemini status      : {'ACTIVE — last model ' + str(_GEMINI_LAST_MODEL) if _GEMINI_LAST_MODEL else 'FALLBACK (regex) — no LLM answered'}")
    print(f"  Cards written to   : {dryrun_dir}/")
    print("[DRY-RUN] ==========================================")
    if total_img_fail == 0: print("[DRY-RUN] PASS: no blank/broken images.")
    else: print("[DRY-RUN] FAIL: some images did not render — investigate above.")

async def main(post: bool = True, allow_rumours: bool = False):
    mode_str = "LIVE" if post else "DRAFT-ONLY"
    llm_status = "Gemini" if _GEMINI_OK else ("Groq" if _GROQ_OK else "regex-only")
    print(f"\n[BOT] Run — {datetime.now(timezone.utc).isoformat()} "
          f"(LLM={llm_status}, mode={mode_str})")
    init_club_data()
    fpl = fetch_fpl_data()
    data = load_data()
    if not check_daily_limit(data):
        print("[BOT] Daily limit reached — nothing will post today.")
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
        rh = data.get("last_read_health", {})
        if rh.get("fail_ratio", 0) >= 0.15:
            print("[BOT] No drafts — but over half of sources failed to read. "
                  "Likely a READ/access problem, not a quiet news day. "
                  "Verify X cookies and Nitter, then re-run.")
        else:
            print("[BOT] Quiet run. No new stories found (sources read OK).")
        save_data(data)
        return

    drafts = []
    for item in queue:
        built = build_draft(item, data, fpl)
        if built is not None:
            drafts.append(built)
    save_data(data)
    print(f"\n[BOT] {len(drafts)} draft(s) written to {PENDING_DIR}/.")

    if not post:
        print("[BOT] DRAFT-ONLY run. Review drafts in queue/pending/ and re-run "
              "with --post to publish.")
        return

    if not (X_POST_AUTH_TOKEN and X_POST_CT0_TOKEN):
        print("[BOT] --post set but no posting cookies. "
              "Set X_POST_AUTH_TOKEN and X_POST_CT0_TOKEN. Nothing posted.")
        return

    modes_ok = set(AUTOPOST_MODES)
    postable = [d for d in drafts if d.get("mode") in modes_ok]

    if not postable:
        print("[BOT] No postable stories this run.")
        return

    postable.sort(key=lambda s: (
        EVENT_PRIORITY.get(s.get("event"), 5),
        0 if s.get("collapsed") else 1,
        -int(s.get("stage", 1)),
    ))

    posted_last_hour = _recent_post_count(data, 3600)
    if posted_last_hour >= MAX_POSTS_PER_HOUR:
        print(f"[BOT] Per-hour cap reached ({posted_last_hour}/{MAX_POSTS_PER_HOUR}) "
              f"— skipping posting this run.")
        return

    try:
        post_client = Client("en-US")
        post_client.set_cookies({"auth_token": X_POST_AUTH_TOKEN, "ct0": X_POST_CT0_TOKEN})
    except Exception as e:
        print(f"[BOT] could not init posting client: {e}")
        return

    remaining_today = data["daily"]["limit"] - data["daily"]["count"]
    remaining_hour = MAX_POSTS_PER_HOUR - posted_last_hour
    batch = postable[:max(0, min(MAX_POSTS_PER_RUN, remaining_today, remaining_hour))]
    print(f"[BOT] Posting {len(batch)} item(s) (run cap {MAX_POSTS_PER_RUN}, "
          f"{remaining_today} left today, {remaining_hour} left this hour).")

    posted = 0
    for i, item in enumerate(batch):
        if not check_daily_limit(data):
            print("[BOT] Hit daily limit mid-batch — stopping.")
            break
        
        jitter = random.randint(*POST_JITTER_RANGE_S)
        print(f"  [PACING] waiting {jitter}s before posting (anti-spam jitter)…")
        await asyncio.sleep(jitter)
        
        try:
            if await post_item(post_client, item, data):
                posted += 1
        except Exception as e:
            if item.get("id") and item["id"] in data["posted_ids"]:
                print(f"  [ERROR] {item['key']}: {e} — already recorded, NOT retrying")
            else:
                print(f"  [ERROR] {item['key']} (attempt 1): {e} — retrying once")
                try:
                    await asyncio.sleep(10)
                    if await post_item(post_client, item, data):
                        posted += 1
                except Exception as e2:
                    print(f"  [ERROR] {item['key']} (attempt 2): {e2} — skipping")
                    if item.get("id") and item["id"] not in data["posted_ids"]:
                        data["posted_ids"].append(item["id"])
                        save_data(data)

    print(f"\n[BOT] {posted} post(s) published; {data['daily']['count']}/"
          f"{data['daily']['limit']} used today.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FPL VORTEX news bot.")
    parser.add_argument("--draft-only", action="store_true",
                        help="Force draft-only mode (no posting). Default is LIVE "
                             "(auto-posts confirmed/OFFICIAL stories, capped per run/day).")
    parser.add_argument("--allow-rumours", action="store_true",
                        help="Also auto-post RUMOUR-labelled stories (NOT recommended).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Offline test: run fixtures through the full pipeline, "
                             "no network reads, no posting. Proves dedup/image/volume.")
    parser.add_argument("--fixtures", default="fixtures/tweets.json",
                        help="Path to fixture tweets JSON for --dry-run.")
    parser.add_argument("--runs", type=int, default=2,
                        help="How many passes over fixtures in --dry-run (>=2 proves dedup).")
    args = parser.parse_args()
    if args.dry_run:
        asyncio.run(run_dry_run(fixtures_path=args.fixtures, runs=args.runs))
    else:
        asyncio.run(main(post=not args.draft_only, allow_rumours=args.allow_rumours))
