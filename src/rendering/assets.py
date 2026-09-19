"""Identity-safe image and crest resolution for the broadcast renderer."""
from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Optional

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


def _club_terms(club_name: str) -> set[str]:
    """Return normalized aliases that identify one verified club."""
    wanted = _norm(club_name)
    if not wanted:
        return set()
    canonical = CLUB_ALIASES.get(wanted)
    terms = {wanted}
    if canonical:
        terms.add(_norm(str(canonical).replace("_", " ")))
        terms.update(
            _norm(alias)
            for alias, target in CLUB_ALIASES.items()
            if target == canonical
        )
    # Very short aliases create false positives in prose/image metadata.
    return {term for term in terms if len(term) >= 4}


def _mentions_club(text: object, club_name: str) -> bool:
    context = _norm(text)
    return bool(context) and any(term in context for term in _club_terms(club_name))


def _wikipedia_image(
    subject: str,
    expected_club: str = "",
) -> Optional[Image.Image]:
    """Return an identity-matched image, club-grounded when a club is known.

    A player page mentioning the current club is not enough: the selected
    Wikimedia image metadata must also mention that club.  This prevents an old
    academy/previous-club portrait from being shown under a current-club card.
    """
    try:
        search = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": f'"{subject}" footballer',
                "gsrlimit": 4,
                "prop": "pageimages|description|extracts",
                "piprop": "name|original",
                "exintro": 1,
                "explaintext": 1,
                "exsentences": 3,
                "format": "json",
                "origin": "*",
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
            extract = str(page.get("extract") or "")
            if target_norm not in _norm(title) and _norm(title) not in target_norm:
                continue
            if not any(
                word in description
                for word in (
                    "football", "soccer", "goalkeeper", "midfielder",
                    "defender", "forward", "winger",
                )
            ):
                continue

            if expected_club and not _mentions_club(
                " ".join((title, description, extract)), expected_club
            ):
                continue

            url = ((page.get("original") or {}).get("source"))
            if expected_club:
                pageimage = str(page.get("pageimage") or "").strip()
                if not pageimage:
                    continue
                media = requests.get(
                    "https://en.wikipedia.org/w/api.php",
                    params={
                        "action": "query",
                        "titles": f"File:{pageimage}",
                        "prop": "imageinfo",
                        "iiprop": "url|extmetadata",
                        "format": "json",
                        "origin": "*",
                    },
                    headers={"User-Agent": "FPLVortexRenderer/1.0"},
                    timeout=12,
                )
                media.raise_for_status()
                media_pages = (media.json().get("query") or {}).get("pages") or {}
                image_info = next(iter(media_pages.values()), {}).get("imageinfo") or []
                if not image_info:
                    continue
                info = image_info[0]
                metadata = info.get("extmetadata") or {}
                metadata_text = " ".join(
                    str((value or {}).get("value") or "")
                    for value in metadata.values()
                    if isinstance(value, dict)
                )
                if not _mentions_club(metadata_text, expected_club):
                    continue
                url = str(info.get("url") or url or "")

            if url:
                key = f"{subject}|{expected_club}" if expected_club else subject
                return _download_image(url, _safe_name("wiki", key))
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
            result["age"] = today.year - born.year - (
                (today.month, today.day) < (born.month, born.day)
            )
        except Exception:
            pass
    teams = {team.get("id"): team for team in data.get("teams", [])}
    team = teams.get(player.get("team")) or {}
    result["club_name"] = team.get("name")
    return {key: value for key, value in result.items() if value not in (None, "")}


def _thesportsdb_player_image(
    subject: str,
    expected_club: str = "",
) -> Optional[Image.Image]:
    """Return an identity-matched footballer image from TheSportsDB."""
    try:
        response = requests.get(
            "https://www.thesportsdb.com/api/v1/json/123/searchplayers.php",
            params={"p": subject.replace(" ", "_")},
            headers={"User-Agent": "FPLVortexRenderer/1.0"},
            timeout=12,
        )
        response.raise_for_status()
        players = response.json().get("player") or []
        target = _norm(subject)

        for player in players:
            name = str(player.get("strPlayer") or "")
            if not name or _norm(name) != target:
                continue
            sport = _norm(player.get("strSport"))
            if sport and sport not in {"soccer", "football"}:
                continue
            team = str(player.get("strTeam") or "")
            if expected_club and team and not _mentions_club(team, expected_club):
                continue

            for field in ("strCutout", "strThumb", "strFanart1"):
                url = str(player.get(field) or "").strip()
                if not url:
                    continue
                image = _download_image(
                    url,
                    _safe_name("thesportsdb_player", f"{target}|{field}|{url}"),
                )
                if image:
                    return image
    except Exception:
        return None
    return None


def _sportsapi_player_image(
    subject: str,
    facts: Mapping[str, Any],
) -> Optional[Image.Image]:
    """Use SportsAPI Pro only when the card already supplies its athlete ID/version."""
    athlete_id = facts.get("sportsapi_athlete_id")
    image_version = facts.get("sportsapi_image_version")
    if not str(athlete_id or "").isdigit() or not str(image_version or "").isdigit():
        return None

    try:
        url = (
            "https://v1.football.sportsapipro.com/images/athletes/"
            f"{int(athlete_id)}?imageVersion={int(image_version)}"
        )
        image = _download_image(
            url,
            _safe_name("sportsapi_player", f"{athlete_id}|{image_version}"),
        )
        return image
    except Exception:
        return None


def _fpl_player_image(
    subject: str,
    data: Optional[dict],
) -> Optional[Image.Image]:
    if not data:
        return None
    player = find_player_in_fpl(subject, data)
    if not player or not player.get("code"):
        return None
    try:
        code = int(player["code"])
    except (TypeError, ValueError):
        return None
    return _download_image(
        f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{code}.png",
        _safe_name("fpl_player", code),
    )


def resolve_player_image(
    subject: str,
    facts: Mapping[str, Any],
    *,
    fpl_data: Optional[dict] = None,
) -> tuple[Optional[Image.Image], str]:
    """Resolve only approved player-card assets in a fixed fallback order.

    Official FPL portrait comes first. FotMob is used only when the verified
    decision supplies its player ID. The final fallback is the official FPL
    team kit. The renderer uses contain-fitting, so each native image keeps
    its aspect ratio with no crop or stretch.
    """
    data = _fpl_data(fpl_data)

    image = _fpl_player_image(subject, data)
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

    shirt = resolve_team_shirt(subject, facts, fpl_data=data)
    if shirt:
        return shirt, "FPL team kit"
    return None, ""

def identity_safe_portrait(image: Image.Image, source: str) -> Image.Image:
    """Preserve the verified source image without cropping or reshaping it."""
    return image.convert("RGBA")


def _team_from_fpl(club_name: str, data: Optional[dict]) -> Optional[dict]:
    if not data:
        return None
    wanted = _norm(club_name)
    canonical = CLUB_ALIASES.get(wanted)
    for team in data.get("teams", []):
        values = {
            _norm(team.get("name")),
            _norm(team.get("short_name")),
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
    luminance = (
        (0.2126 * primary[0])
        + (0.7152 * primary[1])
        + (0.0722 * primary[2])
    )
    secondary = (16, 18, 28) if luminance > 165 else (246, 248, 252)
    return primary, secondary


def _paste_badge(
    base: Image.Image,
    badge: Image.Image,
    box: tuple[int, int, int, int],
) -> None:
    x1, y1, x2, y2 = box
    asset = badge.convert("RGBA")
    scale = min(
        (x2 - x1) / max(1, asset.width),
        (y2 - y1) / max(1, asset.height),
    )
    asset = asset.resize(
        (
            max(1, round(asset.width * scale)),
            max(1, round(asset.height * scale)),
        ),
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
    """Fetch the official FPL kit for the resolved player's current team.

    Bootstrap data supplies the current team code and selects the goalkeeper
    variation where required. If FPL has no usable asset, no generic drawn kit
    is substituted.
    """
    data = _fpl_data(fpl_data)
    if not data:
        return None
    player = find_player_in_fpl(subject, data)
    if not player:
        return None
    teams = {team.get("id"): team for team in data.get("teams", [])}
    team = teams.get(player.get("team")) or {}
    team_code = team.get("code")
    if not str(team_code or "").isdigit():
        return None
    goalkeeper_suffix = "_1" if player.get("element_type") == 1 else ""
    kit_key = f"{team_code}{goalkeeper_suffix}"
    return _download_image(
        "https://fantasy.premierleague.com/dist/img/shirts/standard/"
        f"shirt_{kit_key}-220.png",
        _safe_name("fpl_team_kit", kit_key),
    )
