"""
FPL VORTEX — Official FPL Data Feed & Verification Module.

Handles all interactions with the fantasy.premierleague.com bootstrap-static API.
Caches data locally to prevent rate-limiting and provides robust player/club 
verification functions to enforce data integrity across the bot's logic.
"""

import json
import urllib.request
import re
import unicodedata
from pathlib import Path
from datetime import datetime, timezone
from src.constants import CLUB_ALIASES


def _strip(s: str) -> str:
    """Lowercase + remove diacritics so 'Álvarez' matches 'alvarez', 'Guimarães' matches 'guimaraes'."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()

# Sort aliases by length for safe regex matching (longest first) to prevent partial word overrides
_SORTED_ALIASES = sorted(CLUB_ALIASES.keys(), key=len, reverse=True)

def resolve_club_key(name: str) -> str | None:
    """
    Resolves a raw string name into our standardized CLUB_ALIASES key.
    """
    if not name:
        return None
    
    n = _strip(name)
    for alias in _SORTED_ALIASES:
        if re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', n):
            return CLUB_ALIASES[alias]
    return None

def _valid_fpl_payload(data) -> bool:
    return bool(
        isinstance(data, dict)
        and isinstance(data.get("teams"), list) and data.get("teams")
        and isinstance(data.get("elements"), list) and data.get("elements")
    )


def _sync_squad_registry(data: dict | None) -> None:
    """Keep the closed-world squad allowlist in step with the feed.

    Done here rather than at each of the eight ``fetch_fpl_data()`` call sites so
    the registry can never drift out of sync with the roster it is built from —
    a stale registry silently narrows or widens what the bot may publish.
    """
    from src import squad_registry  # local: squad_registry reads this module

    squad_registry.refresh_registry(data)


def fetch_fpl_data() -> dict | None:
    """Fetch a fresh, schema-validated FPL registry and cache it atomically.

    A missing/corrupt/stale cache never weakens validation: if the official API
    cannot provide a valid payload, ``None`` is returned and V2 blocks publishing.
    """
    cache = Path("data/fpl_cache.json")
    now = datetime.now(timezone.utc).timestamp()
    if cache.exists() and now - cache.stat().st_mtime < 86400:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if _valid_fpl_payload(data):
                _sync_squad_registry(data)
                return data
            print("  [FEED ERROR] FPL cache schema invalid, forcing re-fetch.")
        except (json.JSONDecodeError, OSError) as e:
            print(f"  [FEED ERROR] Cache unreadable, forcing re-fetch: {e}")

    try:
        req = urllib.request.Request(
            "https://fantasy.premierleague.com/api/bootstrap-static/",
            headers={"User-Agent": "FPLVortexBot/2.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not _valid_fpl_payload(data):
            raise ValueError("official FPL payload missing teams/elements")
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(cache)
        _sync_squad_registry(data)
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
        
    q = _strip(player_name)
    tokens = [t for t in re.split(r'[\s\-]+', q) if t]
    
    if not tokens: 
        return None
        
    for el in fpl_data.get("elements", []):
        web = _strip(el["web_name"])
        full = _strip(f"{el['first_name']} {el['second_name']}")
        
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
