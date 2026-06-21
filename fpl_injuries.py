"""
FPL injury & suspension source (PRIMARY, structured, free, official).

Reads the official Fantasy Premier League player feed and turns availability
changes into postable stories — no scraping, no Gemini needed. Each player
carries:
  status: 'a' available, 'i' injured, 's' suspended, 'd' doubtful, 'u' unavailable
  news: human text e.g. "Hamstring injury - 75% chance of playing"
  chance_of_playing_next_round: 0..100 or null
  news_added: ISO date the news was set (e.g. "2026-06-18T10:00:00Z")

DESIGN (important — read before editing):

1) CHANGE DETECTION: we only emit a story when a player's `news` text differs
   from what we last *posted*. This prevents reposting the same injury.

2) DATE-AWARE: an injury whose `news_added` is older than RECENT_DAYS is treated
   as stale (e.g. left over from a finished season) and is NOT posted. This stops
   the off-season "backlog dump" that posts months-old injuries as if new.

3) SILENT SEEDING: the FIRST time the bot ever runs (no state file), it records
   everything currently in the feed WITHOUT posting it. After that, only genuine
   *changes* post. This is the "start from today" behaviour, done safely.

4) NO-MISS GUARANTEE: state is only committed for a story AFTER it has actually
   been posted — see commit_posted_fpl(). If a story is generated but then held
   back by the daily cap, its state is NOT advanced, so it will be regenerated
   and retried on the next run until it posts. Nothing is lost to the cap.

State is kept in fpl_player_state.json:
  { "posted": { "<player_id>": "<news text we have already posted>" },
    "seeded": true }
"""

import json
import urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta

FPL_BOOTSTRAP = "https://fantasy.premierleague.com/api/bootstrap-static/"
STATE_FILE = Path("fpl_player_state.json")

# An FPL injury whose news_added is older than this many days is considered
# stale and will not be posted. Tunable; 3 days is a good default.
RECENT_DAYS = 3

_STATUS_LABEL = {
    "i": "injury",
    "s": "suspension",
    "d": "injury",     # doubtful → treat as injury update
    "u": "injury",     # unavailable → injury update
}


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            raw = json.loads(STATE_FILE.read_text())
            # migrate any old flat {pid: news} format into the new shape
            if "posted" not in raw:
                return {"posted": dict(raw), "seeded": True}
            raw.setdefault("posted", {})
            raw.setdefault("seeded", True)
            return raw
        except Exception:
            return {"posted": {}, "seeded": False}
    return {"posted": {}, "seeded": False}


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


def _is_recent(news_added: str, recent_days: int) -> bool:
    """True if news_added is within recent_days. Missing/garbled dates are
    treated as NOT recent (safer: avoids posting undateable stale items)."""
    if not news_added:
        return False
    try:
        s = news_added.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - dt
        return age <= timedelta(days=recent_days)
    except Exception:
        return False


def _build_story(el, teams):
    status = (el.get("status") or "a").lower()
    news = (el.get("news") or "").strip()
    pid = str(el.get("id"))
    event = _STATUS_LABEL.get(status, "injury")
    nlow = news.lower()
    if any(w in nlow for w in ("suspend", "ban", "red card")):
        event = "suspension"
    team = teams.get(el.get("team"), {})
    team_name = team.get("name", "")
    player_name = el.get("web_name") or (
        (el.get("first_name", "") + " " + el.get("second_name", "")).strip())
    chance = el.get("chance_of_playing_next_round")
    return {
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
        "fpl_pid": pid,                 # used by commit_posted_fpl()
        "fpl_news": news,               # the exact news string to commit on post
        "sources": ["OfficialFPL"],
    }


def fpl_injury_stories(fpl_data=None, min_news_len=4, recent_days=RECENT_DAYS):
    """
    Return story dicts for players whose injury/suspension news is NEW/CHANGED
    versus what we have already POSTED, AND whose news is recent (not stale
    off-season leftovers).

    IMPORTANT: this function does NOT advance posted-state. State is only
    committed after a story actually posts, via commit_posted_fpl(). That is the
    no-miss guarantee: a cap-blocked story will reappear next run until posted.
    """
    data = _fetch_bootstrap(fpl_data)
    if not data:
        return []
    teams = _team_lookup(data)
    state = _load_state()
    posted = state.get("posted", {})

    # ── FIRST-RUN SILENT SEEDING ─────────────────────────────────────────
    # If we've never run before, record what's currently flagged WITHOUT
    # posting it, so we don't dump a backlog of pre-existing injuries.
    if not state.get("seeded"):
        seeded = {}
        for el in data.get("elements", []):
            status = (el.get("status") or "a").lower()
            news = (el.get("news") or "").strip()
            if status != "a" and news and len(news) >= min_news_len:
                seeded[str(el.get("id"))] = news
        _save_state({"posted": seeded, "seeded": True})
        print(f"  [FPL] First run — seeded {len(seeded)} existing injury record(s) "
              f"silently (not posted). Only NEW changes will post from now on.")
        return []

    stories = []
    stale_skipped = 0
    for el in data.get("elements", []):
        status = (el.get("status") or "a").lower()
        news = (el.get("news") or "").strip()
        pid = str(el.get("id"))

        if status == "a" or not news or len(news) < min_news_len:
            continue

        # Already posted this exact news? skip.
        if posted.get(pid) == news:
            continue

        # DATE GUARD: don't post stale (e.g. off-season leftover) injuries.
        if not _is_recent(el.get("news_added"), recent_days):
            stale_skipped += 1
            continue

        stories.append(_build_story(el, teams))

    if stale_skipped:
        print(f"  [FPL] skipped {stale_skipped} stale injury record(s) "
              f"(older than {recent_days} days).")
    if stories:
        print(f"  [FPL] {len(stories)} recent injury/suspension update(s) ready.")
    return stories


def commit_posted_fpl(story: dict):
    """Call this AFTER an FPL story has actually been posted. It advances the
    posted-state so the same injury isn't posted again — but only now that it's
    truly out. Cap-blocked stories never reach here, so they retry next run."""
    pid = story.get("fpl_pid")
    news = story.get("fpl_news")
    if not pid or not news:
        return
    state = _load_state()
    state.setdefault("posted", {})[pid] = news
    state["seeded"] = True
    _save_state(state)


def clear_fit_players(fpl_data=None):
    """Housekeeping: if a previously-injured player is now available, drop them
    from posted-state so a FUTURE re-injury posts correctly. Safe to call each
    run; never posts anything."""
    data = _fetch_bootstrap(fpl_data)
    if not data:
        return
    state = _load_state()
    posted = state.get("posted", {})
    if not posted:
        return
    fit = set()
    for el in data.get("elements", []):
        if (el.get("status") or "a").lower() == "a":
            fit.add(str(el.get("id")))
    changed = False
    for pid in list(posted.keys()):
        if pid in fit:
            posted.pop(pid, None)
            changed = True
    if changed:
        state["posted"] = posted
        _save_state(state)
