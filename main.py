from clubs_cache import get_club_data
import os
import re
import json
import asyncio
import requests
import xml.etree.ElementTree as ET
import urllib.request
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

# ── JOURNALISTS ────────────────────────────────────────────────────────────────
JOURNALISTS = [
    "FabrizioRomano", "David_Ornstein", "Plettigoal", "Santi_J_M",
    "sistoney67", "MatteoMoretto_", "AlfredoPedulla", "cfalk_news",
    "BenJacobs", "GianlucaDiMarzio",
]

# ── NITTER INSTANCES (fallback list) ──────────────────────────────────────────
NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.catsarch.com",
]

# ── KEYWORDS ───────────────────────────────────────────────────────────────────
TRANSFER_KW = ["transfer", "sign", "deal", "fee", "bid", "move", "loan",
                "contract", "agree", "confirm", "medical", "official", "close",
                "interest", "talks", "negotiat", "personal terms", "done",
                "approach", "target", "want", "keen", "pursuit", "swap"]

INJURY_KW   = ["injury", "injured", "ruled out", "scan", "hamstring", "knee",
                "muscle", "fracture", "surgery", "sidelined", "doubt",
                "concern", "knock", "fitness", "unavailable", "recovery"]

MANAGER_KW  = ["sack", "appoint", "manager", "coach", "resign", "dismiss",
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

# ── STAGE LABELS ──────────────────────────────────────────────────────
STAGE_LABELS = {
    "transfer": {
        0: "DEAL COLLAPSED",
        1: "TRANSFER TALKS",
        2: "AGREEMENT REACHED",
        3: "CONTRACT SIGNED",
        4: "TRANSFER CONFIRMED",
    },
    "manager": {
        0: "DEAL COLLAPSED",
        1: "MANAGERIAL CHANGE",
        2: "MANAGER TALKS",
        3: "TERMS AGREED",
        4: "OFFICIALLY APPOINTED",
    },
    "injury": {
        0: "INJURY UPDATE",
        1: "INJURY CONCERN",
        2: "SCAN AWAITED",
        3: "RULED OUT",
        4: "FIT TO RETURN",
    },
}

# ── COUNTRY + LEAGUE HASHTAGS ──────────────────────────────────────────────────
COUNTRY_HASHTAGS = {
    "england":     "#England",
    "france":      "#France",
    "spain":       "#Spain",
    "germany":     "#Germany",
    "italy":       "#Italy",
    "portugal":    "#Portugal",
    "brazil":      "#Brazil",
    "argentina":   "#Argentina",
    "netherlands": "#Netherlands",
    "belgium":     "#Belgium",
}

LEAGUE_HASHTAGS = {
    "premier league":   ["#PremierLeague", "#PL"],
    "la liga":          ["#LaLiga"],
    "serie a":          ["#SerieA"],
    "bundesliga":       ["#Bundesliga"],
    "ligue 1":          ["#Ligue1"],
    "champions league": ["#UCL"],
    "europa league":    ["#UEL"],
}

# ── DATA ───────────────────────────────────────────────────────────────────────
def load_data() -> dict:
    if POSTED_FILE.exists():
        with open(POSTED_FILE) as f:
            return json.load(f)
    return {"daily": {"date": "", "count": 0, "limit": 17},
            "stories": {}, "posted_ids": []}

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
    "Paris", "Saint", "Germain", "Sporting", "Porto", "Benfica", "Ajax",
    "Villa", "City", "United", "Spurs", "Forest", "Athletic", "Atletico"
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
    m = re.search(
        r'[€£\$][\d\.]+[Mm]?|[\d\.]+\s*[Mm]illion|[\d\.]+[Mm]\s*[€£\$]',
        text)
    if m:
        return m.group(0).strip().upper().replace("MILLION", "M")
    return None

def extract_contract(text: str) -> str:
    m = re.search(r'(\d)[- ]year|until\s+20(\d\d)|\b(\d)\s+years\b', text, re.I)
    if not m:
        return None
    if m.group(1):
        return f"{m.group(1)}-year deal"
    if m.group(2):
        return f"until 20{m.group(2)}"
    if m.group(3):
        return f"{m.group(3)}-year deal"
    return None

def extract_country(text: str) -> str:
    tl = text.lower()
    for country, tag in COUNTRY_HASHTAGS.items():
        if country in tl:
            return tag
    return None

def extract_league(text: str) -> list:
    tl = text.lower()
    tags = []
    for league, htags in LEAGUE_HASHTAGS.items():
        if league in tl:
            tags.extend(htags)
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
        if any(k in tl for k in kw[stage]):
            return stage
    return 1

def build_story_key(player: str, club: str, stype: str) -> str:
    p = (player or "unknown").lower().replace(" ", "_")
    c = (club   or "unknown").lower().replace(" ", "_")
    return f"{p}_{c}_{stype}"

# ── PROGRESSION GATE ───────────────────────────────────────────────────────────
def should_post(data: dict, key: str, new_stage: int,
                collapsed: bool) -> tuple[bool, str]:
    existing = data["stories"].get(key)

    if collapsed:
        if existing and existing["status"] == "active":
            return True, "collapse"
        return False, "already_collapsed"

    if not existing:
        return True, "new"

    if existing["status"] == "collapsed":
        return False, "story_collapsed"

    current = existing["stage"]
    if new_stage <= current:
        return False, "no_progression"

    return True, "progression"

# ── HEADLINE BUILDER ───────────────────────────────────────────────────────────
def build_headline(player: str, clubs: list, stage: int, stype: str,
                   fee: str, contract: str, collapsed: bool) -> tuple[str, str]:
    
    FULL_NAMES = {
        "Che": "Chelsea", "Not": "Nott'm Forest", "Ful": "Fulham", 
        "Mci": "Man City", "Mun": "Man Utd", "Ars": "Arsenal", 
        "Liv": "Liverpool", "Tot": "Spurs", "New": "Newcastle",
        "Ast": "Aston Villa", "Bha": "Brighton", "Bre": "Brentford"
    }
    
    p = player or "Player"
    raw_club = clubs[1].title() if len(clubs) > 1 else clubs[0].title() if clubs else "Club"
    to_club = FULL_NAMES.get(raw_club, raw_club)

    details = []
    if fee:
        details.append(f"💰 {fee}")
    if contract:
        details.append(f"⏱️ {contract}")
    detail_line = " | ".join(details) if details else ""

    if collapsed:
        return f"{p} ❌ Deal to {to_club} collapsed", detail_line

    if stype == "transfer":
        texts = {
            1: f"👀 {p} in talks with {to_club}",
            2: f"🤝 {p} reaches agreement with {to_club}",
            3: f"📝 {p} signs contract with {to_club}",
            4: f"🚨 {p} officially joins {to_club} ✅",
        }
    elif stype == "manager":
        texts = {
            1: f"👔 {p} emerging as {to_club} target",
            2: f"🗣️ {p} in talks to become {to_club} manager",
            3: f"✍️ {p} agrees terms with {to_club}",
            4: f"🚨 {p} officially appointed at {to_club} ✅",
        }
    else:
        texts = {
            1: f"⚠️ {p} injury concern — fitness in doubt",
            2: f"🏥 {p} undergoes scan — diagnosis awaited",
            3: f"🤕 {p} ruled out — return date unknown",
            4: f"💪 {p} fit again — available for selection ✅",
        }
    return texts.get(stage, f"{p} update"), detail_line

# ── HASHTAGS ───────────────────────────────────────────────────────────────────
def build_hashtags(stype: str, clubs: list, text: str,
                   club_hashtags: dict, pl_clubs: set) -> str:
    tags = []

    if stype == "transfer":
        tags.append("#TransferNews")
    elif stype == "manager":
        tags.append("#ManagerNews")
    else:
        tags.append("#InjuryNews")

    tags.append("#Football")

    for club in clubs[:2]:
        ht = club_hashtags.get(club)
        if ht and ht not in tags:
            tags.append(ht)

    if any(c in pl_clubs for c in clubs):
        if "#PremierLeague" not in tags:
            tags.append("#PremierLeague")

    country_tag = extract_country(text)
    if country_tag and country_tag not in tags:
        tags.append(country_tag)

    for lt in extract_league(text):
        if lt not in tags:
            tags.append(lt)

    return " ".join(tags[:6])

# ── IMAGE ──────────────────────────────────────────────────────────────────────
def get_premium_font(size: int, weight="Bold"):
    font_path = f"Montserrat-{weight}.ttf"
    if not os.path.exists(font_path):
        font_url = f"https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat-{weight}.ttf"
        urllib.request.urlretrieve(font_url, font_path)
    return ImageFont.truetype(font_path, size)

def create_image(headline: str, detail_line: str, source_users: list,
                 stage: int, stype: str, collapsed: bool, filename: str):
    
    W, H = 1200, 675
    stage_key = 0 if collapsed else stage

    if stype == "transfer":
        accent = (255, 90, 0)   
        tag = "TRANSFER NEWS"
    elif stype == "manager":
        accent = (0, 163, 255)  
        tag = "MANAGER NEWS"
    else:
        accent = (255, 0, 77)   
        tag = "INJURY UPDATE"

    if collapsed:
        accent = (107, 114, 128)

    img = Image.new("RGB", (W, H), (14, 16, 21))
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, W, 12], fill=accent)
    draw.rectangle([0, H - 12, W, H], fill=accent)

    title_font = get_premium_font(70, "Black")
    sub_font   = get_premium_font(40, "Bold")
    small_font = get_premium_font(28, "Bold")

    draw.text((60, 50), "FPL", font=title_font, fill=(255, 255, 255))
    draw.text((215, 50), "VORTEX", font=title_font, fill=accent)

    tag_w = int(draw.textlength(tag, font=small_font)) + 40
    draw.rounded_rectangle([W - tag_w - 60, 60, W - 60, 110], radius=8, fill=accent)
    draw.text((W - tag_w - 40, 68), tag, font=small_font, fill=(255, 255, 255))

    status_label = STAGE_LABELS.get(stype, {}).get(stage_key, "UPDATE").upper()
    draw.rounded_rectangle([60, 170, W - 60, 240], radius=12, fill=(25, 28, 38))
    draw.text((90, 185), f"STATUS: {status_label}", font=sub_font, fill=accent)

    words = headline.split()
    lines, ln = [], ""
    for word in words:
        trial = f"{ln} {word}".strip()
        if draw.textlength(trial, font=title_font) <= W - 140:
            ln = trial
        else:
            lines.append(ln)
            ln = word
    if ln: lines.append(ln)

    y_offset = 280
    with Pilmoji(img) as pilmoji:
        for line in lines:
            pilmoji.text((60, y_offset), line, font=title_font, fill=(255, 255, 255))
            y_offset += 85

        if detail_line:
            pilmoji.text((60, y_offset + 20), detail_line, font=sub_font, fill=(160, 255, 120)) 

    sources = "  ·  ".join(f"@{s}" for s in source_users[:2])
    draw.text((60, H - 60), f"Source: {sources}", font=small_font, fill=(100, 110, 130))

    img.save(filename)

# ── QUEUE FILES ────────────────────────────────────────────────────────────────
def save_pending(item: dict):
    slug = re.sub(r'[^a-z0-9_]', '', item["key"]) + f"_s{item['stage']}"
    path = PENDING_DIR / f"{slug}.json"
    with open(path, "w") as f:
        json.dump(item, f, indent=2)

def move_to_posted(item: dict):
    slug = re.sub(r'[^a-z0-9_]', '', item["key"]) + f"_s{item['stage']}"
    src  = PENDING_DIR / f"{slug}.json"
    dst  = POSTED_DIR  / f"{slug}.json"
    if src.exists():
        src.rename(dst)
    else:
        with open(dst, "w") as f:
            json.dump(item, f, indent=2)

# ── SCRAPE VIA NITTER RSS ──────────────────────────────────────────────────────
def get_nitter_tweets(username: str) -> list:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; RSS reader)"}
    for instance in NITTER_INSTANCES:
        try:
            url = f"{instance}/{username}/rss"
            r   = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            root  = ET.fromstring(r.content)
            items = root.findall(".//item")
            tweets = []
            for item in items[:8]:
                link  = item.find("link")
                title = item.find("title")
                desc  = item.find("description")
                if link is None:
                    continue
                tid  = link.text.strip().split("/")[-1].split("#")[0]
                text = ""
                if desc is not None and desc.text:
                    clean = re.sub(r'<[^>]+>', '', desc.text)
                    text  = clean.strip()
                if not text and title is not None and title.text:
                    text = title.text.strip()
                if tid and text:
                    tweets.append({"id": tid, "text": text})
            if tweets:
                print(f"  ✅ @{username}: {len(tweets)} tweets via {instance}")
                return tweets
        except Exception as e:
            continue
    print(f"  [WARN] @{username}: all nitter instances failed")
    return []

async def scrape(data: dict, club_hashtags: dict) -> list:
    story_map: dict[str, dict] = {}

    for username in JOURNALISTS:
        tweets = get_nitter_tweets(username)

        for tweet in tweets:
            tid  = tweet["id"]
            text = tweet["text"]

            if tid in data["posted_ids"]:
                continue

            tl = text.lower()
            has_signal = (
                any(k in tl for k in TRANSFER_KW) or
                any(k in tl for k in INJURY_KW)   or
                any(k in tl for k in MANAGER_KW)
            )
            if not has_signal:
                continue

            collapsed = is_collapse(text)
            stype     = classify_type(text)
            stage     = 0 if collapsed else get_stage(text, stype)
            player    = extract_player(text)
            clubs     = extract_clubs(text, club_hashtags)
            fee       = extract_fee(text)
            contract  = extract_contract(text)
            key       = build_story_key(player, clubs[0] if clubs else None, stype)

            ok, reason = should_post(data, key, stage, collapsed)
            if not ok:
                continue

            if key in story_map:
                existing = story_map[key]
                if username not in existing["sources"]:
                    existing["sources"].append(username)
                if fee and not existing["fee"]:
                    existing["fee"] = fee
                if contract and not existing["contract"]:
                    existing["contract"] = contract
                if stage > existing["stage"]:
                    existing["stage"] = stage
            else:
                story_map[key] = {
                    "id":        tid,
                    "key":       key,
                    "text":      text,
                    "sources":   [username],
                    "stype":     stype,
                    "stage":     stage,
                    "collapsed": collapsed,
                    "player":    player,
                    "clubs":     clubs,
                    "fee":       fee,
                    "contract":  contract,
                    "reason":    reason,
                }

        await asyncio.sleep(1)

    return sorted(
        story_map.values(),
        key=lambda x: -(1 if x["collapsed"] else x["stage"]),
    )

# ── POST ───────────────────────────────────────────────────────────────────────
async def post_item(client: Client, item: dict, data: dict,
                    club_hashtags: dict, pl_clubs: set):
    headline, detail_line = build_headline(
        item["player"], item["clubs"], item["stage"],
        item["stype"], item["fee"], item["contract"], item["collapsed"],
    )
    hashtags = build_hashtags(
        item["stype"], item["clubs"], item["text"], club_hashtags, pl_clubs
    )
    filename = "news_card.png"

    create_image(
        headline, detail_line, item["sources"],
        item["stage"], item["stype"], item["collapsed"], filename,
    )

    media_id = await client.upload_media(filename, media_type="image/png")

    body = headline
    if detail_line:
        body += f"\n{detail_line}"
    body += f"\n\n{hashtags}"
    if len(body) > 280:
        body = body[:277] + "..."

    await client.create_tweet(text=body, media_ids=[media_id])

    if os.path.exists(filename):
        os.remove(filename)

    data["posted_ids"].append(item["id"])
    status   = "collapsed" if item["collapsed"] else "active"
    existing = data["stories"].get(item["key"], {})
    data["stories"][item["key"]] = {
        "stage":        item["stage"],
        "player":       item["player"],
        "clubs":        item["clubs"],
        "type":         item["stype"],
        "fee":          item["fee"] or existing.get("fee"),
        "contract":     item["contract"] or existing.get("contract"),
        "status":       status,
        "sources":      item["sources"],
        "first_seen":   existing.get("first_seen",
                                     datetime.now(timezone.utc).isoformat()),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    increment_daily(data)
    save_data(data)
    move_to_posted(item)

    print(f"  ✅ [{item['stype'].upper()} S{item['stage']}] {headline}")

# ── MAIN ───────────────────────────────────────────────────────────────────────
async def main():
    print(f"\n[BOT] Run — {datetime.now(timezone.utc).isoformat()}")

    club_data     = get_club_data()
    CLUB_HASHTAGS = club_data["club_hashtags"]
    PL_CLUBS      = set(club_data["pl_clubs"])

    data = load_data()

    if not check_daily_limit(data):
        print(f"[BOT] Daily limit reached ({data['daily']['limit']}). Stopping.")
        return

    queue = await scrape(data, CLUB_HASHTAGS)

    if not queue:
        print("[BOT] Nothing to post this run.")
        return

    for item in queue:
        save_pending(item)

    post_client = Client("en-US")
    post_client.set_cookies({
        "auth_token": X_POST_AUTH_TOKEN,
        "ct0":        X_POST_CT0_TOKEN,
    })

    remaining = data["daily"]["limit"] - data["daily"]["count"]
    to_post   = queue[:min(3, remaining)]

    for i, item in enumerate(to_post):
        try:
            await post_item(post_client, item, data, CLUB_HASHTAGS, PL_CLUBS)
        except Exception as e:
            print(f"  [ERROR] {item['key']}: {e}")
        if i < len(to_post) - 1:
            print("  [BOT] Waiting 60s...")
            await asyncio.sleep(60)

    left = [x for x in queue if x not in to_post]
    if left:
        print(f"[BOT] {len(left)} pending for next run or manual post.")

    print(f"[BOT] Done. Daily count: {data['daily']['count']}/{data['daily']['limit']}")


if __name__ == "__main__":
    asyncio.run(main())
