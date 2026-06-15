import os
import json
import asyncio
from PIL import Image, ImageDraw, ImageFont
from twikit import Client

# ── SECRETS ────────────────────────────────────────────────────────────────────
X_AUTH_TOKEN      = os.getenv("X_AUTH_TOKEN")       # burner - read only
X_CT0_TOKEN       = os.getenv("X_CT0_TOKEN")
X_POST_AUTH_TOKEN = os.getenv("X_POST_AUTH_TOKEN")  # FPLVortex - post only
X_POST_CT0_TOKEN  = os.getenv("X_POST_CT0_TOKEN")

# ── CONFIG ─────────────────────────────────────────────────────────────────────
JOURNALISTS = [
    "FabrizioRomano", "Plettigoal", "Santi_J_M", "David_Ornstein",
    "sistoney67", "PaulJoyce_", "MatteoMoretto_", "AlfredoPedulla",
    "cfalk_news", "FabrizioHawkins",
]

KEYWORDS = [
    "here we go", "agreement reached", "signed", "medical",
    "appointed", "sacked", "confirmed", "done deal",
]

POSTED_NEWS_FILE  = "posted_news.json"
MAX_POSTS_PER_RUN = 3

# ── HASHTAG MAP ────────────────────────────────────────────────────────────────
CLUB_HASHTAGS = {
    "manchester united": "#MUFC",   "man utd":  "#MUFC",   "man united": "#MUFC",
    "arsenal":           "#AFC",
    "chelsea":           "#CFC",
    "liverpool":         "#LFC",
    "manchester city":   "#MCFC",   "man city": "#MCFC",
    "tottenham":         "#THFC",   "spurs":    "#THFC",
    "newcastle":         "#NUFC",
    "aston villa":       "#AVFC",
    "west ham":          "#WHUFC",
    "everton":           "#EFC",
    "brighton":          "#BHAFC",
    "wolves":            "#WWFC",
    "barcelona":         "#FCBarcelona",
    "real madrid":       "#RealMadrid",
    "atletico madrid":   "#Atleti",
    "juventus":          "#Juve",
    "ac milan":          "#ACMilan",
    "inter milan":       "#Inter",
    "napoli":            "#Napoli",
    "psg":               "#PSG",
    "bayern":            "#FCBayern",
    "borussia dortmund": "#BVB",    "bvb": "#BVB",
    "chelsea":           "#CFC",
    "rangers":           "#RangersFC",
    "celtic":            "#CelticFC",
}

PL_CLUBS = {
    "manchester united", "man utd", "man united", "arsenal", "chelsea",
    "liverpool", "manchester city", "man city", "tottenham", "spurs",
    "newcastle", "aston villa", "west ham", "everton", "brighton", "wolves",
}

# ── DEDUP ──────────────────────────────────────────────────────────────────────
def load_posted():
    if not os.path.exists(POSTED_NEWS_FILE):
        return []
    with open(POSTED_NEWS_FILE) as f:
        return json.load(f)

def save_posted(posted):
    with open(POSTED_NEWS_FILE, "w") as f:
        json.dump(posted[-500:], f, indent=2)

def text_key(text: str) -> str:
    return " ".join(text.lower().split())[:80]

def already_seen(tweet, posted: list) -> bool:
    tid = str(tweet.id)
    key = text_key(tweet.text)
    return any(p["id"] == tid or p["key"] == key for p in posted)

# ── HASHTAGS ───────────────────────────────────────────────────────────────────
def build_hashtags(text: str, max_club_tags: int = 3) -> str:
    text_lower = text.lower()
    tags, is_pl = [], False
    for club, tag in CLUB_HASHTAGS.items():
        if club in text_lower and tag not in tags:
            tags.append(tag)
            if club in PL_CLUBS:
                is_pl = True
        if len(tags) >= max_club_tags:
            break
    base = ["#TransferNews", "#Football"]
    if is_pl:
        base.extend(["#PremierLeague", "#FPL"])
    return " ".join(base + tags)

# ── SCRAPE (burner account) ────────────────────────────────────────────────────
async def find_new_items(posted: list) -> list:
    print("Scraping journalists via burner account...")
    client = Client("en-US")
    client.set_cookies({"auth_token": X_AUTH_TOKEN, "ct0": X_CT0_TOKEN})

    found = []
    for username in JOURNALISTS:
        try:
            user   = await client.get_user_by_screen_name(username)
            tweets = await client.get_user_tweets(user.id, "Tweets", count=5)
        except Exception as e:
            print(f"  error reading @{username}: {e}")
            continue

        for tweet in tweets:
            text_lower = tweet.text.lower()
            if any(kw in text_lower for kw in KEYWORDS) and not already_seen(tweet, posted):
                item = {
                    "text": tweet.text,
                    "user": username,
                    "id":   str(tweet.id),
                }
                found.append(item)
                # mark seen immediately — avoids same news from two journalists
                posted.append({"id": str(tweet.id), "key": text_key(tweet.text)})

        if len(found) >= MAX_POSTS_PER_RUN:
            break

        await asyncio.sleep(2)  # polite delay between journalist requests

    return found[:MAX_POSTS_PER_RUN]

# ── IMAGE ──────────────────────────────────────────────────────────────────────
def create_image(news_text: str, source_user: str, filename: str) -> str:
    print(f"  Creating image: {filename}")
    W, H = 1200, 675
    img  = Image.new("RGB", (W, H), color=(12, 12, 28))
    draw = ImageDraw.Draw(img)

    # accent bar at top
    draw.rectangle([(0, 0), (W, 6)], fill=(220, 38, 38))

    try:
        font_label  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_body   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",      38)
        font_source = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",      26)
    except OSError:
        font_label  = ImageFont.load_default()
        font_body   = font_label
        font_source = font_label

    # BREAKING badge
    draw.text((50, 30), "BREAKING TRANSFER NEWS", font=font_label, fill=(220, 38, 38))

    # word-wrap body text
    max_w  = W - 100
    lines, line = [], ""
    for word in news_text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font_body) <= max_w:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)

    y = 100
    for ln in lines[:9]:
        draw.text((50, y), ln, font=font_body, fill=(255, 255, 255))
        y += 58

    # source credit
    draw.text((50, H - 55), f"Source: @{source_user}  |  @FPLVortex", font=font_source, fill=(160, 160, 175))
    # bottom accent bar
    draw.rectangle([(0, H - 6), (W, H)], fill=(220, 38, 38))

    img.save(filename)
    return filename

# ── POST (FPLVortex account) ───────────────────────────────────────────────────
async def post_item(client: Client, news_text: str, source_user: str, image_path: str):
    print(f"  Posting: {news_text[:60]}...")

    # FIX: twikit v2 upload_media requires media_type
    media_id = await client.upload_media(image_path, media_type="image/png")

    hashtags = build_hashtags(news_text)
    body     = news_text[:200].strip()
    status   = f"🚨 {body}\n\n📸 @{source_user}\n{hashtags}"

    await client.create_tweet(text=status, media_ids=[media_id])
    print("  Posted successfully.")

# ── MAIN ───────────────────────────────────────────────────────────────────────
async def main():
    posted = load_posted()
    items  = await find_new_items(posted)

    if not items:
        print("No new transfer news found this run.")
        return

    print(f"Found {len(items)} new item(s) to post.")

    post_client = Client("en-US")
    post_client.set_cookies({
        "auth_token": X_POST_AUTH_TOKEN,
        "ct0":        X_POST_CT0_TOKEN,
    })

    for i, news in enumerate(items):
        img_file = f"news_image_{i}.png"
        try:
            create_image(news["text"], news["user"], img_file)
            await post_item(post_client, news["text"], news["user"], img_file)
        except Exception as e:
            print(f"  Failed to post item {i}: {e}")
        finally:
            if os.path.exists(img_file):
                os.remove(img_file)   # clean up temp image

        if i < len(items) - 1:
            await asyncio.sleep(8)  # gap between posts

    save_posted(posted)
    print("Done. posted_news.json updated.")

if __name__ == "__main__":
    asyncio.run(main())
