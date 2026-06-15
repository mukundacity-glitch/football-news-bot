import os
import json
import asyncio

from PIL import Image, ImageDraw, ImageFont
from twikit import Client

# --- CONFIGURATION (loaded from GitHub Secrets) ---
# BURNER account cookies — used only for reading/scraping
X_AUTH_TOKEN = os.getenv("X_AUTH_TOKEN")
X_CT0_TOKEN  = os.getenv("X_CT0_TOKEN")

# @FPLVortex's OWN cookies — used only for posting
X_POST_AUTH_TOKEN = os.getenv("X_POST_AUTH_TOKEN")
X_POST_CT0_TOKEN  = os.getenv("X_POST_CT0_TOKEN")

JOURNALISTS = [
    "FabrizioRomano", "Plettigoal", "Santi_J_M", "David_Ornstein",
    "sistoney67", "PaulJoyce_", "MatteoMoretto_", "AlfredoPedulla",
    "cfalk_news", "FabrizioHawkins",
]

KEYWORDS = ["here we go", "agreement reached", "signed", "medical",
            "appointed", "sacked", "confirmed", "done deal"]

POSTED_NEWS_FILE = "posted_news.json"
MAX_POSTS_PER_RUN = 3

# club name (lowercase) -> hashtag
CLUB_HASHTAGS = {
    "manchester united": "#MUFC", "man utd": "#MUFC", "man united": "#MUFC",
    "arsenal": "#AFC",
    "chelsea": "#CFC",
    "liverpool": "#LFC",
    "manchester city": "#MCFC", "man city": "#MCFC",
    "tottenham": "#THFC", "spurs": "#THFC",
    "newcastle": "#NUFC",
    "aston villa": "#AVFC",
    "west ham": "#WHUFC",
    "everton": "#EFC",
    "brighton": "#BHAFC",
    "wolves": "#WWFC",
    "barcelona": "#FCBarcelona",
    "real madrid": "#RealMadrid",
    "atletico madrid": "#Atleti",
    "juventus": "#Juve",
    "ac milan": "#ACMilan",
    "inter milan": "#Inter",
    "napoli": "#Napoli",
    "psg": "#PSG",
    "bayern": "#FCBayern",
    "borussia dortmund": "#BVB",
}

PL_CLUBS = {
    "manchester united", "man utd", "man united", "arsenal", "chelsea",
    "liverpool", "manchester city", "man city", "tottenham", "spurs",
    "newcastle", "aston villa", "west ham", "everton", "brighton", "wolves",
}


# --- dedup helpers ---
def load_posted():
    if not os.path.exists(POSTED_NEWS_FILE):
        return []
    with open(POSTED_NEWS_FILE) as f:
        return json.load(f)

def save_posted(posted):
    with open(POSTED_NEWS_FILE, "w") as f:
        json.dump(posted[-500:], f)

def text_key(text):
    return " ".join(text.lower().split())[:80]

def already_seen(tweet, posted):
    tid = str(tweet.id)
    key = text_key(tweet.text)
    return any(p["id"] == tid or p["key"] == key for p in posted)


# --- hashtags ---
def build_hashtags(text, max_club_tags=3):
    text_lower = text.lower()
    tags, is_pl = [], False
    for club, tag in CLUB_HASHTAGS.items():
        if club in text_lower and tag not in tags:
            tags.append(tag)
            if club in PL_CLUBS:
                is_pl = True
        if len(tags) >= max_club_tags:
            break
    base = ["#TransferNews", "#FPL"]
    if is_pl:
        base.append("#PremierLeague")
    return " ".join(base + tags)


# --- 1. READ via burner account ---
async def find_new_items(posted):
    print("🔍 Scraping journalists via twikit (burner)...")
    read_client = Client("en-US")
    read_client.set_cookies({"auth_token": X_AUTH_TOKEN, "ct0": X_CT0_TOKEN})

    found = []
    for username in JOURNALISTS:
        try:
            user = await read_client.get_user_by_screen_name(username)
            tweets = await read_client.get_user_tweets(user.id, "Tweets", count=3)
        except Exception as e:
            print(f"  error reading {username}: {e}")
            continue

        for tweet in tweets:
            text_lower = tweet.text.lower()
            if any(word in text_lower for word in KEYWORDS) and not already_seen(tweet, posted):
                found.append({"text": tweet.text, "user": user.screen_name, "id": str(tweet.id)})
                # mark as seen immediately so a near-duplicate from another
                # journalist in this same run isn't queued twice
                posted.append({"id": str(tweet.id), "key": text_key(tweet.text)})
            if len(found) >= MAX_POSTS_PER_RUN:
                return found
    return found


# --- 2. IMAGE (1200x675 PNG) ---
def create_image(news_text, source_user, filename):
    print(f"🎨 Creating image {filename}...")
    img = Image.new("RGB", (1200, 675), color=(10, 10, 30))
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 50)
        font_body  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 35)
    except OSError:
        font_title = ImageFont.load_default()
        font_body  = ImageFont.load_default()

    draw.text((50, 50), "BREAKING NEWS", font=font_title, fill=(255, 215, 0))

    max_width = 1100
    lines, line = [], ""
    for word in news_text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font_body) <= max_width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)

    y = 150
    for line in lines[:8]:
        draw.text((50, y), line, font=font_body, fill=(255, 255, 255))
        y += 50

    draw.text((50, 600), f"Source: @{source_user}", font=font_body, fill=(200, 200, 200))

    img.save(filename)
    return filename


# --- 3. POST via twikit using @FPLVortex's own cookies (free) ---
async def post_item(post_client, news_text, source_user, image_path):
    print("🚀 Posting to X (twikit, free)...")
    media_id = await post_client.upload_media(image_path)

    hashtags = build_hashtags(news_text)
    body = news_text[:180].strip()
    status = f"🚨 {body}\n\n📸 Source: @{source_user}\n{hashtags}"

    await post_client.create_tweet(text=status, media_ids=[media_id])
    print("✅ Posted.")


# --- MAIN ---
async def main():
    posted = load_posted()
    items = await find_new_items(posted)

    if not items:
        print("No new transfer news found.")
        return

    post_client = Client("en-US")
    post_client.set_cookies({"auth_token": X_POST_AUTH_TOKEN, "ct0": X_POST_CT0_TOKEN})

    for i, news in enumerate(items):
        img_file = create_image(news["text"], news["user"], f"news_image_{i}.png")
        await post_item(post_client, news["text"], news["user"], img_file)
        if i < len(items) - 1:
            await asyncio.sleep(5)  # small gap between posts

    save_posted(posted)  # the workflow step commits this back to the repo


if __name__ == "__main__":
    asyncio.run(main())
