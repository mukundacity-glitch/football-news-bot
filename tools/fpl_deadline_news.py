#!/usr/bin/env python3
"""FPL VORTEX — pre-deadline Top-5 team-news poster.

This is intentionally stricter than a rumour/lineup bot:

* It uses the official FPL deadline timestamp from bootstrap-static. The FPL
  deadline is already the first Premier League kickoff minus 90 minutes, so the
  bot posts at `deadline - 30 minutes` (effectively two hours before kickoff).
* It selects the current official Premier League table Top N teams. If the
  season table is empty before GW1 (all teams played 0), it falls back to FPL
  team strength so the first gameweek still focuses on the strongest clubs
  rather than alphabetical table order.
* It posts only FPL availability/status news: injury, suspension, doubtful, or
  an explicit fit/available return update. No lineups are guessed and no player
  is called a starter unless the official data says so.
* It posts at most one concise text-card tweet per gameweek, with up to three
  critical players. If there are no critical items, it posts nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
PL_FIXTURES_URL = (
    "https://footballapi.pulselive.com/football/fixtures"
    "?comps=1&page=0&pageSize=1&sort=asc&statuses=U,L"
)
PL_STANDINGS_URL = (
    "https://footballapi.pulselive.com/football/standings"
    "?comps=1&compSeasons={season_id}&page=0&pageSize=20&altIds=true"
)
PL_HEADERS = {
    "Origin": "https://www.premierleague.com",
    "User-Agent": "FPLVortexBot/2.0 (+https://github.com/mukundacity-glitch/football-news-bot)",
    "Accept": "application/json",
}
FPL_HEADERS = {"User-Agent": PL_HEADERS["User-Agent"], "Accept": "application/json"}

POSTED_FILE = Path("data/posted_news.json")
STATUS_FILE = Path("data/fpl_deadline_status.json")

CHANNEL_HANDLE = os.getenv("CHANNEL_HANDLE", "@FPLVortex")
TOP_N = int(os.getenv("FPL_DEADLINE_TOP_N", "5"))
CARD_LIMIT = int(os.getenv("FPL_DEADLINE_CARD_LIMIT", "3"))
MARGIN_MINUTES = int(os.getenv("FPL_DEADLINE_MARGIN_MINUTES", "30"))
WINDOW_MINUTES = int(os.getenv("FPL_DEADLINE_WINDOW_MINUTES", "20"))
DAILY_POST_LIMIT = int(os.getenv("DAILY_POST_LIMIT", "16"))
COOLDOWN_FLAGGED_MIN = int(os.getenv("COOLDOWN_FLAGGED_MIN", "180"))
COOLDOWN_RATELIMIT_MIN = int(os.getenv("COOLDOWN_RATELIMIT_MIN", "30"))
ENABLE_AUTOPOST = os.getenv("ENABLE_AUTOPOST", "").lower() == "true"

TWIKIT_SUCCESS_PARSE_KEYS = {
    "urls", "withheld_in_countries", "pinned_tweet_ids_str",
    "entities", "extended_entities", "card",
}
X_DUPLICATE_CODES = {"187"}
X_FLAG_CODES = {"226", "326", "334", "64", "261"}
X_RATELIMIT_CODES = {"429", "88"}
X_AUTH_CODES = {"32", "89", "99", "135", "215", "401"}

TEAM_ALIASES = {
    "arsenal": {"arsenal"},
    "aston villa": {"aston villa", "villa"},
    "bournemouth": {"bournemouth", "afc bournemouth"},
    "brentford": {"brentford"},
    "brighton": {"brighton", "brighton hove albion", "brighton & hove albion"},
    "burnley": {"burnley"},
    "chelsea": {"chelsea"},
    "crystal palace": {"crystal palace", "palace"},
    "everton": {"everton"},
    "fulham": {"fulham"},
    "leeds": {"leeds", "leeds united"},
    "liverpool": {"liverpool"},
    "man city": {"man city", "manchester city", "mci"},
    "man utd": {"man utd", "man united", "manchester united", "mun"},
    "newcastle": {"newcastle", "newcastle united"},
    "nottingham forest": {"nottingham forest", "nottm forest", "nott'm forest"},
    "sunderland": {"sunderland"},
    "tottenham": {"tottenham", "spurs", "tottenham hotspur"},
    "west ham": {"west ham", "west ham united"},
    "wolves": {"wolves", "wolverhampton", "wolverhampton wanderers"},
}


@dataclass(frozen=True)
class DeadlineEvent:
    event_id: int | str
    name: str
    deadline: datetime
    target_run: datetime


@dataclass(frozen=True)
class TeamNewsCard:
    player: str
    team_name: str
    team_short: str
    symbol: str
    reason: str
    priority: int
    selected_by: float
    cost: int


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(raw: str) -> datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fetch_json(url: str, *, headers: dict[str, str] | None = None, timeout: int = 20) -> dict[str, Any]:
    resp = requests.get(url, headers=headers or {}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_fpl_bootstrap() -> dict[str, Any]:
    return fetch_json(FPL_BOOTSTRAP_URL, headers=FPL_HEADERS)


def find_next_event(fpl: dict[str, Any], now: datetime | None = None) -> DeadlineEvent | None:
    now = now or utcnow()
    candidates: list[DeadlineEvent] = []
    for ev in fpl.get("events", []) or []:
        raw_deadline = ev.get("deadline_time")
        if not raw_deadline:
            continue
        try:
            deadline = parse_dt(raw_deadline)
        except Exception:
            continue
        # Keep the current event until the actual deadline passes, so a delayed
        # runner inside the final pre-deadline window still sees the right GW.
        if deadline <= now:
            continue
        event_id = ev.get("id") or ev.get("name") or deadline.strftime("%Y%m%d%H%M")
        name = ev.get("name") or f"GW{event_id}"
        candidates.append(
            DeadlineEvent(
                event_id=event_id,
                name=name,
                deadline=deadline,
                target_run=deadline - timedelta(minutes=MARGIN_MINUTES),
            )
        )
    if not candidates:
        return None
    return min(candidates, key=lambda e: e.deadline)


def deadline_window_status(
    fpl: dict[str, Any],
    *,
    now: datetime | None = None,
    margin_minutes: int = MARGIN_MINUTES,
    window_minutes: int = WINDOW_MINUTES,
) -> tuple[bool, str, DeadlineEvent | None]:
    now = now or utcnow()
    event = find_next_event(fpl, now)
    if event is None:
        return False, "no_future_fpl_deadline", None
    target = event.deadline - timedelta(minutes=margin_minutes)
    window_end = target + timedelta(minutes=window_minutes)
    if target <= now < window_end and now < event.deadline:
        return True, f"inside_{event.name}_pre_deadline_window", event
    if now < target:
        return False, f"too_early_for_{event.name}_target_{target.isoformat()}", event
    return False, f"outside_{event.name}_window_target_{target.isoformat()}", event


def normalize_team_name(value: str | None) -> str:
    value = (value or "").lower()
    value = value.replace("&", " and ")
    value = re.sub(r"\b(fc|afc|the)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = value.replace("brighton and hove albion", "brighton")
    value = value.replace("wolverhampton wanderers", "wolves")
    value = value.replace("manchester city", "man city")
    value = value.replace("manchester united", "man utd")
    value = value.replace("newcastle united", "newcastle")
    value = value.replace("west ham united", "west ham")
    value = value.replace("tottenham hotspur", "tottenham")
    value = value.replace("leeds united", "leeds")
    return value


def alias_set(name: str | None) -> set[str]:
    norm = normalize_team_name(name)
    out = {norm}
    for canonical, variants in TEAM_ALIASES.items():
        if norm == canonical or norm in {normalize_team_name(v) for v in variants}:
            out.add(canonical)
            out.update(normalize_team_name(v) for v in variants)
    return {v for v in out if v}


def fpl_team_lookup(fpl: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {int(t["id"]): t for t in fpl.get("teams", []) if t.get("id") is not None}


def map_table_names_to_fpl_team_ids(names: Iterable[str], fpl: dict[str, Any]) -> list[int]:
    teams = list(fpl.get("teams", []) or [])
    mapped: list[int] = []
    for table_name in names:
        table_aliases = alias_set(table_name)
        matched = None
        for team in teams:
            names_to_try = [team.get("name"), team.get("short_name"), team.get("code")]
            team_aliases: set[str] = set()
            for candidate in names_to_try:
                team_aliases.update(alias_set(str(candidate or "")))
            if table_aliases & team_aliases:
                matched = int(team["id"])
                break
        if matched is not None and matched not in mapped:
            mapped.append(matched)
    return mapped


def fallback_top_team_ids_by_fpl_strength(fpl: dict[str, Any], n: int = TOP_N) -> list[int]:
    teams = list(fpl.get("teams", []) or [])
    def strength(team: dict[str, Any]) -> tuple[int, int, str]:
        # Lower FPL strength numbers are stronger in current bootstrap-static.
        s = int(team.get("strength") or 99)
        total = int(team.get("strength_overall_home") or 0) + int(team.get("strength_overall_away") or 0)
        return (s, -total, str(team.get("name") or ""))
    ranked = sorted(teams, key=strength)
    return [int(t["id"]) for t in ranked[:n] if t.get("id") is not None]


def fetch_pl_table_top_team_ids(fpl: dict[str, Any], n: int = TOP_N) -> tuple[list[int], str]:
    """Return current official PL table Top N mapped to FPL team IDs.

    If the table is all played=0 (pre-GW1 alphabetical order), fall back to FPL
    team strength so the first pre-deadline alert still focuses on the strongest
    teams rather than alphabetical placeholders.
    """
    try:
        fixtures = fetch_json(PL_FIXTURES_URL, headers=PL_HEADERS)
        first_fixture = (fixtures.get("content") or [{}])[0]
        season_id = (
            first_fixture.get("gameweek", {})
            .get("compSeason", {})
            .get("id")
        )
        if not season_id:
            raise ValueError("could not determine current PL compSeason id")
        standings = fetch_json(PL_STANDINGS_URL.format(season_id=int(float(season_id))), headers=PL_HEADERS)
        entries = (standings.get("tables") or [{}])[0].get("entries") or []
        entries = sorted(entries, key=lambda e: int(e.get("position") or 999))
        played = [int((e.get("overall") or {}).get("played") or 0) for e in entries]
        if entries and all(p == 0 for p in played):
            return fallback_top_team_ids_by_fpl_strength(fpl, n), "fpl_strength_table_empty"
        names = [((e.get("team") or {}).get("name") or "") for e in entries[:n]]
        mapped = map_table_names_to_fpl_team_ids(names, fpl)
        if len(mapped) >= min(n, 3):
            return mapped[:n], "official_pl_table"
        raise ValueError(f"only mapped {len(mapped)} table teams")
    except Exception as exc:
        return fallback_top_team_ids_by_fpl_strength(fpl, n), f"fpl_strength_fallback:{exc}"


def parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def short_reason(news: str, fallback: str) -> str:
    text = (news or fallback or "").strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\bexpected back\b.*$", "", text, flags=re.I).strip(" .;-:") or text
    words = text.split()
    if len(words) > 5:
        text = " ".join(words[:5]) + "…"
    if len(text) > 42:
        text = text[:39].rstrip() + "…"
    return text or fallback


def classify_status(el: dict[str, Any]) -> tuple[str, int, str] | None:
    """Return (symbol, priority, fallback_reason) or None if not critical.

    Priority order: confirmed outs/suspensions (0), confirmed returns/fit (1),
    major doubts (2). Lower number is more important.
    """
    status = str(el.get("status") or "a").lower()
    news = str(el.get("news") or "")
    lower = news.lower()
    chance = el.get("chance_of_playing_next_round")
    if chance is None:
        chance = el.get("chance_of_playing_this_round")
    try:
        chance_int = int(chance) if chance is not None else None
    except Exception:
        chance_int = None

    if status == "s" or "suspended" in lower or "suspension" in lower:
        return "❌", 0, "Suspended"
    if status in {"i", "u", "n"} or chance_int == 0 or re.search(r"\b(ruled out|out injured|out with|will miss|unavailable)\b", lower):
        return "❌", 0, "Ruled out"
    if status == "a" and re.search(r"\b(fit|available|returns?|back in training|back available|has trained)\b", lower):
        return "✅", 1, "Available"
    if status == "d" or (chance_int is not None and chance_int < 100) or re.search(r"\b(doubt|doubtful|late fitness|assessment)\b", lower):
        if chance_int is not None:
            return "⚠️", 2, f"{chance_int}% chance"
        return "⚠️", 2, "Doubtful"
    return None


def collect_cards(
    fpl: dict[str, Any],
    top_team_ids: Iterable[int],
    *,
    limit: int = CARD_LIMIT,
) -> list[TeamNewsCard]:
    team_ids = {int(t) for t in top_team_ids}
    teams = fpl_team_lookup(fpl)
    candidates: list[TeamNewsCard] = []
    for el in fpl.get("elements", []) or []:
        try:
            team_id = int(el.get("team"))
        except Exception:
            continue
        if team_id not in team_ids:
            continue
        status = classify_status(el)
        if status is None:
            continue
        news = str(el.get("news") or "").strip()
        if not news:
            # No official FPL status text = no post. Prevents vague statuses.
            continue
        symbol, priority, fallback = status
        team = teams.get(team_id, {})
        player = " ".join(
            p for p in [str(el.get("first_name") or "").strip(), str(el.get("second_name") or "").strip()] if p
        ).strip() or str(el.get("web_name") or "Player")
        candidates.append(
            TeamNewsCard(
                player=player,
                team_name=str(team.get("name") or team.get("short_name") or ""),
                team_short=str(team.get("short_name") or team.get("name") or "").upper()[:4],
                symbol=symbol,
                reason=short_reason(news, fallback),
                priority=priority,
                selected_by=parse_float(el.get("selected_by_percent")),
                cost=int(el.get("now_cost") or 0),
            )
        )
    ranked = sorted(candidates, key=lambda c: (c.priority, -c.selected_by, -c.cost, c.player))
    chosen = ranked[:limit]
    return sorted(chosen, key=lambda c: (c.team_name.lower(), c.player.lower()))


def build_tweet(cards: list[TeamNewsCard], event: DeadlineEvent) -> str:
    lines = ["🚨 FPL TOP 5 TEAM NEWS", "Official FPL availability only", ""]
    for card in cards:
        lines.append(f"{card.player} | [{card.team_short}] | {card.symbol}")
        lines.append(f"({card.reason})")
        lines.append("")
    lines.append(f"Source: Official FPL | {CHANNEL_HANDLE}")
    lines.append("#FPL #PremierLeague")
    text = "\n".join(lines).strip()
    if len(text) <= 280:
        return text
    # Last-resort trimming: keep all selected cards but shorten reasons.
    compact = ["🚨 FPL TOP 5 TEAM NEWS", "Official FPL only", ""]
    for card in cards:
        compact.append(f"{card.player} | [{card.team_short}] | {card.symbol}")
        compact.append(f"({card.reason[:24].rstrip()}…)")
    compact.append(f"Source: Official FPL | {CHANNEL_HANDLE}")
    compact.append("#FPL")
    return "\n".join(compact).strip()[:280]


def deadline_post_id(event: DeadlineEvent, cards: list[TeamNewsCard]) -> str:
    # One post per gameweek deadline. The card hash is stored in metadata for
    # audit only; it is not used to allow reposting changed cards in the same GW.
    return f"fpl_deadline_top5_gw_{event.event_id}"


def card_hash(cards: list[TeamNewsCard]) -> str:
    payload = "|".join(f"{c.player}:{c.team_short}:{c.symbol}:{c.reason}" for c in cards)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def fresh_state() -> dict[str, Any]:
    return {"daily": {"date": "", "count": 0, "limit": DAILY_POST_LIMIT}, "stories": {}, "posted_ids": []}


def load_state() -> dict[str, Any]:
    if POSTED_FILE.exists():
        with POSTED_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = fresh_state()
    data.setdefault("daily", fresh_state()["daily"])
    data.setdefault("stories", {})
    data.setdefault("posted_ids", [])
    data.setdefault("fpl_deadline_news", {})
    return data


def save_state(data: dict[str, Any]) -> None:
    POSTED_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = POSTED_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(POSTED_FILE)


def write_status(payload: dict[str, Any]) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["updated_at"] = utcnow().isoformat()
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    tmp.replace(STATUS_FILE)


def sync_daily(data: dict[str, Any]) -> None:
    today = utcnow().strftime("%Y-%m-%d")
    current = data.get("daily") or {}
    count = int(current.get("count") or 0) if current.get("date") == today else 0
    data["daily"] = {"date": today, "count": count, "limit": DAILY_POST_LIMIT}


def daily_limit_ok(data: dict[str, Any]) -> bool:
    sync_daily(data)
    limit = int(data["daily"].get("limit") or DAILY_POST_LIMIT)
    return True if limit <= 0 else int(data["daily"].get("count") or 0) < limit


def in_cooldown(data: dict[str, Any]) -> bool:
    raw = data.get("cooldown_until")
    if not raw:
        return False
    try:
        return utcnow() < parse_dt(raw)
    except Exception:
        return False


def set_cooldown(data: dict[str, Any], kind: str) -> None:
    minutes = COOLDOWN_FLAGGED_MIN if kind == "flagged" else COOLDOWN_RATELIMIT_MIN
    until = utcnow() + timedelta(minutes=minutes)
    data["cooldown_until"] = until.isoformat()
    save_state(data)
    print(f"[X-SAFETY] {kind.upper()} — backing off until {until.isoformat()}")


def classify_x_error(exc: Exception) -> str:
    text = str(exc).lower()
    cls = type(exc).__name__.lower()
    codes = set(re.findall(r"code[\"']?\s*[:=]\s*[\"']?(\d+)", text))
    if codes & X_DUPLICATE_CODES or "duplicate" in text or "duplicatetweet" in cls:
        return "duplicate"
    if codes & X_RATELIMIT_CODES or "toomanyrequests" in cls or "rate limit" in text:
        return "rate_limited"
    if codes & X_FLAG_CODES or "automated" in text or "spam" in text or "locked" in text or "suspend" in text:
        return "flagged"
    if codes & X_AUTH_CODES or "could not authenticate" in text or "unauthorized" in cls or "invalid or expired" in text:
        return "auth"
    return "transient"


async def post_text_to_x(text: str) -> str:
    from twikit import Client  # Imported lazily so --check-window needs no deps beyond requests.

    auth_token = os.getenv("X_POST_AUTH_TOKEN")
    ct0_token = os.getenv("X_POST_CT0_TOKEN")
    if not auth_token or not ct0_token:
        raise RuntimeError("missing X_POST_AUTH_TOKEN / X_POST_CT0_TOKEN")
    client = Client("en-US")
    client.set_cookies({"auth_token": auth_token, "ct0": ct0_token})
    try:
        await client.create_tweet(text=text)
    except KeyError as ke:
        key = str(ke).strip("'\"")
        if key in TWIKIT_SUCCESS_PARSE_KEYS:
            print(f"[WARN] twikit KeyError({ke}) after create_tweet — treating as posted.")
            return "posted_keyerror_success"
        raise
    return "posted"


def record_success(data: dict[str, Any], post_id: str, event: DeadlineEvent, cards: list[TeamNewsCard], tweet: str) -> None:
    sync_daily(data)
    if post_id not in data["posted_ids"]:
        data["posted_ids"].append(post_id)
    data.setdefault("fpl_deadline_news", {})[str(event.event_id)] = {
        "posted_at": utcnow().isoformat(),
        "deadline": event.deadline.isoformat(),
        "target_run": event.target_run.isoformat(),
        "card_hash": card_hash(cards),
        "players": [c.player for c in cards],
        "tweet": tweet,
    }
    data["daily"]["count"] = int(data["daily"].get("count") or 0) + 1
    save_state(data)


def github_output_check(window_minutes: int, margin_minutes: int) -> int:
    try:
        fpl = fetch_fpl_bootstrap()
        ok, reason, event = deadline_window_status(
            fpl, margin_minutes=margin_minutes, window_minutes=window_minutes
        )
        print(f"should_run={'true' if ok else 'false'}")
        print(f"reason={reason}")
        if event:
            print(f"event_id={event.event_id}")
            print(f"deadline={event.deadline.isoformat()}")
            print(f"target_run={(event.deadline - timedelta(minutes=margin_minutes)).isoformat()}")
        return 0
    except Exception as exc:
        print("should_run=false")
        print(f"reason=check_failed_{type(exc).__name__}:{str(exc).replace(chr(10), ' ')[:160]}")
        return 0


async def run(*, dry_run: bool = False, force_window: bool = False) -> int:
    status: dict[str, Any] = {"run_type": "fpl_deadline_news", "dry_run": dry_run, "force_window": force_window}
    try:
        fpl = fetch_fpl_bootstrap()
        ok, reason, event = deadline_window_status(fpl)
        status.update({"window_ok": ok, "window_reason": reason})
        if event:
            status.update({
                "event_id": event.event_id,
                "event_name": event.name,
                "deadline": event.deadline.isoformat(),
                "target_run": event.target_run.isoformat(),
            })
        if not force_window and not ok:
            print(f"[FPL-DEADLINE] No-op: {reason}")
            write_status(status | {"run_exit": "outside_window"})
            return 0
        if not event:
            print("[FPL-DEADLINE] No-op: no future FPL deadline found.")
            write_status(status | {"run_exit": "no_deadline"})
            return 0

        data = load_state()
        post_id = deadline_post_id(event, [])
        if post_id in data.get("posted_ids", []) or str(event.event_id) in data.get("fpl_deadline_news", {}):
            print(f"[FPL-DEADLINE] Already posted for {event.name}; skipping duplicate.")
            write_status(status | {"run_exit": "duplicate_gameweek", "post_id": post_id})
            return 0
        if in_cooldown(data):
            print(f"[FPL-DEADLINE] X cooldown active until {data.get('cooldown_until')}; skipping.")
            write_status(status | {"run_exit": "cooldown", "cooldown_until": data.get("cooldown_until")})
            return 0
        if not daily_limit_ok(data):
            print(f"[FPL-DEADLINE] Daily post cap reached ({data['daily']}); skipping.")
            write_status(status | {"run_exit": "daily_limit", "daily": data.get("daily")})
            return 0

        top_team_ids, team_source = fetch_pl_table_top_team_ids(fpl, TOP_N)
        cards = collect_cards(fpl, top_team_ids, limit=CARD_LIMIT)
        status.update({
            "top_team_ids": top_team_ids,
            "team_source": team_source,
            "card_count": len(cards),
            "cards": [c.__dict__ for c in cards],
        })
        if not cards:
            print("[FPL-DEADLINE] No critical Top-5 FPL availability news; no post.")
            write_status(status | {"run_exit": "no_critical_news"})
            return 0
        tweet = build_tweet(cards, event)
        post_id = deadline_post_id(event, cards)
        print("[FPL-DEADLINE] Prepared tweet:\n" + tweet)
        if dry_run or not ENABLE_AUTOPOST:
            reason = "dry_run" if dry_run else "autopost_disabled"
            print(f"[FPL-DEADLINE] Not posting ({reason}).")
            write_status(status | {"run_exit": reason, "tweet": tweet, "post_id": post_id})
            return 0

        try:
            result = await post_text_to_x(tweet)
        except Exception as exc:
            kind = classify_x_error(exc)
            if kind == "duplicate":
                print("[FPL-DEADLINE] X says duplicate; recording post id to avoid retry loops.")
                record_success(data, post_id, event, cards, tweet)
                write_status(status | {"run_exit": "x_duplicate_recorded", "post_id": post_id})
                return 0
            if kind in {"flagged", "rate_limited"}:
                set_cooldown(data, kind)
                write_status(status | {"run_exit": f"x_{kind}", "error": str(exc)[:300]})
                return 2
            if kind == "auth":
                print("[FPL-DEADLINE] X auth expired; refresh X_POST_AUTH_TOKEN/X_POST_CT0_TOKEN.")
                write_status(status | {"run_exit": "x_auth_expired", "error": str(exc)[:300]})
                return 2
            print(f"[FPL-DEADLINE] Transient post error: {exc}")
            write_status(status | {"run_exit": "x_transient_error", "error": str(exc)[:300]})
            return 1

        record_success(data, post_id, event, cards, tweet)
        print(f"[FPL-DEADLINE] ✅ POSTED {event.name} Top-5 team news ({result}).")
        write_status(status | {"run_exit": "posted", "post_id": post_id, "tweet": tweet})
        return 0
    except Exception as exc:
        print(f"[FPL-DEADLINE] ERROR: {type(exc).__name__}: {exc}")
        try:
            write_status(status | {"run_exit": "error", "error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Post strict FPL Top-5 team news before the GW deadline.")
    parser.add_argument("--check-window", action="store_true", help="Print GitHub output variables only.")
    parser.add_argument("--window-minutes", type=int, default=WINDOW_MINUTES)
    parser.add_argument("--margin-minutes", type=int, default=MARGIN_MINUTES)
    parser.add_argument("--dry-run", action="store_true", help="Build the tweet but do not post or record state.")
    parser.add_argument("--force-window", action="store_true", help="Run even outside the deadline window (manual testing only).")
    args = parser.parse_args()
    if args.check_window:
        return github_output_check(args.window_minutes, args.margin_minutes)
    return asyncio.run(run(dry_run=args.dry_run, force_window=args.force_window))


if __name__ == "__main__":
    raise SystemExit(main())
