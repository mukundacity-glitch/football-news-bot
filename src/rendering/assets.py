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
from PIL import Image, ImageDraw

from src.constants import CLUB_ALIASES, CLUB_COLORS, FPL_LOGO_IDS
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

    Identity is never guessed. The final team-shirt fallback is built only from
    the player's verified FPL club (or an explicitly verified club fact), so a
    missing portrait cannot silently assign the wrong face or club identity.
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

    # Wikipedia comes before secondary providers by design. The helper accepts
    # only an identity-matched footballer page, never a fuzzy image-search hit.
    image = _wikipedia_image(subject)
    if image:
        return image, "Wikipedia"

    # FotMob is the structured reliable-provider fallback. It is attempted only
    # when the verified story already carries an exact numeric provider ID.
    provider_id = facts.get("provider_player_id")
    if str(provider_id or "").isdigit():
        image = _download_image(
            f"https://images.fotmob.com/image_resources/playerimages/{provider_id}.png",
            _safe_name("fotmob_player", provider_id),
        )
        if image:
            return image, "Reliable provider"

    shirt = resolve_team_shirt(subject, facts, fpl_data=data)
    if shirt:
        return shirt, "Team shirt fallback"
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


def _verified_shirt_club(
    subject: str,
    facts: Mapping[str, Any],
    data: Optional[dict],
) -> tuple[str, object]:
    """Return a truth-anchored club name and optional structured provider ID."""
    if data:
        player = find_player_in_fpl(subject, data)
        teams = {team.get("id"): team for team in data.get("teams", [])}
        team = teams.get((player or {}).get("team")) or {}
        if team.get("name"):
            return str(team["name"]), team.get("code") or team.get("id")

    # A current/owning club is safer than a destination for an incomplete
    # transfer. Destination remains the last resort for completed/new signings.
    for name_key, id_key in (
        ("club_name", "provider_club_id"),
        ("club_from_name", "provider_from_club_id"),
        ("club_to_name", "provider_to_club_id"),
    ):
        value = str(facts.get(name_key) or "").strip()
        if value:
            return value, facts.get(id_key)
    return "", None


def _club_palette(club_name: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    key = CLUB_ALIASES.get(_norm(club_name), club_name)
    primary = CLUB_COLORS.get(key, (31, 93, 173))
    luminance = (0.2126 * primary[0]) + (0.7152 * primary[1]) + (0.0722 * primary[2])
    secondary = (16, 18, 28) if luminance > 165 else (246, 248, 252)
    return primary, secondary


def _paste_badge(base: Image.Image, badge: Image.Image, box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    asset = badge.convert("RGBA")
    scale = min((x2 - x1) / max(1, asset.width), (y2 - y1) / max(1, asset.height))
    asset = asset.resize(
        (max(1, round(asset.width * scale)), max(1, round(asset.height * scale))),
        Image.Resampling.LANCZOS,
    )
    x = x1 + (x2 - x1 - asset.width) // 2
    y = y1 + (y2 - y1 - asset.height) // 2
    base.paste(asset, (x, y), asset)


def resolve_team_shirt(
    subject: str,
    facts: Mapping[str, Any],
    *,
    fpl_data: Optional[dict] = None,
) -> Optional[Image.Image]:
    """Create a polished generic shirt for the player's verified team.

    This is deliberately a team-identity fallback, not a fabricated player
    portrait or a claim that the illustrated garment is the current official kit.
    """
    data = _fpl_data(fpl_data)
    club_name, provider_id = _verified_shirt_club(subject, facts, data)
    if not club_name:
        return None

    primary, secondary = _club_palette(club_name)
    shirt = Image.new("RGBA", (900, 1120), (0, 0, 0, 0))

    # Soft shadow and neon edge give the fallback the same broadcast presence as
    # a portrait while retaining a clearly generic T-shirt silhouette.
    shadow = Image.new("RGBA", shirt.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.ellipse((120, 900, 780, 1090), fill=(0, 0, 0, 105))
    shirt.alpha_composite(shadow)

    draw = ImageDraw.Draw(shirt)
    left_sleeve = [(265, 190), (82, 270), (20, 505), (205, 575), (300, 385)]
    right_sleeve = [(635, 190), (818, 270), (880, 505), (695, 575), (600, 385)]
    torso = [(265, 185), (365, 145), (535, 145), (635, 185), (705, 1000), (195, 1000)]
    for points in (left_sleeve, right_sleeve, torso):
        draw.polygon(points, fill=(*primary, 255), outline=(*secondary, 255))
        draw.line(points + [points[0]], fill=(*secondary, 255), width=13, joint="curve")

    # Collar, cuffs and subtle vertical panels use a contrast color derived from
    # the verified club palette; no unverified sponsor or exact kit pattern is used.
    draw.pieslice((355, 115, 545, 300), start=0, end=180, fill=(*secondary, 255))
    draw.pieslice((388, 142, 512, 260), start=0, end=180, fill=(*primary, 255))
    draw.line((45, 470, 214, 535), fill=(*secondary, 255), width=28)
    draw.line((855, 470, 686, 535), fill=(*secondary, 255), width=28)
    stripe = tuple(round(primary[i] * 0.62 + secondary[i] * 0.38) for i in range(3))
    draw.polygon([(255, 235), (330, 205), (370, 985), (285, 985)], fill=(*stripe, 120))
    draw.polygon([(645, 235), (570, 205), (530, 985), (615, 985)], fill=(*stripe, 120))

    badge = resolve_club_logo(club_name, provider_id=provider_id, fpl_data=data)
    if badge:
        _paste_badge(shirt, badge, (505, 305, 680, 500))
    return shirt
