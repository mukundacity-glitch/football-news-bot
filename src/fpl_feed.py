"""
FPL VORTEX — Official FPL Data Feed & Verification Module.

Handles all interactions with the fantasy.premierleague.com bootstrap-static API.
Caches data locally to prevent rate-limiting and provides robust player/club 
verification functions to enforce data integrity across the bot's logic.
"""

import json
import urllib.request
import re
from pathlib import Path
from datetime import datetime, timezone
from src.constants import CLUB_ALIASES

# Sort aliases by length for safe regex matching (longest first) to prevent partial word overrides
_SORTED_ALIASES = sorted(CLUB_ALIASES.keys(), key=len, reverse=True)

def resolve_club_key(name: str) -> str | None:
    """
    Resolves a raw string name into our standardized CLUB_ALIASES key.
    """
    if not name:
        return None
    
    n = name.lower()
    for alias in _SORTED_ALIASES:
        if re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', n):
            return CLUB_ALIASES[alias]
    return None

def fetch_fpl_data() -> dict | None:
    """
    Fetches and caches the official bootstrap-static FPL data feed.
    Enforces a 24-hour cache limit to ensure data is fresh but not spamming the API.
    """
    cache = Path("data/fpl_cache.json")
    
    # 1. Check local cache (Valid for 24 hours / 86400 seconds)
    if cache.exists() and (datetime.now(timezone.utc).timestamp() - cache.stat().st_mtime < 86400):
        try:
            with open(cache, "r", encoding="utf-8") as f: 
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  [FEED ERROR] Cache unreadable, forcing re-fetch: {e}")

    # 2. Fetch fresh data if cache is missing or stale
    try:
        req = urllib.request.Request(
            "https://fantasy.premierleague.com/api/bootstrap-static/",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        
        # Added a 10-second timeout to prevent the thread from hanging indefinitely
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            
        # 3. Write securely to local cache
        cache.parent.mkdir(parents=True, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f: 
            json.dump(data, f)
            
        return data
        
    except Exception as e:
        print(f"  [FEED ERROR] Failed syncing with FPL API: {e}")
        return None

def find_player_in_fpl(player_name: str, fpl_data: dict) -> dict | None:
    """
    Queries the FPL cache to return verified element token data.
    Uses progressive matching (Exact -> Multi-Token -> Web Name) to ensure high hit rates.
    """
    if not fpl_data or not player_name: 
        return None
        
    q = player_name.lower().strip()
    tokens = [t for t in re.split(r'[\s\-]+', q) if t]
    
    if not tokens: 
        return None
        
    for el in fpl_data.get("elements", []):
        web = el["web_name"].lower()
        full = f"{el['first_name']} {el['second_name']}".lower()
        
        # Priority 1: Exact match on full name or web name
        if q == full or q == web: 
            return el
            
        # Priority 2: All tokens found in the full name (e.g., 'Bruno' and 'Guimaraes')
        if len(tokens) >= 2 and all(re.search(r'(?<![a-z])' + re.escape(t) + r'(?![a-z])', full) for t in tokens):
            return el
            
        # Priority 3: Single token exactly matches the web name
        if len(tokens) == 1 and tokens[0] == web: 
            return el
            
    return None

def fpl_team_key(el: dict, fpl_data: dict) -> str | None:
    """
    Cross-references an FPL element's team ID against the master team list 
    to return our standardized FPL VORTEX club key.
    """
    if not el or not fpl_data: 
        return None
        
    team_id = el.get("team")
    for t in fpl_data.get("teams", []):
        if t.get("id") == team_id:
            raw_name = f"{t.get('name', '')} {t.get('short_name', '')}".lower()
            return resolve_club_key(raw_name)
            
    return None

def is_big_player(player_name: str, fpl_data: dict) -> bool:
    """
    Determines if a player is high-profile based on FPL cost (>= 6.5m) 
    or total points (>= 90). This logic acts as a relevance safety net.
    """
    el = find_player_in_fpl(player_name, fpl_data)
    if not el: 
        return False
        
    return el.get("now_cost", 0) >= 65 or el.get("total_points", 0) >= 90
