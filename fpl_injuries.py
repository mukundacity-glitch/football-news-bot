"""
FPL injury & suspension source (PRIMARY, structured, free, official).

Reads the official Fantasy Premier League player feed and turns availability
changes into postable stories — no scraping, no Gemini needed. Each player
carries:
  status: 'a' available, 'i' injured, 's' suspended, 'd' doubtful, 'u' unavailable
  news: human text e.g. "Hamstring injury - 75% chance of playing"
  chance_of_playing_next_round: 0..100 or null
  news_added: ISO timestamp the news was set

We post when a player's `news` text CHANGES versus what we last saw, so we never
spam the same injury. State is kept in fpl_player_state.json.
"""

import json
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

FPL_BOOTSTRAP = "https://fantasy.premierleague.com/api/bootstrap-static/"
STATE_FILE = Path("fpl_player_state.json")

_STATUS_LABEL = {
    "i": "injury",
    "s": "suspension",
    "d": "injury",     # doubtful → treat as injury update
    "u": "injury",     # unavailable → injury update
}


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_state(state: dict):
    try:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=0))
        tmp.replace(STATE_FILE)
    except Exception as e:
        print(f"  [FPL] could not save player state: {e}")


def _fetch_bootstrap(fpl_data=None):
    """Reuse already-fetched bootstrap if provided, else fetch fresh."""
    if fpl_data and fpl_data.get("elements"):
        return fpl_data
    try:
        req = urllib.request.Request(FPL_BOOTSTRAP, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  [FPL] bootstrap fetch failed: {e}")
        return None


def _team_lookup(data):
    return {t["id"]: t for t in data.get("teams", [])}


def fpl_injury_stories(fpl_data=None, min_news_len=4):
    """
    Return a list of story dicts for players whose injury/suspension news is NEW
    or CHANGED since the last run. Shaped to match the bot's story schema so the
    existing render/post pipeline can consume them directly.
    """
    data = _fetch_bootstrap(fpl_data)
    if not data:
        return []
    teams = _team_lookup(data)
    state = _load_state()
    new_state = dict(state)
    stories = []

    for el in data.get("elements", []):
        status = (el.get("status") or "a").lower()
        news = (el.get("news") or "").strip()
        pid = str(el.get("id"))

        # Only care about non-available players who have actual news text.
        if status == "a" or not news or len(news) < min_news_len:
            # Clear stale state if the player is fit again (lets a future
            # re-injury post correctly).
            if pid in new_state and status == "a":
                new_state.pop(pid, None)
            continue

        prev_news = state.get(pid)
        if prev_news == news:
            continue  # unchanged — already handled, don't repost

        # Record the new news so we don't repeat it next run.
        new_state[pid] = news

        event = _STATUS_LABEL.get(status, "injury")
        # suspension keywords inside news text override status when clearer
        nlow = news.lower()
        if any(w in nlow for w in ("suspend", "ban", "red card")):
            event = "suspension"

        team = teams.get(el.get("team"), {})
        team_name = team.get("name", "")
        player_name = el.get("web_name") or (
            (el.get("first_name", "") + " " + el.get("second_name", "")).strip())
        chance = el.get("chance_of_playing_next_round")

        stories.append({
            "is_football": True,
            "event": event,
            "is_real_move": False,
            "player": player_name,
            "from_club": team_name or None,
            "to_club": None,
            "from_key": None,
            "to_key": None,
            "fee": None, "contract": None, "conditional": None,
            "diagnosis": news,
            "expected_return": (f"{chance}% chance next match"
                                if chance is not None else None),
            "next_match": None,
            "stage": 3 if status in ("i", "s", "u") else 2,
            "collapsed": False,
            "historical": False,
            "headline": player_name,
            "body": f"{player_name} ({team_name}): {news}" if team_name
                    else f"{player_name}: {news}",
            "confidence": 0.95,             # first-party official data
            "from_fallback": False,
            "direction_confident": True,
            "from_video": False,
            "has_written_claim": True,
            "fpl_official": True,           # marks this as CONFIRMED-grade
            "id": f"fpl_{pid}_{el.get('news_added') or ''}",
            "sources": ["OfficialFPL"],
        })

    _save_state(new_state)
    if stories:
        print(f"  [FPL] {len(stories)} new injury/suspension update(s) from FPL feed.")
    return stories
