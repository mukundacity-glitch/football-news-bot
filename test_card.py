"""
test_card.py — Preview FPL VORTEX player cards WITHOUT scraping or posting to X.

Run:  python test_card.py

It builds a few sample cards straight from create_transfer_image() and saves
them to ./card_previews/ so you can open the PNGs and check the design
(photo / logo / FROM-TO / text overlap) with zero X involvement.
"""

from pathlib import Path
import main  # imports your bot; uses its real card generator

# Make sure club data + fonts are ready (same init the bot does)
main.init_club_data()

OUT = Path("card_previews")
OUT.mkdir(exist_ok=True)

# A few sample stories covering the cases you care about.
# Each is shaped exactly like the dicts your scraper builds.
SAMPLES = [
    {
        "name": "confirmed_with_fpl_photo",
        "story": {
            "player": "Bukayo Saka",          # real FPL player -> should pull FPL photo + crest
            "event": "transfer",
            "to_key": "Arsenal", "to_club": "Arsenal",
            "from_key": None, "from_club": None,
            "stage": 4, "mode": "confirmed", "collapsed": False,
            "fee": "£60m", "media_url": None,
        },
        "sources": ["FabrizioRomano"],
    },
    {
        "name": "rumour_with_from_and_to",
        "story": {
            "player": "Marcus Rashford",
            "event": "transfer",
            "to_key": "Aston_Villa", "to_club": "Aston Villa",
            "from_key": "Man_Utd", "from_club": "Man Utd",
            "stage": 1, "mode": "rumour", "collapsed": False,
            "fee": None, "media_url": None,
        },
        "sources": ["David_Ornstein"],
    },
    {
        "name": "non_fpl_player_tweet_image_fallback",
        "story": {
            "player": "Lia Walti",            # not in FPL -> no FPL photo
            "event": "transfer",
            "to_key": "Brighton", "to_club": "Brighton",
            "from_key": None, "from_club": None,
            "stage": 2, "mode": "rumour", "collapsed": False,
            "fee": None,
            # put a real image URL here to test the tweet-image fallback tier,
            # or leave None to see the "V" silhouette fallback:
            "media_url": None,
        },
        "sources": ["BBCSport"],
    },
]

print(f"Generating {len(SAMPLES)} preview cards into ./{OUT}/ ...\n")
for s in SAMPLES:
    out_path = OUT / f"{s['name']}.png"
    try:
        main.create_transfer_image(
            s["story"], s["sources"], str(out_path),
            collapsed=s["story"].get("collapsed", False),
        )
        size = out_path.stat().st_size if out_path.exists() else 0
        status = "OK" if size >= 1000 else "FAILED (file too small / blank)"
        print(f"  {status:>35}  ->  {out_path}  ({size} bytes)")
    except Exception as e:
        import traceback
        print(f"  EXCEPTION for {s['name']}: {e}")
        traceback.print_exc()

print(f"\nDone. Open the PNGs in ./{OUT}/ to inspect the cards.")
