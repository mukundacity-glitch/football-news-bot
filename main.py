"""
FPL VORTEX — Football news automation (DRAFT-ONLY build).

What this build does:
  - Scrapes trusted journalist/club accounts.
  - Extracts ONE accurate story per tweet (LLM with a truthful fallback).
  - Strips RT/@handles/URLs/raw repost text; writes an original short summary.
  - Classifies as OFFICIAL / TRANSFER / RUMOUR / INJURY / LOAN / CONTRACT /
    MANAGER using a strict source rule (official OR >= 2 trusted reporters).
  - Renders a clean card: "FPL VORTEX" wordmark top-left, FROM:/TO: text rows
    (NO arrows), club crests, relevant hashtags only.
  - Writes every result to queue/pending/ as a DRAFT. It NEVER posts.

To post, a human reviews the drafts. Auto-posting is intentionally disabled.
"""

from clubs_cache import get_club_data
import os
import re
import json
import argparse
import asyncio
import requests
import urllib.request
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pilmoji import Pilmoji
from twikit import Client

try:
    import google.generativeai as genai
    _GEMINI_OK = bool(os.getenv("GEMINI_API_KEY"))
    if _GEMINI_OK:
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
except Exception:
    _GEMINI_OK = False

# ── SECRETS ──────────────────────────────────────────────────────────────
X_AUTH_TOKEN = os.getenv("X_AUTH_TOKEN")      # read account (twikit reader)
X_CT0_TOKEN = os.getenv("X_CT0_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

# ── PATHS ────────────────────────────────────────────────────────────────
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
    # ── Tier 2 — Elite trusted reporters (handles VERIFIED) ──
    "FabrizioRomano",      # Fabrizio Romano ✓
    "David_Ornstein",      # David Ornstein (The Athletic) ✓
    "_pauljoyce",          # Paul Joyce (The Times) ✓
    "sistoney67",          # Simon Stone (BBC) ✓
    "SamiMokbel_BBC",      # Sami Mokbel (BBC / Daily Mail) ✓
    "JacobsBen",           # Ben Jacobs ✓
    "JamesPearceLFC",      # James Pearce (The Athletic, LFC) ✓
    # Additional reliable reporters — handles plausible, VERIFY before relying:
    "Plettigoal", "MatteoMoretto", "AlfredoPedulla", "DiMarzio",
    # ── Tier 3 — Trusted media outlets ──
    "SkySportsNews",       # ✓ verified
    "BBCSport",            # ✓ verified
    "TheAthleticFC",       # ✓ verified (The Athletic football)
    "guardianfootball",    # ✓ Guardian Football (active; @guardian_sport is ARCHIVED)
    "lequipe",             # ✓ verified (L'Équipe)
    "marca",               # ✓ verified (Marca)
    "diarioas",            # plausible — VERIFY (Diario AS)
    "kicker",              # plausible — VERIFY (Kicker)
    # NOTE: "relevo" REMOVED — the outlet shut down in May 2025.
    # ── Club + league official accounts (verify each before relying) ──
    "premierleague", "OfficialFPL", "Arsenal", "ManCity", "LFC", "ChelseaFC",
    "ManUtd", "SpursOfficial", "NUFC", "NFFC",
]
NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]

# ── SOURCE TIERS ─────────────────────────────────────────────────────────
# All handles lowercased; matching is case-insensitive.
# Tier 1 — OFFICIAL: club / league / governing-body accounts. Confirm OFFICIAL.
OFFICIAL_ACCOUNTS = {
    "premierleague", "officialfpl", "fpl", "uefa", "fifacom", "fifaworldcup",
    # PL club official handles
    "arsenal", "avfcofficial", "afcbournemouth", "brentfordfc",
    "officialbhafc", "chelseafc", "cpfc", "everton", "fulhamfc",
    "lcfc", "liverpoolfc", "lfc", "mancity", "manutd", "newcastle_nufc", "nufc",
    "nffc", "southamptonfc", "spursofficial", "westham", "wolves",
}
# Official accounts that may also source INJURY news (clubs, league, FPL).
OFFICIAL_INJURY_ACCOUNTS = OFFICIAL_ACCOUNTS | {"officialfpl", "fpl", "premierleague"}

# Tier 2 — ELITE TRUSTED reporters. One = RUMOUR, two = strong (still labelled
# RUMOUR), and they may CONFIRM a transfer when corroborated/strong-worded.
ELITE_TRUSTED = {
    "fabrizioromano", "david_ornstein", "_pauljoyce", "sistoney67",
    "samimokbel_bbc", "jacobsben", "jamespearcelfc",
    "plettigoal", "matteomoretto", "alfredopedulla", "dimarzio",
}
# Backwards-compatible alias used elsewhere in the file.
TRUSTED_REPORTERS = ELITE_TRUSTED

# Tier 3 — TRUSTED MEDIA outlets. Raise confidence on an existing Elite story,
# but NEVER create an OFFICIAL/TRANSFER on their own.
TRUSTED_MEDIA = {
    "skysportsnews", "skysports", "bbcsport", "theathleticfc", "theathletic",
    "guardianfootball", "lequipe", "marca", "diarioas", "as", "kicker",
}


def source_tier(handle: str) -> int:
    """1=official, 2=elite trusted, 3=trusted media, 0=untrusted/unknown."""
    h = (handle or "").lower().lstrip("@")
    if h in OFFICIAL_ACCOUNTS:
        return 1
    if h in ELITE_TRUSTED:
        return 2
    if h in TRUSTED_MEDIA:
        return 3
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
    if not name:
        return None
    n = name.lower()
    for alias in _SORTED_ALIASES:
        if re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', n):
            return CLUB_ALIASES[alias]
    return None


BIG_CLUBS_NON_PL = ELITE_EURO_CLUBS


def is_big_club_name(name: str) -> bool:
    if not name:
        return False
    n = name.lower().strip()
    return any(n == c or c in n for c in BIG_CLUBS_NON_PL)


def is_bundesliga_or_laliga_club(name: str) -> bool:
    if not name:
        return False
    n = name.lower().strip()
    return any(n == c or c in n for c in (BUNDESLIGA_BIG_CLUBS | LA_LIGA_BIG_CLUBS))


BIG_NAMES_NON_FPL = {
    "mbappe", "mbappé", "vinicius", "vinícius", "bellingham", "rodrygo",
    "haaland", "lewandowski", "messi", "neymar", "ronaldo", "modric", "kroos",
    "benzema", "pedri", "gavi", "yamal", "kane", "musiala", "wirtz", "kvaratskhelia",
}

# Current/known managers — must NOT be extracted as a transfer "player".
# A tweet naming a manager is either manager news or (more often) the manager
# commenting on a player, which the fallback can't disentangle safely.
MANAGER_SURNAMES = {
    "de zerbi", "zerbi", "guardiola", "arteta", "klopp", "slot", "postecoglou",
    "ten hag", "amorim", "emery", "howe", "maresca", "iraola", "frank",
    "nuno", "moyes", "dyche", "hurzeler", "glasner", "ancelotti", "xabi alonso",
    "alonso", "flick", "simeone", "mourinho", "conte", "tuchel", "nagelsmann",
}


def is_big_name_player(name: str) -> bool:
    if not name:
        return False
    n = name.lower().strip()
    return any(part in BIG_NAMES_NON_FPL for part in re.split(r'[\s\-]+', n))


# ── CLUBS_CACHE WIRING (all leagues) ─────────────────────────────────────
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
    if not fpl_data:
        return
    for el in fpl_data.get("elements", []):
        nat = (el.get("nationality") or "").lower().strip()
        if nat:
            COUNTRY_NAMES.add(nat)


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
    if name_or_key in CLUB_HASHTAG_MAP:
        return CLUB_HASHTAG_MAP[name_or_key]
    n = name_or_key.replace("_", " ").lower()
    return CLUB_HASHTAG_MAP.get(resolve_club_key(n) or "", CLUB_HASHTAGS.get(n))


# ── STATE ────────────────────────────────────────────────────────────────
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
    d.setdefault("extracted", {})
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
    """Strip RT markers, @handles, URLs and repost cruft (Rule 2)."""
    t = text or ""
    t = re.sub(r'\bRT\s+@\w+:?', ' ', t)            # RT @handle:
    t = re.sub(r'https?://\S+|www\.\S+', ' ', t)     # links
    t = re.sub(r'(?<!\w)@\w+', ' ', t)               # @handles
    t = re.sub(r'#\w+', ' ', t)                       # stray hashtags from source
    t = re.sub(r'[“”"]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


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
    """No-LLM path. Builds an ORIGINAL one-line summary (never the raw tweet)."""
    cleaned = _clean_source_text(tweet_text)
    tl = cleaned.lower()

    def has_word(words_list, text):
        return any(re.search(r'(?<![a-z])' + re.escape(w) + r'(?![a-z])', text) for w in words_list)

    # event detection
    if has_word(["suspended", "suspension", "banned", "ban", "red card", "sent off"], tl):
        event = "suspension"
    elif has_word(["injury", "injured", "ruled out", "scan", "hamstring", "surgery", "doubt"], tl):
        event = "injury"
    elif has_word(["sack", "appoint", "head coach", "manager"], tl):
        event = "manager"
    elif has_word(["new deal", "new contract", "signs new", "extension", "renew"], tl):
        event = "renewal"
    elif has_word(["stay", "staying", "no exit", "not for sale", "remain"], tl) and not has_word(["sign for", "joins", "move to"], tl):
        event = "stay"
    elif has_word(["loan"], tl):
        event = "loan"
    else:
        event = "transfer"

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
            if len(word) > 3:
                ROLE_WORDS.add(word)
    ROLE_WORDS |= POSITION_WORDS

    def _is_bad_name(low: str) -> bool:
        # A manager's name is not a transfer subject (unless this is manager news).
        if event != "manager" and (low in MANAGER_SURNAMES or
                                   any(m in low for m in MANAGER_SURNAMES)):
            return True
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
    for m in re.findall(
        r'\b([A-Z][a-zà-ÿ]+(?:\s+(?:(?:van|de|da|dos|del|el|la|le|di|du|den|der|ten|ter|von|zu)\s+)?[A-Z][a-zà-ÿ]+)+)\b',
        cleaned):
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
            if _is_bad_name(m.lower()):
                continue
            if find_player_in_fpl(m, fpl_data):
                name = m
                break
    if not name:
        for m in re.findall(r'\b([A-Z][a-zà-ÿ]{2,})\b', cleaned):
            if _is_bad_name(m.lower()):
                continue
            if is_big_name_player(m):
                name = m
                break

    # direction anchoring
    clubs = []
    for alias in _SORTED_ALIASES:
        if re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', tl):
            k = CLUB_ALIASES[alias]
            if k not in clubs:
                clubs.append(k)

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
    if from_anchor and to_anchor and from_anchor != to_anchor:
        from_key, to_key = from_anchor, to_anchor
    elif from_anchor and len(clubs) == 2:
        from_key = from_anchor
        other = [c for c in clubs if c != from_anchor]
        to_key = other[0] if other else None
    elif to_anchor and len(clubs) == 2:
        to_key = to_anchor
        other = [c for c in clubs if c != to_anchor]
        from_key = other[0] if other else None
    elif len(clubs) == 1:
        # single club + "stay"/"renewal" => that is the CURRENT club, not a destination
        if event in ("stay", "renewal"):
            from_key = clubs[0]
        else:
            to_key = clubs[0]
    else:
        to_key = clubs[0] if clubs else None

    is_collapsed = has_word(["collapsed", "called off", "rejected", "deal off"], tl)

    # A "stay / no move / not for sale" tweet is about the player remaining at
    # his CURRENT club. Never invent a destination, and never mark it collapsed.
    if event in ("stay", "renewal"):
        if to_key and not from_key:
            from_key, to_key = to_key, None
        to_key = None
        is_collapsed = False

    # Contradiction guard: a collapsed flag with a named destination on a
    # fallback parse is unreliable (often a "no move to X" sentence). Drop the
    # destination so we don't assert a reversed/false direction.
    if is_collapsed and to_key and not from_anchor:
        to_key = None

    # original summary line (never the raw tweet)
    summary = _summarise(name, event, from_key, to_key, stage, is_collapsed)

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
        "body": summary, "confidence": 0.5,
    }


def _summarise(name, event, from_key, to_key, stage, collapsed):
    """One original English sentence — used when no LLM body is available."""
    who = name or "The player"
    fc = from_key.replace("_", " ") if from_key else None
    tc = to_key.replace("_", " ") if to_key else None
    if event == "injury":
        return f"{who} is being assessed and the club is monitoring the situation."
    if event == "suspension":
        return f"{who} faces a suspension and is set to miss upcoming action."
    if event == "manager":
        return f"{who} is linked with a managerial move{f' to {tc}' if tc else ''}."
    if event in ("renewal", "stay"):
        base = f"{who} is set to stay" + (f" at {fc or tc}" if (fc or tc) else "")
        return base + "; no exit is planned at this stage."
    if collapsed:
        return f"A reported move for {who}{f' to {tc}' if tc else ''} has broken down."
    verb = {1: "is being linked with", 2: "is in advanced talks over", 3: "is close to", 4: "is set to complete"}[stage]
    if tc and fc:
        return f"{who} {verb} a move from {fc} to {tc}."
    if tc:
        return f"{who} {verb} a move to {tc}."
    return f"{who} {verb} a transfer."


_VIDEO_MARKERS = re.compile(
    r'(youtu\.be|youtube\.com|/video|watch\?v=|\bfull video\b|\bwatch:?\b|'
    r'\blive\s*stream\b|\bpodcast\b|\bepisode\b|\bclip\b|🎥|▶️|📺)', re.I)
# A "claim" needs an action verb tying a player to a club/state, not just a title.
_CLAIM_MARKERS = re.compile(
    r'\b(agree[d]?|sign[ed|ing]*|join[s|ed|ing]*|move[s|d]*|deal|bid|offer|'
    r'medical|here we go|loan|contract|talks|fee|ruled out|injur|suspend|'
    r'set to|close to|advanced|personal terms|confirmed|official)\b', re.I)


def looks_like_video_post(tweet_text: str) -> bool:
    """Point 3: tweet is primarily a video/podcast/clip pointer."""
    return bool(_VIDEO_MARKERS.search(tweet_text or ""))


def has_written_claim(tweet_text: str) -> bool:
    """True only if the tweet text states an actual claim, not just a title."""
    cleaned = _clean_source_text(tweet_text)
    return bool(_CLAIM_MARKERS.search(cleaned)) and len(cleaned.split()) >= 5


def build_story(tweet_text: str, fpl_data=None) -> dict:
    s = extract_story_llm(tweet_text) or extract_story_fallback(tweet_text, fpl_data)
    # Always clean the body of any leaked source text (Rule 2).
    s["body"] = _clean_source_text(s.get("body") or "")
    s["from_key"] = s.get("from_key") or resolve_club_key(s.get("from_club"))
    s["to_key"] = s.get("to_key") or resolve_club_key(s.get("to_club"))
    try:
        s["stage"] = max(1, min(4, int(s.get("stage", 1))))
    except Exception:
        s["stage"] = 1
    s["collapsed"] = bool(s.get("collapsed"))

    # Point 3 — video handling. If the tweet is a video/podcast pointer:
    #   - require a clear WRITTEN claim in the text, else hold it,
    #   - never let it count as strong/confirmed (cap stage at 2 = "talks"),
    #     so a video can only ever produce a RUMOUR, never OFFICIAL.
    if looks_like_video_post(tweet_text):
        s["from_video"] = True
        s["has_written_claim"] = has_written_claim(tweet_text)
        if s["stage"] > 2:
            s["stage"] = 2
    else:
        s["from_video"] = False
        s["has_written_claim"] = True

    # If the LLM gave an empty/short body, rebuild an original summary.
    if len(s["body"].split()) < 4:
        s["body"] = _summarise(s.get("player"), s.get("event"),
                               s.get("from_key"), s.get("to_key"),
                               s.get("stage"), s.get("collapsed"))
    return s


# ── FPL DATA ─────────────────────────────────────────────────────────────
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


def find_player_in_fpl(player_name, data):
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
    return el.get("now_cost", 0) >= 65 or el.get("total_points", 0) >= 90


def fpl_team_key(el, fpl_data):
    if not el or not fpl_data:
        return None
    for t in fpl_data.get("teams", []):
        if t.get("id") == el.get("team"):
            return resolve_club_key((t.get("name", "") + " " + t.get("short_name", "")).lower())
    return None


# ── DEDUP / PROGRESSION ──────────────────────────────────────────────────
def build_story_key(player, club_key, event) -> str:
    p = (player or "unknown").lower().replace(" ", "_")
    c = (club_key or "unknown").lower()
    fam = "injury" if event == "injury" else "manager" if event == "manager" else "transfer"
    return f"{p}_{c}_{fam}"


def _event_family(event):
    return "injury" if event == "injury" else "manager" if event == "manager" else "transfer"


def reconcile_key(player, anchor, event, *maps):
    """Merge same-player + same-event-family stories that differ only by club
    resolution (e.g. one source has TO=Man_City, another TO=unknown).

    Returns the canonical key to use. Strategy:
      - Build the natural key for this story.
      - Look across the given dicts (in-run story_map, saved stories, pending)
        for an EXISTING key with the same player and event family.
      - If found, prefer the variant that has a REAL club over an 'unknown'
        one, so an "Anderson interest" (unknown) folds into an existing
        "Anderson to Man City" saga rather than spawning a duplicate.
    """
    p = (player or "unknown").lower().replace(" ", "_")
    fam = _event_family(event)
    natural = build_story_key(player, anchor, event)
    natural_is_unknown = natural.endswith(f"_unknown_{fam}")

    prefix = f"{p}_"
    suffix = f"_{fam}"
    candidates = set()
    for mp in maps:
        if not mp:
            continue
        for k in mp.keys():
            if k.startswith(prefix) and k.endswith(suffix):
                candidates.add(k)
    candidates.discard(natural)

    # Prefer an existing key that carries a real (non-unknown) club.
    real_club_keys = [k for k in candidates if not k.endswith(f"_unknown{suffix}")]

    if natural_is_unknown and real_club_keys:
        # this vague story folds into the established saga
        return sorted(real_club_keys)[0]
    if not natural_is_unknown:
        # this story HAS a club; if an 'unknown' variant already exists, we will
        # absorb it (handled by the caller merging sources), but our key wins.
        return natural
    # natural is unknown and no real-club variant exists yet
    if candidates:
        return sorted(candidates)[0]
    return natural


def absorb_unknown_variant(player, event, canonical_key, *maps):
    """If an 'unknown'-club variant of this same player+event exists in any map,
    return its key so the caller can fold its sources into the canonical entry."""
    p = (player or "unknown").lower().replace(" ", "_")
    fam = _event_family(event)
    unknown_key = f"{p}_unknown_{fam}"
    if unknown_key == canonical_key:
        return None
    for mp in maps:
        if mp and unknown_key in mp:
            return unknown_key
    return None


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


# ── SAFETY + ACCURACY GATES ──────────────────────────────────────────────
STRONG_OFFICIAL = ["here we go", "official", "confirmed", "completed", "done deal",
                   "sealed", "unveiled", "joins", "joined", "signs", "signed", "medical"]


def detect_mixed_story(story, raw_text) -> str:
    """Point 4: catch manager/player/article confusion. Returns a reason string
    if the story looks mixed/unsafe, else "" (clean).

    Heuristics (conservative — only flags clear confusion):
      - The extracted 'player' is actually a known manager, on a non-manager event.
      - The tweet names a manager AND a different person is the subject, while the
        event is a transfer (manager commenting on a player → ambiguous).
      - The tweet appears to stitch two stories (clauses each naming clubs).
    """
    text = (raw_text or "")
    tl = text.lower()
    player = (story.get("player") or "").lower()
    ev = story.get("event")

    # 1) A manager extracted as the transfer subject.
    if ev != "manager" and player and (
            player in MANAGER_SURNAMES or any(m in player for m in MANAGER_SURNAMES)):
        return "player_is_manager"

    # 2) Manager named in text + transfer/stay event + subject is someone else.
    #    De Zerbi / Van de Ven pattern: a manager talking about a player.
    if ev in ("transfer", "loan", "loan_option", "stay", "renewal"):
        manager_in_text = any(re.search(r'(?<![a-z])' + re.escape(m) + r'(?![a-z])', tl)
                              for m in MANAGER_SURNAMES)
        subject_is_manager = any(m in player for m in MANAGER_SURNAMES)
        if manager_in_text and not subject_is_manager:
            return "manager_and_player_mixed"

    # 3) Three+ clauses each naming a club → likely stitched stories.
    clauses = re.split(r'[.;]|\bmeanwhile\b|\balso\b|\bplus\b|\belsewhere\b|\bseparately\b', tl)
    clubbed_clauses = 0
    for c in clauses:
        if any(re.search(r'(?<![a-z])' + re.escape(a) + r'(?![a-z])', c) for a in _SORTED_ALIASES):
            clubbed_clauses += 1
    if clubbed_clauses >= 3:
        return "multiple_stories_suspected"

    # 4) "No move to X or Y" multi-destination negation (the De Zerbi/VdV shape):
    #    a negated transfer that names 2+ possible destinations is a "staying"
    #    story the fallback can misread as an active move. Treat as mixed/unsafe
    #    unless the event was correctly classified as stay/renewal.
    if ev in ("transfer", "loan", "loan_option"):
        if re.search(r'\bno\s+(move|exit|transfer|deal)\b', tl) or \
           re.search(r'\b(not?|never)\s+(?:moving|leaving|for sale)\b', tl):
            dests = sum(1 for a in _SORTED_ALIASES
                        if re.search(r'(?<![a-z])' + re.escape(a) + r'(?![a-z])', tl))
            if dests >= 1:
                return "negated_move_misread_as_transfer"

    # 5) Two+ distinct capitalised full-name candidates → possible player mix.
    name_candidates = set()
    for mm in re.findall(r'\b([A-Z][a-zà-ÿ]+(?:\s+[A-Z][a-zà-ÿ]+){1,2})\b', text):
        low = mm.lower()
        if any(m in low for m in MANAGER_SURNAMES):
            continue
        if looks_like_club(low):
            continue
        name_candidates.add(low)
    # Drop near-duplicates (a name that is a subset of another).
    distinct = set()
    for n in sorted(name_candidates, key=len, reverse=True):
        if not any(n != o and n in o for o in distinct):
            distinct.add(n)
    # Two or more distinct full names in one tweet is a strong mix signal,
    # especially when coordinated with "and"/"&".
    if len(distinct) >= 2:
        coordinated = bool(re.search(r'\b[A-Z][a-zà-ÿ]+ [A-Z][a-zà-ÿ]+\s+(?:and|&)\s+[A-Z][a-zà-ÿ]+ [A-Z][a-zà-ÿ]+', text))
        if coordinated or len(distinct) >= 3:
            return "multiple_players_suspected"
        # exactly two distinct names, not obviously coordinated: flag only if
        # the second isn't part of the chosen player's own name.
        others = {n for n in distinct if player and player not in n and n not in player}
        if len(others) >= 1 and len(distinct) >= 2:
            return "multiple_players_suspected"

    return ""


def player_already_at_club(story, fpl_data) -> bool:
    """Reject implying a transfer for a player who already belongs to the
    destination club (the Doku/Man City bug).

    LIMITATION (by design): this check only works for players present in the
    FPL database — i.e. the 20 current Premier League clubs. For a player who
    is NOT in FPL data (e.g. a purely overseas move between two non-PL clubs),
    `find_player_in_fpl` returns None, `fpl_team_key` is None, and this returns
    False (cannot verify). Such moves are NOT guarded against here. This is an
    accepted coverage gap for a PL/FPL-focused channel, not a silent bug.
    """
    if story.get("event") not in ("transfer", "loan", "loan_option"):
        return False
    el = find_player_in_fpl(story.get("player"), fpl_data)
    cur = fpl_team_key(el, fpl_data)
    to_key = story.get("to_key")
    if el is None or cur is None:
        # Player not in FPL data — cannot verify current club. See LIMITATION.
        return False
    return bool(cur and to_key and cur == to_key)


def passes_safety_gate(story, raw_text, fpl_data, sources=None):
    sources = sources or []
    tl = (raw_text or "").lower()
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
    # Point 4: block mixed/confused stories (manager-as-player, manager+player,
    # stitched multi-stories).
    mixed = detect_mixed_story(story, raw_text)
    if mixed:
        return False, f"mixed_story:{mixed}"
    # Point 3: a video/podcast pointer with no clear written claim is held.
    if story.get("from_video") and not story.get("has_written_claim"):
        return False, "video_no_written_claim"
    # Rule 8: reject "already at the club" non-moves.
    if player_already_at_club(story, fpl_data):
        return False, "already_at_destination"

    if story["event"] == "manager":
        to_key = story.get("to_key")
        to_club = story.get("to_club")
        pl_club = bool(to_key) or (to_club and to_club.lower() in PL_CLUB_NAMES)
        if not (pl_club or is_bundesliga_or_laliga_club(to_club)):
            return False, "manager_no_club"
        return True, "ok_manager"

    if story["event"] == "injury":
        # Point 2: injuries must come from an OFFICIAL source (club / league /
        # FPL) OR an elite-trusted reporter. Block unverified social claims.
        tiers = [source_tier(s) for s in sources]
        injury_source_ok = any(t in (1, 2) for t in tiers) or \
            any((s or "").lower().lstrip("@") in OFFICIAL_INJURY_ACCOUNTS for s in sources)
        if not injury_source_ok:
            return False, "injury_source_not_approved"
        pl_player = find_player_in_fpl(story["player"], fpl_data) is not None
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
    big_player = is_big_player(story["player"], fpl_data) or is_big_name_player(story["player"])
    big_club = is_big_club_name(story.get("to_club")) or is_big_club_name(story.get("from_club"))
    if big_player or big_club:
        return True, "ok_big_name"
    return False, "not_fpl_relevant"


def classify_post(story, sources):
    """Balanced 3-tier source discipline.

    Returns "confirmed" | "rumour" | None(hold).

    Tier rules (kept balanced so volume stays healthy):
      - OFFICIAL account in sources                 -> confirmed (OFFICIAL/TRANSFER)
      - strong official wording (here we go / etc.) -> confirmed
      - >= 2 elite-trusted reporters                -> confirmed (corroborated)
      - 1 elite-trusted reporter                    -> rumour
      - 1 elite-trusted + any trusted-media         -> rumour (still unconfirmed,
                                                       but higher internal confidence)
      - 1 trusted-media ONLY (no elite, no official) -> rumour (clearly labelled
                                                       unconfirmed; NEVER confirmed)
    Injuries are handled by their own source gate upstream; here they post as
    confirmed only once they've passed that gate.
    """
    if story.get("collapsed"):
        return "rumour"  # a broken/never-completed move is unconfirmed by nature

    tiers = [source_tier(s) for s in sources]
    has_official = 1 in tiers
    n_elite = sum(1 for t in tiers if t == 2)
    has_media = 3 in tiers

    tl = (story.get("body", "") + " " + (story.get("headline", "") or "")).lower()
    strong_words = story["stage"] >= 4 or any(
        re.search(r'\b' + re.escape(w) + r'\b', tl) for w in STRONG_OFFICIAL)

    # Injury: already source-gated upstream, post as confirmed.
    if story["event"] == "injury":
        if has_official or n_elite >= 1:
            return "confirmed"
        return None

    # CONFIRMED: official source, OR strong official wording FROM A TRUSTED
    # SOURCE, OR >=2 elite reporters. Strong wording alone from a Tier-3 media
    # account (or unknown source) does NOT confirm — media can never create a
    # confirmed/OFFICIAL post, only a labelled RUMOUR.
    trusted_strong = strong_words and (has_official or n_elite >= 1)
    video_only = story.get("from_video") and not has_official
    if (has_official or trusted_strong or n_elite >= 2) and not video_only:
        return "confirmed"

    # RUMOUR: at least one elite-trusted reporter (optionally backed by media).
    if n_elite >= 1:
        return "rumour"

    # A notable name reported somewhere can still run as a rumour.
    if is_big_player(story.get("player"), fetch_fpl_data()) or story.get("confidence", 0) >= 0.7:
        # but require at least a trusted-media source so it's not pure noise
        if has_media:
            return "rumour"

    # VOLUME LEVER: a single Tier-3 trusted-media source posts as RUMOUR.
    # This NEVER reaches "confirmed" — the confirmed branch above requires an
    # OFFICIAL account, official wording, or >=2 elite reporters, none of which
    # a Tier-3 source can satisfy. So this cannot create a false OFFICIAL or
    # confirmed TRANSFER. It only adds clearly-labelled unconfirmed rumours.
    if has_media:
        return "rumour"

    # Untrusted/unknown sources only -> hold.
    return None


def validate_story(story, fpl_data=None):
    """Final accuracy gate before a card is built."""
    ev = story.get("event")
    player = (story.get("player") or "").strip()
    if not player:
        return False, "missing_player"
    PLACEHOLDERS = ("player name", "example", "xxx", "[", "]", "tbd", "to follow",
                    "lorem", "duration & details", "updated heading", "from club", "to club")
    blob = " ".join(str(story.get(k, "") or "") for k in
                    ("player", "headline", "body", "from_club", "to_club", "fee",
                     "contract", "conditional", "diagnosis", "expected_return")).lower()
    for ph in PLACEHOLDERS:
        if ph in blob:
            return False, f"placeholder_text:{ph!r}"
    if looks_like_club(player):
        return False, "player_is_club"
    # Rule 2: no leaked source text in the body.
    if re.search(r'\bRT\s+@|@\w+|https?://', story.get("body", "")):
        return False, "raw_source_text_in_body"
    # Rule 8: already at destination.
    if player_already_at_club(story, fpl_data):
        return False, "already_at_destination"
    if ev in ("transfer", "loan", "loan_option"):
        fk = story.get("from_key"); tk = story.get("to_key")
        fc = (story.get("from_club") or "").strip().lower()
        tc = (story.get("to_club") or "").strip().lower()
        if (fk and tk and fk == tk) or (fc and tc and fc == tc):
            return False, "from_equals_to"
        if not (tk or story.get("to_club") or fk or story.get("from_club")):
            return False, "no_clubs"
        leak = (story.get("body", "") + " " + story.get("headline", "")).lower()
        if re.search(r'\b(head coach|sacked|appointed as manager|hamstring|ruled out for)\b', leak):
            return False, "event_data_mismatch"
    if ev == "manager" and not (story.get("to_key") or story.get("to_club")):
        return False, "manager_no_club"
    return True, "ok"


# ── LABELS (approved categories ONLY) ────────────────────────────────────
# Approved: TRANSFER, RUMOUR, INJURY, SUSPENSION, CONTRACT EXTENSION, LOAN,
# MANAGER NEWS, OFFICIAL. Unapproved/custom labels are never emitted.
APPROVED_LABELS = {
    "TRANSFER", "RUMOUR", "INJURY", "SUSPENSION", "CONTRACT EXTENSION",
    "LOAN", "MANAGER NEWS", "OFFICIAL",
}
EVENT_PREFIX = {
    "transfer": "TRANSFER",
    "loan": "LOAN",
    "loan_option": "LOAN",
    "renewal": "CONTRACT EXTENSION",
    "stay": "CONTRACT EXTENSION",
    "injury": "INJURY",
    "suspension": "SUSPENSION",
    "manager": "MANAGER NEWS",
    "collapse": "TRANSFER",
    # NOTE: "other" intentionally has no mapping — such stories are held, not
    # posted with an unapproved label.
}


def status_label(story, mode):
    """Final label — always one of APPROVED_LABELS, or None to hold the post."""
    if story.get("collapsed"):
        return "RUMOUR"
    if mode == "rumour":
        return "RUMOUR"
    ev = story.get("event")
    tl = (story.get("body", "") + " " + (story.get("headline", "") or "")).lower()
    if ev in ("transfer", "loan", "loan_option") and (
            story.get("stage", 1) >= 4 or any(w in tl for w in ("official", "here we go", "completed", "confirmed"))):
        return "OFFICIAL"
    label = EVENT_PREFIX.get(ev)
    return label if label in APPROVED_LABELS else None


# ── HASHTAGS (relevant football tags only — Rule 9) ──────────────────────
BASE_TAGS = ["#FPLVortex"]


def build_hashtags(story):
    ev = story["event"]
    tags = list(BASE_TAGS)
    if ev == "injury":
        tags.append("#InjuryNews")
    elif ev == "suspension":
        tags.append("#InjuryNews")
    elif ev in ("transfer", "loan", "loan_option", "renewal", "stay"):
        tags.append("#Transfers")
    elif ev == "manager":
        tags.append("#FootballNews")
    else:
        tags.append("#FootballNews")
    # club tags
    for key, name in ((story.get("to_key"), story.get("to_club")),
                      (story.get("from_key"), story.get("from_club"))):
        ht = hashtag_for(key) or hashtag_for(name)
        if ht and ht not in tags:
            tags.append(ht)
    if (story.get("to_key") or story.get("from_key")) and "#PremierLeague" not in tags:
        tags.append("#PremierLeague")
    # keep it minimal and relevant
    return " ".join(tags[:5])


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


def build_tweet_body(story, sources, mode) -> str:
    """Clean, short, professional caption (Rule 1). No raw source text, no arrows."""
    label = status_label(story, mode)
    head = story.get("headline") or story.get("player") or "Update"
    summary = (story.get("body") or "").strip()

    lines = [f"{label} | {head}", "", summary]

    # direction in plain text (no arrows) for transfers/loans
    if story.get("event") in ("transfer", "loan", "loan_option") and not story.get("collapsed"):
        fc = (story.get("from_club") or (story.get("from_key") or "").replace("_", " ")).strip()
        tc = (story.get("to_club") or (story.get("to_key") or "").replace("_", " ")).strip()
        dir_bits = []
        if fc:
            dir_bits.append(f"From: {fc}")
        if tc:
            dir_bits.append(f"To: {tc}")
        if dir_bits:
            lines.append("\n" + "  |  ".join(dir_bits))

    if story.get("conditional"):
        lines.append("\n" + story["conditional"])
    if story.get("fpl_impact"):
        lines.append("\nFPL: " + story["fpl_impact"])

    details = []
    if story.get("fee"):
        details.append(f"Fee: {story['fee']}")
    if story.get("contract"):
        details.append(story["contract"])
    if details:
        lines.append("\n" + "  |  ".join(details))

    if mode == "rumour" and label == "RUMOUR":
        lines.insert(0, "Unconfirmed report")

    body = "\n".join(p for p in lines if p is not None).strip()
    body += "\n\n" + build_hashtags(story)
    return body


def build_detail_line(story) -> str:
    bits = []
    if story.get("fee"):
        bits.append(story["fee"])
    if story.get("contract"):
        bits.append(story["contract"])
    if story.get("conditional"):
        bits.append(story["conditional"])
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
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
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
            f = _load_fallback(size, weight)
            _FONT_CACHE[key] = f
            return f
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
        im = Image.open(path)
        im.load()
        return im.convert("RGBA")
    except Exception:
        try:
            path.unlink()
        except Exception:
            pass
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
    if not player_el:
        return False
    cur = fpl_team_key(player_el, fpl)
    if cur is None:
        return False
    return cur == from_key or cur == to_key


def _load_crest(club_key, box=120):
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


def _draw_wordmark(draw, xy):
    """FPL VORTEX channel wordmark, top-left (Rule: channel name on heading)."""
    x, y = xy
    f = get_premium_font(46, "Black")
    _draw_text_shadow(draw, (x, y), "FPL", f, (255, 255, 255), offset=2)
    fpl_w = draw.textlength("FPL ", font=f)
    _draw_text_shadow(draw, (x + fpl_w, y), "VORTEX", f, (84, 224, 124), offset=2)


def create_transfer_image(story, sources, filename, collapsed=False):
    """Clean transfer/loan card. FPL VORTEX wordmark top-left, player name,
    FROM/TO crests as TEXT ROWS (no arrows), price/contract, optional portrait."""
    W, H = 1380, 776
    fpl = fetch_fpl_data()
    player_el = find_player_in_fpl(story.get("player"), fpl)
    player_name = (player_el["web_name"] if player_el else story.get("player")) or "PLAYER"
    to_key = story.get("to_key")
    from_key = story.get("from_key")

    NAVY = (11, 18, 32)
    GOLD = (212, 175, 55)
    accent = (120, 30, 34) if collapsed else (30, 55, 110)

    face_verified = _photo_verified(player_el, fpl, from_key, to_key)

    img = Image.new("RGB", (W, H), NAVY)
    sheen = Image.new("L", (1, H), 0)
    for y in range(H):
        sheen.putpixel((0, y), int(30 * (1 - abs(y - H / 2) / (H / 2))))
    img.paste(Image.new("RGB", (W, H), (28, 40, 70)), (0, 0), sheen.resize((W, H)))
    _draw_diagonal_accents(img, accent, GOLD)
    draw = ImageDraw.Draw(img, "RGBA")

    TEXT_X = 70
    # Wordmark heading (channel name, left)
    _draw_wordmark(draw, (TEXT_X, 48))

    # Category strip
    label = "TRANSFER UPDATE" if not collapsed else "TRANSFER UPDATE"
    lf = get_premium_font(34, "Bold")
    draw.rounded_rectangle([TEXT_X, 120, TEXT_X + draw.textlength(label, font=lf) + 36, 168],
                           radius=10, fill=(227, 30, 36))
    _draw_text_shadow(draw, (TEXT_X + 18, 126), label, lf, (255, 255, 255), offset=1)

    # Player name
    name_up = player_name.upper()
    TEXT_MAX_W = 760
    nsize = 96
    nf = get_premium_font(nsize, "Black")
    while draw.textlength(name_up, font=nf) > TEXT_MAX_W and nsize > 40:
        nsize -= 3
        nf = get_premium_font(nsize, "Black")
    name_y = 210
    _draw_text_shadow(draw, (TEXT_X, name_y), name_up, nf, (255, 255, 255), offset=3)
    nb = draw.textbbox((0, 0), name_up, font=nf)
    name_bottom = name_y + (nb[3] - nb[1]) + 24

    # FROM / TO rows — TEXT, NO ARROW
    crest_font = get_premium_font(30, "Bold")
    row_label_font = get_premium_font(26, "Bold")
    CREST = 84
    y = name_bottom

    def _row(tag, club_key, club_text, color):
        nonlocal y
        crest = _load_crest(club_key, CREST)
        x = TEXT_X
        _draw_text_shadow(draw, (x, y + (CREST - 30) // 2), tag, row_label_font, (170, 180, 200))
        x += 130
        if crest is not None:
            img.paste(crest, (x, y + (CREST - crest.height) // 2), crest)
            x += CREST + 18
        name = (club_text or (club_key or "").replace("_", " ")).upper()
        _draw_text_shadow(draw, (x, y + (CREST - 34) // 2), name, crest_font, color)
        y += CREST + 18

    if from_key or story.get("from_club"):
        _row("FROM:", from_key, story.get("from_club"), (225, 225, 225))
    if to_key or story.get("to_club"):
        _row("TO:", to_key, story.get("to_club"), (255, 255, 255))

    # detail line
    detail = build_detail_line(story)
    if detail:
        _safe_emoji_text(img, (TEXT_X, y + 8), detail.upper()[:60],
                         get_premium_font(28, "Bold"), (160, 255, 120))

    # portrait (only if verified — never the wrong face)
    if face_verified and player_el:
        pid = player_el.get("code")
        if pid:
            pp = Path(f"players/{pid}.png")
            if not pp.exists():
                _download_asset(f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{pid}.png", pp)
            portrait = _safe_open_rgba(pp)
            if portrait is not None:
                portrait = _fit_contain(portrait, 520, 600)
                img.paste(portrait, (W - portrait.width - 60, H - portrait.height - 100), portrait)

    # bottom bar
    draw.rectangle([0, H - 90, W, H - 12], fill=(20, 24, 33))
    draw.rectangle([0, H - 12, W, H], fill=accent)
    src = " · ".join(f"@{s}" for s in sources[:2])
    bar = f"Source: {src}  |  {CHANNEL_HANDLE}"
    bsize = 30
    bf = get_premium_font(bsize, "Bold")
    while bsize > 18 and draw.textlength(bar, font=bf) > (W - 120):
        bsize -= 1
        bf = get_premium_font(bsize, "Bold")
    bbox = draw.textbbox((0, 0), bar, font=bf)
    by = (H - 90) + (78 - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((60, by), bar, font=bf, fill=(190, 200, 220))
    img.save(filename)


def create_injury_image(story, sources, filename):
    """Injury card: FPL VORTEX wordmark, red INJURY label, detail rows."""
    W, H = 1380, 776
    fpl = fetch_fpl_data()
    player_el = find_player_in_fpl(story.get("player"), fpl)
    player_name = (player_el["web_name"] if player_el else story.get("player")) or "PLAYER"

    img = Image.new("RGB", (W, H), (24, 10, 12))
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rectangle([W // 2, 0, W, H], fill=(120, 18, 22))

    TEXT_X = 70
    _draw_wordmark(draw, (TEXT_X, 48))

    lf = get_premium_font(34, "Bold")
    label = "INJURY UPDATE"
    draw.rounded_rectangle([TEXT_X, 120, TEXT_X + draw.textlength(label, font=lf) + 36, 168],
                           radius=10, fill=(210, 30, 34))
    _draw_text_shadow(draw, (TEXT_X + 18, 126), label, lf, (255, 255, 255), offset=1)

    nf = get_premium_font(88, "Black")
    _draw_text_shadow(draw, (TEXT_X, 210), player_name.upper(), nf, (255, 255, 255), offset=3)

    rows = []
    if story.get("diagnosis"):
        rows.append(("DIAGNOSIS", story["diagnosis"]))
    stage = story.get("stage", 1)
    avail = {4: "Available / fit again", 3: "Ruled out", 2: "Doubt", 1: "To be assessed"}.get(stage, "To be assessed")
    rows.append(("AVAILABILITY", avail))
    rows.append(("TIMELINE", story.get("expected_return") or "Awaiting update"))
    if story.get("next_match"):
        rows.append(("NEXT MATCH", story["next_match"]))

    y = 340
    lab_f = get_premium_font(26, "Bold")
    val_f = get_premium_font(34, "Bold")
    for tag, val in rows[:4]:
        _draw_text_shadow(draw, (TEXT_X, y), tag, lab_f, (255, 140, 140))
        _draw_text_shadow(draw, (TEXT_X, y + 32), str(val), val_f, (255, 255, 255))
        y += 96

    draw.rectangle([0, H - 90, W, H - 12], fill=(20, 10, 12))
    src = " · ".join(f"@{s}" for s in sources[:2])
    bar = f"Source: {src}  |  {CHANNEL_HANDLE}"
    bf = get_premium_font(28, "Bold")
    draw.text((60, H - 70), bar, font=bf, fill=(220, 190, 190))
    img.save(filename)


def create_image(story, sources, filename, rumour=False):
    if story.get("event") == "injury" and not rumour and not story.get("collapsed"):
        try:
            create_injury_image(story, sources, filename)
            return
        except Exception as e:
            print(f"  [IMG] injury card failed, using standard card: {e}")
    create_transfer_image(story, sources, filename, collapsed=bool(story.get("collapsed")))


# ── QUEUE FILES ──────────────────────────────────────────────────────────
def _slug(item):
    return re.sub(r'[^a-z0-9_]', '', item["key"]) + f"_s{item['stage']}"


def save_draft(item, body, image_path):
    """Write a reviewable DRAFT (caption + image + metadata). No posting."""
    draft = dict(item)
    draft["draft_caption"] = body
    draft["draft_image"] = str(image_path) if image_path else None
    draft["drafted_at"] = datetime.now(timezone.utc).isoformat()
    with open(PENDING_DIR / f"{_slug(item)}.json", "w") as f:
        json.dump(draft, f, indent=2, default=str)


# ── SCRAPER ──────────────────────────────────────────────────────────────
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
    tweets = await get_twikit_tweets(read_client, username)
    if tweets:
        return tweets, "twikit"
    nit = get_nitter_tweets(username)
    return nit, ("nitter" if nit else "none")


async def scrape(data, read_client):
    fpl = fetch_fpl_data()
    story_map = {}
    seen = skipped = 0
    accounts_total = len(JOURNALISTS)
    accounts_failed = 0
    for username in JOURNALISTS:
        try:
            tweets, src = await fetch_tweets(read_client, username)
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
            if tid in data["posted_ids"]:
                continue
            if not any(k in text.lower() for k in FOOTBALL_KW):
                continue
            seen += 1
            if tid in data["extracted"]:
                story = dict(data["extracted"][tid])
            else:
                story = build_story(text, fpl)
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
            # Reconcile against same-player+event stories that differ only by
            # club resolution, so "Anderson interest" (unknown) folds into an
            # existing "Anderson to Man City" saga instead of duplicating.
            key = reconcile_key(story["player"], anchor, story["event"],
                                story_map, data.get("stories", {}), data.get("pending", {}))
            ok, reason = should_post(data, key, story["stage"], story["collapsed"])
            if not ok:
                print(f"   skip ({reason}): {key}")
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
                # Fold in any pre-existing 'unknown'-club variant's sources.
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

    # Point 5: surface read-reliability. Low draft counts can be caused by X
    # read failures (expired cookies / Nitter down), NOT by a genuinely quiet
    # news day. Make that distinction explicit in the run output.
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
        for k in list(data["extracted"].keys())[:-600]:
            del data["extracted"][k]
    if len(data["posted_ids"]) > 1500:
        data["posted_ids"] = data["posted_ids"][-1500:]
    save_data(data)
    return sorted(ready, key=lambda x: -(1 if x["collapsed"] else x["stage"]))


# ── DRAFT BUILDER (NO POSTING) ───────────────────────────────────────────
def build_draft(item, data, fpl):
    """Render the preview image + caption and save as a draft. Never posts."""
    valid, why = validate_story(item, fpl)
    if not valid:
        print(f"  VALIDATION FAILED ({why}) — not drafting: {item.get('player')!r}")
        if item.get("id") and item["id"] not in data["posted_ids"]:
            data["posted_ids"].append(item["id"])
        return False

    mode = item.get("mode", "rumour")
    rumour = (mode == "rumour")

    # Hold anything that doesn't resolve to an approved label (e.g. "other").
    label = status_label(item, mode)
    if label is None or label not in APPROVED_LABELS:
        print(f"  HELD (no approved label for event={item.get('event')!r}): {item.get('player')!r}")
        if item.get("id") and item["id"] not in data["posted_ids"]:
            data["posted_ids"].append(item["id"])
        return False

    image_path = PENDING_DIR / f"{_slug(item)}.png"

    # Rule 10: image must succeed; a broken image must not silently pass.
    try:
        create_image(item, item["sources"], str(image_path), rumour=rumour)
        if not image_path.exists() or image_path.stat().st_size < 1000:
            raise RuntimeError("image missing or empty")
    except Exception as e:
        print(f"  [IMG] generation FAILED ({e}) — draft skipped: {item.get('player')!r}")
        return False

    body = trim_for_twitter(build_tweet_body(item, item["sources"], mode), limit=278)
    save_draft(item, body, image_path)
    print(f"  DRAFT READY [{status_label(item, mode)}]: {item['player']} — {item['event']} "
          f"(stage {item['stage']}, {len(item['sources'])} src) -> {image_path.name}")
    return True


async def post_drafts(client: Client):
    """Iterate through pending drafts, post them to X, and move to posted/"""
    pending_files = list(PENDING_DIR.glob("*.json"))
    if not pending_files:
        print("[POST] No drafts available to post.")
        return

    for draft_file in pending_files:
        try:
            with open(draft_file, "r") as f:
                draft = json.load(f)

            caption = draft.get("draft_caption", "")
            image_path = draft.get("draft_image")

            media_ids = None
            if image_path and os.path.exists(image_path):
                print(f"[POST] Uploading media: {image_path}")
                media_id = await client.upload_media(image_path)
                media_ids = [media_id]

            print(f"[POST] Publishing tweet for: {draft.get('player')}")
            await client.create_tweet(text=caption, media_ids=media_ids)

            # Move files out of pending queue to finalize state
            shutil.move(draft_file, POSTED_DIR / draft_file.name)
            if image_path and os.path.exists(image_path):
                img_name = Path(image_path).name
                shutil.move(image_path, POSTED_DIR / img_name)

            print("[POST] Success.")
            await asyncio.sleep(5)

        except Exception as e:
            print(f"[POST] Failed to post draft {draft_file.name}: {e}")

# ── MAIN (AUTO-POST BUILD) ───────────────────────────────────────────────
async def main(post: bool = False):
    print(f"\n[BOT] Run — {datetime.now(timezone.utc).isoformat()} "
          f"(LLM={'Gemini' if _GEMINI_OK else 'off/fallback'}, mode={'POST' if post else 'DRAFT-ONLY'})")
    init_club_data()
    fpl = fetch_fpl_data()
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
        rh = data.get("last_read_health", {})
        if rh.get("fail_ratio", 0) >= 0.5:
          
            print("[BOT] No drafts — but over half of sources failed to read. "
                  "This is likely a READ/access problem, not a quiet news day. "
                  "Verify X cookies and Nitter, then re-run.")
        else:
            print("[BOT] Quiet run. No new stories found (sources read OK).")
        save_data(data)
        return

    drafted = 0
    for item in queue:
        if build_draft(item, data, fpl):
            drafted += 1
            if item.get("id") and item["id"] not in data["posted_ids"]:
                data["posted_ids"].append(item["id"])
            
            data["stories"][item["key"]] = {
                "stage": item["stage"],
                "status": "collapsed" if item.get("collapsed") else "active"
            }
    save_data(data)

    print(f"\n[BOT] {drafted} draft(s) written to {PENDING_DIR}/.")
    if post:
        if read_client:
            print("\n[BOT] Auto-posting is ENABLED. Publishing drafts...")
            await post_drafts(read_client)
        else:
            print("\n[BOT] Cannot post: No authenticated X client available. Ensure variables match active cookies.")
    else:
        print("\n[BOT] --post flag omitted. Run complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FPL VORTEX news bot (draft-only).")
    parser.add_argument("--post", action="store_true",
                        help="(reserved) auto-posting is disabled; drafts only.")
    args = parser.parse_args()
    asyncio.run(main(post=args.post))
