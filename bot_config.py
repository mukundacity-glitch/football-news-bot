"""
Central config loader for FPL VORTEX.

All CHANGING DATA (handles, club lists, keywords, tiers, settings) lives in
config.json. This module loads it once, validates it, and exposes it. If the
file is missing or broken, we fail LOUD with a clear message rather than run on
half-loaded data — that is the safe behaviour for an auto-posting bot.
"""

import json
import sys
from pathlib import Path

CONFIG_PATH = Path("config.json")

# Minimal fallback so the bot still runs if a non-critical section is absent.
_DEFAULTS = {
    "settings": {
        "use_fpl_injuries": True,
        "confidence_threshold": 0.40,
        "low_confidence_policy": "skip",
        "post_reported": True,
        "max_posts_per_run": 5,
        "max_posts_per_hour": 6,
        "daily_limit": 40,
        "tweets_per_account": 8,
        "post_jitter_seconds": [0, 15],
        "fpl_min_news_len": 4,
    },
    "journalists": [],
    "media_accounts": [],
    "official_club_accounts": [],
    "source_tiers": {"official": [], "reporter": [], "media": []},
    "nitter_instances": [],
    "keywords": {
        "football": [], "transfer_signals": [], "injury_words": [],
        "suspension_words": [], "staff_block": [], "strong_official": [],
    },
    "big_name_players": [],
    "club_aliases": {},
    "club_crest_ids": {},
    "club_colors": {},
    "club_hashtags": {},
}


def _deep_merge(base, override):
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        print(f"[CONFIG] FATAL: {path} not found. The bot needs config.json "
              f"(handles, clubs, keywords). Create it and re-run.")
        sys.exit(2)
    try:
        raw = json.loads(path.read_text())
    except Exception as e:
        print(f"[CONFIG] FATAL: could not parse {path}: {e}")
        sys.exit(2)

    cfg = _deep_merge(_DEFAULTS, raw)

    # Light validation: warn (don't crash) on empty critical lists so the
    # operator knows WHY volume might be low, but the bot keeps running.
    if not cfg["journalists"]:
        print("[CONFIG] WARNING: 'journalists' list is empty — no transfer "
              "detection sources. Add handles in config.json.")
    if not cfg["club_aliases"]:
        print("[CONFIG] WARNING: 'club_aliases' is empty — club detection "
              "disabled. Posts will still work but without club crests/hashtags.")
    # Tuples are friendlier for colours downstream.
    cfg["club_colors"] = {k: tuple(v) for k, v in cfg.get("club_colors", {}).items()}
    cfg["settings"]["post_jitter_seconds"] = tuple(
        cfg["settings"].get("post_jitter_seconds", [0, 15]))
    print(f"[CONFIG] Loaded: {len(cfg['journalists'])} journalists, "
          f"{len(cfg['official_club_accounts'])} club accounts, "
          f"{len(cfg['club_aliases'])} club aliases.")
    return cfg
