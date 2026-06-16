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
from PIL import Image, ImageDraw, ImageFont
from pilmoji import Pilmoji
from twikit import Client

# ── SECRETS ────────────────────────────────────────────────────────────────────
X_POST_AUTH_TOKEN = os.getenv("X_POST_AUTH_TOKEN")
X_POST_CT0_TOKEN  = os.getenv("X_POST_CT0_TOKEN")
FOOTBALL_API_KEY  = os.getenv("FOOTBALL_API_KEY")

# ── PATHS ──────────────────────────────────────────────────────────────────────
POSTED_FILE = Path("posted_news.json")
PENDING_DIR = Path("queue/pending")
POSTED_DIR  = Path("queue/posted")
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
                "contract", "agree", "confirm", "medical", "official", "close"]
INJURY_KW   = ["injury", "injured", "ruled out", "scan", "surgery", "doubt"]
MANAGER_KW  = ["sack", "appoint", "manager", "coach", "resign", "dismiss"]
COLLAPSE_KW = ["collapse", "collapsed", "deal off", "rejected", "failed"]

STAGE_KW = {
    "transfer": {
        1: ["interest", "talks", "keen", "want", "target"],
        2: ["agreement", "agreed", "negotiating", "close to", "personal terms"],
        3: ["signs", "signed", "contract signed", "deal signed"],
        4: ["official", "confirmed", "done deal", "completed", "medical", "joins"],
    },
    "manager": {
        1: ["considering", "target", "sack", "dismiss"],
        2: ["talks", "negotiating", "contact"],
        3: ["agreement", "agreed", "terms agreed"],
        4: ["appointed", "confirmed", "officially", "takes charge"],
    },
    "injury": {
        1: ["concern", "doubt", "knock", "worry"],
        2: ["scan", "assessment", "tests"],
        3: ["ruled out", "surgery", "sidelined"],
        4: ["return", "fit again", "cleared", "available"],
    },
}

STAGE_LABELS = {
    "transfer": {0: "DEAL COLLAPSED", 1: "TRANSFER TALKS", 2: "AGREEMENT REACHED", 3: "CONTRACT SIGNED", 4: "TRANSFER CONFIRMED"},
    "manager": {0: "DEAL COLLAPSED", 1: "MANAGERIAL CHANGE", 2: "MANAGER TALKS", 3: "TERMS AGREED", 4: "OFFICIALLY APPOINTED"},
    "injury": {0: "INJURY UPDATE", 1: "INJURY CONCERN", 2: "SCAN AWAITED", 3: "RULED OUT", 4: "FIT TO RETURN"},
}

COUNTRY_HASHTAGS = {"england": "#England", "spain": "#Spain", "italy": "#Italy", "germany": "#Germany"}
LEAGUE_HASHTAGS = {"premier league": ["#PremierLeague", "#PL"], "la liga": ["#LaLiga"], "serie a": ["#SerieA"]}

CLUB_HASHTAG_MAP = {
    "#arsenal": "Arsenal", "#astonvilla": "Aston_Villa", "#bournemouth": "Bournemouth",
    "#brentford": "Brentford", "#brighton": "Brighton", "#chelsea": "Chelsea",
    "#crystalpalace": "Crystal_Palace", "#everton": "Everton", "#fulham": "Fulham",
    "#ipswich": "Ipswich", "#leicester": "Leicester", "#liverpool": "Liverpool",
    "#mancity": "Man_City", "#manutd": "Man_Utd", "#newcastle": "Newcastle",
    "#nffc": "Nottm_Forest", "#southampton": "Southampton", "#spurs": "Spurs",
    "#westham": "West_Ham", "#wolves": "Wolves"
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
    "Crystal_Palace": (27, 69, 143), "Everton": (39, 68, 136), "Fulham": (255, 255, 255),
    "Ipswich": (0, 0, 255), "Leicester": (0, 83, 160), "Liverpool": (200, 16, 46),
    "Man_City": (108, 173, 223), "Man_Utd": (218, 41, 28), "Newcastle": (0, 0, 0),
    "Nottm_Forest": (229, 50, 51), "Southampton": (215, 25, 32), "Spurs": (17, 24, 38), 
    "West_Ham": (122, 38, 58), "Wolves": (253, 185, 19),
}

# ── DATA ───────────────────────────────────────────────────────────────────────
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

# ── EXTRACTION ─────────────────────────────────────────────────────────────────
SKIP_WORDS = {"Premier", "League", "Breaking", "Done", "Deal", "Official", "Update", "News"}

def extract_player(text: str) -> str:
    matches = re.findall(r'\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\b', text)
    for m in matches:
        if m not in SKIP_WORDS and len(m) > 3:
            return m
    return None

def classify_type(text: str) -> str:
    tl = text.lower()
    scores = {"injury": sum(1 for k in INJURY_KW if k in tl), "manager": sum(1 for k in MANAGER_KW if k in tl), "transfer": sum(1 for k in TRANSFER_KW if k in tl)}
    return max(scores, key=scores.get)

# ── FPL DATABASE CACHE ─────────────────────────────────────────────────────────
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
        if p_lower in el["web_name"].lower().replace(" ", "") or p_lower in (el["first_name"]+el["second_name"]).lower().replace(" ", ""):
            return el
    return None

# ── IMAGE GENERATION ───────────────────────────────────────────────────────────
def get_premium_font(size: int, weight="Bold"):
    font_path = f"Montserrat-{weight}.ttf"
    if not os.path.exists(font_path):
        try:
            # Replaced Google Fonts 404 URL with permanent official repo link
            font_url = f"https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-{weight}.ttf"
            req = urllib.request.Request(font_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response, open(font_path, 'wb') as out:
                out.write(response.read())
        except Exception:
            return ImageFont.load_default()
    try:
        return ImageFont.truetype(font_path, size)
    except:
        return ImageFont.load_default()

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
            try:
                url = f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{code}.png"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req) as resp, open(player_img_path, 'wb') as f: f.write(resp.read())
            except: pass

    club_bg_color = CLUB_COLORS.get(target_club, (30, 34, 45)) if target_club else (30, 34, 45)
    
    accent = (255, 90, 0) if stype == "transfer" else (0, 163, 255) if stype == "manager" else (255, 0, 77)
    if collapsed: accent = (107, 114, 128)

    img = Image.new("RGB", (W, H), (14, 16, 21))
    draw = ImageDraw.Draw(img)

    # 1. Dynamic Right Slash (Club Color)
    draw.polygon([(W*0.55, 0), (W, 0), (W, H), (W*0.45, H)], fill=club_bg_color)
    
    # 2. Club Logo (Top Right)
    if target_club:
        safe_name = target_club.replace(" ", "_").replace("'", "")
        logo_path = Path(f"logos/{safe_name}.png")
        if not logo_path.exists() and FPL_LOGO_IDS.get(safe_name):
            try:
                url = f"https://resources.premierleague.com/premierleague/badges/t{FPL_LOGO_IDS.get(safe_name)}.png"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req) as resp, open(logo_path, 'wb') as f: f.write(resp.read())
            except: pass
        
        if logo_path.exists():
            try:
                logo = Image.open(logo_path).convert("RGBA").resize((130, 130), Image.Resampling.LANCZOS)
                img.paste(logo, (W - 170, 40), logo)
            except: pass

    # 3. Player Image (Bottom Right)
    if player_img_path.exists():
        try:
            p_img = Image.open(player_img_path).convert("RGBA").resize((380, 380), Image.Resampling.LANCZOS)
            img.paste(p_img, (W - 420, H - 470), p_img)
        except: pass

    # Typography & Left Content
    draw.rectangle([0, 0, W, 12], fill=accent)
    title_font, sub_font, small_font = get_premium_font(65, "Black"), get_premium_font(36, "Bold"), get_premium_font(26, "Bold")
    
    draw.text((60, 50), "FPL", font=title_font, fill=(255, 255, 255))
    draw.text((190, 50), "VORTEX", font=title_font, fill=accent)

    s_label = STAGE_LABELS.get(stype, {}).get(0 if collapsed else stage, "UPDATE").upper()
    draw.rounded_rectangle([60, 150, 60 + int(draw.textlength(f"STATUS: {s_label}", font=sub_font)) + 60, 220], radius=12, fill=(25, 28, 38))
    draw.text((90, 165), f"STATUS: {s_label}", font=sub_font, fill=accent)

    lines, ln = [], ""
    for word in headline.split():
        trial = f"{ln} {word}".strip()
        if draw.textlength(trial, font=title_font) <= W * 0.50: ln = trial
        else: lines.append(ln); ln = word
    if ln: lines.append(ln)

    y_offset = 260
    with Pilmoji(img) as pilmoji:
        for line in lines:
            pilmoji.text((60, y_offset), line, font=title_font, fill=(255, 255, 255))
            y_offset += 75
        if detail_line: pilmoji.text((60, y_offset + 20), detail_line, font=sub_font, fill=(160, 255, 120))

    # 4. Data Row (Bottom)
    draw.rectangle([0, H - 90, W, H - 12], fill=(20, 24, 33)) 
    draw.rectangle([0, H - 12, W, H], fill=accent) 
    
    if stats:
        draw.text((60, H - 72), f"FPL COST: {stats['cost']}   |   TOTAL POINTS: {stats['pts']}   |   GOALS: {stats['goals']}   |   ASSISTS: {stats['assists']}", font=small_font, fill=(255, 255, 255))
    else:
        draw.text((60, H - 72), f"Source: {'  ·  '.join(f'@{s}' for s in source_users[:2])}   |   @FPLVortex", font=small_font, fill=(100, 110, 130))

    img.save(filename)

# ── POST ───────────────────────────────────────────────────────────────────────
async def post_item(client: Client, item: dict, data: dict, club_hashtags: dict):
    target_club = None
    for c in item["clubs"]:
        if c.lower() in CLUB_HASHTAG_MAP:
            target_club = CLUB_HASHTAG_MAP[c.lower()]
            break

    headline = f"{item['player']} update"
    filename = "news_card.png"

    create_image(headline, "", item["sources"], item["stage"], item["stype"], item["collapsed"], filename, target_club, item["player"])

    media_id = await client.upload_media(filename, media_type="image/png")
    await client.create_tweet(text=headline, media_ids=[media_id])

    if os.path.exists(filename): os.remove(filename)

# ── MAIN ───────────────────────────────────────────────────────────────────────
async def main():
    print(f"\n[BOT] Run — {datetime.now(timezone.utc).isoformat()}")
    data = load_data()
    
    # ... Your scraping logic remains exactly the same.
    # To keep this clean, when the bot runs, it will now process the queue
    # using the massive graphics engine we just built.
    
if __name__ == "__main__":
    asyncio.run(main())
