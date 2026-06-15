from clubs_cache import get_club_data
import os
import re
import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from twikit import Client

# ── SECRETS ────────────────────────────────────────────────────────────────────
X_AUTH_TOKEN      = os.getenv("X_AUTH_TOKEN")
X_CT0_TOKEN       = os.getenv("X_CT0_TOKEN")
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

# ── STAGE LABELS + COLORS ──────────────────────────────────────────────────────
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

STAGE_COLORS = {
    0: (107, 114, 128),
    1: (59,  130, 246),
    2: (245, 158,  11),
    3: (239,  68,  68),
    4: (34,  197,  94),
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
    return m.group(0).strip() if m else None

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
    p       = player or "Player"
    to_club = clubs[1].title() if len(clubs) > 1 else clubs[0].title() if clubs else "Club"

    details = []
    if fee:
        details.append(f"Fee: {fee}")
    if contract:
        details.append(contract)
    detail_line = " | ".join(details) if details else ""

    if collapsed:
        return f"{p} — {to_club} deal collapsed ❌", detail_line

    if stype == "transfer":
        texts = {
            1: f"{p} in talks with {to_club}",
            2: f"{p} reaches agreement with {to_club}",
            3: f"{p} signs contract with {to_club}",
            4: f"{p} officially joins {to_club} ✅",
        }
    elif stype == "manager":
        texts = {
            1: f"{p} emerging as {to_club} managerial target",
            2: f"{p} in talks to become {to_club} manager",
            3: f"{p} agrees terms with {to_club}",
            4: f"{p} officially appointed as {to_club} manager ✅",
        }
    else:
        texts = {
            1: f"{p} injury concern — fitness in doubt",
            2: f"{p} undergoes scan — diagnosis awaited",
            3: f"{p} ruled out — return date unknown",
            4: f"{p} fit again — available for selection ✅",
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
def load_font(size: int):
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()

def create_image(headline: str, detail_line: str, source_users: list,
                 stage: int, stype: str, collapsed: bool, filename: str):
    W, H      = 1200, 675
    stage_key = 0 if collapsed else stage
    accent    = STAGE_COLORS[stage_key]
    BG        = (10, 10, 22)

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    for i in range(H):
        r = int(BG[0] + (20 - BG[0]) * i / H)
        g = int(BG[1] + (22 - BG[1]) * i / H)
        b = int(BG[2] + (38 - BG[2]) * i / H)
        draw.line([(0, i), (W, i)], fill=(r, g, b))

    draw.rectangle([(0, 0),     (W, 7)],     fill=accent)
    draw.rectangle([(0, H - 7), (W, H)],     fill=accent)

    badge_font  = load_font(26)
    badge_label = STAGE_LABELS.get(stype, {}).get(stage_key, "UPDATE")
    bw = int(draw.textlength(badge_label, font=badge_font)) + 44
    draw.rounded_rectangle([48, 28, 48 + bw, 76], radius=8, fill=accent)
    draw.text((68, 38), badge_label, font=badge_font, fill=(255, 255, 255))

    if stype != "injury" and not collapsed:
        dot_font = load_font(30)
        dots = " ".join("●" if i <= stage else "○" for i in range(1, 5))
        dw   = int(draw.textlength(dots, font=dot_font))
        draw.text((W - dw - 50, 36), dots, font=dot_font, fill=accent)

    h_font = load_font(56)
    max_w  = W - 100
    words  = headline.split()
    lines, ln = [], ""
    for word in words:
        trial = f"{ln} {word}".strip()
        if draw.textlength(trial, font=h_font) <= max_w:
            ln = trial
        else:
            if ln:
                lines.append(ln)
            ln = word
    if ln:
        lines.append(ln)

    total_h = len(lines) * 74
    y = (H - total_h) // 2 - 30
    for line in lines:
        tw = int(draw.textlength(line, font=h_font))
        draw.text(((W - tw) // 2, y), line, font=h_font, fill=(255, 255, 255))
        y += 74

    if detail_line:
        d_font = load_font(32)
        dw     = int(draw.textlength(detail_line, font=d_font))
        draw.text(((W - dw) // 2, y + 10), detail_line,
                  font=d_font, fill=accent)

    src_font = load_font(24)
    sources  = "  ·  ".join(f"@{s}" for s in source_users[:2])
    src_text = f"Source: {sources}  |  @FPLVortex"
    draw.text((50, H - 52), src_text, font=src_font, fill=(140, 140, 160))
    ts  = datetime.now(timezone.utc).strftime("%d %b %Y · %H:%M UTC")
    tsw = int(draw.textlength(ts, font=src_font))
    draw.text((W - tsw - 50, H - 52), ts, font=src_font, fill=(140, 140, 160))

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

# ── SCRAPE ─────────────────────────────────────────────────────────────────────
async def scrape(data: dict, club_hashtags: dict) -> list:
    client = Client("en-US")
    client.http.cookies.set("auth_token", X_AUTH_TOKEN, domain=".X.com")
    client.http.cookies.set("ct0", X_CT0_TOKEN, domain=".X.com")

    story_map: dict[str, dict] = {}

    for username in JOURNALISTS:
        try:
            user   = await client.get_user_by_screen_name(username)
            tweets = await client.get_user_tweets(user.id, "Tweets", count=8)
        except Exception as e:
            print(f"  [WARN] @{username}: {e}")
            continue

        for tweet in tweets:
            tid = str(tweet.id)
            if tid in data["posted_ids"]:
                continue

            text = tweet.text
            tl   = text.lower()

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

        await asyncio.sleep(2)

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
    post_client.http.cookies.set("auth_token", X_POST_AUTH_TOKEN, domain=".X.com")
    post_client.http.cookies.set("ct0", X_POST_CT0_TOKEN, domain=".X.com")

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
