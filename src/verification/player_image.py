"""Player image resolution for verified transfer cards.

Strict separation of concerns, per policy:
  - The FPL bootstrap-static API supplies player metadata (name tokens,
    current club, position, nationality where available) AND a player photo.
    It is NEVER treated as a transfer-confirmation source anywhere in this
    module.
  - Wikipedia may supply a fallback portrait image/biography only. It is
    NEVER treated as evidence of transfer status, club, or fee.
  - If neither photo source produces a valid image, a clean placeholder is
    generated locally (no network, no AI-generated player likeness) using the
    player's name and the destination club's badge/colour.

Every candidate image is validated before use: HTTP success, an image
content-type, and usable (non-trivial, non-degenerate) pixel dimensions. The
image is never stretched to a different aspect ratio.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Mapping, Optional

from PIL import Image, ImageDraw, ImageFont

_UA = {"User-Agent": "Mozilla/5.0 (compatible; FPLVortexBot/2.0; +official-transfer-cards)"}
_MIN_USABLE_DIMENSION = 160  # px; below this a "photo" is treated as a broken thumbnail


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", str(text or ""))
        if not unicodedata.combining(c)
    )


def normalize_name_tokens(name: str) -> set[str]:
    cleaned = re.sub(r"\([^)]*\)", " ", _strip_accents(name).lower())
    return {t for t in re.findall(r"[a-z]{2,}", cleaned)}


@dataclass(frozen=True)
class PlayerMatch:
    element: Mapping[str, Any]
    confidence: float
    method: str


def match_fpl_player(
    *,
    full_name: str,
    first_name: Optional[str],
    last_name: Optional[str],
    club_name: Optional[str],
    position: Optional[str],
    nationality: Optional[str],
    fpl_data: Optional[Mapping[str, Any]],
) -> Optional[PlayerMatch]:
    """High-confidence FPL player match.

    Surname-only matching is explicitly disallowed. A match is accepted only
    when normalized full-name tokens match exactly, OR first+last tokens both
    match AND (club or position) corroborates, OR the FPL web_name matches
    together with at least one further corroborating signal (club/position/
    nationality). This deliberately requires more than a bare surname hit.
    """
    if not fpl_data or not isinstance(fpl_data, dict):
        return None
    elements = fpl_data.get("elements") or []
    teams = {t.get("id"): t for t in fpl_data.get("teams", [])}

    wanted_full = normalize_name_tokens(full_name)
    wanted_first = normalize_name_tokens(first_name or "")
    wanted_last = normalize_name_tokens(last_name or "")
    wanted_club = normalize_name_tokens(club_name or "")

    best: Optional[PlayerMatch] = None
    for el in elements:
        el_first = normalize_name_tokens(el.get("first_name", ""))
        el_last = normalize_name_tokens(el.get("second_name", ""))
        el_full = el_first | el_last
        el_web = normalize_name_tokens(el.get("web_name", ""))
        team = teams.get(el.get("team"))
        el_club = normalize_name_tokens(
            (team or {}).get("name", "") + " " + (team or {}).get("short_name", "")
        )

        full_match = bool(wanted_full) and wanted_full == el_full
        first_last_match = (
            bool(wanted_first) and bool(wanted_last)
            and wanted_first <= el_first and wanted_last <= el_last
        )
        club_corroborates = bool(wanted_club) and bool(wanted_club & el_club)

        if full_match:
            best = PlayerMatch(el, 0.99, "normalized_full_name")
            break
        if first_last_match and (club_corroborates or not club_name):
            candidate = PlayerMatch(el, 0.95, "first_and_last_name")
            best = best or candidate
        elif first_last_match:
            # First+last matched but club actively contradicts -- do not
            # accept a surname-adjacent false positive silently; keep looking.
            continue
        elif wanted_last and wanted_last <= el_web and club_corroborates:
            best = best or PlayerMatch(el, 0.9, "web_name_plus_club")

    return best


def _fetch_bytes(url: str, *, timeout: float = 10.0) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            content_type = resp.headers.get("Content-Type", "")
            if not content_type.lower().startswith("image/"):
                return None
            data = resp.read()
            if not data:
                return None
            return data
    except Exception:
        return None


def _validate_image_bytes(data: bytes) -> Optional[Image.Image]:
    """Confirms the bytes decode to a real, sufficiently large image."""
    try:
        image = Image.open(BytesIO(data))
        image.load()
    except Exception:
        return None
    width, height = image.size
    if width < _MIN_USABLE_DIMENSION or height < _MIN_USABLE_DIMENSION:
        return None
    return image.convert("RGBA")


def fpl_player_photo(element: Mapping[str, Any]) -> Optional[Image.Image]:
    code = element.get("code")
    if not code:
        return None
    url = f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{code}.png"
    data = _fetch_bytes(url)
    if not data:
        return None
    return _validate_image_bytes(data)


_WIKI_FOOTBALL_TERMS = (
    "footballer", "football player", "soccer player", "association football",
    "football club", "premier league", "midfielder", "defender", "goalkeeper",
    "forward", "striker", "winger",
)


def _wiki_page_is_player(summary: Mapping[str, Any], player_name: str) -> bool:
    title = str(summary.get("title") or "")
    blurb = " ".join(str(summary.get(k) or "") for k in ("description", "extract")).lower()
    if not any(term in blurb for term in _WIKI_FOOTBALL_TERMS):
        return False
    wanted = normalize_name_tokens(player_name)
    got = normalize_name_tokens(title)
    if not wanted or not got:
        return False
    surname = sorted(wanted, key=len)[-1]
    return surname in got


def wikipedia_player_photo(player_name: str) -> Optional[Image.Image]:
    """Biography/portrait fallback ONLY -- never used to validate a transfer."""
    if not player_name:
        return None
    candidates = [player_name]
    try:
        search_url = (
            "https://en.wikipedia.org/w/api.php?action=query&list=search"
            "&format=json&srlimit=5&srsearch="
            + urllib.parse.quote(f"{player_name} footballer")
        )
        data = _fetch_json(search_url)
        if data:
            hits = data.get("query", {}).get("search", [])
            candidates += [h.get("title") for h in hits if h.get("title")]
    except Exception:
        pass

    for title in dict.fromkeys(candidates):
        if not title:
            continue
        try:
            summary_url = (
                "https://en.wikipedia.org/api/rest_v1/page/summary/"
                + urllib.parse.quote(str(title).replace(" ", "_"), safe="")
            )
            summary = _fetch_json(summary_url, accept_non_image=True)
        except Exception:
            continue
        if not summary or not _wiki_page_is_player(summary, player_name):
            continue
        original = (summary.get("originalimage") or {}).get("source") or ""
        thumb = (summary.get("thumbnail") or {}).get("source") or ""
        for image_url in (original, thumb):
            if not image_url:
                continue
            data = _fetch_bytes(image_url)
            if not data:
                continue
            image = _validate_image_bytes(data)
            if image:
                return image
    return None


def _fetch_json(url: str, *, accept_non_image: bool = False):
    import json as _json
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def _font(size: int, bold: bool = True) -> ImageFont.ImageFont:
    names = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def generate_placeholder(
    player_name: str,
    club_badge: Optional[Image.Image] = None,
    *,
    size: tuple[int, int] = (600, 750),
    bg_color: tuple[int, int, int] = (24, 33, 58),
) -> Image.Image:
    """Clean, high-resolution, non-AI placeholder portrait.

    Uses initials on a solid panel plus (when available) the destination
    club's badge -- never an AI-generated likeness of the player.
    """
    width, height = size
    image = Image.new("RGBA", (width, height), (*bg_color, 255))
    draw = ImageDraw.Draw(image)

    initials = "".join(w[0] for w in re.findall(r"[A-Za-z]+", player_name)[:2]).upper() or "?"
    font = _font(int(height * 0.32))
    bbox = draw.textbbox((0, 0), initials, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    ellipse_top, ellipse_bottom = height * 0.12, height * 0.68
    ellipse_center_y = (ellipse_top + ellipse_bottom) / 2
    draw.ellipse(
        (width * 0.15, ellipse_top, width * 0.85, ellipse_bottom),
        outline=(120, 140, 190, 255), width=6,
    )
    draw.text(
        ((width - text_w) / 2 - bbox[0], ellipse_center_y - text_h / 2 - bbox[1]),
        initials, font=font, fill=(230, 236, 250, 255),
    )

    if club_badge is not None:
        badge = club_badge.convert("RGBA")
        badge.thumbnail((int(width * 0.3), int(width * 0.3)), Image.Resampling.LANCZOS)
        bx = int((width - badge.width) / 2)
        by = int(height * 0.72)
        image.alpha_composite(badge, (bx, by))

    return image


def resolve_player_image(
    *,
    full_name: str,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    club_name: Optional[str] = None,
    position: Optional[str] = None,
    nationality: Optional[str] = None,
    fpl_data: Optional[Mapping[str, Any]] = None,
    club_badge: Optional[Image.Image] = None,
) -> tuple[Image.Image, str, Optional[PlayerMatch]]:
    """Resolve the best available player image following the mandated chain.

    Returns (image, source_label, match) where source_label is one of
    "fpl", "wikipedia", or "placeholder".
    """
    match = match_fpl_player(
        full_name=full_name, first_name=first_name, last_name=last_name,
        club_name=club_name, position=position, nationality=nationality,
        fpl_data=fpl_data,
    )
    if match:
        image = fpl_player_photo(match.element)
        if image is not None:
            return image, "fpl", match

    wiki_image = wikipedia_player_photo(full_name)
    if wiki_image is not None:
        return wiki_image, "wikipedia", match

    return generate_placeholder(full_name, club_badge), "placeholder", match
