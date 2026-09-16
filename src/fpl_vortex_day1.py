"""Free tactical source backed by the user's FPL-VORTEX-AUTO Day 1 data layer.

The module deliberately does not copy Day 1's HTTP/retry implementation. At run
 time the workflow checks out FPL-VORTEX-AUTO and this adapter imports its
``vortex.official_fpl._get_json`` request layer. API-Football remains optional
extra enrichment; Official FPL can produce evidence-limited posts on its own.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Mapping

from src import tactical_intelligence as base
from src.tactical_quality import enhance_candidate

DAY1_PATH = Path(os.getenv("FPL_VORTEX_AUTO_PATH", ".fpl-vortex-auto"))
FPL_API_PREFIX = "https://fantasy.premierleague.com/api"


def available() -> bool:
    return (DAY1_PATH / "vortex" / "official_fpl.py").is_file()


def _day1_getter() -> Callable[[str], Any]:
    if not available():
        raise RuntimeError(f"FPL-VORTEX-AUTO Day 1 source is unavailable at {DAY1_PATH}")
    root = str(DAY1_PATH.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    from vortex.official_fpl import _get_json  # type: ignore

    return _get_json


def request_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    fallback: Callable[..., base.Retrieved] | None = None,
) -> base.Retrieved:
    """Route Official FPL requests through Day 1; preserve other callers."""
    if not str(url).startswith(FPL_API_PREFIX) or params:
        if fallback is None:
            raise RuntimeError(f"Day 1 adapter cannot service non-FPL URL: {url}")
        return fallback(url, headers=headers, params=params)

    data = _day1_getter()(url)
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base.Retrieved(
        data=data,
        url=url,
        checked_at=base.iso(base.utcnow()),
        digest=hashlib.sha256(payload).hexdigest(),
    )


def _num(row: Mapping[str, Any], key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _player_name(row: Mapping[str, Any]) -> str:
    full = " ".join(
        part
        for part in (
            str(row.get("first_name") or "").strip(),
            str(row.get("second_name") or "").strip(),
        )
        if part
    )
    return full or str(row.get("web_name") or "Player")


def _set_piece_roles(row: Mapping[str, Any]) -> list[str]:
    roles: list[str] = []
    for key, label in (
        ("penalties_order", "penalties"),
        ("direct_freekicks_order", "direct free kicks"),
        ("corners_and_indirect_freekicks_order", "corners/free kicks"),
    ):
        try:
            order = int(float(row.get(key)))
        except (TypeError, ValueError):
            continue
        if 1 <= order <= 2:
            roles.append(label)
    return roles


def _eligible_players(bootstrap: Mapping[str, Any], team_id: int, *, limit: int = 4) -> list[Mapping[str, Any]]:
    rows = []
    for row in bootstrap.get("elements", []) or []:
        try:
            if int(row.get("team") or 0) != int(team_id):
                continue
        except (TypeError, ValueError):
            continue
        if str(row.get("status") or "a") not in {"a", "d"}:
            continue
        chance = row.get("chance_of_playing_next_round")
        try:
            if chance not in (None, "") and float(chance) < 50:
                continue
        except (TypeError, ValueError):
            pass
        rows.append(row)

    def score(row: Mapping[str, Any]) -> float:
        set_piece = 7.0 if _set_piece_roles(row) else 0.0
        return (
            _num(row, "selected_by_percent") * 1.4
            + _num(row, "form") * 3.0
            + _num(row, "expected_goal_involvements_per_90") * 20.0
            + min(_num(row, "minutes") / 90.0, 30.0) * 0.2
            + set_piece
        )

    return sorted(rows, key=score, reverse=True)[:limit]


def _summary(player_id: int) -> base.Retrieved:
    url = f"{FPL_API_PREFIX}/element-summary/{int(player_id)}/"
    return request_json(url)


def _recent_history(summary: Mapping[str, Any], *, limit: int = 3) -> list[Mapping[str, Any]]:
    rows = list(summary.get("history", []) or [])
    rows.sort(key=lambda row: (int(row.get("round") or 0), str(row.get("kickoff_time") or "")))
    return rows[-limit:]


def _fixture_history(summary: Mapping[str, Any], fixture_id: int) -> Mapping[str, Any] | None:
    for row in reversed(list(summary.get("history", []) or [])):
        try:
            if int(row.get("fixture") or 0) == int(fixture_id):
                return row
        except (TypeError, ValueError):
            continue
    return None


def _xgi(row: Mapping[str, Any]) -> float:
    direct = _num(row, "expected_goal_involvements")
    if direct > 0:
        return direct
    return _num(row, "expected_goals") + _num(row, "expected_assists")


def _player_watch_profile(player: Mapping[str, Any]) -> tuple[float, dict[str, Any], base.Retrieved] | None:
    try:
        player_id = int(player.get("id") or 0)
    except (TypeError, ValueError):
        return None
    if not player_id:
        return None
    retrieved = _summary(player_id)
    recent = _recent_history(retrieved.data, limit=3)
    if not recent:
        return None
    minutes = [_num(row, "minutes") for row in recent]
    avg_minutes = mean(minutes)
    if avg_minutes < 30:
        return None
    xgi_total = sum(_xgi(row) for row in recent)
    threat = sum(_num(row, "threat") for row in recent)
    creativity = sum(_num(row, "creativity") for row in recent)
    roles = _set_piece_roles(player)
    profile = {
        "player_id": player_id,
        "name": _player_name(player),
        "avg_minutes": avg_minutes,
        "xgi_total": xgi_total,
        "threat": threat,
        "creativity": creativity,
        "set_piece_roles": roles,
    }
    strength = min(1.0, 0.30 + min(xgi_total / 2.0, 0.35) + min(avg_minutes / 270.0, 0.20) + (0.15 if roles else 0.0))
    return strength, profile, retrieved


def _build_watch_candidate(
    fixture: base.Fixture,
    teams: Mapping[int, Mapping[str, Any]],
    ownership: Mapping[int, float],
    bootstrap: Mapping[str, Any],
    now: datetime,
    tz,
    official_sources: list[dict[str, str]],
) -> base.Candidate | None:
    options: list[tuple[float, dict[str, Any], base.Retrieved, int, str]] = []
    for team_id, team_name in ((fixture.home_id, fixture.home), (fixture.away_id, fixture.away)):
        for player in _eligible_players(bootstrap, team_id):
            profile = _player_watch_profile(player)
            if profile:
                strength, details, retrieved = profile
                options.append((strength, details, retrieved, team_id, team_name))
    if not options:
        return None

    novelty, details, summary_source, _team_id, team_name = max(
        options,
        key=lambda row: (row[0], row[1]["xgi_total"], row[1]["avg_minutes"]),
    )
    player_name = str(details["name"])
    roles = list(details["set_piece_roles"])
    if roles:
        thesis = base.fit_thesis(f"Watch whether {player_name}'s set-piece role creates another attacking route")
        explanation = base.bounded_sentence(
            f"Recent minutes and expected involvement keep {player_name} central, while set-piece duty adds a repeatable source of chances.",
            24,
        )
        focus = "set_piece"
        diagram = "Set-piece role"
        third_value = ", ".join(roles[:2])
    else:
        thesis = base.fit_thesis(f"Watch whether {player_name} sustains this recent attacking involvement")
        explanation = base.bounded_sentence(
            f"Across recent matches, minutes and expected goal involvement show a repeatable FPL attacking pattern worth monitoring.",
            24,
        )
        focus = "player_role"
        diagram = "Attacking involvement"
        third_value = f"{details['threat'] + details['creativity']:.0f} threat + creativity"

    local_kickoff = fixture.kickoff.astimezone(tz)
    score = base.priority_score(fixture, teams, ownership, novelty, now, review=False)
    post = {
        "heading": "FPL TACTICAL WATCH",
        "topic_line": f"{fixture.home} vs {fixture.away} • {local_kickoff:%a %d %b, %H:%M}",
        "thesis": thesis,
        "explanation": explanation,
        "evidence": [
            base.evidence("Recent minutes", f"{details['avg_minutes']:.0f} average across last three"),
            base.evidence("Recent xGI", f"{details['xgi_total']:.2f} across last three matches"),
            base.evidence("Role signal", third_value),
        ],
        "diagram_focus": focus,
        "diagram_label": diagram,
        "score_label": f"Content priority {score}/100",
        "status": "ANALYSIS",
        "source_label": "Official FPL via FPL Vortex Day 1",
        "checked_local": now.astimezone(tz).strftime("%d %b %H:%M %Z"),
        "hero_player_id": int(details["player_id"]),
        "focus_team": team_name,
    }
    candidate = base.Candidate(
        "watch",
        fixture,
        post,
        score,
        [*official_sources, base.source_record("official_fpl_player_summary", summary_source)],
    )
    provider_stub = {
        "teams": {
            "home": {"name": fixture.home, "logo": ""},
            "away": {"name": fixture.away, "logo": ""},
        }
    }
    return enhance_candidate(candidate, bootstrap, provider_stub, provider_get=None)


def choose_watch(
    fixtures: list[base.Fixture],
    teams: Mapping[int, Mapping[str, Any]],
    ownership: Mapping[int, float],
    config: Mapping[str, Any],
    now: datetime,
    tz,
    official_sources: list[dict[str, str]],
    *,
    bootstrap: Mapping[str, Any],
) -> base.Candidate | None:
    lower = now + timedelta(hours=float(config["watch_fixture_min_hours"]))
    upper = now + timedelta(hours=float(config["watch_fixture_max_hours"]))
    built = []
    for fixture in fixtures:
        if fixture.finished or not (lower <= fixture.kickoff <= upper):
            continue
        candidate = _build_watch_candidate(
            fixture, teams, ownership, bootstrap, now, tz, official_sources
        )
        if candidate:
            built.append(candidate)
    return max(built, key=lambda item: item.priority_score, default=None)


def _build_review_candidate(
    fixture: base.Fixture,
    teams: Mapping[int, Mapping[str, Any]],
    ownership: Mapping[int, float],
    bootstrap: Mapping[str, Any],
    now: datetime,
    tz,
    official_sources: list[dict[str, str]],
) -> base.Candidate | None:
    options: list[tuple[float, Mapping[str, Any], Mapping[str, Any], base.Retrieved, str]] = []
    for team_id, team_name in ((fixture.home_id, fixture.home), (fixture.away_id, fixture.away)):
        for player in _eligible_players(bootstrap, team_id, limit=6):
            try:
                player_id = int(player.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if not player_id:
                continue
            retrieved = _summary(player_id)
            match_row = _fixture_history(retrieved.data, fixture.fixture_id)
            if not match_row:
                continue
            minutes = _num(match_row, "minutes")
            if minutes < 30:
                continue
            xgi = _xgi(match_row)
            threat = _num(match_row, "threat")
            creativity = _num(match_row, "creativity")
            points = _num(match_row, "total_points")
            set_piece_bonus = 0.20 if _set_piece_roles(player) else 0.0
            strength = min(1.0, 0.35 + min(xgi / 1.2, 0.35) + min((threat + creativity) / 180.0, 0.20) + set_piece_bonus)
            options.append((strength, player, match_row, retrieved, team_name))
    if not options:
        return None

    novelty, player, match_row, summary_source, team_name = max(
        options,
        key=lambda row: (row[0], _xgi(row[2]), _num(row[2], "total_points")),
    )
    player_name = _player_name(player)
    roles = _set_piece_roles(player)
    match_xgi = _xgi(match_row)
    minutes = _num(match_row, "minutes")
    threat_creativity = _num(match_row, "threat") + _num(match_row, "creativity")
    if roles:
        thesis = base.fit_thesis(f"A major factor was {player_name}'s set-piece attacking involvement")
        explanation = base.bounded_sentence(
            f"Fixture-specific FPL data shows {player_name} combined set-piece responsibility with measurable attacking involvement across {minutes:.0f} minutes.",
            28,
        )
        focus = "set_piece"
        diagram = "Set-piece involvement"
        action = "Set-piece role stayed central"
    else:
        thesis = base.fit_thesis(f"The clearest FPL pattern was {player_name}'s attacking involvement")
        explanation = base.bounded_sentence(
            f"Fixture-specific FPL data shows expected goal involvement plus threat and creativity, supporting {player_name} as {team_name}'s main attacking signal.",
            28,
        )
        focus = "player_role"
        diagram = "Player involvement"
        action = "Attacking involvement stood out"

    score = base.priority_score(fixture, teams, ownership, novelty, now, review=True)
    score_text = (
        f"{fixture.home} {fixture.home_score}-{fixture.away_score} {fixture.away}"
        if fixture.home_score is not None and fixture.away_score is not None
        else f"{fixture.home} vs {fixture.away}"
    )
    post = {
        "heading": "TACTICAL REVIEW",
        "topic_line": score_text,
        "thesis": thesis,
        "explanation": explanation,
        "evidence": [
            base.evidence("Tactical action", action),
            base.evidence("Verified statistic", f"{match_xgi:.2f} xGI in {minutes:.0f} minutes"),
            base.evidence("Consequence", f"{threat_creativity:.0f} threat + creativity signal"),
        ],
        "diagram_focus": focus,
        "diagram_label": diagram,
        "score_label": f"Content priority {score}/100",
        "status": "ANALYSIS",
        "source_label": "Official FPL via FPL Vortex Day 1",
        "checked_local": now.astimezone(tz).strftime("%d %b %H:%M %Z"),
        "hero_player_id": int(player.get("id") or 0),
        "focus_team": team_name,
    }
    candidate = base.Candidate(
        "review",
        fixture,
        post,
        score,
        [*official_sources, base.source_record("official_fpl_player_summary", summary_source)],
    )
    provider_stub = {
        "teams": {
            "home": {"name": fixture.home, "logo": ""},
            "away": {"name": fixture.away, "logo": ""},
        }
    }
    return enhance_candidate(candidate, bootstrap, provider_stub, provider_get=None)


def choose_review(
    fixtures: list[base.Fixture],
    teams: Mapping[int, Mapping[str, Any]],
    ownership: Mapping[int, float],
    config: Mapping[str, Any],
    now: datetime,
    tz,
    state: dict[str, Any],
    official_sources: list[dict[str, str]],
    *,
    bootstrap: Mapping[str, Any],
) -> base.Candidate | None:
    tracker = state.setdefault("full_time_seen", {})
    recent = [
        fixture
        for fixture in fixtures
        if fixture.finished and timedelta(0) <= now - fixture.kickoff <= timedelta(hours=10)
    ]
    minimum = timedelta(minutes=int(config["review_min_after_full_time_minutes"]))
    maximum = timedelta(minutes=int(config["review_max_after_full_time_minutes"]))
    eligible: list[base.Fixture] = []
    for fixture in recent:
        key = str(fixture.fixture_id)
        first_seen = tracker.get(key)
        if not first_seen:
            tracker[key] = base.iso(now)
            continue
        seen_at = base.parse_dt(str(first_seen))
        age = now - seen_at
        if minimum <= age <= maximum:
            eligible.append(fixture)

    built = []
    for fixture in eligible:
        candidate = _build_review_candidate(
            fixture, teams, ownership, bootstrap, now, tz, official_sources
        )
        if candidate:
            built.append(candidate)
    return max(built, key=lambda item: item.priority_score, default=None)
