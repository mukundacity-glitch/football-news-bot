
import os
import re
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

# Connected to Core Engines & Ground Truth Caches
from src.fpl_feed import fetch_fpl_data, find_player_in_fpl, fpl_team_key, is_big_player
from src.renderer import create_transfer_image, create_injury_image, _create_fallback_card
from src.parser import extract_story_fallback, detect_historical, passes_safety_gate, _clean_source_text
from src.constants import (
    CHANNEL_NAME, CHANNEL_HANDLE, POSTED_FILE, PENDING_DIR, POSTED_DIR, DRAFTS_DIR,
    JOURNALISTS, NITTER_INSTANCES, OFFICIAL_ACCOUNTS, OFFICIAL_INJURY_ACCOUNTS,
    ELITE_TRUSTED, TRUSTED_MEDIA, FOOTBALL_KW, STAFF_BLOCK_KW, MANAGER_SURNAMES,
    CLUB_ALIASES, FPL_LOGO_IDS, CLUB_COLORS, CLUB_HASHTAG_MAP
)

# Shared Canvas Namespace Initialization
FONT = ImageFont.load_default()
font = FONT 

# Twikit API Runtime Inline Workaround
try:
    _tx_mod = __import__("twikit.x_client_transaction.transaction", fromlist=["ClientTransaction"])
except Exception as e:
    _tx_mod = None
    print(f"[PATCH] twikit transaction module not found, skipping patch: {e}")

if _tx_mod is not None:
    _tx_mod.ON_DEMAND_FILE_REGEX = re.compile(r',(\d+):["\']ondemand\.s["\']', flags=(re.VERBOSE | re.MULTILINE))
    _tx_mod.ON_DEMAND_HASH_PATTERN = r',{}:"([0-9a-f]+)"'
    _tx_mod.INDICES_REGEX = re.compile(r'(\(\w{1,2}\[(\d{1,2})\],\s*16\))+', flags=(re.VERBOSE | re.MULTILINE))

    async def _patched_get_indices(self, home_page_response, session, headers):
        key_byte_indices = []
        response = self.validate_response(home_page_response) or self.home_page_response
        response_str = str(response)
        on_demand_file = _tx_mod.ON_DEMAND_FILE_REGEX.search(response_str)
        if on_demand_file:
            on_demand_file_index = on_demand_file.group(1)
            hash_regex = re.compile(_tx_mod.ON_DEMAND_HASH_PATTERN.format(on_demand_file_index))
            hash_match = hash_regex.search(response_str)
            if hash_match:
                filename = hash_match.group(1)
                on_demand_file_url = f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{filename}a.js"
                on_demand_file_response = await session.request(method="GET", url=on_demand_file_url, headers=headers)
                key_byte_indices_match = _tx_mod.INDICES_REGEX.finditer(str(on_demand_file_response.text))
                for item in key_byte_indices_match:
                    key_byte_indices.append(item.group(2))
        if not key_byte_indices:
            raise Exception("Couldn't get KEY_BYTE indices")
        key_byte_indices = list(map(int, key_byte_indices))
        return key_byte_indices[0], key_byte_indices[1:]

    _tx_mod.ClientTransaction.get_indices = _patched_get_indices
    print("[PATCH] twikit ClientTransaction.get_indices patched (issue #408 workaround).")

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

# ── CONFIGURATION & BRANDING (Imported from src.constants) ───────────────
from src.constants import (
    CHANNEL_NAME, CHANNEL_HANDLE, POSTED_FILE, PENDING_DIR, POSTED_DIR,
    JOURNALISTS, NITTER_INSTANCES, OFFICIAL_ACCOUNTS, OFFICIAL_INJURY_ACCOUNTS,
    ELITE_TRUSTED, TRUSTED_MEDIA, FOOTBALL_KW, STAFF_BLOCK_KW, MANAGER_SURNAMES,
    CLUB_ALIASES, FPL_LOGO_IDS, CLUB_COLORS, CLUB_HASHTAG_MAP
)

def source_tier(handle: str) -> int:
    h = (handle or "").lower().lstrip("@")
    if h in OFFICIAL_ACCOUNTS: return 1
    if h in ELITE_TRUSTED: return 2
    if h in TRUSTED_MEDIA: return 3
    return 0

# Retained local variables required for cache wiring fallback
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

def is_big_name_player(name: str) -> bool:
    return False

# ── CLUBS_CACHE WIRING ───────────────────────────────────────────────────
CLUB_NAME_SET = set()
CLUB_HASHTAGS = {}
PL_CLUB_NAMES = set()
def resolve_club_key(name: str):
    if not name: return None
    n = name.lower()
    for alias in _SORTED_ALIASES:
        if re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', n):
            return CLUB_ALIASES[alias]
    return None

BIG_NAMES_NON_FPL: set = set()

def is_big_club_name(name: str) -> bool:
    return False

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



from src.renderer import create_transfer_image, create_injury_image, _create_fallback_card

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
                
            safe, why = passes_safety_gate(story, text, fpl, sources=[username], source_tier_func=source_tier)
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
            safe, why = passes_safety_gate(story, text, fpl, sources=[username], source_tier_func=source_tier)
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
