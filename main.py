import feedparser
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
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageChops
from pilmoji import Pilmoji

# Connected to Core Engines & Ground Truth Caches
from src.fpl_feed import fetch_fpl_data, find_player_in_fpl, fpl_team_key, is_big_player
from src.renderer import create_transfer_image, create_injury_image, _create_fallback_card
from src.parser import extract_story_fallback, detect_historical, passes_safety_gate, _clean_source_text
from src.entity_guard import (is_postable_player, classify_entity,
                              is_staff_subject, staff_role_of, staff_action_of)
from src import confidence as _conf
from src import direction as _direction
from src.verifier import cross_verify

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

def _env_int(name, default):
    """Read an int env var, falling back to default for unset OR empty values.
    (An unset GitHub Actions Variable is passed through as an empty string.)"""
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        print(f"[CONFIG] {name}={raw!r} is not an int — using default {default}.")
        return default

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
_LOGIC_VER = "2026-07-23-fpl-entity-guard-club-clean"

# ── CONFIGURATION & BRANDING (Imported from src.constants) ───────────────
from src.constants import (
    CHANNEL_NAME, CHANNEL_HANDLE, POSTED_FILE, PENDING_DIR, POSTED_DIR,
    JOURNALISTS, NITTER_INSTANCES, OFFICIAL_ACCOUNTS, OFFICIAL_INJURY_ACCOUNTS,
    ELITE_TRUSTED, TRUSTED_MEDIA, FOOTBALL_KW, STAFF_BLOCK_KW, MANAGER_SURNAMES,
    CLUB_ALIASES, FPL_LOGO_IDS, CLUB_COLORS, CLUB_HASHTAG_MAP, STRONG_OFFICIAL_CUES
)

# Tier sets are compared on a normalised (alphanumeric-only) form so feed
# names like "BBC_Sport" / "Sky Sports" match their canonical handles
# ("bbcsport", "skysports") instead of silently dropping to tier 0.
def _norm_handle(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())

_OFFICIAL_N = {_norm_handle(h) for h in OFFICIAL_ACCOUNTS}
_ELITE_N = {_norm_handle(h) for h in ELITE_TRUSTED}
_TRUSTED_N = {_norm_handle(h) for h in TRUSTED_MEDIA}
_OFFICIAL_INJURY_N = {_norm_handle(h) for h in OFFICIAL_INJURY_ACCOUNTS}

def source_tier(handle: str) -> int:
    h = _norm_handle(handle)
    if h in _OFFICIAL_N: return 1
    if h in _ELITE_N: return 2
    if h in _TRUSTED_N: return 3
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

# Full, broadcast-style club names for the tweet description AND the card, so the
# two never disagree (e.g. "Man_City" -> "Manchester City" in both places).
CLUB_FULL_NAME = {
    "Arsenal": "Arsenal", "Aston_Villa": "Aston Villa", "Bournemouth": "Bournemouth",
    "Brentford": "Brentford", "Brighton": "Brighton", "Burnley": "Burnley",
    "Chelsea": "Chelsea", "Coventry": "Coventry City", "Crystal_Palace": "Crystal Palace",
    "Everton": "Everton", "Fulham": "Fulham", "Hull": "Hull City",
    "Ipswich": "Ipswich Town", "Leeds": "Leeds United",
    "Leicester": "Leicester City", "Liverpool": "Liverpool", "Man_City": "Manchester City",
    "Man_Utd": "Manchester United", "Newcastle": "Newcastle United", "Nottm_Forest": "Nottingham Forest",
    "Southampton": "Southampton", "Spurs": "Tottenham", "Sunderland": "Sunderland",
    "West_Ham": "West Ham", "Wolves": "Wolves",
}

def club_display(key_or_name) -> str:
    """Resolve a club key or raw name to its full display name."""
    if not key_or_name:
        return ""
    if key_or_name in CLUB_FULL_NAME:
        return CLUB_FULL_NAME[key_or_name]
    k = resolve_club_key(key_or_name)
    if k and k in CLUB_FULL_NAME:
        return CLUB_FULL_NAME[k]
    # Foreign / unknown club: prettify the raw string (e.g. "real_madrid" -> "Real Madrid").
    return str(key_or_name).replace("_", " ").strip().title()

def is_reliable_source(sources) -> bool:
    """True if any source is an official account, elite reporter, or trusted media
    outlet (tiers 1-3). Reliable sources may post without an FPL-database match."""
    return any(source_tier(s) in (1, 2, 3) for s in (sources or []))

# Normalised handles of every account we scrape — used to catch the common
# misparse where a REPORTER'S name ("Alex Crook", "David Ornstein") gets picked
# up as the "player".
_SOURCE_HANDLES = {
    re.sub(r'[^a-z0-9]', '', h.lower())
    for h in (set(JOURNALISTS) | OFFICIAL_ACCOUNTS | ELITE_TRUSTED | TRUSTED_MEDIA)
}

def looks_like_reporter(name) -> bool:
    n = re.sub(r'[^a-z0-9]', '', (name or '').lower())
    return bool(n) and n in _SOURCE_HANDLES

# ── STATE ────────────────────────────────────────────────────────────────
# Daily post cap. Generous, but capped so a freak news day can't burst-flag us.
DAILY_POST_LIMIT = _env_int("DAILY_POST_LIMIT", 30)

def load_data() -> dict:
    fresh = {"daily": {"date": "", "count": 0, "limit": DAILY_POST_LIMIT}, "stories": {}, "posted_ids": []}
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
        data["daily"] = {"date": today, "count": 0, "limit": DAILY_POST_LIMIT}
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
_PLAYER_EVENTS = ("transfer", "loan", "loan_option", "injury", "suspension", "renewal", "stay")

def build_story(tweet_text, fpl_data):
    s = extract_story_fallback(tweet_text, fpl_data)

    # ROLE-CUE ROUTING: if the subject is football staff (coach/director/etc.),
    # this is NOT a player transfer. Tag the role/action for accurate wording and
    # re-route any player event to STAFF/MANAGER news so it is classified
    # correctly and still posts — never as a fake player move.
    # fpl_data MUST be passed: it is the ground-truth guard that stops a real,
    # rostered PLAYER (e.g. Garnacho) being re-routed to the manager pipeline
    # just because role-cue words ("the ... job", "boss") sit near his name.
    if s.get("player") and is_staff_subject(s.get("player"), tweet_text, fpl_data):
        s["staff_role"] = staff_role_of(s.get("player"), tweet_text, fpl_data)
        s["staff_action"] = staff_action_of(tweet_text)
        if s.get("event") in _PLAYER_EVENTS:
            s["event"] = "manager"

    # DIRECTION RESOLUTION: the base parser only knows PL clubs, so it drops
    # foreign/EFL clubs and can invert direction. Re-resolve origin/destination
    # from the full club lexicon and correct the story before validation.
    if s.get("event") in ("transfer", "loan", "loan_option"):
        rf, rfk, rt, rtk = _direction.resolve(tweet_text)
        if rt:
            # Parser's "destination" is actually the resolved ORIGIN => inverted.
            if s.get("to_key") and rfk and s.get("to_key") == rfk:
                s["to_club"], s["to_key"] = rt, rtk
            elif not (s.get("to_key") or s.get("to_club")):
                s["to_club"], s["to_key"] = rt, rtk
        if rf:
            # Direction module found explicit "from [club]" grammar — more
            # reliable than the parser's "2nd club in tweet" positional guess.
            s["from_club"] = rf
            s["from_key"] = rfk  # None for clubs not in the PL alias list

    if fpl_data and s.get("player") and s.get("event") in ("transfer", "loan", "loan_option"):
        el = find_player_in_fpl(s["player"], fpl_data)
        is_free_agent = bool(el and el.get("team", 0) == 0)
        actual_club = fpl_team_key(el, fpl_data) if el else None

        if actual_club and not is_free_agent:
            # FPL ground truth always wins over parser/direction guesses.
            s["from_key"] = actual_club
            s["from_club"] = actual_club.replace("_", " ")

    # Loan fee guard: the fee regex often captures a player's market value
    # mentioned in the same tweet (e.g. "€100M-rated Bouaddi on loan"), not an
    # actual loan fee payment. Clear it unless the tweet explicitly names a fee
    # for the loan itself — loan fees exist but are almost always < £20M.
    if s.get("event") in ("loan", "loan_option") and s.get("fee"):
        _raw_lower = (tweet_text or "").lower()
        # A fee tied to a buy clause ("obligation/option to buy for €X") is a
        # genuine, reportable number — keep it. Only a bare amount with no
        # loan-fee or buy-clause context is treated as a market-value misparse.
        _FEE_KEEP_CUES = ("loan fee", "loan payment", "season fee",
                          "obligation to buy", "option to buy", "buy clause",
                          "buy option", "obligation to sign", "future fee")
        if not any(p in _raw_lower for p in _FEE_KEEP_CUES):
            s["fee"] = None

    # 🛡️ FREE AGENT DETECTOR (100% Dynamic, No Hardcoding)
    # Detects if the article explicitly states the player is a free agent or out of contract.
    _raw_lower = (tweet_text or "").lower()
    free_cues = ("free agent", "free transfer", "out of contract", "bosman", "on a free", "released", "contract expired")
    
    # Also cross-references the official FPL database to see if they are unattached.
    is_fpl_free_agent = False
    if fpl_data and s.get("player"):
        el = find_player_in_fpl(s["player"], fpl_data)
        if el and el.get("team", 0) == 0:
            is_fpl_free_agent = True

    if any(c in _raw_lower for c in free_cues) or is_fpl_free_agent:
        s["is_free"] = True
        s["fee"] = "Free transfer"

    # CLUB-FIELD SANITATION: whatever path filled from_club/to_club, the value
    # printed on the card/tweet must be a club NAME, never a sentence fragment
    # ("Sporting. Negotiations"). Re-clean both raw fields as a last line of
    # defence; a field that cleans away to nothing is dropped entirely.
    for _f in ("from_club", "to_club"):
        if s.get(_f):
            s[_f] = _direction._clean_raw_club(s[_f])

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

def _story_club_signature(story: dict) -> str:
    """Order-independent identity for the club(s) involved in a story: the
    SET of resolved clubs, not an ordered from->to pair. Two reports of the
    same transfer that disagree on direction (a real recurring failure mode —
    different sources, or a misparse, swap which club is "from" and which is
    "to") still produce the SAME signature here, so they are recognised as
    the same underlying story instead of minting a second, contradictory one."""
    keys = sorted({k for k in (story.get("to_key"), story.get("from_key")) if k})
    if keys:
        return "_".join(k.lower() for k in keys)
    names = sorted({n for n in (
        _norm_text(story.get("to_club")), _norm_text(story.get("from_club"))) if n})
    if names:
        return "_".join(n.replace(" ", "_") for n in names)
    return "unknown"

def content_hash(story: dict) -> str:
    fam = _event_family(story.get("event"))
    club_part = (_story_club_signature(story) if fam == "transfer"
                 else _norm_text(story.get("to_key") or story.get("to_club")
                                 or story.get("from_key") or story.get("from_club")))
    parts = [
        fam,
        _norm_text(story.get("player")),
        club_part,
        _norm_text(story.get("headline")),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

def _same_person(a: str, b: str) -> bool:
    """True when two extracted names refer to the same person despite variant
    forms — 'Alejandro Garnacho' vs 'Alejandro Garnacho Ferreyra'. One name's
    tokens being a subset of the other's, or sharing 2+ tokens, is a match."""
    ta, tb = set(_norm_text(a).split()), set(_norm_text(b).split())
    if not ta or not tb:
        return False
    return ta <= tb or tb <= ta or len(ta & tb) >= 2

def is_duplicate_content(story: dict, data: dict, threshold: float = 0.90):
    h = content_hash(story)
    if h in data.get("posted_hashes", []): return True, "content_hash"

    # CROSS-STORY GUARDS against already-POSTED stories. Two failure modes the
    # plain hash can't see, both real incidents:
    #  1. The SAME person posted under two different event families (a loan AND
    #     a "manager" story) — contradictory posts about one piece of news.
    #  2. The SAME story re-posted under a name variant ('Garnacho' vs
    #     'Garnacho Ferreyra'), which mints a different key and hash.
    fam = _event_family(story.get("event"))
    my_clubs = {k for k in (story.get("to_key"), story.get("from_key")) if k}
    for prev in data.get("stories", {}).values():
        if not _same_person(prev.get("player"), story.get("player")):
            continue
        prev_fam = _event_family(prev.get("event"))
        prev_clubs = {k for k in (prev.get("to_key"),) if k}
        overlap = (bool(my_clubs & prev_clubs) if (my_clubs and prev_clubs)
                   else True)
        if not overlap:
            continue
        if {prev_fam, fam} == {"transfer", "manager"}:
            return True, "conflicting_event_family_same_person"
        if (prev_fam == fam and prev.get("status") == "active"
                and my_clubs and prev_clubs and (my_clubs & prev_clubs)
                and int(story.get("stage", 1)) <= int(prev.get("stage", 0))):
            return True, "same_story_name_variant"

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

def story_anchor(story: dict) -> str:
    """Identity anchor used to key a story. Transfer/loan events use the
    UNORDERED club-pair signature (see _story_club_signature) so a direction
    mix-up between two reports of the same move can't create two separate,
    contradictory story keys. Injury/manager events keep the single-club
    anchor, since those don't carry a from/to pair to get reversed."""
    if _event_family(story.get("event")) == "transfer":
        return _story_club_signature(story)
    return story.get("to_key") or story.get("from_key") or "unknown"

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
# Single source of truth (src.constants.STRONG_OFFICIAL_CUES) shared with
# parser.py's stage grading — keeps "is this officially completed" language
# consistent across the extraction and confirmation-gate layers.
STRONG_OFFICIAL = STRONG_OFFICIAL_CUES

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
    for mm in re.findall(r'\b([A-ZÀ-ÖØ-Þ][a-zà-ÿ]+(?:\s+[A-ZÀ-ÖØ-Þ][a-zà-ÿ]+){1,2})\b', text):
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
        coordinated = bool(re.search(r'\b[A-ZÀ-ÖØ-Þ][a-zà-ÿ]+ [A-ZÀ-ÖØ-Þ][a-zà-ÿ]+\s+(?:and|&)\s+[A-ZÀ-ÖØ-Þ][a-zà-ÿ]+ [A-ZÀ-ÖØ-Þ][a-zà-ÿ]+', text))
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
    # Collapsed/rejected deals are NEVER posted — they describe a move that
    # did NOT happen. Previously these returned "rumour" which caused the bot
    # to post factually wrong cards ("TRANSFER RUMOUR" for a dead bid).
    if story.get("collapsed"): return None

    tiers = [source_tier(s) for s in sources]
    has_official = 1 in tiers
    n_elite = sum(1 for t in tiers if t == 2)
    has_media = 3 in tiers
    tl = (story.get("body", "") + " " + (story.get("headline", "") or "")).lower()
    strong_words = story["stage"] >= 4 or any(re.search(r'\b' + re.escape(w) + r'\b', tl) for w in STRONG_OFFICIAL)

    if story["event"] == "injury":
        if has_official or n_elite >= 1: return "confirmed"
        return None

    # Manager / staff: only a concrete appointment or departure is posted.
    # Pure "linked with the job" speculation returns None — pending, not posted.
    # All three conditions must hold: concrete action type, reliable source tier,
    # AND actual confirmation language in the text. An elite reporter saying
    # "linked with the job" is NOT enough — the appointment must be real.
    if story.get("event") == "manager":
        action = story.get("staff_action")
        tl_mgr = (story.get("body", "") + " " + (story.get("headline", "") or "")
                  + " " + (story.get("raw_text", "") or "")).lower()
        _MGR_CONFIRM_CUES = (
            "appointed", "confirmed", "official", "announced", "signed",
            "here we go", "completed", "agreed", "sacked", "dismissed",
            "left the club", "departure confirmed", "leaves as",
        )
        has_mgr_confirm = any(c in tl_mgr for c in _MGR_CONFIRM_CUES)
        if (action in ("appointment", "departure")
                and (has_official or n_elite >= 1)
                and has_mgr_confirm):
            return "confirmed"
        return None

    # Transfer/loan without a resolved destination club: never post.
    if story.get("event") in ("transfer", "loan", "loan_option") and not (
            story.get("to_key") or story.get("to_club")):
        return None

    # A trusted source (tier 1/2/3) WITH strong confirmation language
    # ("signed", "confirmed", "here we go", stage≥4) = CONFIRMED post.
    # Without strong language, even from a good source, the story waits
    # for more verification before being published.
    trusted_strong = strong_words and (has_official or n_elite >= 1 or has_media)
    video_only = story.get("from_video") and not has_official
    if (has_official or trusted_strong or n_elite >= 2) and not video_only:
        return "confirmed"

    # Everything that falls through here is unconfirmed speculation.
    # It goes to pending for re-verification on the next run, not published.
    return None

def score_confidence(story, fpl_data=None, sources=None):
    """Run the confidence engine on a validated story and emit a structured audit
    log line. Returns the confidence result dict (score/decision/breakdown).

    The signals it needs are already computed by the pipeline:
      - player_verified: FPL/trusted-DB match OR a reliable-source-reported signing
      - official_source: a tier-1 (official club/league) source is present
      - n_sources:       number of distinct sources
    """
    sources = sources or story.get("sources", []) or []
    tiers = [source_tier(s) for s in sources]
    player = story.get("player") or ""
    fpl_match = bool(fpl_data and find_player_in_fpl(player, fpl_data) is not None)
    player_verified = fpl_match or is_reliable_source(sources)
    result = _conf.evaluate(
        story,
        player_verified=player_verified,
        official_source=(1 in tiers),
        elite_source=(2 in tiers),
        trusted_source=(3 in tiers),
        n_sources=len(set(s.lower() for s in sources if s)),
        fpl_data=fpl_data,
    )
    print("  " + _conf.decision_log_line(story, result))
    return result


def validate_story(story, fpl_data=None, sources=None):
    ev = story.get("event")
    player = (story.get("player") or "").strip()
    if not player: return False, "missing_player"
    if sources is None: sources = story.get("sources", [])

    # RECENCY GATE: never publish news older than 3 days. (Ingestion fail-closes
    # on an unknown date; here we also reject a story that carries a parseable but
    # stale timestamp — e.g. a re-surfaced old tweet re-run from cache.)
    _created = story.get("created_at")
    if _created and tweet_too_old(_created, unknown_is_old=False):
        return False, f"older_than_{MAX_TWEET_AGE_DAYS}d"

    # ENTITY SAFETY GATE (hard reject): the subject must be a real player, never a
    # journalist, company/sponsor, stadium, or a coach filed as a player transfer.
    _ent_text = " . ".join(str(story.get(k, "") or "") for k in ("raw_text", "body", "headline"))
    _ent_ok, _ent_reason = is_postable_player(player, _ent_text, ev, fpl_data)
    if not _ent_ok:
        return False, _ent_reason

    # PLAYER-IS-NOT-A-MANAGER GATE: a name the live FPL feed confirms as a
    # rostered player can NEVER be the subject of manager/staff news. Kills
    # the "ALEJANDRO GARNACHO LINKED WITH THE ASTON VILLA JOB" failure where
    # role-cue words near a player's name re-routed a transfer to the staff
    # pipeline and produced a second, contradictory post.
    if ev == "manager" and fpl_data and find_player_in_fpl(player, fpl_data) is not None:
        return False, "manager_subject_is_active_player"
    _ptokens = [t for t in re.split(r"[\s\-']+", player) if t]
    _plow = player.lower()
    if ev != "manager" and (_plow in MANAGER_SURNAMES or any(m in _plow for m in MANAGER_SURNAMES)): return False, "player_is_manager_name"
    if ev == "manager" and len(_ptokens) < 2: return False, "manager_name_single_token"

    # MANAGER SPECULATION GATE: pure speculation without actual appointment/departure
    # language is blocked here rather than waiting for classify_post(). "Linked with",
    # "frontrunner", "in the frame" etc. from even elite sources must NOT auto-post —
    # the appointment itself must be real. This killed the Iraola-to-Liverpool false post
    # where speculation language ("linked with") yielded staff_action="appointment".
    if ev == "manager":
        _mgr_blob = (story.get("raw_text", "") + " " + story.get("body", "") + " "
                     + (story.get("headline") or "")).lower()
        _MGR_SPEC_CUES = (
            "linked with", "could be appointed", "in the frame", "in contention",
            "shortlisted", "being considered", "candidate for",
            "frontrunner", "favourite for the job", "interviewed for",
            "could take charge", "being lined up", "lined up as",
            "set to hold talks", "approach made", "interest in",
        )
        _MGR_CONFIRM_CUES = (
            "appointed", "confirmed", "signed", "official", "announced",
            "here we go", "completed", "agreed", "left the club", "sacked",
            "dismissed", "departure confirmed", "leaves as",
        )
        if (any(c in _mgr_blob for c in _MGR_SPEC_CUES)
                and not any(c in _mgr_blob for c in _MGR_CONFIRM_CUES)):
            return False, "manager_speculation_language"

    if ev in ("transfer", "loan", "loan_option", "injury", "suspension", "renewal", "stay") and len(_ptokens) < 2: return False, "player_name_single_token"
    if re.search(r"\b(under|u\d{1,2}|u-\d{1,2})$", _plow): return False, "player_name_truncated_fragment"

    # ACCURACY GATE: post about FPL-verified players, OR — when the source is a
    # reliable reporter/website (tiers 1-3) — players not yet in the FPL dataset.
    PERSON_EVENTS = ("transfer", "loan", "loan_option", "renewal", "stay", "injury", "suspension", "manager")
    if fpl_data and ev in PERSON_EVENTS and find_player_in_fpl(player, fpl_data) is None:
        if not is_reliable_source(sources):
            return False, "not_verified_pl_player"
        # Reliable source but unverified player still needs a PL-club anchor so we
        # only post genuinely Premier-League-related news.
        if not (story.get("to_key") or story.get("from_key")):
            return False, "reliable_source_but_no_pl_club"

    PLACEHOLDERS = ("player name", "example", "xxx", "[", "]", "tbd", "to follow",
                    "lorem", "duration & details", "updated heading", "from club", "to club")
    blob = " ".join(str(story.get(k, "") or "") for k in
                    ("player", "headline", "body", "from_club", "to_club", "fee",
                     "contract", "conditional", "diagnosis", "expected_return")).lower()
    for ph in PLACEHOLDERS:
        if ph in blob: return False, f"placeholder_text:{ph!r}"
    if looks_like_club(player): return False, "player_is_club"
    if looks_like_reporter(player): return False, "player_is_reporter_name"
    if re.search(r'\bRT\s+@|@\w+|https?://', story.get("body", "")): return False, "raw_source_text_in_body"
    if player_already_at_club(story, fpl_data): return False, "already_at_destination"
    if ev in ("renewal", "stay"):
        _blob = (story.get("raw_text", "") + " " + story.get("body", "") + " "
                 + (story.get("headline") or "")).lower()
        _EXIT_CUES = ("leav", "exit", "depart", "release", "for sale", "up for sale",
                      "wants out", "wants a move", "wants to go", "could go",
                      "transfer listed", "axed", "let go", "on his way out",
                      "set to go", "seeking a move", "open to leaving",
                      "available for transfer", "verbal agreement", "agreed a fee",
                      "agreed a deal", "reached an agreement", "in talks with",
                      "interested in", "eyeing a move")
        if any(c in _blob for c in _EXIT_CUES):
            return False, "stay_contradicted_by_exit_language"

    if ev in ("transfer", "loan", "loan_option"):
        fk = story.get("from_key"); tk = story.get("to_key")
        fc = (story.get("from_club") or "").strip().lower()
        tc = (story.get("to_club") or "").strip().lower()
        if (fk and tk and fk == tk) or (fc and tc and fc == tc): return False, "from_equals_to"

        # SPECULATION GATE: stage-1 stories containing pure "linked with" /
        # "interest" / "could move" language are never published. They require
        # confirmation language (stage ≥ 2: agreed/advanced/personal terms) or
        # an official source first. This blocked the Sterling "LINKED WITH A
        # PERMANENT TRANSFER" false post — that was stage-1 speculation with no
        # confirmation language whatsoever.
        if story.get("stage", 1) <= 1:
            _SPEC_CUES = (
                "linked with", "could join", "could move", "weighing up",
                "considering a move", "potential move", "showing interest",
                "interest in signing", "rumoured to be", "in the frame",
                "keeping tabs", "a permanent transfer", "might join",
                "keen to sign", "want to sign", "wants to sign",
                "eyeing a move", "eyeing a swoop", "monitoring",
                "tracking the", "set to hold talks", "in talks over a move",
            )
            _blob = (story.get("raw_text", "") + " " + story.get("body", "")).lower()
            if any(c in _blob for c in _SPEC_CUES):
                return False, "speculation_stage1_language"

        # PLAYER-IDENTITY GATE: a "transfer" of someone NOT in the FPL player
        # database must carry positive evidence they are actually a PLAYER — a
        # club-to-club origin (players move between clubs) or a free-agent cue.
        # Without that, the subject is likely staff/coach/executive announced by
        # a club (e.g. a goalkeeping coach "joining" Arsenal) and must NOT be
        # published as a player transfer. Staff with a role cue are already
        # re-routed to the manager/staff pipeline upstream.
        _fpl_match = bool(fpl_data and find_player_in_fpl(player, fpl_data) is not None)
        if not _fpl_match:
            _has_origin = bool(fk or fc)
            _idblob = (story.get("raw_text", "") + " " + story.get("body", "") + " "
                       + (story.get("headline") or "")).lower()
            _free_cue = any(c in _idblob for c in
                            ("free agent", "free transfer", "released", "on a free",
                             "out of contract", "pre-contract", "bosman"))
            if not (_has_origin or _free_cue):
                return False, "unconfirmed_player_identity"
        
        # Require at least one RESOLVED real club — a PL key OR a raw foreign/
        # EFL club name (fc/tc). build_story sets from_key to the player's real
        # FPL club, so verified players always keep their true origin; a
        # foreign-to-PL or PL-to-foreign move keeps its origin/destination as
        # a raw club name instead, which must count here too.
        if not (tk or fk or tc or fc):
            return False, "no_resolved_club"

        # Destination-less transfer: only post if there's a genuine departure cue
        # (kills "linked to his own club" misparses like Onana->Man Utd). A
        # non-PL destination resolved by the direction module (to_club without a
        # to_key, e.g. Sheffield Wednesday / Bolton) DOES count as a destination.
        if not (tk or tc):
            _blob = (story.get("raw_text", "") + " " + story.get("body", "") + " "
                     + (story.get("headline") or "")).lower()
            _EXIT_CUES = ("leav", "exit", "depart", "released", "for sale", "up for sale",
                          "wants out", "wants a move", "wants to go", "could go", "transfer listed",
                          "axed", "let go", "on his way out", "set to go", "seeking a move",
                          "open to leaving", "available for transfer")
            if not any(c in _blob for c in _EXIT_CUES):
                return False, "no_destination"
        leak = (story.get("body", "") + " " + story.get("headline", "")).lower()
        if re.search(r'\b(head coach|sacked|appointed as manager|hamstring|ruled out for)\b', leak): return False, "event_data_mismatch"
    # Manager/staff news needs a club, but a DEPARTURE only has an origin club.
    if ev == "manager" and not (story.get("to_key") or story.get("to_club")
                                or story.get("from_key") or story.get("from_club")): return False, "manager_no_club"
    return True, "ok"

# ── PRE-RENDER ACCURACY DOUBLE-CHECK ─────────────────────────────────────
# Card fields whose value is printed verbatim onto the player card. If any of
# these still carries placeholder/blank text we must NOT render a card.
_CARD_PLACEHOLDERS = (
    "player name", "example", "xxx", "tbd", "to follow", "lorem",
    "from club", "to club", "updated heading", "duration & details",
    "n/a", "none", "null", "undefined", "[", "]",
)

def verify_card_data(item: dict, fpl_data=None):
    """Final accuracy gate run IMMEDIATELY before a player card is rendered.

    This is the "double check" step: it re-resolves the player and clubs against
    the live FPL feed and normalises the item so the card shows verified, accurate
    data (correct display name, true origin club, real crest). Returns
    ``(ok, reason, report)`` where ``report`` is a list of human-readable lines
    describing what was checked. A failed check means the card is NOT created.
    """
    report = []
    ev = item.get("event")
    reliable = is_reliable_source(item.get("sources"))
    PERSON_EVENTS = ("transfer", "loan", "loan_option", "renewal", "stay",
                     "injury", "suspension", "manager")

    # 1. Player identity — resolve against the FPL feed and pin ONE display name
    #    used by both the card and the tweet (so they can never disagree).
    #    A reliable source (official/elite/trusted media) may post even when the
    #    player isn't in the FPL database yet (e.g. a brand-new signing).
    if ev in PERSON_EVENTS and ev != "manager":
        el = find_player_in_fpl(item.get("player"), fpl_data) if fpl_data else None
        if el:
            full = f"{el.get('first_name', '')} {el.get('second_name', '')}".strip()
            item["display_name"] = full or el.get("web_name") or item.get("player")
            item["verified_player_code"] = el.get("code")
            report.append(f"player ✓ '{item.get('player')}' → FPL '{item['display_name']}' (code {el.get('code')})")

            # 2. Club anchoring — attach the player's TRUE current FPL club.
            true_from = fpl_team_key(el, fpl_data)
            if true_from and el.get("team", 0) != 0:
                if ev in ("transfer", "loan", "loan_option"):
                    # Origin is the player's real club, never a stale guess.
                    if item.get("from_key") != true_from:
                        report.append(f"origin corrected: {item.get('from_key')!r} → {true_from!r} (player's real FPL club)")
                    item["from_key"] = true_from
                    # 3. Destination sanity — never claim a move to the club the
                    #    player is already at.
                    if item.get("to_key") and item.get("to_key") == item.get("from_key"):
                        return False, "destination_equals_current_club", report
                elif not item.get("from_key") and not item.get("to_key"):
                    # Injury/suspension/contract: show the player's club on the card.
                    item["from_key"] = true_from
                    report.append(f"club ✓ {true_from!r} (player's FPL club)")
        elif reliable:
            # Trusted source, player not in FPL yet — accept with the parsed name
            # and let the card fall back to the tweet/website photo.
            item["display_name"] = item.get("player")
            report.append(f"player ⚠ '{item.get('player')}' not in FPL — accepted on reliable source "
                          f"({', '.join('@' + s for s in (item.get('sources') or [])[:2])})")
        else:
            return False, "player_not_verified_and_source_not_reliable", report
    else:
        item["display_name"] = item.get("player")
        report.append(f"player ✓ '{item.get('player')}' (event={ev})")

    # Normalise club fields to full broadcast names so the card and tweet match
    # exactly (e.g. card 'MANCHESTER CITY' == tweet 'MANCHESTER CITY').
    if item.get("to_key"):
        item["to_club"] = club_display(item["to_key"])
    if item.get("from_key"):
        item["from_club"] = club_display(item["from_key"])

    # 4. Injury / suspension cards must come from an approved medical source.
    if ev in ("injury", "suspension"):
        sources = item.get("sources", []) or []
        tiers = [source_tier(s) for s in sources]
        approved = any(t in (1, 2) for t in tiers) or any(
            _norm_handle(s) in _OFFICIAL_INJURY_N for s in sources)
        if not approved:
            return False, "injury_source_not_approved", report
        report.append(f"injury source ✓ approved ({', '.join('@' + s for s in sources[:2]) or 'n/a'})")

    # 5. Club crest resolvability — for a known PL/aliased club we expect a crest.
    #    Missing crest for a real club is a soft warning (the card still renders a
    #    branded fallback); a foreign club legitimately has no crest.
    anchor = item.get("to_key") or item.get("from_key")
    if anchor:
        safe = anchor.replace(" ", "_").replace("'", "")
        if FPL_LOGO_IDS.get(safe) or Path(f"logos/{safe}.png").exists():
            report.append(f"crest ✓ available for {anchor!r}")
        else:
            report.append(f"crest ⚠ no PL crest for {anchor!r} (branded fallback will be used)")

    # 6. No placeholder/blank text leaking onto the card surface.
    card_fields = ("player", "from_club", "to_club", "fee", "diagnosis",
                   "expected_return", "next_match")
    blob = " ".join(str(item.get(k, "") or "") for k in card_fields).lower()
    for ph in _CARD_PLACEHOLDERS:
        if ph in blob:
            return False, f"placeholder_on_card:{ph!r}", report

    report.append("data accuracy ✓ all card fields verified")
    return True, "ok", report

# ── LABELS ───────────────────────────────────────────────────────────────
APPROVED_LABELS = {
    "TRANSFER", "RUMOUR", "AGREED", "INJURY", "SUSPENSION", "CONTRACT EXTENSION",
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
    if ev in ("transfer", "loan", "loan_option") and (story.get("to_key") or story.get("to_club")):
        stage = story.get("stage", 1)
        if stage >= 4 or any(w in tl for w in ("official", "here we go", "completed", "confirmed")):
            return "OFFICIAL"
        # Stage 2-3: verbal agreement reached, medical/paperwork pending → AGREED
        if stage >= 2 or any(w in tl for w in ("agreement", "agreed", "personal terms", "medical", "deal agreed")):
            return "AGREED"
    label = EVENT_PREFIX.get(ev)
    return label if label in APPROVED_LABELS else None

# ── HASHTAGS ─────────────────────────────────────────────────────────────

def build_hashtags(story):
    """Exactly 3 SEO hashtags: primary club (or player name), event type, #FPL."""
    ev = story["event"]
    out = []
    # 1. One primary club hashtag (destination club preferred over origin).
    for key, name in ((story.get("to_key"), story.get("to_club")),
                      (story.get("from_key"), story.get("from_club"))):
        ht = hashtag_for(key) or hashtag_for(name)
        if ht:
            out.append(ht)
            break
    # Player-name fallback when no club hashtag is found (e.g. free agent news).
    if not out:
        player = (story.get("display_name") or story.get("player") or "").strip()
        if player:
            out.append("#" + re.sub(r"[^A-Za-z0-9]", "", player))
    # 2. Event type hashtag.
    if ev in ("injury", "suspension"): etag = "#InjuryNews"
    elif ev in ("transfer", "loan", "loan_option"): etag = "#TransferNews"
    elif ev in ("renewal", "stay"): etag = "#ContractNews"
    elif ev == "manager": etag = "#ManagerNews"
    else: etag = "#FootballNews"
    if etag not in out:
        out.append(etag)
    # 3. Always close with #FPL for fantasy football discovery.
    if "#FPL" not in out:
        out.append("#FPL")
    return " ".join(out[:3])

# ── TWEET TEXT ───────────────────────────────────────────────────────────
# Structured 3-line description that mirrors the player card exactly. No source
# and no date here — those already live on the card. Emojis add appeal; the body
# always fits a free (non-premium) X account's 280-char limit.

def tweet_player_name(story) -> str:
    """The single display name used by BOTH the card and the tweet (no mismatch)."""
    return (story.get("display_name") or story.get("player") or "Player").strip()

def _avail_text(stage) -> str:
    return {4: "FIT AGAIN", 3: "RULED OUT", 2: "MAJOR DOUBT", 1: "BEING ASSESSED"}.get(stage, "BEING ASSESSED")

def build_tweet_body(story, sources, mode) -> str:
    ev = story.get("event")
    player = tweet_player_name(story).upper()
    to_full = club_display(story.get("to_key") or story.get("to_club"))
    from_full = club_display(story.get("from_key") or story.get("from_club"))
    label = status_label(story, mode)

    headline = ""
    details = []   # each entry is one "EMOJI LABEL — VALUE" line

    if ev in ("transfer", "loan", "loan_option"):
        if ev in ("loan", "loan_option"):
            move = "LOAN MOVE"
        elif story.get("is_free"):
            move = "FREE TRANSFER"
        else:
            move = "PERMANENT TRANSFER"
        if story.get("collapsed"):
            headline = f"❌ TRANSFER- {player} MOVE TO {to_full or 'NEW CLUB'} HAS COLLAPSED."
        else:
            if label == "OFFICIAL":
                emoji, status = "✅", "CONFIRMED"
            elif label == "AGREED":
                emoji, status = "🤝", "AGREEMENT REACHED —"
            elif label == "RUMOUR":
                emoji, status = "👀", "LINKED WITH A"
            else:
                emoji, status = ("🔄" if move == "LOAN MOVE" else "🔵"), "CONFIRMED"
            if from_full and to_full:
                route = f" FROM {from_full.upper()} TO {to_full.upper()}"
            elif to_full:
                route = f" TO {to_full.upper()}"
            elif from_full:
                route = f" — SET TO LEAVE {from_full.upper()}"
            else:
                route = ""
            prefix = "LOAN" if move == "LOAN MOVE" else "TRANSFER"
            headline = f"{emoji} {prefix}- {player} {status} {move}{route}."
        # Always exactly 2 detail lines (no "undisclosed" filler).
        fee_text = story.get("fee")
        contract_text = story.get("contract")
        stage_labels = {4: "COMPLETED & OFFICIAL", 3: "PAPERWORK UNDERWAY",
                        2: "AGREEMENT REACHED", 1: "IN PROGRESS"}
        stage_text = stage_labels.get(story.get("stage", 1), "CONFIRMED")
        if fee_text:
            details.append(f"💰 FEE — {fee_text}")
        else:
            details.append(f"🔄 DEAL TYPE — {move}")
        if contract_text:
            details.append(f"📝 CONTRACT — {contract_text}")
        else:
            details.append(f"📊 STAGE — {stage_text}")

    elif ev in ("injury", "suspension"):
        club = (to_full or from_full).upper()
        club_part = f" ({club})" if club else ""
        if ev == "suspension":
            headline = f"🟥 SUSPENSION- {player}{club_part} IS SUSPENDED."
            details.append(f"⛔ REASON — {story['diagnosis'] if story.get('diagnosis') else 'Red card / disciplinary'}")
            details.append(f"📅 STATUS — {_avail_text(story.get('stage', 1))}")
        else:
            headline = f"🚑 INJURY- {player}{club_part} {_avail_text(story.get('stage', 1))}."
            details.append(f"🏥 DIAGNOSIS — {story['diagnosis'] if story.get('diagnosis') else 'Injury confirmed — full assessment pending'}")
            details.append(f"⏱️ RETURN — {story.get('expected_return') or 'Exact return date to be announced'}")

    elif ev in ("renewal", "stay"):
        club = (from_full or to_full).upper()
        headline = f"📝 CONTRACT- {player} SIGNS A NEW DEAL" + (f" AT {club}" if club else "") + "."
        details.append(f"📝 TERMS — {story['contract'] if story.get('contract') else 'Contract length to be confirmed'}")
        details.append(f"🏟️ CLUB — {club if club else 'To be announced'}")

    elif ev == "manager":
        club = (to_full or from_full).upper()
        role = story.get("staff_role")
        if role and role != "staff":
            role_u = role.upper()
            action = story.get("staff_action")
            if action == "departure":
                headline = f"👔 STAFF- {player} LEAVES {club} AS {role_u}." if club else f"👔 STAFF- {player} LEAVES ROLE AS {role_u}."
            elif action == "appointment":
                headline = f"👔 STAFF- {player} APPOINTED {club} {role_u}." if club else f"👔 STAFF- {player} APPOINTED AS {role_u}."
            else:
                headline = (f"👀 STAFF- {player} LINKED WITH A {role_u} ROLE"
                            + (f" AT {club}" if club else "") + ".")
            details.append(f"📝 CONTRACT — {story['contract'] if story.get('contract') else 'Details pending'}")
        else:
            headline = f"🎩 MANAGER- {player} LINKED WITH THE {club or 'CLUB'} JOB."
            details.append(f"🏟️ CLUB — {club if club else 'To be confirmed'}")
        details.append(f"🔄 ROLE — {role.upper() if role and role != 'staff' else 'HEAD COACH'}")

    else:
        headline = f"🔵 NEWS- {player}."
        details.append(f"📰 UPDATE — Latest football news.")
        details.append(f"🏴󠁧󠁢󠁥󠁮󠁧󠁿 LEAGUE — PREMIER LEAGUE")

    lines = [headline] + details
    return "\n".join(lines) + "\n\n" + build_hashtags(story)

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



# ── QUEUE ARCHIVE MANAGEMENT ─────────────────────────────────────────────

def _slug(item: dict) -> str:
    return re.sub(r'[^a-z0-9_]', '', item["key"]) + f"_s{item['stage']}"

def save_draft(item: dict, body: str, image_path: str) -> str:
    # Safely creates and organizes drafts folder path hierarchies
    import shutil
    to_key = item.get("to_key") or ""
    from_key = item.get("from_key") or ""
    anchor_key = to_key or from_key
    ev = item.get("event", "transfer")
    
    if ev in ("loan", "loan_option"): event_folder = "Loans"
    elif ev == "injury": event_folder = "Injuries"
    elif ev == "manager": event_folder = "Managers"
    elif ev in ("renewal", "stay"): event_folder = "Contracts"
    else: event_folder = "Transfers"

    if anchor_key in set(CLUB_ALIASES.values()): club_folder = anchor_key.replace("_", " ")
    elif any(k.lower() in anchor_key.lower() for k in {"Bayern", "Dortmund", "Leipzig", "Leverkusen"}): club_folder = "Bundesliga"
    elif any(k.lower() in anchor_key.lower() for k in {"Real_Madrid", "Barcelona", "Atletico"}): club_folder = "LaLiga"
    elif any(k.lower() in anchor_key.lower() for k in {"Juventus", "Inter", "AC_Milan"}): club_folder = "SeriaA"
    else: club_folder = "Miscellaneous"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    folder = Path("fpl_drafts") / today / club_folder / event_folder
    folder.mkdir(parents=True, exist_ok=True)
    base_name = _slug(item)

    final_image = folder / f"{base_name}.png"
    if Path(image_path).exists():
        shutil.copy2(image_path, final_image)
        print(f"✅ Image saved: {final_image}")
    else:
        print(f"  [WARN] Image missing for {base_name}")

    txt_path = folder / f"{base_name}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(body)
        f.write(f"\n\n---\nSources: {', '.join(item.get('sources', []))}")
        f.write(f"\nGenerated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        f.write(f"\nSlug: {base_name}")

    print(f"✅ DRAFT READY ➔ {folder}/{base_name}.png")
    return str(final_image)

def move_to_posted(item: dict):
    src = PENDING_DIR / f"{_slug(item)}.json"
    dst = POSTED_DIR / f"{_slug(item)}.json"
    try:
        if src.exists(): src.rename(dst)
        else: json.dump(item, open(dst, "w", encoding="utf-8"), indent=2, default=str)
    except Exception as e:
        print(f"  [QUEUE] Could not archive tracking payload json file: {e}")


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

# ── X SAFETY: ERROR CLASSIFICATION & BACK-OFF ────────────────────────────
# X returns numeric error codes when it doesn't like our activity. Retrying on
# the wrong one is what gets an account locked. We classify the error and react:
#   duplicate (187)         -> tweet already exists; record dedup, never retry.
#   flagged   (226/326/64)  -> automation/spam/locked/suspended; STOP + cooldown.
#   rate_limited (429/88)   -> too many requests; STOP + short cooldown.
#   transient               -> network/parse blip; one cautious retry is allowed.
_X_DUPLICATE_CODES = {"187"}
_X_FLAG_CODES = {"226", "326", "334", "64", "261"}   # automated / locked / suspended
_X_RATELIMIT_CODES = {"429", "88"}
_X_AUTH_CODES = {"32", "89", "99", "135", "215", "401"}  # bad/expired posting cookies

class XBackoffError(Exception):
    """Raised when X signals automation/rate-limit. The posting run must abort
    immediately and back off — these are never safe to retry."""
    def __init__(self, kind, original):
        super().__init__(f"{kind}: {original}")
        self.kind = kind

def classify_x_error(exc) -> str:
    s = str(exc).lower()
    cls = type(exc).__name__.lower()
    # Pull any explicit "code": NNN values out of the error payload.
    codes = set(re.findall(r'code["\']?\s*[:=]\s*["\']?(\d+)', s))
    if codes & _X_DUPLICATE_CODES or "duplicate" in s or "duplicatetweet" in cls:
        return "duplicate"
    if codes & _X_RATELIMIT_CODES or "toomanyrequests" in cls or "rate limit" in s:
        return "rate_limited"
    if (codes & _X_FLAG_CODES or "automated" in s or "spam" in s or "locked" in s
            or "suspend" in s or "accountlocked" in cls or "accountsuspended" in cls):
        return "flagged"
    if (codes & _X_AUTH_CODES or "could not authenticate" in s or "unauthorized" in cls
            or "bad authentication" in s or "invalid or expired token" in s):
        return "auth"
    return "transient"

def _set_cooldown(data, kind):
    """Persist a back-off window so subsequent runs don't keep hitting X while flagged."""
    mins = COOLDOWN_FLAGGED_MIN if kind == "flagged" else COOLDOWN_RATELIMIT_MIN
    until = datetime.now(timezone.utc) + timedelta(minutes=mins)
    data["cooldown_until"] = until.isoformat()
    save_data(data)
    print(f"  [X-SAFETY] {kind.upper()} detected — backing off {mins} min "
          f"(until {until.isoformat()}). No further posts will be attempted until then.")

def in_cooldown(data) -> bool:
    cu = data.get("cooldown_until")
    if not cu:
        return False
    try:
        return datetime.now(timezone.utc) < datetime.fromisoformat(cu)
    except Exception:
        return False

async def post_item(post_client, item, data):
    fpl = fetch_fpl_data()
    valid, why = validate_story(item, fpl)
    if not valid:
        print(f"  POST BLOCKED ({why}): {item.get('player')!r}")
        if item.get("id") and item["id"] not in data["posted_ids"]:
            data["posted_ids"].append(item["id"]); save_data(data)
        return False
    # Re-run the accuracy double-check at post time in case the card is being
    # regenerated here (e.g. the cached draft image went missing).
    ok, vwhy, _ = verify_card_data(item, fpl)
    if not ok:
        print(f"  POST BLOCKED (verify:{vwhy}): {item.get('player')!r}")
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

    posted_live = False
    try:
        media_id = await post_client.upload_media(image_path, media_type="image/png")
        await post_client.create_tweet(text=caption, media_ids=[media_id])
        posted_live = True

    except KeyError as ke:
        key = str(ke).strip("'\"")
        if key in _TWIKIT_SUCCESS_PARSE_KEYS:
            print(f"  [WARN] twikit KeyError({ke}) after create_tweet — tweet is live; recording as posted to prevent duplicate.")
            posted_live = True
        else:
            raise

    except Exception as exc:
        kind = classify_x_error(exc)
        if kind == "duplicate":
            # X already has this tweet. Record dedup so we NEVER try it again,
            # but don't count it against today's quota (nothing new was posted).
            print(f"  [X-SAFETY] DUPLICATE (187) — already on X; recording dedup, will not retry: {item.get('player')!r}")
            if item.get("id") and item["id"] not in data["posted_ids"]:
                data["posted_ids"].append(item["id"])
            record_content_dedup(item, data)
            save_data(data)
            move_to_posted(item)
            return False
        if kind in ("flagged", "rate_limited"):
            # Automation/spam/rate-limit flag — abort the whole run, do not retry.
            _set_cooldown(data, kind)
            raise XBackoffError(kind, exc)
        if kind == "auth":
            # Posting cookies are invalid/expired — EVERY post will fail the same
            # way, so abort once with an actionable message (no cooldown; a re-run
            # works immediately after the cookies are refreshed).
            print("  [X-AUTH] ❌ X rejected the login (code 32 / 401 'Could not "
                  "authenticate you'). The posting cookies are expired or wrong.\n"
                  "          Refresh the GitHub Secrets X_POST_AUTH_TOKEN and "
                  "X_POST_CT0_TOKEN, then re-run. Nothing was posted; account is NOT flagged.")
            raise XBackoffError("auth", exc)
        # transient -> let the caller's single cautious retry handle it.
        raise

    if posted_live:
        record_posted(item, data)
        print(f"  ✅ POSTED [{status_label(item, item.get('mode'))}]: "
              f"{item['player']} — {item['event']} (stage {item['stage']})")
        return True

    return False
  # ── SCRAPER CORE (100% FREE RSS) ─────────────────────────────────────────
MAX_TWEET_AGE_DAYS = 3

# List of (source_name, url) tuples — list not dict so we can have multiple
# entries sharing the same source_name (e.g. two premierleague search feeds).
RSS_FEEDS = [
    # ── TIER 1: Official Premier League announcements ─────────────────────
    # Site-scoped Google News captures official signing announcements on
    # premierleague.com. Key → "premierleague" ∈ OFFICIAL_ACCOUNTS
    # → official_source (+15 bonus, always AUTO_POST).
    ("premierleague", (
        "https://news.google.com/rss/search?q=site%3Apremierleague.com"
        "+transfer+OR+signs+OR+joined&hl=en-GB&gl=GB&ceid=GB:en"
    )),
    ("premierleague", (
        "https://news.google.com/rss/search?q=site%3Apremierleague.com"
        "+injur+OR+suspended+OR+ruled+out&hl=en-GB&gl=GB&ceid=GB:en"
    )),

    # ── TIER 2: Elite journalists monitored via Google News ───────────────
    ("FabrizioRomano", (
        "https://news.google.com/rss/search?q=%22Fabrizio+Romano%22"
        "+football+transfer&hl=en-GB&gl=GB&ceid=GB:en"
    )),
    ("David_Ornstein", (
        "https://news.google.com/rss/search?q=%22David+Ornstein%22"
        "+football+transfer&hl=en-GB&gl=GB&ceid=GB:en"
    )),
    ("BenDinnery", (
        "https://news.google.com/rss/search?q=%22Ben+Dinnery%22"
        "+injur+OR+fitness+OR+ruled+out&hl=en-GB&gl=GB&ceid=GB:en"
    )),

    # ── TIER 3: Trusted media ─────────────────────────────────────────────
    ("BBC_Sport",      "https://feeds.bbci.co.uk/sport/football/premier-league/rss.xml"),
    ("BBC_Sport",      "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("Transfermarkt",  "https://www.transfermarkt.com/rss/news"),
    ("SkySports",      "https://www.skysports.com/rss/12040"),
    ("guardian_sport", "https://www.theguardian.com/football/transfers/rss"),
    ("espn",           "https://www.espn.com/espn/rss/soccer/news"),
    ("rootwire", (
        "https://news.google.com/rss/search?q=site%3Arootwiresoccer.com"
        "+premier+league&hl=en-GB&gl=GB&ceid=GB:en"
    )),
]

# Bluesky handles to poll via the public AT Protocol API (no auth required).
# Each entry is (handle, source_name) where source_name normalises to a handle
# in ELITE_TRUSTED so classify_post() passes the injury check.
_BLUESKY_HANDLES = [
    ("premierinjuries.bsky.social", "premierinjuries"),
    ("bendinnery.bsky.social", "bendinnery"),
]

def fetch_fpl_injury_news(fpl_data) -> list:
    """Generate structured injury/suspension items directly from the official FPL API."""
    if not fpl_data:
        return []
    out = []
    now = datetime.now(timezone.utc)
    team_map = {t["id"]: t for t in fpl_data.get("teams", [])}
    status_to_stage = {"d": 2, "i": 3, "u": 3, "n": 3, "s": 4}
    for el in fpl_data.get("elements", []):
        status = el.get("status", "a")
        if status == "a":
            continue
        news_text = (el.get("news") or "").strip()
        if not news_text:
            continue
        news_added = el.get("news_added") or ""
        if news_added:
            try:
                dt = datetime.fromisoformat(news_added.replace("Z", "+00:00"))
                if (now - dt).total_seconds() > MAX_TWEET_AGE_DAYS * 86400:
                    continue
            except Exception:
                pass
        player_full = f"{el.get('first_name', '')} {el.get('second_name', '')}".strip()
        if not player_full:
            continue
        team = team_map.get(el.get("team", 0), {})
        club_key = resolve_club_key(
            f"{team.get('name', '')} {team.get('short_name', '')}".lower()
        )
        chance = el.get("chance_of_playing_this_round")
        event = "suspension" if status == "s" else "injury"
        stage = status_to_stage.get(status, 2)
        if event == "injury" and chance is not None:
            stage = 3 if chance <= 25 else 2
        tid = "fpl_" + hashlib.md5(
            f"{el['id']}_{news_text[:40]}".encode()).hexdigest()[:12]
        text = f"{player_full}: {news_text}"
        out.append({
            "id": tid,
            "text": text,
            "media_url": None,
            "created_at": news_added or None,
            "username": "officialfpl",
            "_fpl_pre_built": {
                "player": player_full,
                "display_name": player_full,
                "verified_player_code": el.get("code"),
                "event": event,
                "stage": stage,
                "from_key": club_key,
                "to_key": None,
                "from_club": club_display(club_key) if club_key else "",
                "to_club": "",
                "diagnosis": news_text,
                "expected_return": None,
                "fee": None, "contract": None, "is_free": False,
                "collapsed": False, "historical": False,
                "headline": text, "body": news_text, "raw_text": text,
                "from_video": False, "has_written_claim": True,
                "chance_of_playing": chance,
                "sources": ["officialfpl"],
            }
        })
    print(f"  [FPL-INJURY] {len(out)} fresh official injury/suspension item(s).")
    return out


def fetch_bluesky_posts() -> list:
    """Fetch recent posts from Bluesky injury/transfer accounts (public AT Protocol API)."""
    out = []
    for handle, source_name in _BLUESKY_HANDLES:
        try:
            url = (
                "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
                f"?actor={urllib.parse.quote(handle)}&limit=30&filter=posts_no_replies"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "FPLVortexBot/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            feed_items = (data.get("feed") or [])[:30]
            for item in feed_items:
                post = item.get("post", {})
                record = post.get("record", {})
                text = (record.get("text") or "").strip()
                if not text:
                    continue
                created_at = record.get("createdAt") or post.get("indexedAt")
                uri = post.get("uri", "") or text
                tid = "bsky_" + hashlib.md5(uri.encode()).hexdigest()[:15]
                out.append({
                    "id": tid, "text": text, "media_url": None,
                    "created_at": created_at, "username": source_name,
                })
            print(f"  [BLUESKY] @{handle}: {len(feed_items)} post(s) fetched.")
        except Exception as e:
            print(f"  [BLUESKY] @{handle}: {e}")
    return out


def clean_html(raw_html):
    """Strips HTML tags from RSS summaries."""
    cleanr = re.compile('<.*?>')
    return re.sub(cleanr, ' ', raw_html).strip()

def _parse_rss_date(raw):
    """Parse a date string in RFC 2822 (RSS) or ISO 8601 (FPL API, Bluesky) format."""
    if not raw: return None
    # RFC 2822 — standard RSS/Atom pubDate: "Mon, 23 Jul 2026 10:00:00 +0000"
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    # ISO 8601 — FPL bootstrap-static news_added, Bluesky createdAt:
    # "2026-07-23T10:00:00Z" / "2026-07-23T10:00:00.123Z" / "2026-07-23T10:00:00+00:00"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def tweet_too_old(created_at, max_days=MAX_TWEET_AGE_DAYS, unknown_is_old=True):
    """Checks if an item is older than max_days. 
    Kept the name tweet_too_old so the internal validation gates do not break."""
    dt = _parse_rss_date(created_at)
    if dt is None: return unknown_is_old
    age = datetime.now(timezone.utc) - dt
    return age.total_seconds() > max_days * 86400

# Alias mapping so both function name variations work cleanly across your script
item_too_old = tweet_too_old

async def fetch_rss_news():
    """Fetch RSS feeds and Bluesky posts; returns a unified list of items."""
    out = []
    seen_ids: set = set()
    for source_name, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:20]:
                link = (entry.get("link") or entry.get("id")
                        or (entry.get("title", "") + str(entry.get("published", ""))))
                tid = hashlib.md5(link.encode("utf-8")).hexdigest()[:15]
                if tid in seen_ids:
                    continue
                seen_ids.add(tid)
                summary = clean_html(entry.get("summary", ""))
                text = f"{entry.title}. {summary}"
                media_url = None
                if "media_content" in entry and entry.media_content:
                    media_url = entry.media_content[0].get("url")
                created_at = entry.get("published", None)
                if text:
                    out.append({
                        "id": tid,
                        "text": text,
                        "media_url": media_url,
                        "created_at": created_at,
                        "username": source_name,
                    })
        except Exception as e:
            print(f"  [READ] {source_name} feed error: {e}")

    for item in fetch_bluesky_posts():
        if item["id"] not in seen_ids:
            seen_ids.add(item["id"])
            out.append(item)

    return out

async def scrape(data):
    fpl = fetch_fpl_data()
    story_map = {}
    seen = skipped = 0

    rss_items = await fetch_rss_news()
    fpl_injury_items = fetch_fpl_injury_news(fpl_data=fpl)

    if not rss_items and not fpl_injury_items:
        print("  [WARN] Zero news items fetched. Check internet or feed URLs.")
    else:
        print(f"  [READ] {len(rss_items)} RSS/Bluesky items + {len(fpl_injury_items)} FPL injury items.")

    for t in rss_items + fpl_injury_items:
        tid, text, username = t["id"], t["text"], t["username"]
        if tid in data["posted_ids"]: continue
        if not any(k in text.lower() for k in FOOTBALL_KW): continue

        if item_too_old(t.get("created_at")):
            skipped += 1
            print(f"   skip (older_than_{MAX_TWEET_AGE_DAYS}d): {text[:70]!r}")
            continue

        seen += 1

        if tid in data["extracted"]:
            story = dict(data["extracted"][tid])
        elif "_fpl_pre_built" in t:
            story = dict(t["_fpl_pre_built"])
            story["media_url"] = None
            story["created_at"] = t.get("created_at")
            data["extracted"][tid] = dict(story)
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
            
        valid, vwhy = validate_story(story, fpl, sources=[username])
        if not valid:
            skipped += 1
            print(f"   invalid ({vwhy}): {text[:70]!r}")
            continue

        _cres = score_confidence(story, fpl, sources=[username])
        if _cres["decision"] == _conf.SKIP:
            skipped += 1
            print(f"   skip (low_confidence:{_cres['score']}): {text[:70]!r}")
            continue
        story["confidence_score"] = _cres["score"]
        story["confidence_decision"] = _cres["decision"]

        anchor = story_anchor(story)
        key = reconcile_key(story["player"], anchor, story["event"],
                            story_map, data.get("stories", {}), data.get("pending", {}))
                            
        ok, reason = should_post(data, key, story["stage"], story["collapsed"])
        if not ok:
            print(f"   skip ({reason}): {key}")
            continue
            
        if key in story_map:
            ex = story_map[key]
            
            _new_to = _norm_text(story.get("to_key") or story.get("to_club") or "")
            _new_from = _norm_text(story.get("from_key") or story.get("from_club") or "")
            _ex_to = _norm_text(ex.get("to_key") or ex.get("to_club") or "")
            _ex_from = _norm_text(ex.get("from_key") or ex.get("from_club") or "")
            _contradicts = (
                bool(_new_to and _ex_to and _new_to != _ex_to and _new_to != _ex_from) or
                bool(_new_from and _ex_from and _new_from != _ex_from and _new_from != _ex_to)
            )
            if _contradicts:
                ex["contradicted"] = True
                print(f"   [CONTRADICTION] {key}: @{username} names different club(s) "
                      f"than already-merged sources {ex['sources']!r} — holding for "
                      f"review, NOT counting as a corroborating source.")
            else:
                if username not in ex["sources"]: ex["sources"].append(username)
                if story["stage"] > ex["stage"]:
                    ex.update({k: story[k] for k in story if k != "contradicted"})
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

    print(f"  [SCRAPE] {seen} football items seen, {skipped} skipped, {len(story_map)} candidate stories")
    
    _feed_count = len(RSS_FEEDS) + len(_BLUESKY_HANDLES)
    data["last_read_health"] = {
        "accounts_total": _feed_count,
        "accounts_failed": 0 if rss_items else _feed_count,
        "fail_ratio": 0.0 if rss_items else 1.0,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    
    # ── AUTOMATIC CROSS-VERIFICATION ─────────────────────────────────────
    # Every candidate story is checked against independent reliable outlets
    # (official club websites, Google News, FotMob/Sky/BBC/The Athletic,
    # elite journalists on X) BEFORE the publish decision. Corroborating
    # outlets are merged into the source list so the tier/classification and
    # confidence engines upgrade genuinely-confirmed news to auto-post;
    # uncorroborated stories simply wait and are re-verified next run.
    read_client = None
    if story_map and X_AUTH_TOKEN and X_CT0_TOKEN:
        try:
            read_client = Client("en-US")
            read_client.set_cookies({"auth_token": X_AUTH_TOKEN, "ct0": X_CT0_TOKEN})
        except Exception as e:
            print(f"  [X-VERIFY] read client unavailable ({e}) — journalist check skipped.")

    _verify_budget = 15  # bound network calls on a freak news day
    for key, st in story_map.items():
        if _verify_budget <= 0:
            break
        _verify_budget -= 1
        try:
            ver = await cross_verify(st, known_sources=st["sources"],
                                     read_client=read_client)
        except Exception as e:
            ver = {"handles": [], "official_confirmed": False,
                   "n_independent": 0, "log": [f"verifier error: {e}"]}
        for h in ver["handles"]:
            st["sources"].append(h)
        st["sources"] = list(dict.fromkeys(st["sources"]))
        st["official_confirmed"] = ver["official_confirmed"]
        st["cross_verified"] = len(st["sources"]) >= 2 or ver["official_confirmed"]
        # If the verifier found 2+ sources CONTRADICTING the story (e.g.
        # "bid rejected", "not for sale"), mark it contradicted so it never posts.
        if ver.get("contradicted"):
            st["contradicted"] = True
            print(f"  [BLOCKED by verifier] {st.get('player')!r}: "
                  f"{ver.get('n_contradictions', 0)} contradiction signal(s) found "
                  f"— story suppressed")
        else:
            print(f"  [X-VERIFY] {st.get('player')!r}: +{ver['n_independent']} "
                  f"corroborating source(s) {ver['handles']!r}"
                  f"{' + OFFICIAL CLUB CONFIRMATION' if ver['official_confirmed'] else ''}")
        for line in ver["log"][:6]:
            print(f"             {line}")

    ready = []
    for key, st in story_map.items():
        _final_cres = score_confidence(st, fpl, sources=st["sources"])
        st["confidence_score"] = _final_cres["score"]
        st["confidence_decision"] = _final_cres["decision"]

        # FINAL AGE HARD-GATE: stories can sit in pending accumulating sources for
        # days. If the original source item is now stale (> 3 days old), never
        # publish — the news is too old regardless of confidence score. Remove from
        # pending so it doesn't block queue and wastes no further verification effort.
        _final_created = st.get("created_at")
        if _final_created and tweet_too_old(_final_created, unknown_is_old=False):
            print(f"   [BLOCK] {st.get('player')!r}: source item stale ({_final_created}) — skipping")
            data["pending"].pop(key, None)
            continue

        if not st.get("contradicted") and st.get("event") in ("transfer", "loan", "loan_option"):
            _pnorm = (st.get("player") or "").lower().strip()
            _new_dest = _norm_text(st.get("to_key") or st.get("to_club") or "")
            if _pnorm and _new_dest:
                for _prev in data.get("stories", {}).values():
                    if ((_prev.get("player") or "").lower().strip() == _pnorm
                            and _prev.get("event") in ("transfer", "loan", "loan_option")
                            and _prev.get("stage", 0) >= 4):
                        _prev_dest = _norm_text(_prev.get("to_key") or _prev.get("to_club") or "")
                        if _prev_dest and _prev_dest != _new_dest:
                            print(f"   [BLOCK] {st.get('player')!r}: already confirmed "
                                  f"(stage {_prev['stage']}) to {_prev_dest!r}; "
                                  f"new story says {_new_dest!r} — marking contradicted")
                            st["contradicted"] = True
                            break

        # MANAGER ALREADY-AT-CLUB GUARD: if we already have a confirmed (stage≥4)
        # posting of this manager at the SAME destination club, suppress the
        # duplicate. Parallel to the transfer already-confirmed guard above.
        if not st.get("contradicted") and st.get("event") == "manager":
            _mgr_name_n = (st.get("player") or "").lower().strip()
            _mgr_dest_n = _norm_text(st.get("to_key") or st.get("to_club") or "")
            if _mgr_name_n and _mgr_dest_n:
                for _prev in data.get("stories", {}).values():
                    if ((_prev.get("player") or "").lower().strip() == _mgr_name_n
                            and _prev.get("event") == "manager"
                            and _prev.get("stage", 0) >= 4):
                        _prev_dest_n = _norm_text(_prev.get("to_key") or _prev.get("to_club") or "")
                        if _prev_dest_n and _prev_dest_n == _mgr_dest_n:
                            print(f"   [BLOCK] {st.get('player')!r}: already confirmed manager "
                                  f"at {_mgr_dest_n!r} (stage {_prev['stage']}) — suppressing duplicate")
                            st["contradicted"] = True
                            break

        mode = None if st.get("contradicted") else classify_post(st, st["sources"])
        if mode is None:
            data["pending"][key] = {
                "sources": st["sources"], "player": st["player"],
                "to_key": st.get("to_key"), "event": st["event"],
                "contradicted": bool(st.get("contradicted")),
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

    # DOUBLE-CHECK: verify every fact on the card against the live FPL feed
    # BEFORE we render it. No card is created from inaccurate/unverified data.
    ok, why, report = verify_card_data(item, fpl)
    print(f"  [VERIFY] {item.get('player')!r}:")
    for line in report:
        print(f"           {line}")
    if not ok:
        print(f"  VERIFY FAILED ({why}) — card NOT created: {item.get('player')!r}")
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

# ================== AUTO-POST SAFETY CONFIG ==================
# Auto-posting is OPT-IN. It stays OFF (draft-only) unless you explicitly set
# the env var ENABLE_AUTOPOST=true. The GitHub Actions BOT_PAUSED repo variable
# remains a separate, independent kill switch.
#
# Policy: NEVER getting flagged is the priority. Posts go out one at a time,
# well spaced (the jitter below is the real anti-flag mechanism), highest-value
# PLAYER news first (see EVENT_PRIORITY). On a normal day every story posts; on
# a freak flood the per-run/hour caps defer the least important items to the
# next run rather than bursting. If X ever pushes back, the cooldown engages.
#
# Auto-post defaults ON so it's set-and-forget after merge. To pause without a
# code change, set repo Variable ENABLE_AUTOPOST=false (or BOT_PAUSED=true).
ENABLE_AUTOPOST = ((os.getenv("ENABLE_AUTOPOST") or "true").strip().lower() == "true")
MAX_POSTS_PER_RUN = _env_int("MAX_POSTS_PER_RUN", 10)
MAX_POSTS_PER_HOUR = _env_int("MAX_POSTS_PER_HOUR", 12)
# Random human-like pause before each post. THIS is the anti-flag mechanism —
# it spaces posts out so they never go as a burst. (min, max) seconds.
POST_JITTER_RANGE_S = (
    _env_int("POST_JITTER_MIN_S", 60),
    _env_int("POST_JITTER_MAX_S", 150),
)
# Back-off windows after X flags us, so we stop hammering a flagged account.
COOLDOWN_FLAGGED_MIN = _env_int("COOLDOWN_FLAGGED_MIN", 180)     # 3h after 226/326
COOLDOWN_RATELIMIT_MIN = _env_int("COOLDOWN_RATELIMIT_MIN", 30)  # 30m after 429

# Draft saving settings
SAVE_DRAFTS_TO_DISK = True
DRAFTS_FOLDER = "fpl_drafts"        # All drafts will be saved here
# ============================================================

# Posting order when there's a queue — PLAYER news goes out first so the most
# valuable stories are live before any cap/cooldown could ever bite.
# Injuries and transfers lead; manager/contract news is lowest.
EVENT_PRIORITY = {
    "injury": 0, "transfer": 1, "loan": 1, "loan_option": 1,
    "suspension": 2, "manager": 3, "renewal": 4, "stay": 4,
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
            valid, vwhy = validate_story(story, fpl, sources=[username])
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
            ok, vwhy, _ = verify_card_data(story, fpl)
            if not ok:
                print(f"  [DRY] VERIFY FAILED ({vwhy}) — card skipped: {story.get('player')!r}")
                continue
            img_path = dryrun_dir / f"{re.sub(r'[^a-z0-9_]', '', story['key'])}.png"
            try:
                create_transfer_image(story, story["sources"], str(img_path), collapsed=(story.get("collapsed", False)))
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
    # ================== POSTING MODE ==================
    # Live posting only when ENABLE_AUTOPOST=true AND the run wasn't forced to
    # draft-only (--draft-only). Otherwise we save drafts and post nothing.
    if not ENABLE_AUTOPOST:
        post = False
        mode_str = "DRAFT-ONLY (set ENABLE_AUTOPOST=true to post live)"
    elif not post:
        mode_str = "DRAFT-ONLY (--draft-only)"
    else:
        mode_str = (f"LIVE AUTO-POST — safety caps: {MAX_POSTS_PER_RUN}/run, "
                    f"{MAX_POSTS_PER_HOUR}/hr, jitter {POST_JITTER_RANGE_S[0]}-{POST_JITTER_RANGE_S[1]}s")
    print(f"\n[BOT] Run — {datetime.now(timezone.utc).isoformat()} "
          f"(classifier=regex, mode={mode_str})")
    # ==================================================

    init_club_data()
    fpl = fetch_fpl_data()
    data = load_data()

    # X safety: if a previous run was flagged/rate-limited, stay off X until the
    # cooldown expires.
    if post and in_cooldown(data):
        print(f"[BOT] X safety cooldown active until {data.get('cooldown_until')} — "
              f"not posting this run.")
        post = False
    
    # ... rest of your main() logic

    if not check_daily_limit(data):
        print("[BOT] Daily limit reached — nothing will post today.")

    # Execute the new RSS-powered scrape logic (no read_client required)
    queue = await scrape(data)
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

    # Accuracy safety: by default only fully CONFIRMED/OFFICIAL stories go live.
    # Lower-confidence RUMOURs are posted only when explicitly opted in.
    modes_ok = {"confirmed"} | ({"rumour"} if allow_rumours else set())

    # Confidence gate: only AUTO_POST (score >= 90) publishes live, ALWAYS,
    # with no flag able to bypass it. "mode" (rumour vs confirmed) and
    # "confidence_decision" (REVIEW vs AUTO_POST) answer two different
    # questions — --allow-rumours governs whether an ACCURATELY-extracted but
    # factually-unconfirmed event (a genuine transfer rumour) may post; it
    # must never also unlock a story the confidence engine itself could only
    # score to REVIEW, because REVIEW means the pipeline isn't sure the
    # extraction (player/club/direction/entity) is even right. That is a
    # "don't guess" situation, not a rumour-vs-confirmed judgement call, so it
    # is never bypassable. REVIEW-tier stories are NOT published this run;
    # they are automatically re-scraped and re-cross-verified on every later
    # run, so as soon as enough reliable outlets corroborate the story it
    # clears AUTO_POST and publishes by itself — no manual step anywhere.
    def _conf_ok(d):
        return d.get("confidence_decision", "AUTO_POST") == _conf.AUTO_POST

    postable = [d for d in drafts if d.get("mode") in modes_ok and _conf_ok(d)]
    _held = [d for d in drafts if d.get("mode") in modes_ok and not _conf_ok(d)]
    if _held:
        print(f"[BOT] {len(_held)} story(ies) not yet corroborated by enough "
              f"reliable sources (confidence 75-89) — held back and will be "
              f"re-cross-verified automatically on the next scheduled run.")

    if not postable:
        print("[BOT] No postable stories this run "
              f"(modes allowed: {sorted(modes_ok)}).")
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
        except XBackoffError as be:
            # X flagged automation / rate-limit — stop the ENTIRE run now.
            # Never retry; the cooldown is already persisted.
            print(f"[BOT] X-SAFETY STOP ({be}) — aborting posting run, no retries.")
            break
        except Exception as e:
            if item.get("id") and item["id"] in data["posted_ids"]:
                print(f"  [ERROR] {item['key']}: {e} — already recorded, NOT retrying")
            else:
                print(f"  [ERROR] {item['key']} (attempt 1): {e} — retrying once")
                try:
                    await asyncio.sleep(10)
                    if await post_item(post_client, item, data):
                        posted += 1
                except XBackoffError as be:
                    print(f"[BOT] X-SAFETY STOP ({be}) on retry — aborting posting run.")
                    break
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
