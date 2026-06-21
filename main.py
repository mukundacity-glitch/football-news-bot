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

from bot_config import load_config
from fpl_injuries import fpl_injury_stories, commit_posted_fpl, clear_fit_players
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

# ── SECRETS ──────────────────────────────────────────────────────────────
X_AUTH_TOKEN = (os.getenv("X_AUTH_TOKEN") or "").strip()
X_CT0_TOKEN = (os.getenv("X_CT0_TOKEN") or "").strip()
X_POST_AUTH_TOKEN = (os.getenv("X_POST_AUTH_TOKEN") or "").strip()
X_POST_CT0_TOKEN = (os.getenv("X_POST_CT0_TOKEN") or "").strip()

# ── CONFIG (all changing data lives in config.json) ──────────────────────
CFG = load_config()
SETTINGS = CFG["settings"]

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
# ── SOURCES (from config) ────────────────────────────────────────────────
# Journalists drive transfer detection; club accounts confirm; media supplement.
JOURNALISTS = (CFG["journalists"] + CFG["media_accounts"]
               + CFG["official_club_accounts"])
NITTER_INSTANCES = CFG["nitter_instances"]

# ── SOURCE TIERS (from config) ───────────────────────────────────────────
OFFICIAL_ACCOUNTS = set(s.lower() for s in CFG["source_tiers"]["official"])
ELITE_TRUSTED = set(s.lower() for s in CFG["source_tiers"]["reporter"])
TRUSTED_MEDIA = set(s.lower() for s in CFG["source_tiers"]["media"])
OFFICIAL_INJURY_ACCOUNTS = OFFICIAL_ACCOUNTS | {"officialfpl", "fpl", "premierleague"}
TRUSTED_REPORTERS = ELITE_TRUSTED

def source_tier(handle: str) -> int:
    h = (handle or "").lower().lstrip("@")
    if h in OFFICIAL_ACCOUNTS: return 1
    if h in ELITE_TRUSTED: return 2
    if h in TRUSTED_MEDIA: return 3
    return 0

# ── KEYWORDS (from config) ───────────────────────────────────────────────
FOOTBALL_KW = CFG["keywords"]["football"]
STAFF_BLOCK_KW = CFG["keywords"]["staff_block"]
TRANSFER_SIGNAL_KW = CFG["keywords"]["transfer_signals"]
INJURY_KW = CFG["keywords"]["injury_words"]
SUSPENSION_KW = CFG["keywords"]["suspension_words"]

# ── CLUB MAPS (from config) ──────────────────────────────────────────────
CLUB_ALIASES = CFG["club_aliases"]
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
FPL_LOGO_IDS = CFG["club_crest_ids"]
CLUB_COLORS = CFG["club_colors"]
CLUB_HASHTAG_MAP = CFG["club_hashtags"]
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

BIG_NAMES_NON_FPL = set(s.lower() for s in CFG["big_name_players"])
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

# ── CLUB DATA WIRING (config-only, no external API) ──────────────────────
CLUB_NAME_SET = set()
CLUB_HASHTAGS = {}

def init_club_data():
    """Build club lookup tables purely from config — no football API call.
    This used to hit football-data.org (now removed). Everything comes from
    config.json's club_aliases / club_hashtags."""
    global CLUB_NAME_SET, CLUB_HASHTAGS, PL_CLUB_NAMES
    CLUB_HASHTAGS = {k.lower(): v for k, v in CLUB_HASHTAG_MAP.items()}
    # also index by alias so hashtag_for() can resolve human names
    for alias, key in CLUB_ALIASES.items():
        if key in CLUB_HASHTAG_MAP:
            CLUB_HASHTAGS[alias.lower()] = CLUB_HASHTAG_MAP[key]
    PL_CLUB_NAMES = {a for a in CLUB_ALIASES}
    CLUB_NAME_SET = set(CLUB_ALIASES.keys()) | set(CLUB_HASHTAGS.keys())
    _build_club_word_fragments()
    print(f"[CLUBS] Club data ready from config ({len(CLUB_ALIASES)} aliases).")

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
    fresh = {"daily": {"date": "", "count": 0, "limit": SETTINGS["daily_limit"]}, "stories": {}, "posted_ids": []}
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
    d.setdefault("posted_headlines_staged", [])
    d.setdefault("posted_stage_keys", [])
    d.setdefault("deferred", [])
    d.setdefault("posts_by_type_today", {})
    d.setdefault("posts_by_type_date", "")
    # Daily limit always reflects current config, even on an old state file.
    d["daily"]["limit"] = SETTINGS["daily_limit"]
    return d

def save_data(data: dict):
    tmp = POSTED_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f: json.dump(data, f, indent=2)
    tmp.replace(POSTED_FILE)

def check_daily_limit(data: dict) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if data["daily"]["date"] != today:
        data["daily"] = {"date": today, "count": 0, "limit": SETTINGS["daily_limit"]}
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
    # Remove a full Nitter/Twitter byline like "paul joyce (@_pauljoyce)"
    # before stripping bare handles, so no orphaned name/parens are left.
    t = re.sub(r'[A-Za-z][\w.\'’-]*(?:\s+[A-Za-z][\w.\'’-]*){0,3}\s*\(@\w+\)', ' ', t)
    t = re.sub(r'(?<!\w)@\w+', ' ', t)
    t = re.sub(r'#\w+', ' ', t)
    t = re.sub(r'\(\s*\)', ' ', t)          # drop orphaned empty parentheses
    t = re.sub(r'[“”"]', '', t)
    t = re.sub(r'\s+([.,;:])', r'\1', t)     # tidy space-before-punctuation
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
    if not _GEMINI_OK: return None
    text, _model = _gemini_generate(_EXTRACT_PROMPT.format(tweet=tweet_text))
    if not text: return None
    try:
        return json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception as e:
        print(f"  [LLM] JSON parse failed, using fallback: {e}")
        return None

_FALLBACK_BANNED_SOLO = {
    "neil", "silva", "alonso", "robinson", "edwards", "wilder", "obi",
    "kompany", "nuno", "frank", "howe", "moyes", "dyche", "emery", "conte",
    "tuchel", "klopp", "arteta", "slot", "amorim", "maresca", "iraola",
}

def _is_safe_fallback_name(name: str, fpl_data=None) -> bool:
    if not name: return False
    tokens = [t for t in re.split(r"[\s\-']+", name.strip()) if t]
    low = name.lower()
    # Manager surnames are still blocked as transfer SUBJECTS.
    if low in _FALLBACK_BANNED_SOLO: return False
    if any(m == low for m in MANAGER_SURNAMES): return False
    # Two+ token names are accepted as before.
    if len(tokens) >= 2:
        if any(m in low for m in MANAGER_SURNAMES): return False
        return True
    # Single-token names (Tonali, Casemiro, Cunha, Ederson) are allowed ONLY
    # when corroborated as a real player — FPL match or known big name — so we
    # don't turn random capitalised words into "players".
    if fpl_data is not None and find_player_in_fpl(name, fpl_data):
        return True
    if is_big_name_player(name):
        return True
    return False

def extract_story_fallback(tweet_text: str, fpl_data=None) -> dict:
    cleaned = _clean_source_text(tweet_text)
    tl = cleaned.lower()

    def has_word(words_list, text):
        return any(re.search(r'(?<![a-z])' + re.escape(w) + r'(?![a-z])', text) for w in words_list)

    if has_word(["suspended", "suspension", "banned", "ban", "red card", "sent off"], tl): event = "suspension"
    elif has_word(["injury", "injured", "ruled out", "scan", "hamstring", "surgery", "doubt"], tl): event = "injury"
    elif has_word(["sack", "appoint", "head coach", "manager"], tl): event = "manager"
    elif has_word(["new deal", "new contract", "signs new", "extension", "renew"], tl): event = "renewal"
    elif has_word(["stay", "staying", "no exit", "not for sale", "remain"], tl) and not has_word(["sign for", "joins", "move to"], tl): event = "stay"
    elif has_word(["loan"], tl): event = "loan"
    else: event = "transfer"

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
    # FIRST: a known big-name single token (Casemiro, Tonali…) or an FPL player,
    # so we don't mistakenly grab an unrecognised two-word CLUB as the player.
    for cand in re.findall(r'\b([A-Z][a-zà-ÿ]{2,})\b', cleaned):
        cl = cand.lower()
        if _is_bad_name(cl): continue
        if is_big_name_player(cand) or (fpl_data and find_player_in_fpl(cand, fpl_data)):
            name = cand
            break
    # THEN: multi-word proper names (with optional nobiliary particles).
    if not name:
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

    if name and not _is_safe_fallback_name(name, fpl_data):
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

    is_collapsed = has_word(["collapsed", "called off", "rejected", "deal off"], tl)

    if event in ("stay", "renewal"):
        if to_key and not from_key:
            from_key, to_key = to_key, None
        to_key = None
        is_collapsed = False

    if is_collapsed and to_key and not from_anchor:
        to_key = None

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

def detect_historical(text: str) -> bool:
    """True when a tweet is about a clearly OLD event, not current news."""
    t = text or ""
    tl = t.lower()
    if _HISTORICAL_MARKERS.search(tl):
        return True
    # Any 1900s year in a football tweet = historical, full stop.
    if re.search(r"\b19\d\d\b", t):
        return True
    # A 2000s year >= 2 seasons old, stated as the time of the event
    # ("in/back in/during/on 2018"), = old news. "until 2030" / "since 2022"
    # are NOT matched, so future contract terms and ongoing context are safe.
    cur = datetime.now(timezone.utc).year
    for y in re.findall(r"\b(?:in|back in|during|on)\s+(20\d\d)\b", tl):
        if int(y) <= cur - 2:
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
    """Stage-AWARE content hash. A story at stage 2 and the SAME story later at
    stage 3 hash differently (so genuine progression can post), but the exact
    same story at the same stage hashes identically and can never repost."""
    parts = [
        _event_family(story.get("event")),
        _norm_text(story.get("player")),
        _norm_text(story.get("from_key") or story.get("from_club")),
        _norm_text(story.get("to_key") or story.get("to_club")),
        _norm_text(story.get("headline")),
        f"stage{int(story.get('stage', 1))}",          # ← per-stage uniqueness
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

def stage_post_key(story: dict) -> str:
    """A compact key identifying a story at a specific stage, used for the
    explicit 'one post per stage' ledger."""
    fam = _event_family(story.get("event"))
    player = _norm_text(story.get("player"))
    club = _norm_text(story.get("to_key") or story.get("to_club")
                      or story.get("from_key") or story.get("from_club") or "unknown")
    return f"{fam}|{player}|{club}|stage{int(story.get('stage', 1))}"

def is_duplicate_content(story: dict, data: dict, threshold: float = 0.90):
    # 1) Explicit per-stage ledger — the hard "never same stage twice" rule.
    spk = stage_post_key(story)
    if spk in data.get("posted_stage_keys", []):
        return True, "stage_already_posted"
    # 2) Stage-aware content hash.
    h = content_hash(story)
    if h in data.get("posted_hashes", []): return True, "content_hash"
    # 3) Fuzzy headline match — but ONLY within the same stage, so a stage-2
    #    "talks" post doesn't block the later stage-4 "here we go" post.
    head = _norm_text(story.get("headline") or story.get("player"))
    stg = int(story.get("stage", 1))
    if head:
        for prev_head, prev_stage in data.get("posted_headlines_staged", []):
            if prev_stage == stg and difflib.SequenceMatcher(
                    None, head, prev_head).ratio() >= threshold:
                return True, f"fuzzy_headline_same_stage>={threshold:.2f}"
    return False, ""

def record_content_dedup(story: dict, data: dict):
    spk = stage_post_key(story)
    if spk not in data.setdefault("posted_stage_keys", []):
        data["posted_stage_keys"].append(spk)
    h = content_hash(story)
    if h not in data.setdefault("posted_hashes", []):
        data["posted_hashes"].append(h)
    head = _norm_text(story.get("headline") or story.get("player"))
    stg = int(story.get("stage", 1))
    staged = data.setdefault("posted_headlines_staged", [])
    if head and [head, stg] not in staged:
        staged.append([head, stg])
    # legacy field kept for backward compat with old state files
    legacy = data.setdefault("posted_headlines", [])
    if head and head not in legacy:
        legacy.append(head)
    for fld in ("posted_stage_keys", "posted_hashes", "posted_headlines",
                "posted_headlines_staged"):
        if len(data.get(fld, [])) > 3000:
            data[fld] = data[fld][-3000:]

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

def should_post(data, key, new_stage, collapsed, new_sources=None):
    existing = data["stories"].get(key)
    if collapsed:
        if not existing or existing["status"] == "active": return True, "collapse"
        return False, "already_collapsed"
    if not existing: return True, "new"
    if existing["status"] == "collapsed": return False, "story_collapsed"
    if new_stage > existing.get("stage", 1): return True, "progression"
    # RELAXED no_progression: a same-stage story is still worth (re)considering
    # if a NEW trusted source has corroborated it since we last posted. Content
    # dedup downstream still prevents identical re-posts, so this only lets a
    # genuinely strengthened story through, not spam.
    prev_sources = set(s.lower() for s in (existing.get("sources") or []))
    fresh = [s for s in (new_sources or []) if s.lower() not in prev_sources]
    if fresh:
        return True, "new_corroboration"
    return False, "no_progression"

# ── SAFETY + ACCURACY GATES ──────────────────────────────────────────────
STRONG_OFFICIAL = CFG["keywords"]["strong_official"]

def detect_mixed_story(story, raw_text) -> str:
    """Only flag a story as mixed when it is TRULY unusable. Relaxed per spec:
    a tweet that mentions two clubs (selling + buying) or a player plus a
    passing manager reference is normal transfer news, not a mixed story."""
    text = (raw_text or "")
    tl = text.lower()
    player = (story.get("player") or "").lower()
    ev = story.get("event")
    # The transfer SUBJECT itself being a manager is a real error.
    if ev != "manager" and player and (player in MANAGER_SURNAMES or
                                       any(m == player for m in MANAGER_SURNAMES)):
        return "player_is_manager"
    # Negated move misread as a transfer ("X will NOT leave / not for sale").
    if ev in ("transfer", "loan", "loan_option"):
        if re.search(r'\bno\s+(move|exit|transfer|deal)\b', tl) or \
           re.search(r'\b(not|never)\s+(?:moving|leaving|for sale|going)\b', tl):
            return "negated_move_misread_as_transfer"
    # Genuinely tangled: THREE or more clearly distinct full-name players.
    name_candidates = set()
    for mm in re.findall(r'\b([A-Z][a-zà-ÿ]+(?:\s+[A-Z][a-zà-ÿ]+){1,2})\b', text):
        low = mm.lower()
        if any(m in low for m in MANAGER_SURNAMES): continue
        if looks_like_club(low): continue
        name_candidates.add(low)
    distinct = set()
    for n in sorted(name_candidates, key=len, reverse=True):
        if not any(n != o and n in o for o in distinct): distinct.add(n)
    if len(distinct) >= 3:
        return "multiple_players_suspected"
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
    if story.get("historical") and not ALLOW_HISTORICAL_POSTS: return False, "historical_news"
    if story.get("confidence", 0) < 0.40: return False, "low_confidence"
    # Block genuine off-pitch STAFF stories (sporting director, CEO, scout…)
    # but NOT when the subject is a player/manager move.
    if any(re.search(r'(?<![a-z])' + re.escape(w) + r'(?![a-z])', tl) for w in STAFF_BLOCK_KW):
        return False, "staff_or_offpitch"
    if not story.get("player"): return False, "no_player"
    mixed = detect_mixed_story(story, raw_text)
    if mixed: return False, f"mixed_story:{mixed}"
    if story.get("from_video") and not story.get("has_written_claim"): return False, "video_no_written_claim"
    if player_already_at_club(story, fpl_data): return False, "already_at_destination"

    # ── INJURY / SUSPENSION ──────────────────────────────────────────────
    # Per spec: allow partial-but-clear updates. Require a trusted/official
    # source OR an FPL-known player, but do NOT require Bundesliga/LaLiga gating
    # or a club lookup that the (now absent) football API used to provide.
    if story["event"] in ("injury", "suspension"):
        tiers = [source_tier(s) for s in sources]
        trusted = any(t in (1, 2, 3) for t in tiers) or \
            any((s or "").lower().lstrip("@") in OFFICIAL_INJURY_ACCOUNTS for s in sources)
        pl_player = find_player_in_fpl(story["player"], fpl_data) is not None
        if trusted or pl_player:
            return True, f"ok_{story['event']}"
        return False, f"{story['event']}_source_not_trusted"

    # ── MANAGER ──────────────────────────────────────────────────────────
    if story["event"] == "manager":
        if story.get("to_key") or story.get("to_club"):
            return True, "ok_manager"
        # allow if there's a clear appointment cue even without resolved club
        if re.search(r"\b(appoint|appointed|new (head coach|manager|boss)|"
                     r"sacked|named (as )?(head coach|manager)|takes over)\b", tl):
            return True, "ok_manager_cue"
        return False, "manager_no_club"

    # ── TRANSFER / LOAN / RENEWAL / STAY ─────────────────────────────────
    # Per spec: do NOT require destination club, fee, or confirmation. A clear
    # transfer SIGNAL from a trusted source about a real player is postable.
    TRANSFER_SIGNALS = [
        "fresh bid", "bid expected", "new bid", "bid", "talks", "negotiation",
        "personal terms", "interest", "interested", "medical", "here we go",
        "agreement", "agreed", "deal", "close to", "set to", "advanced",
        "loan", "sign", "signing", "joins", "join", "move", "target",
        "contract", "new deal", "extension", "renew", "stay", "swap", "fee",
        "offer", "linked", "approach", "verbal", "green light",
    ]
    has_signal = any(re.search(r'(?<![a-z])' + re.escape(w) + r'(?![a-z])', tl)
                     for w in TRANSFER_SIGNALS)
    tiers = [source_tier(s) for s in sources]
    trusted_source = any(t in (1, 2, 3) for t in tiers)
    pl_player = find_player_in_fpl(story["player"], fpl_data) is not None
    big_player = is_big_player(story["player"], fpl_data) or is_big_name_player(story["player"])
    big_club = is_big_club_name(story.get("to_club")) or is_big_club_name(story.get("from_club"))
    any_club = bool(story.get("to_club") or story.get("from_club") or
                    story.get("to_key") or story.get("from_key"))

    # Accept when there's a clear transfer signal AND any of:
    #   trusted source / FPL or big player / big or named club.
    if has_signal and (trusted_source or pl_player or big_player or big_club or any_club):
        return True, "ok_transfer_signal"
    # Last resort: an FPL/big player named by a trusted source, even if the
    # signal keyword set didn't match (e.g. unusual phrasing).
    if trusted_source and (pl_player or big_player):
        return True, "ok_trusted_player"
    return False, "not_clearly_transfer"

def classify_post(story, sources):
    """Return the post MODE: 'confirmed' (official/strong) or 'rumour'
    (reported by a journalist). Per spec, a single trusted reporter is enough
    to post — we just label it 'reported' rather than 'confirmed'."""
    if story.get("fpl_official"):
        return "confirmed"   # first-party FPL data is confirmed by definition
    if story.get("collapsed"): return "rumour"
    tiers = [source_tier(s) for s in sources]
    has_official = 1 in tiers
    has_elite = any(t == 2 for t in tiers)
    has_media = any(t == 3 for t in tiers)
    tl = (story.get("body", "") + " " + (story.get("headline", "") or "")).lower()
    strong_words = story.get("stage", 1) >= 4 or any(
        re.search(r'\b' + re.escape(w) + r'\b', tl) for w in STRONG_OFFICIAL)

    if story["event"] in ("injury", "suspension"):
        # official or trusted reporter ⇒ confirmed; reputable media ⇒ reported
        if has_official or has_elite: return "confirmed"
        if has_media: return "rumour"
        return "rumour"

    video_only = story.get("from_video") and not has_official
    if (has_official or (strong_words and (has_official or has_elite))) and not video_only:
        return "confirmed"
    # A single trusted journalist or reputable outlet ⇒ post as reported.
    if has_elite or has_media:
        return "rumour"
    return None

def validate_story(story, fpl_data=None):
    ev = story.get("event")
    player = (story.get("player") or "").strip()
    if not player: return False, "missing_player"
    _ptokens = [t for t in re.split(r"[\s\-']+", player) if t]
    _plow = player.lower()
    # The transfer SUBJECT being an exact manager surname is still an error,
    # but a single-name player (Tonali, Casemiro, Cunha, Ederson) is FINE.
    if ev != "manager" and _plow in MANAGER_SURNAMES:
        return False, "player_is_manager_name"
    if ev == "manager" and len(_ptokens) < 2:
        return False, "manager_name_single_token"
    # RELAXED: single-token player names are allowed for all event types.
    # Only reject an obviously truncated youth-team fragment ("... Under" / "U21").
    if re.search(r"\b(under|u\d{1,2}|u-\d{1,2})$", _plow):
        return False, "player_name_truncated_fragment"

    PLACEHOLDERS = ("player name", "example", "xxx", "tbd", "to follow",
                    "lorem", "duration & details", "updated heading",
                    "from club", "to club")
    blob = " ".join(str(story.get(k, "") or "") for k in
                    ("player", "headline", "body", "from_club", "to_club", "fee",
                     "contract", "conditional", "diagnosis", "expected_return")).lower()
    for ph in PLACEHOLDERS:
        if ph in blob: return False, f"placeholder_text:{ph!r}"
    if looks_like_club(player): return False, "player_is_club"
    # Scrub raw source artefacts (RT/@handles/URLs) in place rather than reject.
    if re.search(r'\bRT\s+@|@\w+|https?://', story.get("body", "")):
        cleaned_body = _clean_source_text(story.get("body", ""))
        if len(cleaned_body.split()) < 4:
            cleaned_body = _summarise(
                story.get("player"), ev,
                story.get("from_key"), story.get("to_key"),
                story.get("stage", 1), story.get("collapsed"))
        story["body"] = cleaned_body
        if re.search(r'\bRT\s+@|@\w+|https?://', story.get("body", "")):
            return False, "raw_source_text_in_body"
    if player_already_at_club(story, fpl_data): return False, "already_at_destination"
    if ev in ("transfer", "loan", "loan_option"):
        fk = story.get("from_key"); tk = story.get("to_key")
        fc = (story.get("from_club") or "").strip().lower()
        tc = (story.get("to_club") or "").strip().lower()
        # If both clubs are named and identical, that's a genuine error.
        if (fk and tk and fk == tk) or (fc and tc and fc == tc):
            return False, "from_equals_to"
        # RELAXED: a transfer with NO club at all is still postable (e.g.
        # "Tonali to Spurs" where Spurs failed to resolve, or "bid expected").
        # We no longer reject on missing clubs or unconfident direction.
        leak = (story.get("body", "") + " " + story.get("headline", "")).lower()
        if re.search(r'\b(head coach|sacked|appointed as manager|hamstring|ruled out for)\b', leak):
            return False, "event_data_mismatch"
    if ev == "manager" and not (story.get("to_key") or story.get("to_club") or story.get("headline")):
        return False, "manager_no_club"
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

def _pretty_club_name(story, which):
    """Human-readable club name from key or club field. which = 'to' or 'from'."""
    key = story.get(f"{which}_key")
    club = story.get(f"{which}_club")
    name = club or (key or "").replace("_", " ")
    name = name.strip()
    if not name:
        return None
    return name.title() if name.islower() else name


def build_tweet_body(story, sources, mode) -> str:
    """Build a short, clear, informative caption.

    Format per type:
      🔴 INJURY: Sousa (Aston Villa)
      Hamstring injury – 25% chance of playing
      <hashtags>

      🔵 REPORTED: Tonali → Tottenham
      Fresh bid expected as talks continue
      <hashtags>
    """
    label = status_label(story, mode)
    tiers = [source_tier(s) for s in (sources or [])]
    is_official = 1 in tiers
    ev = story.get("event", "transfer")
    player = story.get("player") or story.get("headline") or "Update"

    # Resolve the label word and a leading emoji.
    if mode == "rumour" and label in ("RUMOUR", None):
        tag, emoji = "REPORTED", "🔵"
    elif label == "OFFICIAL" or is_official:
        tag, emoji = (label or "OFFICIAL"), "✅"
    else:
        tag, emoji = (label or "UPDATE"), "🔵"

    to_club = _pretty_club_name(story, "to")
    from_club = _pretty_club_name(story, "from")

    # ── Line 1: the headline, shaped per event type ──────────────────────
    if ev in ("injury", "suspension"):
        emoji = "🔴" if ev == "injury" else "🟥"
        club = from_club or to_club
        head = f"{player} ({club})" if club else player
        first_line = f"{emoji} {tag}: {head}"
    elif ev in ("transfer", "loan", "loan_option"):
        if to_club and from_club:
            head = f"{player}: {from_club} → {to_club}"
        elif to_club:
            head = f"{player} → {to_club}"
        elif from_club:
            head = f"{player} ({from_club})"
        else:
            head = player
        first_line = f"{emoji} {tag}: {head}"
    elif ev in ("renewal", "stay"):
        club = from_club or to_club
        head = f"{player} ({club})" if club else player
        first_line = f"📝 {tag}: {head}"
    elif ev == "manager":
        head = f"{player} → {to_club}" if to_club else player
        first_line = f"📋 {tag}: {head}"
    else:
        first_line = f"{emoji} {tag}: {player}"

    # ── Line 2: the key detail (this is what was missing before) ─────────
    detail = None
    if ev in ("injury", "suspension"):
        # FPL gives us the real text: "Hamstring injury - 25% chance of playing"
        detail = story.get("diagnosis") or story.get("expected_return")
        if detail:
            detail = detail.replace(" - ", " – ")
    else:
        bits = []
        if story.get("fee"): bits.append(story["fee"])
        if story.get("contract"): bits.append(story["contract"])
        if story.get("conditional"): bits.append(story["conditional"])
        if bits:
            detail = "  •  ".join(bits)
        else:
            # fall back to the one-sentence summary body if it adds info
            b = (story.get("body") or "").strip()
            if b and b.lower() not in (player.lower(), first_line.lower()):
                detail = b

    # Assemble, keeping it compact. trim_for_twitter() enforces the hard limit.
    lines = [first_line]
    if detail:
        lines.append(detail)
    body = "\n".join(lines) + "\n\n" + build_hashtags(story)
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

def _draw_wordmark(draw, xy):
    x, y = xy
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

def create_transfer_image(story, sources, filename, collapsed=False):
    W, H = 1380, 776
    fpl = fetch_fpl_data()
    player_el = find_player_in_fpl(story.get("player"), fpl)
    player_name = (player_el["web_name"] if player_el else story.get("player")) or "PLAYER"
    to_key = story.get("to_key")
    from_key = story.get("from_key")

    NAVY = (11, 18, 32)
    GOLD = (212, 175, 55)
    accent = (120, 30, 34) if collapsed else (30, 55, 110)

    img = Image.new("RGB", (W, H), NAVY)
    sheen = Image.new("L", (1, H), 0)
    for y in range(H): sheen.putpixel((0, y), int(30 * (1 - abs(y - H / 2) / (H / 2))))
    img.paste(Image.new("RGB", (W, H), (28, 40, 70)), (0, 0), sheen.resize((W, H)))
    _draw_diagonal_accents(img, accent, GOLD)
    draw = ImageDraw.Draw(img, "RGBA")

    TEXT_X = 70
    _draw_wordmark(draw, (TEXT_X, 48))

    ev = story.get("event", "transfer")
    if collapsed:
        label = "DEAL COLLAPSED"
        badge_color = (120, 30, 34)
    elif ev == "manager":
        label = "MANAGER NEWS"
        badge_color = (30, 80, 160)
    elif ev in ("loan", "loan_option"):
        label = "LOAN UPDATE"
        badge_color = (180, 100, 0)
    elif ev in ("renewal", "stay"):
        label = "CONTRACT UPDATE"
        badge_color = (30, 130, 80)
    else:
        label = "TRANSFER UPDATE"
        badge_color = (227, 30, 36)
    lf = get_premium_font(44, "Bold")
    draw.rounded_rectangle([TEXT_X, 116, TEXT_X + draw.textlength(label, font=lf) + 40, 178],
                           radius=10, fill=badge_color)
    _draw_text_shadow(draw, (TEXT_X + 20, 124), label, lf, (255, 255, 255), offset=1)

    name_up = player_name.upper()
    TEXT_MAX_W = 780
    nsize = 104
    nf = get_premium_font(nsize, "Black")
    while draw.textlength(name_up, font=nf) > TEXT_MAX_W and nsize > 56:
        nsize -= 3
        nf = get_premium_font(nsize, "Black")
    name_y = 200
    _draw_text_shadow(draw, (TEXT_X, name_y), name_up, nf, (255, 255, 255), offset=3)
    nb = draw.textbbox((0, 0), name_up, font=nf)
    name_bottom = name_y + (nb[3] - nb[1]) + 24

    crest_font = get_premium_font(46, "Bold")
    row_label_font = get_premium_font(38, "Bold")
    CREST = 104
    y = name_bottom

    def _row(tag, club_key, club_text, color):
        nonlocal y
        crest = _load_crest(club_key, CREST)
        x = TEXT_X
        _draw_text_shadow(draw, (x, y + (CREST - 34) // 2), tag, row_label_font, (170, 180, 200))
        x += 150
        if crest is not None:
            img.paste(crest, (x, y + (CREST - crest.height) // 2), crest)
        else:
            # Generic clean emblem outline for non-PL teams so spacing stays perfectly aligned
            cy = y + (CREST - 70) // 2
            draw.rounded_rectangle([x + 15, cy, x + 85, cy + 70], radius=12, outline=(255, 255, 255, 40), width=3)
            draw.ellipse([x + 35, cy + 20, x + 65, cy + 50], fill=(84, 224, 124, 60))
        x += CREST + 20
        name = (club_text or (club_key or "").replace("_", " ")).upper()
        _draw_text_shadow(draw, (x, y + (CREST - 44) // 2), name, crest_font, color)
        y += CREST + 22

    if from_key or story.get("from_club"): _row("FROM:", from_key, story.get("from_club"), (225, 225, 225))
    if to_key or story.get("to_club"): _row("TO:", to_key, story.get("to_club"), (255, 255, 255))

    detail = build_detail_line(story)
    if detail:
        _safe_emoji_text(img, (TEXT_X, y + 8), detail.upper()[:60],
                         get_premium_font(28, "Bold"), (160, 255, 120))

    # PORTRAIT / RIGHT-SIDE VISUAL — always renders something on-brand.
    # 1. If the player resolves in FPL, the official headshot is keyed on the
    #    player code, so it is correct by definition — show it. (Relaxed: no
    #    longer requires the from/to club to match, which was suppressing many
    #    valid photos and leaving cards empty.)
    # 2. Else a big club crest (destination, then origin).
    # 3. Else the glowing V emblem.
    LEGENDS = {"harry kane": "78830"}
    legend_pid = LEGENDS.get(player_name.lower())
    drew_player = False
    if player_el or legend_pid:
        pid = legend_pid or (player_el.get("code") if player_el else None)
        if pid:
            pp = Path(f"players/{pid}.png")
            if not pp.exists():
                _download_asset(f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{pid}.png", pp)
            portrait = _safe_open_rgba(pp)
            if portrait is not None:
                portrait = _fit_contain(portrait, 520, 600)
                img.paste(portrait, (W - portrait.width - 60, H - portrait.height - 100), portrait)
                drew_player = True
    if not drew_player:
        _draw_right_visual_fallback(img, draw, W, H, story)

    draw.rectangle([0, H - 90, W, H - 12], fill=(20, 24, 33))
    draw.rectangle([0, H - 12, W, H], fill=accent)
    src = " · ".join(f"@{s}" for s in sources[:2])
    bar = f"Source: {src}  |  {CHANNEL_HANDLE}"
    bsize = 34
    bf = get_premium_font(bsize, "Bold")
    while bsize > 24 and draw.textlength(bar, font=bf) > (W - 120):
        bsize -= 1
        bf = get_premium_font(bsize, "Bold")
    bbox = draw.textbbox((0, 0), bar, font=bf)
    by = (H - 90) + (78 - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((60, by), bar, font=bf, fill=(190, 200, 220))
    img.save(filename)

def create_injury_image(story, sources, filename):
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

def _wrap_text_to_width(draw, text, font, max_width, max_lines=2):
    """Wrap text into up to max_lines that each fit max_width. The final line is
    ellipsised if the text is too long. Returns a list of line strings."""
    if not text:
        return []
    words = str(text).split()
    lines = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_width or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                break
    if len(lines) < max_lines and cur and (not lines or lines[-1] != cur):
        lines.append(cur)
    # If we ran out of room, ellipsise the last visible line.
    if len(lines) == max_lines:
        joined_words = " ".join(words)
        shown = " ".join(lines)
        if draw.textlength(shown, font=font) < draw.textlength(joined_words, font=font):
            last = lines[-1]
            while last and draw.textlength(last + "…", font=font) > max_width:
                last = last[:-1]
            lines[-1] = (last.rstrip() + "…") if last else "…"
    return lines[:max_lines]


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
    accent = (120, 30, 34) if collapsed else (30, 55, 110)
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

    # team crest at the TOP of the image area (kept big + clear).
    # For INJURY/SUSPENSION cards we deliberately show NO club crest — instead a
    # red medical cross — so it reads instantly as an availability update.
    is_injury_card = ev in ("injury", "suspension")
    if is_injury_card:
        # Red medical cross centred at the top of the panel. Arms must be clearly
        # longer than they are thick, or the two bars merge into a square.
        cross_cx = fcx
        cross_cy = FY0 + 70
        arm = 46      # half-length of each arm (long)
        thick = 16    # half-thickness of each bar (thin)
        red = (227, 30, 36)
        draw.rounded_rectangle(
            [cross_cx - thick, cross_cy - arm, cross_cx + thick, cross_cy + arm],
            radius=7, fill=red)
        draw.rounded_rectangle(
            [cross_cx - arm, cross_cy - thick, cross_cx + arm, cross_cy + thick],
            radius=7, fill=red)
        crest_bottom = cross_cy + arm
    else:
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
    if player_el or legend_pid:
        pid = legend_pid or (player_el.get("code") if player_el else None)
        if pid:
            pp = Path(f"players/{pid}.png")
            if not pp.exists():
                _download_asset(f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{pid}.png", pp)
            photo = _safe_open_rgba(pp)
    if photo is None and story.get("media_url"):
        tp = Path(f"players/tweet_{story.get('id')}.jpg")
        if not tp.exists():
            _download_asset(story["media_url"], tp)
        photo = _safe_open_rgba(tp)

    if photo is not None:
        # COVER-fit: fill the box edge-to-edge, center-crop with a slight upward
        # bias so faces are kept; round the corners to match the panel. This
        # makes the image big, sharp, centered and never tiny/letterboxed.
        filled = ImageOps.fit(photo, (PB_W, pb_h), Image.Resampling.LANCZOS,
                              centering=(0.5, 0.38)).convert("RGBA")
        round_mask = Image.new("L", (PB_W, pb_h), 0)
        ImageDraw.Draw(round_mask).rounded_rectangle([0, 0, PB_W, pb_h], radius=20, fill=255)
        # respect the photo's own transparency (FPL cut-outs) so the panel shows
        # through instead of a black box; opaque photos stay fully visible.
        paste_mask = ImageChops.multiply(filled.getchannel("A"), round_mask)
        img.paste(filled, (pbx0, pby0), paste_mask)
    else:
        # guaranteed silhouette IMAGE filling the same box (never empty)
        _draw_player_silhouette(img, draw, fcx, pby0, PB_W, pb_h)

    # ---------- LEFT: wordmark, label, name, info ----------
    LX = 70
    _draw_wordmark(draw, (LX, 54))
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
    # Hard truncate if still too wide at the minimum size (prevents overlap).
    if draw.textlength(name_up, font=nf) > NAME_MAX_W:
        while name_up and draw.textlength(name_up + "…", font=nf) > NAME_MAX_W:
            name_up = name_up[:-1]
        name_up = name_up.rstrip() + "…"
    _draw_text_shadow(draw, (LX, name_y), name_up, nf, (255, 255, 255), offset=3)
    nb = draw.textbbox((0, 0), name_up, font=nf)
    y = name_y + (nb[3] - nb[1]) + 34
    info_f = get_premium_font(38, "Bold"); sub_f = get_premium_font(27, "Bold")
    # All info text must stay LEFT of the photo panel (FX0) with a 40px gutter.
    INFO_MAX_X = FX0 - 40
    def _info_row(label, value, col=(255, 255, 255)):
        nonlocal y
        if not value: return
        _draw_text_shadow(draw, (LX, y + 4), label, sub_f, (150, 165, 195))
        lw = draw.textlength(label + "   ", font=sub_f)
        value_max_w = INFO_MAX_X - (LX + lw)
        # Wrap the value to the remaining width (up to 2 lines) so long
        # diagnoses/club names never run into the photo.
        vlines = _wrap_text_to_width(draw, str(value), info_f, value_max_w, max_lines=2)
        for i, vl in enumerate(vlines):
            _draw_text_shadow(draw, (LX + lw, y), vl, info_f, col)
            if i < len(vlines) - 1:
                y += 46
        y += 60
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

    # ---------- source bar (date left in bold green, source right) ----------
    draw.rectangle([0, H - 90, W, H - 12], fill=(20, 24, 33))
    draw.rectangle([0, H - 12, W, H], fill=accent)
    # Date — bottom-left, bold green.
    date_str = datetime.now(timezone.utc).strftime("%d %b %Y").upper()
    date_f = get_premium_font(30, "Black")
    db = draw.textbbox((0, 0), date_str, font=date_f)
    dy = (H - 90) + (78 - (db[3] - db[1])) // 2 - db[1]
    _draw_text_shadow(draw, (60, dy), date_str, date_f, (84, 224, 124), offset=1)
    date_w = draw.textlength(date_str, font=date_f)
    # Source — to the RIGHT of the date, right-aligned to the card edge.
    src = " · ".join(f"@{s}" for s in sources[:2])
    bar = f"Source: {src}  |  {CHANNEL_HANDLE}"
    bsize = 30; bf = get_premium_font(bsize, "Bold")
    src_left = 60 + date_w + 40
    while bsize > 22 and draw.textlength(bar, font=bf) > (W - src_left - 40):
        bsize -= 1; bf = get_premium_font(bsize, "Bold")
    bb = draw.textbbox((0, 0), bar, font=bf)
    by = (H - 90) + (78 - (bb[3] - bb[1])) // 2 - bb[1]
    bx = W - 40 - draw.textlength(bar, font=bf)
    draw.text((max(src_left, bx), by), bar, font=bf, fill=(190, 200, 220))
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
    # Advance FPL posted-state ONLY now that the story is truly out. Cap-blocked
    # FPL stories never reach here, so they regenerate and retry next run.
    if item.get("fpl_official"):
        try:
            commit_posted_fpl(item)
        except Exception as e:
            print(f"  [FPL] could not commit posted-state: {e}")
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
                # Nitter prepends/appends an author byline like "paul joyce (@_pauljoyce)".
                # It can appear at the FRONT or the END, so strip any line that is just
                # "<name> (@handle)", then drop any leftover bare @handles and URLs that
                # would otherwise trip the raw-source-text validator.
                text = re.sub(r'(?m)^\s*[^\n]*\(@\w+\)\s*$', '', text)
                text = re.sub(r'^\s*Updated:\s*\S+', '', text)
                text = re.sub(r'@\w+', '', text)
                text = re.sub(r'https?://\S+', '', text)
                text = re.sub(r'\n{2,}', '\n', text).strip()
                
                # Extract image from Nitter HTML
                media_url = None
                img_match = re.search(r'<img[^>]+src="([^">]+)"', desc_text)
                if img_match:
                    media_url = img_match.group(1)
                    if media_url.startswith("/"):
                        media_url = f"{inst}{media_url}"
                
                if tid and text: out.append({"id": tid, "text": text, "media_url": media_url})
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
                
                # Safely extract image from Twikit
                media_url = None
                if hasattr(t, "media") and t.media:
                    for m in t.media:
                        m_type = getattr(m, "type", None) or (m.get("type") if isinstance(m, dict) else None)
                        if m_type == "photo":
                            media_url = getattr(m, "media_url_https", None) or (m.get("media_url_https") if isinstance(m, dict) else None)
                            if media_url: break
                            
                if tid and txt: out.append({"id": tid, "text": txt, "media_url": media_url})
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
    # Per-spec decision log: every tweet's outcome with full reasoning.
    decisions = []
    rejected_items = []
    dup_count = 0
    def _log(outcome, source, story, text, reason):
        rec = {
            "outcome": outcome,                      # accepted | rejected | duplicate
            "source": source,
            "player": (story or {}).get("player"),
            "club": (story or {}).get("to_club") or (story or {}).get("from_club")
                    or (story or {}).get("to_key") or (story or {}).get("from_key"),
            "event": (story or {}).get("event"),
            "fresh": not (story or {}).get("historical", False),
            "reason": reason,
            "text": (text or "")[:200],
        }
        decisions.append(rec)
        if outcome == "rejected":
            rejected_items.append(rec)
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
            seen += 1
            if tid in data["extracted"]:
                story = dict(data["extracted"][tid])
                # Re-sanitise the body on every reuse: older cache entries were
                # stored before byline/handle stripping existed, and a stale
                # poisoned body would otherwise fail validation forever.
                story["body"] = _clean_source_text(story.get("body") or "")
                if len(story["body"].split()) < 4:
                    story["body"] = _summarise(
                        story.get("player"), story.get("event"),
                        story.get("from_key"), story.get("to_key"),
                        story.get("stage", 1), story.get("collapsed"))
                data["extracted"][tid] = dict(story)
            else:
                story = build_story(text, fpl)
                story["media_url"] = t.get("media_url")
                data["extracted"][tid] = dict(story)
            safe, why = passes_safety_gate(story, text, fpl, sources=[username])
            if not safe:
                skipped += 1
                _log("rejected", username, story, text, why)
                print(f"   skip ({why}): {text[:70]!r}")
                continue
            valid, vwhy = validate_story(story, fpl)
            if not valid:
                skipped += 1
                _log("rejected", username, story, text, vwhy)
                print(f"   invalid ({vwhy}): {text[:70]!r}")
                continue
            anchor = story.get("to_key") or story.get("from_key") or "unknown"
            key = reconcile_key(story["player"], anchor, story["event"],
                                story_map, data.get("stories", {}), data.get("pending", {}))
            ok, reason = should_post(data, key, story["stage"], story["collapsed"],
                                     new_sources=[username])
            if not ok:
                _log("rejected", username, story, text, reason)
                print(f"   skip ({reason}): {key}")
                continue
            _log("accepted", username, story, text, reason)
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

    # ── SPEC-REQUIRED DECISION SUMMARY + DEBUG FILE ──────────────────────
    n_accepted = sum(1 for d in decisions if d["outcome"] == "accepted")
    n_rejected = sum(1 for d in decisions if d["outcome"] == "rejected")
    print(f"  [tweets_seen={seen}]")
    print(f"  [candidates={len(story_map)}]")
    print(f"  [rejected={n_rejected}]")
    print(f"  [duplicates={dup_count}]")
    if not story_map:
        # Surface the top rejection reasons so it's obvious WHY nothing posted.
        from collections import Counter
        reasons = Counter(d["reason"] for d in rejected_items)
        if reasons:
            print("  [WHY-NOTHING] top rejection reasons: " +
                  ", ".join(f"{r}×{c}" for r, c in reasons.most_common(6)))
    try:
        dbg = Path("queue/debug")
        dbg.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        with open(dbg / f"decisions_{stamp}.json", "w") as f:
            json.dump({
                "at": datetime.now(timezone.utc).isoformat(),
                "tweets_seen": seen, "candidates": len(story_map),
                "rejected": n_rejected, "duplicates": dup_count,
                "decisions": decisions,
            }, f, indent=2, default=str)
        # Always (re)write the latest rejected items for quick inspection.
        with open(dbg / "rejected_latest.json", "w") as f:
            json.dump(rejected_items, f, indent=2, default=str)
    except Exception as e:
        print(f"  [DEBUG] could not write debug file: {e}")

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
MAX_POSTS_PER_RUN = SETTINGS["max_posts_per_run"]
MAX_POSTS_PER_HOUR = SETTINGS["max_posts_per_hour"]
POST_JITTER_RANGE_S = tuple(SETTINGS["post_jitter_seconds"])

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
                      "count": 0, "limit": SETTINGS["daily_limit"]},
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
    print(f"\n[BOT] Run — {datetime.now(timezone.utc).isoformat()} "
          f"(LLM={'Gemini' if _GEMINI_OK else 'off/fallback'}, mode={mode_str})")
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

    # ── PRIMARY SOURCE: FPL injuries/suspensions (structured, official, free)
    fpl_stories = []
    if SETTINGS.get("use_fpl_injuries", True):
        try:
            # Housekeeping: drop now-fit players so a future re-injury posts.
            clear_fit_players(fpl)
            raw_fpl = fpl_injury_stories(
                fpl,
                min_news_len=SETTINGS.get("fpl_min_news_len", 4),
                recent_days=SETTINGS.get("fpl_recent_days", 3))
            for st in raw_fpl:
                # Run each through validation so a malformed entry can't post.
                valid, why = validate_story(st, fpl)
                if not valid:
                    print(f"  [FPL] skip ({why}): {st.get('player')!r}")
                    continue
                dup, dreason = is_duplicate_content(st, data)
                if dup:
                    continue
                st["mode"] = "confirmed"
                st["key"] = build_story_key(st["player"],
                                            st.get("from_key") or "unknown", st["event"])
                fpl_stories.append(st)
            if fpl_stories:
                print(f"  [FPL] {len(fpl_stories)} FPL injury/suspension stor(ies) queued.")
        except Exception as e:
            print(f"  [FPL] injury source failed (non-fatal): {e}")

    # ── SECONDARY SOURCE: X transfer scrape (fallback-layered) ───────────
    queue = await scrape(data, read_client)
    # FPL stories go FIRST — they're confirmed and highest-value.
    queue = fpl_stories + queue
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

    # ── POLICY: REPORTED + low-confidence handling (config-driven) ───────
    # post_reported=False → only CONFIRMED (official/FPL) stories post.
    if not SETTINGS.get("post_reported", True):
        before = len(postable)
        postable = [d for d in postable if d.get("mode") == "confirmed"]
        if before != len(postable):
            print(f"[BOT] post_reported=false → held {before - len(postable)} "
                  f"REPORTED story(ies); CONFIRMED only.")
    # low_confidence_policy: 'skip' (default) drops anything under the threshold
    # that isn't first-party FPL/official.
    if SETTINGS.get("low_confidence_policy", "skip") == "skip":
        thr = SETTINGS.get("confidence_threshold", 0.40)
        kept = []
        for d in postable:
            if d.get("fpl_official") or d.get("mode") == "confirmed":
                kept.append(d); continue
            if d.get("confidence", 0.5) >= thr:
                kept.append(d)
        postable = kept

    # Re-attach any stories DEFERRED last run because the cap was hit. This is
    # the "do not miss any news" guarantee — cap-blocked stories wait, they are
    # not dropped. They keep their original mode/stage and are de-duped normally.
    deferred = data.get("deferred", [])
    if deferred:
        print(f"[BOT] {len(deferred)} story(ies) carried over from previous capped run(s).")
        postable = deferred + postable
        data["deferred"] = []

    if not postable:
        print("[BOT] No postable stories this run.")
        return

    # ── PRIORITY ORDERING ────────────────────────────────────────────────
    # 1) CONFIRMED (official / FPL) before REPORTED (journalist).
    # 2) Within a mode, by event importance (injury/suspension/transfer…).
    # 3) Higher stage first (more progressed news).
    def _priority(s):
        confirmed = 0 if s.get("mode") == "confirmed" else 1
        return (
            confirmed,
            EVENT_PRIORITY.get(s.get("event"), 5),
            0 if s.get("collapsed") else 1,
            -int(s.get("stage", 1)),
        )
    postable.sort(key=_priority)

    posted_last_hour = _recent_post_count(data, 3600)
    if posted_last_hour >= MAX_POSTS_PER_HOUR:
        # Cap hit for the hour — DEFER everything to next run, lose nothing.
        data["deferred"] = postable
        save_data(data)
        print(f"[BOT] Per-hour cap reached ({posted_last_hour}/{MAX_POSTS_PER_HOUR}) "
              f"— {len(postable)} story(ies) deferred to next run (none lost).")
        return

    try:
        post_client = Client("en-US")
        post_client.set_cookies({"auth_token": X_POST_AUTH_TOKEN, "ct0": X_POST_CT0_TOKEN})
    except Exception as e:
        print(f"[BOT] could not init posting client: {e}")
        data["deferred"] = postable           # nothing posted → defer all
        save_data(data)
        return

    remaining_today = data["daily"]["limit"] - data["daily"]["count"]
    remaining_hour = MAX_POSTS_PER_HOUR - posted_last_hour
    allow = max(0, min(MAX_POSTS_PER_RUN, remaining_today, remaining_hour))
    batch = postable[:allow]
    overflow = postable[allow:]               # everything we can't post now
    print(f"[BOT] Posting {len(batch)} item(s) (run cap {MAX_POSTS_PER_RUN}, "
          f"{remaining_today} left today, {remaining_hour} left this hour). "
          f"{len(overflow)} will carry over.")

    posted = 0
    posts_by_type = {"transfer": 0, "injury": 0, "suspension": 0,
                     "loan": 0, "renewal": 0, "manager": 0, "other": 0}
    for i, item in enumerate(batch):
        if not check_daily_limit(data):
            print("[BOT] Hit daily limit mid-batch — stopping, remainder deferred.")
            overflow = batch[i:] + overflow
            break

        jitter = random.randint(*POST_JITTER_RANGE_S)
        print(f"  [PACING] waiting {jitter}s before posting (anti-spam jitter)…")
        await asyncio.sleep(jitter)

        try:
            if await post_item(post_client, item, data):
                posted += 1
                ev = item.get("event", "other")
                key = ev if ev in posts_by_type else "other"
                posts_by_type[key] = posts_by_type.get(key, 0) + 1
        except Exception as e:
            if item.get("id") and item["id"] in data["posted_ids"]:
                print(f"  [ERROR] {item['key']}: {e} — already recorded, NOT retrying")
            else:
                print(f"  [ERROR] {item['key']} (attempt 1): {e} — retrying once")
                try:
                    await asyncio.sleep(10)
                    if await post_item(post_client, item, data):
                        posted += 1
                        ev = item.get("event", "other")
                        key = ev if ev in posts_by_type else "other"
                        posts_by_type[key] = posts_by_type.get(key, 0) + 1
                except Exception as e2:
                    print(f"  [ERROR] {item['key']} (attempt 2): {e2} — deferring")
                    overflow.append(item)     # failed → carry over, don't lose it

    # ── PERSIST CARRY-OVER (nothing is missed) ───────────────────────────
    # Deduplicate the overflow against itself by stage key so we don't store a
    # story twice, then save it for the next run.
    seen_keys = set()
    clean_overflow = []
    for s in overflow:
        sk = stage_post_key(s)
        if sk in seen_keys:
            continue
        seen_keys.add(sk)
        clean_overflow.append(s)
    data["deferred"] = clean_overflow

    # ── VOLUME / TYPE LOGGING ────────────────────────────────────────────
    data.setdefault("posts_by_type_today", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if data.get("posts_by_type_date") != today:
        data["posts_by_type_today"] = {}
        data["posts_by_type_date"] = today
    for k, v in posts_by_type.items():
        if v:
            data["posts_by_type_today"][k] = data["posts_by_type_today"].get(k, 0) + v
    save_data(data)

    print(f"\n[BOT] {posted} post(s) published this run.")
    print(f"[VOLUME] total_posts_today = {data['daily']['count']}/{data['daily']['limit']}")
    bt = data["posts_by_type_today"]
    print(f"[VOLUME] posts_by_type today: " +
          (", ".join(f"{k}={v}" for k, v in bt.items()) if bt else "none"))
    if clean_overflow:
        print(f"[VOLUME] {len(clean_overflow)} story(ies) deferred to next run "
              f"(carried over, not lost).")

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
