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
            return json.load(f)
    return {"daily": {"date": "", "count": 0, "limit": 17}, "stories": {}, "posted_ids": []}
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
def extract_player(text: str) -> str:
    matches = re.findall(r'\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\b', text)
    for m in matches:
        if m not in SKIP_WORDS and len(m) > 3:
            return m
    return None
def extract_clubs(text: str, club_hashtags: dict) -> list:
    tl = text.lower()
    return [c for c in club_hashtags if c in tl]
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
# ── TEXT GENERATORS ────────────────────────────────────────────────────────────
def build_headline(player: str, clubs: list, stage: int, stype: str, fee: str, contract: str, collapsed: bool) -> tuple[str, str]:
    p = player or "Player"
    raw_club = clubs[1].title() if len(clubs) > 1 else clubs[0].title() if clubs else "Club"

    details = []
    if fee: details.append(f"💰 {fee}")
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
def build_tweet_body(player: str, club: str, stage: int, stype: str, fee: str, contract: str, collapsed: bool, hashtags: str) -> str:
    p = player or "Player"
    c = club or "Club"
    clean_club = c.replace("_", " ")
    if collapsed:
        base = f"🚨 COLLAPSED | {p} ❌ {clean_club}\n\nThe proposed deal taking {p} to {clean_club} has officially collapsed. The move is completely off and the player will explore other options. 🚫"
    elif stype == "transfer":
        if stage == 1:
            base = f"🚨 EXCLUSIVE | {p} ⏳ {clean_club}\n\n{clean_club} have concrete interest in signing {p} and contacts are underway as they monitor the situation. 👀"
        elif stage == 2:
            base = f"🚨 ADVANCED | {p} 🔜 {clean_club}\n\nNegotiations are moving fast! {p} is now close to an agreement with {clean_club} as final details are discussed. ⏳"
        elif stage == 3:
            base = f"🚨 HERE WE GO | {p} ✍️ {clean_club}\n\n{p} has signed the contract with {clean_club}. All documents are completed and ready to go! 📝"
        else:
            base = f"🚨 OFFICIAL | {p} ➡️ {clean_club}\n\n{p} has completed a permanent move to {clean_club}, arriving with big expectations to strengthen the squad. ⭐"
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
    if fee and stype == "transfer": details.append(f"💰 Fee: {fee}")
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


def build_hashtags(stype: str, clubs: list, text: str, club_hashtags: dict, pl_clubs: set) -> str:
    tags = ["#TransferNews" if stype == "transfer" else "#ManagerNews" if stype == "manager" else "#InjuryNews", "#Football"]
    for club in clubs[:2]:
        ht = club_hashtags.get(club)
        if ht and ht not in tags: tags.append(ht)
    if any(c in pl_clubs for c in clubs) and "#PremierLeague" not in tags: tags.append("#PremierLeague")
    c_tag = extract_country(text)
    if c_tag and c_tag not in tags: tags.append(c_tag)
    for lt in extract_league(text):
        if lt not in tags: tags.append(lt)
    return " ".join(tags[:6])
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


def create_image(headline: str, detail_line: str, source_users: list, stage: int, stype: str, collapsed: bool, filename: str, target_club: str, player_name: str):
    W, H = 1200, 675
    fpl_data = fetch_fpl_data()
    player_el = find_player_in_fpl(player_name, fpl_data)

    stats = None
    player_img_path = Path("players/silhouette.png")

    if player_el:
        code = player_el["code"]
        stats = {"cost": f"£{player_el['now_cost']/10.0}m", "pts": str(player_el['total_points']), "goals": str(player_el['goals_scored']), "assists": str(player_el['assists'])}
        player_img_path = Path(f"players/{code}.png")
        if not player_img_path.exists():
            _download_asset(f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{code}.png", player_img_path)
    have_player_img = player_img_path.exists()

    bg_color = CLUB_COLORS.get(target_club, (25, 29, 38)) if target_club else (25, 29, 38)
    accent = (255, 90, 0) if stype == "transfer" else (0, 163, 255) if stype == "manager" else (255, 0, 77)
    if collapsed: accent = (107, 114, 128)

    img = Image.new("RGB", (W, H), (14, 16, 21))
    draw = ImageDraw.Draw(img)

    # 1. Right diagonal cutout (dynamic team-colour panel) + subtle vertical shading for depth
    draw.polygon([(W*0.52, 0), (W, 0), (W, H), (W*0.42, H)], fill=bg_color)
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    sd.polygon([(W*0.52, 0), (W, 0), (W, H), (W*0.42, H)], fill=(0, 0, 0, 70))
    grad = Image.new("L", (1, H), 0)
    for y in range(H):
        grad.putpixel((0, y), int(110 * (y / H)))
    grad = grad.resize((W, H))
    img.paste(shade, (0, 0), Image.composite(shade.split()[3], Image.new("L", (W, H), 0), grad))

    # 2. Player headshot FIRST (so the crest sits on top of the player, not hidden) — aspect-correct + soft shadow
    if have_player_img:
        p_src = _safe_open_rgba(player_img_path)
        if p_src is not None:
            p_img = _fit_contain(p_src, 470, 470)
            shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            sh = p_img.split()[3].point(lambda a: int(a * 0.55))
            shadow.paste((0, 0, 0, 255), (W - 430 + 8, H - p_img.height - 6 + 8), sh)
            shadow = shadow.filter(ImageFilter.GaussianBlur(8))
            img.paste(shadow, (0, 0), shadow)
            img.paste(p_img, (W - 430, H - p_img.height - 6), p_img)

    # 3. Automated club crest (top-right), aspect-correct, on top of the headshot
    if target_club:
        safe_name = target_club.replace(" ", "_").replace("'", "")
        logo_path = Path(f"logos/{safe_name}.png")
        if not logo_path.exists() and FPL_LOGO_IDS.get(safe_name):
            _download_asset(f"https://resources.premierleague.com/premierleague/badges/t{FPL_LOGO_IDS.get(safe_name)}.png", logo_path)
        if logo_path.exists():
            logo_src = _safe_open_rgba(logo_path)
            if logo_src is not None:
                logo = _fit_contain(logo_src, 130, 130)
                img.paste(logo, (W - 40 - logo.width, 40), logo)

    # ── Left layout: borders, brand, status ─────────────────────────────────────
    TEXT_X = 60
    TEXT_MAX_W = int(W * 0.60) - TEXT_X      # ~660px usable text width

    draw.rectangle([0, 0, W, 12], fill=accent)
    brand_font = get_premium_font(64, "Black")
    sub_font = get_premium_font(40, "Bold")

    draw.text((TEXT_X, 46), "FPL", font=brand_font, fill=(255, 255, 255))
    fpl_w = draw.textlength("FPL ", font=brand_font)
    draw.text((TEXT_X + fpl_w, 46), "VORTEX", font=brand_font, fill=accent)

    s_label = STAGE_LABELS.get(stype, {}).get(0 if collapsed else stage, "UPDATE").upper()
    badge_txt = f"STATUS: {s_label}"
    badge_w = int(draw.textlength(badge_txt, font=sub_font))
    draw.rounded_rectangle([TEXT_X, 150, TEXT_X + badge_w + 56, 228], radius=14, fill=(25, 28, 38))
    draw.text((TEXT_X + 28, 166), badge_txt, font=sub_font, fill=accent)

    # ── FROM → TO headline: two ALL-CAPS words, thick arrows above each ──────────
    # Left word = player (FROM, red arrow ◄).  Right word = club (TO, green arrow ►).
    RED = (235, 30, 40)
    GREEN = (40, 210, 90)

    from_word = (player_name or "PLAYER").upper()
    to_word = (target_club.replace("_", " ") if target_club else "CLUB").upper()

    # Auto-size both words so each fits the column.
    gap = 50
    name_size = 92
    while name_size >= 46:
        nf = get_premium_font(name_size, "Black")
        wf = draw.textlength(from_word, font=nf)
        wt = draw.textlength(to_word, font=nf)
        if max(wf, wt) <= TEXT_MAX_W:
            break
        name_size -= 4
    nf = get_premium_font(name_size, "Black")
    wf = int(draw.textlength(from_word, font=nf))
    wt = int(draw.textlength(to_word, font=nf))

    arrow_h = 24
    arrow_to_word = 14
    word_pad = 18
    side_by_side = (wf + gap + wt) <= TEXT_MAX_W

    # Measure ACTUAL glyph metrics so arrows never collide with text.
    def _measure(word):
        b = draw.textbbox((0, 0), word, font=nf)
        return b[1], b[3] - b[1]               # (top offset, visual height)
    f_off, f_h = _measure(from_word)
    t_off, t_h = _measure(to_word)

    arr_w_from = max(90, min(wf - 20, 180))
    arr_w_to = max(90, min(wt - 20, 210))

    unit_from = arrow_h + arrow_to_word + f_h
    unit_to = arrow_h + arrow_to_word + t_h

    block_top, block_bottom = 244, H - 90 - 16
    detail_h = (sub_font.size + 16) if detail_line else 0
    if side_by_side:
        total_h = max(unit_from, unit_to) + word_pad + detail_h
    else:
        total_h = unit_from + word_pad + unit_to + word_pad + detail_h
    y_top = block_top + max(0, ((block_bottom - block_top) - total_h) // 2)

    with Pilmoji(img) as pilmoji:
        if side_by_side:
            x_from, x_to = TEXT_X, TEXT_X + wf + gap
            _draw_arrow(draw, x_from, y_top, arr_w_from, RED, "left", thick=arrow_h)
            _draw_arrow(draw, x_to, y_top, arr_w_to, GREEN, "right", thick=arrow_h)
            wy = y_top + arrow_h + arrow_to_word
            pilmoji.text((x_from, wy - f_off), from_word, font=nf, fill=(255, 255, 255))
            pilmoji.text((x_to, wy - t_off), to_word, font=nf, fill=(255, 255, 255))
            detail_y = wy + max(f_h, t_h) + word_pad
        else:
            _draw_arrow(draw, TEXT_X, y_top, arr_w_from, RED, "left", thick=arrow_h)
            wy1 = y_top + arrow_h + arrow_to_word
            pilmoji.text((TEXT_X, wy1 - f_off), from_word, font=nf, fill=(255, 255, 255))
            y2 = wy1 + f_h + word_pad
            _draw_arrow(draw, TEXT_X, y2, arr_w_to, GREEN, "right", thick=arrow_h)
            wy2 = y2 + arrow_h + arrow_to_word
            pilmoji.text((TEXT_X, wy2 - t_off), to_word, font=nf, fill=(255, 255, 255))
            detail_y = wy2 + t_h + word_pad

        if detail_line:
            pilmoji.text((TEXT_X, detail_y), detail_line.upper(), font=sub_font, fill=(160, 255, 120))

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
async def scrape(data: dict, club_hashtags: dict) -> list:
    story_map = {}
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
            player = extract_player(text)
            clubs = extract_clubs(text, club_hashtags)
            key = build_story_key(player, clubs[0] if clubs else None, stype)
            ok, reason = should_post(data, key, stage, collapsed)
            if not ok: continue
            if key in story_map:
                existing = story_map[key]
                if username not in existing["sources"]: existing["sources"].append(username)
                if stage > existing["stage"]: existing["stage"] = stage
            else:
                story_map[key] = {
                    "id": tid, "key": key, "text": text, "sources": [username], "stype": stype,
                    "stage": stage, "collapsed": collapsed, "player": player, "clubs": clubs,
                    "fee": extract_fee(text), "contract": extract_contract(text), "reason": reason
                }
        await asyncio.sleep(1)
    return sorted(story_map.values(), key=lambda x: -(1 if x["collapsed"] else x["stage"]))
# ── TWITTER PUBLISHER ──────────────────────────────────────────────────────────
async def post_item(client: Client, item: dict, data: dict, club_hashtags: dict, pl_clubs: set):
    headline, detail_line = build_headline(item["player"], item["clubs"], item["stage"], item["stype"], item["fee"], item["contract"], item["collapsed"])
    hashtags = build_hashtags(item["stype"], item["clubs"], item["text"], club_hashtags, pl_clubs)

    target_club = None
    for c in item["clubs"]:
        cleaned = c.replace("#", "").replace("_", " ").lower().strip()
        if cleaned in CLUB_NAME_MAP:
            target_club = CLUB_NAME_MAP[cleaned]
            break
    filename = "news_card.png"
    create_image(headline, detail_line, item["sources"], item["stage"], item["stype"], item["collapsed"], filename, target_club, item["player"])
    media_id = await client.upload_media(filename, media_type="image/png")

    # Fully integrated dynamic elite text layout formula mapping
    raw_club_name = target_club if target_club else (item["clubs"][0].title() if item["clubs"] else "Club")
    body = build_tweet_body(item["player"], raw_club_name, item["stage"], item["stype"], item["fee"], item["contract"], item["collapsed"], hashtags)

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
