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

def _render_html_sync(html_content, filename, error_box=None, width=1380, height=776):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
            page.set_content(html_content, wait_until="domcontentloaded")
            page.wait_for_timeout(500)
            page.screenshot(path=filename)
            browser.close()
    except Exception:
        if error_box is not None:
            import traceback
            error_box.append(traceback.format_exc())


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
    """Shared: resolve the verified player, display name, brand logo and player photo."""
    fpl = fetch_fpl_data()
    player_el = find_player_in_fpl(story.get("player"), fpl)
    # Prefer the single canonical display name set by verify_card_data so the card
    # and the tweet always show the exact same name.
    player_name = (story.get("display_name")
                   or (player_el["web_name"] if player_el else story.get("player"))
                   or "PLAYER")

    logo_uri = _data_uri(Path("Logo.png"))

    photo_uri = ""
    pid = player_el.get("code") if player_el else None
    if pid:
        pp = Path(f"players/{pid}.png")
        if not pp.exists():
            _download_asset(f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{pid}.png", pp)
        photo_uri = _data_uri(pp)
    if not photo_uri and story.get("media_url"):
        murl = story["media_url"]
        ext = ".jpg" if any(x in murl.lower() for x in (".jpg", ".jpeg")) else ".png"
        mp = Path("players/tw_" + hashlib.md5(murl.encode()).hexdigest()[:12] + ext)
        if not mp.exists():
            _download_asset(murl, mp)
        photo_uri = _data_uri(mp)

    # Wikipedia fallback: if neither FPL API nor media_url produced a photo,
    # resolve the player's article and take its lead image.
    # This covers players not in the FPL dataset (foreign signings, academy
    # players, brand-new arrivals) — exactly the ones with no FPL headshot.
    if not photo_uri:
        pname = story.get("player", "")
        if pname:
            thumb = _wikipedia_player_image(pname)
            if thumb:
                wp = Path("players/wiki_" + hashlib.md5(pname.encode()).hexdigest()[:12] + ".jpg")
                if not wp.exists():
                    _download_asset(thumb, wp)
                if wp.exists() and wp.stat().st_size > 500:
                    photo_uri = _data_uri(wp)
                    print(f"  [PHOTO] Wikipedia image found for {pname!r}")

    # ESPN fallback: search the ESPN athletes API and fetch their headshot.
    if not photo_uri:
        pname = story.get("player", "")
        if pname:
            try:
                espn_url = ("https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1"
                            "/athletes?search=" + urllib.parse.quote(pname) + "&limit=5")
                req = urllib.request.Request(
                    espn_url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; FPLVortexBot/1.0)"}
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    edata = json.loads(resp.read().decode("utf-8"))
                athletes = edata.get("athletes") or []
                for ath in athletes[:3]:
                    ath_id = str(ath.get("id") or "")
                    if not ath_id:
                        continue
                    img_url = (f"https://a.espncdn.com/combiner/i?img=/i/headshots/soccer"
                               f"/players/full/{ath_id}.png&w=350&h=254")
                    ep = Path("players/espn_" + hashlib.md5(pname.encode()).hexdigest()[:12] + ".png")
                    if not ep.exists():
                        _download_asset(img_url, ep)
                    if ep.exists() and ep.stat().st_size > 500:
                        photo_uri = _data_uri(ep)
                        print(f"  [PHOTO] ESPN image found for {pname!r} (id={ath_id})")
                        break
            except Exception as _ee:
                print(f"  [PHOTO] ESPN lookup failed for {pname!r}: {_ee}")

    # BBC Sport fallback: scrape the og:image from the player's BBC Sport page.
    if not photo_uri:
        pname = story.get("player", "")
        if pname:
            try:
                slug = re.sub(r"[^a-z0-9]+", "-", pname.lower()).strip("-")
                bbc_url = f"https://www.bbc.co.uk/sport/football/players/{slug}"
                req = urllib.request.Request(
                    bbc_url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; FPLVortexBot/1.0)"}
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
                m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
                if not m:
                    m = re.search(r'content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html)
                if m:
                    img_url = m.group(1)
                    ext = ".jpg" if any(x in img_url.lower() for x in (".jpg", ".jpeg")) else ".png"
                    bp = Path("players/bbc_" + hashlib.md5(pname.encode()).hexdigest()[:12] + ext)
                    if not bp.exists():
                        _download_asset(img_url, bp)
                    if bp.exists() and bp.stat().st_size > 500:
                        photo_uri = _data_uri(bp)
                        print(f"  [PHOTO] BBC Sport image found for {pname!r}")
            except Exception as _be:
                pass

    # FotMob fallback: search FotMob for the player and fetch their photo.
    # FotMob has the most comprehensive photo database for active football players.
    if not photo_uri:
        pname = story.get("player", "")
        if pname:
            try:
                fotmob_url = ("https://www.fotmob.com/api/search?term="
                              + urllib.parse.quote(pname) + "&lang=en")
                req = urllib.request.Request(
                    fotmob_url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; FPLVortexBot/1.0)",
                             "Accept": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    fdata = json.loads(resp.read().decode("utf-8"))
                players = fdata.get("squad") or fdata.get("players") or []
                for entry in players[:3]:
                    fid = str(entry.get("id") or "")
                    if not fid:
                        continue
                    img_url = (entry.get("imageUrl") or
                               f"https://images.fotmob.com/image_resources/playerimages/{fid}.png")
                    fp = Path("players/fm_" + hashlib.md5(pname.encode()).hexdigest()[:12] + ".png")
                    if not fp.exists():
                        _download_asset(img_url, fp)
                    if fp.exists() and fp.stat().st_size > 500:
                        photo_uri = _data_uri(fp)
                        print(f"  [PHOTO] FotMob image found for {pname!r} (id={fid})")
                        break
            except Exception as _fe:
                print(f"  [PHOTO] FotMob lookup failed for {pname!r}: {_fe}")

    # Club crest fallback: if still no photo, use the destination or origin crest
    # as a last resort so the card is never completely imageless.
    if not photo_uri:
        crest = _crest_uri(story.get("to_key") or story.get("from_key"))
        if crest:
            photo_uri = crest
            print(f"  [PHOTO] Crest fallback for {story.get('player')!r}")

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


def _render_card(html_content, filename, width=1380, height=776) -> bool:
    """Render HTML to PNG via the threaded Playwright helper. Returns True on success."""
    try:
        import threading
        error_box = []
        t = threading.Thread(target=_render_html_sync, args=(html_content, filename, error_box, width, height))
        t.start()
        t.join()
        if error_box:
            print("  [THREAD TRACEBACK]\n" + error_box[0])
        if Path(filename).exists() and Path(filename).stat().st_size >= 1000:
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


def _split_player_name_html(name: str) -> str:
    from html import escape
    parts = [p for p in str(name or "OFFICIAL UPDATE").upper().split() if p]
    if len(parts) <= 1:
        return f'<span class="name-green">{escape(parts[0] if parts else "UPDATE")}</span>'
    first = escape(" ".join(parts[:-1]))
    last = escape(parts[-1])
    return f'<span>{first}</span> <span class="name-green">{last}</span>'


def _broadcast_rows_html(rows):
    from html import escape
    html = []
    for icon, label, value in rows[:3]:
        html.append(
            '<div class="info-row">'
            f'<div class="info-icon">{escape(str(icon))}</div>'
            f'<div class="info-label">{escape(str(label))}:</div>'
            f'<div class="info-value">{escape(str(value))}</div>'
            '</div>'
        )
    return "".join(html)


def _build_verified_broadcast_html(
    *,
    event,
    player_name,
    status,
    logo_uri,
    photo_uri,
    origin_name="",
    origin_code="",
    origin_crest="",
    destination_name="",
    destination_code="",
    destination_crest="",
    club_name="",
    club_code="",
    club_crest="",
    club_color="",
    rows=None,
    source_text="Official source",
    footer_tag="TRANSFER",
):
    """4K premium broadcast template for V2 verified cards only."""
    from html import escape
    rows = rows or []
    is_injury_theme = str(event).upper() == "INJURY"
    accent = "#FF3045" if is_injury_theme else "#27FF89"
    accent2 = "#FF4B5E" if is_injury_theme else "#2CFF95"
    accent_rgb = "255,48,69" if is_injury_theme else "39,255,137"
    accent2_rgb = "255,75,94" if is_injury_theme else "44,255,149"
    table_accent = "#FF3045" if is_injury_theme else "#D4AF37"
    table_rgb = "255,48,69" if is_injury_theme else "212,175,55"
    badge_bg = "#FF3045" if is_injury_theme else "#27FF89"
    badge_fg = "#FFFFFF" if is_injury_theme else "#000000"
    icon_fg = "#FFFFFF" if is_injury_theme else "#07111D"
    # The portrait panel is filled with the club's own colour. Falls back to
    # the event accent so an unmapped club still themes sensibly.
    club_color = str(club_color or "").strip() or accent
    logo_html = (
        f'<img class="brand-logo" src="{logo_uri}" alt="FPL VORTEX logo" />'
        if logo_uri else '<div class="brand-logo placeholder"></div>'
    )
    if photo_uri:
        player_visual = f'<img class="player-photo" src="{photo_uri}" alt="{escape(player_name)}" />'
    else:
        fallback_crest = club_crest or destination_crest or origin_crest
        if fallback_crest:
            player_visual = f'<img class="player-photo crest-fallback" src="{fallback_crest}" alt="club crest" />'
        else:
            player_visual = '<div class="player-photo player-fallback">FPL</div>'

    is_transfer = str(event).upper() == "TRANSFER"
    if is_transfer:
        origin_img = f'<img src="{origin_crest}" alt="{escape(origin_name)}" />' if origin_crest else '<div class="crest-placeholder">FROM</div>'
        destination_img = f'<img src="{destination_crest}" alt="{escape(destination_name)}" />' if destination_crest else '<div class="crest-placeholder">TO</div>'
        origin_box = f"""
            <div class="club-block">
              <div class="club-code">{escape(origin_code or "FROM")}</div>
              <div class="club-box">{origin_img}</div>
            </div>"""
        destination_box = f"""
            <div class="club-block">
              <div class="club-code">{escape(destination_code or "TO")}</div>
              <div class="club-box">{destination_img}</div>
            </div>"""
        transfer_panel = f'<div class="transfer-panel">{origin_box}<div class="arrow">➜</div>{destination_box}</div>'
    else:
        club_img = f'<img src="{club_crest}" alt="{escape(club_name)}" />' if club_crest else '<div class="crest-placeholder">CLUB</div>'
        transfer_panel = f"""
            <div class="transfer-panel single-club">
              <div class="club-block">
                <div class="club-code">{escape(club_code or "PL")}</div>
                <div class="club-box">{club_img}</div>
              </div>
              <div class="event-panel"><div>{escape(status)}</div><span>{escape(club_name or "Premier League")}</span></div>
            </div>"""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8" />
    <style>
      * {{ box-sizing:border-box; }}
      html, body {{ margin:0; width:3840px; height:2160px; overflow:hidden; }}
      body {{
        font-family: "Montserrat", "DejaVu Sans", Arial, sans-serif;
        color:#fff;
        background:
          radial-gradient(circle at 18% 52%, rgba({accent_rgb},0.18), transparent 30%),
          radial-gradient(circle at 82% 22%, rgba(26,107,255,0.17), transparent 25%),
          linear-gradient(135deg,#07111D 0%,#0C1828 100%);
        position:relative;
      }}
      body::before {{
        content:""; position:absolute; inset:-240px;
        background:
          repeating-linear-gradient(125deg,
            transparent 0 74px,
            rgba({accent_rgb},0.14) 75px 80px,
            transparent 81px 168px,
            rgba(26,107,255,0.18) 169px 174px,
            transparent 175px 260px);
        filter:blur(0.2px); opacity:0.72; transform:skewX(-6deg); pointer-events:none;
      }}
      body::after {{
        content:""; position:absolute; inset:0;
        background:radial-gradient(circle at center, transparent 42%, rgba(0,0,0,0.38) 100%);
        pointer-events:none;
      }}
      .safe {{
        position:absolute; inset:60px; border:4px solid rgba({accent_rgb},0.70);
        border-radius:45px; box-shadow:0 0 20px rgba({accent_rgb},0.65), inset 0 0 24px rgba({accent_rgb},0.16);
        z-index:1;
      }}
      .brand {{ position:absolute; left:78px; top:36px; height:420px; display:flex; align-items:center; z-index:3; }}
      .brand-logo {{ width:650px; height:420px; object-fit:contain; margin-right:26px; filter:drop-shadow(0 0 34px rgba({accent_rgb},.78)); }}
      .brand-logo.placeholder {{ border-radius:50%; border:5px solid {accent}; }}
      .brand-title {{ font-size:126px; font-weight:950; letter-spacing:-4px; line-height:.9; font-style:italic; text-shadow:0 6px 18px rgba(0,0,0,.75); margin-left:8px; }}
      .brand-title .fpl {{ color:#fff; }}
      .brand-title .vortex {{ color:{accent}; margin-left:24px; text-shadow:0 0 22px rgba({accent_rgb},.58); }}
      .brand-text {{ display:flex; flex-direction:column; justify-content:center; }}
      .subtitle {{ color:#A8B5C9; font-size:42px; font-weight:700; opacity:.92; margin-left:12px; margin-top:10px; white-space:nowrap; }}
      .badge {{ position:absolute; right:135px; top:75px; width:700px; height:160px; border-radius:60px; background:{badge_bg}; color:{badge_fg}; display:flex; align-items:center; justify-content:center; font-size:82px; font-weight:950; letter-spacing:1px; font-style:italic; z-index:3; box-shadow:0 0 30px rgba({accent_rgb},.42); }}
      /* Rectangular portrait panel, filled with the club's own colour. */
      .portrait-wrap {{ position:absolute; left:165px; top:300px; width:1320px; height:1220px; z-index:2; display:flex; align-items:center; justify-content:center; }}
      /* Solid club colour — flat fill, no gradient. */
      .portrait-ring {{ position:absolute; width:1080px; height:1180px; border-radius:32px; border:8px solid {accent2};
        background:{club_color}; box-shadow:0 0 60px rgba({accent2_rgb},.84); }}
      .portrait-lines {{ position:absolute; width:1064px; height:1164px; border-radius:26px; overflow:hidden; opacity:.30; }}
      .portrait-lines::before {{ content:""; position:absolute; inset:-80px; background:repeating-linear-gradient(135deg, transparent 0 58px, rgba(255,255,255,.14) 59px 70px, transparent 71px 136px, rgba(0,0,0,.16) 137px 145px); }}
      .player-photo {{ position:relative; z-index:4; max-width:940px; max-height:1120px; object-fit:contain; filter:drop-shadow(0 28px 28px rgba(0,0,0,.60)); }}
      /* The crest stand-in sits on the club colour, so it needs its own
         separation or a red crest disappears into a red panel. */
      .crest-fallback {{ width:620px; opacity:.97; filter:drop-shadow(0 0 26px rgba(0,0,0,.75)) drop-shadow(0 10px 20px rgba(0,0,0,.55)); }}
      .player-fallback {{ font-size:190px; font-weight:950; color:rgba(255,255,255,.42); text-shadow:0 6px 18px rgba(0,0,0,.55); }}
      .right {{ position:absolute; left:1725px; right:125px; top:380px; bottom:315px; z-index:3; }}
      .left-player-name {{ position:absolute; left:95px; top:1540px; width:1460px; text-align:center; z-index:5; font-size:126px; line-height:.95; font-weight:950; letter-spacing:-2px; text-transform:uppercase; white-space:nowrap; text-shadow:0 10px 24px rgba(0,0,0,.80); }}
      .left-player-name .fit-name {{ display:inline-block; max-width:1420px; }}
      .transfer-panel {{ height:560px; display:flex; align-items:flex-start; justify-content:center; gap:92px; }}
      .club-block {{ width:420px; text-align:center; }}
      .club-code {{ font-size:90px; font-weight:950; letter-spacing:2px; margin-bottom:14px; text-shadow:0 5px 14px rgba(0,0,0,.7); }}
      /* No white plate behind the crest — it reads as a sticker on a dark
         card. Transparent box, accent border only. */
      .club-box {{ width:320px; height:320px; margin:0 auto; border-radius:35px; background:transparent; border:6px solid {accent}; box-shadow:0 0 24px rgba({accent_rgb},.5); display:flex; align-items:center; justify-content:center; overflow:hidden; }}
      .club-box img {{ max-width:240px; max-height:240px; object-fit:contain; }}
      /* No crest available: a solid club-coloured block carrying the club
         code, not bare text. The box behind it is transparent now, so dark
         text on a dark card would simply vanish. */
      .crest-placeholder {{ width:100%; height:100%; background:{club_color};
        color:#fff; font-size:76px; font-weight:950; letter-spacing:2px;
        display:flex; align-items:center; justify-content:center;
        text-shadow:0 4px 12px rgba(0,0,0,.75); }}
      .arrow {{ font-size:190px; line-height:320px; padding-top:100px; color:{accent}; filter:drop-shadow(0 0 14px rgba({accent_rgb},.88)); }}
      .single-club {{ justify-content:flex-start; gap:80px; }}
      .event-panel {{ flex:1; min-height:320px; border-left:6px solid {accent}; padding:52px 0 0 72px; font-size:86px; line-height:1; font-weight:950; color:{accent}; text-shadow:0 0 14px rgba({accent_rgb},.35); }}
      .event-panel span {{ display:block; margin-top:36px; font-size:58px; color:#fff; }}
      .player-name {{ margin-top:20px; font-size:158px; line-height:.95; font-weight:950; letter-spacing:-2px; text-transform:uppercase; white-space:nowrap; text-shadow:0 10px 24px rgba(0,0,0,.75); }}
      .name-green {{ color:{accent}; text-shadow:0 0 24px rgba({accent_rgb},.55); }}
      .divider {{ height:5px; background:linear-gradient(90deg,transparent,{table_accent},transparent); margin:28px 0 32px 0; box-shadow:0 0 18px rgba({table_rgb},.75); }}
      .info-row {{ min-height:110px; display:grid; grid-template-columns:100px minmax(0,1fr) minmax(420px,760px); align-items:center; gap:24px; border-top:4px solid rgba({table_rgb},.90); background:rgba(7,17,29,.74); padding:15px 26px 14px 0; box-shadow:0 0 16px rgba({table_rgb},.20); }}
      .info-row:last-child {{ border-bottom:4px solid rgba({table_rgb},.90); }}
      .info-icon {{ width:75px; height:75px; border-radius:50%; background:{table_accent}; color:{icon_fg}; display:flex; align-items:center; justify-content:center; font-size:48px; font-weight:950; margin-left:0; box-shadow:0 0 18px rgba({table_rgb},.58); }}
      .info-label {{ font-size:74px; font-weight:950; color:#fff; white-space:nowrap; }}
      /* Values are auto-fitted by fitValues() below rather than clipped: a
         real diagnosis ("UNSPECIFIED INJURY - UNAVAILABLE") must read in
         full, so it wraps to a second line and shrinks until it fits. */
      .info-value {{ font-size:74px; font-weight:950; color:{accent}; text-align:right; max-width:760px; line-height:1.06; overflow-wrap:break-word; }}
      .footer {{ position:absolute; left:76px; right:76px; bottom:48px; height:150px; z-index:4; border:4px solid rgba({accent_rgb},.82); border-radius:26px; background:rgba(7,17,29,.92); box-shadow:0 0 25px rgba({accent_rgb},.52); display:grid; grid-template-columns:1.28fr .88fr .98fr; align-items:center; overflow:hidden; }}
      .foot-section {{ height:100%; display:flex; align-items:center; gap:30px; padding:0 50px; font-size:58px; font-weight:950; white-space:nowrap; }}
      .foot-section + .foot-section {{ border-left:5px solid {accent}; }}
      .foot-icon {{ font-size:76px; color:{accent}; line-height:1; }}
      .youtube {{ width:150px; height:104px; background:#FF0000; border-radius:24px; position:relative; box-shadow:0 0 24px rgba(255,0,0,.55); flex:0 0 auto; }}
      .youtube::after {{ content:""; position:absolute; left:58px; top:25px; width:0; height:0; border-top:27px solid transparent; border-bottom:27px solid transparent; border-left:42px solid white; }}
      .x-icon {{ font-size:126px; color:#fff; font-weight:300; line-height:.85; }}
      .fit-name {{ display:inline-block; max-width:1840px; }}
    </style></head><body>
      <div class="safe"></div>
      <div class="brand">{logo_html}<div class="brand-text"><div class="brand-title"><span class="fpl">FPL</span><span class="vortex">VORTEX</span></div><div class="subtitle">Verified Premier League News</div></div></div>
      <div class="badge">{escape(status)}</div>
      <div class="portrait-wrap"><div class="portrait-ring"></div><div class="portrait-lines"></div>{player_visual}</div>
      <div class="left-player-name"><span class="fit-name">{_split_player_name_html(player_name)}</span></div>
      <main class="right">
        {transfer_panel}
        <div class="divider"></div>
        <div class="info-rows">{_broadcast_rows_html(rows)}</div>
      </main>
      <footer class="footer">
        <div class="foot-section"><span class="foot-icon">◎</span><span>SOURCE: {escape(source_text.upper())}</span></div>
        <div class="foot-section"><span class="youtube"></span><span>@FPLVORTEX</span></div>
        <div class="foot-section"><span class="x-icon">𝕏</span><span>{escape(CHANNEL_HANDLE.upper())}</span></div>
      </footer>
      <script>
        function fitName() {{
          const el = document.querySelector('.left-player-name .fit-name');
          if (!el) return;
          let fs = 126;
          el.parentElement.style.fontSize = fs + 'px';
          while (el.scrollWidth > 1420 && fs > 70) {{ fs -= 2; el.parentElement.style.fontSize = fs + 'px'; }}
        }}
        // Shrink each value until it fits its cell on at most two lines.
        // Nothing is ever cut off — a truncated diagnosis is worse than a
        // slightly smaller one.
        function fitValues() {{
          document.querySelectorAll('.info-value').forEach(function (el) {{
            let fs = 74;
            el.style.fontSize = fs + 'px';
            const maxH = 150;
            while ((el.scrollWidth > el.clientWidth || el.scrollHeight > maxH) && fs > 26) {{
              fs -= 2;
              el.style.fontSize = fs + 'px';
            }}
          }});
        }}
        function fitAll() {{ fitName(); fitValues(); }}
        document.addEventListener('DOMContentLoaded', fitAll);
        window.addEventListener('load', fitAll);
      </script>
    </body></html>"""


def create_verified_branded_card(event, subject, facts, source_handles, filename):
    """Render the premium 4K FPL VORTEX broadcast card from verified facts only.

    This adapter is deliberately data-in/data-out: it never performs a news
    classification and never invents a fee, diagnosis, timeline, or source.
    """
    event = str(event).upper()
    subject = str(subject or "OFFICIAL UPDATE")

    # A confirmed signing, a booked medical and an agreed deal are different
    # claims and the badge has to say which — the engine tells us via
    # _event_status, and the card must never call a medical a done deal.
    _status = str(facts.get("_event_status") or "")
    transfer_badge = (
        "MEDICAL" if _status == "MEDICAL"
        else "DEAL AGREED" if _status in {"AGREEMENT", "HERE_WE_GO"}
        else "CONFIRMED"
    )

    if event == "TRANSFER":
        origin = str(facts.get("club_from_name") or "")
        destination = str(facts.get("club_to_name") or "")
        origin_key = _verified_club_key(origin)
        destination_key = _verified_club_key(destination)
        # Never show a blank or guessed FROM club: if the story did not name
        # one, take the player's current club from the official FPL data, and
        # only if it differs from where they are going.
        if not origin:
            _fpl = fetch_fpl_data()
            _el = find_player_in_fpl(subject, _fpl) if _fpl else None
            fpl_key, fpl_name, fpl_code = _fpl_team_for_player(_el, _fpl)
            if fpl_name and _verified_club_key(fpl_name) != destination_key:
                origin, origin_key = fpl_name, fpl_key
        story = {"player": subject, "to_key": destination_key or origin_key, "from_key": origin_key or destination_key}
        _, player_name, logo_uri, photo_uri = _img_assets(story)
        rows = []
        if facts.get("fee"):
            rows.append(("£", "TRANSFER FEE", str(facts["fee"]).upper()))
        if facts.get("contract_length"):
            rows.append(("▣", "CONTRACT PERIOD", str(facts["contract_length"]).upper()))
        if facts.get("transfer_kind"):
            rows.append(("✓", "MOVE TYPE", str(facts["transfer_kind"]).upper()))
        if not rows:
            rows.append(("✓", "STATUS", {
                "MEDICAL": "MEDICAL BOOKED",
                "DEAL AGREED": "DEAL AGREED",
            }.get(transfer_badge, "OFFICIALLY CONFIRMED")))
        html = _build_verified_broadcast_html(
            event=event,
            player_name=player_name,
            status=transfer_badge,
            logo_uri=logo_uri,
            photo_uri=photo_uri,
            origin_name=origin,
            origin_code=_club_code_from_key(origin_key, origin),
            origin_crest=_crest_uri(origin_key),
            destination_name=destination,
            destination_code=_club_code_from_key(destination_key, destination),
            destination_crest=_crest_uri(destination_key),
            club_color=get_club_color(destination_key),
            rows=rows,
            source_text=" · ".join("@" + str(h).lstrip("@") for h in source_handles[:2]) or "OFFICIAL SOURCE",
            footer_tag="TRANSFER",
        )
    elif event == "INJURY":
        club_name = str(facts.get("club_name") or "")
        club_key = _verified_club_key(club_name)
        story = {"player": subject, "to_key": club_key, "from_key": club_key}
        _, player_name, logo_uri, photo_uri = _img_assets(story)
        rows = []
        if facts.get("injury_status"):
            rows.append(("✚", "INJURY UPDATE", str(facts["injury_status"]).upper()))
        if facts.get("return_date"):
            rows.append(("▣", "RETURN", str(facts["return_date"]).upper()))
        if not rows:
            rows.append(("✚", "STATUS", "OFFICIALLY CONFIRMED"))
        html = _build_verified_broadcast_html(
            event=event,
            player_name=player_name,
            status="✚ INJURY",
            logo_uri=logo_uri,
            photo_uri=photo_uri,
            club_name=club_name,
            club_code=_club_code_from_key(club_key, club_name),
            club_crest=_crest_uri(club_key),
            club_color=get_club_color(club_key),
            rows=rows,
            source_text=" · ".join("@" + str(h).lstrip("@") for h in source_handles[:2]) or "OFFICIAL SOURCE",
            footer_tag="INJURY",
        )
    else:  # SUSPENSION
        club_name = str(facts.get("club_name") or "")
        club_key = _verified_club_key(club_name)
        story = {"player": subject, "to_key": club_key, "from_key": club_key}
        _, player_name, logo_uri, photo_uri = _img_assets(story)
        rows = []
        if facts.get("suspension_status"):
            rows.append(("!", "SUSPENSION", str(facts["suspension_status"]).upper()))
        if facts.get("suspension_length"):
            rows.append(("▣", "LENGTH", str(facts["suspension_length"]).upper()))
        if not rows:
            rows.append(("✓", "STATUS", "OFFICIALLY CONFIRMED"))
        html = _build_verified_broadcast_html(
            event=event,
            player_name=player_name,
            status="SUSPENSION",
            logo_uri=logo_uri,
            photo_uri=photo_uri,
            club_name=club_name,
            club_code=_club_code_from_key(club_key, club_name),
            club_crest=_crest_uri(club_key),
            club_color=get_club_color(club_key),
            rows=rows,
            source_text=" · ".join("@" + str(h).lstrip("@") for h in source_handles[:2]) or "OFFICIAL SOURCE",
            footer_tag="SUSPENSION",
        )

    if _render_card(html, filename, width=3840, height=2160):
        return True
    return False


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
        status, badge = "DEAL COLLAPSED", "#e31e24"
    elif is_staff:
        action = story.get("staff_action")
        if action == "appointment":
            status, badge = "APPOINTED", "#54e07c"
        elif action == "departure":
            status, badge = "DEPARTURE", "#e31e24"
        else:
            status, badge = "LINKED", "#f5c518"      # speculation, not confirmed
    elif mode == "rumour" or not to_key:
        # No verified destination -> never claim CONFIRMED/OFFICIAL.
        status, badge = "TRANSFER RUMOUR", "#e31e24"
    else:
        status = "OFFICIAL" if story.get("stage", 1) >= 4 else "CONFIRMED"
        badge = "#54e07c"

    club_color = get_club_color(to_key or from_key)
    main_crest = _crest_uri(to_key or from_key)

    rows = []
    if from_club:
        rows.append(("FROM", "#f5c518", _club_cell(from_club, _crest_uri(from_key)), ""))
    if to_club:
        rows.append(("TO" if not is_staff else "CLUB", "#00d4ff",
                     _club_cell(to_club, _crest_uri(to_key)), ""))
    if is_staff:
        # Staff/manager cards show the ROLE, never a transfer FEE.
        rows.append(("ROLE", "#f5c518",
                     (role.upper() if role and role.lower() != "staff" else "MANAGER"), ""))
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
        rows.append(("FEE", "#e31e24", fee_value, "color:#54e07c;"))

    source_text = " · ".join(f"@{s}" for s in sources[:2])
    html = _build_card_html(player_name, status, badge, club_color, logo_uri, photo_uri,
                            main_crest, rows, source_text, footer_tag)

    if not _render_card(html, filename):
        Image.new('RGB', (1380, 776), color=(11, 18, 32)).save(filename)


def create_injury_image(story, sources, filename):
    # Same template/branding as transfer cards (lion logo + header + footer).
    player_el, player_name, logo_uri, photo_uri = _img_assets(story)
    club_key = story.get("to_key") or story.get("from_key")
    club_color = get_club_color(club_key)
    crest_uri = _crest_uri(club_key)

    stage = story.get("stage", 1)
    avail = {4: "Available / fit again", 3: "Ruled out", 2: "Doubt", 1: "To be assessed"}.get(stage, "To be assessed")
    rows = []
    if story.get("diagnosis"):
        rows.append(("DIAGNOSIS", "#ff8c8c", str(story["diagnosis"]), ""))
    rows.append(("AVAILABILITY", "#ff8c8c", avail, ""))
    rows.append(("TIMELINE", "#ff8c8c", story.get("expected_return") or "Awaiting update", ""))
    if story.get("next_match"):
        rows.append(("NEXT MATCH", "#ff8c8c", str(story["next_match"]), ""))

    source_text = " · ".join(f"@{s}" for s in sources[:2])
    html = _build_card_html(player_name, "INJURY UPDATE", "#d2261e", club_color,
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
        logo_path = Path("Logo.png")
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
