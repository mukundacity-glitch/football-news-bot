# src/fpl_cache.py
import json
import urllib.request
from datetime import datetime
from pathlib import Path
import re

FPL_CACHE_PATH = Path("data/fpl_cache.json")

def fetch_fpl_data():
    """Retrieves static data definitions from the official FPL API endpoint."""
    FPL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    if FPL_CACHE_PATH.exists() and (datetime.now().timestamp() - FPL_CACHE_PATH.stat().st_mtime < 86400):
        with open(FPL_CACHE_PATH, "r", encoding="utf-8") as f: 
            return json.load(f)
            
    try:
        req = urllib.request.Request(
            "https://fantasy.premierleague.com/api/bootstrap-static/",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        with open(FPL_CACHE_PATH, "w", encoding="utf-8") as f: 
            json.dump(data, f, indent=2)
        return data
    except Exception as e:
        print(f"  [API ERROR] Failed to fetch official FPL Bootstrap: {e}")
        if FPL_CACHE_PATH.exists():
            with open(FPL_CACHE_PATH, "r", encoding="utf-8") as f: 
                return json.load(f)
        return None

def find_player_in_fpl(player_name: str, fpl_data: dict):
    """Cross-references scraped tokens against FPL element definitions to confirm identities."""
    if not fpl_data or not player_name: 
        return None
        
    q = player_name.lower().strip()
    tokens = [t for t in re.split(r'[\s\-]+', q) if t]
    if not tokens: 
        return None
        
    for el in fpl_data.get("elements", []):
        web_name = el["web_name"].lower()
        full_name = f"{el['first_name']} {el['second_name']}".lower()
        
        if q == full_name or q == web_name: 
            return el
        if len(tokens) >= 2 and all(re.search(r'(?<![a-z])' + re.escape(t) + r'(?![a-z])', full_name) for t in tokens):
            return el
        if len(tokens) == 1 and tokens[0] == web_name: 
            return el
    return None
