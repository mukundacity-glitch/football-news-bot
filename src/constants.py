# src/constants.py
from pathlib import Path

# Branding & Channels
CHANNEL_NAME = "FPL VORTEX"
CHANNEL_HANDLE = "@FPLVortex"

# System Paths
POSTED_FILE = Path("data/posted_news.json")
PENDING_DIR = Path("queue/pending")
POSTED_DIR = Path("queue/posted")
LOGOS_DIR = Path("data/logos")
PLAYERS_DIR = Path("data/players")
DRAFTS_DIR = Path("fpl_drafts")

# Target Scrape Accounts
JOURNALISTS = [
    "FabrizioRomano", "David_Ornstein", 
]

NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]

# Source Verification Tiers
OFFICIAL_ACCOUNTS = {
    "premierleague", "officialfpl", "fpl", "uefa", "fifacom", "fifaworldcup",
    "arsenal", "avfcofficial", "afcbournemouth", "brentfordfc",
    "officialbhafc", "burnleyofficial", "chelseafc", "cpfc", "everton", "fulhamfc",
    "ipswichtown", "leedsunited", "lufc",
    "lcfc", "liverpoolfc", "lfc", "mancity", "manutd", "newcastle_nufc", "nufc",
    "nffc", "southamptonfc", "spursofficial", "sunderlandafc", "safc",
    "westham", "wolves",
}
OFFICIAL_INJURY_ACCOUNTS = OFFICIAL_ACCOUNTS | {"officialfpl", "fpl", "premierleague", "premierinjuries"}
ELITE_TRUSTED = {
    "fabrizioromano", "david_ornstein", 
}
TRUSTED_MEDIA = {
    "skysportsnews", "skysports", "bbcsport", "theathleticfc", "theathletic",
    "guardian_sport", "lequipe", "marca", "diarioas", "as", "kicker",
    "alex_crook", "alexcrabb31", "telegraph", "telegraphfootball",
    "fotmob", "transfermarkt",
}

# ── AUTOMATIC CROSS-VERIFICATION SOURCES ─────────────────────────────────
# Official club website domain + the club's canonical (tier-1) handle, keyed
# by club key. When a story's club's OWN website carries the news, that is
# treated as an official confirmation.
CLUB_OFFICIAL_DOMAINS = {
    "Arsenal": ("arsenal.com", "arsenal"),
    "Aston_Villa": ("avfc.co.uk", "avfcofficial"),
    "Bournemouth": ("afcb.co.uk", "afcbournemouth"),
    "Brentford": ("brentfordfc.com", "brentfordfc"),
    "Brighton": ("brightonandhovealbion.com", "officialbhafc"),
    "Burnley": ("burnleyfootballclub.com", "burnleyofficial"),
    "Chelsea": ("chelseafc.com", "chelseafc"),
    "Crystal_Palace": ("cpfc.co.uk", "cpfc"),
    "Everton": ("evertonfc.com", "everton"),
    "Fulham": ("fulhamfc.com", "fulhamfc"),
    "Ipswich": ("itfc.co.uk", "ipswichtown"),
    "Leeds": ("leedsunited.com", "leedsunited"),
    "Leicester": ("lcfc.com", "lcfc"),
    "Liverpool": ("liverpoolfc.com", "liverpoolfc"),
    "Man_City": ("mancity.com", "mancity"),
    "Man_Utd": ("manutd.com", "manutd"),
    "Newcastle": ("newcastleunited.com", "nufc"),
    "Nottm_Forest": ("nottinghamforest.co.uk", "nffc"),
    "Southampton": ("southamptonfc.com", "southamptonfc"),
    "Spurs": ("tottenhamhotspur.com", "spursofficial"),
    "Sunderland": ("safc.com", "sunderlandafc"),
    "West_Ham": ("whufc.com", "westham"),
    "Wolves": ("wolves.co.uk", "wolves"),
}

# Trusted media website domain -> canonical handle (tier 2/3 via the sets
# above). Used to map Google News results back onto the source-tier system.
TRUSTED_MEDIA_DOMAINS = {
    "bbc.co.uk": "bbcsport",
    "bbc.com": "bbcsport",
    "skysports.com": "skysports",
    "theathletic.com": "theathleticfc",
    "nytimes.com": "theathleticfc",       # The Athletic lives under NYT
    "fotmob.com": "fotmob",
    "theguardian.com": "guardian_sport",
    "telegraph.co.uk": "telegraph",
    "transfermarkt.com": "transfermarkt",
    "transfermarkt.co.uk": "transfermarkt",
    "transfermarkt.us": "transfermarkt",
    "premierleague.com": "premierleague",
    "lequipe.fr": "lequipe",
    "marca.com": "marca",
    "kicker.de": "kicker",
}

# Single source of truth for "this reads as an officially completed deal"
# language — used both to grade a story's stage (parser.py) and to decide
# whether a CONFIRMED card is warranted (main.py). Two separate, drifting
# copies of this list previously disagreed (parser.py's list was missing
# "joined"/"signed"/"medical"/etc.), which is exactly the kind of consistency
# gap that lets a genuinely-completed move ("has joined ... on loan") get
# stuck at a lower confidence stage than the wording actually supports.
STRONG_OFFICIAL_CUES = [
    "here we go", "official", "confirmed", "completed", "done deal",
    "sealed", "unveiled", "joins", "joined", "signs", "signed", "medical",
]

# Parsing Keywords
FOOTBALL_KW = [
    "transfer", "sign", "deal", "fee", "bid", "loan", "contract", "agree",
    "medical", "official", "here we go", "talks", "joins", "move", "target",
    "injury", "injured", "ruled out", "scan", "hamstring", "surgery", "doubt",
    "sack", "appoint", "manager", "head coach", "stay", "return", "recall",
    "suspended", "suspension", "banned", "red card", "sent off",
]

STAFF_BLOCK_KW = [
    "head of recruitment", "sporting director", "director of football",
    "technical director", "chief scout", "scouting", "ceo", "chairman",
    "owner", "president", "physio", "kit man", "head of football",
    "transfer chief", "negotiator",
]

MANAGER_SURNAMES = {
    "de zerbi", "zerbi", "guardiola", "arteta", "klopp", "slot", "postecoglou",
    "ten hag", "amorim", "emery", "howe", "maresca", "iraola", "frank",
    "nuno", "moyes", "dyche", "hurzeler", "glasner", "ancelotti", "xabi alonso",
    "alonso", "flick", "simeone", "mourinho", "conte", "tuchel", "nagelsmann",
    "wilder", "edwards", "robinson", "silva", "kompany", "lopetegui", "obi",
}

# Position & Nationality filters (used by parser._is_bad_name)
# Lowercase, single-token words — _is_bad_name lowercases and splits candidate names.
POSITION_WORDS = {
    "goalkeeper", "keeper", "goalie", "defender", "defenders", "fullback",
    "wingback", "centreback", "centre-back", "center-back", "midfielder",
    "midfielders", "midfield", "winger", "wingers", "striker", "strikers",
    "forward", "forwards", "attacker", "attackers", "playmaker", "sweeper",
    "stopper", "defence", "defense", "attack",
}

NATIONALITY_ADJECTIVES = {
    "english", "british", "welsh", "scottish", "irish", "french", "spanish",
    "portuguese", "italian", "german", "dutch", "belgian", "brazilian",
    "argentine", "argentinian", "uruguayan", "colombian", "chilean", "mexican",
    "american", "canadian", "croatian", "serbian", "polish", "czech", "slovak",
    "swedish", "norwegian", "danish", "finnish", "swiss", "austrian", "turkish",
    "greek", "russian", "ukrainian", "hungarian", "romanian", "bulgarian",
    "moroccan", "algerian", "tunisian", "egyptian", "nigerian", "ghanaian",
    "senegalese", "ivorian", "cameroonian", "malian", "japanese", "korean",
    "australian", "ecuadorian", "paraguayan", "peruvian", "venezuelan",
    "icelandic", "albanian", "kosovan", "bosnian", "slovenian", "georgian",
    "armenian", "israeli", "iranian", "jamaican", "spaniard",
}

# Club Mapping Metadata
CLUB_ALIASES = {
    "arsenal": "Arsenal", "aston villa": "Aston_Villa", "villa": "Aston_Villa",
    "bournemouth": "Bournemouth", "brentford": "Brentford", "brighton": "Brighton",
    "burnley": "Burnley", "chelsea": "Chelsea", "crystal palace": "Crystal_Palace",
    "palace": "Crystal_Palace", "everton": "Everton", "fulham": "Fulham",
    "ipswich": "Ipswich", "ipswich town": "Ipswich", "leeds": "Leeds",
    "leeds united": "Leeds", "leicester": "Leicester", "leicester city": "Leicester",
    "liverpool": "Liverpool", "manchester city": "Man_City", "man city": "Man_City",
    "manchester united": "Man_Utd", "man united": "Man_Utd", "man utd": "Man_Utd",
    "newcastle": "Newcastle", "newcastle united": "Newcastle", "nottingham forest": "Nottm_Forest",
    "nott'm forest": "Nottm_Forest", "forest": "Nottm_Forest", "southampton": "Southampton",
    "sunderland": "Sunderland", "tottenham": "Spurs", "spurs": "Spurs",
    "tottenham hotspur": "Spurs", "west ham": "West_Ham", "west ham united": "West_Ham",
    "wolves": "Wolves", "wolverhampton": "Wolves",
}

FPL_LOGO_IDS = {
    "Arsenal": "3", "Aston_Villa": "7", "Bournemouth": "91", "Brentford": "94",
    "Brighton": "36", "Burnley": "90", "Chelsea": "8", "Crystal_Palace": "31", "Everton": "11",
    "Fulham": "54", "Ipswich": "40", "Leeds": "2", "Leicester": "13", "Liverpool": "14",
    "Man_City": "43", "Man_Utd": "1", "Newcastle": "4", "Nottm_Forest": "17",
    "Southampton": "20", "Spurs": "6", "Sunderland": "56", "West_Ham": "21", "Wolves": "39",
}

CLUB_COLORS = {
    "Arsenal": (239, 1, 7), "Aston_Villa": (103, 14, 54), "Bournemouth": (181, 14, 18),
    "Brentford": (227, 6, 19), "Brighton": (0, 87, 184), "Chelsea": (3, 70, 148),
    "Crystal_Palace": (27, 69, 143), "Everton": (39, 68, 136), "Fulham": (15, 15, 15),
    "Ipswich": (0, 0, 255), "Leicester": (0, 83, 160), "Liverpool": (200, 16, 46),
    "Man_City": (108, 173, 223), "Man_Utd": (218, 41, 28), "Newcastle": (15, 15, 15),
    "Nottm_Forest": (229, 50, 51), "Southampton": (215, 25, 32), "Spurs": (17, 24, 38),
    "West_Ham": (122, 38, 58), "Wolves": (253, 185, 19),
}

CLUB_HASHTAG_MAP = {
    "Arsenal": "#Arsenal", "Aston_Villa": "#AVFC", "Bournemouth": "#AFCB",
    "Brentford": "#Brentford", "Brighton": "#BHAFC", "Chelsea": "#Chelsea",
    "Crystal_Palace": "#CPFC", "Everton": "#EFC", "Fulham": "#FFC",
    "Ipswich": "#ITFC", "Leicester": "#LCFC", "Liverpool": "#LFC",
    "Man_City": "#MCFC", "Man_Utd": "#MUFC", "Newcastle": "#NUFC",
    "Nottm_Forest": "#NFFC", "Southampton": "#SaintsFC", "Spurs": "#THFC",
    "West_Ham": "#WHUFC", "Wolves": "#Wolves",
}
