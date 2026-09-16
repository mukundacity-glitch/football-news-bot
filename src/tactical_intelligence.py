"""Premier League tactical intelligence agent.

The agent is intentionally fail-closed. A publishable post requires an official
Premier League/FPL fixture confirmation plus matching structured match data.
Teams, players, fixtures, scores, topics, conclusions and source references are
selected from live responses at run time; no club list or story is embedded.
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
from statistics import mean
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import requests
from twikit import Client

from src.rendering.tactical import TacticalGraphicRenderer
from src.x_delivery import create_tweet_confirmed

OFFICIAL_BOOTSTRAP_ENDPOINT = "https://fantasy.premierleague.com/api/bootstrap-static/"
OFFICIAL_FIXTURES_ENDPOINT = "https://fantasy.premierleague.com/api/fixtures/"
STRUCTURED_ENDPOINT = "https://v3.football.api-sports.io"
PREMIER_LEAGUE_PROVIDER_ID = 39

CONFIG_PATH = Path(os.getenv("TACTICAL_CONFIG_PATH", "config/tactical_intelligence.json"))
STATE_PATH = Path("data/tactical_posts.json")
RUN_STATUS_PATH = Path("data/tactical_last_run.json")
OUTPUT_DIR = Path("queue/tactical")
ANALYTICS_PATH = Path("data/account_analytics.json")


@dataclass(frozen=True)
class Retrieved:
    data: Any
    url: str
    checked_at: str
    digest: str


@dataclass(frozen=True)
class Fixture:
    fixture_id: int
    home_id: int
    away_id: int
    home: str
    away: str
    kickoff: datetime
    finished: bool
    home_score: int | None
    away_score: int | None


@dataclass(frozen=True)
class Candidate:
    mode: str
    fixture: Fixture
    post: dict[str, Any]
    priority_score: int
    source_records: list[dict[str, str]]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def normalize_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def word_count(value: object) -> int:
    return len(str(value or "").split())


def bounded_sentence(value: str, max_words: int) -> str:
    words = str(value or "").split()
    return " ".join(words[:max_words]).rstrip(" ,;:-")


def fit_thesis(value: str) -> str:
    words = value.split()
    if len(words) < 8:
        words += ["in", "this", "Premier", "League", "matchup"][: 8 - len(words)]
    return " ".join(words[:14]).rstrip(" ,;:-")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def load_config() -> dict[str, Any]:
    config = load_json(CONFIG_PATH, {})
    required = {
        "audience_timezone",
        "watch_local_time",
        "review_local_time",
        "schedule_window_minutes",
        "watch_fixture_min_hours",
        "watch_fixture_max_hours",
        "review_min_after_full_time_minutes",
        "review_max_after_full_time_minutes",
        "minimum_publish_score",
        "max_posts_per_day",
        "max_posts_per_type_per_day",
    }
    missing = sorted(required.difference(config))
    if missing:
        raise RuntimeError(f"tactical config missing: {', '.join(missing)}")
    ZoneInfo(str(config["audience_timezone"]))
    return config


def request_json(url: str, *, headers: Mapping[str, str] | None = None, params: Mapping[str, Any] | None = None) -> Retrieved:
    response = requests.get(url, headers=dict(headers or {}), params=dict(params or {}), timeout=25)
    response.raise_for_status()
    raw = response.content
    return Retrieved(
        data=response.json(),
        url=response.url,
        checked_at=iso(utcnow()),
        digest=hashlib.sha256(raw).hexdigest(),
    )


def source_record(kind: str, retrieved: Retrieved) -> dict[str, str]:
    return {
        "kind": kind,
        "url": retrieved.url,
        "retrieved_at": retrieved.checked_at,
        "sha256": retrieved.digest,
        "verification": "confirmed",
    }


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def official_snapshot() -> tuple[Retrieved, Retrieved, dict[int, dict[str, Any]], list[Fixture], dict[int, float]]:
    bootstrap = request_json(OFFICIAL_BOOTSTRAP_ENDPOINT)
    fixtures_raw = request_json(OFFICIAL_FIXTURES_ENDPOINT)
    teams = {int(row["id"]): row for row in bootstrap.data.get("teams", [])}
    fixtures: list[Fixture] = []
    for row in fixtures_raw.data:
        kickoff_raw = row.get("kickoff_time")
        home_id, away_id = int(row.get("team_h") or 0), int(row.get("team_a") or 0)
        if not kickoff_raw or home_id not in teams or away_id not in teams:
            continue
        fixtures.append(
            Fixture(
                fixture_id=int(row["id"]),
                home_id=home_id,
                away_id=away_id,
                home=str(teams[home_id]["name"]),
                away=str(teams[away_id]["name"]),
                kickoff=parse_dt(kickoff_raw),
                finished=bool(row.get("finished")),
                home_score=row.get("team_h_score"),
                away_score=row.get("team_a_score"),
            )
        )
    ownership: dict[int, float] = {team_id: 0.0 for team_id in teams}
    for player in bootstrap.data.get("elements", []):
        try:
            ownership[int(player["team"])] += float(player.get("selected_by_percent") or 0)
        except (TypeError, ValueError, KeyError):
            continue
    return bootstrap, fixtures_raw, teams, fixtures, ownership


def provider_key() -> str:
    return (os.getenv("API_FOOTBALL_KEY") or os.getenv("APIFOOTBALL_KEY") or "").strip()


def provider_get(path: str, params: Mapping[str, Any]) -> Retrieved:
    key = provider_key()
    if not key:
        raise RuntimeError("API_FOOTBALL_KEY is required for tactical verification")
    return request_json(
        f"{STRUCTURED_ENDPOINT}/{path.lstrip('/')}",
        headers={"x-apisports-key": key},
        params=params,
    )


def season_for(value: datetime) -> int:
    local = value.astimezone(timezone.utc)
    return local.year if local.month >= 7 else local.year - 1


def provider_fixture(official: Fixture) -> tuple[dict[str, Any], Retrieved] | tuple[None, Retrieved]:
    day = official.kickoff.date().isoformat()
    result = provider_get(
        "fixtures",
        {"league": PREMIER_LEAGUE_PROVIDER_ID, "season": season_for(official.kickoff), "date": day},
    )
    home_key, away_key = normalize_name(official.home), normalize_name(official.away)
    for row in result.data.get("response", []):
        teams = row.get("teams") or {}
        if normalize_name((teams.get("home") or {}).get("name")) != home_key:
            continue
        if normalize_name((teams.get("away") or {}).get("name")) != away_key:
            continue
        try:
            provider_kickoff = datetime.fromtimestamp(int(row["fixture"]["timestamp"]), timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        if abs((provider_kickoff - official.kickoff).total_seconds()) <= 3 * 3600:
            return row, result
    return None, result


def numeric(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).replace("%", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def stat_map(row: Mapping[str, Any]) -> dict[str, float]:
    found: dict[str, float] = {}
    for item in row.get("statistics", []) or []:
        value = numeric(item.get("value"))
        if value is not None:
            found[str(item.get("type") or "").casefold()] = value
    return found


def match_stats(provider_fixture_id: int) -> tuple[list[dict[str, Any]], Retrieved]:
    result = provider_get("fixtures/statistics", {"fixture": provider_fixture_id})
    return list(result.data.get("response", [])), result


def row_for_team(rows: Iterable[Mapping[str, Any]], team_id: int) -> Mapping[str, Any] | None:
    for row in rows:
        try:
            if int((row.get("team") or {}).get("id")) == int(team_id):
                return row
        except (TypeError, ValueError):
            pass
    return None


def recent_team_profile(team_id: int, at: datetime) -> tuple[dict[str, float], list[dict[str, str]]]:
    fixtures_result = provider_get(
        "fixtures",
        {
            "league": PREMIER_LEAGUE_PROVIDER_ID,
            "season": season_for(at),
            "team": team_id,
            "last": 3,
        },
    )
    samples: dict[str, list[float]] = {
        "shots": [],
        "shots_on_goal": [],
        "corners": [],
        "possession": [],
        "opp_shots": [],
        "opp_corners": [],
    }
    records = [source_record("structured_recent_fixtures", fixtures_result)]
    for fixture_row in fixtures_result.data.get("response", [])[-3:]:
        fixture_id = int((fixture_row.get("fixture") or {}).get("id") or 0)
        if not fixture_id:
            continue
        rows, stats_result = match_stats(fixture_id)
        records.append(source_record("structured_match_statistics", stats_result))
        own = row_for_team(rows, team_id)
        opponents = [row for row in rows if own is not row]
        if own is None or not opponents:
            continue
        own_stats, opp_stats = stat_map(own), stat_map(opponents[0])
        mapping = {
            "shots": own_stats.get("total shots"),
            "shots_on_goal": own_stats.get("shots on goal"),
            "corners": own_stats.get("corner kicks"),
            "possession": own_stats.get("ball possession"),
            "opp_shots": opp_stats.get("total shots"),
            "opp_corners": opp_stats.get("corner kicks"),
        }
        for key, value in mapping.items():
            if value is not None:
                samples[key].append(value)
    profile = {key: mean(values) for key, values in samples.items() if values}
    return profile, records


def table_position(team: Mapping[str, Any]) -> int | None:
    try:
        position = int(team.get("position"))
        return position if position > 0 else None
    except (TypeError, ValueError):
        return None


def normalized_signal(values: Mapping[int, float], team_id: int) -> float:
    if not values:
        return 0.0
    highest = max(values.values()) or 1.0
    return max(0.0, min(1.0, float(values.get(team_id, 0.0)) / highest))


def analytics_signal(team: str) -> float | None:
    data = load_json(ANALYTICS_PATH, {})
    rows = data.get("teams") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        return None
    target = normalize_name(team)
    for name, value in rows.items():
        if normalize_name(name) == target:
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                return None
    return None


def priority_score(
    fixture: Fixture,
    teams: Mapping[int, Mapping[str, Any]],
    ownership: Mapping[int, float],
    novelty: float,
    now: datetime,
    *,
    review: bool,
) -> int:
    interest_parts = []
    for team_id, name in ((fixture.home_id, fixture.home), (fixture.away_id, fixture.away)):
        owned = normalized_signal(ownership, team_id)
        account = analytics_signal(name)
        signal = owned if account is None else min(1.0, 0.55 * owned + 0.45 * account)
        interest_parts.append(signal)
    big_interest = round(25 * max(interest_parts or [0.0]))

    positions = [table_position(teams.get(fixture.home_id, {})), table_position(teams.get(fixture.away_id, {}))]
    valid_positions = [p for p in positions if p]
    importance = 6
    if valid_positions:
        best, worst = min(valid_positions), max(valid_positions)
        importance += max(0, 7 - abs((positions[0] or best) - (positions[1] or worst)))
        if best <= 4 or worst >= max(17, len(teams) - 3):
            importance += 7
    importance = min(20, importance)

    tactical_novelty = min(20, max(0, round(20 * novelty)))
    fpl_relevance = min(15, round(15 * max(normalized_signal(ownership, fixture.home_id), normalized_signal(ownership, fixture.away_id))))
    confirmed = 10
    if review:
        urgency_hours = max(0.0, (now - fixture.kickoff).total_seconds() / 3600)
        urgency = 10 if urgency_hours <= 3 else 7 if urgency_hours <= 8 else 3
    else:
        lead = max(0.0, (fixture.kickoff - now).total_seconds() / 3600)
        urgency = 10 if lead <= 24 else 7 if lead <= 36 else 3
    return min(100, big_interest + importance + tactical_novelty + fpl_relevance + confirmed + urgency)


def evidence(label: str, value: str) -> dict[str, str]:
    return {"label": bounded_sentence(label, 3), "value": bounded_sentence(value, 7)}


def build_watch(
    fixture: Fixture,
    provider_row: Mapping[str, Any],
    teams: Mapping[int, Mapping[str, Any]],
    ownership: Mapping[int, float],
    now: datetime,
    tz: ZoneInfo,
    base_sources: list[dict[str, str]],
) -> Candidate | None:
    pteams = provider_row.get("teams") or {}
    home_pid = int((pteams.get("home") or {}).get("id") or 0)
    away_pid = int((pteams.get("away") or {}).get("id") or 0)
    if not home_pid or not away_pid:
        return None
    home_profile, home_sources = recent_team_profile(home_pid, fixture.kickoff)
    away_profile, away_sources = recent_team_profile(away_pid, fixture.kickoff)
    if not home_profile or not away_profile:
        return None

    options: list[tuple[float, str, str, str, list[dict[str, str]]]] = []
    if "corners" in home_profile and "opp_corners" in away_profile:
        edge = (home_profile["corners"] + away_profile["opp_corners"]) / 2
        options.append((edge / 10, "set_piece", fixture.home, fixture.away, [
            evidence("Recent corners", f"{home_profile['corners']:.1f} created per match"),
            evidence("Opponent pressure", f"{away_profile['opp_corners']:.1f} corners conceded per match"),
            evidence("FPL angle", "Set pieces can lift goal threat"),
        ]))
    if "corners" in away_profile and "opp_corners" in home_profile:
        edge = (away_profile["corners"] + home_profile["opp_corners"]) / 2
        options.append((edge / 10, "set_piece", fixture.away, fixture.home, [
            evidence("Recent corners", f"{away_profile['corners']:.1f} created per match"),
            evidence("Opponent pressure", f"{home_profile['opp_corners']:.1f} corners conceded per match"),
            evidence("FPL angle", "Set pieces can lift goal threat"),
        ]))
    if "shots" in home_profile and "opp_shots" in away_profile:
        edge = (home_profile["shots"] + away_profile["opp_shots"]) / 2
        options.append((edge / 20, "pressure", fixture.home, fixture.away, [
            evidence("Shot volume", f"{home_profile['shots']:.1f} recent shots per match"),
            evidence("Shots allowed", f"{away_profile['opp_shots']:.1f} conceded per match"),
            evidence("Key battle", "Sustained pressure around the box"),
        ]))
    if "shots" in away_profile and "opp_shots" in home_profile:
        edge = (away_profile["shots"] + home_profile["opp_shots"]) / 2
        options.append((edge / 20, "pressure", fixture.away, fixture.home, [
            evidence("Shot volume", f"{away_profile['shots']:.1f} recent shots per match"),
            evidence("Shots allowed", f"{home_profile['opp_shots']:.1f} conceded per match"),
            evidence("Key battle", "Sustained pressure around the box"),
        ]))
    if not options:
        return None
    novelty, focus, attacking, defending, cards = max(options, key=lambda row: row[0])
    novelty = min(1.0, novelty)
    if focus == "set_piece":
        thesis = fit_thesis(f"Watch whether {attacking} turn repeated corners into a set-piece edge")
        explanation = bounded_sentence(
            f"Recent corner volume points to pressure on {defending}, but the matchup remains uncertain until the game settles.", 24
        )
        diagram_label = "Set-piece pressure"
    else:
        thesis = fit_thesis(f"The key battle may be {attacking} sustaining pressure around the box")
        explanation = bounded_sentence(
            f"Their recent shot volume meets an opponent allowing attempts, which could create repeat attacking phases rather than one-off chances.", 24
        )
        diagram_label = "Sustained pressure"
    local_kickoff = fixture.kickoff.astimezone(tz)
    score = priority_score(fixture, teams, ownership, novelty, now, review=False)
    post = {
        "heading": "FPL TACTICAL WATCH" if max(normalized_signal(ownership, fixture.home_id), normalized_signal(ownership, fixture.away_id)) >= 0.55 else "TACTICAL WATCH",
        "topic_line": f"{fixture.home} vs {fixture.away} • {local_kickoff:%a %d %b, %H:%M}",
        "thesis": thesis,
        "explanation": explanation,
        "evidence": cards[:3],
        "diagram_focus": focus,
        "diagram_label": diagram_label,
        "score_label": f"Content priority {score}/100",
        "status": "ANALYSIS",
        "source_label": "Official PL/FPL + licensed data",
        "checked_local": now.astimezone(tz).strftime("%d %b %H:%M %Z"),
    }
    return Candidate("watch", fixture, post, score, [*base_sources, *home_sources, *away_sources])


def build_review(
    fixture: Fixture,
    provider_row: Mapping[str, Any],
    teams: Mapping[int, Mapping[str, Any]],
    ownership: Mapping[int, float],
    now: datetime,
    tz: ZoneInfo,
    base_sources: list[dict[str, str]],
) -> Candidate | None:
    pteams = provider_row.get("teams") or {}
    home_pid = int((pteams.get("home") or {}).get("id") or 0)
    away_pid = int((pteams.get("away") or {}).get("id") or 0)
    fixture_id = int((provider_row.get("fixture") or {}).get("id") or 0)
    status = str(((provider_row.get("fixture") or {}).get("status") or {}).get("short") or "")
    if not fixture_id or status not in {"FT", "AET", "PEN"}:
        return None
    rows, stats_result = match_stats(fixture_id)
    home_row, away_row = row_for_team(rows, home_pid), row_for_team(rows, away_pid)
    if home_row is None or away_row is None:
        return None
    hs, aws = stat_map(home_row), stat_map(away_row)
    options: list[tuple[float, str, str, str, list[dict[str, str]]]] = []
    corner_delta = abs(hs.get("corner kicks", 0) - aws.get("corner kicks", 0))
    if corner_delta >= 3:
        leader = fixture.home if hs.get("corner kicks", 0) > aws.get("corner kicks", 0) else fixture.away
        loser = fixture.away if leader == fixture.home else fixture.home
        leader_value = max(hs.get("corner kicks", 0), aws.get("corner kicks", 0))
        options.append((min(1.0, corner_delta / 8), "set_piece", leader, loser, [
            evidence("Tactical action", "Repeated set-piece pressure built territory"),
            evidence("Verified statistic", f"{leader_value:.0f} corners won"),
            evidence("Consequence", "Defence faced repeated restart pressure"),
        ]))
    possession_delta = abs(hs.get("ball possession", 0) - aws.get("ball possession", 0))
    shot_delta = abs(hs.get("total shots", 0) - aws.get("total shots", 0))
    if possession_delta >= 10 and shot_delta >= 3:
        leader = fixture.home if hs.get("ball possession", 0) > aws.get("ball possession", 0) else fixture.away
        leader_poss = max(hs.get("ball possession", 0), aws.get("ball possession", 0))
        leader_shots = max(hs.get("total shots", 0), aws.get("total shots", 0))
        loser = fixture.away if leader == fixture.home else fixture.home
        options.append((min(1.0, (possession_delta / 30 + shot_delta / 10) / 2), "possession", leader, loser, [
            evidence("Tactical action", "Possession became sustained attacking pressure"),
            evidence("Verified statistic", f"{leader_poss:.0f}% possession, {leader_shots:.0f} shots"),
            evidence("Consequence", "Opponent defended for longer spells"),
        ]))
    target_delta = abs(hs.get("shots on goal", 0) - aws.get("shots on goal", 0))
    if target_delta >= 3:
        leader = fixture.home if hs.get("shots on goal", 0) > aws.get("shots on goal", 0) else fixture.away
        leader_targets = max(hs.get("shots on goal", 0), aws.get("shots on goal", 0))
        loser = fixture.away if leader == fixture.home else fixture.home
        options.append((min(1.0, target_delta / 7), "pressure", leader, loser, [
            evidence("Tactical action", "Cleaner entries produced better final attempts"),
            evidence("Verified statistic", f"{leader_targets:.0f} shots on target"),
            evidence("Consequence", "More attacks reached dangerous endings"),
        ]))
    if not options:
        return None
    novelty, focus, leader, loser, cards = max(options, key=lambda row: row[0])
    if focus == "set_piece":
        thesis = fit_thesis(f"A major factor was {leader} building repeated pressure from set pieces")
        explanation = bounded_sentence(
            f"The corner count supports a repeatable territorial pattern: {loser} had to defend several restarts instead of resetting comfortably.", 28
        )
        diagram_label = "Set-piece pressure"
    elif focus == "possession":
        thesis = fit_thesis(f"The clearest pattern was {leader} turning possession into sustained pressure")
        explanation = bounded_sentence(
            f"Possession and shot volume moved together, supporting the view that {leader} controlled territory rather than simply keeping the ball safely.", 28
        )
        diagram_label = "Territorial control"
    else:
        thesis = fit_thesis(f"The evidence points to {leader} creating cleaner final-third entries")
        explanation = bounded_sentence(
            f"The shots-on-target gap shows more attacks reached dangerous endings, giving {loser} fewer chances to escape without facing a save or block.", 28
        )
        diagram_label = "Final-third entries"
    score = priority_score(fixture, teams, ownership, novelty, now, review=True)
    score_text = ""
    if fixture.home_score is not None and fixture.away_score is not None:
        score_text = f"{fixture.home} {fixture.home_score}-{fixture.away_score} {fixture.away}"
    post = {
        "heading": "TACTICAL REVIEW",
        "topic_line": score_text or f"{fixture.home} vs {fixture.away}",
        "thesis": thesis,
        "explanation": explanation,
        "evidence": cards[:3],
        "diagram_focus": focus,
        "diagram_label": diagram_label,
        "score_label": f"Content priority {score}/100",
        "status": "ANALYSIS",
        "source_label": "Official PL/FPL + licensed data",
        "checked_local": now.astimezone(tz).strftime("%d %b %H:%M %Z"),
    }
    return Candidate("review", fixture, post, score, [*base_sources, source_record("structured_match_statistics", stats_result)])


def local_time_due(now: datetime, config: Mapping[str, Any], key: str) -> bool:
    tz = ZoneInfo(str(config["audience_timezone"]))
    local = now.astimezone(tz)
    hour, minute = [int(part) for part in str(config[key]).split(":", 1)]
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return abs((local - target).total_seconds()) <= int(config["schedule_window_minutes"]) * 60


def state_rows(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = state.get("posts", []) if isinstance(state, Mapping) else []
    return rows if isinstance(rows, list) else []


def posted_today(state: Mapping[str, Any], now: datetime, tz: ZoneInfo, mode: str | None = None) -> int:
    local_day = now.astimezone(tz).date().isoformat()
    return sum(1 for row in state_rows(state) if row.get("local_day") == local_day and (mode is None or row.get("mode") == mode) and row.get("published"))


def post_key(candidate: Candidate) -> str:
    raw = f"{candidate.mode}|{candidate.fixture.fixture_id}|{candidate.post['thesis']}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def caption(candidate: Candidate) -> str:
    post = candidate.post
    text = f"{post['heading']}\n{post['topic_line']}\n\n{post['thesis']}\n\n{post['explanation']}\n\n#PremierLeague #FPL"
    return text[:280]


async def publish_x(image_path: str, text: str) -> str:
    auth = (os.getenv("X_POST_AUTH_TOKEN") or os.getenv("X_AUTH_TOKEN") or "").strip()
    ct0 = (os.getenv("X_POST_CT0_TOKEN") or os.getenv("X_CT0_TOKEN") or "").strip()
    if not auth or not ct0:
        raise RuntimeError("X posting credentials are missing")
    client = Client("en-US")
    client.set_cookies({"auth_token": auth, "ct0": ct0})
    media_id = await client.upload_media(image_path, media_type="image/png")
    receipt = await create_tweet_confirmed(client, text=text, media_ids=[media_id])
    return receipt.url


def choose_watch(
    fixtures: list[Fixture],
    teams: Mapping[int, Mapping[str, Any]],
    ownership: Mapping[int, float],
    config: Mapping[str, Any],
    now: datetime,
    tz: ZoneInfo,
    official_sources: list[dict[str, str]],
) -> Candidate | None:
    lower = now + timedelta(hours=float(config["watch_fixture_min_hours"]))
    upper = now + timedelta(hours=float(config["watch_fixture_max_hours"]))
    candidates = [fixture for fixture in fixtures if not fixture.finished and lower <= fixture.kickoff <= upper]
    built: list[Candidate] = []
    for fixture in candidates:
        provider_row, provider_source = provider_fixture(fixture)
        if provider_row is None:
            continue
        candidate = build_watch(
            fixture,
            provider_row,
            teams,
            ownership,
            now,
            tz,
            [*official_sources, source_record("structured_fixture", provider_source)],
        )
        if candidate:
            built.append(candidate)
    return max(built, key=lambda item: item.priority_score, default=None)


def choose_review(
    fixtures: list[Fixture],
    teams: Mapping[int, Mapping[str, Any]],
    ownership: Mapping[int, float],
    config: Mapping[str, Any],
    now: datetime,
    tz: ZoneInfo,
    state: dict[str, Any],
    official_sources: list[dict[str, str]],
) -> Candidate | None:
    tracker = state.setdefault("full_time_seen", {})
    recent = [fixture for fixture in fixtures if fixture.finished and timedelta(0) <= now - fixture.kickoff <= timedelta(hours=10)]
    eligible: list[Fixture] = []
    min_age = timedelta(minutes=int(config["review_min_after_full_time_minutes"]))
    max_age = timedelta(minutes=int(config["review_max_after_full_time_minutes"]))
    for fixture in recent:
        key = str(fixture.fixture_id)
        first_seen = tracker.get(key)
        if not first_seen:
            tracker[key] = iso(now)
            continue
        seen_at = parse_dt(first_seen)
        age = now - seen_at
        if min_age <= age <= max_age:
            eligible.append(fixture)
    built: list[Candidate] = []
    for fixture in eligible:
        provider_row, provider_source = provider_fixture(fixture)
        if provider_row is None:
            continue
        candidate = build_review(
            fixture,
            provider_row,
            teams,
            ownership,
            now,
            tz,
            [*official_sources, source_record("structured_fixture", provider_source)],
        )
        if candidate:
            built.append(candidate)
    return max(built, key=lambda item: item.priority_score, default=None)


def run(mode: str, *, dry_run: bool = False) -> int:
    config = load_config()
    now = utcnow()
    tz = ZoneInfo(str(config["audience_timezone"]))
    state = load_json(STATE_PATH, {"posts": [], "full_time_seen": {}})
    status: dict[str, Any] = {"checked_at": iso(now), "mode": mode, "published": False, "reason": ""}

    bootstrap, fixtures_response, teams, fixtures, ownership = official_snapshot()
    official_sources = [source_record("official_bootstrap", bootstrap), source_record("official_fixtures", fixtures_response)]

    if posted_today(state, now, tz) >= int(config["max_posts_per_day"]):
        status["reason"] = "daily_limit_reached"
        save_json(STATE_PATH, state)
        save_json(RUN_STATUS_PATH, status)
        return 0

    desired_modes: list[str]
    if mode in {"watch", "review"}:
        desired_modes = [mode]
    else:
        desired_modes = []
        if local_time_due(now, config, "watch_local_time"):
            desired_modes.append("watch")
        desired_modes.append("review")

    candidate: Candidate | None = None
    for desired in desired_modes:
        if posted_today(state, now, tz, desired) >= int(config["max_posts_per_type_per_day"]):
            continue
        if desired == "watch":
            candidate = choose_watch(fixtures, teams, ownership, config, now, tz, official_sources)
        else:
            candidate = choose_review(fixtures, teams, ownership, config, now, tz, state, official_sources)
        if candidate:
            break

    if candidate is None:
        status["reason"] = "no_verified_tactical_candidate"
        save_json(STATE_PATH, state)
        save_json(RUN_STATUS_PATH, status)
        return 0

    threshold = int(config["minimum_publish_score"])
    if candidate.priority_score < threshold:
        status.update({"reason": "priority_below_publish_threshold", "priority_score": candidate.priority_score})
        save_json(STATE_PATH, state)
        save_json(RUN_STATUS_PATH, status)
        return 0

    key = post_key(candidate)
    if any(row.get("key") == key for row in state_rows(state)):
        status["reason"] = "duplicate_post"
        save_json(STATE_PATH, state)
        save_json(RUN_STATUS_PATH, status)
        return 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output = OUTPUT_DIR / f"{now.astimezone(tz):%Y%m%d-%H%M}-{candidate.mode}-{candidate.fixture.fixture_id}.png"
    TacticalGraphicRenderer().render(candidate.post, output)

    manual_review = bool(config.get("manual_review")) or os.getenv("TACTICAL_MANUAL_REVIEW", "").casefold() == "true"
    autopost = os.getenv("ENABLE_TACTICAL_AUTOPOST", "true").casefold() == "true"
    published = False
    delivery_url = ""
    if not dry_run and not manual_review and autopost:
        delivery_url = asyncio.run(publish_x(str(output), caption(candidate)))
        published = True

    row = {
        "key": key,
        "mode": candidate.mode,
        "fixture_id": candidate.fixture.fixture_id,
        "teams": [candidate.fixture.home, candidate.fixture.away],
        "priority_score": candidate.priority_score,
        "heading": candidate.post["heading"],
        "thesis": candidate.post["thesis"],
        "status": candidate.post["status"],
        "local_day": now.astimezone(tz).date().isoformat(),
        "created_at": iso(now),
        "published": published,
        "delivery_url": delivery_url,
        "image": str(output),
        "sources": candidate.source_records,
    }
    state.setdefault("posts", []).append(row)
    state["posts"] = state["posts"][-250:]
    status.update({
        "published": published,
        "reason": "published" if published else "draft_created",
        "priority_score": candidate.priority_score,
        "candidate": row,
    })
    save_json(STATE_PATH, state)
    save_json(RUN_STATUS_PATH, status)
    print(json.dumps(status, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["auto", "watch", "review"], default="auto")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    env_dry = os.getenv("TACTICAL_DRY_RUN", "").casefold() == "true"
    return run(args.mode, dry_run=args.dry_run or env_dry)


if __name__ == "__main__":
    raise SystemExit(main())
