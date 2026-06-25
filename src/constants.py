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

# Target Scrape Accounts
JOURNALISTS = [
    "FabrizioRomano", "David_Ornstein", "_pauljoyce", "sistoney67",
    "SamiMokbel_BBC", "JacobsBen", "JamesPearceLFC", "SachaTavolieri",
    "Plettigoal", "MatteoMoretto", "AlfredoPedulla", "DiMarzio",
    "SkySportsNews", "BBCSport", "TheAthleticFC", "guardian_sport",
    "lequipe", "marca", "diarioas", "kicker", "alex_crook", "AlexCrabb31", 
    "Transferzone00", "premierleague", "OfficialFPL", "PremierInjuries",
    "Arsenal", "AVFCOfficial", "ManCity", "LFC", "ChelseaFC",
    "ManUtd", "SpursOfficial", "NUFC", "NFFC", "Everton",
    "WestHam", "CPFC", "OfficialBHAFC", "Wolves", "BrentfordFC",
    "FulhamFC", "AFCBournemouth", "lcfc",
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
    "officialbhafc", "chelseafc", "cpfc", "everton", "fulhamfc",
    "lcfc", "liverpoolfc", "lfc", "mancity", "manutd", "newcastle_nufc", "nufc",
    "nffc", "southamptonfc", "spursofficial", "westham", "wolves",
}
OFFICIAL_INJURY_ACCOUNTS = OFFICIAL_ACCOUNTS | {"officialfpl", "fpl", "premierleague", "premierinjuries"}
ELITE_TRUSTED = {
    "fabrizioromano", "david_ornstein", "_pauljoyce", "sistoney67",
    "samimokbel_bbc", "jacobsben", "jamespearcelfc", "sachatavolieri",
    "plettigoal", "matteomoretto", "alfredopedulla", "dimarzio",
}
TRUSTED_MEDIA = {
    "skysportsnews", "skysports", "bbcsport", "theathleticfc", "theathletic",
    "guardian_sport", "lequipe", "marca", "diarioas", "as", "kicker",
    "alex_crook", "alexcrabb31",
}

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
