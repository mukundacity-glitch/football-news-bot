"""
FPL VORTEX — Graphics Engine
Handles generation of cinematic transfer and injury cards via PIL and Playwright.
"""

import os
import re
import base64
import hashlib
import urllib.request
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

def _render_html_sync(html_content, filename, error_box=None):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1380, "height": 776}, device_scale_factor=1)
            page.set_content(html_content, wait_until="domcontentloaded")
            page.wait_for_timeout(500)
            page.screenshot(path=filename)
            browser.close()
    except Exception:
        if error_box is not None:
            import traceback
            error_box.append(traceback.format_exc())

def create_transfer_image(story, sources, filename, collapsed=False):
    fpl = fetch_fpl_data()
    player_el = find_player_in_fpl(story.get("player"), fpl)
    player_name = (player_el["web_name"] if player_el else story.get("player")) or "PLAYER"

    to_club = story.get("to_club") or (story.get("to_key") or "").replace("_", " ")
    from_club = story.get("from_club") or (story.get("from_key") or "").replace("_", " ")

    mode = story.get("mode", "confirmed")
    if collapsed or story.get("collapsed"):
        status = "DEAL COLLAPSED"
        badge_color = "#e31e24"
    elif mode == "rumour":
        status = "TRANSFER RUMOUR"
        badge_color = "#e31e24"
    else:
        status = "OFFICIAL" if story.get("stage", 1) >= 4 else "CONFIRMED"
        badge_color = "#54e07c"

    club_color = get_club_color(story.get("to_key") or story.get("from_key"))
    source_text = " · ".join(f"@{s}" for s in sources[:2])

    logo_data_uri = ""
    logo_path = Path("Logo.png")
    if logo_path.exists() and logo_path.stat().st_size >= 500:
        logo_data_uri = "data:image/png;base64," + base64.b64encode(logo_path.read_bytes()).decode("ascii")

    photo_data_uri = None
    pid = player_el.get("code") if player_el else None
    if pid:
        pp = Path(f"players/{pid}.png")
        if not pp.exists():
            _download_asset(f"https://resources.premierleague.com/premierleague/photos/players/250x250/p{pid}.png", pp)
        if pp.exists() and pp.stat().st_size >= 500:
            photo_data_uri = "data:image/png;base64," + base64.b64encode(pp.read_bytes()).decode("ascii")

    if not photo_data_uri and story.get("media_url"):
        murl = story["media_url"]
        mp = Path("players/tw_" + hashlib.md5(murl.encode()).hexdigest()[:12] + ".png")
        if not mp.exists():
            _download_asset(murl, mp)
        if mp.exists() and mp.stat().st_size >= 500:
            photo_data_uri = "data:image/png;base64," + base64.b64encode(mp.read_bytes()).decode("ascii")

    def _crest_uri(club_key):
        if not club_key: return ""
        safe = club_key.replace(" ", "_").replace("'", "")
        cp = Path(f"logos/{safe}.png")
        if not cp.exists() and FPL_LOGO_IDS.get(safe):
            _download_asset(f"https://resources.premierleague.com/premierleague/badges/t{FPL_LOGO_IDS[safe]}.png", cp)
        if cp.exists() and cp.stat().st_size >= 500:
            return "data:image/png;base64," + base64.b64encode(cp.read_bytes()).decode("ascii")
        return ""

    main_crest_uri = _crest_uri(story.get("to_key") or story.get("from_key"))
    from_crest_uri = _crest_uri(story.get("from_key"))
    to_crest_uri = _crest_uri(story.get("to_key"))

    crest_img_html = f'<img class="crest-badge" src="{main_crest_uri}" />' if main_crest_uri else ''
    if photo_data_uri:
        photo_img_html = f'<img src="{photo_data_uri}" style="width:100%;height:100%;object-fit:cover;position:relative;z-index:1;" />'
    else:
        if main_crest_uri:
            photo_img_html = f'<img src="{main_crest_uri}" style="width:70%;height:70%;object-fit:contain;position:relative;z-index:1;opacity:0.85;" />'
        else:
            photo_img_html = f'<div style="z-index:1;font-size:150px;color:rgba(255,255,255,0.15);font-weight:900;">V</div>'

    def _club_with_crest(name, crest_uri):
        if not name: return "TBD"
        if crest_uri:
            return f'{name} <img src="{crest_uri}" style="width:52px;height:52px;object-fit:contain;vertical-align:middle;margin-left:10px;filter:drop-shadow(0 2px 4px rgba(0,0,0,0.5));" />'
        return name

    from_html = _club_with_crest(from_club or "TBD", from_crest_uri)
    to_html = _club_with_crest(to_club or "TBD", to_crest_uri)
    fee_value = story.get('fee') or "Undisclosed"
    if fee_value and fee_value != "Undisclosed" and not fee_value.startswith("$"):
        fee_value = "$" + fee_value

    logo_html = f'<img src="{logo_data_uri}" style="width:64px;height:64px;object-fit:contain;margin-right:16px;filter:drop-shadow(0 2px 6px rgba(0,0,0,0.6));" />' if logo_data_uri else ''

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@700;900&display=swap');
            body {{ margin: 0; padding: 0; width: 1380px; height: 776px; background: linear-gradient(135deg, #0b1220 0%, #1c2846 100%); font-family: 'Montserrat', sans-serif; color: white; display: flex; overflow: hidden; position: relative; }}
            .accent-slash {{ position: absolute; width: 200%; height: 100px; background: {club_color}; opacity: 0.15; transform: rotate(-35deg) translateY(-200px); z-index: 0; }}
            .accent-slash:nth-child(2) {{ transform: rotate(-35deg) translateY(200px); opacity: 0.05; }}
            .container {{ width: 100%; height: 100%; display: flex; flex-direction: row; padding: 40px 60px 80px 60px; box-sizing: border-box; z-index: 1; }}
            .left-column {{ flex: 1; display: flex; flex-direction: column; justify-content: flex-start; padding-top: 30px; }}
            .right-column {{ width: 420px; display: flex; align-items: center; justify-content: flex-end; }}
            .wordmark {{ font-size: 52px; font-weight: 900; margin-bottom: 24px; text-shadow: 0 4px 10px rgba(0,0,0,0.5); display: flex; align-items: center; }}
            .wordmark span {{ color: #54e07c; margin-left: 10px; }}
            .status-badge {{ display: inline-block; background: {badge_color}; color: #fff; padding: 14px 30px; font-size: 42px; font-weight: 900; border-radius: 12px; letter-spacing: 3px; margin-bottom: 20px; text-transform: uppercase; box-shadow: 0 8px 20px rgba(0,0,0,0.4); }}
            .player-name {{ font-size: 88px; font-weight: 900; line-height: 1.0; text-transform: uppercase; margin-bottom: 28px; text-shadow: 0 8px 20px rgba(0,0,0,0.6); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 820px; }}
            .details-grid {{ display: grid; grid-template-columns: 130px 1fr; gap: 18px 30px; font-size: 44px; }}
            .detail-label {{ font-weight: 700; text-transform: uppercase; }}
            .detail-label.from {{ color: #f5c518; }}
            .detail-label.to {{ color: #00d4ff; }}
            .detail-label.fee {{ color: #e31e24; }}
            .detail-value {{ font-weight: 900; text-transform: uppercase; color: white; display: flex; align-items: center; }}
            .photo-panel {{ width: 370px; height: 560px; background: rgba(255, 255, 255, 0.03); backdrop-filter: blur(20px); border: 2px solid rgba(255, 255, 255, 0.1); border-radius: 24px; display: flex; align-items: center; justify-content: center; box-shadow: 0 20px 50px rgba(0,0,0,0.5); position: relative; overflow: hidden; }}
            .crest-badge {{ position: absolute; top: 16px; right: 16px; width: 75px; height: 75px; z-index: 2; filter: drop-shadow(0 4px 8px rgba(0,0,0,0.5)); }}
            .photo-panel::before {{ content: ''; position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: radial-gradient(circle at center, {club_color} 0%, transparent 70%); opacity: 0.2; z-index: 0; }}
            .footer {{ position: absolute; bottom: 0; left: 0; width: 100%; height: 65px; background: #141821; display: flex; align-items: center; justify-content: space-between; padding: 0 60px; box-sizing: border-box; font-size: 24px; font-weight: 700; color: #bec8dc; border-top: 4px solid {club_color}; }}
        </style>
    </head>
    <body>
        <div class="accent-slash"></div>
        <div class="accent-slash"></div>
        <div class="container">
            <div class="left-column">
                <div class="wordmark">{logo_html}FPL<span>VORTEX</span></div>
                <div><div class="status-badge">{status}</div></div>
                <div class="player-name">{player_name}</div>
                <div class="details-grid">
                    <div class="detail-label from">FROM</div>
                    <div class="detail-value">{from_html}</div>
                    <div class="detail-label to">TO</div>
                    <div class="detail-value">{to_html}</div>
                    <div class="detail-label fee">FEE</div>
                    <div class="detail-value" style="color:#54e07c;">{fee_value}</div>
                </div>
            </div>
            <div class="right-column">
                <div class="photo-panel">
                    {crest_img_html}
                    {photo_img_html}
                </div>
            </div>
        </div>
        <div class="footer">
            <div>Source: {source_text} | @FPLVortex</div>
            <div style="color: #d4af37;">{story.get('event', 'TRANSFER').upper()}</div>
        </div>
        <script>
            document.addEventListener("DOMContentLoaded", function() {{
                const nameEl = document.querySelector('.player-name');
                let fontSize = 88;
                while(nameEl.scrollWidth > nameEl.clientWidth && fontSize > 30) {{
                    fontSize--;
                    nameEl.style.fontSize = fontSize + 'px';
                }}
            }});
        </script>
    </body>
    </html>
    """

    try:
        import threading
        error_box = []
        t = threading.Thread(target=_render_html_sync, args=(html_content, filename, error_box))
        t.start()
        t.join()
        if error_box:
            print("  [THREAD TRACEBACK]\n" + error_box[0])
        if not Path(filename).exists() or Path(filename).stat().st_size < 1000:
            raise RuntimeError("Thread completed but image missing")
    except Exception as e:
        import traceback; traceback.print_exc()
        Image.new('RGB', (1380, 776), color=(11, 18, 32)).save(filename)

def create_injury_image(story, sources, filename):
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

    if not img_pasted and story.get("media_url"):
        murl = story["media_url"]
        mp = Path("players/tw_" + hashlib.md5(murl.encode()).hexdigest()[:12] + ".png")
        if not mp.exists():
            try: _download_asset(murl, mp)
            except Exception: pass
        if mp.exists() and mp.stat().st_size >= 500:
            t_img = _safe_open_rgba(mp)
            if t_img is not None:
                t_img = _fit_contain(t_img, 400, 500)
                img.paste(t_img, (right_center[0] - t_img.width // 2, right_center[1] - t_img.height // 2 + 30), t_img)
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
