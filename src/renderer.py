"""
FPL VORTEX — Graphics Engine
Handles generation of cinematic transfer and injury cards via PIL and Playwright.
"""

import os
import re
import json
import base64
import hashlib
import urllib.request
import urllib.parse
import unicodedata
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps

# Connect to our Core Engines and Constants
from src.constants import CLUB_COLORS, FPL_LOGO_IDS, CHANNEL_HANDLE
from src.fpl_feed import fetch_fpl_data, find_player_in_fpl

FONT = ImageFont.load_default()
_FONT_CACHE = {}
_FALLBACK_FONTS = {
    "Black": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
    "Bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
}

def _load_fallback(size, weight):
    for path in _FALLBACK_FONTS.get(weight, _FALLBACK_FONTS["Bold"]):
        if os.path.exists(path):
            try: return ImageFont.truetype(path, size)
            except Exception: continue
    return ImageFont.load_default()

def get_premium_font(size, weight="Bold"):
    key = (weight, size)
    if key in _FONT_CACHE: return _FONT_CACHE[key]
    fp = f"Montserrat-{weight}.ttf"
    if not os.path.exists(fp):
        try:
            url = f"https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-{weight}.ttf"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r, open(fp, "wb") as out:
                out.write(r.read())
        except Exception:
            f = _load_fallback(size, weight)
            _FONT_CACHE[key] = f
            return f
    try: f = ImageFont.truetype(fp, size)
    except Exception: f = _load_fallback(size, weight)
    _FONT_CACHE[key] = f
    return f

def _download_asset(url, dest: Path) -> bool:
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200: return False
            data = resp.read()
            if not data: return False
        with open(tmp, "wb") as f: f.write(data)
        tmp.replace(dest)
        return True
    except Exception:
        try: tmp.exists() and tmp.unlink()
        except Exception: pass
        return False

def _safe_open_rgba(path: Path):
    try:
        im = Image.open(path)
        im.load()
        return im.convert("RGBA")
    except Exception:
        return None

def _fit_contain(im, w, h):
    return ImageOps.contain(im, (w, h), Image.Resampling.LANCZOS)

def _draw_text_shadow(draw, xy, text, font, fill, shadow=(0, 0, 0), offset=2):
    x, y = xy
    draw.text((x + offset, y + offset), text, font=font or FONT, fill=shadow)
    draw.text((x, y), text, font=font or FONT, fill=fill)

def _load_crest(club_key, box=120):
    if not club_key: return None
    safe = club_key.replace(" ", "_").replace("'", "")
    p = Path(f"logos/{safe}.png")
    if not p.exists() and FPL_LOGO_IDS.get(safe):
        _download_asset(f"https://resources.premierleague.com/premierleague/badges/t{FPL_LOGO_IDS[safe]}.png", p)
    if p.exists():
        src = _safe_open_rgba(p)
        if src is not None: return _fit_contain(src, box, box)
    return None

def _draw_wordmark(draw, xy):
    x, y = xy
    f = get_premium_font(46, "Black")
    _draw_text_shadow(draw, (x, y), "FPL", f, (255, 255, 255), offset=2)
    fpl_w = draw.textlength("FPL ", font=f)
    _draw_text_shadow(draw, (x + fpl_w, y), "VORTEX", f, (84, 224, 124), offset=2)

def get_club_color(club_key):
    color_tuple = CLUB_COLORS.get(club_key, (84, 224, 124)) # Default to VORTEX Green
    return f"rgb({color_tuple[0]}, {color_tuple[1]}, {color_tuple[2]})"


def _hex_to_rgb(hex_color: str) -> str:
    """'#FF453A' -> '255,69,58', for use inside an rgba(...) CSS value."""
    h = str(hex_color or "").lstrip("#")
    if len(h) != 6:
        return "255,255,255"
    try:
        return f"{int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)}"
    except ValueError:
        return "255,255,255"


def current_fpl_season(today=None) -> str:
    """Auto-derive the season badge text, e.g. '2026/27', from the date.

    The English football season runs August through May. June/July is the
    off-season gap — no new season has started yet, so those two months
    still read as the season that just finished in May, matching how the
    football calendar itself names a season (there is no month where "no
    season" is the correct label). No value is ever hardcoded; a card
    rendered next year reads correctly with zero code change.
    """
    from datetime import date
    d = today or date.today()
    if d.month >= 8:
        start_year = d.year
    elif d.month <= 5:
        start_year = d.year - 1
    else:  # June/July off-season -> still the season that ended in May
        start_year = d.year - 1
    return f"{start_year}/{str(start_year + 1)[-2:]}"

# Every card ships at 4K UHD, 16:9 — the master resolution. Templates keep
# their own CSS design size; the renderer scales the browser's device pixel
# ratio to reach this output, so a template laid out at 1380x776 produces a
# genuinely 3840x2160 image rather than an upscaled 1380x776 one. Text, crests
# and borders are re-rasterised at the higher density, not stretched.
CARD_OUTPUT_W, CARD_OUTPUT_H = 3840, 2160


def _render_html_sync(html_content, filename, error_box=None, width=1380, height=776,
                      scale=1.0):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": width, "height": height},
                                    device_scale_factor=scale)
            page.set_content(html_content, wait_until="domcontentloaded")
            page.wait_for_timeout(500)
            # The template reports text it could not fit rather than outlining it
            # in red on the image. Surface it here so a layout regression shows up
            # in the run log instead of on the published card.
            try:
                overflow = page.evaluate("window.__CARD_OVERFLOW__ || []")
                for item in overflow or []:
                    print(f"  [CARD] text overflow — {item}")
            except Exception:
                pass
            page.screenshot(path=filename)
            browser.close()
    except Exception:
        if error_box is not None:
            import traceback
            error_box.append(traceback.format_exc())


def _normalise_card_size(filename) -> None:
    """Force the rendered card to exactly 3840x2160.

    A template's design aspect can be a hair off 16:9 (1380x776 is 1.7784 vs
    1.7778), and device_scale_factor rounds to whole pixels. Both leave the
    screenshot a few pixels short of the target, which downstream consumers —
    X's media pipeline especially — treat as a non-standard size. The correction
    is sub-0.1% so it costs nothing visually.
    """
    try:
        with Image.open(filename) as im:
            if im.size == (CARD_OUTPUT_W, CARD_OUTPUT_H):
                return
            im.convert("RGB").resize((CARD_OUTPUT_W, CARD_OUTPUT_H), Image.LANCZOS).save(filename)
    except Exception:
        pass  # A card that renders slightly off-size still beats no card.


# ── SHARED ASSET / RENDER HELPERS ─────────────────────────────────────────
def _data_uri(path: Path, min_size: int = 500) -> str:
    """Return a base64 data-URI for an image file, or '' if missing/too small."""
    try:
        if path.exists() and path.stat().st_size >= min_size:
            ext = path.suffix.lower()
            mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
            return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    except Exception:
        pass
    return ""


_LOGO_PATH_CACHE = None


def _resolve_logo_path() -> Path:
    """Find the brand logo file regardless of its exact filename casing.

    The repo root file has been renamed between 'Logo.png' and 'logo.png'
    at least once already (a rename that silently dropped the logo off
    every rendered card, since _data_uri() degrades to '' on a missing
    path with no error/log — nothing flagged it happened). Rather than
    hardcode whichever casing happens to be current today, check the
    repo root directory listing directly and match case-insensitively,
    so a future rename doesn't reproduce the same silent failure. Cached
    after the first successful resolution per process, since the
    filesystem doesn't change mid-run.
    """
    global _LOGO_PATH_CACHE
    if _LOGO_PATH_CACHE is not None and _LOGO_PATH_CACHE.exists():
        return _LOGO_PATH_CACHE
    repo_root = Path(__file__).resolve().parent.parent
    for candidate in repo_root.glob("*"):
        if candidate.is_file() and candidate.stem.lower() == "logo" and candidate.suffix.lower() in (".png", ".jpg", ".jpeg"):
            _LOGO_PATH_CACHE = candidate
            return candidate
    # Nothing matched -- fall back to the most recently known filename so
    # the caller's own missing-file handling (_data_uri returning "") still
    # applies, but log clearly so this doesn't fail silently again.
    print("  [ASSET] could not find a logo.png/Logo.png file in the repo root; "
          "brand logo will be missing from rendered cards")
    return repo_root / "logo.png"


def _crest_uri(club_key) -> str:
    """Resolve (downloading if needed) a club crest to a data-URI."""
    if not club_key:
        return ""
    safe = club_key.replace(" ", "_").replace("'", "")
    cp = Path(f"logos/{safe}.png")
    if not cp.exists() and FPL_LOGO_IDS.get(safe):
        _download_asset(f"https://resources.premierleague.com/premierleague/badges/t{FPL_LOGO_IDS[safe]}.png", cp)
    return _data_uri(cp)


def _strip_accents(text: str) -> str:
    """'Milosavljević' -> 'Milosavljevic' — feeds name comparison only."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", str(text))
        if not unicodedata.combining(c)
    )


def _name_tokens(text: str):
    cleaned = re.sub(r"\([^)]*\)", " ", _strip_accents(text).lower())
    return {t for t in re.findall(r"[a-z]{2,}", cleaned)}


# Words that mean the article is about a footballer. A page can only supply a
# player photo if it is actually about a player.
_WIKI_FOOTBALL_TERMS = (
    "footballer", "football player", "soccer player", "association football",
    "football club", "premier league", "midfielder", "defender", "goalkeeper",
    "forward", "striker", "winger",
)


def _wiki_page_is_player(summary: dict, player_name: str) -> bool:
    """Is this article the footballer we are looking for?

    Two independent checks, because a wrong photo is far worse than no photo:
    the article has to describe a footballer, AND its title has to carry the
    player's surname. Without the second check a search for an obscure player
    happily returns a politician or a village and we would print their face
    on a verified news card.
    """
    title = str(summary.get("title") or "")
    blurb = " ".join(str(summary.get(k) or "")
                     for k in ("description", "extract")).lower()
    if not any(term in blurb for term in _WIKI_FOOTBALL_TERMS):
        return False
    wanted = _name_tokens(player_name)
    got = _name_tokens(title)
    if not wanted or not got:
        return False
    surname = sorted(wanted, key=len)[-1]
    return surname in got


def _wiki_summary(title: str):
    url = ("https://en.wikipedia.org/api/rest_v1/page/summary/"
           + urllib.parse.quote(str(title).replace(" ", "_"), safe=""))
    req = urllib.request.Request(
        url, headers={"User-Agent": "FPLVortexBot/1.0 (football-news-bot)"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wikipedia_player_image(player_name: str) -> str:
    """Best available Wikipedia lead image for this player, or "".

    The old version guessed the article title straight from the name, so any
    player written with diacritics — 'Veljko Milosavljević' — missed every
    time, which is precisely the kind of new signing that has no FPL headshot
    either. This resolves the real title through the search API first, then
    verifies the page before trusting its picture.
    """
    candidates = []
    try:
        search = ("https://en.wikipedia.org/w/api.php?action=query&list=search"
                  "&format=json&srlimit=5&srsearch="
                  + urllib.parse.quote(f"{player_name} footballer"))
        req = urllib.request.Request(
            search, headers={"User-Agent": "FPLVortexBot/1.0 (football-news-bot)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        candidates = [hit.get("title") for hit
                      in data.get("query", {}).get("search", []) if hit.get("title")]
    except Exception as exc:
        print(f"  [PHOTO] Wikipedia search failed for {player_name!r}: {exc}")

    # The plain name still goes first — for well-known players it is the
    # article title and saves a round trip.
    for title in [player_name] + [c for c in candidates if c != player_name]:
        try:
            summary = _wiki_summary(title)
        except Exception:
            continue
        if not _wiki_page_is_player(summary, player_name):
            continue
        # Prefer the full-resolution original: the summary thumbnail is only
        # ~320px, which is visibly soft in the card's portrait area.
        original = (summary.get("originalimage") or {}).get("source") or ""
        thumb = (summary.get("thumbnail") or {}).get("source") or ""
        if original or thumb:
            return original or thumb
    return ""


def _img_assets(story):
    """Resolve a player image only from identity-verified sources.

    Policy: FPL's canonical player asset is preferred. Wikipedia is the
    only fallback and is accepted only after the page is independently
    verified as the requested footballer. Article/media URLs, ESPN/BBC/
    FotMob search results and club crests are never treated as player
    photos because a visually plausible but wrong face is worse than no
    photo on a verified-news card.
    """
    fpl = fetch_fpl_data()
    player_el = find_player_in_fpl(story.get("player"), fpl)
    player_name = (story.get("display_name")
                   or (player_el["web_name"] if player_el else story.get("player"))
                   or "PLAYER")

    logo_uri = _data_uri(_resolve_logo_path())
    photo_uri = ""

    # 1) Canonical FPL player identity/asset.
    pid = player_el.get("code") if player_el else None
    if pid:
        pp = Path(f"players/{pid}.png")
        if not pp.exists():
            _download_asset(
                f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{pid}.png",
                pp,
            )
        photo_uri = _data_uri(pp)

    # 2) Verified Wikipedia identity fallback only.
    # Never use a raw article/media URL merely because it was attached
    # to the story: the source image may be a teammate, manager or logo.
    if not photo_uri:
        pname = story.get("player", "")
        if pname:
            image_url = _wikipedia_player_image(pname)
            if image_url:
                wp = Path("players/wiki_" + hashlib.md5(pname.encode()).hexdigest()[:12] + ".jpg")
                if not wp.exists():
                    _download_asset(image_url, wp)
                if wp.exists() and wp.stat().st_size > 500:
                    photo_uri = _data_uri(wp)
                    print(f"  [PHOTO] Verified Wikipedia image found for {pname!r}")

    if not photo_uri:
        print(f"  [PHOTO] No identity-verified image available for {player_name!r}; using neutral silhouette.")

    return player_el, player_name, logo_uri, photo_uri


# X rejects PNG uploads over 5 MB. The 4K card lands around 4.5 MB before a
# player photo is composited in, so a good render can still fail at upload
# time — after the story has been marked as posted. Stay clearly under.
MAX_UPLOAD_BYTES = 4_400_000


def _ensure_upload_safe(filename, max_bytes: int = MAX_UPLOAD_BYTES) -> None:
    """Downscale an oversized card until X will accept it.

    Full 4K is kept whenever it fits; only cards that would be rejected are
    stepped down, so quality is never given away for nothing.
    """
    try:
        path = Path(filename)
        if not path.exists() or path.stat().st_size <= max_bytes:
            return
        for target_width in (3200, 2560, 1920, 1600):
            with Image.open(path) as im:
                if im.width <= target_width:
                    continue
                ratio = target_width / im.width
                resized = im.convert("RGB").resize(
                    (target_width, max(1, int(im.height * ratio))), Image.LANCZOS)
            resized.save(path, format="PNG", optimize=True)
            size = path.stat().st_size
            print(f"  [CARD] Downscaled to {target_width}px for upload "
                  f"({size // 1024} KB)")
            if size <= max_bytes:
                return
    except Exception as exc:
        print(f"  [CARD] Could not downscale {filename}: {exc}")


def image_is_blank(path, stddev_floor: float = 3.0) -> bool:
    """True when a rendered card carries no visible content.

    File size is not evidence of content: when Playwright cannot start, the
    card paths fall back to a flat rectangle that still weighs several KB
    and passes any size check. A real card is text and crests on a
    background, so its pixels vary; a flat fill has almost zero variance.

    A missing or unreadable file counts as blank — nothing to post either
    way.
    """
    try:
        from PIL import ImageStat
        p = Path(path)
        if not p.exists() or p.stat().st_size < 100:
            return True
        with Image.open(p) as im:
            # Downscale first: variance survives, and this stays fast on 4K.
            small = im.convert("RGB").resize((96, 54), Image.Resampling.BILINEAR)
            stat = ImageStat.Stat(small)
        return max(stat.stddev) < stddev_floor
    except Exception:
        return True


def _render_card(html_content, filename, width=1380, height=776) -> bool:
    """Render HTML to PNG at 4K via the threaded Playwright helper.

    ``width``/``height`` are the template's CSS design size, not the output
    size — the output is always CARD_OUTPUT_W x CARD_OUTPUT_H. The device pixel
    ratio does the work, so a template does not have to be rewritten in 4K units
    to ship a 4K card.
    """
    try:
        import threading
        error_box = []
        scale = max(1.0, CARD_OUTPUT_W / float(width or CARD_OUTPUT_W))
        t = threading.Thread(target=_render_html_sync,
                             args=(html_content, filename, error_box, width, height, scale))
        t.start()
        t.join()
        if error_box:
            print("  [THREAD TRACEBACK]\n" + error_box[0])
        if Path(filename).exists() and Path(filename).stat().st_size >= 1000:
            _normalise_card_size(filename)
            _ensure_upload_safe(filename)
            return True
    except Exception:
        import traceback
        traceback.print_exc()
    return False


def _build_card_html(player_name, status, badge_color, club_color,
                     logo_uri, photo_uri, crest_uri, rows, source_text, footer_tag):
    """One template for ALL card types so branding (lion logo, header, footer) is identical.

    rows: list of (label, label_color, value_html, value_style).
    """
    logo_html = (f'<img src="{logo_uri}" style="width:64px;height:64px;object-fit:contain;'
                 f'margin-right:16px;filter:drop-shadow(0 2px 6px rgba(0,0,0,0.6));" />') if logo_uri else ''
    crest_badge_html = f'<img class="crest-badge" src="{crest_uri}" />' if crest_uri else ''
    if photo_uri:
        photo_img_html = f'<img src="{photo_uri}" style="width:100%;height:100%;object-fit:cover;position:relative;z-index:1;" />'
    elif crest_uri:
        photo_img_html = f'<img src="{crest_uri}" style="width:70%;height:70%;object-fit:contain;position:relative;z-index:1;opacity:0.85;" />'
    else:
        photo_img_html = '<div style="z-index:1;font-size:150px;color:rgba(255,255,255,0.15);font-weight:900;">V</div>'

    rows_html = "".join(
        f'<div class="detail-label" style="color:{color};">{label}</div>'
        f'<div class="detail-value" style="{vstyle}">{value}</div>'
        for (label, color, value, vstyle) in rows)

    return f"""<!DOCTYPE html><html><head><style>
        @import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@700;900&display=swap');
        body {{ margin:0; padding:0; width:1380px; height:776px; background:linear-gradient(135deg,#0b1220 0%,#1c2846 100%); font-family:'Montserrat',sans-serif; color:white; display:flex; overflow:hidden; position:relative; }}
        .accent-slash {{ position:absolute; width:200%; height:100px; background:{club_color}; opacity:0.15; transform:rotate(-35deg) translateY(-200px); z-index:0; }}
        .accent-slash:nth-child(2) {{ transform:rotate(-35deg) translateY(200px); opacity:0.05; }}
        .container {{ width:100%; height:100%; display:flex; flex-direction:row; padding:40px 60px 80px 60px; box-sizing:border-box; z-index:1; }}
        .left-column {{ flex:1; min-width:0; display:flex; flex-direction:column; justify-content:flex-start; padding-top:30px; }}
        .right-column {{ width:420px; flex-shrink:0; display:flex; align-items:center; justify-content:flex-end; }}
        .wordmark {{ font-size:52px; font-weight:900; margin-bottom:24px; text-shadow:0 4px 10px rgba(0,0,0,0.5); display:flex; align-items:center; }}
        .wordmark span {{ color:#54e07c; margin-left:10px; }}
        .status-badge {{ display:inline-block; background:{badge_color}; color:#fff; padding:14px 30px; font-size:42px; font-weight:900; border-radius:12px; letter-spacing:3px; margin-bottom:20px; text-transform:uppercase; box-shadow:0 8px 20px rgba(0,0,0,0.4); }}
        .player-name {{ font-size:88px; font-weight:900; line-height:1.0; text-transform:uppercase; margin-bottom:28px; text-shadow:0 8px 20px rgba(0,0,0,0.6); white-space:nowrap; max-width:100%; }}
        .details-grid {{ display:grid; grid-template-columns:max-content 1fr; gap:18px 40px; font-size:44px; align-items:center; }}
        .detail-label {{ font-weight:700; text-transform:uppercase; }}
        .detail-value {{ font-weight:900; text-transform:uppercase; color:white; display:flex; align-items:center; }}
        .photo-panel {{ width:370px; height:560px; background:rgba(255,255,255,0.03); border:2px solid rgba(255,255,255,0.1); border-radius:24px; display:flex; align-items:center; justify-content:center; box-shadow:0 20px 50px rgba(0,0,0,0.5); position:relative; overflow:hidden; }}
        .crest-badge {{ position:absolute; top:18px; right:18px; width:120px; height:120px; object-fit:contain; z-index:2; filter:drop-shadow(0 4px 8px rgba(0,0,0,0.6)); }}
        .photo-panel::before {{ content:''; position:absolute; top:0; left:0; right:0; bottom:0; background:radial-gradient(circle at center,{club_color} 0%,transparent 70%); opacity:0.2; z-index:0; }}
        .footer {{ position:absolute; bottom:0; left:0; width:100%; height:65px; background:#141821; display:flex; align-items:center; justify-content:space-between; padding:0 60px; box-sizing:border-box; font-size:24px; font-weight:700; color:#bec8dc; border-top:4px solid {club_color}; }}
    </style></head><body>
        <div class="accent-slash"></div><div class="accent-slash"></div>
        <div class="container">
            <div class="left-column">
                <div class="wordmark">{logo_html}FPL<span>VORTEX</span></div>
                <div><div class="status-badge">{status}</div></div>
                <div class="player-name">{player_name}</div>
                <div class="details-grid">{rows_html}</div>
            </div>
            <div class="right-column"><div class="photo-panel">{crest_badge_html}{photo_img_html}</div></div>
        </div>
        <div class="footer"><div>Source: {source_text} | @FPLVortex</div><div style="color:#d4af37;">{footer_tag}</div></div>
        <script>
            // Shrink the player name to ALWAYS fit the left column — never clip or
            // ellipsize. Measure against the real available width (the parent),
            // and run after full load so font/layout metrics are final.
            function fitPlayerName() {{
                const nameEl = document.querySelector('.player-name');
                if (!nameEl) return;
                const avail = nameEl.parentElement.clientWidth;
                let fs = 88;
                nameEl.style.fontSize = fs + 'px';
                while (nameEl.scrollWidth > avail && fs > 22) {{
                    fs -= 1; nameEl.style.fontSize = fs + 'px';
                }}
            }}
            document.addEventListener("DOMContentLoaded", fitPlayerName);
            window.addEventListener("load", fitPlayerName);
        </script>
    </body></html>"""


# ── BROADCAST FRAME TEMPLATE ─────────────────────────────────────────────
# Replaces the plain navy-gradient card above as the live production
# template. Matches the approved reference broadcast frame (red/black,
# diagonal corner cuts, glowing edge lines, top brand strip, bottom
# source/social strip) with the real lion Logo.png used as the brand mark
# instead of the reference's placeholder circular badge — that badge isn't
# an asset that exists in this repo, so it is not something the renderer
# can reproduce; the lion is the actual approved logo (see Logo.png).
#
# One frame, one background treatment, for every category — only the
# category chip/heading colour and the edge-glow colour change, matching
# the instruction that the background must read as identical across every
# post and only the heading should carry category colour.
CATEGORY_FRAME = {
    "TRANSFER":        {"heading": "TRANSFER NEWS",     "accent": "#39FF88", "bg": "transfer_bg.jpg"},
    "SUSPENSION":      {"heading": "SUSPENSION NEWS",   "accent": "#FFD60A", "bg": "suspension_bg.jpg"},
    "INJURY":          {"heading": "INJURY NEWS",       "accent": "#FF3B30", "bg": "injury_bg.jpg"},
    "PRESS_CONFERENCE":{"heading": "PRESS CONFERENCE",  "accent": "#39FF88", "bg": "press_conference_bg.jpg"},
}
# ^ accent is used only for text/row-icon colour now that the background
# itself is the real per-category asset (see assets/frames/) -- the
# reference PDF's own tint already carries the primary category colour
# (purple/transfer, gold/suspension, magenta/injury, green/press), so the
# accent here is deliberately the brand green almost everywhere, matching
# how the reference's row icons and text stay green/white regardless of
# background tint. Suspension keeps gold and injury keeps red to match
# their reference's own heading-pill colour, since those two are the
# exception in the source material (visibly different pill colour, not
# just a tint).

_ASSET_FRAMES_DIR = Path(__file__).resolve().parent.parent / "assets" / "frames"


def _frame_category(event: str) -> dict:
    key = str(event or "").upper()
    if key in {"PRESS", "TEAM_NEWS"}:
        key = "PRESS_CONFERENCE"
    return CATEGORY_FRAME.get(key, CATEGORY_FRAME["TRANSFER"])


def _frame_background_uri(event: str) -> str:
    theme = _frame_category(event)
    return _data_uri(_ASSET_FRAMES_DIR / theme["bg"])


# Row icons as inline SVG (no external icon font / no network dependency),
# matching the icon-list reference's row-icon set: person, calendar,
# shield, clipboard, trending-chart, cash, quote-mark, speech-bubble.
_ROW_ICONS = {
    "person": '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4.4 3.6-8 8-8s8 3.6 8 8"/></svg>',
    "calendar": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>',
    "shield": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6l7-3z"/></svg>',
    "clipboard": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V3a1 1 0 011-1h4a1 1 0 011 1v1M8 11h8M8 15h5"/></svg>',
    "trend": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 17l6-6 4 4 8-8M15 7h6v6"/></svg>',
    "cash": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="12" cy="12" r="3"/></svg>',
    "quote": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 7c-2 0-3.5 1.5-3.5 4S5 15 7 15v3c-3.5 0-6-2.5-6-6.5S3.5 5 7 5v2zm10 0c-2 0-3.5 1.5-3.5 4s1.5 4 3.5 4v3c-3.5 0-6-2.5-6-6.5S13.5 5 17 5v2z"/></svg>',
    "mic": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0014 0M12 18v4M8 22h8"/></svg>',
    "transfer_arrow": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h14M13 7l5 5-5 5"/></svg>',
}


def _row_html(icon_key: str, label: str, value: str, accent: str) -> str:
    icon_svg = _ROW_ICONS.get(icon_key, _ROW_ICONS["clipboard"])
    return (f'<div class="data-row"><div class="row-icon" style="color:{accent};">{icon_svg}</div>'
            f'<div class="row-body"><div class="row-label">{label}</div>'
            f'<div class="row-value">{value}</div></div></div>')


def category_rows(event: str, story: dict) -> list:
    """Build the icon-row field list for a category from REAL story/extractor
    field names only -- no invented field names.

    Field sources (see src/verification/extractor.py add_fact calls):
      injury:     diagnosis, expected_return                (existing)
      suspension: diagnosis (reused as reason), suspension_length, expected_return
      transfer:   from_club/to_club, fee, contract           (existing, unchanged)
      press_conference: quote_summary, quote_topic
    Any field not present on the story is simply omitted from the row
    list rather than rendered with a placeholder -- an absent fact should
    not appear to have a value.
    """
    key = str(event or "").upper()
    if key in {"PRESS", "TEAM_NEWS"}:
        key = "PRESS_CONFERENCE"
    rows = []
    if key == "INJURY":
        if story.get("diagnosis"):
            rows.append(("clipboard", "DIAGNOSIS", str(story["diagnosis"])))
        rows.append(("trend", "AVAILABILITY",
                     {4: "Available / fit again", 3: "Ruled out", 2: "Doubt",
                      1: "To be assessed"}.get(story.get("stage", 1), "To be assessed")))
        rows.append(("calendar", "TIMELINE", story.get("expected_return") or "Awaiting update"))
        if story.get("next_match"):
            rows.append(("shield", "NEXT MATCH", str(story["next_match"])))
    elif key == "SUSPENSION":
        rows.append(("shield", "REASON", str(story.get("diagnosis") or "Disciplinary")))
        if story.get("suspension_length"):
            rows.append(("clipboard", "LENGTH", str(story["suspension_length"])))
        rows.append(("calendar", "RETURNS", story.get("expected_return") or "To be confirmed"))
    elif key == "PRESS_CONFERENCE":
        if story.get("quote_summary"):
            rows.append(("quote", "SAID", str(story["quote_summary"])))
        if story.get("quote_topic"):
            rows.append(("mic", "TOPIC", str(story["quote_topic"])))
        if not rows:
            rows.append(("mic", "TOPIC", "Press conference update"))
    else:  # TRANSFER and anything else routed through the transfer path
        pass  # transfer rows are built by the caller (create_transfer_image), unchanged
    return rows


def _build_photo_frame_html(player_name, status, event, logo_uri, photo_uri,
                            crest_uri, rows, source_text, footer_tag,
                            season_text=None):
    """FPL VORTEX card frame using the real approved background image per
    category (assets/frames/*.jpg, extracted from the provided reference
    deck) instead of a CSS-drawn approximation. The header (lion logo,
    wordmark, category pill, PL crest) and footer (source/follow/YouTube
    strip) are baked into the background image itself; this function only
    overlays the dynamic player content into the reserved empty region on
    the right ~55% of the frame.

    rows: list of (icon_key, label, value) tuples -- see category_rows().
    Season text is auto-derived (current_fpl_season()) unless overridden.
    """
    theme = _frame_category(event)
    accent = theme["accent"]
    if season_text is None:
        season_text = current_fpl_season()
    bg_uri = _frame_background_uri(event)

    photo_accent_html = f'<div class="photo-accent"><img src="{photo_uri}" /></div>' if photo_uri else ''
    crest_badge_html = f'<img class="crest-badge" src="{crest_uri}" />' if crest_uri else ''

    rows_html = "".join(_row_html(icon, label, value, accent) for (icon, label, value) in rows)

    return f"""<!DOCTYPE html><html><head><style>
        @import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@700;800;900&display=swap');
        * {{ box-sizing:border-box; }}
        body {{ margin:0; padding:0; width:1380px; height:776px; overflow:hidden; position:relative;
                font-family:'Montserrat',sans-serif; color:#fff;
                background-image:url('{bg_uri}'); background-size:cover; background-position:center; }}

        /* Content lives in the empty right region of the reference image --
           header (logo/wordmark/pill/PL crest) and footer (source/follow/
           YouTube strip) are already baked into the background, so nothing
           is drawn here for those; this only positions the dynamic card
           content into that reserved space. */
        .content {{ position:absolute; left:44%; right:4%; top:17%; bottom:13%;
                    display:flex; flex-direction:column; justify-content:center; }}

        .season-badge {{ position:absolute; top:-2.2%; right:0; background:rgba(0,0,0,.4);
                         border:1px solid rgba(255,255,255,.18); border-radius:6px;
                         padding:4px 12px; text-align:right; }}
        .season-badge .label {{ font-size:11px; font-weight:800; letter-spacing:1.5px; color:#B8C2CC; }}
        .season-badge .value {{ font-size:15px; font-weight:900; color:#fff; }}

        .status-badge {{ display:inline-block; background:{accent}; color:#05070B; padding:10px 24px;
                         font-size:30px; font-weight:900; border-radius:8px; letter-spacing:2px;
                         margin-bottom:16px; text-transform:uppercase; align-self:flex-start;
                         box-shadow:0 6px 16px rgba(0,0,0,.4); }}
        .player-name {{ font-size:64px; font-weight:900; line-height:1.02; text-transform:uppercase;
                        margin-bottom:22px; text-shadow:0 6px 16px rgba(0,0,0,.7); white-space:nowrap;
                        max-width:100%; }}

        /* Icon-row data fields, styled after the approved reference layout:
           angled icon tab + bordered label/value row, one per fact. */
        .data-row {{ display:flex; align-items:stretch; margin-bottom:12px; }}
        .row-icon {{ flex:0 0 auto; width:52px; display:flex; align-items:center; justify-content:center;
                    background:rgba(255,255,255,.06); border:1px solid rgba(255,255,255,.18);
                    border-right:none; border-radius:6px 0 0 6px; }}
        .row-icon svg {{ width:26px; height:26px; }}
        .row-body {{ flex:1; min-width:0; padding:8px 18px; background:rgba(255,255,255,.03);
                    border:1px solid rgba(255,255,255,.18); border-radius:0 6px 6px 0; }}
        .row-label {{ font-size:13px; font-weight:800; letter-spacing:2px; color:#B8C2CC; }}
        .row-value {{ font-size:22px; font-weight:900; color:#fff; text-transform:uppercase;
                     overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}

        .photo-accent {{ position:absolute; right:0; top:0; bottom:0; width:26%; z-index:0;
                        -webkit-mask-image:linear-gradient(to right, transparent, rgba(0,0,0,.7) 45%, black 70%);
                        mask-image:linear-gradient(to right, transparent, rgba(0,0,0,.7) 45%, black 70%); }}
        .photo-accent img {{ width:100%; height:100%; object-fit:cover; opacity:.35; }}
        .crest-badge {{ position:absolute; bottom:6%; right:0; width:64px; height:64px;
                        object-fit:contain; z-index:2; filter:drop-shadow(0 3px 8px rgba(0,0,0,.7)); }}
    </style></head><body>
        {photo_accent_html}
        <div class="content">
            <div class="season-badge"><span class="label">SEASON&nbsp;</span><span class="value">{season_text}</span></div>
            <div class="status-badge">{status}</div>
            <div class="player-name">{player_name}</div>
            {rows_html}
            {crest_badge_html}
        </div>
        <script>
            function fitPlayerName() {{
                const nameEl = document.querySelector('.player-name');
                if (!nameEl) return;
                const avail = nameEl.parentElement.clientWidth;
                let fs = 64;
                nameEl.style.fontSize = fs + 'px';
                while (nameEl.scrollWidth > avail && fs > 20) {{
                    fs -= 1; nameEl.style.fontSize = fs + 'px';
                }}
            }}
            document.addEventListener("DOMContentLoaded", fitPlayerName);
            window.addEventListener("load", fitPlayerName);
        </script>
    </body></html>"""


def _club_code_from_key(key, fallback=""):
    codes = {
        "Arsenal": "ARS", "Aston_Villa": "AVL", "Bournemouth": "BOU",
        "Brentford": "BRE", "Brighton": "BHA", "Burnley": "BUR",
        "Chelsea": "CHE", "Crystal_Palace": "CRY", "Everton": "EVE",
        "Fulham": "FUL", "Ipswich": "IPS", "Leeds": "LEE",
        "Leicester": "LEI", "Liverpool": "LIV", "Man_City": "MCI",
        "Man_Utd": "MUN", "Newcastle": "NEW", "Nottm_Forest": "NFO",
        "Southampton": "SOU", "Spurs": "TOT", "Sunderland": "SUN",
        "West_Ham": "WHU", "Wolves": "WOL",
    }
    if key in codes:
        return codes[key]
    text = str(fallback or key or "").upper()
    letters = re.sub(r"[^A-Z]", "", text)
    return letters[:3] if letters else ""


def _fpl_team_for_player(player_el, fpl_data):
    if not player_el or not fpl_data:
        return None, "", ""
    team_id = player_el.get("team")
    team = next((t for t in fpl_data.get("teams", []) if t.get("id") == team_id), None)
    if not team:
        return None, "", ""
    name = str(team.get("name") or team.get("short_name") or "")
    key = _verified_club_key(name)
    code = str(team.get("short_name") or _club_code_from_key(key, name)).upper()
    return key, name, code


def _safe_card_text(value, fallback=""):
    text = " ".join(str(value or "").split()).strip()
    if not text or text.lower() in {"none", "null", "unknown", "n/a", "na"}:
        return fallback
    if text.isdigit():
        return fallback
    return text


def _display_name_parts(name: str):
    parts = [p for p in str(name or "PLAYER").upper().split() if p]
    if len(parts) <= 1:
        return "", parts[0] if parts else "PLAYER"
    return " ".join(parts[:-1]), parts[-1]


def _fpl_position(player_el, fpl_data):
    if not player_el or not fpl_data:
        return ""
    et = player_el.get("element_type")
    if et:
        record = next((x for x in fpl_data.get("element_types", []) if x.get("id") == et), None)
        if record:
            return str(record.get("singular_name") or record.get("singular_name_short") or "").upper()
    return str(player_el.get("position") or "").upper()


def _fpl_price(player_el):
    if not player_el:
        return ""
    try:
        return f"£{float(player_el.get('now_cost')) / 10:.1f}M"
    except Exception:
        return ""


def _optional_age(player_el, facts):
    raw = facts.get("age") or (player_el or {}).get("age")
    if raw:
        return str(raw)
    birth = facts.get("birth_date") or (player_el or {}).get("birth_date")
    if birth:
        try:
            from datetime import date
            year, month, day = [int(x) for x in str(birth)[:10].split("-")]
            today = date.today()
            return str(today.year - year - ((today.month, today.day) < (month, day)))
        except Exception:
            return ""
    return ""


def _optional_nationality(player_el, facts):
    for key in ("nationality", "country", "country_name", "region_name"):
        value = _safe_card_text(facts.get(key) or (player_el or {}).get(key))
        if value:
            return value.upper()
    return ""


def _compact_status_text(value, fallback="NOT REPORTED"):
    text = _safe_card_text(value, fallback=fallback).upper()
    text = re.sub(r"\s+-\s+UNKNOWN.*$", "", text).strip()
    text = text.replace("UNSPECIFIED INJURY", "NOT REPORTED")
    return text or fallback


def _card_theme(event: str, status: str = ""):
    event = str(event or "").upper()
    status = str(status or "").upper()
    # Accents follow docs/CARD_SPEC.md: the colour alone must separate official
    # from unofficial, and medical from administrative, before a word is read.
    if event == "INJURY":
        return {"heading": "INJURY UPDATE", "accent": "#FF5555", "accent_rgb": "255,85,85", "heading_text": "#FFFFFF"}
    if event == "SUSPENSION":
        return {"heading": "SUSPENSION", "accent": "#FFA500", "accent_rgb": "255,165,0", "heading_text": "#05070B"}
    if event in {"PRESS", "PRESS_CONFERENCE", "TEAM_NEWS"}:
        return {"heading": "PRESS CONFERENCE", "accent": "#00BFFF", "accent_rgb": "0,191,255", "heading_text": "#05070B"}
    if status == "MEDICAL":
        return {"heading": "MEDICAL", "accent": "#00E676", "accent_rgb": "0,230,118", "heading_text": "#05070B"}
    if status in {"AGREEMENT", "HERE_WE_GO", "DEAL AGREED"}:
        return {"heading": "DEAL AGREED", "accent": "#00E676", "accent_rgb": "0,230,118", "heading_text": "#05070B"}
    return {"heading": "TRANSFER CONFIRMED", "accent": "#00E676", "accent_rgb": "0,230,118", "heading_text": "#05070B"}


def _html_escape(value):
    from html import escape
    return escape(str(value or ""))


def _value_or_not_disclosed(value):
    return _safe_card_text(value, "NOT DISCLOSED").upper()


def _fact_grid_items(player_el, fpl_data, facts, event):
    items = []
    age = _optional_age(player_el, facts)
    position = _safe_card_text(facts.get("position") or _fpl_position(player_el, fpl_data))
    price = _fpl_price(player_el)
    nationality = _optional_nationality(player_el, facts)
    if age:
        items.append(("AGE", age))
    if position:
        items.append(("POSITION", position.upper()))
    if price:
        items.append(("FPL PRICE", price))
    if nationality:
        items.append(("NATIONALITY", nationality))
    return items[:3]


def _metric_tile(label, value, sub=""):
    return (
        '<div class="metric-tile">'
        f'<div class="tile-label">{_html_escape(label)}</div>'
        f'<div class="tile-value fit-text" data-max="118" data-min="48">{_html_escape(value)}</div>'
        f'<div class="tile-sub fit-text" data-max="34" data-min="24">{_html_escape(sub)}</div>'
        '</div>'
    )


def _mini_tiles(items):
    return "".join(
        '<div class="mini-tile">'
        f'<div class="mini-label">{_html_escape(label)}</div>'
        f'<div class="mini-value fit-text" data-max="48" data-min="26">{_html_escape(value)}</div>'
        '</div>'
        for label, value in (items or [])[:3]
    )


def _club_node(kind, name, code, crest_uri):
    """Club name ABOVE its crest. ``kind`` (FROM/TO/CLUB) is kept as an
    accessible label but not drawn — the arrow between two nodes carries the
    direction, and a name reads faster than a four-letter code."""
    if not name and not crest_uri:
        return ""
    label = name or code
    crest = (f'<img class="club-crest" src="{crest_uri}" alt="{_html_escape(label)}" />'
             if crest_uri else f'<div class="club-crest-fallback">{_html_escape(code or "")}</div>')
    return f'''
      <div class="club-node" data-kind="{_html_escape(kind)}">
        <div class="club-name fit-text" data-max="62" data-min="30">{_html_escape(label)}</div>
        {crest}
      </div>
    '''


def _detail_row(label, label_color, value, crest_uri=""):
    """One LABEL / VALUE line. The label carries the colour; the value is white.

    This is the Elliott card's core device: a narrow coloured label column
    against bold white values, so the eye scans the facts down one axis and
    picks up what kind of fact each one is from the colour, without the card
    ever becoming a colour chart.
    """
    if not value:
        return ""
    crest = f'<img class="row-crest" src="{crest_uri}" alt="" />' if crest_uri else ""
    return (
        '<div class="row">'
        f'<div class="row-label fit-text" data-max="58" data-min="26" '
        f'style="color:{label_color};">{_html_escape(label)}</div>'
        f'<div class="row-value fit-text" data-max="96" data-min="42">{_html_escape(value)}</div>'
        f'{crest}'
        '</div>'
    )


def _build_responsive_verified_card_html(
    *, event, status, player_name, logo_uri, photo_uri, source_text,
    origin_name="", origin_code="", origin_crest="",
    destination_name="", destination_code="", destination_crest="",
    club_name="", club_code="", club_crest="",
    transfer_fee="", contract_length="", contract_expiry="",
    facts_grid=None, status_detail="", position_text="",
    quote_summary="", quote_topic="",
):
    """Render the FPL VORTEX card: pure black, category-coloured heading only.

    Layout follows the original Elliott card — brand mark and category chip top
    left, the player's name set large beneath them, the facts as LABEL/VALUE
    rows, and the player cut out on the right. The only thing that changes
    colour between card types is the category chip and the row labels; the name
    and every value stay white, so a viewer reads the same card each time and
    only the accent tells them what kind of news it is.
    """
    event = str(event or "").upper()
    status = str(status or "").upper()
    theme = _card_theme(event, status)
    accent = theme["accent"]
    accent_rgb = theme["accent_rgb"]
    facts_grid = facts_grid or []
    brand_logo = f'<img class="brand-logo" src="{logo_uri}" />' if logo_uri else ""
    player = (f'<img class="player-img" src="{photo_uri}" />' if photo_uri
              else '<div class="player-silhouette"><div class="sil-head"></div>'
                   '<div class="sil-body"></div></div>')
    # Neutral fallback: only a caller that has checked the evidence may pass
    # "OFFICIALLY CONFIRMED" in. The template must never mint it for itself.
    status_detail = _safe_card_text(status_detail, "NOT REPORTED").upper()
    full_name = _safe_card_text(player_name, "PLAYER").upper()

    # Fixed label palette, shared by every card type. These do NOT vary with
    # category — only the chip does. Keeping them constant is what lets someone
    # who has seen one card read the next one without re-learning it.
    L_FROM, L_TO, L_KEY = "#F2C14E", "#4FA8FF", "#FF5C5C"

    if event == "TRANSFER":
        rows = (
            _detail_row("FROM", L_FROM, _safe_card_text(origin_name or origin_code), origin_crest)
            + _detail_row("TO", L_TO, _safe_card_text(destination_name or destination_code), destination_crest)
            + _detail_row("FEE", L_KEY, _value_or_not_disclosed(transfer_fee))
            + _detail_row("CONTRACT", L_KEY, _safe_card_text(contract_length or contract_expiry))
        )
    elif event in {"INJURY", "SUSPENSION"}:
        rows = (
            _detail_row("CLUB", L_FROM, _safe_card_text(club_name or club_code), club_crest)
            + _detail_row(event, L_KEY, status_detail)
            + "".join(_detail_row(lbl, L_TO, val) for lbl, val in facts_grid[:2])
        )
    elif event == "PRESS_CONFERENCE":
        # Never a longer quote than a short verified summary -- the caller
        # (extractor/renderer) is responsible for keeping this to a few
        # words; the card never truncates raw article text onto itself.
        rows = (
            _detail_row("CLUB", L_FROM, _safe_card_text(club_name or club_code), club_crest)
            + _detail_row("SAID", L_KEY, _safe_card_text(quote_summary, "NOT REPORTED"))
            + (_detail_row("TOPIC", L_TO, quote_topic) if quote_topic else "")
        )
    else:
        rows = (
            _detail_row("CLUB", L_FROM, _safe_card_text(club_name or club_code), club_crest)
            + _detail_row("UPDATE", L_KEY, status_detail)
            + "".join(_detail_row(lbl, L_TO, val) for lbl, val in facts_grid[:2])
        )

    return f'''<!doctype html><html><head><meta charset="utf-8" />
    <style>
      * {{ box-sizing:border-box; }}
      /* Pure #000000, no gradient. The card is lit only by the photo and one
         accent, which is what makes it read as premium rather than busy. */
      html,body {{ margin:0; width:3840px; height:2160px; overflow:hidden; background:#000000; }}
      body {{ font-family: Inter, Montserrat, "DejaVu Sans", Arial, sans-serif; color:#FFFFFF;
              -webkit-font-smoothing:antialiased; }}
      .stage {{ width:3840px; height:2160px; position:relative; background:#000000;
                display:grid; grid-template-rows:minmax(0,1fr) 190px; overflow:hidden; }}
      /* A single soft accent wash behind the subject. Nothing else decorates. */
      .stage::before {{ content:""; position:absolute; right:0; top:0; bottom:0; width:46%;
                        background:radial-gradient(ellipse at 70% 45%, rgba({accent_rgb},.13), transparent 62%);
                        pointer-events:none; }}
      main, footer {{ position:relative; z-index:2; }}
      main {{ min-height:0; display:grid; grid-template-columns:60fr 40fr; gap:64px;
              padding:96px 96px 40px 112px; }}

      .left {{ min-width:0; display:grid; grid-template-rows:auto minmax(0,1fr); }}
      .left-body {{ min-width:0; align-self:center; }}
      .brand {{ display:flex; align-items:center; gap:36px; }}
      /* Logo made significantly larger -- 150px on a 3840px-wide 4K canvas
         was only ~4% of the width and read as small/timid next to the
         reference design's much bolder branding. */
      .brand-logo {{ width:260px; height:260px; object-fit:contain; border-radius:32px; flex:0 0 auto; }}
      .brand-title {{ font-size:120px; line-height:1; font-weight:950; font-style:italic;
                      letter-spacing:-1px; white-space:nowrap; }}
      /* The wordmark is the brand and never tracks the category — only the chip
         and the row labels carry colour. A logo that changes colour per story
         reads as a different publisher each time. */
      .brand-title .vortex {{ color:#00E676; }}

      /* The ONLY element whose colour tracks the news category. Centering
         is handled by .left-body {{ text-align:center }} below (see the
         .chip.fit-text override), since fit-text's own display:block/
         inline-block would otherwise fight a margin:auto centering here. */
      .chip {{ padding:22px 56px; border-radius:14px;
               background:{accent}; color:#000000; font-size:66px; font-weight:950;
               letter-spacing:3px; white-space:nowrap; margin-top:52px; }}

      .player-name {{ margin-top:44px; color:#FFFFFF; font-size:170px; line-height:.94;
                      font-weight:950; letter-spacing:-4px; white-space:nowrap; }}

      .rows {{ margin-top:56px; display:flex; flex-direction:column; gap:34px; }}
      .row {{ display:flex; align-items:center; gap:40px; min-width:0; }}
      /* The label column is fixed-width so values align down one axis, and
         the label itself is fitted. Widened from 300px -- "EXPECTED RETURN"
         (used for injury return dates) was still clipping into the value
         column even at the fit-text shrink floor, because 300px was really
         only enough for the shorter labels like "TO"/"FEE"/"CLUB". */
      .row-label {{ flex:0 0 460px; width:460px; font-size:58px; font-weight:950;
                    letter-spacing:2px; white-space:nowrap; }}
      .row-value {{ flex:0 1 auto; min-width:0; color:#FFFFFF; font-size:96px; font-weight:950;
                    letter-spacing:-1px; white-space:nowrap; }}
      /* Team/club crest enlarged to match the row value's visual weight --
         96px next to 96px text read as an afterthought icon rather than a
         proper club identity mark. */
      .row-crest {{ flex:0 0 auto; width:140px; height:140px; object-fit:contain; }}

      .right {{ min-width:0; display:flex; align-items:center; justify-content:center; }}
      .photo-frame {{ position:relative; width:100%; height:100%; border:5px solid rgba({accent_rgb},.55);
                      border-radius:40px; overflow:hidden; background:rgba(255,255,255,.02);
                      display:flex; align-items:flex-end; justify-content:center;
                      box-shadow:0 0 60px rgba({accent_rgb},.20); }}
      /* width/height:100% (not just max-*) is required: a browser sizes an
         <img> to its own intrinsic pixel size by default, so a small source
         photo (the FPL headshot endpoint is only 250x250) rendered as a tiny
         thumbnail in a mostly-empty frame instead of filling it -- object-fit
         only takes effect once the box itself is actually large. This made
         the player image occupy a few percent of the panel instead of the
         intended ~75-90%. */
      .player-img {{ width:100%; height:100%; object-fit:cover; object-position:center top; }}
      .player-silhouette {{ width:66%; height:82%; position:relative; align-self:flex-end; }}
      .sil-head {{ position:absolute; left:34%; top:2%; width:32%; aspect-ratio:1/1; border-radius:50%;
                   background:rgba(255,255,255,.08); border:6px solid rgba({accent_rgb},.38); }}
      .sil-body {{ position:absolute; left:12%; right:12%; bottom:0; height:66%;
                   border-radius:190px 190px 30px 30px; background:rgba({accent_rgb},.10);
                   border:6px solid rgba({accent_rgb},.30); }}

      footer {{ min-height:0; display:grid; grid-template-columns:minmax(0,1fr) auto;
                align-items:center; gap:48px; padding:0 96px 0 112px;
                border-top:4px solid rgba({accent_rgb},.42); background:#000000; }}
      /* Metadata sits in one muted grey so it never competes with the facts. */
      .source {{ color:#9AA3B2; font-size:50px; font-weight:850; letter-spacing:1px;
                 white-space:nowrap; min-width:0; }}
      .category {{ color:{accent}; font-size:56px; font-weight:950; letter-spacing:4px; white-space:nowrap; }}
      .fit-text {{ display:block; overflow:visible; }}
      /* .fit-text sets display:block and would otherwise stretch the category
         chip to the full column width. inline-block (not block/table) makes
         it hug its own text; centering is done on the parent via
         text-align so it doesn't fight fit-text's own display value. */
      .chip.fit-text {{ display:inline-block; }}
      .left-body {{ text-align:center; }}
      /* Sub-elements that must stay left-aligned despite the centered
         parent (the chip is the only thing that should visually center;
         name/facts still read left-to-right underneath it). */
      .player-name, .rows {{ text-align:left; }}
    </style></head><body><section class="stage">
      <main>
        <section class="left">
          <div class="brand">{brand_logo}<div class="brand-title">FPL <span class="vortex">VORTEX</span></div></div>
          <div class="left-body">
            <div class="chip fit-text" data-max="66" data-min="34">{_html_escape(theme['heading'])}</div>
            <div class="player-name fit-text" data-max="170" data-min="72">{_html_escape(full_name)}</div>
            <div class="rows">{rows}</div>
          </div>
        </section>
        <section class="right"><div class="photo-frame">{player}</div></section>
      </main>
      <footer>
        <div class="source fit-text" data-max="50" data-min="28">Source: {_html_escape(source_text)} | @FPLVortex</div>
        <div class="category">{_html_escape((event or "UPDATE").replace("_", " "))}</div>
      </footer>
    </section><script>
      // fitText shrinks only genuinely overflowing text. These elements are all
      // white-space:nowrap, so they cannot wrap and their HEIGHT is fixed by font
      // metrics, not by content. Measuring height therefore reports overflow for
      // any tight line-height and shrank names that fit perfectly well — width is
      // the only meaningful test here.
      function overflowsWidth(el) {{ return el.scrollWidth > el.clientWidth + 2; }}
      function fitText(el) {{
        const max = Number(el.dataset.max || 64);
        const min = Number(el.dataset.min || 24);
        let fs = max;
        el.style.fontSize = fs + 'px';
        el.style.whiteSpace = 'nowrap';
        while (overflowsWidth(el) && fs > min) {{
          fs -= Math.max(1, Math.ceil(fs * 0.05));
          el.style.fontSize = fs + 'px';
        }}
      }}
      function validateLayout() {{
        const over = [];
        document.querySelectorAll('.fit-text').forEach(function(el) {{
          fitText(el);
          if (overflowsWidth(el)) {{
            over.push((el.className || 'fit-text') + ': ' + (el.textContent || '').slice(0, 60));
          }}
        }});
        // Overflow is REPORTED, never drawn. A debug outline on a published card
        // is a defect the viewer sees; the renderer logs it instead.
        window.__CARD_OVERFLOW__ = over;
        window.__CARD_VALID__ = over.length === 0;
      }}
      document.addEventListener('DOMContentLoaded', validateLayout);
      window.addEventListener('load', validateLayout);
    </script></body></html>'''


def create_verified_branded_card(event, subject, facts, source_handles, filename):
    """Render the responsive production verified card.

    The card is content-driven: CSS Grid/Flexbox controls layout and fitText()
    shrinks only measured overflow. No player-specific coordinates or facts are
    hardcoded.
    """
    event = str(event or "").upper()
    subject = str(subject or "OFFICIAL UPDATE")
    fpl = fetch_fpl_data()
    player_el = find_player_in_fpl(subject, fpl) if fpl else None
    status = str(facts.get("_event_status") or "").upper()
    logo_uri = _data_uri(_resolve_logo_path())
    source_text = " · ".join("@" + str(h).lstrip("@") for h in source_handles[:2]) or "OFFICIAL SOURCE"
    club_name = str(facts.get("club_name") or "")
    club_key = _verified_club_key(club_name)
    origin = str(facts.get("club_from_name") or "")
    destination = str(facts.get("club_to_name") or "")
    origin_key = _verified_club_key(origin)
    destination_key = _verified_club_key(destination)
    if event == "TRANSFER":
        if not origin:
            fpl_key, fpl_name, _fpl_code = _fpl_team_for_player(player_el, fpl)
            if fpl_name and _verified_club_key(fpl_name) != destination_key:
                origin, origin_key = fpl_name, fpl_key
        seed_key = destination_key or origin_key
    else:
        seed_key = club_key
    _, player_name, asset_logo_uri, photo_uri = _img_assets({"player": subject, "to_key": seed_key, "from_key": seed_key})
    logo_uri = logo_uri or asset_logo_uri
    if photo_uri and seed_key and photo_uri == _crest_uri(seed_key):
        photo_uri = ""
    position = _safe_card_text(facts.get("position") or _fpl_position(player_el, fpl), "PREMIER LEAGUE").upper()
    facts_grid = _fact_grid_items(player_el, fpl, facts, event)
    if event == "TRANSFER":
        if status == "MEDICAL":
            status_detail = "MEDICAL BOOKED"
        elif status in {"AGREEMENT", "HERE_WE_GO"}:
            status_detail = "DEAL AGREED"
        else:
            status_detail = "OFFICIALLY CONFIRMED"
        html = _build_responsive_verified_card_html(
            event=event, status=status, player_name=player_name, logo_uri=logo_uri,
            photo_uri=photo_uri, source_text=source_text, origin_name=origin,
            origin_code=_club_code_from_key(origin_key, origin), origin_crest=_crest_uri(origin_key),
            destination_name=destination, destination_code=_club_code_from_key(destination_key, destination),
            destination_crest=_crest_uri(destination_key), transfer_fee=facts.get("fee") or "",
            contract_length=facts.get("contract_length") or "", contract_expiry=facts.get("contract_expiry") or facts.get("expiry_date") or "",
            facts_grid=facts_grid, status_detail=status_detail, position_text=position,
        )
    elif event == "PRESS_CONFERENCE":
        # A quote card never quotes the raw article -- quote_summary/topic
        # are already a short, verified summary produced upstream by the
        # extractor, never truncated article text.
        html = _build_responsive_verified_card_html(
            event=event, status=status, player_name=player_name, logo_uri=logo_uri,
            photo_uri=photo_uri, source_text=source_text, club_name=club_name,
            club_code=_club_code_from_key(club_key, club_name), club_crest=_crest_uri(club_key),
            position_text=position, quote_summary=facts.get("quote_summary") or "",
            quote_topic=facts.get("quote_topic") or "",
        )
    else:
        # An injury/suspension card with no reported status must NOT fall back to
        # "OFFICIALLY CONFIRMED" — that stamps a provenance claim, next to a tick,
        # onto the one field we do not have. Saying nothing is known is honest and
        # still useful; claiming it is official when the source never said so is
        # the same failure mode as inventing the subject.
        status_detail = _compact_status_text(
            facts.get("injury_status") if event == "INJURY" else facts.get("suspension_status"),
            fallback="NOT REPORTED",
        )
        html = _build_responsive_verified_card_html(
            event=event, status=status, player_name=player_name, logo_uri=logo_uri,
            photo_uri=photo_uri, source_text=source_text, club_name=club_name,
            club_code=_club_code_from_key(club_key, club_name), club_crest=_crest_uri(club_key),
            facts_grid=facts_grid, status_detail=status_detail, position_text=position,
        )
    return _render_card(html, filename, width=3840, height=2160)


def _verified_club_key(name):
    """Map a verified display name to a renderer key without guessing clubs."""
    normalized = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
    aliases = {"manchester_city": "Man_City", "manchester_united": "Man_Utd",
               "tottenham_hotspur": "Spurs", "nottingham_forest": "Nottm_Forest",
               "aston_villa": "Aston_Villa", "crystal_palace": "Crystal_Palace",
               "west_ham_united": "West_Ham"}
    return aliases.get(normalized, "_".join(part.title() for part in normalized.split("_")))


def _club_cell(name, crest_uri):
    """Club name with an inline crest (or just the name)."""
    if crest_uri:
        return (f'{name} <img src="{crest_uri}" style="width:60px;height:60px;object-fit:contain;'
                f'vertical-align:middle;margin-left:12px;filter:drop-shadow(0 2px 4px rgba(0,0,0,0.5));" />')
    return name


def create_transfer_image(story, sources, filename, collapsed=False):
    player_el, player_name, logo_uri, photo_uri = _img_assets(story)

    to_key = story.get("to_key")
    from_key = story.get("from_key")
    to_club = story.get("to_club") or (to_key or "").replace("_", " ")
    from_club = story.get("from_club") or (from_key or "").replace("_", " ")

    ev = (story.get("event") or "transfer").lower()
    is_staff = ev == "manager"
    mode = story.get("mode", "confirmed")
    # Footer tag ALWAYS states who the subject is: MANAGER / a specific STAFF role
    # (e.g. "GOALKEEPING COACH") / TRANSFER — so a coach is never shown as a player.
    role = (story.get("staff_role") or "").strip()
    if is_staff:
        footer_tag = (role.upper() if role and role.lower() != "staff"
                      else "MANAGER" if "manager" in role.lower() or not role else "STAFF")
    else:
        footer_tag = "TRANSFER"

    if collapsed or story.get("collapsed"):
        status = "DEAL COLLAPSED"
    elif is_staff:
        action = story.get("staff_action")
        if action == "appointment":
            status = "APPOINTED"
        elif action == "departure":
            status = "DEPARTURE"
        else:
            status = "LINKED"      # speculation, not confirmed
    elif mode == "rumour" or not to_key:
        # No verified destination -> never claim CONFIRMED/OFFICIAL.
        status = "TRANSFER RUMOUR"
    else:
        status = "OFFICIAL" if story.get("stage", 1) >= 4 else "CONFIRMED"

    main_crest = _crest_uri(to_key or from_key)

    # Resolve the REAL category instead of always framing as TRANSFER --
    # suspension and press_conference stories were previously routed
    # through this function with transfer's FROM/TO/FEE fields, which
    # don't apply to them. Real event types now get their real category
    # frame + real fields via category_rows(); transfer/loan/manager keep
    # the existing FROM/TO/FEE row-building below, unchanged.
    frame_event = "TRANSFER"
    if ev == "suspension":
        frame_event = "SUSPENSION"
    elif ev == "press_conference":
        frame_event = "PRESS_CONFERENCE"

    if frame_event in ("SUSPENSION", "PRESS_CONFERENCE"):
        rows = category_rows(frame_event, story)
    else:
        rows = []
        if from_club:
            rows.append(("person", "FROM", _club_cell(from_club, _crest_uri(from_key))))
        if to_club:
            rows.append(("transfer_arrow" if not is_staff else "shield", "TO" if not is_staff else "CLUB",
                         _club_cell(to_club, _crest_uri(to_key))))
        if is_staff:
            # Staff/manager cards show the ROLE, never a transfer FEE.
            rows.append(("shield", "ROLE",
                         (role.upper() if role and role.lower() != "staff" else "MANAGER")))
        else:
            raw_fee = story.get("fee")
            if raw_fee:
                fee_value = raw_fee
            elif ev in ("loan", "loan_option"):
                fee_value = "LOAN DEAL"
            elif story.get("is_free"):
                fee_value = "FREE TRANSFER"
            else:
                fee_value = "UNDISCLOSED"
            rows.append(("cash", "FEE", fee_value))

    source_text = " · ".join(f"@{s}" for s in sources[:2])
    html = _build_photo_frame_html(player_name, status, frame_event, logo_uri, photo_uri,
                                   main_crest, rows, source_text, footer_tag)

    if not _render_card(html, filename):
        Image.new('RGB', (CARD_OUTPUT_W, CARD_OUTPUT_H), color=(7, 9, 13)).save(filename)


def create_injury_image(story, sources, filename):
    # Same template/branding as transfer cards (lion logo + header + footer).
    player_el, player_name, logo_uri, photo_uri = _img_assets(story)
    club_key = story.get("to_key") or story.get("from_key")
    crest_uri = _crest_uri(club_key)

    rows = category_rows("INJURY", story)

    source_text = " · ".join(f"@{s}" for s in sources[:2])
    html = _build_photo_frame_html(player_name, "INJURY UPDATE", "INJURY",
                                   logo_uri, photo_uri, crest_uri, rows, source_text,
                                   (story.get("event", "INJURY") or "INJURY").upper())

    if not _render_card(html, filename):
        _create_injury_image_pil(story, sources, filename)


def _create_injury_image_pil(story, sources, filename):
    """PIL fallback for injury cards if HTML rendering is unavailable."""
    W, H = 1380, 776
    fpl = fetch_fpl_data()
    player_el = find_player_in_fpl(story.get("player"), fpl)
    player_name = (player_el["web_name"] if player_el else story.get("player")) or "PLAYER"

    img = Image.new("RGB", (W, H), (24, 10, 12))
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rectangle([W // 2, 0, W, H], fill=(120, 18, 22))

    right_center = (W - (W // 4), H // 2)
    pid = player_el.get("code") if player_el else None
    img_pasted = False

    if pid:
        pp = Path(f"players/{pid}.png")
        if not pp.exists():
            try: _download_asset(f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{pid}.png", pp)
            except Exception: pass
        if pp.exists() and pp.stat().st_size >= 500:
            p_img = _safe_open_rgba(pp)
            if p_img is not None:
                p_img = _fit_contain(p_img, 400, 500)
                img.paste(p_img, (right_center[0] - p_img.width // 2, right_center[1] - p_img.height // 2 + 30), p_img)
                img_pasted = True

    if not img_pasted:
        club_key = story.get("to_key") or story.get("from_key")
        if club_key:
            crest = _load_crest(club_key, box=350)
            if crest is not None:
                img.paste(crest, (right_center[0] - crest.width // 2, right_center[1] - crest.height // 2), crest)
                img_pasted = True

    if not img_pasted:
        logo_path = _resolve_logo_path()
        if logo_path.exists():
            l_img = _safe_open_rgba(logo_path)
            if l_img is not None:
                l_img = _fit_contain(l_img, 300, 300)
                img.paste(l_img, (right_center[0] - l_img.width // 2, right_center[1] - l_img.height // 2), l_img)

    TEXT_X = 70
    _draw_wordmark(draw, (TEXT_X, 48))

    lf = get_premium_font(34, "Bold")
    label = "INJURY UPDATE"
    draw.rounded_rectangle([TEXT_X, 120, TEXT_X + draw.textlength(label, font=lf) + 36, 168], radius=10, fill=(210, 30, 34))
    _draw_text_shadow(draw, (TEXT_X + 18, 126), label, lf, (255, 255, 255), offset=1)

    nf = get_premium_font(88, "Black")
    _draw_text_shadow(draw, (TEXT_X, 210), player_name.upper(), nf, (255, 255, 255), offset=3)

    rows = []
    if story.get("diagnosis"): rows.append(("DIAGNOSIS", story["diagnosis"]))
    stage = story.get("stage", 1)
    avail = {4: "Available / fit again", 3: "Ruled out", 2: "Doubt", 1: "To be assessed"}.get(stage, "To be assessed")
    rows.append(("AVAILABILITY", avail))
    rows.append(("TIMELINE", story.get("expected_return") or "Awaiting update"))
    if story.get("next_match"): rows.append(("NEXT MATCH", story["next_match"]))

    y = 340
    lab_f = get_premium_font(26, "Bold")
    val_f = get_premium_font(34, "Bold")
    for tag, val in rows[:4]:
        _draw_text_shadow(draw, (TEXT_X, y), tag, lab_f, (255, 140, 140))
        _draw_text_shadow(draw, (TEXT_X, y + 32), str(val), val_f, (255, 255, 255))
        y += 96

    draw.rectangle([0, H - 90, W, H - 12], fill=(20, 10, 12))
    src = " · ".join(f"@{s}" for s in sources[:2])
    bar = f"Source: {src}  |  {CHANNEL_HANDLE}"
    bf = get_premium_font(32, "Bold")
    draw.text((60, H - 70), bar, font=bf, fill=(220, 190, 190))
    img.save(filename)


def _create_fallback_card(story, sources, filename):
    W, H = 1200, 675
    img = Image.new("RGB", (W, H), (11, 18, 32))
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rectangle([0, 0, W, 12], fill=(212, 175, 55))
    draw.rectangle([0, H - 12, W, H], fill=(212, 175, 55))
    _draw_wordmark(draw, (60, 48))
    lf = get_premium_font(40, "Bold")
    label = "BREAKING NEWS"
    draw.rounded_rectangle([60, 130, 60 + draw.textlength(label, font=lf) + 44, 192], radius=12, fill=(210, 30, 34))
    _draw_text_shadow(draw, (60 + 22, 138), label, lf, (255, 255, 255), offset=2)
    head = (story.get("headline") or story.get("player") or "Football update").upper()
    hf = get_premium_font(64, "Black")
    words, line, y = head.split(), "", 250
    for w in words:
        test = (line + " " + w).strip()
        if draw.textlength(test, font=hf) > W - 120 and line:
            _draw_text_shadow(draw, (60, y), line, hf, (255, 255, 255), offset=3)
            y += 78
            line = w
        else: line = test
    if line: _draw_text_shadow(draw, (60, y), line, hf, (255, 255, 255), offset=3)
    src = " · ".join(f"@{s}" for s in (sources or [])[:2]) or CHANNEL_HANDLE
    draw.rectangle([0, H - 78, W, H - 12], fill=(20, 24, 33))
    bf = get_premium_font(30, "Bold")
    draw.text((60, H - 64), f"Source: {src}  |  {CHANNEL_HANDLE}", font=bf, fill=(190, 200, 220))
    img.save(filename)
