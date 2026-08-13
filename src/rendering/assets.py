"""Identity-safe image and crest resolution for the broadcast renderer."""
from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import quote

import requests
from PIL import Image

from src.constants import CLUB_ALIASES, FPL_LOGO_IDS
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


def _wikipedia_image(subject: str) -> Optional[Image.Image]:
    try:
        search = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query", "generator": "search", "gsrsearch": f'"{subject}" footballer',
                "gsrlimit": 4, "prop": "pageimages|description", "piprop": "original",
                "format": "json", "origin": "*",
            },
            headers={"User-Agent": "FPLVortexRenderer/1.0"},
            timeout=12,
        )
        search.raise_for_status()
        pages = (search.json().get("query") or {}).get("pages") or {}
        target_norm = _norm(subject)
        for page in pages.values():
            title = str(page.get("title") or "")
            description = str(page.get("description") or "").casefold()
            if target_norm not in _norm(title) and _norm(title) not in target_norm:
                continue
            if not any(word in description for word in ("football", "soccer", "goalkeeper", "midfielder", "defender", "forward")):
                continue
            url = ((page.get("original") or {}).get("source"))
            if url:
                return _download_image(url, _safe_name("wiki", subject))
    except Exception:
        return None
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
    return {key: value for key, value in result.items() if value not in (None, "")}


def resolve_player_image(
    subject: str,
    facts: Mapping[str, Any],
    *,
    fpl_data: Optional[dict] = None,
) -> tuple[Optional[Image.Image], str]:
    """Resolve a real player image in the user's required priority order.

    Available structured identifiers are required; no fuzzy image search and no
    generated substitute can silently assign the wrong face or club shirt.
    """
    data = _fpl_data(fpl_data)
    if data:
        player = find_player_in_fpl(subject, data)
        if player and player.get("code"):
            code = int(player["code"])
            image = _download_image(
                f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{code}.png",
                _safe_name("fpl_player", code),
            )
            if image:
                return image, "FPL API"

    provider_id = facts.get("provider_player_id")
    if str(provider_id or "").isdigit():
        image = _download_image(
            f"https://images.fotmob.com/image_resources/playerimages/{provider_id}.png",
            _safe_name("fotmob_player", provider_id),
        )
        if image:
            return image, "FotMob"

    image = _wikipedia_image(subject)
    if image:
        return image, "Wikipedia"
    return None, ""


def _team_from_fpl(club_name: str, data: Optional[dict]) -> Optional[dict]:
    if not data:
        return None
    wanted = _norm(club_name)
    canonical = CLUB_ALIASES.get(wanted)
    for team in data.get("teams", []):
        values = {
            _norm(team.get("name")), _norm(team.get("short_name")),
        }
        team_canonical = CLUB_ALIASES.get(_norm(team.get("name")))
        if wanted in values or (canonical and team_canonical == canonical):
            return team
    return None


def resolve_club_logo(
    club_name: str,
    *,
    provider_id: object = None,
    fpl_data: Optional[dict] = None,
) -> Optional[Image.Image]:
    data = _fpl_data(fpl_data)
    team = _team_from_fpl(club_name, data)
    if team:
        badge_id = team.get("code") or team.get("id")
        if badge_id:
            image = _download_image(
                f"https://resources.premierleague.com/premierleague/badges/100/t{badge_id}.png",
                _safe_name("fpl_badge", badge_id),
            )
            if image:
                return image

    key = CLUB_ALIASES.get(_norm(club_name))
    badge_id = FPL_LOGO_IDS.get(key or club_name)
    if badge_id:
        image = _download_image(
            f"https://resources.premierleague.com/premierleague/badges/100/t{badge_id}.png",
            _safe_name("pl_badge", badge_id),
        )
        if image:
            return image

    if str(provider_id or "").isdigit():
        return _download_image(
            f"https://images.fotmob.com/image_resources/logo/teamlogo/{provider_id}.png",
            _safe_name("fotmob_badge", provider_id),
        )
    return None
