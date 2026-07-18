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
    f = get_premium_font(48, "Black")
    # FPL in white with glow
    _draw_text_shadow(draw, (x, y), "FPL", f, (255, 255, 255),
                      shadow=(255, 255, 255), offset=0)
    _draw_text_shadow(draw, (x, y), "FPL", f, (255, 255, 255), offset=2)
    fpl_w = draw.textlength("FPL ", font=f)
    # VORTEX in bright green
    _draw_text_shadow(draw, (x + fpl_w, y), "VORTEX", f, (84, 224, 124),
                      shadow=(0, 120, 80), offset=2)

def get_club_color(club_key):
    color_tuple = CLUB_COLORS.get(club_key, (84, 224, 124)) # Default to VORTEX Green
    return f"rgb({color_tuple[0]}, {color_tuple[1]}, {color_tuple[2]})"

def _render_html_sync(html_content, filename, error_box=None):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1380, "height": 776}, device_scale_factor=1)
            page.set_content(html_content, wait_until="domcontentloaded")
            page.evaluate("document.fonts.ready.then(() => true)")
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
            return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
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


def _pl_logo_uri() -> str:
    """Download and cache the Premier League logo as a data-URI."""
    pl_path = Path("logos/PremierLeague.png")
    if not pl_path.exists():
        _download_asset(
            "https://resources.premierleague.com/premierleague/photos/pl-main-logo.png",
            pl_path
        )
    return _data_uri(pl_path)


def _wikimedia_photo_uri(player_name: str) -> str:
    """Fetch a Creative Commons player photo from Wikimedia Commons.

    Used as a fallback when the FPL API has no photo (e.g. brand-new signings).
    Images are cached locally so we only hit Wikipedia once per player.
    Returns '' on any failure — callers are never blocked.
    """
    if not player_name:
        return ""
    cache_key = hashlib.md5(player_name.lower().encode()).hexdigest()[:12]
    cache_path = Path(f"players/wiki_{cache_key}.png")
    # Negative-cache sentinel: a tiny file means we tried and found nothing
    if cache_path.exists():
        return _data_uri(cache_path) if cache_path.stat().st_size >= 500 else ""

    try:
        params = urllib.parse.urlencode({
            "action": "query",
            "titles": player_name,
            "prop": "pageimages",
            "pithumbsize": 300,
            "format": "json",
            "redirects": 1,
        })
        url = f"https://en.wikipedia.org/w/api.php?{params}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "FPLVortexBot/1.0 (football automation; non-commercial)"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        pages = data.get("query", {}).get("pages", {})
        img_url = next(
            (p.get("thumbnail", {}).get("source", "")
             for p in pages.values()
             if p.get("thumbnail")),
            "",
        )
        if img_url and _download_asset(img_url, cache_path):
            print(f"[WIKI] photo found for {player_name!r}")
            return _data_uri(cache_path)
    except Exception as exc:
        print(f"[WIKI] photo lookup failed for {player_name!r}: {exc}")

    # Write a tiny sentinel so we don't retry on every run
    cache_path.write_bytes(b"x")
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
        mp = Path("players/tw_" + hashlib.md5(murl.encode()).hexdigest()[:12] + ".png")
        if not mp.exists():
            _download_asset(murl, mp)
        photo_uri = _data_uri(mp)
    # Wikimedia Commons fallback: used for players not yet in the FPL photo DB
    # (brand-new signings, foreign arrivals). CC-licensed images only.
    if not photo_uri:
        photo_uri = _wikimedia_photo_uri(player_name)

    return player_el, player_name, logo_uri, photo_uri


def _render_card(html_content, filename) -> bool:
    """Render HTML to PNG via the threaded Playwright helper. Returns True on success."""
    try:
        import threading
        error_box = []
        t = threading.Thread(target=_render_html_sync, args=(html_content, filename, error_box))
        t.start()
        t.join()
        if error_box:
            print("  [THREAD TRACEBACK]\n" + error_box[0])
        if Path(filename).exists() and Path(filename).stat().st_size >= 1000:
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
    logo_html = (f'<img src="{logo_uri}" style="width:100px;height:100px;object-fit:contain;'
                 f'margin-right:20px;flex-shrink:0;filter:drop-shadow(0 3px 10px rgba(0,0,0,0.7));" />') if logo_uri else ''
    crest_badge_html = f'<img class="crest-badge" src="{crest_uri}" />' if crest_uri else ''

    # Premier League logo in top-left of photo panel (replaces national team flag)
    pl_uri = _pl_logo_uri()
    pl_badge_html = f'<img class="pl-badge" src="{pl_uri}" />' if pl_uri else ''

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
        @import url('https://fonts.googleapis.com/css2?family=Montserrat:ital,wght@0,700;0,900;1,900&display=swap');
        body {{ margin:0; padding:0; width:1380px; height:776px; background:linear-gradient(135deg,#0b1220 0%,#1c2846 100%); font-family:'Montserrat',sans-serif; color:white; display:flex; overflow:hidden; position:relative; }}
        .accent-slash {{ position:absolute; width:200%; height:100px; background:{club_color}; opacity:0.15; transform:rotate(-35deg) translateY(-200px); z-index:0; }}
        .accent-slash:nth-child(2) {{ transform:rotate(-35deg) translateY(200px); opacity:0.05; }}
        .container {{ width:100%; height:100%; display:flex; flex-direction:row; padding:40px 60px 80px 60px; box-sizing:border-box; z-index:1; }}
        .left-column {{ flex:1; min-width:0; display:flex; flex-direction:column; justify-content:flex-start; padding-top:30px; }}
        .right-column {{ width:420px; flex-shrink:0; display:flex; align-items:center; justify-content:flex-end; }}

        /* ── Artistic FPL VORTEX wordmark ── */
        .wordmark {{ display:flex; align-items:center; margin-bottom:20px; min-height:108px; }}
        .brand-text {{ display:flex; align-items:baseline; gap:0; line-height:1; }}
        .brand-fpl {{
            font-size:56px; font-weight:900; letter-spacing:5px;
            color:#ffffff;
            text-shadow:0 0 30px rgba(255,255,255,0.45), 0 0 60px rgba(255,255,255,0.15), 0 4px 14px rgba(0,0,0,0.7);
        }}
        .brand-vortex {{
            font-size:56px; font-weight:900; letter-spacing:5px; margin-left:10px;
            background:linear-gradient(90deg,#54e07c 0%,#00ffb3 45%,#00d4ff 100%);
            -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text;
            filter:drop-shadow(0 0 14px rgba(84,224,124,0.65)) drop-shadow(0 0 28px rgba(0,212,255,0.35));
        }}

        .status-badge {{ display:inline-block; background:{badge_color}; color:#fff; padding:14px 30px; font-size:42px; font-weight:900; border-radius:12px; letter-spacing:3px; margin-bottom:20px; text-transform:uppercase; box-shadow:0 8px 20px rgba(0,0,0,0.4); }}
        .player-name {{ font-size:88px; font-weight:900; line-height:1.0; text-transform:uppercase; margin-bottom:28px; text-shadow:0 8px 20px rgba(0,0,0,0.6); white-space:nowrap; max-width:100%; }}
        .details-grid {{ display:grid; grid-template-columns:max-content 1fr; gap:18px 40px; font-size:44px; align-items:center; }}
        .detail-label {{ font-weight:700; text-transform:uppercase; }}
        .detail-value {{ font-weight:900; text-transform:uppercase; color:white; display:flex; align-items:center; }}
        .photo-panel {{ width:370px; height:560px; background:rgba(255,255,255,0.03); border:2px solid rgba(255,255,255,0.1); border-radius:24px; display:flex; align-items:center; justify-content:center; box-shadow:0 20px 50px rgba(0,0,0,0.5); position:relative; overflow:hidden; }}
        .photo-panel::before {{ content:''; position:absolute; top:0; left:0; right:0; bottom:0; background:radial-gradient(circle at center,{club_color} 0%,transparent 70%); opacity:0.2; z-index:0; }}
        /* Club crest — top-right of photo panel */
        .crest-badge {{ position:absolute; top:18px; right:18px; width:110px; height:110px; object-fit:contain; z-index:2; filter:drop-shadow(0 4px 8px rgba(0,0,0,0.6)); }}
        /* Premier League logo — top-left of photo panel (replaces national team flag) */
        .pl-badge {{ position:absolute; top:18px; left:18px; width:72px; height:72px; object-fit:contain; z-index:2; filter:drop-shadow(0 4px 8px rgba(0,0,0,0.8)); }}
        .footer {{ position:absolute; bottom:0; left:0; width:100%; height:65px; background:#141821; display:flex; align-items:center; justify-content:space-between; padding:0 60px; box-sizing:border-box; font-size:24px; font-weight:700; color:#bec8dc; border-top:4px solid {club_color}; }}
    </style></head><body>
        <div class="accent-slash"></div><div class="accent-slash"></div>
        <div class="container">
            <div class="left-column">
                <div class="wordmark">
                    {logo_html}
                    <div class="brand-text">
                        <span class="brand-fpl">FPL</span><span class="brand-vortex">VORTEX</span>
                    </div>
                </div>
                <div><div class="status-badge">{status}</div></div>
                <div class="player-name">{player_name}</div>
                <div class="details-grid">{rows_html}</div>
            </div>
            <div class="right-column">
                <div class="photo-panel">
                    {pl_badge_html}
                    {crest_badge_html}
                    {photo_img_html}
                </div>
            </div>
        </div>
        <div class="footer"><div>Source: {source_text} | @FPLVortex</div><div style="color:#d4af37;">{footer_tag}</div></div>
        <script>
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
        fee_value = story.get("fee") or "TBD"   # matches the tweet body
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
