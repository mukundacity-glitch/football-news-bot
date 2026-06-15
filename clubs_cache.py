import os
import json
import requests
from datetime import datetime, timezone
from pathlib import Path

FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")
CACHE_FILE       = Path("clubs_cache.json")
CACHE_HOURS      = 24

# api.football-data.org competition IDs
COMPETITIONS = {
    "PL":  "Premier League",
    "ELC": "Championship",
    "PD":  "La Liga",
    "SA":  "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "CL":  "Champions League",
    "EL":  "Europa League",
    "DED": "Eredivisie",
    "PPL": "Primeira Liga",
    "BL2": "Bundesliga 2",
    "SB":  "Serie B",
}

# ── CACHE CHECK ────────────────────────────────────────────────────────────────
def cache_valid() -> bool:
    if not CACHE_FILE.exists():
        return False
    with open(CACHE_FILE) as f:
        data = json.load(f)
    fetched = datetime.fromisoformat(data.get("fetched_at", "2000-01-01"))
    age     = (datetime.now(timezone.utc) - fetched.replace(tzinfo=timezone.utc))
    return age.total_seconds() < CACHE_HOURS * 3600

def load_cache() -> dict:
    with open(CACHE_FILE) as f:
        return json.load(f)

def save_cache(data: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ── SLUG BUILDER ───────────────────────────────────────────────────────────────
def make_hashtag(name: str) -> str:
    """Liverpool FC → #LiverpoolFC"""
    cleaned = name.replace(".", "").replace("-", "")
    words   = cleaned.split()
    return "#" + "".join(w.capitalize() for w in words)

def make_short_name(name: str) -> str:
    """Lowercase full name for dict key"""
    return name.lower().strip()

# ── KNOWN SHORT NAMES ──────────────────────────────────────────────────────────
# These override auto-generated hashtags for well known clubs
KNOWN_OVERRIDES = {
    "manchester united":    "#MUFC",
    "manchester city":      "#MCFC",
    "arsenal":              "#AFC",
    "chelsea":              "#CFC",
    "liverpool":            "#LFC",
    "tottenham hotspur":    "#THFC",
    "newcastle united":     "#NUFC",
    "aston villa":          "#AVFC",
    "west ham united":      "#WHUFC",
    "everton":              "#EFC",
    "brighton":             "#BHAFC",
    "wolverhampton":        "#WWFC",
    "leicester city":       "#LCFC",
    "nottingham forest":    "#NFFC",
    "brentford":            "#BrentfordFC",
    "fulham":               "#FFC",
    "crystal palace":       "#CPFC",
    "fc barcelona":         "#FCBarcelona",
    "barcelona":            "#FCBarcelona",
    "real madrid":          "#RealMadrid",
    "atletico madrid":      "#Atletico",
    "juventus":             "#Juve",
    "ac milan":             "#ACMilan",
    "inter milan":          "#Inter",
    "ssc napoli":           "#Napoli",
    "napoli":               "#Napoli",
    "paris saint-germain":  "#PSG",
    "psg":                  "#PSG",
    "fc bayern münchen":    "#FCBayern",
    "bayern munich":        "#FCBayern",
    "borussia dortmund":    "#BVB",
    "celtic":               "#CelticFC",
    "rangers":              "#RangersFC",
    "porto":                "#FCPorto",
    "benfica":              "#SLBenfica",
    "ajax":                 "#Ajax",
    "sevilla":              "#Sevilla",
    "as roma":              "#ASRoma",
    "roma":                 "#ASRoma",
    "ss lazio":             "#Lazio",
    "lazio":                "#Lazio",
}

# ── FETCH ──────────────────────────────────────────────────────────────────────
def fetch_all_clubs() -> dict:
    print("[CLUBS] Fetching fresh club data from API...")
    headers      = {"X-Auth-Token": FOOTBALL_API_KEY}
    club_hashtags = {}
    short_names   = {}   # alias → full name (for detection)
    pl_clubs      = set()

    for code, league_name in COMPETITIONS.items():
        url = f"https://api.football-data.org/v4/competitions/{code}/teams"
        try:
            r    = requests.get(url, headers=headers, timeout=10)
            data = r.json()

            if "teams" not in data:
                print(f"  [WARN] {league_name}: {data.get('message', 'no teams')}")
                continue

            for team in data["teams"]:
                full   = team.get("name", "")
                short  = team.get("shortName", "")
                tla    = team.get("tla", "")        # e.g. MUN, ARS
                area   = team.get("area", {}).get("name", "")

                full_lower  = make_short_name(full)
                short_lower = make_short_name(short)

                # pick hashtag — override first, then auto-generate
                tag = (KNOWN_OVERRIDES.get(full_lower)
                       or KNOWN_OVERRIDES.get(short_lower)
                       or make_hashtag(short or full))

                club_hashtags[full_lower]  = tag
                club_hashtags[short_lower] = tag
                if tla:
                    club_hashtags[tla.lower()] = tag

                # short_names for text matching
                short_names[full_lower]  = full_lower
                short_names[short_lower] = full_lower

                # track PL clubs for #PremierLeague tag
                if code == "PL":
                    pl_clubs.add(full_lower)
                    pl_clubs.add(short_lower)

            print(f"  ✅ {league_name}: {len(data['teams'])} clubs")

        except Exception as e:
            print(f"  [ERROR] {league_name}: {e}")

    return {
        "fetched_at":    datetime.now(timezone.utc).isoformat(),
        "club_hashtags": club_hashtags,
        "short_names":   short_names,
        "pl_clubs":      list(pl_clubs),
    }

# ── PUBLIC INTERFACE ───────────────────────────────────────────────────────────
def get_club_data() -> dict:
    """Call this once at bot startup. Returns fresh or cached club data."""
    if cache_valid():
        print("[CLUBS] Using cached club data.")
        return load_cache()

    data = fetch_all_clubs()
    save_cache(data)
    return data
