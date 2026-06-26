"""
FPL VORTEX — Football news automation.

What this build does:
  - Scrapes trusted journalist/club accounts.
  - Extracts ONE accurate story per tweet (rule-based regex classifier — no LLM).
  - Strips RT/@handles/URLs/raw repost text; writes an original short summary.
  - Classifies as OFFICIAL / TRANSFER / RUMOUR / INJURY / LOAN / CONTRACT /
    MANAGER using a strict source rule (official OR >= 2 trusted reporters).
  - Renders a clean card: "FPL VORTEX" wordmark top-left, FROM:/TO: text rows
    (NO arrows), club crests, relevant hashtags only.
  - DEFAULT IS LIVE: it AUTO-POSTS confirmed/rumour stories to X, capped per
    run, per hour and per day. Every post is guaranteed an image card.
  - Tweet wording rotates across 3 templates per event type so posts never
    look identical.
"""


import os
import re

from playwright.async_api import async_playwright

import json
import hashlib
import base64
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

FONT = ImageFont.load_default()
font = FONT  # backwards-compat alias for any legacy calls that reference `font`

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

# ── SECRETS ──────────────────────────────────────────────────────────────
X_AUTH_TOKEN = (os.getenv("X_AUTH_TOKEN") or "").strip()
X_CT0_TOKEN = (os.getenv("X_CT0_TOKEN") or "").strip()
X_POST_AUTH_TOKEN = (os.getenv("X_POST_AUTH_TOKEN") or "").strip()
X_POST_CT0_TOKEN = (os.getenv("X_POST_CT0_TOKEN") or "").strip()
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID")

# ── PATHS ────────────────────────────────────────────────────────────────
POSTED_FILE = Path("posted_news.json")
PENDING_DIR = Path("queue/pending")
POSTED_DIR = Path("queue/posted")
DRAFTS_DIR = Path("fpl_drafts")
for d in (PENDING_DIR, POSTED_DIR, Path("logos"), Path("players"), DRAFTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ── CHANNEL BRANDING ─────────────────────────────────────────────────────
CHANNEL_NAME = "FPL VORTEX"
CHANNEL_HANDLE = "@FPLVortex"

# Bump this string whenever extraction/validation logic changes.
# It auto-clears the 'extracted' cache so old tweets re-run through new code.
_LOGIC_VER = "2026-06-25-rawtext"

# ── JOURNALISTS ──────────────────────────────────────────────────────────
JOURNALISTS = [
    "FabrizioRomano", "David_Ornstein", "_pauljoyce", "sistoney67",
    "SamiMokbel_BBC", "JacobsBen", "JamesPearceLFC", "SachaTavolieri",
    "Plettigoal", "MatteoMoretto", "AlfredoPedulla", "DiMarzio",
    "SkySportsNews", "BBCSport", "TheAthleticFC", "guardian_sport",
    "lequipe", "marca", "diarioas", "kicker",
    "alex_crook", "AlexCrabb31", "Transferzone00",
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
    "https://nitter.moomoo.me",
    "https://n.opnxng.com",
    "https://n.s0.gg",
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

CLUB_ALIASES = {
    "arsenal": "Arsenal",
    "aston villa": "Aston_Villa", "villa": "Aston_Villa", "avfc": "Aston_Villa",
    "bournemouth": "Bournemouth", "afcb": "Bournemouth",
    "brentford": "Brentford",
    "brighton": "Brighton", "bhafc": "Brighton",
    "burnley": "Burnley",
    "chelsea": "Chelsea", "cfc": "Chelsea",
    "crystal palace": "Crystal_Palace", "palace": "Crystal_Palace", "cpfc": "Crystal_Palace",
    "everton": "Everton", "efc": "Everton",
    "fulham": "Fulham", "ffc": "Fulham",
    "ipswich": "Ipswich", "ipswich town": "Ipswich", "itfc": "Ipswich",
    "leeds": "Leeds", "leeds united": "Leeds", "lufc": "Leeds",
    "leicester": "Leicester", "leicester city": "Leicester", "lcfc": "Leicester",
    "liverpool": "Liverpool", "lfc": "Liverpool",
    "manchester city": "Man_City", "man city": "Man_City", "mcfc": "Man_City", "city": "Man_City",
    "manchester united": "Man_Utd", "man united": "Man_Utd", "man utd": "Man_Utd", "mufc": "Man_Utd",
    "newcastle": "Newcastle", "newcastle united": "Newcastle", "nufc": "Newcastle",
    "nottingham forest": "Nottm_Forest", "nott'm forest": "Nottm_Forest", "forest": "Nottm_Forest", "nffc": "Nottm_Forest",
    "southampton": "Southampton", "saintsfc": "Southampton",
    "sunderland": "Sunderland",  # optional coverage
    "tottenham": "Spurs", "spurs": "Spurs", "tottenham hotspur": "Spurs", "thfc": "Spurs",
    "west ham": "West_Ham", "west ham united": "West_Ham", "whufc": "West_Ham",
    "wolves": "Wolves", "wolverhampton": "Wolves"
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
    "Brighton": "36", "Burnley": "90", "Chelsea": "8", "Crystal_Palace": "31", "Everton": "11",
    "Fulham": "54", "Ipswich": "40", "Leeds": "2", "Leicester": "13", "Liverpool": "14",
    "Man_City": "43", "Man_Utd": "1", "Newcastle": "4", "Nottm_Forest": "17",
    "Southampton": "20", "Spurs": "6", "Sunderland": "56", "West_Ham": "21", "Wolves": "39",
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
    if not name: return None
    n = name.lower()
    for alias in _SORTED_ALIASES:
        if re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', n):
            return CLUB_ALIASES[alias]
    return None

# ── CLUBS_CACHE WIRING ───────────────────────────────────────────────────
CLUB_NAME_SET = set()
CLUB_HASHTAGS = {}
PL_CLUB_NAMES = set()

def init_club_data():
    global CLUB_NAME_SET, CLUB_HASHTAGS, PL_CLUB_NAMES
    CLUB_HASHTAGS = CLUB_HASHTAG_MAP.copy()
    PL_CLUB_NAMES = set(CLUB_ALIASES.keys())
    CLUB_NAME_SET = set(CLUB_HASHTAGS.keys()) | set(CLUB_ALIASES.keys())
    _build_club_word_fragments()
    print(f"[CLUBS] Loaded {len(PL_CLUB_NAMES)} clubs from CLUB_ALIASES.")
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
    fresh = {"daily": {"date": "", "count": 0, "limit": 24}, "stories": {}, "posted_ids": []}
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
    if d.get("_logic_ver") != _LOGIC_VER:
        print(f"[STATE] logic version changed -> clearing {len(d.get('extracted', {}))} cached extractions.")
        d["extracted"] = {}
        d["_logic_ver"] = _LOGIC_VER
    d.setdefault("posted_hashes", [])
    d.setdefault("posted_headlines", [])
    d["posted_hashes"] = [h for h in d["posted_hashes"] if "|" not in h]

    _reset_names = {"dubravka", "wilson", "verkooijen"}
    d["posted_headlines"] = [h for h in d["posted_headlines"] if not any(n in h.lower() for n in _reset_names)]
    d["posted_hashes"] = [h for h in d["posted_hashes"] if not any(n in h.lower() for n in _reset_names)]
    d["posted_ids"] = [i for i in d["posted_ids"] if not any(n in str(i).lower() for n in _reset_names)]
    for k in list(d.get("stories", {}).keys()):
        if any(n in k.lower() for n in _reset_names):
            del d["stories"][k]
    return d

def save_data(data: dict):
    tmp = POSTED_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f: json.dump(data, f, indent=2)
    tmp.replace(POSTED_FILE)

def check_daily_limit(data: dict) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if data["daily"]["date"] != today:
        data["daily"] = {"date": today, "count": 0, "limit": 24}
    return data["daily"]["count"] < data["daily"]["limit"]

def increment_daily(data: dict):
    data["daily"]["count"] += 1

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

# ── STORY EXTRACTION (regex-only, no LLM) ────────────────────────────────
def _clean_source_text(text: str) -> str:
    t = text or ""
    t = re.sub(r'\bRT\s+@\w+:?', ' ', t)
    t = re.sub(r'https?://\S+|www\.\S+', ' ', t)
    
    # ARCHITECT FIX: Strip only the @ and # characters, preserving the actual club names
    t = re.sub(r'[@#]', '', t)
    
    t = re.sub(r'["""]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t

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

    loan_signal = ("on loan" in tl) or bool(re.search(r"\bjoine?d?\b.*\bon loan\b", tl))
    if has_word(["suspended", "suspension", "banned", "ban", "red card", "sent off"], tl): event = "suspension"
    elif loan_signal: event = "loan"
    elif has_word(["injury", "injured", "ruled out", "scan", "hamstring", "surgery", "doubt"], tl): event = "injury"
    elif has_word(["sack", "appoint", "head coach", "manager"], tl): event = "manager"
    elif has_word(["new deal", "new contract", "signs new", "extension", "renew"], tl): event = "renewal"
    elif has_word(["stay", "staying", "no exit", "not for sale", "remain"], tl) and not has_word(["sign for", "joins", "move to"], tl): event = "stay"
    elif has_word(["loan"], tl): event = "loan"
    else: event = "transfer"

    stage = 4 if has_word(["here we go", "official", "confirmed", "completed", "joins"], tl) else \
        2 if has_word(["agreement", "agreed", "advanced", "personal terms"], tl) else 1

    confidence_signals = 0
    if stage >= 4: confidence_signals += 3
    elif stage >= 2: confidence_signals += 1
    if has_word(["here we go", "official", "confirmed", "done deal", "medical"], tl): confidence_signals += 2
    if has_word(["talks", "interest", "target", "bid", "offer"], tl): confidence_signals += 1
    confidence = min(0.95, 0.45 + confidence_signals * 0.1)

    FILLER = {"excl", "exclusive", "breaking", "official", "understand", "understands",
              "update", "here", "done", "deal", "medical", "nothing", "all", "source",
              "news", "report", "reports", "told", "says", "said", "claim", "claims",
              "today", "tonight", "tomorrow", "now", "latest", "just", "also",
              "meanwhile", "plus", "however", "elsewhere", "separately",
              "full", "free", "new", "big", "top", "key", "real", "transfer",
              "window", "deadline", "fee", "bid", "offer", "loan", "agree", "agreed",
              "talks", "interest", "signed", "signing", "joins", "joined", "move",
              "permanent", "option", "clause", "release", "extension", "premier",
              "league", "champions", "europa", "conference", "sport", "press",
              "watch", "video", "highlights", "live", "stream", "footage",
              "scenes", "behind", "relive", "throwback"}
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

    if name and not _is_safe_fallback_name(name):
        name = None

    clubs = []
    for alias in _SORTED_ALIASES:
        if re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', tl):
            k = CLUB_ALIASES[alias]
            if k not in clubs: clubs.append(k)

    fpl_player_el = find_player_in_fpl(name, fpl_data) if name and fpl_data else None
    actual_current_club_key = fpl_team_key(fpl_player_el, fpl_data) if fpl_player_el else None

    from_key = None
    to_key = None
    direction_confident = False
    from_anchor = None

    if actual_current_club_key:
        from_key = actual_current_club_key
        other_clubs = [c for c in clubs if c != actual_current_club_key]
        if other_clubs:
            to_key = other_clubs[0]
            direction_confident = True
        else:
            to_key = None
            direction_confident = event in ("stay", "renewal", "injury", "suspension")
    else:
        if clubs:
            to_key = clubs[0]
            if len(clubs) > 1:
                from_key = clubs[1]
            direction_confident = False

    is_collapsed = has_word(["collapsed", "called off", "rejected", "deal off"], tl)

    if event in ("stay", "renewal"):
        if to_key and not from_key:
            from_key, to_key = to_key, None
        to_key = None
        is_collapsed = False

    if is_collapsed and to_key and not from_anchor:
        to_key = None

    fee_match = re.search(r'([£€$]\d+(?:\.\d+)?\s*(?:m|k|million|billion))', cleaned, re.IGNORECASE)
    extracted_fee = fee_match.group(1).upper() if fee_match else None

    return {
        "is_football": True, "event": event,
        "is_real_move": event in ("transfer", "loan", "loan_option"),
        "player": name,
        "from_club": (from_key.replace("_", " ") if from_key else None),
        "to_club": (to_key.replace("_", " ") if to_key else None),
        "from_key": from_key, "to_key": to_key,
        "fee": extracted_fee, "contract": None, "conditional": None, "fpl_impact": None,
        "diagnosis": None, "expected_return": None, "next_match": None,
        "stage": stage, "collapsed": is_collapsed,
        "headline": name if name else "Transfer update",
        "body": tweet_text, "confidence": confidence,
        "direction_confident": direction_confident,
        "from_fallback": True,
    }

def _summarise(name, event, from_key, to_key, stage, collapsed):
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
    
_CLAIM_MARKERS = re.compile(
    r'\b(agree[d]?|sign[ed|ing]*|join[s|ed|ing]*|move[s|d]*|deal|bid|offer|'
    r'medical|here we go|loan|contract|talks|fee|ruled out|injur|suspend|'
    r'set to|close to|advanced|personal terms|confirmed|official)\b', re.I)

def looks_like_video_post(tweet_text: str) -> bool:
    return bool(_VIDEO_MARKERS.search(tweet_text or ""))

def has_written_claim(tweet_text: str) -> bool:
    cleaned = _clean_source_text(tweet_text)
    return bool(_CLAIM_MARKERS.search(cleaned)) and len(cleaned.split()) >= 5

ALLOW_HISTORICAL_POSTS = False

_HISTORICAL_MARKERS = re.compile(
    r"\b(on this day|on this date|this day in|otd|\d+\s+years?\s+(ago|on)|"
    r"years?\s+ago|anniversary|throwback|#tbt|flashback|remember when|"
    r"back in (the\s+)?(19|20)\d\d|years on|on this very day)\b", re.I)

_FRESH_CUE = re.compile(
    r"\b(today|tonight|tomorrow|breaking|here we go|confirmed|just (in|now)|"
    r"official|now|set to|close to|agreed|agree|recovered|back in training|"
    r"returns? to training|fit again|stepped up|ruled fit|available again|"
    r"new deal|signs new|signed new|extension)\b", re.I)

_RECYCLED_STATUS = re.compile(
    r"\b(has|have)\s+joined\b.*\bon loan\b|"
    r"\bon loan\b.*\b(rest of the|until end of|for the season)\b|"
    r"\bfor the rest of the (season|campaign)\b", re.I)

def detect_historical(text: str) -> bool:
    t = text or ""
    tl = t.lower()
    has_fresh = bool(_FRESH_CUE.search(tl))
    if _HISTORICAL_MARKERS.search(tl) and not has_fresh:
        return True
    if re.search(r"\b19\d\d\b", t):
        return True
    cur = datetime.now(timezone.utc).year
    for y in re.findall(r"\b(?:in|back in|during|on)\s+(20\d\d)\b", tl):
        if int(y) <= cur - 2:
            return True
    if _RECYCLED_STATUS.search(tl) and not has_fresh:
        return True
    return False

# ── STORY BUILDER (COMBINED & CORRECTLY PLACED) ──────────────────────────
def build_story(tweet_text, fpl_data):
    s = extract_story_fallback(tweet_text, fpl_data)

    if fpl_data and s.get("player") and s.get("event") in ("transfer", "loan", "loan_option"):
        el = find_player_in_fpl(s["player"], fpl_data)
        is_free_agent = bool(el and el.get("team", 0) == 0)
        actual_club = fpl_team_key(el, fpl_data) if el else None
        
        if actual_club and not is_free_agent and not s.get("from_key"):
            s["from_key"] = actual_club
            s["from_club"] = actual_club.replace("_", " ")

    try: 
        s["stage"] = max(1, min(4, int(s.get("stage", 1))))
    except Exception: 
        s["stage"] = 1
        
    s["collapsed"] = bool(s.get("collapsed"))
    s["historical"] = detect_historical(tweet_text)

    if looks_like_video_post(tweet_text):
        s["from_video"] = True
        s["has_written_claim"] = has_written_claim(tweet_text)
        if s["stage"] > 2: 
            s["stage"] = 2
    else:
        s["from_video"] = False
        s["has_written_claim"] = True

    s["raw_text"] = tweet_text
    if len(s.get("body", "").split()) < 4:
        s["body"] = _summarise(s.get("player"), s.get("event"),
                               s.get("from_key"), s.get("to_key"),
                               s.get("stage"), s.get("collapsed"))
    else:
        s["body"] = _clean_source_text(s["body"])
    return s

# ── DEDUP / PROGRESSION ──────────────────────────────────────────────────
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2190-\u21FF\u2B00-\u2BFF\uFE0F]")

def _norm_text(s: str) -> str:
    s = (s or "").lower()
    s = _EMOJI_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def content_hash(story: dict) -> str:
    parts = [
        _event_family(story.get("event")),
        _norm_text(story.get("player")),
        _norm_text(story.get("from_key") or story.get("from_club")),
        _norm_text(story.get("to_key") or story.get("to_club")),
        _norm_text(story.get("headline")),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

def is_duplicate_content(story: dict, data: dict, threshold: float = 0.90):
    h = content_hash(story)
    if h in data.get("posted_hashes", []): return True, "content_hash"
    
    player_name = _norm_text(story.get("player") or "")
    event_type = _norm_text(story.get("event") or "")
    stage_num = str(story.get("stage", 1))
    is_collapsed = "collapsed" if story.get("collapsed") else "active"
    
    if not player_name:
        return False, ""
        
    head = f"{player_name}_{event_type}_stage{stage_num}_{is_collapsed}"
    
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
    if len(data["posted_hashes"]) > 2000: data["posted_hashes"] = data["posted_hashes"][-2000:]
    if len(data["posted_headlines"]) > 2000: data["posted_headlines"] = data["posted_headlines"][-2000:]

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

def detect_mixed_story(story, raw_text, fpl_data=None) -> str:
    text = (raw_text or "")
    tl = text.lower()
    player = (story.get("player") or "").lower()
    ev = story.get("event")
    if ev != "manager" and player and (player in MANAGER_SURNAMES or any(m in player for m in MANAGER_SURNAMES)):
        return "player_is_manager"

    has_clear_direction = bool(
        (story.get("to_key") or story.get("to_club")) or
        (story.get("from_key") or story.get("from_club")))
    if ev in ("transfer", "loan", "loan_option", "stay", "renewal") and not has_clear_direction:
        subject_is_manager = any(m in player for m in MANAGER_SURNAMES)
        if not subject_is_manager:
            clauses = re.split(r'[.;]|\bmeanwhile\b|\balso\b|\bplus\b|\belsewhere\b|\bseparately\b', tl)
            player_first = player.split()[0] if player else ""
            for clause in clauses:
                has_manager_here = any(
                    re.search(r'(?<![a-z])' + re.escape(m) + r'(?![a-z])', clause)
                    for m in MANAGER_SURNAMES)
                player_in_clause = bool(player_first and player_first in clause)
                if has_manager_here and not player_in_clause:
                    return "manager_and_player_mixed"

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
    deduped = set()
    for n in sorted(name_candidates, key=len, reverse=True):
        if not any(n != o and n in o for o in deduped): deduped.add(n)
    if fpl_data is not None:
        distinct = {n for n in deduped if find_player_in_fpl(n, fpl_data) is not None}
    else:
        distinct = deduped
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
    if not story.get("is_football"): return False, "not_football"
    if tweet_too_old(story.get("created_at")): return False, f"older_than_{MAX_TWEET_AGE_DAYS}d"
    if story.get("historical") and not ALLOW_HISTORICAL_POSTS: return False, "historical_news"
    if story.get("confidence", 0) < 0.40: return False, "low_confidence"
    if any(re.search(r'(?<![a-z])' + re.escape(w) + r'(?![a-z])', tl) for w in STAFF_BLOCK_KW): return False, "staff_or_offpitch"
    if not story.get("player"): return False, "no_player"
    
    mixed = detect_mixed_story(story, raw_text, fpl_data)
    tiers = [source_tier(s) for s in (sources or [])]
    is_elite = any(t in (1, 2) for t in tiers)
    
    if mixed and not is_elite: 
        return False, f"mixed_story:{mixed}"
    if story.get("from_video") and not story.get("has_written_claim"): return False, "video_no_written_claim"
    if player_already_at_club(story, fpl_data): return False, "already_at_destination"

    # --- 1. MANAGER GATE: STRICTLY EPL CLUBS ---
    if story["event"] == "manager":
        to_key = story.get("to_key")
        to_club = story.get("to_club")
        pl_club = bool(to_key) or (to_club and to_club.lower() in PL_CLUB_NAMES)
        if not pl_club: return False, "manager_no_pl_club"
        
        appoint_cue = re.search(
            r"\b(appoint|appointed|new (head coach|manager|boss)|"
            r"set to (become|take over|be appointed)|sacked|"
            r"named (as )?(head coach|manager)|takes over|"
            r"agree(s|d)? to (become|join)|done deal)\b", tl)
        if not appoint_cue: return False, "manager_no_appointment_cue"
        return True, "ok_manager"

    pl_player = find_player_in_fpl(story["player"], fpl_data) is not None

    # --- 2. INJURY/SUSPENSION GATE: STRICTLY FPL PLAYERS ---
    if story["event"] in ("injury", "suspension"):
        injury_source_ok = any(t in (1, 2) for t in tiers) or \
            any((s or "").lower().lstrip("@") in OFFICIAL_INJURY_ACCOUNTS for s in sources)
        if not injury_source_ok: return False, "injury_source_not_approved"
        
        if pl_player: return True, f"ok_{story['event']}"
        return False, f"{story['event']}_not_pl_player"

    # --- 3. TRANSFER/LOAN GATE: MUST INVOLVE EPL CLUB OR FPL PLAYER ---
    pl_club = bool(story.get("to_key") or story.get("from_key"))
    if not pl_club:
        for nm in (story.get("to_club"), story.get("from_club")):
            if nm and nm.lower() in PL_CLUB_NAMES:
                pl_club = True
                break

    if not (pl_player or pl_club):
        return False, "not_pl_relevant"

    # Elite sources are trusted to report on players entering the EPL before they hit the API
    if not is_elite and not pl_player:
        return False, "player_not_verified_fpl"

    if pl_player and (pl_club or story.get("to_club") or story.get("from_club")):
        return True, "ok_pl_transfer"
        
    if pl_player:
        return True, "ok_verified_pl_player_staying"

    elite_source = any(t == 2 for t in tiers)
    if elite_source and pl_club: return True, "ok_elite_source_incoming_pl_transfer"
    
    return False, "not_pl_relevant_catchall"

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
        
        # New "Catch-All": Check if ANY club alias exists anywhere in the tweet body/text
        tweet_body = (story.get("body", "") + " " + (story.get("headline", "") or "")
                      + " " + (story.get("raw_text", "") or "")).lower()
        has_any_club = any(
            re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', tweet_body)
            for alias in CLUB_ALIASES
        )
        
        if not (tk or story.get("to_club") or fk or story.get("from_club") or has_any_club): 
            return False, "no_clubs"
        leak = (story.get("body", "") + " " + story.get("headline", "")).lower()
        if re.search(r'\b(head coach|sacked|appointed as manager|hamstring|ruled out for)\b', leak): return False, "event_data_mismatch"
    if ev == "manager" and not (story.get("to_key") or story.get("to_club")): return False, "manager_no_club"
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
    ev = story["event"]
    tags = list(BASE_TAGS)
    if ev in ("injury", "suspension"): tags.append("#InjuryNews")
    elif ev in ("transfer", "loan", "loan_option", "renewal", "stay"): tags.append("#Transfers")
    else: tags.append("#FootballNews")
    for key, name in ((story.get("to_key"), story.get("to_club")),
                      (story.get("from_key"), story.get("from_club"))):
        ht = hashtag_for(key) or hashtag_for(name)
        if ht and ht not in tags: tags.append(ht)
    if (story.get("to_key") or story.get("from_key")) and "#PremierLeague" not in tags:
        tags.append("#PremierLeague")
    return " ".join(tags[:4])

# ── TWEET TEXT ───────────────────────────────────────────────────────────
_TWEET_TEMPLATES = {
    "OFFICIAL": [
        "✅ OFFICIAL | {player} completes move to {dest}!",
        "🔵 DONE DEAL | {player} joins {dest} — it's fully confirmed!",
        "🚨 HERE WE GO | {player} is officially a {dest} player!",
        "💎 SIGNED & SEALED | {player} has finalized his move to {dest}!",
        "🤝 IT'S ANNOUNCED | {player} unrevealed as a new signing for {dest}!",
        "🏟️ NEW ERA | {player} begins a new chapter at {dest}!",
    ],
    "TRANSFER": [
        "🔴 TRANSFER | {player} linked with a move to {dest}.",
        "⚡ TRANSFER NEWS | {player} attracting serious interest from {dest}.",
        "📋 TRANSFER UPDATE | {player} is on {dest}'s radar this window.",
        "🔥 MARKET TALK | {dest} are monitoring the situation of {player}.",
        "🎯 TARGET SPOTTED | {dest} identifying {player} as a key option.",
        "📈 MOVE POSSIBLE | Discussions surrounding {player} to {dest} gathering pace.",
    ],
    "RUMOUR": [
        "👀 RUMOUR | {player} being linked with {dest} — unconfirmed.",
        "🔍 TRANSFER TALK | Speculation suggests {player} could move to {dest}.",
        "💬 UNCONFIRMED | {player} mentioned in connection with {dest}.",
        "📰 PRESS REPORTS | Gossip linking {player} with a potential switch to {dest}.",
        "🔮 WHISPERS | Internal chatter suggests {dest} might look at {player}.",
        "📡 ON THE RADAR | Rumours growing over {player} testing the waters with {dest}.",
    ],
    "INJURY": [
        "🚑 INJURY NEWS | {player} facing a spell on the sidelines.",
        "❌ INJURY UPDATE | {player} being assessed — FPL managers take note!",
        "⚠️ FITNESS CONCERN | {player} pickup an issue, confirms {origin}.",
        "🏥 MEDICAL ROOM | {player} undergoing tests following a fresh setback.",
        "💔 FPL BLOW | {player} sustained an injury and is set for a scans.",
        "⏳ TIMELINE PENDING | {player} is a major doubt for upcoming fixtures.",
    ],
    "LOAN": [
        "🔄 LOAN DEAL | {player} set for a temporary move to {dest}.",
        "📤 LOAN UPDATE | {player} heading to {dest} on a short-term switch.",
        "🤝 LOAN MOVE | {player} closing in on a temporary contract with {dest}.",
        "🚀 TEMPORARY SWITCH | {player} departs on loan to join {dest}.",
        "📦 SENT ON LOAN | {player} will spend the next stage of the season at {dest}.",
        "📈 DEVELOPMENT Swapping shirts: {player} completes loan move to {dest}.",
    ],
    "SUSPENSION": [
        "🟥 SUSPENSION | {player} set to miss upcoming fixtures.",
        "⛔ BANNED | {player} faces a suspension penalty — check your FPL lines!",
        "🚫 SUSPENDED | {player} ruled out of the selection pool through a disciplinary ban.",
        "🟨 CARD TROUBLE | Disciplinary action sidelines {player} for the upcoming matches.",
        "❌ RULED OUT | {player} will serve a suspension block starting immediately.",
        "⚖️ DISCIPLINARY | {player} faces a mandatory layout suspension.",
    ],
    "CONTRACT EXTENSION": [
        "📝 NEW DEAL | {player} set to extend his stay at {origin}!",
        "🖊️ CONTRACT | {player} closing in on a brand new deal at {origin}!",
        "✍️ STAYING PUT | {player} commits his future by signing a new contract!",
        "🔒 LOCKED IN | {player} pens a renewal deal to stay with {origin}!",
        "💎 EXTENSION | {player} rejects exit talks and extends with {origin}!",
        "👑 FUTURE SECURED | {player} stays right where he is at {origin}!",
    ],
    "MANAGER NEWS": [
        "🎩 MANAGER | {player} in the frame for the empty {dest} job.",
        "👔 MANAGERIAL | {player} heavily linked with the {dest} hotseat.",
        "📣 MANAGER NEWS | {player} being seriously considered at {dest}.",
        "🗂️ DUGOUT SEARCH | {dest} open discussions over appointing {player}.",
        "🧠 TACTICAL SHIFT | {player} leading the race to become the new boss at {dest}.",
        "📋 APPOINTMENT PENDING | {player} enters advanced stages for the {dest} vacancy.",
    ],
    "HISTORICAL": [
        "📅 HISTORICAL | {player} — {dest}.",
        "🕰️ ON THIS DAY | Looking back at {player} — {dest}.",
        "📖 FLASHBACK | Iconic moments: {player} — {dest}.",
        "⏪ REWIND | Throwback file on {player} during his time with {dest}.",
        "🎞️ MEMORY LANE | Celebrating {player} and his milestones at {dest}.",
        "🌟 RETRO ARCHIVE | Unlocking a classic moment involving {player} and {dest}.",
    ],
}

def _pick_template(key: str, templates: list) -> str:
    idx = int(hashlib.md5((key or "default").encode()).hexdigest(), 16) % len(templates)
    return templates[idx]

def build_tweet_body(story, sources, mode) -> str:
    label = status_label(story, mode)
    if label is None:
        label = "TRANSFER"

    player = story.get("player") or "Transfer update"
    to_club = (story.get("to_club") or (story.get("to_key") or "").replace("_", " ")).strip()
    from_club = (story.get("from_club") or (story.get("from_key") or "").replace("_", " ")).strip()
    dest = to_club or from_club or "a new club"
    origin = from_club or to_club or "their current club"

    templates = _TWEET_TEMPLATES.get(label, _TWEET_TEMPLATES["TRANSFER"])
    template = _pick_template(story.get("key", player), templates)

    first_line = template.format(player=player, dest=dest, origin=origin)
    return first_line + "\n\n" + build_hashtags(story)

def build_detail_line(story) -> str:
    bits = []
    if story.get("fee"): bits.append(story["fee"])
    if story.get("contract"): bits.append(story["contract"])
    if story.get("conditional"): bits.append(story["conditional"])
    return "  |  ".join(bits)

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
    except Exception as e:
        print(f"  [IMG ERROR] PIL failed to open or convert {path}: {e}")
        return None

def _fit_contain(im, w, h):
    return ImageOps.contain(im, (w, h), Image.Resampling.LANCZOS)

def _draw_text_shadow(draw, xy, text, font, fill, shadow=(0, 0, 0), offset=2):
    x, y = xy
    draw.text((x + offset, y + offset), text, font=font or FONT, fill=shadow)
    draw.text((x, y), text, font=font or FONT, fill=fill)

def _safe_emoji_text(img, xy, text, font, fill):
    try:
        with Pilmoji(img) as pj:
            pj.text(xy, text, font=font or FONT, fill=fill)
    except Exception:
        plain = _EMOJI_RE.sub("", text).strip()
        ImageDraw.Draw(img).text(xy, plain, font=font or FONT, fill=fill)

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

def _draw_wordmark(draw, xy):
    x, y = xy
    f = get_premium_font(46, "Black")
    _draw_text_shadow(draw, (x, y), "FPL", f, (255, 255, 255), offset=2)
    fpl_w = draw.textlength("FPL ", font=f)
    _draw_text_shadow(draw, (x + fpl_w, y), "VORTEX", f, (84, 224, 124), offset=2)

def _draw_right_visual_fallback(img, draw, W, H, story):
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

def get_club_color(club_key):
    """Retrieve the exact RGB string for the destination club."""
    color_tuple = CLUB_COLORS.get(club_key, (84, 224, 124)) # Default to VORTEX Green
    return f"rgb({color_tuple[0]}, {color_tuple[1]}, {color_tuple[2]})"

def _render_html_sync(html_content, filename, error_box=None):
    """Helper function to run Playwright in a separate thread to prevent asyncio crashes."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1380, "height": 776}, device_scale_factor=1)
            page.set_content(html_content, wait_until="domcontentloaded")
            page.wait_for_timeout(500)
            page.screenshot(path=filename)
            browser.close()
    except Exception:
        if error_box is not None:
            import traceback
            error_box.append(traceback.format_exc())

def create_transfer_image(story, sources, filename, collapsed=False):
    """Generates a premium sports broadcast graphic using HTML/CSS and Playwright."""
    fpl = fetch_fpl_data()
    player_el = find_player_in_fpl(story.get("player"), fpl)
    player_name = (player_el["web_name"] if player_el else story.get("player")) or "PLAYER"

    to_club = story.get("to_club") or (story.get("to_key") or "").replace("_", " ")
    from_club = story.get("from_club") or (story.get("from_key") or "").replace("_", " ")

    mode = story.get("mode", "confirmed")
    if collapsed or story.get("collapsed"):
        status = "DEAL COLLAPSED"
        badge_color = "#e31e24"
    elif mode == "rumour":
        status = "TRANSFER RUMOUR"
        badge_color = "#e31e24"
    else:
        status = "OFFICIAL" if story.get("stage", 1) >= 4 else "CONFIRMED"
        badge_color = "#54e07c"

    club_color = get_club_color(story.get("to_key") or story.get("from_key"))
    source_text = " · ".join(f"@{s}" for s in sources[:2])

    # Bot logo
    logo_data_uri = ""
    logo_path = Path("Logo.png")
    if logo_path.exists() and logo_path.stat().st_size >= 500:
        logo_data_uri = "data:image/png;base64," + base64.b64encode(logo_path.read_bytes()).decode("ascii")

    # Tier 1: FPL player photo
    photo_data_uri = None
    pid = player_el.get("code") if player_el else None
    if pid:
        pp = Path(f"players/{pid}.png")
        if not pp.exists():
            _download_asset(f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{pid}.png", pp)
        if pp.exists() and pp.stat().st_size >= 500:
            photo_data_uri = "data:image/png;base64," + base64.b64encode(pp.read_bytes()).decode("ascii")

    # Tier 2: tweet image
    if not photo_data_uri and story.get("media_url"):
        murl = story["media_url"]
        mp = Path("players/tw_" + hashlib.md5(murl.encode()).hexdigest()[:12] + ".png")
        if not mp.exists():
            _download_asset(murl, mp)
        if mp.exists() and mp.stat().st_size >= 500:
            photo_data_uri = "data:image/png;base64," + base64.b64encode(mp.read_bytes()).decode("ascii")

    # Helper: get crest data URI for a club key
    def _crest_uri(club_key):
        if not club_key: return ""
        safe = club_key.replace(" ", "_").replace("'", "")
        cp = Path(f"logos/{safe}.png")
        if not cp.exists() and FPL_LOGO_IDS.get(safe):
            _download_asset(f"https://resources.premierleague.com/premierleague/badges/t{FPL_LOGO_IDS[safe]}.png", cp)
        if cp.exists() and cp.stat().st_size >= 500:
            return "data:image/png;base64," + base64.b64encode(cp.read_bytes()).decode("ascii")
        return ""

    # Main crest (for photo panel)
    crest_key = story.get("to_key") or story.get("from_key")
    main_crest_uri = _crest_uri(crest_key)

    # From/To crests (for details grid)
    from_crest_uri = _crest_uri(story.get("from_key"))
    to_crest_uri = _crest_uri(story.get("to_key"))

    # Photo panel content
    crest_img_html = f'<img class="crest-badge" src="{main_crest_uri}" />' if main_crest_uri else ''
    if photo_data_uri:
        photo_img_html = f'<img src="{photo_data_uri}" style="width:100%;height:100%;object-fit:cover;position:relative;z-index:1;" />'
    else:
        if main_crest_uri:
            photo_img_html = f'<img src="{main_crest_uri}" style="width:70%;height:70%;object-fit:contain;position:relative;z-index:1;opacity:0.85;" />'
        elif logo_data_uri:
            photo_img_html = f'<img src="{logo_data_uri}" style="width:75%;height:75%;object-fit:contain;position:relative;z-index:1;opacity:1.0;filter:drop-shadow(0 0 30px rgba(84,224,124,0.8));" />'
        else:
            photo_img_html = f'<div style="z-index:1;font-size:150px;color:rgba(255,255,255,0.15);font-weight:900;">V</div>'

    # Club name + crest inline HTML helper
    def _club_with_crest(name, crest_uri):
        if not name: return "TBD"
        if crest_uri:
            return f'{name} <img src="{crest_uri}" style="width:52px;height:52px;object-fit:contain;vertical-align:middle;margin-left:10px;filter:drop-shadow(0 2px 4px rgba(0,0,0,0.5));" />'
        return name

    from_html = _club_with_crest(from_club or "TBD", from_crest_uri)
    to_html = _club_with_crest(to_club or "TBD", to_crest_uri)
    fee_value = story.get('fee') or "Undisclosed"
    if fee_value and fee_value != "Undisclosed" and not fee_value.startswith("$"):
        fee_value = "$" + fee_value

    logo_html = f'<img src="{logo_data_uri}" style="width:64px;height:64px;object-fit:contain;margin-right:16px;filter:drop-shadow(0 2px 6px rgba(0,0,0,0.6));" />' if logo_data_uri else ''

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@700;900&display=swap');

            body {{
                margin: 0;
                padding: 0;
                width: 1380px;
                height: 776px;
                background: linear-gradient(135deg, #0b1220 0%, #1c2846 100%);
                font-family: 'Montserrat', sans-serif;
                color: white;
                display: flex;
                overflow: hidden;
                position: relative;
            }}

            .accent-slash {{
                position: absolute;
                width: 200%;
                height: 100px;
                background: {club_color};
                opacity: 0.15;
                transform: rotate(-35deg) translateY(-200px);
                z-index: 0;
            }}
            .accent-slash:nth-child(2) {{ transform: rotate(-35deg) translateY(200px); opacity: 0.05; }}

            .container {{
                width: 100%;
                height: 100%;
                display: flex;
                flex-direction: row;
                padding: 40px 60px 80px 60px;
                box-sizing: border-box;
                z-index: 1;
            }}

            .left-column {{
                flex: 1;
                display: flex;
                flex-direction: column;
                justify-content: flex-start;
                padding-top: 30px;
            }}

            .right-column {{
                width: 420px;
                display: flex;
                align-items: center;
                justify-content: flex-end;
            }}

            .wordmark {{
                font-size: 52px;
                font-weight: 900;
                margin-bottom: 24px;
                text-shadow: 0 4px 10px rgba(0,0,0,0.5);
                display: flex;
                align-items: center;
            }}
            .wordmark span {{ color: #54e07c; margin-left: 10px; }}

            .status-badge {{
                display: inline-block;
                background: {badge_color};
                color: #fff;
                padding: 14px 30px;
                font-size: 42px;
                font-weight: 900;
                border-radius: 12px;
                letter-spacing: 3px;
                margin-bottom: 20px;
                text-transform: uppercase;
                box-shadow: 0 8px 20px rgba(0,0,0,0.4);
            }}

            .player-name {{
                font-size: 88px;
                font-weight: 900;
                line-height: 1.0;
                text-transform: uppercase;
                margin-bottom: 28px;
                text-shadow: 0 8px 20px rgba(0,0,0,0.6);
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
                max-width: 820px;
            }}

            .details-grid {{
                display: grid;
                grid-template-columns: 130px 1fr;
                gap: 18px 30px;
                font-size: 44px;
            }}

            .detail-label {{ font-weight: 700; text-transform: uppercase; }}
            .detail-label.from {{ color: #f5c518; }}
            .detail-label.to {{ color: #00d4ff; }}
            .detail-label.fee {{ color: #e31e24; }}
            .detail-value {{ font-weight: 900; text-transform: uppercase; color: white; display: flex; align-items: center; }}

            .photo-panel {{
                width: 370px;
                height: 560px;
                background: rgba(255, 255, 255, 0.03);
                backdrop-filter: blur(20px);
                border: 2px solid rgba(255, 255, 255, 0.1);
                border-radius: 24px;
                display: flex;
                align-items: center;
                justify-content: center;
                box-shadow: 0 20px 50px rgba(0,0,0,0.5);
                position: relative;
                overflow: hidden;
            }}

            .crest-badge {{
                position: absolute;
                top: 16px;
                right: 16px;
                width: 75px;
                height: 75px;
                z-index: 2;
                filter: drop-shadow(0 4px 8px rgba(0,0,0,0.5));
            }}

            .photo-panel::before {{
                content: '';
                position: absolute;
                top: 0; left: 0; right: 0; bottom: 0;
                background: radial-gradient(circle at center, {club_color} 0%, transparent 70%);
                opacity: 0.2;
                z-index: 0;
            }}

            .footer {{
                position: absolute;
                bottom: 0;
                left: 0;
                width: 100%;
                height: 65px;
                background: #141821;
                display: flex;
                align-items: center;
                justify-content: space-between;
                padding: 0 60px;
                box-sizing: border-box;
                font-size: 24px;
                font-weight: 700;
                color: #bec8dc;
                border-top: 4px solid {club_color};
            }}
        </style>
    </head>
    <body>
        <div class="accent-slash"></div>
        <div class="accent-slash"></div>

        <div class="container">
            <div class="left-column">
                <div class="wordmark">
                    {logo_html}FPL<span>VORTEX</span>
                </div>
                <div><div class="status-badge">{status}</div></div>
                <div class="player-name">{player_name}</div>

                <div class="details-grid">
                    <div class="detail-label from">FROM</div>
                    <div class="detail-value">{from_html}</div>
                    <div class="detail-label to">TO</div>
                    <div class="detail-value">{to_html}</div>
                    <div class="detail-label fee">FEE</div>
                    <div class="detail-value" style="color:#54e07c;">{fee_value}</div>
                </div>
            </div>

            <div class="right-column">
                <div class="photo-panel">
                    {crest_img_html}
                    {photo_img_html}
                </div>
            </div>
        </div>

        <div class="footer">
            <div>Source: {source_text} | @FPLVortex</div>
            <div style="color: #d4af37;">{story.get('event', 'TRANSFER').upper()}</div>
        </div>
        
        <script>
    document.addEventListener("DOMContentLoaded", function() {{
        const nameEl = document.querySelector('.player-name');
        let fontSize = 88;

        // Dynamically shrink the font until it fits inside the 820px bounding box
        while(nameEl.scrollWidth > nameEl.clientWidth && fontSize > 30) {{
            fontSize--;
            nameEl.style.fontSize = fontSize + 'px';
        }}
    }});
</script>
    </body>
    </html>
    """

    try:
        import threading
        error_box = []
        t = threading.Thread(target=_render_html_sync, args=(html_content, filename, error_box))
        t.start()
        t.join()
        if error_box:
            print("  [THREAD TRACEBACK]\n" + error_box[0])
        if not Path(filename).exists() or Path(filename).stat().st_size < 1000:
            raise RuntimeError("Thread completed but image missing")
    except Exception as e:
        import traceback; traceback.print_exc()
        from PIL import Image
        Image.new('RGB', (1380, 776), color=(11, 18, 32)).save(filename)

def create_injury_image(story, sources, filename):
    W, H = 1380, 776
    fpl = fetch_fpl_data()
    player_el = find_player_in_fpl(story.get("player"), fpl)
    player_name = (player_el["web_name"] if player_el else story.get("player")) or "PLAYER"

    img = Image.new("RGB", (W, H), (24, 10, 12))
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rectangle([W // 2, 0, W, H], fill=(120, 18, 22))
  # ─── ASSET FALLBACK CHAIN ───
    import hashlib
    right_center = (W - (W // 4), H // 2)
    pid = player_el.get("code") if player_el else None
    img_pasted = False

    # Tier 1: Try official FPL Player Photo
    if pid:
        pp = Path(f"players/{pid}.png")
        if not pp.exists():
            try:
                _download_asset(f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{pid}.png", pp)
            except Exception:
                pass
        if pp.exists() and pp.stat().st_size >= 500:
            p_img = _safe_open_rgba(pp)
            if p_img is not None:
                p_img = _fit_contain(p_img, 400, 500)
                img.paste(p_img, (right_center[0] - p_img.width // 2, right_center[1] - p_img.height // 2 + 30), p_img)
                img_pasted = True

    # Tier 2: Try Tweet Image (media_url) from the journalist
    if not img_pasted and story.get("media_url"):
        murl = story["media_url"]
        mp = Path("players/tw_" + hashlib.md5(murl.encode()).hexdigest()[:12] + ".png")
        if not mp.exists():
            try:
                _download_asset(murl, mp)
            except Exception:
                pass
        if mp.exists() and mp.stat().st_size >= 500:
            t_img = _safe_open_rgba(mp)
            if t_img is not None:
                t_img = _fit_contain(t_img, 400, 500)
                img.paste(t_img, (right_center[0] - t_img.width // 2, right_center[1] - t_img.height // 2 + 30), t_img)
                img_pasted = True

    # Tier 3: Hard fallback to FPL VORTEX logo
    if not img_pasted:
        logo_path = Path("Logo.png")
        if logo_path.exists():
            l_img = _safe_open_rgba(logo_path)
            if l_img is not None:
                l_img = _fit_contain(l_img, 300, 300)
                img.paste(l_img, (right_center[0] - l_img.width // 2, right_center[1] - l_img.height // 2), l_img)

    # Tier 2: If no player photo, dynamically load the Club Crest instead
    if not img_pasted:
        club_key = story.get("to_key") or story.get("from_key")
        if club_key:
            crest = _load_crest(club_key, box=350)
            if crest is not None:
                img.paste(crest, (right_center[0] - crest.width // 2, right_center[1] - crest.height // 2), crest)
                img_pasted = True

    # Tier 3: Hard fallback to the branded FPL VORTEX logo placeholder
    if not img_pasted:
        logo_path = Path("Logo.png")
        if logo_path.exists():
            l_img = _safe_open_rgba(logo_path)
            if l_img is not None:
                l_img = _fit_contain(l_img, 300, 300)
                img.paste(l_img, (right_center[0] - l_img.width // 2, right_center[1] - l_img.height // 2), l_img)

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
    if story.get("diagnosis"): rows.append(("DIAGNOSIS", story["diagnosis"]))
    stage = story.get("stage", 1)
    avail = {4: "Available / fit again", 3: "Ruled out", 2: "Doubt", 1: "To be assessed"}.get(stage, "To be assessed")
    rows.append(("AVAILABILITY", avail))
    rows.append(("TIMELINE", story.get("expected_return") or "Awaiting update"))
    if story.get("next_match"): rows.append(("NEXT MATCH", story["next_match"]))

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
    bf = get_premium_font(32, "Bold")
    draw.text((60, H - 70), bar, font=bf, fill=(220, 190, 190))
    img.save(filename)

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

# ── QUEUE FILES ──────────────────────────────────────────────────────────
def _slug(item):
    return re.sub(r'[^a-z0-9_]', '', item["key"]) + f"_s{item['stage']}"

def save_draft(item, body, image_path):
    """Save draft with date and club-based subfolder organisation."""
    import shutil

    # ── Determine which folder this story belongs to ──
    to_key = item.get("to_key") or ""
    from_key = item.get("from_key") or ""
    anchor_key = to_key or from_key

    # Event type subfolder
    ev = item.get("event", "transfer")
    if ev in ("loan", "loan_option"):
        event_folder = "Loans"
    elif ev == "injury":
        event_folder = "Injuries"
    elif ev == "manager":
        event_folder = "Managers"
    elif ev in ("renewal", "stay"):
        event_folder = "Contracts"
    else:
        event_folder = "Transfers"

    # Club/league folder
    pl_keys = set(CLUB_ALIASES.values())
    bundesliga_keys = {"Bayern", "Dortmund", "Leipzig", "Leverkusen"}
    laliga_keys = {"Real_Madrid", "Barcelona", "Atletico", "Sevilla",
                   "Villarreal", "Real_Sociedad", "Athletic_Bilbao"}
    seriea_keys = {"Juventus", "Inter", "AC_Milan", "Napoli", "Roma"}

    if anchor_key in pl_keys:
        club_folder = anchor_key.replace("_", " ")
    elif any(k.lower() in anchor_key.lower() for k in bundesliga_keys):
        club_folder = "Bundesliga"
    elif any(k.lower() in anchor_key.lower() for k in laliga_keys):
        club_folder = "LaLiga"
    elif any(k.lower() in anchor_key.lower() for k in seriea_keys):
        club_folder = "SeriaA"
    else:
        club_folder = "Miscellaneous"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    folder = Path(DRAFTS_FOLDER) / today / club_folder / event_folder
    folder.mkdir(parents=True, exist_ok=True)

    base_name = _slug(item)

    # Copy image
    final_image = folder / f"{base_name}.png"
    if Path(image_path).exists():
        shutil.copy2(image_path, final_image)
        print(f"✅ Image saved: {final_image}")
    else:
        print(f"  [WARN] Image missing for {base_name}")

    # Save caption text file
    txt_path = folder / f"{base_name}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(body)
        f.write(f"\n\n---\nSources: {', '.join(item.get('sources', []))}")
        f.write(f"\nGenerated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        f.write(f"\nSlug: {base_name}")

    print(f"✅ DRAFT READY → {folder}/{base_name}.png + {base_name}.txt")
    return str(final_image)

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

    def _img_ok():
        return os.path.exists(image_path) and os.path.getsize(image_path) >= 1000

    if not _img_ok():
        print(f"  [IMG] post-time card missing — regenerating: {item.get('player')!r}")
        try:
            if item.get("event") == "injury":
                create_injury_image(item, item["sources"], image_path)
            else:
                # ARCHITECT FIX: Removed invalid 'await'
                create_transfer_image(item, item["sources"], image_path, collapsed=item.get("collapsed", False))
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
            print(f"  [WARN] twikit KeyError({ke}) after create_tweet — tweet is live; recording as posted to prevent duplicate.")
            posted_live = True
        else:
            raise

    if posted_live:
        record_posted(item, data)
        print(f"  ✅ POSTED [{status_label(item, item.get('mode'))}]: "
              f"{item['player']} — {item['event']} (stage {item['stage']})")
        return True

    return False
  # ── SCRAPER CORE ─────────────────────────────────────────────────────────
MAX_TWEET_AGE_DAYS = 3

def _parse_tweet_date(raw):
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    s = str(raw).strip()
    s_norm = re.sub(r'\b(GMT|UTC)\b', '+0000', s)
    for fmt in ("%a, %d %b %Y %H:%M:%S %z",
                "%a %b %d %H:%M:%S %z %Y",
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S%z"):
        try:
            dt = datetime.strptime(s_norm, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

def tweet_too_old(created_at, max_days=MAX_TWEET_AGE_DAYS):
    dt = _parse_tweet_date(created_at)
    if dt is None:
        return False
    age = datetime.now(timezone.utc) - dt
    return age.total_seconds() > max_days * 86400

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
                link, desc, pubdate = it.find("link"), it.find("description"), it.find("pubDate")
                if link is None: 
                    continue
                
                tid = link.text.strip().split("/")[-1].split("#")[0]
                desc_text = desc.text if desc is not None and desc.text else ""
                text = re.sub(r'\<[^\>]+\>', '', desc_text).strip()
                
                created_at = pubdate.text.strip() if pubdate is not None and pubdate.text else None

                # Image extraction logic
                media_url = None
                img_match = re.search(r'<img[^>]+src="([^">]+)"', desc_text)
                if img_match:
                    media_url = img_match.group(1)
                    if media_url.startswith("/"):
                        media_url = f"{inst}{media_url}"

                if tid and text: 
                    out.append({
                        "id": tid, 
                        "text": text, 
                        "media_url": media_url, 
                        "created_at": created_at
                    })
                    
            if out: 
                return out
                
        except Exception: 
            continue
            
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
            if tid in data["posted_ids"]: continue
            if not any(k in text.lower() for k in FOOTBALL_KW): continue
            
            if tweet_too_old(t.get("created_at")):
                skipped += 1
                print(f"   skip (older_than_{MAX_TWEET_AGE_DAYS}d): {text[:70]!r}")
                continue
                
            seen += 1
            
            if tid in data["extracted"]: 
                story = dict(data["extracted"][tid])
            else:
                story = build_story(text, fpl)
                story["media_url"] = t.get("media_url")
                story["created_at"] = t.get("created_at")
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
        print("[READ-HEALTH] WARNING: more than half of sources failed. If Twikit is enabled, refresh X_AUTH_TOKEN / X_CT0_TOKEN. Also verify NITTER_INSTANCES are reachable.")
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

async def build_draft(item, data, fpl):
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
        if item.get("event") == "injury":
            create_injury_image(item, item["sources"], str(image_path))
        else:
            # ARCHITECT FIX: Removed invalid 'await'
            create_transfer_image(item, item["sources"], str(image_path), collapsed=item.get("collapsed", False))
        
        if not image_path.exists() or image_path.stat().st_size < 1000:
            raise RuntimeError("image missing or empty")
    except Exception as e:
        import traceback, sys
        print(f"  [IMG] generation FAILED ({e}) — draft skipped: {item.get('player')!r}")
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
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

# ================== MANUAL DRAFT MODE ==================
# Auto-posting is completely disabled for safety
BOT_PAUSED = True
ENABLE_AUTOPOST = False
MAX_POSTS_PER_RUN = 0
MAX_POSTS_PER_HOUR = 0

# Draft saving settings
SAVE_DRAFTS_TO_DISK = True
DRAFTS_FOLDER = "fpl_drafts"        # All drafts will be saved here
# ======================================================

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
                      "count": 0, "limit": 24},
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
                await create_transfer_image(story, story["sources"], str(img_path), collapsed=(story.get("collapsed", False)))
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
    print(f"  Classifier         : regex-only (no LLM)")
    print(f"  Cards written to   : {dryrun_dir}/")
    # ... (End of run_dry_run function)
    print("[DRY-RUN] ==========================================")
    if total_img_fail == 0: 
        print("[DRY-RUN] PASS: no blank/broken images.")
    else: 
        print("[DRY-RUN] FAIL: some images did not render — investigate above.")


# 1. Unindent main to the absolute left edge (module level)
async def main(post: bool = True, allow_rumours: bool = False):
    # ================== MANUAL DRAFT MODE ==================
    # Force draft-only mode (no auto posting)
    post = False
    mode_str = "DRAFT-ONLY (Manual Save Mode)"
    print(f"\n[BOT] Run — {datetime.now(timezone.utc).isoformat()} "
          f"(classifier=regex, mode={mode_str})")
    # ======================================================
    
    init_club_data()
    fpl = fetch_fpl_data()
    data = load_data()
    
    # ... rest of your main() logic

    # Build Twikit client only if tokens are present; otherwise rely on Nitter
    read_client = None
    if X_AUTH_TOKEN and X_CT0_TOKEN:
        try:
            read_client = Client("en-US")
            read_client.set_cookies({"auth_token": X_AUTH_TOKEN, "ct0": X_CT0_TOKEN})
        except Exception as e:
            print(f"[READ] could not init twikit read client: {e}")
            read_client = None
    else:
        print("[READ] Twikit disabled — X_AUTH_TOKEN / X_CT0_TOKEN not set; using Nitter only.")

    if not check_daily_limit(data):
        print("[BOT] Daily limit reached — nothing will post today.")

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
        built = await build_draft(item, data, fpl)
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
    # PRIORITY 2 — success cap, not attempt cap. We walk the FULL ranked list
    # and stop only after target successful posts or when candidates run out.
    # A duplicate (post_item -> False) is skipped, not run-ending.
    target = max(0, min(MAX_POSTS_PER_RUN, remaining_today, remaining_hour))
    print(f"[BOT] Up to {target} post(s) this run from {len(postable)} ranked "
          f"candidate(s) (run cap {MAX_POSTS_PER_RUN}, {remaining_today} left "
          f"today, {remaining_hour} left this hour).")

    posted = 0
    for i, item in enumerate(postable):
        if posted >= target:
            break
        if not check_daily_limit(data):
            print("[BOT] Hit daily limit mid-batch — stopping.")
            break

        jitter = random.randint(*POST_JITTER_RANGE_S)
        print(f"  [PACING] waiting {jitter}s before posting (anti-spam jitter)…")
        await asyncio.sleep(jitter)

        try:
            if await post_item(post_client, item, data):
                posted += 1
            else:
                # Blocked as duplicate/invalid — advance to next candidate
                # instead of ending the run at zero.
                print(f"  [SKIP] {item.get('key')} not posted — trying next ranked candidate.")
                continue
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
                        help="Force draft-only mode (no posting). Default is LIVE.")
    parser.add_argument("--allow-rumours", action="store_true",
                        help="Also auto-post RUMOUR-labelled stories (NOT recommended).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Offline test: run fixtures through the full pipeline.")
    parser.add_argument("--fixtures", default="fixtures/tweets.json",
                        help="Path to fixture tweets JSON for --dry-run.")
    parser.add_argument("--runs", type=int, default=2,
                        help="How many passes over fixtures in --dry-run (>=2 proves dedup).")
    args = parser.parse_args()
    if args.dry_run:
        asyncio.run(run_dry_run(fixtures_path=args.fixtures, runs=args.runs))
    else:
        asyncio.run(main(post=not args.draft_only, allow_rumours=args.allow_rumours))
