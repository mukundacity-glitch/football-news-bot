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
# ── SECRETS ────────────────────────────────────────────────────────────────────
X_POST_AUTH_TOKEN = os.getenv("X_POST_AUTH_TOKEN")
X_POST_CT0_TOKEN = os.getenv("X_POST_CT0_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")
# ── PATHS ──────────────────────────────────────────────────────────────────────
POSTED_FILE = Path("posted_news.json")
PENDING_DIR = Path("queue/pending")
POSTED_DIR = Path("queue/posted")
PENDING_DIR.mkdir(parents=True, exist_ok=True)
POSTED_DIR.mkdir(parents=True, exist_ok=True)
Path("logos").mkdir(parents=True, exist_ok=True)
Path("players").mkdir(parents=True, exist_ok=True)
# ── JOURNALISTS ────────────────────────────────────────────────────────────────
JOURNALISTS = [
    "FabrizioRomano", "David_Ornstein", "Plettigoal", "Santi_J_M",
    "sistoney67", "MatteoMoretto_", "AlfredoPedulla", "cfalk_news",
    "BenJacobs", "GianlucaDiMarzio",
]
NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]
# Tier-1 journalists: their "official/here we go" word is trusted to post alone.
# Anyone else needs a 2nd source to confirm before we post.
TOP_SOURCES = {"FabrizioRomano", "David_Ornstein"}
# Strongest "it's truly official" words — only these let a single top source post.
OFFICIAL_WORDS = ["here we go", "official", "confirmed", "completed", "medical",
                  "joins", "signs", "done deal", "sealed", "unveiled"]
# ── KEYWORDS ───────────────────────────────────────────────────────────────────
TRANSFER_KW = ["transfer", "sign", "deal", "fee", "bid", "move", "loan",
                "contract", "agree", "confirm", "medical", "official", "close",
                "interest", "talks", "negotiat", "personal terms", "done",
                "approach", "target", "want", "keen", "pursuit", "swap"]
INJURY_KW = ["injury", "injured", "ruled out", "scan", "hamstring", "knee",
                "muscle", "fracture", "surgery", "sidelined", "doubt",
                "concern", "knock", "fitness", "unavailable", "recovery"]
MANAGER_KW = ["sack", "appoint", "manager", "coach", "resign", "dismiss",
                "interim", "replace", "head coach", "taking over", "departure",
                "leave", "new manager", "managerial"]
COLLAPSE_KW = ["collapse", "collapsed", "fell through", "breaks down",
                "no deal", "deal off", "pulled out", "rejected", "refused",
                "failed", "cancelled", "called off", "walks away"]
# ── SAFETY: words that mean the tweet is about a STAFF/OFF-PITCH person, not a
#    playing transfer. If any appear we skip — this blocks scouts/directors like
#    Steve Nickson (head of recruitment) being posted as a "transfer".
STAFF_BLOCK_KW = [
    "head of recruitment", "recruitment", "sporting director", "director of football",
    "technical director", "chief scout", "scout", "scouting", "ceo", "chairman",
    "owner", "president", "board", "agent", "physio", "kit man", "analyst",
    "head of football", "transfer chief", "negotiator", "sd ", "dof",
]
# ── SAFETY: a STRONG signal that an actual player move is really happening.
#    Max-safety mode requires one of these before a transfer can be posted.
STRONG_TRANSFER_SIGNAL = [
    "here we go", "official", "confirmed", "completed", "complete", "signs",
    "signed", "joins", "joined", "medical", "done deal", "agreement", "agreed",
    "sign ", "signing", "bid accepted", "personal terms", "loan move", "permanent",
]
# ── STAGE KEYWORDS ─────────────────────────────────────────────────────────────
STAGE_KW = {
    "transfer": {
        1: ["interest", "talks", "keen", "want", "monitoring", "approach",
            "considering", "linked", "target", "pursuit", "looking at", "contact"],
        2: ["agreement", "agreed", "negotiating", "offer accepted", "advanced talks",
            "bid accepted", "close to", "personal terms", "verbal"],
        3: ["signs", "signed", "contract signed", "penned", "contract agreed",
            "contract completed", "deal signed"],
        4: ["official", "confirmed", "done deal", "completed", "medical",
            "transfer confirmed", "announced", "unveiled", "joins"],
    },
    "manager": {
        1: ["considering", "target", "candidate", "looking at", "search",
            "under pressure", "sack", "dismiss", "could leave"],
        2: ["talks", "negotiating", "in discussions", "approached", "contact",
            "interest", "close"],
        3: ["agreement", "agreed", "contract agreed", "terms agreed", "signed"],
        4: ["appointed", "confirmed", "officially", "unveiled", "announced",
            "takes charge", "new manager"],
    },
    "injury": {
        1: ["concern", "doubt", "knock", "worry", "picked up", "slight", "discomfort"],
        2: ["scan", "assessment", "diagnosis", "awaiting", "tests", "results", "examined"],
        3: ["ruled out", "weeks", "months", "surgery", "sidelined", "out until"],
        4: ["return", "back in training", "fit again", "cleared", "available", "recovered"],
    },
}
STAGE_LABELS = {
    "transfer": {0: "DEAL COLLAPSED", 1: "TRANSFER TALKS", 2: "AGREEMENT REACHED", 3: "CONTRACT SIGNED", 4: "TRANSFER CONFIRMED"},
    "manager": {0: "DEAL COLLAPSED", 1: "MANAGERIAL CHANGE", 2: "MANAGER TALKS", 3: "TERMS AGREED", 4: "OFFICIALLY APPOINTED"},
    "injury": {0: "INJURY UPDATE", 1: "INJURY CONCERN", 2: "SCAN AWAITED", 3: "RULED OUT", 4: "FIT TO RETURN"},
}
COUNTRY_HASHTAGS = {"england": "#England", "france": "#France", "spain": "#Spain", "germany": "#Germany", "italy": "#Italy"}
LEAGUE_HASHTAGS = {"premier league": ["#PremierLeague", "#PL"], "la liga": ["#LaLiga"], "serie a": ["#SerieA"], "bundesliga": ["#Bundesliga"]}
# FPL specific identifier text mappings for mapping club text string to official naming conventions
CLUB_NAME_MAP = {
    "arsenal": "Arsenal", "aston villa": "Aston_Villa", "bournemouth": "Bournemouth",
    "brentford": "Brentford", "brighton": "Brighton", "chelsea": "Chelsea",
    "crystal palace": "Crystal_Palace", "everton": "Everton", "fulham": "Fulham",
    "ipswich": "Ipswich", "leicester": "Leicester", "liverpool": "Liverpool",
    "man city": "Man_City", "manchester city": "Man_City", "man utd": "Man_Utd",
    "manchester united": "Man_Utd", "newcastle": "Newcastle", "forest": "Nottm_Forest",
    "nottingham forest": "Nottm_Forest", "southampton": "Southampton", "spurs": "Spurs",
    "tottenham": "Spurs", "west ham": "West_Ham", "wolves": "Wolves"
}
FPL_LOGO_IDS = {
    "Arsenal": "3", "Aston_Villa": "7", "Bournemouth": "91", "Brentford": "94",
    "Brighton": "36", "Chelsea": "8", "Crystal_Palace": "31", "Everton": "11",
    "Fulham": "54", "Ipswich": "40", "Leicester": "13", "Liverpool": "14",
    "Man_City": "43", "Man_Utd": "1", "Newcastle": "4", "Nottm_Forest": "17",
    "Southampton": "20", "Spurs": "6", "West_Ham": "21", "Wolves": "39"
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
# ── DATA LOADERS ───────────────────────────────────────────────────────────────
def load_data() -> dict:
    if POSTED_FILE.exists():
        with open(POSTED_FILE) as f:
            d = json.load(f)
    else:
        d = {"daily": {"date": "", "count": 0, "limit": 17}, "stories": {}, "posted_ids": []}
    # "pending" holds UNCONFIRMED stories waiting for a 2nd source before posting.
    d.setdefault("pending", {})
    return d
def save_data(data: dict):
    with open(POSTED_FILE, "w") as f:
        json.dump(data, f, indent=2)
def check_daily_limit(data: dict) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if data["daily"]["date"] != today:
        data["daily"] = {"date": today, "count": 0, "limit": 17}
    return data["daily"]["count"] < data["daily"]["limit"]
def increment_daily(data: dict):
    data["daily"]["count"] += 1
# ── EXTRACTION ─────────────────────────────────────────────────────────────────
SKIP_WORDS = {
    "Premier", "League", "Serie", "Bundesliga", "Ligue", "Champions",
    "Europa", "Transfer", "Breaking", "Done", "Deal", "Here", "Medical",
    "Exclusive", "Source", "Official", "Update", "News", "Today", "More",
    "Just", "Now", "Final", "After", "Club", "Move", "This", "That",
    "Real", "Madrid", "Bayern", "Munich", "Inter", "Milan", "Juventus",
    "Paris", "Saint", "Germain", "Sporting", "Porto", "Benfica", "Ajax"
}
# ── ROBUST CLUB MATCHING ─────────────────────────────────────────────────────
# Match ONLY full, real club names with word boundaries. This kills the old
# substring bug where 3-letter keys like "che"/"eve"/"val" matched inside
# words such as "Valverde", "reached", or "Manchester".
# Built from CLUB_NAME_MAP (clean full names) plus a few common variants.
CLUB_ALIASES = {
    # phrase that may appear in a tweet  ->  official FPL key
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
# longest aliases first so "manchester united" matches before "man"
_SORTED_ALIASES = sorted(CLUB_ALIASES.keys(), key=len, reverse=True)

def extract_clubs(text: str, club_hashtags: dict = None) -> list:
    """Return official club keys (e.g. 'Man_Utd') found as whole words/phrases,
    in the order they appear. club_hashtags is accepted but ignored (legacy)."""
    tl = text.lower()
    found = []
    used_spans = []
    for alias in _SORTED_ALIASES:
        for m in re.finditer(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', tl):
            span = m.span()
            # skip if this overlaps a longer alias we already matched
            if any(s[0] <= span[0] < s[1] or s[0] < span[1] <= s[1] for s in used_spans):
                continue
            official = CLUB_ALIASES[alias]
            if official not in found:
                found.append((span[0], official))
                used_spans.append(span)
    found.sort(key=lambda x: x[0])           # preserve appearance order
    return [c for _, c in found]

def extract_transfer_clubs(text: str) -> tuple:
    """Work out the (FROM, TO) clubs of a move as official keys.
    Heuristics, best-effort:
      - 'from X'            -> X is the FROM (selling) club
      - 'X sign/signs ...'  -> X is the TO (buying) club
      - 'joins/to X'        -> X is the TO club
    Falls back to order of appearance. Returns (from_key_or_None, to_key_or_None)."""
    tl = text.lower()
    clubs_in_order = extract_clubs(text)
    if not clubs_in_order:
        return None, None

    from_club = None
    to_club = None

    # FROM: the club that appears right after the word "from"
    for alias in _SORTED_ALIASES:
        if re.search(r'\bfrom\b[^.]{0,40}?(?<![a-z])' + re.escape(alias) + r'(?![a-z])', tl):
            from_club = CLUB_ALIASES[alias]
            break

    # TO: club before "sign/signs/signing" OR after "joins/join/to/move to"
    for alias in _SORTED_ALIASES:
        a = re.escape(alias)
        if re.search(r'(?<![a-z])' + a + r'(?![a-z])[^.]{0,30}?\bsign', tl) or \
           re.search(r'\b(?:join|joins|joining|move to|moves to|to)\b[^.]{0,25}?(?<![a-z])' + a + r'(?![a-z])', tl):
            if CLUB_ALIASES[alias] != from_club:
                to_club = CLUB_ALIASES[alias]
                break

    # Fallbacks using appearance order
    remaining = [c for c in clubs_in_order if c not in (from_club, to_club)]
    if to_club is None:
        # last distinct club mentioned is usually the destination
        for c in reversed(clubs_in_order):
            if c != from_club:
                to_club = c
                break
    if from_club is None and remaining:
        # first club that isn't the destination
        for c in clubs_in_order:
            if c != to_club:
                from_club = c
                break

    return from_club, to_club


def extract_player(text: str, clubs: list = None) -> str:
    """First plausible person name that is NOT a club/skip word."""
    club_words = set()
    for off in (clubs or []):
        for w in off.replace("_", " ").lower().split():
            club_words.add(w)
    matches = re.findall(r'\b([A-Z][a-zà-ÿ]+(?:[-\' ][A-Z][a-zà-ÿ]+)*)\b', text)
    for m in matches:
        words = m.split()
        if any(w in SKIP_WORDS for w in words):
            continue
        if m.lower() in CLUB_ALIASES:                 # it's a club, not a player
            continue
        if any(w.lower() in club_words for w in words):
            continue
        if len(m) > 3:
            return m
    return None
def extract_fee(text: str) -> str:
    m = re.search(r'[€£\$][\d\.]+[Mm]?|[\d\.]+\s*[Mm]illion|[\d\.]+[Mm]\s*[€£\$]', text)
    if m: return m.group(0).strip().upper().replace("MILLION", "M")
    return None
def extract_contract(text: str) -> str:
    m = re.search(r'(\d)[- ]year|until\s+20(\d\d)|\b(\d)\s+years\b', text, re.I)
    if not m: return None
    if m.group(1): return f"{m.group(1)}-year deal"
    if m.group(2): return f"until 20{m.group(2)}"
    if m.group(3): return f"{m.group(3)}-year deal"
    return None
def extract_country(text: str) -> str:
    tl = text.lower()
    for country, tag in COUNTRY_HASHTAGS.items():
        if country in tl: return tag
    return None
def extract_league(text: str) -> list:
    tl = text.lower()
    tags = []
    for league, htags in LEAGUE_HASHTAGS.items():
        if league in tl: tags.extend(htags)
    return tags
def classify_type(text: str) -> str:
    tl = text.lower()
    scores = {
        "injury":   sum(1 for k in INJURY_KW  if k in tl),
        "manager":  sum(1 for k in MANAGER_KW if k in tl),
        "transfer": sum(1 for k in TRANSFER_KW if k in tl),
    }
    return max(scores, key=scores.get)
def is_collapse(text: str) -> bool:
    tl = text.lower()
    return any(k in tl for k in COLLAPSE_KW)
def get_stage(text: str, stype: str) -> int:
    tl = text.lower()
    kw = STAGE_KW.get(stype, STAGE_KW["transfer"])
    for stage in [4, 3, 2, 1]:
        if any(k in tl for k in kw[stage]): return stage
    return 1
def build_story_key(player: str, club: str, stype: str) -> str:
    p = (player or "unknown").lower().replace(" ", "_")
    c = (club   or "unknown").lower().replace(" ", "_")
    return f"{p}_{c}_{stype}"
def should_post(data: dict, key: str, new_stage: int, collapsed: bool) -> tuple[bool, str]:
    existing = data["stories"].get(key)
    if collapsed:
        if existing and existing["status"] == "active": return True, "collapse"
        return False, "already_collapsed"
    if not existing: return True, "new"
    if existing["status"] == "collapsed": return False, "story_collapsed"
    if new_stage <= existing["stage"]: return False, "no_progression"
    return True, "progression"
# ── FPL SYNCING ENGINE ─────────────────────────────────────────────────────────
def fetch_fpl_data():
    cache_file = Path("fpl_cache.json")
    if cache_file.exists() and (datetime.now().timestamp() - cache_file.stat().st_mtime < 86400):
        with open(cache_file, "r") as f: return json.load(f)
    try:
        req = urllib.request.Request("https://fantasy.premierleague.com/api/bootstrap-static/", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read())
            with open(cache_file, "w") as f: json.dump(data, f)
            return data
    except Exception:
        return None
def find_player_in_fpl(player_name, data):
    if not data or not player_name: return None
    elements = data.get("elements", [])
    p_lower = player_name.lower().replace(" ", "")
    for el in elements:
        fullname = (el["first_name"] + el["second_name"]).lower().replace(" ", "")
        if p_lower in el["web_name"].lower().replace(" ", "") or p_lower in fullname:
            return el
    return None

def fpl_player_club(player_el, data):
    """Return the player's CURRENT club as an official key (e.g. 'Man_Utd'),
    read straight from FPL team data. Reliable — no tweet parsing needed."""
    if not player_el or not data:
        return None
    teams = {t["id"]: t for t in data.get("teams", [])}
    t = teams.get(player_el.get("team"))
    if not t:
        return None
    # match the FPL team name to our official key via CLUB_NAME_MAP / aliases
    name = t.get("name", "").lower()
    if name in CLUB_NAME_MAP:
        return CLUB_NAME_MAP[name]
    # try our broader alias table too
    return CLUB_ALIASES.get(name)
# ── TEXT GENERATORS ────────────────────────────────────────────────────────────
def status_label(stype: str, stage: int, collapsed: bool, rumour: bool = False) -> str:
    """ONE consistent status word used on BOTH the card pill and the tweet,
    so the brand looks the same every time."""
    if collapsed:
        return "COLLAPSED"
    if rumour:
        return "RUMOUR"
    if stype == "transfer":
        return {1: "CONFIRMED", 2: "ADVANCED", 3: "HERE WE GO", 4: "DONE DEAL"}.get(stage, "CONFIRMED")
    if stype == "manager":
        return {1: "LATEST", 2: "TALKS", 3: "AGREED", 4: "APPOINTED"}.get(stage, "LATEST")
    # injury
    return {1: "INJURY DOUBT", 2: "INJURY SCAN", 3: "RULED OUT", 4: "FIT AGAIN"}.get(stage, "INJURY")


def label_color(stype: str, stage: int, collapsed: bool, rumour: bool = False):
    """Eye-catching colour for the status label, by meaning:
       RED = bad/injury/collapsed, GREEN = good/done/fit,
       ORANGE = transfer in progress, AMBER = rumour."""
    RED, GREEN, ORANGE, AMBER, BLUE = (255, 60, 70), (40, 210, 90), (255, 120, 0), (255, 196, 0), (0, 170, 255)
    if collapsed:
        return RED
    if rumour:
        return AMBER
    if stype == "injury":
        return GREEN if stage == 4 else RED          # fit again = green, else red
    if stype == "manager":
        return GREEN if stage == 4 else BLUE
    # transfer
    return GREEN if stage >= 3 else ORANGE           # signed/done = green, talks = orange


def build_headline(player: str, clubs: list, stage: int, stype: str, fee: str, contract: str, collapsed: bool) -> tuple[str, str]:
    p = player or "Player"
    # destination club = last one mentioned; clubs are official keys ("Man_Utd")
    raw_club = clubs[-1].replace("_", " ") if clubs else "Club"

    details = []
    if stype == "transfer":
        details.append(f"💰 {fee}" if fee else "💰 Undisclosed")
    elif fee:
        details.append(f"💰 {fee}")
    if contract: details.append(f"⏱️ {contract}")
    detail_line = " | ".join(details) if details else ""
    if collapsed: return f"{p} ❌ Deal to {raw_club} collapsed", detail_line
    if stype == "transfer":
        texts = {1: f"👀 {p} in talks with {raw_club}", 2: f"🤝 {p} reaches agreement with {raw_club}", 3: f"📝 {p} signs contract with {raw_club}", 4: f"🚨 {p} officially joins {raw_club} ✅"}
    elif stype == "manager":
        texts = {1: f"👔 {p} emerging as {raw_club} target", 2: f"🗣️ {p} in talks to become {raw_club} manager", 3: f"✍️ {p} agrees terms with {raw_club}", 4: f"🚨 {p} officially appointed at {raw_club} ✅"}
    else:
        texts = {1: f"⚠️ {p} injury concern — fitness in doubt", 2: f"🏥 {p} undergoes scan — diagnosis awaited", 3: f"🤕 {p} ruled out — return date unknown", 4: f"💪 {p} fit again — available for selection ✅"}
    return texts.get(stage, f"{p} update"), detail_line
def build_tweet_body(player: str, club: str, stage: int, stype: str, fee: str, contract: str, collapsed: bool, hashtags: str, from_club: str = None) -> str:
    p = player or "Player"
    c = club or "Club"
    clean_club = c.replace("_", " ")
    # Move string for the headline: "Chelsea ➜ Liverpool" if we know the FROM club,
    # otherwise just the destination club.
    from_clean = from_club.replace("_", " ") if from_club else None
    move = f"{from_clean} ➜ {clean_club}" if from_clean else clean_club
    lbl = status_label(stype, stage, collapsed)        # consistent brand label
    if collapsed:
        base = f"🚨 {lbl} | {p} | {move}\n\nThe proposed deal taking {p} to {clean_club} has officially collapsed. The move is completely off and the player will explore other options. 🚫"
    elif stype == "transfer":
        if stage == 1:
            base = f"🚨 {lbl} | {p} | {move}\n\n{clean_club} have concrete interest in signing {p} and contacts are underway as they monitor the situation. 👀"
        elif stage == 2:
            base = f"🚨 {lbl} | {p} | {move}\n\nNegotiations are moving fast! {p} is now close to an agreement with {clean_club} as final details are discussed. ⏳"
        elif stage == 3:
            base = f"🚨 {lbl} | {p} | {move}\n\n{p} has signed the contract with {clean_club}. All documents are completed and ready to go! 📝"
        else:
            base = f"🚨 {lbl} | {p} | {move}\n\n{p} has completed a permanent move to {clean_club}, arriving with big expectations to strengthen the squad. ⭐"
    elif stype == "manager":
        if stage == 4:
            base = f"🚨 OFFICIAL | {p} 👔 {clean_club}\n\n{p} has been officially appointed as the new manager of {clean_club}. A new era begins at the club! 📋"
        else:
            base = f"🚨 MANAGERIAL UPDATE | {p} ⏳ {clean_club}\n\n{p} is heavily linked with the managerial role at {clean_club}. Talks are ongoing regarding the project and vision. 🗣️"
    else:
        if stage == 4:
            base = f"💪 INJURY UPDATE | {p} ✅\n\nGreat news! {p} is fully fit and available for selection once again. A massive boost for the squad. ⚡"
        else:
            base = f"⚠️ INJURY ALERT | {p} 🤕\n\n{p} has picked up an injury concern. The medical staff is currently assessing the situation to determine a return timeline. 🏥"
    details = []
    if stype == "transfer": details.append(f"💰 Fee: {fee if fee else 'Undisclosed'}")
    if contract: details.append(f"📄 Contract: {contract}")

    if details:
        base += "\n\n" + "\n".join(details)

    base += f"\n\n{hashtags} #FPL"
    return base
def twitter_len(text: str) -> int:
    """Twitter's weighted character count (not len()).
    - Every URL counts as 23 chars regardless of real length.
    - Emoji / most non-Latin chars count as 2.
    This matches what the API enforces, so our 280 guard is accurate."""
    # URLs are flattened to 23 each
    url_re = re.compile(r'https?://\S+|www\.\S+')
    urls = url_re.findall(text)
    stripped = url_re.sub("", text)
    weight = 23 * len(urls)
    for ch in stripped:
        o = ord(ch)
        # CJK, emoji, symbols, and other wide ranges weigh 2; Latin/punct weigh 1
        if o <= 0x10FF or (0x2000 <= o <= 0x200D) or (0x2010 <= o <= 0x201F) or (0x2032 <= o <= 0x2037):
            weight += 1
        else:
            weight += 2
    return weight


def trim_for_twitter(body: str, limit: int = 278) -> str:
    """Ensure a tweet fits Twitter's weighted limit. Strategy:
    1. If it already fits, return unchanged.
    2. Drop trailing hashtags one at a time (keeps the headline/story intact).
    3. If still too long, hard-truncate the remaining text with an ellipsis."""
    if twitter_len(body) <= limit:
        return body

    # Split off the trailing hashtag block (last paragraph that is all #tags).
    parts = body.rsplit("\n\n", 1)
    if len(parts) == 2 and parts[1].strip().startswith("#"):
        head, tag_line = parts[0], parts[1]
        tags = tag_line.split()
        while tags and twitter_len(head + "\n\n" + " ".join(tags)) > limit:
            tags.pop()                       # remove least-important (last) tag
        candidate = head + ("\n\n" + " ".join(tags) if tags else "")
        if twitter_len(candidate) <= limit:
            return candidate
        body = head                          # tags gone, still long → trim head below

    # Hard truncate by weighted length, leaving room for the ellipsis.
    out = ""
    for ch in body:
        if twitter_len(out + ch) > limit - 1:
            break
        out += ch
    return out.rstrip() + "…"


# Clean official-club -> hashtag map (replaces the junky club_hashtags lookup)
CLUB_HASHTAG_MAP = {
    "Arsenal": "#Arsenal", "Aston_Villa": "#AVFC", "Bournemouth": "#AFCB",
    "Brentford": "#Brentford", "Brighton": "#BHAFC", "Chelsea": "#Chelsea",
    "Crystal_Palace": "#CPFC", "Everton": "#EFC", "Fulham": "#FFC",
    "Ipswich": "#ITFC", "Leicester": "#LCFC", "Liverpool": "#LFC",
    "Man_City": "#MCFC", "Man_Utd": "#MUFC", "Newcastle": "#NUFC",
    "Nottm_Forest": "#NFFC", "Southampton": "#SaintsFC", "Spurs": "#THFC",
    "West_Ham": "#WHUFC", "Wolves": "#Wolves",
}

def build_hashtags(stype: str, clubs: list, text: str, club_hashtags: dict = None, pl_clubs: set = None) -> str:
    """clubs are now official keys (e.g. 'Man_Utd'). Builds clean tags."""
    tags = ["#TransferNews" if stype == "transfer" else "#ManagerNews" if stype == "manager" else "#InjuryNews", "#Football"]
    for club in clubs[:2]:
        ht = CLUB_HASHTAG_MAP.get(club)
        if ht and ht not in tags: tags.append(ht)
    # every club in our map is a Premier League club, so tag the league
    if clubs and "#PremierLeague" not in tags: tags.append("#PremierLeague")
    return " ".join(tags[:5])
# ── PREMIUM GRAPHICS ENGINE ────────────────────────────────────────────────────
_FONT_CACHE = {}

# Scalable TrueType fallbacks if the Montserrat download fails. PIL's
# load_default() is a fixed 10px bitmap, so without these the whole card
# renders in tiny text. These cover common Linux/macOS install paths.
_FALLBACK_FONTS = {
    "Black": [
        "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ],
    "Bold": [
        "/usr/share/fonts/truetype/google-fonts/Poppins-SemiBold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ],
}


def _load_fallback(size: int, weight: str):
    for path in _FALLBACK_FONTS.get(weight, _FALLBACK_FONTS["Bold"]):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    # last resort: scalable default (Pillow >= 10 accepts size; older ignores it)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def get_premium_font(size: int, weight="Bold"):
    """Cache fonts by (weight,size) so we don't re-open the file on every draw call.
    Falls back to a SCALABLE system TTF (not the 10px bitmap default) if the
    Montserrat download is unavailable, so text never collapses to tiny."""
    key = (weight, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]

    font_path = f"Montserrat-{weight}.ttf"
    if not os.path.exists(font_path):
        try:
            font_url = f"https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-{weight}.ttf"
            req = urllib.request.Request(font_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as response, open(font_path, 'wb') as out:
                out.write(response.read())
        except Exception:
            f = _load_fallback(size, weight)
            _FONT_CACHE[key] = f
            return f
    try:
        f = ImageFont.truetype(font_path, size)
    except Exception:
        f = _load_fallback(size, weight)
    _FONT_CACHE[key] = f
    return f


def _download_asset(url: str, dest: Path) -> bool:
    """Download to a temp file then atomically rename, so a crash mid-write
    never leaves a 0-byte/corrupt cache file that breaks every future run."""
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                return False
            data = resp.read()
        if not data:
            return False
        with open(tmp, 'wb') as f:
            f.write(data)
        tmp.replace(dest)
        return True
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False


def _safe_open_rgba(path: Path):
    """Open an image defensively; delete & skip if it's a corrupt/empty cache file."""
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


def _fit_contain(im: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """Resize preserving aspect ratio (logos are NOT square — never squash them)."""
    return ImageOps.contain(im, (box_w, box_h), Image.Resampling.LANCZOS)


def _draw_arrow(d, x, y, w, color, direction, thick=24):
    """Thick horizontal arrow. direction: 'left' or 'right'.
    (x,y) = top-left of the arrow's bounding box; w = total width."""
    head = int(thick * 2.2)                   # arrowhead length
    cy = y + thick // 2                        # vertical center of the shaft
    if direction == "right":
        d.rounded_rectangle([x, y, x + w - head, y + thick], radius=thick // 2, fill=color)
        d.polygon([(x + w - head, cy - thick), (x + w, cy), (x + w - head, cy + thick)], fill=color)
    else:  # left
        d.rounded_rectangle([x + head, y, x + w, y + thick], radius=thick // 2, fill=color)
        d.polygon([(x + head, cy - thick), (x, cy), (x + head, cy + thick)], fill=color)


def _load_crest(club_key: str, box: int = 150):
    """Download (if needed) and return a club crest as RGBA, aspect-fit to box."""
    if not club_key:
        return None
    safe = club_key.replace(" ", "_").replace("'", "")
    p = Path(f"logos/{safe}.png")
    if not p.exists() and FPL_LOGO_IDS.get(safe):
        _download_asset(f"https://resources.premierleague.com/premierleague/badges/t{FPL_LOGO_IDS.get(safe)}.png", p)
    if p.exists():
        src = _safe_open_rgba(p)
        if src is not None:
            return _fit_contain(src, box, box)
    return None


def create_image(headline: str, detail_line: str, source_users: list, stage: int, stype: str, collapsed: bool, filename: str, target_club: str, player_name: str, from_club: str = None, to_club: str = None, rumour: bool = False):
    W, H = 1200, 675
    fpl_data = fetch_fpl_data()
    player_el = find_player_in_fpl(player_name, fpl_data)

    # default the move endpoints if not supplied
    if to_club is None:
        to_club = target_club
    GREEN = (40, 210, 90)

    # For INJURY cards there is no move. Use the player's OWN club (from FPL) as
    # the single club shown, and as the colour panel.
    injury_club = None
    if stype == "injury":
        injury_club = fpl_player_club(player_el, fpl_data) or target_club
        to_club = injury_club            # colour panel = player's club

    stats = None
    player_img_path = Path("players/silhouette.png")
    if player_el:
        code = player_el["code"]
        stats = {"cost": f"£{player_el['now_cost']/10.0}m", "pts": str(player_el['total_points']), "goals": str(player_el['goals_scored']), "assists": str(player_el['assists'])}
        player_img_path = Path(f"players/{code}.png")
        if not player_img_path.exists():
            _download_asset(f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{code}.png", player_img_path)
    have_player_img = player_img_path.exists()

    bg_color = CLUB_COLORS.get(to_club, (25, 29, 38)) if to_club else (25, 29, 38)
    accent = (255, 90, 0) if stype == "transfer" else (0, 163, 255) if stype == "manager" else (255, 0, 77)
    if collapsed: accent = (107, 114, 128)

    img = Image.new("RGB", (W, H), (14, 16, 21))
    draw = ImageDraw.Draw(img)

    # 1. Right diagonal cutout (TO-club colour panel) + depth shading
    draw.polygon([(W*0.52, 0), (W, 0), (W, H), (W*0.42, H)], fill=bg_color)
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    sd.polygon([(W*0.52, 0), (W, 0), (W, H), (W*0.42, H)], fill=(0, 0, 0, 70))
    grad = Image.new("L", (1, H), 0)
    for y in range(H):
        grad.putpixel((0, y), int(110 * (y / H)))
    grad = grad.resize((W, H))
    img.paste(shade, (0, 0), Image.composite(shade.split()[3], Image.new("L", (W, H), 0), grad))

    # 2. Player headshot (bottom-right), aspect-correct + soft shadow.
    #    If there is no FPL photo, draw a clean "NO PHOTO" placeholder instead
    #    so the right panel never looks empty/unfinished.
    photo_ok = False
    if have_player_img:
        p_src = _safe_open_rgba(player_img_path)
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
        # silhouette circle + "NO PHOTO" label, centred in the colour panel
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        cx, cyc, r = int(W * 0.78), int(H * 0.50), 150
        od.ellipse([cx - r, cyc - r, cx + r, cyc + r], fill=(0, 0, 0, 70))
        # simple person silhouette (head + shoulders) in a soft tint
        head_r = 52
        od.ellipse([cx - head_r, cyc - 78, cx + head_r, cyc - 78 + head_r * 2],
                   fill=(255, 255, 255, 60))
        od.pieslice([cx - 95, cyc + 6, cx + 95, cyc + 210], 180, 360, fill=(255, 255, 255, 60))
        img.paste(ov, (0, 0), ov)
        ph_font = get_premium_font(34, "Black")
        label = "NO PHOTO"
        lw = od.textlength(label, font=ph_font)
        ImageDraw.Draw(img).text((cx - lw / 2, cyc + r - 6), label,
                                  font=ph_font, fill=(255, 255, 255))

    # ── Left layout: borders, brand, status ─────────────────────────────────────
    TEXT_X = 60
    TEXT_MAX_W = int(W * 0.62) - TEXT_X
    draw.rectangle([0, 0, W, 12], fill=accent)
    brand_font = get_premium_font(60, "Black")
    sub_font = get_premium_font(38, "Bold")
    crest_name_font = get_premium_font(34, "Black")

    draw.text((TEXT_X, 44), "FPL", font=brand_font, fill=(255, 255, 255))
    fpl_w = draw.textlength("FPL ", font=brand_font)
    draw.text((TEXT_X + fpl_w, 44), "VORTEX", font=brand_font, fill=accent)

    if rumour:
        badge_txt = "RUMOUR – NOT CONFIRMED"
        badge_fill = (255, 196, 0)             # amber warning
        badge_bg = (60, 45, 0)
    else:
        # SAME label set as the tweet, so the brand reads consistently.
        badge_txt = status_label(stype, stage, collapsed)
        badge_fill = label_color(stype, stage, collapsed)   # colour-coded by meaning
        badge_bg = (25, 28, 38)
    badge_w = int(draw.textlength(badge_txt, font=sub_font))
    draw.rounded_rectangle([TEXT_X, 138, TEXT_X + badge_w + 52, 206], radius=14, fill=badge_bg)
    draw.text((TEXT_X + 26, 152), badge_txt, font=sub_font, fill=badge_fill)

    # ── Player name (big, ALL CAPS, auto-fit to the left column) ─────────────────
    name_up = (player_name or "PLAYER").upper()
    nsize = 78
    while nsize >= 40:
        nf = get_premium_font(nsize, "Black")
        if draw.textlength(name_up, font=nf) <= TEXT_MAX_W:
            break
        nsize -= 3
    nf = get_premium_font(nsize, "Black")
    name_y = 236
    with Pilmoji(img) as pilmoji:
        pilmoji.text((TEXT_X, name_y), name_up, font=nf, fill=(255, 255, 255))
    nb = draw.textbbox((0, 0), name_up, font=nf)
    name_bottom = name_y + (nb[3] - nb[1]) + 12

    CREST = 132
    row_y = name_bottom + 36
    cy = row_y + CREST // 2

    if stype == "injury":
        # ── INJURY: ONE club crest + name + medical icon (NO arrow) ─────────────
        club_im = _load_crest(injury_club, CREST)
        club_name = (injury_club.replace("_", " ") if injury_club else "")
        x = TEXT_X
        if club_im is not None:
            img.paste(club_im, (x, row_y + (CREST - club_im.height)//2), club_im)
            cnw = draw.textlength(club_name.upper(), font=crest_name_font)
            draw.text((x + (CREST - cnw)//2, row_y + CREST + 10), club_name.upper(), font=crest_name_font, fill=(255, 255, 255))
            x += CREST + 36
        # medical cross badge instead of an arrow
        cross_fill = label_color(stype, stage, collapsed)
        cs = 96
        cyy = row_y + (CREST - cs)//2
        draw.rounded_rectangle([x, cyy, x + cs, cyy + cs], radius=18, fill=cross_fill)
        bar = 22
        midx, midy = x + cs//2, cyy + cs//2
        draw.rectangle([midx - bar//2, cyy + 18, midx + bar//2, cyy + cs - 18], fill=(255, 255, 255))
        draw.rectangle([x + 18, midy - bar//2, x + cs - 18, midy + bar//2], fill=(255, 255, 255))
    else:
        # ── TRANSFER / MANAGER: FROM crest ─► TO crest, names underneath ────────
        from_im = _load_crest(from_club, CREST)
        to_im = _load_crest(to_club, CREST)
        x = TEXT_X
        from_name = (from_club.replace("_", " ") if from_club else "")
        to_name = (to_club.replace("_", " ") if to_club else "")
        if from_im is not None:
            img.paste(from_im, (x, row_y + (CREST - from_im.height)//2), from_im)
            fnw = draw.textlength(from_name.upper(), font=crest_name_font)
            draw.text((x + (CREST - fnw)//2, row_y + CREST + 10), from_name.upper(), font=crest_name_font, fill=(235, 235, 235))
            x += CREST + 30
        arrow_w = 150
        _draw_arrow(draw, x, cy - 14, arrow_w, GREEN, "right", thick=28)
        x += arrow_w + 30
        if to_im is not None:
            img.paste(to_im, (x, row_y + (CREST - to_im.height)//2), to_im)
            tnw = draw.textlength(to_name.upper(), font=crest_name_font)
            draw.text((x + (CREST - tnw)//2, row_y + CREST + 10), to_name.upper(), font=crest_name_font, fill=(255, 255, 255))

    # ── Detail line (fee / contract) in caps, green, clearly visible ─────────────
    if detail_line:
        det_y = row_y + CREST + 56
        with Pilmoji(img) as pilmoji:
            pilmoji.text((TEXT_X, det_y), detail_line.upper(), font=sub_font, fill=(160, 255, 120))

    # 4. Bottom stats / source bar — auto-fit width so nothing clips off-edge
    draw.rectangle([0, H - 90, W, H - 12], fill=(20, 24, 33))
    draw.rectangle([0, H - 12, W, H], fill=accent)

    if stats:
        stat_txt = (f"FPL COST: {stats['cost']}    |    POINTS: {stats['pts']}"
                    f"    |    GOALS: {stats['goals']}    |    ASSISTS: {stats['assists']}")
        fill = (255, 255, 255)
    else:
        src_txt = "  ·  ".join(f"@{s}" for s in source_users[:2])
        stat_txt = f"Source: {src_txt}    |    @FPLVortex"
        fill = (170, 180, 200)

    bar_size = 30
    bar_font = get_premium_font(bar_size, "Bold")
    while bar_size > 18 and draw.textlength(stat_txt, font=bar_font) > (W - 120):
        bar_size -= 1
        bar_font = get_premium_font(bar_size, "Bold")

    bbox = draw.textbbox((0, 0), stat_txt, font=bar_font)
    text_h = bbox[3] - bbox[1]
    bar_y = (H - 90) + (78 - text_h) // 2 - bbox[1]
    draw.text((60, bar_y), stat_txt, font=bar_font, fill=fill)

    img.save(filename)
# ── QUEUE MANAGEMENT ───────────────────────────────────────────────────────────
def save_pending(item: dict):
    slug = re.sub(r'[^a-z0-9_]', '', item["key"]) + f"_s{item['stage']}"
    with open(PENDING_DIR / f"{slug}.json", "w") as f: json.dump(item, f, indent=2)
def move_to_posted(item: dict):
    slug = re.sub(r'[^a-z0-9_]', '', item["key"]) + f"_s{item['stage']}"
    src, dst = PENDING_DIR / f"{slug}.json", POSTED_DIR / f"{slug}.json"
    if src.exists(): src.rename(dst)
    else:
        with open(dst, "w") as f: json.dump(item, f, indent=2)
# ── SCRAPER CORE ───────────────────────────────────────────────────────────────
def get_nitter_tweets(username: str) -> list:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; RSS reader)"}
    for instance in NITTER_INSTANCES:
        try:
            r = requests.get(f"{instance}/{username}/rss", headers=headers, timeout=10)
            if r.status_code != 200: continue
            root = ET.fromstring(r.content)
            tweets = []
            for item in root.findall(".//item")[:8]:
                link = item.find("link")
                desc = item.find("description")
                if link is None: continue
                tid = link.text.strip().split("/")[-1].split("#")[0]
                text = re.sub(r'<[^>]+>', '', desc.text).strip() if desc is not None and desc.text else ""
                if tid and text: tweets.append({"id": tid, "text": text})
            if tweets: return tweets
        except: continue
    return []
def passes_safety_gate(text, stype, player, clubs, from_club, to_club, fpl_data, collapsed):
    """MAX-SAFETY gate. Returns (ok: bool, why: str).
    Goal: never post a false/garbled transfer. When unsure -> reject."""
    tl = text.lower()

    # 1) Block staff / off-pitch people (scouts, directors, agents, owners…).
    if any(re.search(r'(?<![a-z])' + re.escape(w) + r'(?![a-z])', tl) for w in STAFF_BLOCK_KW):
        return False, "staff_or_offpitch"

    # Injuries: lighter rules (need a real FPL player to be safe + on-topic).
    if stype == "injury":
        if not player or find_player_in_fpl(player, fpl_data) is None:
            return False, "injury_player_not_verified"
        return True, "ok_injury"

    # Managers: only post if clearly a manager appointment AND a PL club present.
    if stype == "manager":
        if not clubs:
            return False, "manager_no_club"
        if not any(k in tl for k in ["appoint", "appointed", "new manager", "new head coach",
                                     "sacked", "sack", "takes charge", "confirmed as", "officially"]):
            return False, "manager_weak_signal"
        return True, "ok_manager"

    # Transfers (the risky one) — strictest rules:
    if not player:
        return False, "no_player"
    # (a) player must be a REAL FPL player -> filters scouts/non-PL noise
    if find_player_in_fpl(player, fpl_data) is None:
        return False, "player_not_in_fpl"
    # (b) must have a destination PL club
    if not to_club:
        return False, "no_destination_club"
    # (c) ambiguity guard: many clubs named but no clear from/to wording -> skip
    if len(clubs) >= 3 and not from_club:
        return False, "too_ambiguous"
    # NOTE: weak-signal (rumour) transfers are allowed through here, but only get
    # POSTED later if the player is a 'big name' — decided in classify_post().
    return True, "ok_transfer"


def is_big_player(player, fpl_data):
    """A 'big name' = worth posting an UNCONFIRMED rumour about.
    Objective FPL test: expensive OR high-scoring."""
    el = find_player_in_fpl(player, fpl_data)
    if not el:
        return False
    return el.get("now_cost", 0) >= 70 or el.get("total_points", 0) >= 100   # £7.0m or 100+ pts


def classify_post(text, stype, player, sources, collapsed, fpl_data):
    """Decide HOW a story may be posted:
       'confirmed' -> post as fact
       'rumour'    -> post but clearly labelled UNCONFIRMED (big players only)
       None        -> do not post (yet)
    """
    tl = text.lower()
    has_official = any(w in tl for w in OFFICIAL_WORDS)
    top_source = any(s in TOP_SOURCES for s in sources)
    multi_source = len(set(sources)) >= 2

    if collapsed:
        return "confirmed"
    if stype in ("manager", "injury"):
        return "confirmed"                      # already gated tightly upstream

    # Transfers:
    if has_official and (top_source or multi_source):
        return "confirmed"                      # trusted + official wording
    if multi_source:
        return "confirmed"                      # 2+ journalists agree = confirmed
    # Otherwise it's a single-source, non-official report = RUMOUR.
    if is_big_player(player, fpl_data):
        return "rumour"                         # big name -> post, clearly labelled
    return None                                 # small-player rumour -> skip


async def scrape(data: dict, club_hashtags: dict) -> list:
    story_map = {}
    fpl_data = fetch_fpl_data()
    for username in JOURNALISTS:
        tweets = get_nitter_tweets(username)
        for t in tweets:
            tid, text = t["id"], t["text"]
            if tid in data["posted_ids"]: continue
            tl = text.lower()
            if not (any(k in tl for k in TRANSFER_KW) or any(k in tl for k in INJURY_KW) or any(k in tl for k in MANAGER_KW)): continue

            collapsed = is_collapse(text)
            stype = classify_type(text)
            stage = 0 if collapsed else get_stage(text, stype)
            clubs = extract_clubs(text)                 # official keys, word-boundary matched
            player = extract_player(text, clubs)        # skip club words when picking a name
            from_club, to_club = extract_transfer_clubs(text)   # direction of the move

            # ── MAX-SAFETY GATE: reject anything we aren't confident about ──
            safe, why = passes_safety_gate(text, stype, player, clubs, from_club, to_club, fpl_data, collapsed)
            if not safe:
                continue
            key = build_story_key(player, to_club or (clubs[0] if clubs else None), stype)
            ok, reason = should_post(data, key, stage, collapsed)
            if not ok: continue
            if key in story_map:
                existing = story_map[key]
                if username not in existing["sources"]: existing["sources"].append(username)
                if stage > existing["stage"]: existing["stage"] = stage
            else:
                # carry over any sources seen for this story in PREVIOUS runs
                prior = data.get("pending", {}).get(key, {}).get("sources", [])
                merged_sources = list(dict.fromkeys(prior + [username]))
                story_map[key] = {
                    "id": tid, "key": key, "text": text, "sources": merged_sources, "stype": stype,
                    "stage": stage, "collapsed": collapsed, "player": player, "clubs": clubs,
                    "from_club": from_club, "to_club": to_club,
                    "fee": extract_fee(text), "contract": extract_contract(text), "reason": reason
                }
        await asyncio.sleep(1)

    # ── Decide post mode (confirmed / rumour / hold) for each story ──────────────
    ready = []
    for key, st in story_map.items():
        mode = classify_post(st["text"], st["stype"], st["player"], st["sources"], st["collapsed"], fpl_data)
        if mode is None:
            # not postable yet — but if it's a real story, remember it so a future
            # source can CONFIRM it later (hold-and-retry).
            data["pending"][key] = {
                "sources": st["sources"], "player": st["player"],
                "to_club": st.get("to_club"), "stype": st["stype"],
                "last_seen": datetime.now(timezone.utc).isoformat(),
            }
            continue
        st["rumour"] = (mode == "rumour")
        # once we decide to post, drop it from pending
        data["pending"].pop(key, None)
        ready.append(st)

    save_data(data)
    return sorted(ready, key=lambda x: -(1 if x["collapsed"] else x["stage"]))
# ── TWITTER PUBLISHER ──────────────────────────────────────────────────────────
async def post_item(client: Client, item: dict, data: dict, club_hashtags: dict, pl_clubs: set):
    headline, detail_line = build_headline(item["player"], item["clubs"], item["stage"], item["stype"], item["fee"], item["contract"], item["collapsed"])
    hashtags = build_hashtags(item["stype"], item["clubs"], item["text"], club_hashtags, pl_clubs)

    # FROM / TO clubs (official keys). TO = destination = the buying club.
    from_club = item.get("from_club")
    to_club = item.get("to_club") or (item["clubs"][-1] if item["clubs"] else None)
    target_club = to_club
    rumour = item.get("rumour", False)
    filename = "news_card.png"
    create_image(headline, detail_line, item["sources"], item["stage"], item["stype"], item["collapsed"], filename, target_club, item["player"], from_club, to_club, rumour=rumour)
    media_id = await client.upload_media(filename, media_type="image/png")

    # full club name for the tweet body (Man_Utd -> "Man Utd")
    raw_club_name = to_club if to_club else "Club"
    body = build_tweet_body(item["player"], raw_club_name, item["stage"], item["stype"], item["fee"], item["contract"], item["collapsed"], hashtags, from_club)

    # Clearly mark unconfirmed rumours at the very top of the tweet.
    if rumour:
        body = "⚠️ RUMOUR (UNCONFIRMED)\n" + body

    body = trim_for_twitter(body, limit=278)   # weighted trim (URLs=23, emoji=2)
    await client.create_tweet(text=body, media_ids=[media_id])
    if os.path.exists(filename): os.remove(filename)
    data["posted_ids"].append(item["id"])
    data["stories"][item["key"]] = {
        "stage": item["stage"], "player": item["player"], "clubs": item["clubs"], "type": item["stype"],
        "status": "collapsed" if item["collapsed"] else "active", "sources": item["sources"],
        "last_updated": datetime.now(timezone.utc).isoformat()
    }
    increment_daily(data)
    save_data(data)
    move_to_posted(item)
    print(f"  ✅ Posted card for {item['player']}!")
# ── MAIN EXECUTION LOOP ────────────────────────────────────────────────────────
async def main():
    print(f"\n[BOT] Run — {datetime.now(timezone.utc).isoformat()}")
    club_data = get_club_data()
    CLUB_HASHTAGS = club_data["club_hashtags"]
    PL_CLUBS = set(club_data["pl_clubs"])
    data = load_data()
    if not check_daily_limit(data): return
    queue = await scrape(data, CLUB_HASHTAGS)
    if not queue:
        print("[BOT] Quiet run. No new stories found.")
        return
    for item in queue: save_pending(item)
    post_client = Client("en-US")
    post_client.set_cookies({"auth_token": X_POST_AUTH_TOKEN, "ct0": X_POST_CT0_TOKEN})
    remaining = data["daily"]["limit"] - data["daily"]["count"]
    for i, item in enumerate(queue[:min(3, remaining)]):
        try:
            await post_item(post_client, item, data, CLUB_HASHTAGS, PL_CLUBS)
        except Exception as e:
            print(f"  [ERROR] Failed to post {item['key']}: {e}")
        if i < min(3, remaining) - 1: await asyncio.sleep(60)
if __name__ == "__main__":
    asyncio.run(main())
