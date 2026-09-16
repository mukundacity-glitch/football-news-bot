"""Enhanced tactical renderer with dynamic team logos and player/jersey visuals."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlparse

import requests
from PIL import Image, ImageDraw

from .assets import resolve_club_logo, resolve_team_shirt
from .layout import alpha_panel, fit_font, paste_contain, truncate
from .tactical import CYAN, GOLD, LIME, MUTED, WHITE, TacticalGraphicRenderer

_ALLOWED_ASSET_HOSTS = {
    "resources.premierleague.com",
    "media.api-sports.io",
    "media.api-football.com",
}
_CACHE = Path(".cache/tactical_visuals")


def _remote_asset(url: str, *, minimum: int = 100) -> Image.Image | None:
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.netloc.casefold() not in _ALLOWED_ASSET_HOSTS:
        return None
    _CACHE.mkdir(parents=True, exist_ok=True)
    target = _CACHE / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()[:24]}.png"
    try:
        if target.exists() and target.stat().st_size > 1000:
            image = Image.open(target).convert("RGBA")
        else:
            response = requests.get(
                url,
                headers={"User-Agent": "FPLVortexTactical/2.0"},
                timeout=20,
            )
            response.raise_for_status()
            image = Image.open(io.BytesIO(response.content)).convert("RGBA")
            image.save(target, "PNG")
        if image.width < minimum or image.height < minimum:
            return None
        return image
    except Exception:
        return None


class EnhancedTacticalGraphicRenderer(TacticalGraphicRenderer):
    """Reuse the locked layout and replace only its right-side visual stage."""

    def render(self, post: Mapping[str, object], output_path: str | Path) -> str:
        assets = post.get("assets")
        if not isinstance(assets, Mapping):
            raise ValueError("tactical visual assets are required")
        home = assets.get("home_logo")
        away = assets.get("away_logo")
        hero = assets.get("hero_player")
        if not all(isinstance(item, Mapping) for item in (home, away, hero)):
            raise ValueError("team identities and hero visual metadata are required")

        home_team = str(home.get("team") or "").strip()
        away_team = str(away.get("team") or "").strip()
        home_img = _remote_asset(str(home.get("url") or ""), minimum=80)
        away_img = _remote_asset(str(away.get("url") or ""), minimum=80)
        if home_img is None and home_team:
            home_img = resolve_club_logo(home_team)
        if away_img is None and away_team:
            away_img = resolve_club_logo(away_team)
        if home_img is None or away_img is None:
            raise ValueError("verified team logos are unavailable")

        hero_img = None
        if str(hero.get("kind") or "player") == "player":
            hero_img = _remote_asset(str(hero.get("url") or ""), minimum=120)
        if hero_img is None:
            club_name = str(hero.get("club_name") or home_team or "").strip()
            hero_img = resolve_team_shirt(
                str(hero.get("name") or club_name or "TEAM"),
                {"club_name": club_name},
            )
            if hero_img is None:
                raise ValueError("player image and verified team-jersey fallback are unavailable")
            self._hero_is_jersey = True
        else:
            self._hero_is_jersey = False

        self._loaded_assets = {"home": home_img, "away": away_img, "hero": hero_img}
        try:
            return super().render(post, output_path)
        finally:
            self._loaded_assets = {}
            self._hero_is_jersey = False

    def _footer(self, image: Image.Image, post: Mapping[str, object]) -> None:
        # Preserve the audited source in post/state, but use a compact display
        # label so the locked footer never ellipsizes the Day 1 source name.
        display_post = dict(post)
        source = str(display_post.get("source_label") or "")
        if source == "Official FPL via FPL Vortex Day 1":
            display_post["source_label"] = "Official FPL • Vortex D1"
        super()._footer(image, display_post)

    def _body(self, image: Image.Image, post: Mapping[str, object], evidence: Sequence[object]) -> None:
        # Keep the original left-side hierarchy/evidence cards untouched. Then
        # repaint only the right panel where the base renderer placed its pitch.
        super()._body(image, post, evidence)
        right = (2505, 420, 3720, 1870)
        alpha_panel(
            image,
            right,
            fill=(1, 3, 10, 255),
            outline=(123, 38, 238, 255),
            width=7,
            radius=38,
            glow=False,
        )
        self._visual_stage(image, right, post)

    def _visual_stage(self, image: Image.Image, box, post: Mapping[str, object]) -> None:
        draw = ImageDraw.Draw(image)
        x1, y1, x2, y2 = box
        assets = post["assets"]
        hero = assets["hero_player"]
        home_img = self._loaded_assets["home"]
        away_img = self._loaded_assets["away"]
        hero_img = self._loaded_assets["hero"]

        paste_contain(image, home_img, (x1 + 70, y1 + 55, x1 + 300, y1 + 285))
        paste_contain(image, away_img, (x2 - 300, y1 + 55, x2 - 70, y1 + 285))
        versus = fit_font(draw, "VS", 180, max_size=58, min_size=46, role="condensed")
        draw.text(((x1 + x2) // 2, y1 + 165), "VS", anchor="mm", font=versus, fill=GOLD)

        # Leave a clear lower buffer for the hero-name card and priority tier.
        hero_box = (x1 + 100, y1 + 350, x2 - 100, y2 - 220)
        paste_contain(image, hero_img, hero_box)

        focus = str(post.get("diagram_label") or "TACTICAL FOCUS").upper()
        focus_font = fit_font(draw, focus, x2 - x1 - 170, max_size=42, min_size=32, role="condensed")
        draw.text(((x1 + x2) // 2, y1 + 320), focus, anchor="mm", font=focus_font, fill=LIME)

        display_name = str(hero.get("name") or "TEAM")
        if getattr(self, "_hero_is_jersey", False):
            display_name = f"{str(hero.get('club_name') or display_name)} • TEAM JERSEY"
        name_font = fit_font(draw, display_name, x2 - x1 - 160, max_size=52, min_size=36, role="bold")
        draw.rounded_rectangle(
            (x1 + 55, y2 - 190, x2 - 55, y2 - 100),
            radius=24,
            fill=(3, 5, 12),
            outline=CYAN,
            width=4,
        )
        draw.text(
            ((x1 + x2) // 2, y2 - 145),
            truncate(draw, display_name, name_font, x2 - x1 - 180),
            anchor="mm",
            font=name_font,
            fill=WHITE,
        )

        tier = str(post.get("score_label") or "")
        if tier:
            tier_font = fit_font(draw, tier, x2 - x1 - 180, max_size=34, min_size=28, role="bold")
            draw.text(((x1 + x2) // 2, y2 - 62), tier, anchor="mm", font=tier_font, fill=MUTED)
