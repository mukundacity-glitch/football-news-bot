"""
FPL VORTEX — Broadcast Graphics Engine (pure PIL, no browser dependency).

Design goals (permanent fixes, not patches):
  * Fonts NEVER silently degrade to the tiny PIL bitmap font. A FontBook
    resolves each weight through: bundled assets/fonts -> cached download of
    Montserrat -> system DejaVu/Liberation -> scalable PIL default. Every
    fallback is a real vector font at the requested size.
  * Player images NEVER fail the card. An ordered provider chain resolves the
    photo (FPL official -> Premier League resources -> X profile URL if the
    story carries one -> RSS/article image -> Wikipedia -> local cache) and the
    final fallback is a branded club-crest panel — the card always renders.
  * Nothing is positioned by magic numbers alone: text is measured and
    auto-fitted (shrink / wrap / ellipsize) so long names and values can never
    overlap or bleed off the canvas.
  * Every fact shown comes from the verified story dict or the live FPL feed.
    Fields with no real data are simply not drawn — no fake placeholders.
"""

import re
import json
import hashlib
import unicodedata
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageChops

from src.constants import (
    CHANNEL_NAME, CHANNEL_HANDLE, CLUB_COLORS, FPL_LOGO_IDS,
    LOGOS_DIR, PLAYERS_DIR,
)
from src.fpl_feed import fetch_fpl_data, find_player_in_fpl, fpl_team_key, resolve_club_key

# ── CANVAS ───────────────────────────────────────────────────────────────
WIDTH, HEIGHT = 1920, 1080
MARGIN = 64

# ── PALETTE ──────────────────────────────────────────────────────────────
BG_TOP = (9, 14, 24)
BG_BOTTOM = (16, 24, 42)
BRAND_RED = (200, 18, 34)
PANEL_LINE = (46, 60, 84)
TEXT_DIM = (128, 146, 168)
TEXT_MID = (176, 190, 208)
WHITE = (255, 255, 255)

GREEN = (0, 168, 93)
BLUE = (0, 122, 214)
ORANGE = (238, 118, 0)
RED = (218, 37, 29)
GOLD = (255, 186, 0)

# ── THEMES ───────────────────────────────────────────────────────────────
# No emoji here on purpose: PIL fonts can't rasterise emoji, which is what
# produced the "▯" tofu boxes. Icons are drawn as vector shapes instead.
THEMES = {
    "official":       {"color": GREEN,  "label": "CONFIRMED TRANSFER", "pill": "OFFICIAL",   "icon": "check"},
    "news":           {"color": BLUE,   "label": "TRANSFER NEWS",      "pill": "LATEST",     "icon": "doc"},
    "rumour":         {"color": ORANGE, "label": "STRONG RUMOUR",      "pill": "DEVELOPING", "icon": "flame"},
    "collapsed":      {"color": RED,    "label": "DEAL COLLAPSED",     "pill": "MOVE OFF",   "icon": "cross"},
    "injury":         {"color": GOLD,   "label": "INJURY UPDATE",      "pill": "OUT",        "icon": "plus"},
    "suspension":     {"color": RED,    "label": "SUSPENSION",         "pill": "SUSPENDED",  "icon": "redcard"},
    "contract":       {"color": GREEN,  "label": "CONFIRMED CONTRACT", "pill": "SIGNED",     "icon": "pen"},
    "contract_talks": {"color": ORANGE, "label": "CONTRACT TALKS",     "pill": "DEVELOPING", "icon": "pen"},
    "manager":        {"color": BLUE,   "label": "MANAGER NEWS",       "pill": "LATEST",     "icon": "doc"},
}


# ═════════════════════════════════════════════════════════════════════════
# FONTS — layered resolution, never the tiny bitmap default
# ═════════════════════════════════════════════════════════════════════════
_FONT_SEARCH_DIRS = (Path("assets/fonts"), Path("data/fonts"))
_FONT_DOWNLOAD_DIR = Path("data/fonts")
_FONT_URL = ("https://raw.githubusercontent.com/JulietaUla/Montserrat/"
             "master/fonts/ttf/Montserrat-{weight}.ttf")
_SYSTEM_FONTS = {
    "heavy": ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
              "C:/Windows/Fonts/arialbd.ttf"),
    "regular": ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                "C:/Windows/Fonts/arial.ttf"),
}
_WEIGHT_CLASS = {"Black": "heavy", "ExtraBold": "heavy", "Bold": "heavy",
                 "SemiBold": "heavy", "Medium": "regular", "Regular": "regular"}


class FontBook:
    """Resolves (weight, size) -> a real scalable font, caching aggressively."""

    def __init__(self):
        self._paths = {}   # weight -> resolved ttf path or ""
        self._cache = {}   # (weight, size) -> ImageFont

    def _resolve_path(self, weight: str) -> str:
        if weight in self._paths:
            return self._paths[weight]
        fname = f"Montserrat-{weight}.ttf"
        # 1) bundled or previously downloaded copy
        for d in _FONT_SEARCH_DIRS:
            p = d / fname
            if p.exists() and p.stat().st_size > 10_000:
                self._paths[weight] = str(p)
                return str(p)
        # 2) one-time download into the persistent data/ cache
        dest = _FONT_DOWNLOAD_DIR / fname
        if _download(_FONT_URL.format(weight=weight), dest, min_size=10_000):
            self._paths[weight] = str(dest)
            return str(dest)
        # 3) system fonts
        for p in _SYSTEM_FONTS[_WEIGHT_CLASS.get(weight, "heavy")]:
            if Path(p).exists():
                self._paths[weight] = p
                return p
        self._paths[weight] = ""
        return ""

    def get(self, size: int, weight: str = "Bold"):
        size = max(8, int(size))
        key = (weight, size)
        if key in self._cache:
            return self._cache[key]
        path = self._resolve_path(weight)
        font = None
        if path:
            try:
                font = ImageFont.truetype(path, size)
            except Exception:
                font = None
        if font is None:
            # Pillow >= 10.1 ships a scalable default; older versions get the
            # bitmap font only as the absolute last resort.
            try:
                font = ImageFont.load_default(size=size)
            except TypeError:
                font = ImageFont.load_default()
        self._cache[key] = font
        return font

    def fit(self, draw, text, max_width, max_size, min_size=14, weight="Bold"):
        """Largest font of `weight` that renders `text` within max_width."""
        size = int(max_size)
        while size > min_size:
            f = self.get(size, weight)
            if draw.textlength(text, font=f) <= max_width:
                return f
            size -= 2
        return self.get(min_size, weight)


FONTS = FontBook()


# ═════════════════════════════════════════════════════════════════════════
# GENERIC ASSET HELPERS
# ═════════════════════════════════════════════════════════════════════════
def _download(url, dest: Path, timeout=15, min_size=200) -> bool:
    """Atomic download; returns True only for a plausible, non-empty file."""
    if not url:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        if not data or len(data) < min_size:
            return False
        tmp.write_bytes(data)
        tmp.replace(dest)
        return True
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False


def _open_rgba(path):
    try:
        im = Image.open(path)
        im.load()
        return im.convert("RGBA")
    except Exception:
        return None


def _contain(im, w, h):
    return ImageOps.contain(im, (int(w), int(h)), Image.Resampling.LANCZOS)


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "player"


# ═════════════════════════════════════════════════════════════════════════
# CLUB CRESTS — resolved from the LIVE FPL feed, cached on disk
# ═════════════════════════════════════════════════════════════════════════
def _badge_code(club_key, fpl_data) -> str | None:
    """Badge code for the PL CDN. Resolved dynamically from the live FPL
    bootstrap (teams[].code) so a new season's promoted clubs work without
    code changes; the FPL_LOGO_IDS constant is only a network-down fallback."""
    if not club_key:
        return None
    if fpl_data:
        for t in fpl_data.get("teams", []):
            raw = f"{t.get('name', '')} {t.get('short_name', '')}"
            if resolve_club_key(raw) == club_key and t.get("code"):
                return str(t["code"])
    return FPL_LOGO_IDS.get(club_key)


def crest_image(club_key, fpl_data, box: int):
    """Club crest as RGBA fitted into `box`, or None (foreign club, offline)."""
    if not club_key:
        return None
    safe = str(club_key).replace(" ", "_").replace("'", "")
    cached = LOGOS_DIR / f"{safe}.png"
    if not cached.exists():
        code = _badge_code(club_key, fpl_data)
        if code:
            # @x2 is ~200px — noticeably sharper on a 1080p card.
            for url in (f"https://resources.premierleague.com/premierleague/badges/t{code}@x2.png",
                        f"https://resources.premierleague.com/premierleague/badges/t{code}.png"):
                if _download(url, cached):
                    break
    im = _open_rgba(cached) if cached.exists() else None
    return _contain(im, box, box) if im else None


# ═════════════════════════════════════════════════════════════════════════
# PLAYER PHOTO — ordered provider chain; the card can never lose its image
# slot because one source is down.  Chain (first hit wins):
#   1. official FPL photo         (by verified element code, live feed)
#   2. Premier League resources   (alternate size on the same CDN)
#   3. X profile image            (only if the story carries a profile URL)
#   4. RSS / article image        (story["media_url"] from the feed item)
#   5. Wikipedia page image       (REST summary, guarded to footballers)
#   6. local cache                (any photo a previous run saved)
# Every hit is copied to the canonical cache so future runs are offline-safe.
# ═════════════════════════════════════════════════════════════════════════
def _p_fpl(story, el, dest):
    code = el.get("code") if el else None
    if not code:
        return False
    return _download(
        f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{code}.png",
        dest, min_size=2_000)


def _p_pl_site(story, el, dest):
    code = el.get("code") if el else None
    if not code:
        return False
    return _download(
        f"https://resources.premierleague.com/premierleague/photos/players/110x140/p{code}.png",
        dest, min_size=1_000)


def _p_x_profile(story, el, dest):
    # Used when an upstream step attaches the player's X profile image URL.
    return _download(story.get("x_photo_url"), dest, min_size=2_000)


def _p_rss_media(story, el, dest):
    return _download(story.get("media_url"), dest, min_size=5_000)


def _p_wikipedia(story, el, dest):
    name = story.get("display_name") or story.get("player")
    if not name:
        return False
    try:
        title = urllib.parse.quote(str(name).strip().replace(" ", "_"))
        req = urllib.request.Request(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        # Never put a namesake's face on the card: the page must clearly be
        # about a footballer before its image is trusted.
        desc = (info.get("description") or "").lower()
        if "football" not in desc and "soccer" not in desc:
            return False
        src = (info.get("originalimage") or {}).get("source") or \
              (info.get("thumbnail") or {}).get("source")
        return _download(src, dest, min_size=5_000)
    except Exception:
        return False


_PHOTO_PROVIDERS = (
    ("fpl", _p_fpl),
    ("pl_site", _p_pl_site),
    ("x_profile", _p_x_profile),
    ("rss", _p_rss_media),
    ("wikipedia", _p_wikipedia),
)


def player_photo(story, el):
    """Resolve the player photo through the provider chain. Returns RGBA or None."""
    slug = _slug(story.get("display_name") or story.get("player"))
    canonical = PLAYERS_DIR / f"{slug}.png"

    # Local cache first when it already exists (offline-safe fast path)…
    im = _open_rgba(canonical) if canonical.exists() else None
    if im is not None:
        return im

    for name, provider in _PHOTO_PROVIDERS:
        tmp = PLAYERS_DIR / f"{slug}__{name}.img"
        try:
            if provider(story, el, tmp):
                im = _open_rgba(tmp)
                if im is not None:
                    # Persist as canonical cache (provider file may be jpg/webp).
                    try:
                        im.save(canonical, "PNG")
                    except Exception:
                        pass
                    print(f"  [IMG] player photo via {name}: {story.get('player')!r}")
                    return im
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
    print(f"  [IMG] no player photo found for {story.get('player')!r} — using branded fallback")
    return None


# ═════════════════════════════════════════════════════════════════════════
# VECTOR ICONS (drawn, not emoji)
# ═════════════════════════════════════════════════════════════════════════
def _draw_icon(draw, kind, cx, cy, r, color, on_light=False):
    """Small badge icon centred at (cx, cy) with radius r."""
    fg = color if on_light else WHITE
    lw = max(3, r // 4)
    if kind == "check":
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fg)
        pts = [(cx - r * 0.45, cy + r * 0.02), (cx - r * 0.12, cy + r * 0.38),
               (cx + r * 0.5, cy - r * 0.35)]
        draw.line(pts, fill=color, width=lw, joint="curve")
    elif kind == "cross":
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fg)
        d = r * 0.42
        draw.line([cx - d, cy - d, cx + d, cy + d], fill=color, width=lw)
        draw.line([cx - d, cy + d, cx + d, cy - d], fill=color, width=lw)
    elif kind == "flame":
        draw.polygon([(cx, cy - r), (cx + r * 0.75, cy + r * 0.2), (cx + r * 0.3, cy + r),
                      (cx - r * 0.3, cy + r), (cx - r * 0.75, cy + r * 0.2)], fill=fg)
        draw.ellipse([cx - r * 0.35, cy, cx + r * 0.35, cy + r * 0.7], fill=color)
    elif kind == "plus":
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fg)
        d, w2 = r * 0.55, max(2, r // 5)
        draw.rectangle([cx - w2, cy - d, cx + w2, cy + d], fill=color)
        draw.rectangle([cx - d, cy - w2, cx + d, cy + w2], fill=color)
    elif kind == "redcard":
        w2, h2 = r * 0.7, r
        draw.rounded_rectangle([cx - w2, cy - h2, cx + w2, cy + h2],
                               radius=max(2, r // 5), fill=fg,
                               outline=(255, 255, 255, 200), width=2)
    elif kind == "pen":
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fg)
        draw.line([cx - r * 0.4, cy + r * 0.4, cx + r * 0.35, cy - r * 0.35],
                  fill=color, width=lw)
        draw.polygon([(cx + r * 0.2, cy - r * 0.5), (cx + r * 0.5, cy - r * 0.2),
                      (cx + r * 0.55, cy - r * 0.55)], fill=color)
    elif kind == "doc":
        w2, h2 = r * 0.7, r * 0.9
        draw.rounded_rectangle([cx - w2, cy - h2, cx + w2, cy + h2],
                               radius=max(2, r // 6), fill=fg)
        for i in (-1, 0, 1):
            y = cy + i * h2 * 0.45
            draw.line([cx - w2 * 0.55, y, cx + w2 * 0.55, y], fill=color,
                      width=max(2, r // 7))


def _arrow_down(draw, cx, top, h, color):
    """Broadcast-style chunky down arrow (incoming player)."""
    w = h * 0.52
    shaft_w = w * 0.42
    head_h = h * 0.42
    draw.rectangle([cx - shaft_w / 2, top, cx + shaft_w / 2, top + h - head_h], fill=color)
    draw.polygon([(cx - w / 2, top + h - head_h), (cx + w / 2, top + h - head_h),
                  (cx, top + h)], fill=color)


# ═════════════════════════════════════════════════════════════════════════
# TEXT HELPERS — measured, fitted, never overlapping
# ═════════════════════════════════════════════════════════════════════════
def _shadow_text(draw, xy, text, font, fill, offset=3):
    x, y = xy
    draw.text((x + offset, y + offset), text, font=font, fill=(0, 0, 0, 160))
    draw.text((x, y), text, font=font, fill=fill)


def _ellipsize(draw, text, font, max_w):
    if draw.textlength(text, font=font) <= max_w:
        return text
    t = text
    while t and draw.textlength(t + "…", font=font) > max_w:
        t = t[:-1]
    return (t + "…") if t else text


def _name_lines(draw, name, max_w, max_size, min_size, weight="Black"):
    """Fit the player name: one line if possible, else two balanced lines,
    shrinking the font until everything fits. Returns (lines, font)."""
    name = (name or "PLAYER").upper().strip()
    f = FONTS.fit(draw, name, max_w, max_size, min_size=min_size, weight=weight)
    # Good enough on one line at a healthy size?
    if draw.textlength(name, font=f) <= max_w and f.size >= max_size * 0.62:
        return [name], f
    words = name.split()
    if len(words) >= 2:
        # Split point that balances the two lines best.
        best = min(range(1, len(words)),
                   key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))))
        l1, l2 = " ".join(words[:best]), " ".join(words[best:])
        size = int(max_size)
        while size > min_size:
            f2 = FONTS.get(size, weight)
            if max(draw.textlength(l1, font=f2),
                   draw.textlength(l2, font=f2)) <= max_w:
                return [l1, l2], f2
            size -= 2
        return [l1, l2], FONTS.get(min_size, weight)
    return [name], f


# ═════════════════════════════════════════════════════════════════════════
# THEME / FIELD RESOLUTION — accuracy first
# ═════════════════════════════════════════════════════════════════════════
def get_theme_mode(story) -> str:
    ev = story.get("event")
    if story.get("collapsed"):
        return "collapsed"
    if ev == "injury":
        return "injury"
    if ev == "suspension":
        return "suspension"
    if ev in ("renewal", "stay"):
        confirmed = (str(story.get("mode", "")).lower() == "confirmed"
                     or int(story.get("stage", 1) or 1) >= 4)
        return "contract" if confirmed else "contract_talks"
    if ev == "manager":
        return "manager"
    # transfer / loan family. "mode" comes from classify_post and is either
    # "confirmed" or "rumour" — matching it case-insensitively (the old code
    # looked for "OFFICIAL" in it, which never matched, so every confirmed
    # transfer was rendered as a rumour).
    mode = str(story.get("mode", "rumour")).lower()
    stage = int(story.get("stage", 1) or 1)
    if mode == "confirmed" or "official" in mode or "agreed" in mode:
        return "official" if stage >= 4 else "news"
    return "rumour"


def _pretty_club(story, key_field, name_field):
    v = story.get(name_field) or (story.get(key_field) or "").replace("_", " ")
    return str(v).strip().title() if v else None


def _availability(stage) -> str:
    return {4: "Fit Again", 3: "Ruled Out", 2: "Major Doubt",
            1: "Being Assessed"}.get(int(stage or 1), "Being Assessed")


def build_fields(story, theme_key):
    """(label, value, club_key_for_crest) rows. Rows with no real data are
    dropped — the card never prints placeholder noise."""
    rows = []

    def add(label, value, crest_key=None):
        if value and str(value).strip():
            rows.append((label, str(value).strip(), crest_key))

    from_club = _pretty_club(story, "from_key", "from_club")
    to_club = _pretty_club(story, "to_key", "to_club")
    club = from_club or to_club
    club_key = story.get("from_key") or story.get("to_key")

    if theme_key in ("official", "news", "rumour"):
        add("From", from_club, story.get("from_key"))
        label_to = "Interested Club" if theme_key == "rumour" else "To"
        add(label_to, to_club, story.get("to_key"))
        is_loan = story.get("event") in ("loan", "loan_option")
        fee = story.get("fee")
        if not fee:
            fee = "Free Transfer" if story.get("is_free") else "Undisclosed"
        add("Loan Deal" if is_loan else ("Fee Expected" if theme_key == "rumour" else "Fee"),
            "Season-Long Loan" if is_loan and not story.get("fee") else fee)
        add("Contract", story.get("contract"))
    elif theme_key in ("contract", "contract_talks"):
        add("Club", club, club_key)
        add("Contract", story.get("contract"))
        add("Status", "Official" if theme_key == "contract" else "In Talks")
    elif theme_key == "collapsed":
        add("Buying Club", to_club, story.get("to_key"))
        add("Selling Club", from_club, story.get("from_key"))
        add("Reason", story.get("reason") or story.get("diagnosis"))
        add("Status", "Deal Collapsed")
    elif theme_key == "injury":
        add("Club", club, club_key)
        add("Injury", story.get("diagnosis"))
        add("Expected Return", story.get("expected_return"))
        add("Availability", _availability(story.get("stage")))
    elif theme_key == "suspension":
        add("Club", club, club_key)
        add("Reason", story.get("diagnosis"))
        add("Matches", story.get("matches") or story.get("matches_slashed"))
        add("Returns", story.get("expected_return"))
    elif theme_key == "manager":
        add("Club", to_club or from_club, story.get("to_key") or story.get("from_key"))
        add("Role", (story.get("staff_role") or "Manager").title())
        action = story.get("staff_action")
        add("Status", {"appointment": "Appointed", "departure": "Departure"}.get(action, "Linked"))
    return rows


def build_stats(story, el):
    """Bottom stat boxes from VERIFIED data only (live FPL feed + parsed
    contract). Anything unknown is simply omitted — never dash-filled."""
    stats = []
    if el:
        bd = el.get("birth_date")
        if bd:
            try:
                born = datetime.fromisoformat(str(bd)).replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                age = now.year - born.year - ((now.month, now.day) < (born.month, born.day))
                if 14 < age < 50:
                    stats.append(("Age", str(age)))
            except Exception:
                pass
        pos = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}.get(el.get("element_type"))
        if pos:
            stats.append(("Position", pos))
        cost = el.get("now_cost")
        if cost:
            stats.append(("FPL Price", f"£{cost / 10:.1f}m"))
        pts = el.get("total_points")
        if pts is not None and int(pts) > 0:
            stats.append(("FPL Points", str(pts)))
        own = el.get("selected_by_percent")
        if own not in (None, "", "0.0"):
            stats.append(("Ownership", f"{own}%"))
    m = re.search(r"(20\d{2})", str(story.get("contract") or ""))
    if m:
        stats.append(("Contract Until", m.group(1)))
    return stats[:5]


# ═════════════════════════════════════════════════════════════════════════
# DRAW SECTIONS
# ═════════════════════════════════════════════════════════════════════════
def _draw_background(theme_color):
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_TOP)
    d = ImageDraw.Draw(img)
    # vertical gradient
    for y in range(HEIGHT):
        t = y / HEIGHT
        d.line([(0, y), (WIDTH, y)], fill=tuple(
            int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)))
    # theme aura on the right (behind the photo panel)
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    cx, cy, rmax = WIDTH - 420, HEIGHT // 2, 640
    for r in range(rmax, 0, -8):
        od.ellipse([cx - r, cy - r, cx + r, cy + r],
                   fill=theme_color + (int((1 - r / rmax) * 26),))
    # faint diagonal accent slashes
    od.polygon([(WIDTH * 0.52, 0), (WIDTH * 0.60, 0),
                (WIDTH * 0.30, HEIGHT), (WIDTH * 0.22, HEIGHT)],
               fill=(255, 255, 255, 6))
    od.polygon([(WIDTH * 0.66, 0), (WIDTH * 0.70, 0),
                (WIDTH * 0.44, HEIGHT), (WIDTH * 0.40, HEIGHT)],
               fill=(255, 255, 255, 4))
    # Flatten to RGB: ImageDraw's "RGBA" mode only alpha-BLENDS on RGB images
    # (on RGBA it overwrites pixels, which turns every translucent fill into
    # an opaque one).
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _load_brand_logo(box):
    im = _open_rgba(Path("Logo.png"))
    if im is None:
        return None
    im = ImageOps.fit(im, (box, box), Image.Resampling.LANCZOS)
    mask = Image.new("L", (box, box), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, box, box], fill=255)
    im.putalpha(mask)
    return im


def _draw_header(img, draw):
    """Brand bar: logo disc + FPL VORTEX wordmark on the red flare, and the
    competition tag on the right. Sizes are measured so nothing collides."""
    bar_h = 118
    draw.polygon([(0, 0), (WIDTH * 0.56, 0), (WIDTH * 0.53, bar_h), (0, bar_h)],
                 fill=BRAND_RED)
    draw.polygon([(WIDTH * 0.56, 0), (WIDTH * 0.585, 0), (WIDTH * 0.555, bar_h),
                  (WIDTH * 0.53, bar_h)], fill=(255, 255, 255, 36))

    x = MARGIN
    logo = _load_brand_logo(86)
    if logo is not None:
        img.paste(logo, (x, (bar_h - 86) // 2), logo)
        x += 86 + 26

    wf = FONTS.get(56, "Black")
    y = (bar_h - 60) // 2
    _shadow_text(draw, (x, y), "FPL", wf, WHITE, offset=2)
    x2 = x + draw.textlength("FPL ", font=wf)
    _shadow_text(draw, (x2, y), "VORTEX", wf, (255, 214, 70), offset=2)

    # competition tag, right-aligned
    tag_f = FONTS.get(30, "Bold")
    tag = "PREMIER LEAGUE"
    tw = draw.textlength(tag, font=tag_f)
    tx = WIDTH - MARGIN - tw
    draw.rounded_rectangle([tx - 24, 34, WIDTH - MARGIN + 24, 84],
                           radius=10, outline=(94, 234, 148), width=2)
    draw.text((tx, 42), tag, font=tag_f, fill=(94, 234, 148))
    return bar_h


def _draw_category_badge(draw, theme, y):
    """Angled category banner with a drawn icon; width follows the text."""
    f = FONTS.get(40, "ExtraBold")
    label = theme["label"]
    icon_r = 24
    pad_l, pad_r, h = 34, 46, 78
    text_w = draw.textlength(label, font=f)
    x0 = MARGIN
    x1 = x0 + pad_l + icon_r * 2 + 22 + text_w + pad_r
    draw.polygon([(x0, y), (x1, y), (x1 - 26, y + h), (x0, y + h)], fill=theme["color"])
    _draw_icon(draw, theme["icon"], x0 + pad_l + icon_r, y + h // 2, icon_r,
               theme["color"])
    draw.text((x0 + pad_l + icon_r * 2 + 22, y + (h - 48) // 2), label, font=f, fill=WHITE)
    return y + h


def _draw_fields(img, draw, rows, theme_color, fpl_data, y, y_limit, max_w):
    """Label-over-value rows with inline crests. Row spacing adapts to the
    available vertical space so rows can never run into the stats bar."""
    if not rows:
        return
    n = len(rows)
    row_h = min(118, max(84, (y_limit - y) // n))
    lab_f = FONTS.get(28, "SemiBold")
    for label, value, crest_key in rows:
        if y + row_h > y_limit + 8:
            break
        draw.text((MARGIN, y), label.upper(), font=lab_f, fill=theme_color)
        crest = crest_image(crest_key, fpl_data, 54) if crest_key else None
        text_w = max_w - (66 if crest else 0)
        val_f = FONTS.fit(draw, value.upper(), text_w, 46, min_size=26, weight="Bold")
        vy = y + 36
        _shadow_text(draw, (MARGIN, vy), value.upper(), val_f, WHITE, offset=2)
        if crest is not None:
            cx = MARGIN + draw.textlength(value.upper(), font=val_f) + 18
            img.paste(crest, (int(cx), vy - 4), crest)
        y += row_h


def _draw_confidence(draw, story, theme, y):
    """Segmented probability bar backed by the pipeline's real confidence
    score (never an invented percentage). Skipped when no score exists."""
    score = story.get("confidence_score") or story.get("probability")
    try:
        score = int(str(score).replace("%", ""))
    except (TypeError, ValueError):
        return y
    score = max(0, min(100, score))
    lab_f = FONTS.get(28, "SemiBold")
    draw.text((MARGIN, y), "CONFIDENCE", font=lab_f, fill=theme["color"])
    by = y + 40
    seg_w, seg_h, gap = 34, 30, 8
    for i in range(10):
        x = MARGIN + i * (seg_w + gap)
        on = (i * 10) < score
        draw.rectangle([x, by, x + seg_w, by + seg_h],
                       fill=theme["color"] if on else (42, 54, 74))
    pct_f = FONTS.get(34, "Bold")
    draw.text((MARGIN + 10 * (seg_w + gap) + 12, by - 2), f"{score}%",
              font=pct_f, fill=WHITE)
    return by + seg_h


def _draw_photo_panel(img, draw, story, el, theme, fpl_data):
    """Right-hand hero panel: player photo (provider chain) with the club
    crest and a theme accessory. Falls back to a large crest, then to the
    brand logo — the panel is never empty."""
    pw, ph = 500, 640
    px, py = WIDTH - MARGIN - pw - 60, 150
    radius = 26

    # Subtle glass background for the panel (blended onto the RGB base).
    draw.rounded_rectangle([px, py, px + pw, py + ph], radius=radius,
                           fill=(255, 255, 255, 12))

    # Panel content built as a transparent RGBA layer, composited by ITS OWN
    # alpha (clipped to the rounded shape) so glows stay translucent.
    panel = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    pd = ImageDraw.Draw(panel)
    club_key = story.get("to_key") or story.get("from_key")
    glow = CLUB_COLORS.get(club_key, theme["color"])
    for r in range(ph // 2, 0, -6):
        a = int((1 - r / (ph / 2)) * 44)
        pd.ellipse([pw / 2 - r, ph / 2 - r, pw / 2 + r, ph / 2 + r], fill=glow + (a,))

    photo = player_photo(story, el)
    if photo is not None:
        has_alpha = photo.mode == "RGBA" and photo.getextrema()[3][0] < 250
        if has_alpha:
            # official cut-out headshot: anchor to the bottom like a broadcast card
            fit = _contain(photo, pw - 40, ph - 60)
            panel.alpha_composite(fit, ((pw - fit.width) // 2, ph - fit.height - 10))
        else:
            fit = ImageOps.fit(photo.convert("RGB"), (pw, ph),
                               Image.Resampling.LANCZOS, centering=(0.5, 0.30))
            panel.paste(fit, (0, 0))
    else:
        big = crest_image(club_key, fpl_data, 300) or _load_brand_logo(280)
        if big is not None:
            panel.alpha_composite(big, ((pw - big.width) // 2, (ph - big.height) // 2))

    # clip content to the rounded shape, then blend via its own alpha
    shape = Image.new("L", (pw, ph), 0)
    ImageDraw.Draw(shape).rounded_rectangle([0, 0, pw, ph], radius=radius, fill=255)
    panel.putalpha(ImageChops.multiply(panel.getchannel("A"), shape))
    img.paste(panel, (px, py), panel)
    draw.rounded_rectangle([px, py, px + pw, py + ph], radius=radius,
                           outline=PANEL_LINE, width=3)

    # crest badge floating top-right of the panel
    crest = crest_image(club_key, fpl_data, 132)
    if crest is not None and photo is not None:
        img.paste(crest, (px + pw - 70, py - 20), crest)

    # theme accessory on the outer right edge
    acc_x = px + pw + 46
    if get_theme_mode(story) in ("official", "news", "rumour") and story.get("to_key"):
        _arrow_down(draw, acc_x + 30, py + 90, 150, theme["color"])
    elif story.get("event") == "suspension":
        card = Image.new("RGBA", (110, 170), (0, 0, 0, 0))
        ImageDraw.Draw(card).rounded_rectangle([0, 0, 110, 170], radius=14, fill=RED)
        card = card.rotate(12, expand=True, resample=Image.Resampling.BICUBIC)
        img.paste(card, (px + pw - 40, py + 60), card)
    elif story.get("event") == "injury":
        _draw_icon(draw, "plus", px + pw - 20, py + ph - 70, 52, GOLD)
    return px  # left edge — the text column must stay left of this


def _draw_stats_bar(draw, stats, y):
    if not stats:
        return
    gap = 14
    total_w = WIDTH - 2 * MARGIN
    box_w = (total_w - gap * (len(stats) - 1)) / len(stats)
    box_h = 108
    lab_f = FONTS.get(24, "SemiBold")
    for i, (label, value) in enumerate(stats):
        x = MARGIN + i * (box_w + gap)
        draw.rounded_rectangle([x, y, x + box_w, y + box_h], radius=10,
                               fill=(255, 255, 255, 10), outline=PANEL_LINE, width=2)
        draw.text((x + 20, y + 14), label.upper(), font=lab_f, fill=TEXT_DIM)
        val_f = FONTS.fit(draw, str(value).upper(), box_w - 40, 44, min_size=22,
                          weight="Bold")
        draw.text((x + 20, y + 48), str(value).upper(), font=val_f, fill=WHITE)


def _draw_status_pill(draw, theme, y):
    f = FONTS.get(36, "ExtraBold")
    text = theme["pill"]
    tw = draw.textlength(text, font=f)
    icon_r = 17
    pad = 30
    w = pad + icon_r * 2 + 18 + tw + pad
    x1 = WIDTH - MARGIN
    x0 = x1 - w
    h = 74
    draw.rounded_rectangle([x0, y, x1, y + h], radius=16, fill=theme["color"])
    _draw_icon(draw, theme["icon"], x0 + pad + icon_r, y + h // 2, icon_r, theme["color"])
    draw.text((x0 + pad + icon_r * 2 + 18, y + (h - 42) // 2), text, font=f, fill=WHITE)


def _draw_footer(draw, sources):
    y0 = HEIGHT - 66
    draw.rectangle([0, y0, WIDTH, HEIGHT], fill=(12, 17, 28))
    draw.line([(0, y0), (WIDTH, y0)], fill=PANEL_LINE, width=2)
    f = FONTS.get(26, "Medium")
    ty = y0 + 18
    src = ", ".join(str(s).replace("_", " ").upper()
                    for s in (sources or [])[:2]) or CHANNEL_NAME
    draw.text((MARGIN, ty), f"SOURCE: {src}", font=f, fill=TEXT_MID)
    stamp = datetime.now(timezone.utc).strftime("%d %b %Y | %H:%M UTC").upper()
    mid = f"UPDATED: {stamp}"
    draw.text(((WIDTH - draw.textlength(mid, font=f)) / 2, ty), mid, font=f, fill=TEXT_MID)
    handle_f = FONTS.get(28, "Bold")
    handle = CHANNEL_HANDLE
    hw = draw.textlength(handle, font=handle_f)
    draw.polygon([(WIDTH - MARGIN - hw - 34, ty + 8), (WIDTH - MARGIN - hw - 34, ty + 24),
                  (WIDTH - MARGIN - hw - 18, ty + 16)], fill=BRAND_RED)
    draw.text((WIDTH - MARGIN - hw, ty - 2), handle, font=handle_f, fill=(255, 120, 130))


# ═════════════════════════════════════════════════════════════════════════
# MAIN RENDER PIPELINE
# ═════════════════════════════════════════════════════════════════════════
def render_core_card(story, sources, output_path):
    for d in (LOGOS_DIR, PLAYERS_DIR, _FONT_DOWNLOAD_DIR):
        d.mkdir(parents=True, exist_ok=True)

    fpl_data = fetch_fpl_data()
    el = find_player_in_fpl(story.get("display_name") or story.get("player"), fpl_data)

    # If the story has no club at all but the FPL feed knows the player's club,
    # anchor the card to verified truth (matches verify_card_data's behaviour).
    if el and not (story.get("from_key") or story.get("to_key")):
        true_club = fpl_team_key(el, fpl_data)
        if true_club:
            story = dict(story)
            story["from_key"] = true_club

    theme_key = get_theme_mode(story)
    theme = THEMES[theme_key]

    img = _draw_background(theme["color"])
    draw = ImageDraw.Draw(img, "RGBA")

    _draw_header(img, draw)

    # Right panel first — it defines the text column's right boundary.
    panel_left = _draw_photo_panel(img, draw, story, el, theme, fpl_data)
    text_max_w = panel_left - MARGIN - 48

    y = _draw_category_badge(draw, theme, 150)

    # Player name — auto-fit, max two lines, guaranteed inside the column.
    name = story.get("display_name") or story.get("player") or "PLAYER"
    lines, name_f = _name_lines(draw, name, text_max_w, 104, 44)
    y += 26
    for ln in lines:
        _shadow_text(draw, (MARGIN, y), ln, name_f, WHITE, offset=4)
        y += int(name_f.size * 1.12)
    draw.rectangle([MARGIN, y + 8, MARGIN + 130, y + 16], fill=theme["color"])
    y += 44

    # Info rows + optional confidence bar, capped above the stats strip.
    stats = build_stats(story, el)
    stats_y = HEIGHT - 66 - 30 - 108 if stats else HEIGHT - 66 - 30
    rows = build_fields(story, theme_key)
    show_conf = theme_key in ("rumour", "contract_talks")
    fields_limit = stats_y - (110 if show_conf else 20)
    _draw_fields(img, draw, rows, theme["color"], fpl_data, y, fields_limit, text_max_w)
    if show_conf:
        _draw_confidence(draw, story, theme, fields_limit + 10)

    _draw_stats_bar(draw, stats, stats_y)
    _draw_status_pill(draw, theme, stats_y - 96)
    _draw_footer(draw, sources)

    img.convert("RGB").save(output_path, "PNG")
    print(f"  [IMG] card rendered [{theme_key}]: {output_path}")


# ── PIPELINE INTERFACE (unchanged signatures) ────────────────────────────
def create_transfer_image(item, sources, image_path, collapsed=False):
    item = dict(item)
    item["collapsed"] = bool(collapsed or item.get("collapsed"))
    render_core_card(item, sources, image_path)


def create_injury_image(item, sources, image_path):
    render_core_card(item, sources, image_path)


def _create_fallback_card(item, sources, image_path):
    """Last-resort card: same engine, but tolerant of missing fields. The
    engine already degrades gracefully, so this simply strips anything that
    could raise and renders the branded layout."""
    safe = {k: item.get(k) for k in (
        "player", "display_name", "event", "mode", "stage", "collapsed",
        "from_key", "to_key", "from_club", "to_club", "fee", "contract",
        "diagnosis", "expected_return", "media_url", "confidence_score")}
    safe["player"] = safe.get("player") or "FOOTBALL NEWS"
    try:
        render_core_card(safe, sources, image_path)
    except Exception as e:
        print(f"  [IMG] fallback render error ({e}) — emitting minimal branded card")
        img = Image.new("RGB", (WIDTH, HEIGHT), BG_TOP)
        d = ImageDraw.Draw(img, "RGBA")
        _draw_header(img, d)
        f = FONTS.get(84, "Black")
        _shadow_text(d, (MARGIN, 300), str(safe["player"]).upper()[:40], f, WHITE)
        _draw_footer(d, sources)
        img.save(image_path, "PNG")
