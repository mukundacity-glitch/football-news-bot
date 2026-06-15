import os
import json
import asyncio
from PIL import Image, ImageDraw, ImageFont
import tweepy
from twikit import Client

# --- CONFIGURATION ---
# These will be loaded from GitHub Secrets
X_AUTH_TOKEN = os.getenv('X_AUTH_TOKEN')
X_CT0_TOKEN = os.getenv('X_CT0_TOKEN')
X_API_KEY = os.getenv('X_API_KEY')
X_API_SECRET = os.getenv('X_API_SECRET')
X_ACCESS_TOKEN = os.getenv('X_ACCESS_TOKEN')
X_ACCESS_SECRET = os.getenv('X_ACCESS_SECRET')

# Journalists to monitor (Fastest sources)
JOURNALISTS = [
    "FabrizioRomano", "Plettigoal", "Santi_J_M", "David_Ornstein", 
    "sistoney67", "PaulJoyce_", "MatteoMoretto_", "AlfredoPedulla", 
    "cfalk_news", "FabrizioHawkins"
]

# Keywords to detect news
KEYWORDS = ["here we go", "agreement reached", "signed", "medical", "appointed", "sacked", "confirmed", "done deal"]

# File to track posted news (stored in GitHub repo)
POSTED_NEWS_FILE = "posted_news.json"

# --- 1. SCRAPE LATEST NEWS (Using Twikit for Free Reading) ---
async def get_latest_transfer():
    print("🔍 Scraping journalists via Twikit...")
    client = Client('en-US')
    
    # Login using cookies (No API key needed for reading)
    await client.login(
        auth_token=X_AUTH_TOKEN,
        ct0=X_CT0_TOKEN
    )
    
    latest_tweet = None
    
    # Check each journalist
    for username in JOURNALISTS:
        try:
            # Get last 3 tweets from user
            tweets = await client.get_user_tweets(username, 'tweets', count=3)
            for tweet in tweets:
                text_lower = tweet.text.lower()
                # Check for keywords
                if any(word in text_lower for word in KEYWORDS):
                    # Return the first matching tweet found
                    return {
                        "text": tweet.text,
                        "user": tweet.user_name,
                        "id": tweet.id
                    }
        except Exception as e:
            print(f"Error scraping {username}: {e}")
            continue
            
    return None

# --- 2. CHECK IF NEWS IS NEW ---
def is_new_news(tweet_id):
    if not os.path.exists(POSTED_NEWS_FILE):
        return True
    with open(POSTED_NEWS_FILE, 'r') as f:
        posted = json.load(f)
    return str(tweet_id) not in posted

def save_news(tweet_id):
    posted = []
    if os.path.exists(POSTED_NEWS_FILE):
        with open(POSTED_NEWS_FILE, 'r') as f:
            posted = json.load(f)
    
    posted.append(str(tweet_id))
    # Keep only last 500 items to save space
    if len(posted) > 500:
        posted = posted[-500:]
        
    with open(POSTED_NEWS_FILE, 'w') as f:
        json.dump(posted, f)

# --- 3. GENERATE IMAGE (PNG) ---
def create_image(news_text, source_user):
    print("🎨 Creating image...")
    # Create 1200x675 image (Optimal for X)
    img = Image.new('RGB', (1200, 675), color=(10, 10, 30)) # Dark Blue
    draw = ImageDraw.Draw(img)
    
    # Load Fonts (Linux standard fonts on GitHub Actions)
    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 50)
        font_body = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 35)
    except:
        font_title = ImageFont.load_default()
        font_body = ImageFont.load_default()

    # Add Text
    draw.text((50, 50), "BREAKING NEWS", font=font_title, fill=(255, 215, 0)) # Gold
    
    # Simple text wrapping
    words = news_text.split()
    lines = []
    current_line = ""
    for word in words:
        if len(current_line + word) < 55:
            current_line += word + " "
        else:
            lines.append(current_line)
            current_line = word + " "
    lines.append(current_line)
    
    y = 150
    for line in lines[:7]: # Max 7 lines
        draw.text((50, y), line, font=font_body, fill=(255, 255, 255))
        y += 45
        
    draw.text((50, 550), f"Source: @{source_user}", font=font_body, fill=(200, 200, 200))
    
    filename = "news_image.png"
    img.save(filename)
    return filename

# --- 4. POST TO X (Using Tweepy for Free Posting) ---
def post_to_x(text, image_path):
    print("🚀 Posting to X via Tweepy...")
    client = tweepy.Client(
        consumer_key=X_API_KEY,
        consumer_secret=X_API_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_SECRET
    )
    
    # Upload Media
    media = client.media_upload(filename=image_path)
    
    # Create Short Post
    short_text = f"🚨 BREAKING: {text[:180]}...\n\n📸 Source: @{source_user}\n#TransferNews #FPL #Football"
    
    # Post Tweet
    client.create_tweet(text=short_text, media_ids=[media.media_id])
    print("✅ Posted successfully!")

# --- MAIN EXECUTION ---
async def main():
    # 1. Scrape
    news = await get_latest_transfer()
    
    if news:
        # 2. Check Duplicate
        if is_new_news(news['id']):
            # 3. Create Image
            img_file = create_image(news['text'], news['user'])
            
            # 4. Post to X
            # Note: We need to pass 'news' to post_to_x properly
            # Modifying post_to_x call to include user for text
            global source_user
            source_user = news['user']
            
            # Re-define short text inside post_to_x or pass variable
            # For simplicity, let's just call it and handle text inside
            # Actually, let's just pass the text and user
            post_to_x(news['text'], img_file)
            
            # 5. Save ID
            save_news(news['id'])
            
            # Commit the posted_news.json back to repo is handled by GitHub Action step
        else:
            print("ℹ️ News already posted.")
    else:
        print("No new transfer news found.")

if __name__ == "__main__":
    asyncio.run(main())   
