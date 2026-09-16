"""Identity-safe FPL asset resolution for the broadcast renderer.

Player and club images are resolved from the official FPL registry and the
corresponding Premier League asset CDN identifiers exposed by that registry.
No Wikipedia, FotMob, or other third-party image source is used for graphics.
"""
from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Optional

import requests
from PIL import Image

from src.constants import CLUB_ALIASES, CLUB_COLORS
from src.fpl_feed import fetch_fpl_data, find_player_in_fpl

CACHE = Path(".cache/render_assets")


def _norm(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def _safe_name(prefix: str, key: object, suffix: str = ".png") -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:20]
    return CACHE / f"{prefix}_{digest}{suffix}"


def _download_image(url: str, cache_path: Path) -> Optional[Image.Image]:
    try:
        if cache_path.exists() and cache_path.stat().st_size > 1000:
            return Image.open(cache_path).convert("RGBA")
        response = requests.get(
            url,
            headers={"User-Agent": "FPLVortexRenderer/1.0"},
            timeout=15,
        )
        response.raise_for_status()
        image = Image.open(io.BytesIO(response.content)).convert("RGBA")
        if image.width < 100 or image.height < 100:
            return None
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(cache_path, "PNG")
        return image
    except Exception:
        return None


def _fpl_data(value: Optional[dict]) -> Optional[dict]:
    if isinstance(value, dict) and value.get("teams") and value.get("elements"):
        return value
    try:
        return fetch_fpl_data()
    except Exception:
        return None


def _team_from_fpl(club_name: str, data: Optional[dict]) -> Optional[dict]:
    if not data:
        return None
    wanted = _norm(club_name)
    canonical = CLUB_ALIASES.get(wanted)
    for team in data.get("teams", []):
        values = {_norm(team.get("name")), _norm(team.get("short_name"))}
        team_canonical = CLUB_ALIASES.get(_norm(team.get("name")))
        if wanted in values or (canonical and team_canonical == canonical):
            return team
    return None


def resolve_player_metadata(subject: str, *, fpl_data: Optional[dict] = None) -> dict[str, Any]:
    data = _fpl_data(fpl_data)
    if not data:
        return {}
    player = find_player_in_fpl(subject, data)
    if not player:
        return {}
    result: dict[str, Any] = {}
    element_types = {row.get("id"): row for row in data.get("element_types", [])}
    role = element_types.get(player.get("element_type")) or {}
    result["position"] = role.get("singular_name") or role.get("singular_name_short")
    birth = str(player.get("birth_date") or "").strip()
    if birth:
        try:
            from datetime import date
            born = date.fromisoformat(birth[:10])
            today = date.today()
            result["age"] = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        except Exception:
            pass
    teams = {team.get("id"): team for team in data.get("teams", [])}
    team = teams.get(player.get("team")) or {}
    result["club_name"] = team.get("name")
    result["fpl_player_id"] = player.get("id")
    result["fpl_player_code"] = player.get("code")
    result["fpl_team_id"] = player.get("team")
    result["fpl_team_code"] = team.get("code")
    return {key: value for key, value in result.items() if value not in (None, "")}


def resolve_player_image(
    subject: str,
    facts: Mapping[str, Any],
    *,
    fpl_data: Optional[dict] = None,
) -> tuple[Optional[Image.Image], str]:
    """Resolve the verified player's official FPL/Premier League headshot only.

    The FPL bootstrap payload is the identity authority. Its player ``code`` is
    used to address the official Premier League player asset. If the FPL player
    cannot be matched or the official image cannot be fetched, this fails closed
    instead of substituting an image from another provider.
    """
    data = _fpl_data(fpl_data)
    if not data:
        return None, ""
    player = find_player_in_fpl(subject, data)
    if not player:
        return None, ""
    code = player.get("code")
    if not str(code or "").isdigit():
        return None, ""
    image = _download_image(
        f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{int(code)}.png",
        _safe_name("fpl_player", int(code)),
    )
    if image:
        return image, "FPL API"
    return None, ""


def identity_safe_portrait(image: Image.Image, source: str) -> Image.Image:
    """Preserve the verified FPL asset without altering its source pixels."""
    return image.convert("RGBA")


def resolve_club_logo(
    club_name: str,
    *,
    provider_id: object = None,
    fpl_data: Optional[dict] = None,
) -> Optional[Image.Image]:
    """Resolve a club badge from the FPL team registry only.

    ``provider_id`` is intentionally ignored so a FotMob/team-provider ID can
    never become an image-source escape hatch.
    """
    data = _fpl_data(fpl_data)
    team = _team_from_fpl(club_name, data)
    if not team:
        return None
    badge_id = team.get("code")
    if not str(badge_id or "").isdigit():
        return None
    return _download_image(
        f"https://resources.premierleague.com/premierleague/badges/100/t{int(badge_id)}.png",
        _safe_name("fpl_badge", int(badge_id)),
    )


def _verified_shirt_club(
    subject: str,
    facts: Mapping[str, Any],
    data: Optional[dict],
) -> tuple[str, object]:
    """Return the current club anchored to the FPL player registry."""
    if data:
        player = find_player_in_fpl(subject, data)
        teams = {team.get("id"): team for team in data.get("teams", [])}
        team = teams.get((player or {}).get("team")) or {}
        if team.get("name"):
            return str(team["name"]), team.get("code") or team.get("id")
    return "", None


def _club_palette(club_name: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    key = CLUB_ALIASES.get(_norm(club_name), club_name)
    primary = CLUB_COLORS.get(key, (31, 93, 173))
    luminance = (0.2126 * primary[0]) + (0.7152 * primary[1]) + (0.0722 * primary[2])
    secondary = (16, 18, 28) if luminance > 165 else (246, 248, 252)
    return primary, secondary


def resolve_team_shirt(
    subject: str,
    facts: Mapping[str, Any],
    *,
    fpl_data: Optional[dict] = None,
) -> Optional[Image.Image]:
    """Create only a clearly generic, FPL-team-anchored fallback illustration.

    This is not used as a substitute for a missing player photo by
    ``resolve_player_image``. It remains available for callers that explicitly
    request a generic team-identity illustration.
    """
    data = _fpl_data(fpl_data)
    club_name, _provider_id = _verified_shirt_club(subject, facts, data)
    if not club_name:
        return None
    primary, secondary = _club_palette(club_name)
    shirt = Image.new("RGBA", (900, 1120), (0, 0, 0, 0))
    from PIL import ImageDraw
    draw = ImageDraw.Draw(shirt)
    left_sleeve = [(265, 190), (82, 270), (20, 505), (205, 575), (300, 385)]
    right_sleeve = [(635, 190), (818, 270), (880, 505), (695, 575), (600, 385)]
    torso = [(265, 185), (365, 145), (535, 145), (635, 185), (705, 1000), (195, 1000)]
    for points in (left_sleeve, right_sleeve, torso):
        draw.polygon(points, fill=(*primary, 255), outline=(*secondary, 255))
        draw.line(points + [points[0]], fill=(*secondary, 255), width=13, joint="curve")
    draw.pieslice((355, 115, 545, 300), start=0, end=180, fill=(*secondary, 255))
    draw.pieslice((388, 142, 512, 260), start=0, end=180, fill=(*primary, 255))
    draw.line((45, 470, 214, 535), fill=(*secondary, 255), width=28)
    draw.line((855, 470, 686, 535), fill=(*secondary, 255), width=28)
    return shirt
