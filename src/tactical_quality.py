"""Editorial quality gates and dynamic visual-asset selection for tactical posts.

Nothing in this module hardcodes a club, player, fixture or story. Visuals are
selected from live fixture/provider responses and Official FPL evidence.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Callable, Mapping

PRIORITY_SCORE = 75
STRONG_SCORE = 60
TREND_SCORE = 45


def _normalize_name(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def score_tier(score: int) -> str:
    value = int(score)
    if value >= PRIORITY_SCORE:
        return "PRIORITY"
    if value >= STRONG_SCORE:
        return "STRONG"
    if value >= TREND_SCORE:
        return "TREND"
    return "REJECT"


def story_quality_gate(candidate: Any) -> tuple[bool, str]:
    """Reject weak/generic observations before priority scoring can publish them."""
    focus = str(candidate.post.get("diagram_focus") or "").casefold()
    evidence = list(candidate.post.get("evidence") or [])
    if len(evidence) < 2:
        return False, "insufficient_tactical_evidence"
    if candidate.mode == "review" and focus == "pressure":
        # A shots-on-target gap by itself describes an outcome, not necessarily
        # the tactical mechanism behind it. Keep this out until richer evidence
        # supports the conclusion.
        return False, "generic_single_stat_review"
    if focus not in {"set_piece", "possession", "pressure", "player_role"}:
        return False, "unsupported_tactical_pattern"
    if score_tier(candidate.priority_score) == "REJECT":
        return False, "priority_below_trend_threshold"
    return True, "verified_tactical_pattern"


def _focus_team(candidate: Any) -> tuple[int, str]:
    explicit = _normalize_name(candidate.post.get("focus_team"))
    if explicit:
        if explicit == _normalize_name(candidate.fixture.home):
            return candidate.fixture.home_id, candidate.fixture.home
        if explicit == _normalize_name(candidate.fixture.away):
            return candidate.fixture.away_id, candidate.fixture.away

    thesis = _normalize_name(candidate.post.get("thesis"))
    home_key = _normalize_name(candidate.fixture.home)
    away_key = _normalize_name(candidate.fixture.away)
    if home_key and home_key in thesis:
        return candidate.fixture.home_id, candidate.fixture.home
    if away_key and away_key in thesis:
        return candidate.fixture.away_id, candidate.fixture.away
    return candidate.fixture.home_id, candidate.fixture.home


def _fpl_player_score(row: Mapping[str, Any]) -> float:
    """Dynamic FPL usefulness score used only to pick a relevant hero visual."""
    if str(row.get("status") or "a") not in {"a", "d"}:
        return -1.0

    def number(key: str) -> float:
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    chance = row.get("chance_of_playing_next_round")
    if chance not in (None, ""):
        try:
            if float(chance) < 50:
                return -1.0
        except (TypeError, ValueError):
            pass
    return (
        number("selected_by_percent") * 1.6
        + number("form") * 3.0
        + number("total_points") * 0.16
        + number("expected_goal_involvements_per_90") * 16.0
        + min(number("minutes") / 90.0, 30.0) * 0.2
    )


def _official_fpl_hero(
    bootstrap: Mapping[str, Any],
    team_id: int,
    *,
    preferred_player_id: int | None = None,
) -> dict[str, Any] | None:
    players = [
        row
        for row in bootstrap.get("elements", [])
        if int(row.get("team") or 0) == int(team_id)
        and str(row.get("code") or "").isdigit()
    ]
    if not players:
        return None

    player = None
    if preferred_player_id:
        player = next(
            (
                row
                for row in players
                if int(row.get("id") or 0) == int(preferred_player_id)
            ),
            None,
        )
    if player is None:
        player = max(players, key=_fpl_player_score)
        if _fpl_player_score(player) < 0:
            return None

    name = " ".join(
        part
        for part in (
            str(player.get("first_name") or "").strip(),
            str(player.get("second_name") or "").strip(),
        )
        if part
    ) or str(player.get("web_name") or "PLAYER")
    code = int(player["code"])
    return {
        "kind": "player",
        "name": name,
        "url": f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{code}.png",
        "source": "Official Premier League/FPL",
        "source_id": str(code),
    }


def _review_provider_hero(
    provider_row: Mapping[str, Any],
    focus_name: str,
    provider_get: Callable[[str, Mapping[str, Any]], Any] | None,
) -> dict[str, Any] | None:
    if provider_get is None:
        return None
    fixture_id = int(((provider_row.get("fixture") or {}).get("id")) or 0)
    if not fixture_id:
        return None
    try:
        result = provider_get("fixtures/players", {"fixture": fixture_id})
    except Exception:
        return None
    wanted = _normalize_name(focus_name)
    best: tuple[float, dict[str, Any]] | None = None
    for team_row in result.data.get("response", []) or []:
        team = team_row.get("team") or {}
        if _normalize_name(team.get("name")) != wanted:
            continue
        for row in team_row.get("players", []) or []:
            player = row.get("player") or {}
            photo = str(player.get("photo") or "").strip()
            if not photo:
                continue
            stats = (row.get("statistics") or [{}])[0] or {}
            games = stats.get("games") or {}
            shots = stats.get("shots") or {}
            goals = stats.get("goals") or {}
            passes = stats.get("passes") or {}

            def n(value: Any) -> float:
                try:
                    return float(value or 0)
                except (TypeError, ValueError):
                    return 0.0

            minutes = n(games.get("minutes"))
            if minutes <= 0:
                continue
            rating = n(games.get("rating"))
            score = (
                minutes * 0.03
                + rating * 3.0
                + n(shots.get("on")) * 2.0
                + n(goals.get("total")) * 6.0
                + n(goals.get("assists")) * 5.0
                + n(passes.get("key")) * 1.5
            )
            payload = {
                "kind": "player",
                "name": str(player.get("name") or "PLAYER"),
                "url": photo,
                "source": "Licensed structured provider",
                "source_id": str(player.get("id") or ""),
            }
            if best is None or score > best[0]:
                best = (score, payload)
    return best[1] if best else None


def visual_assets(
    candidate: Any,
    bootstrap: Mapping[str, Any],
    provider_row: Mapping[str, Any],
    *,
    provider_get: Callable[[str, Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Return dynamic team identities and a player visual with jersey fallback.

    A missing portrait never kills the story. The renderer first tries the
    player specifically referenced by the analysis, then other approved player
    imagery, and finally the verified-team generic jersey fallback.
    """
    provider_teams = provider_row.get("teams") or {}
    home_provider = provider_teams.get("home") or {}
    away_provider = provider_teams.get("away") or {}
    focus_id, focus_name = _focus_team(candidate)

    try:
        preferred_player_id = int(candidate.post.get("hero_player_id") or 0) or None
    except (TypeError, ValueError):
        preferred_player_id = None

    hero = None
    if preferred_player_id:
        hero = _official_fpl_hero(
            bootstrap,
            focus_id,
            preferred_player_id=preferred_player_id,
        )
    if hero is None and candidate.mode == "review":
        hero = _review_provider_hero(provider_row, focus_name, provider_get)
    if hero is None:
        hero = _official_fpl_hero(bootstrap, focus_id)
    if hero is None:
        hero = {
            "kind": "team_shirt",
            "name": focus_name,
            "url": "",
            "source": "Verified team jersey fallback",
            "club_name": focus_name,
        }
    else:
        # If the selected real image cannot be downloaded at render time, this
        # verified club name tells the renderer which jersey fallback to build.
        hero["club_name"] = focus_name

    return {
        "home_logo": {
            "team": candidate.fixture.home,
            "url": str(home_provider.get("logo") or "").strip(),
            "source": "Licensed structured provider" if home_provider.get("logo") else "Official FPL logo resolver",
        },
        "away_logo": {
            "team": candidate.fixture.away,
            "url": str(away_provider.get("logo") or "").strip(),
            "source": "Licensed structured provider" if away_provider.get("logo") else "Official FPL logo resolver",
        },
        "hero_player": hero,
    }


def enhance_candidate(
    candidate: Any,
    bootstrap: Mapping[str, Any],
    provider_row: Mapping[str, Any],
    *,
    provider_get: Callable[[str, Mapping[str, Any]], Any] | None = None,
) -> Any:
    if candidate is None:
        return None
    allowed, reason = story_quality_gate(candidate)
    if not allowed:
        return None

    post = dict(candidate.post)
    tier = score_tier(candidate.priority_score)
    post["priority_tier"] = tier
    post["quality_gate"] = reason
    post["assets"] = visual_assets(
        candidate,
        bootstrap,
        provider_row,
        provider_get=provider_get,
    )
    if tier == "TREND":
        post["heading"] = "TACTICAL TREND"
    post["score_label"] = f"{tier.title()} • {candidate.priority_score}/100"
    return replace(candidate, post=post)
