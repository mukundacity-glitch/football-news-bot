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
    # query the Wikipedia REST summary API for the player's lead image.
    # This covers players not in the FPL dataset (foreign signings, new arrivals).
    if not photo_uri:
        pname = story.get("player", "")
        if pname:
            try:
                wiki_api = ("https://en.wikipedia.org/api/rest_v1/page/summary/"
                            + urllib.parse.quote(pname.replace(" ", "_")))
                req = urllib.request.Request(
                    wiki_api,
                    headers={"User-Agent": "FPLVortexBot/1.0 (football-news-bot)"}
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    wdata = json.loads(resp.read().decode("utf-8"))
                thumb = wdata.get("thumbnail", {}).get("source", "")
                if thumb:
                    wp = Path("players/wiki_" + hashlib.md5(pname.encode()).hexdigest()[:12] + ".jpg")
                    if not wp.exists():
                        _download_asset(thumb, wp)
                    if wp.exists() and wp.stat().st_size > 500:
                        photo_uri = _data_uri(wp)
                        print(f"  [PHOTO] Wikipedia image found for {pname!r}")
            except Exception as _we:
                print(f"  [PHOTO] Wikipedia lookup failed for {pname!r}: {_we}")

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
        value = float(player_el.get("now_cost")) / 10
        return f"£{value:.1f}M"
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
            age = today.year - year - ((today.month, today.day) < (month, day))
            return str(age)
        except Exception:
            pass
    return ""


def _optional_nationality(player_el, facts):
    for key in ("nationality", "country", "country_name", "region", "region_name"):
        val = facts.get(key) or (player_el or {}).get(key)
        if val:
            return str(val).upper()
    return ""


def _display_name_parts(name: str):
    parts = [p for p in str(name or "PLAYER").upper().split() if p]
    if len(parts) <= 1:
        return "", parts[0] if parts else "PLAYER"
    return " ".join(parts[:-1]), parts[-1]


def _build_info_rows(player_el, fpl_data, facts, *, event):
    rows = []
    age = _optional_age(player_el, facts)
    nat = _optional_nationality(player_el, facts)
    pos = _fpl_position(player_el, fpl_data) or str(facts.get("position") or "").upper()
    price = _fpl_price(player_el)
    if age:
        rows.append(("◌", "AGE", age))
    if nat:
        rows.append(("◉", "NATIONALITY", nat))
    if pos:
        rows.append(("▣", "POSITION", pos))
    if price:
        rows.append(("£", "FPL PRICE", price))
    if event == "INJURY" and facts.get("injury_status"):
        rows.insert(0, ("✚", "INJURY", str(facts["injury_status"]).upper()))
    if event == "SUSPENSION" and facts.get("suspension_status"):
        rows.insert(0, ("!", "SUSPENSION", str(facts["suspension_status"]).upper()))
    return rows[:4]


def _rows_html(rows):
    from html import escape
    return "".join(
        '<div class="info-row">'
        f'<div class="icon">{escape(str(icon))}</div>'
        f'<div class="label">{escape(str(label))}</div>'
        f'<div class="value">{escape(str(value))}</div>'
        '</div>'
        for icon, label, value in rows[:4]
    )


def _build_premium_card_html(
    *,
    event,
    player_name,
    logo_uri,
    photo_uri,
    source_text,
    origin_name="",
    origin_code="",
    origin_crest="",
    destination_name="",
    destination_code="",
    destination_crest="",
    club_name="",
    club_code="",
    club_crest="",
    rows=None,
    status_label="",
):
    from html import escape
    event = str(event).upper()
    rows = rows or []
    is_injury = event == "INJURY"
    is_suspension = event == "SUSPENSION"
    accent = "#FF3045" if is_injury else "#00FF66"
    accent2 = "#FF4B5E" if is_injury else "#7CFF00"
    accent_rgb = "255,48,69" if is_injury else "0,255,102"
    badge_text = status_label or ("✚ INJURY" if is_injury else ("SUSPENSION" if is_suspension else "CONFIRMED"))
    status_value = "OFFICIALLY CONFIRMED"
    if badge_text == "MEDICAL":
        status_value = "MEDICAL BOOKED"
    elif badge_text == "DEAL AGREED":
        status_value = "DEAL AGREED"
    badge_fg = "#FFFFFF" if is_injury else "#00FF66"
    first, surname = _display_name_parts(player_name)
    logo_html = f'<img src="{logo_uri}" class="brand-logo" />' if logo_uri else ''
    player_visual = f'<img src="{photo_uri}" class="player-img" />' if photo_uri else '<div class="player-fallback">FPL</div>'

    def club_block(kind, name, code, crest):
        if not name and not crest:
            return ""
        crest_html = f'<img src="{crest}" />' if crest else '<div class="crest-fallback">CLUB</div>'
        return f"""
        <div class="club-block">
          <div class="club-heading">{escape(kind)}</div>
          <div class="club-logo">{crest_html}</div>
          <div class="club-name">{escape(name or code)}</div>
        </div>"""

    if event == "TRANSFER":
        from_block = club_block("FROM", origin_name, origin_code, origin_crest)
        to_block = club_block("TO", destination_name, destination_code, destination_crest)
        if from_block and to_block:
            transfer_html = f'<div class="transfer-panel">{from_block}<div class="transfer-arrow">➜</div>{to_block}</div>'
        elif to_block:
            transfer_html = f'<div class="transfer-panel single-transfer">{to_block}</div>'
        else:
            transfer_html = '<div class="transfer-panel single-transfer"><div class="confirmed-only">OFFICIALLY CONFIRMED</div></div>'
    else:
        transfer_html = f"""<div class="transfer-panel single-transfer">
            {club_block("CLUB", club_name, club_code, club_crest)}
        </div>"""

    position_text = next((v for _i, l, v in rows if l == "POSITION"), "PREMIER LEAGUE")
    return f"""<!doctype html><html><head><meta charset="utf-8" />
    <style>
      * {{ box-sizing:border-box; }}
      html,body {{ margin:0; width:1920px; height:1080px; overflow:hidden; background:#060606; }}
      body {{ font-family: Montserrat, DejaVu Sans, Arial, sans-serif; color:#fff; position:relative;
        background: radial-gradient(circle at 35% 45%, rgba({accent_rgb},.35), transparent 27%), #060606; }}
      .vortex {{ position:absolute; left:160px; top:110px; width:820px; height:820px; border-radius:50%;
        background: repeating-conic-gradient(from 20deg, rgba({accent_rgb},.38) 0 7deg, transparent 7deg 19deg);
        filter:blur(8px); opacity:.72; transform:rotate(-18deg); }}
      .vortex::after {{ content:""; position:absolute; inset:78px; border-radius:50%; background:#060606; filter:blur(25px); opacity:.72; }}
      .particles {{ position:absolute; inset:0; background:
        radial-gradient(circle at 18% 24%, #FFD54F 0 2px, transparent 3px),
        radial-gradient(circle at 42% 16%, rgba({accent_rgb},.8) 0 2px, transparent 3px),
        radial-gradient(circle at 68% 34%, rgba({accent_rgb},.45) 0 2px, transparent 3px),
        radial-gradient(circle at 28% 78%, #FFD54F 0 2px, transparent 3px); opacity:.9; }}
      .card-border {{ position:absolute; inset:18px; border:2px solid rgba({accent_rgb},.72); border-radius:28px; box-shadow:0 0 22px rgba({accent_rgb},.55); }}
      .brand {{ position:absolute; left:45px; top:35px; height:110px; display:flex; align-items:center; gap:25px; z-index:5; }}
      .brand-logo {{ width:110px; height:110px; object-fit:contain; filter:drop-shadow(0 0 12px rgba({accent_rgb},.55)); }}
      .brand-word {{ font-weight:950; font-style:italic; font-size:64px; line-height:.82; letter-spacing:-2px; }}
      .brand-word span {{ color:{accent}; text-shadow:0 0 8px rgba({accent_rgb},.85); }}
      .subtitle {{ font-size:18px; letter-spacing:6px; color:#D0D0D0; margin-top:12px; }}
      .badge {{ position:absolute; right:50px; top:35px; width:250px; height:90px; border:3px solid {accent}; border-radius:18px;
        display:flex; align-items:center; justify-content:center; color:{badge_fg}; font-size:48px; font-weight:950; font-style:italic; background:rgba(0,50,25,.62); box-shadow:0 0 20px rgba({accent_rgb},.85); }}
      .player-wrap {{ position:absolute; left:40px; bottom:190px; width:910px; height:820px; z-index:3; display:flex; align-items:flex-end; justify-content:center; }}
      .player-img {{ max-height:820px; max-width:900px; object-fit:contain; filter:drop-shadow(0 25px 22px rgba(0,0,0,.72)) drop-shadow(0 0 18px rgba({accent_rgb},.45)); }}
      .player-fallback {{ font-size:120px; color:rgba(255,255,255,.2); font-weight:950; margin-bottom:300px; }}
      .player-name {{ position:absolute; left:70px; bottom:92px; width:900px; z-index:4; }}
      .first-name {{ font-size:40px; font-weight:700; font-style:italic; color:#fff; text-shadow:0 3px 8px #000; }}
      .surname {{ font-size:108px; line-height:.86; font-weight:950; font-style:italic; color:{accent2}; text-shadow:0 0 12px rgba({accent_rgb},.9), 0 4px 12px #000; }}
      .position-bar {{ display:inline-flex; align-items:center; justify-content:center; min-width:560px; height:65px; padding:0 32px; border:2px solid {accent}; background:#080808; transform:skewX(-8deg); margin-left:40px; margin-top:8px; }}
      .position-bar span {{ transform:skewX(8deg); font-size:34px; font-weight:900; letter-spacing:4px; color:#fff; }}
      .right-panel {{ position:absolute; left:1000px; top:155px; width:820px; z-index:4; }}
      .transfer-panel {{ display:flex; align-items:center; justify-content:center; gap:42px; min-height:270px; margin-bottom:22px; }}
      .single-transfer {{ justify-content:center; }}
      .club-block {{ width:240px; text-align:center; }}
      .club-heading {{ font-size:34px; font-weight:950; margin-bottom:8px; }}
      .club-logo {{ width:220px; height:220px; margin:0 auto; border:2px solid {accent}; border-radius:18px; display:flex; align-items:center; justify-content:center; background:rgba(16,16,16,.72); box-shadow:0 0 15px rgba({accent_rgb},.55); }}
      .club-logo img {{ max-width:150px; max-height:150px; object-fit:contain; filter:drop-shadow(0 0 10px rgba({accent_rgb},.45)); }}
      .crest-fallback {{ color:#fff; font-size:28px; font-weight:900; opacity:.6; }}
      .club-name {{ font-size:28px; font-weight:950; margin-top:8px; color:#fff; }}
      .transfer-arrow {{ font-size:90px; color:{accent}; filter:drop-shadow(0 0 15px rgba({accent_rgb},.9)); }}
      .confirmed-only {{ width:520px; height:130px; border:3px solid {accent}; border-radius:20px; display:flex; align-items:center; justify-content:center; color:{accent}; font-size:54px; font-weight:950; box-shadow:0 0 20px rgba({accent_rgb},.6); }}
      .info {{ margin-top:12px; border-top:1px solid rgba({accent_rgb},.28); }}
      .info-row {{ height:95px; display:grid; grid-template-columns:74px 205px 1fr; align-items:center; gap:18px; border-bottom:1px solid rgba({accent_rgb},.28); }}
      .icon {{ width:46px; height:46px; border:3px solid {accent}; color:{accent}; display:flex; align-items:center; justify-content:center; font-size:29px; margin-left:10px; box-shadow:0 0 8px rgba({accent_rgb},.55); }}
      .label {{ color:#D0D0D0; font-size:24px; font-weight:600; }}
      .value {{ color:#fff; font-size:34px; font-weight:950; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
      .status-bar {{ margin-top:20px; height:120px; width:100%; border:3px solid {accent}; border-radius:20px; box-shadow:0 0 18px rgba({accent_rgb},.7); display:flex; align-items:center; padding:0 34px; background:rgba(16,16,16,.82); }}
      .tick {{ width:60px; height:60px; border-radius:50%; background:{accent}; color:#060606; display:flex; align-items:center; justify-content:center; font-size:42px; font-weight:950; margin-right:26px; }}
      .status-label {{ color:#fff; font-size:36px; font-weight:950; margin-right:18px; }}
      .status-value {{ color:{accent}; font-size:60px; font-weight:950; font-style:italic; text-shadow:0 0 10px rgba({accent_rgb},.9); }}
      .footer {{ position:absolute; left:0; right:0; bottom:0; height:85px; border-top:2px solid {accent}; display:grid; grid-template-columns:1fr 1fr 1fr; background:#060606; z-index:6; }}
      .foot {{ display:flex; align-items:center; gap:18px; padding:0 58px; font-size:28px; font-weight:900; color:#fff; }}
      .foot:nth-child(2), .foot:nth-child(3) {{ border-left:1px solid rgba({accent_rgb},.45); }}
      .foot-icon {{ color:{accent}; font-size:36px; }}
      .yt {{ width:52px; height:36px; background:#FF0000; border-radius:8px; position:relative; }}
      .yt::after {{ content:""; position:absolute; left:21px; top:9px; border-top:9px solid transparent; border-bottom:9px solid transparent; border-left:14px solid #fff; }}
      .x {{ color:#fff; font-size:42px; }}
    </style></head><body>
      <div class="vortex"></div><div class="particles"></div><div class="card-border"></div>
      <div class="brand">{logo_html}<div><div class="brand-word">FPL<span>.VORTEX</span></div><div class="subtitle">YOUR FPL EDGE</div></div></div>
      <div class="badge">{escape(badge_text)}</div>
      <div class="player-wrap">{player_visual}</div>
      <div class="player-name"><div class="first-name">{escape(first)}</div><div class="surname">{escape(surname)}</div><div class="position-bar"><span>{escape(position_text)}</span></div></div>
      <div class="right-panel">{transfer_html}<div class="info">{_rows_html(rows)}</div><div class="status-bar"><div class="tick">✓</div><div class="status-label">STATUS:</div><div class="status-value">{escape(status_value)}</div></div></div>
      <div class="footer"><div class="foot"><span class="foot-icon">◎</span><span>SOURCE: {escape(source_text.upper())}</span></div><div class="foot"><span class="yt"></span><span>@FPLVORTEX</span></div><div class="foot"><span class="x">𝕏</span><span>@FPLVORTEX</span></div></div>
    </body></html>"""


def create_verified_branded_card(event, subject, facts, source_handles, filename):
    """Render the premium FPL VORTEX broadcast card from verified facts only.

    Robust rule: never display a blank/fake FROM club. Use verified facts first,
    then the official FPL current club for the player, otherwise omit the FROM
    block instead of guessing.
    """
    event = str(event).upper()
    subject = str(subject or "OFFICIAL UPDATE")
    fpl = fetch_fpl_data()
    player_el = find_player_in_fpl(subject, fpl) if fpl else None
    player_name = (player_el.get("web_name") if player_el else subject) or subject
    logo_uri = _data_uri(Path("Logo.png"))
    seed_club = str(facts.get("club_to_name") or facts.get("club_name") or facts.get("club_from_name") or "")
    seed_key = _verified_club_key(seed_club)
    _, player_name, _logo_from_assets, photo_uri = _img_assets({"player": subject, "to_key": seed_key, "from_key": seed_key})
    logo_uri = logo_uri or _logo_from_assets
    source_text = " · ".join("@" + str(h).lstrip("@") for h in source_handles[:2]) or "OFFICIAL SOURCE"

    if event == "TRANSFER":
        destination = str(facts.get("club_to_name") or "")
        destination_key = _verified_club_key(destination)
        origin = str(facts.get("club_from_name") or "")
        origin_key = _verified_club_key(origin) if origin else ""
        origin_code = _club_code_from_key(origin_key, origin)
        if not origin:
            fpl_key, fpl_name, fpl_code = _fpl_team_for_player(player_el, fpl)
            if fpl_name and _verified_club_key(fpl_name) != destination_key:
                origin, origin_key, origin_code = fpl_name, fpl_key, fpl_code
        rows = _build_info_rows(player_el, fpl, facts, event=event)
        html = _build_premium_card_html(
            event=event,
            player_name=player_name,
            logo_uri=logo_uri,
            photo_uri=photo_uri,
            source_text=source_text,
            origin_name=origin,
            origin_code=origin_code,
            origin_crest=_crest_uri(origin_key) if origin_key else "",
            destination_name=destination,
            destination_code=_club_code_from_key(destination_key, destination),
            destination_crest=_crest_uri(destination_key),
            rows=rows,
            status_label=("MEDICAL" if str(facts.get("_event_status")) == "MEDICAL" else "DEAL AGREED" if str(facts.get("_event_status")) in {"AGREEMENT", "HERE_WE_GO"} else "CONFIRMED"),
        )
    else:
        club_name = str(facts.get("club_name") or "")
        club_key = _verified_club_key(club_name)
        rows = _build_info_rows(player_el, fpl, facts, event=event)
        html = _build_premium_card_html(
            event=event,
            player_name=player_name,
            logo_uri=logo_uri,
            photo_uri=photo_uri,
            source_text=source_text,
            club_name=club_name,
            club_code=_club_code_from_key(club_key, club_name),
            club_crest=_crest_uri(club_key),
            rows=rows,
        )
    return _render_card(html, filename, width=1920, height=1080)


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
