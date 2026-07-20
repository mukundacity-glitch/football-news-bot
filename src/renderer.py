"""
FPL VORTEX — Graphics Engine
Handles generation of cinematic transfer and injury cards via PIL and Playwright.
"""

import os
import re
import base64
import hashlib
import urllib.request
from datetime import datetime, timezone
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
            page = browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
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


# ══════════════════════════════════════════════════════════════════════════
#  BROADCAST CARD ENGINE (v2) — 1920×1080 TV-style templates
#
#  One master frame (top red banner + FPL VORTEX wordmark + PL mark + footer)
#  drives SIX data-populated templates. Every value is a dynamic slot pulled
#  from the story dict, so the GitHub automation only changes DATA, never the
#  layout:
#     confirmed → CONFIRMED TRANSFER (green)   news    → TRANSFER NEWS (blue)
#     rumour    → STRONG RUMOUR (orange)       collapsed → DEAL COLLAPSED (red)
#     injury    → INJURY UPDATE (gold)         suspension → SUSPENSION (dark red)
#  (renewal/manager reuse the frame with their own accent + fields.)
# ══════════════════════════════════════════════════════════════════════════

_POS_MAP = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

# (accent, accent2, accent_lite, accent_border, glow) per template.
_BC_PALETTE = {
    "confirmed":  ("#18d16f", "#0a8f47", "#7ff0ad", "rgba(24,209,111,.55)", "rgba(24,209,111,.9)"),
    "news":       ("#2f8bff", "#155fce", "#8fc0ff", "rgba(47,139,255,.55)", "rgba(47,139,255,.9)"),
    "rumour":     ("#ff9016", "#d15e00", "#ffc48a", "rgba(255,144,22,.55)", "rgba(255,144,22,.9)"),
    "collapsed":  ("#ff3b3b", "#a71212", "#ff9d9d", "rgba(255,59,59,.55)",  "rgba(255,59,59,.9)"),
    "injury":     ("#ffc31c", "#c98f00", "#ffe08a", "rgba(255,195,28,.55)", "rgba(255,195,28,.9)"),
    "suspension": ("#e0202e", "#7c0a12", "#ff8f96", "rgba(224,32,46,.55)",  "rgba(224,32,46,.9)"),
    "renewal":    ("#18d16f", "#0a8f47", "#7ff0ad", "rgba(24,209,111,.55)", "rgba(24,209,111,.9)"),
    "manager":    ("#8a6bff", "#4b2fd1", "#c3b4ff", "rgba(138,107,255,.55)", "rgba(138,107,255,.9)"),
}
# (header_icon, header_text, footer_tag) per template. Badge text is derived.
_BC_HEAD = {
    "confirmed":  ("check", "CONFIRMED TRANSFER", "TRANSFER"),
    "news":       ("news",  "TRANSFER NEWS",      "TRANSFER"),
    "rumour":     ("fire",  "STRONG RUMOUR",      "RUMOUR"),
    "collapsed":  ("x",     "DEAL COLLAPSED",     "TRANSFER"),
    "injury":     ("cross", "INJURY UPDATE",      "INJURY"),
    "suspension": ("card",  "SUSPENSION",         "SUSPENSION"),
    "renewal":    ("check", "NEW CONTRACT",       "CONTRACT"),
    "manager":    ("news",  "MANAGER NEWS",       "MANAGER"),
}
_STAGE_TXT = {4: "HERE WE GO", 3: "ADVANCED", 2: "NEGOTIATING", 1: "EARLY TALKS"}

# Static CSS (plain string so literal { } need no escaping). Accent values are
# injected as CSS custom properties, so this block never changes per template.
_BC_CSS = """
*{box-sizing:border-box;}
body{margin:0;width:1920px;height:1080px;overflow:hidden;position:relative;color:#fff;
  font-family:'Montserrat','DejaVu Sans',sans-serif;
  background:radial-gradient(120% 95% at 80% 26%, rgba(38,58,108,.42) 0%, rgba(8,12,22,.98) 62%),
             linear-gradient(160deg,#0a0f1c 0%,#0c1424 55%,#080b14 100%);}
.bgglow{position:absolute;inset:0;z-index:0;opacity:.4;
  background:radial-gradient(58% 55% at 82% 46%, var(--glow) 0%, transparent 62%);}
.slash{position:absolute;left:-40%;width:240%;height:150px;transform:rotate(-30deg);z-index:0;}
.slash.a{top:150px;background:linear-gradient(90deg,transparent,var(--accent),transparent);opacity:.06;}
.slash.b{top:660px;background:linear-gradient(90deg,transparent,#fff,transparent);opacity:.03;}
.topbar-red{position:absolute;top:0;left:0;height:120px;width:63%;z-index:2;opacity:.94;
  clip-path:polygon(0 0,100% 0,87% 100%,0 100%);
  background:linear-gradient(100deg,#c40913 0%,#7c0009 68%,rgba(124,0,9,0) 100%);}
.brand{position:absolute;top:30px;left:60px;display:flex;align-items:center;gap:16px;z-index:6;}
.brand img{height:66px;width:66px;object-fit:contain;filter:drop-shadow(0 3px 8px rgba(0,0,0,.6));}
.brand .wm{font-size:50px;font-weight:800;font-style:italic;letter-spacing:1px;
  text-shadow:0 4px 10px rgba(0,0,0,.6);}
.brand .wm b{color:#fff;} .brand .wm i{color:#ff2d2d;}
.plmark{position:absolute;top:30px;right:60px;z-index:6;text-align:right;line-height:.92;
  font-weight:800;font-style:italic;color:#7ef0a6;text-shadow:0 3px 8px rgba(0,0,0,.6);}
.plmark .p1{font-size:22px;letter-spacing:3px;color:#e9f6ff;}
.plmark .p2{font-size:30px;letter-spacing:2px;}
.cat{position:absolute;top:150px;left:60px;height:66px;display:flex;align-items:center;gap:16px;
  padding:0 48px 0 22px;z-index:5;color:#fff;font-size:40px;font-weight:800;font-style:italic;
  letter-spacing:1px;text-transform:uppercase;box-shadow:0 10px 26px rgba(0,0,0,.45);
  clip-path:polygon(0 0,100% 0,93% 100%,0 100%);
  background:linear-gradient(90deg,var(--accent) 0%,var(--accent2) 100%);}
.cat .ic{display:flex;align-items:center;justify-content:center;width:46px;height:46px;
  border-radius:50%;background:rgba(255,255,255,.22);}
.content{position:absolute;top:250px;left:60px;right:60px;bottom:170px;display:flex;z-index:4;}
.left{width:57%;padding-right:24px;display:flex;flex-direction:column;min-width:0;}
.right{width:43%;position:relative;}
.pname{font-size:86px;line-height:.92;font-weight:800;font-style:italic;text-transform:uppercase;
  letter-spacing:1px;margin:0 0 30px 0;text-shadow:0 6px 22px rgba(0,0,0,.7);}
.rows{display:flex;flex-direction:column;gap:15px;}
.row{display:flex;flex-direction:column;gap:1px;}
.row .lbl{font-size:25px;font-weight:800;letter-spacing:1.6px;text-transform:uppercase;color:var(--lite);}
.row .val{font-size:40px;font-weight:800;text-transform:uppercase;color:#fff;line-height:1.02;
  display:flex;align-items:center;gap:13px;}
.row .val img{height:44px;width:44px;object-fit:contain;filter:drop-shadow(0 2px 5px rgba(0,0,0,.6));}
.row.sm .val{font-size:33px;}
.confbar{display:flex;align-items:center;gap:14px;}
.confbar .track{display:flex;gap:5px;}
.confbar .seg{width:26px;height:22px;border-radius:3px;background:rgba(255,255,255,.14);}
.confbar .seg.on{background:linear-gradient(90deg,var(--accent),var(--accent2));
  box-shadow:0 0 10px var(--accent);}
.confbar .pct{font-size:34px;font-weight:800;}
.photoglow{position:absolute;right:70px;bottom:40px;width:520px;height:520px;border-radius:50%;z-index:1;
  background:radial-gradient(circle,var(--accent) 0%,transparent 62%);opacity:.26;filter:blur(4px);}
.photo{position:absolute;right:60px;bottom:-46px;height:648px;max-width:112%;object-fit:contain;z-index:2;
  filter:drop-shadow(0 18px 42px rgba(0,0,0,.65));}
.photoph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;z-index:1;
  font-size:220px;font-weight:800;font-style:italic;color:rgba(255,255,255,.07);}
.logos{position:absolute;top:-8px;right:0;z-index:3;display:flex;flex-direction:column;align-items:center;gap:12px;}
.logos .crest{width:104px;height:104px;object-fit:contain;background:rgba(255,255,255,.07);border-radius:50%;
  padding:9px;border:2px solid rgba(255,255,255,.18);filter:drop-shadow(0 4px 10px rgba(0,0,0,.6));}
.arrow{filter:drop-shadow(0 4px 12px var(--accent));}
.extra-plus{position:absolute;right:22px;bottom:118px;z-index:4;width:120px;height:120px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;border:3px solid rgba(255,255,255,.3);
  background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 10px 26px rgba(0,0,0,.5);}
.extra-card{position:absolute;right:26px;top:14px;z-index:4;width:118px;height:158px;border-radius:12px;
  transform:rotate(14deg);border:2px solid rgba(255,255,255,.2);
  background:linear-gradient(135deg,#ff2a2a,#a30d0d);box-shadow:0 14px 30px rgba(0,0,0,.55);}
.badge{position:absolute;right:0;bottom:2px;z-index:5;display:flex;align-items:center;gap:13px;
  padding:14px 30px;border-radius:14px;font-size:38px;font-weight:800;font-style:italic;
  text-transform:uppercase;letter-spacing:1px;color:#fff;border:2px solid rgba(255,255,255,.28);
  background:linear-gradient(90deg,var(--accent),var(--accent2));box-shadow:0 12px 30px rgba(0,0,0,.5);}
.badge .ic{display:flex;align-items:center;}
.stats{position:absolute;left:60px;bottom:88px;display:flex;z-index:5;overflow:hidden;border-radius:12px;
  border:2px solid var(--brd);background:rgba(9,14,25,.72);}
.stats .cell{padding:11px 26px;text-align:center;border-right:2px solid var(--brd);min-width:150px;}
.stats .cell:last-child{border-right:none;}
.stats .cl{font-size:21px;font-weight:800;letter-spacing:1px;text-transform:uppercase;color:var(--lite);}
.stats .cv{font-size:35px;font-weight:800;color:#fff;margin-top:2px;}
.footer{position:absolute;left:0;bottom:0;width:100%;height:64px;background:#0a0e17;z-index:6;
  border-top:3px solid var(--accent);display:flex;align-items:center;justify-content:space-between;
  padding:0 60px;font-size:23px;font-weight:700;color:#b9c4d8;}
.footer .yt{display:inline-flex;align-items:center;gap:11px;color:#e6ecf6;}
.footer .yt .ico{display:inline-flex;width:38px;height:27px;border-radius:7px;background:#ff0000;
  align-items:center;justify-content:center;}
.footer .yt .ico:after{content:'';margin-left:2px;border-left:12px solid #fff;
  border-top:8px solid transparent;border-bottom:8px solid transparent;}
"""


def _bc_icon(kind, size=26):
    s = (f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" stroke="#fff" '
         f'stroke-width="3" stroke-linecap="round" stroke-linejoin="round">')
    paths = {
        "check": '<path d="M4 12l5 5L20 6"/>',
        "news":  '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 9h8M8 13h8M8 17h5"/>',
        "fire":  '<path d="M12 3c1 4 5 4 4 9a4 4 0 1 1-8 0c0-2 1-3 1-3 1 2 2 2 2 0 0-2-1-3 1-6z"/>',
        "x":     '<path d="M6 6l12 12M18 6L6 18"/>',
        "cross": '<path d="M12 5v14M5 12h14"/>',
        "card":  '<rect x="7" y="4" width="10" height="16" rx="2" fill="#fff" stroke="none"/>',
    }
    return s + paths.get(kind, "") + "</svg>"


def _bc_arrow(kind):
    """Down transfer arrow, or a jagged 'broken' bolt for a collapsed deal."""
    if kind == "broken":
        d, w, h = "M24 0L9 40L19 40L6 92L36 34L24 34Z", 72, 150
    else:
        d, w, h = "M14 0h12v50h13L20 90L1 50h13z", 62, 150
    return (f'<svg class="arrow" width="{w}" height="{h}" viewBox="0 0 40 92">'
            f'<defs><linearGradient id="ag" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="var(--accent)"/>'
            f'<stop offset="1" stop-color="var(--accent2)"/></linearGradient></defs>'
            f'<path d="{d}" fill="url(#ag)"/></svg>')


def _bc_confbar(pct):
    on = max(0, min(10, round(pct / 10)))
    segs = "".join(f'<span class="seg{" on" if i < on else ""}"></span>' for i in range(10))
    return f'<div class="confbar"><div class="track">{segs}</div><div class="pct">{pct}%</div></div>'


def _bc_logos(crests, arrow_kind):
    crests = [c for c in crests if c]
    if not crests:
        return ""
    out = ['<div class="logos">']
    if len(crests) >= 2:
        out.append(f'<img class="crest" src="{crests[0]}"/>')
        out.append(_bc_arrow(arrow_kind))
        out.append(f'<img class="crest" src="{crests[1]}"/>')
    else:
        out.append(f'<img class="crest" src="{crests[0]}"/>')
    out.append("</div>")
    return "".join(out)


def _bc_rows(rows):
    """rows: list of dicts {lbl, val (text) | html (raw), logo (uri), sm (bool)}."""
    html = []
    for r in rows:
        logo = f'<img src="{r["logo"]}"/>' if r.get("logo") else ""
        inner = r.get("html") or (r.get("val") or "—")
        cls = " sm" if r.get("sm") else ""
        html.append(f'<div class="row{cls}"><div class="lbl">{r["lbl"]}</div>'
                    f'<div class="val">{logo}{inner}</div></div>')
    return "".join(html)


def _card_timestamp(story):
    ts = story.get("timestamp") or story.get("updated")
    if ts:
        return str(ts).upper()
    return datetime.now(timezone.utc).strftime("%d %b %Y | %H:%M UTC").upper()


def _card_stats(story, player_el):
    """Bottom 5-cell strip. Every value is a dynamic slot; unknown → '—'."""
    def v(key, default="—"):
        val = story.get(key)
        return str(val).upper() if val not in (None, "") else default
    position = story.get("position")
    if not position and player_el:
        position = _POS_MAP.get(player_el.get("element_type"))
    contract_until = (story.get("contract_until") or story.get("contract") or "—")
    return [
        ("AGE", v("age")),
        ("POSITION", (position or "—").upper()),
        ("NATIONALITY", v("nationality")),
        ("MARKET VALUE", v("market_value")),
        ("CONTRACT UNTIL", str(contract_until).upper()),
    ]


def _pick_template(story):
    """Map a story onto one of the six broadcast templates. `card_template` on
    the story forces a specific one (handy for the automation / previews)."""
    forced = story.get("card_template")
    if forced in _BC_PALETTE:
        return forced
    ev = (story.get("event") or "transfer").lower()
    if story.get("collapsed"):
        return "collapsed"
    if ev == "injury":
        return "injury"
    if ev == "suspension":
        return "suspension"
    if ev in ("renewal", "stay"):
        return "renewal"
    if ev == "manager":
        return "manager"
    if story.get("mode", "confirmed") == "rumour":
        # A well-developed rumour (agreement/advanced) gets the STRONG RUMOUR
        # treatment; an early link is plain TRANSFER NEWS.
        return "rumour" if story.get("stage", 1) >= 2 else "news"
    return "confirmed"


def _build_broadcast_html(tmpl, *, logo_uri, player_name, rows_html, stats_html,
                          photo_html, logos_html, extra_html, badge_text,
                          source_text, timestamp, footer_handle):
    accent, accent2, lite, brd, glow = _BC_PALETTE[tmpl]
    header_icon, header_text, _footer_tag = _BC_HEAD[tmpl]
    root = (f":root{{--accent:{accent};--accent2:{accent2};--lite:{lite};"
            f"--brd:{brd};--glow:{glow};}}")
    logo_html = (f'<img src="{logo_uri}"/>') if logo_uri else ""
    # Player name over two lines (first / rest) like a broadcast lower-third.
    parts = (player_name or "PLAYER").split()
    if len(parts) >= 2:
        name_html = parts[0] + "<br>" + " ".join(parts[1:])
    else:
        name_html = player_name or "PLAYER"
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
        "@import url('https://fonts.googleapis.com/css2?family=Montserrat:ital,wght@0,700;0,800;1,700;1,800&display=swap');"
        + root + _BC_CSS +
        "</style></head><body>"
        "<div class='bgglow'></div><div class='slash a'></div><div class='slash b'></div>"
        "<div class='topbar-red'></div>"
        f"<div class='brand'>{logo_html}<div class='wm'><b>FPL</b> <i>VORTEX</i></div></div>"
        "<div class='plmark'><div class='p1'>PREMIER</div><div class='p2'>LEAGUE</div></div>"
        f"<div class='cat'><span class='ic'>{_bc_icon(header_icon)}</span>{header_text}</div>"
        "<div class='content'>"
        f"<div class='left'><div class='pname'>{name_html}</div><div class='rows'>{rows_html}</div></div>"
        f"<div class='right'><div class='photoglow'></div>{photo_html}{logos_html}{extra_html}"
        f"<div class='badge'><span class='ic'>{_bc_icon(header_icon, 30)}</span>{badge_text}</div></div>"
        "</div>"
        f"<div class='stats'>{stats_html}</div>"
        f"<div class='footer'><div>SOURCE: {source_text}</div>"
        f"<div>UPDATED: {timestamp}</div>"
        f"<div class='yt'><span class='ico'></span>{footer_handle}</div></div>"
        "<script>"
        "function fit(){var e=document.querySelector('.pname');if(!e)return;"
        "var w=e.parentElement.clientWidth,f=86;e.style.fontSize=f+'px';"
        "while(e.scrollWidth>w&&f>34){f-=1;e.style.fontSize=f+'px';}}"
        "document.addEventListener('DOMContentLoaded',fit);window.addEventListener('load',fit);"
        "</script></body></html>"
    )


def _render_broadcast(story, sources, filename):
    """Assemble and render one broadcast card for any event type."""
    player_el, player_name, logo_uri, photo_uri = _img_assets(story)
    tmpl = _pick_template(story)

    to_key, from_key = story.get("to_key"), story.get("from_key")
    to_club = (story.get("to_club") or (to_key or "").replace("_", " ") or "").upper()
    from_club = (story.get("from_club") or (from_key or "").replace("_", " ") or "").upper()
    to_crest, from_crest = _crest_uri(to_key), _crest_uri(from_key)
    club_key = from_key or to_key
    club_crest = from_crest or to_crest
    club_name = from_club or to_club

    stage = story.get("stage", 1)
    fee = (str(story.get("fee")).upper() if story.get("fee") else "UNDISCLOSED")
    contract = (str(story.get("contract")).upper() if story.get("contract") else "TBD")
    negotiation = (story.get("negotiation_stage") or _STAGE_TXT.get(stage, "IN PROGRESS")).upper()
    try:
        pct = int(story.get("probability") or {4: 92, 3: 80, 2: 68, 1: 55}.get(stage, 60))
    except (TypeError, ValueError):
        pct = 60
    tier = str(story.get("source_tier") or "TIER 2").upper()
    src_name = (str(sources[0]) if sources else story.get("source") or "FPL VORTEX").upper()

    # ── template-specific left rows, right logos/arrow, extra graphic, badge ──
    extra_html = ""
    arrow = "down"
    if tmpl == "confirmed":
        rows = [
            {"lbl": "FROM", "val": from_club, "logo": from_crest},
            {"lbl": "TO", "val": to_club, "logo": to_crest},
            {"lbl": "FEE", "val": fee, "sm": True},
            {"lbl": "CONTRACT", "val": contract, "sm": True},
            {"lbl": "STATUS", "val": ("OFFICIAL" if stage >= 4 else "AGREED"), "sm": True},
        ]
        crests, badge_text = [from_crest, to_crest], ("OFFICIAL" if stage >= 4 else "AGREED")
    elif tmpl == "news":
        rows = [
            {"lbl": "CLUB", "val": from_club, "logo": from_crest},
            {"lbl": "INTERESTED CLUB", "val": to_club, "logo": to_crest},
            {"lbl": "NEGOTIATIONS", "val": negotiation, "sm": True},
            {"lbl": "FEE EXPECTED", "val": fee, "sm": True},
            {"lbl": "SOURCE", "val": src_name, "sm": True},
        ]
        crests, badge_text = [from_crest, to_crest], "LATEST"
    elif tmpl == "rumour":
        rows = [
            {"lbl": "CURRENT CLUB", "val": from_club, "logo": from_crest},
            {"lbl": "INTERESTED CLUB", "val": to_club, "logo": to_crest},
            {"lbl": "PROBABILITY", "html": _bc_confbar(pct)},
            {"lbl": "SOURCE TIER", "val": tier, "sm": True},
            {"lbl": "EXPECTED FEE", "val": fee, "sm": True},
        ]
        crests, badge_text = [from_crest, to_crest], "DEVELOPING"
    elif tmpl == "collapsed":
        reason = str(story.get("collapse_reason") or story.get("reason") or "UNDISCLOSED").upper()
        rows = [
            {"lbl": "BUYING CLUB", "val": to_club, "logo": to_crest},
            {"lbl": "SELLING CLUB", "val": from_club, "logo": from_crest},
            {"lbl": "REASON", "val": reason, "sm": True},
            {"lbl": "STATUS", "val": "DEAL COLLAPSED", "sm": True},
        ]
        crests, arrow, badge_text = [to_crest, from_crest], "broken", "MOVE OFF"
    elif tmpl == "injury":
        injury = str(story.get("injury_type") or story.get("diagnosis") or "UNDISCLOSED").upper()
        ret = str(story.get("expected_return") or story.get("return_date") or "TBD").upper()
        avail = str(story.get("availability")
                    or {4: "AVAILABLE", 3: "RULED OUT", 2: "DOUBT", 1: "BEING ASSESSED"}.get(stage, "TBD")).upper()
        rows = [
            {"lbl": "CLUB", "val": club_name, "logo": club_crest},
            {"lbl": "INJURY", "val": injury, "sm": True},
            {"lbl": "EXPECTED RETURN", "val": ret, "sm": True},
            {"lbl": "AVAILABILITY", "val": avail, "sm": True},
        ]
        crests, badge_text = [club_crest], "OUT"
        extra_html = f'<div class="extra-plus">{_bc_icon("cross", 62)}</div>'
    elif tmpl == "suspension":
        reason = str(story.get("suspension_reason") or story.get("reason") or "RED CARD").upper()
        matches = str(story.get("matches") or story.get("matches_suspended") or "TBD").upper()
        returns = str(story.get("return_gameweek") or story.get("returns") or "TBD").upper()
        rows = [
            {"lbl": "CLUB", "val": club_name, "logo": club_crest},
            {"lbl": "REASON", "val": reason, "sm": True},
            {"lbl": "MATCHES", "val": matches, "sm": True},
            {"lbl": "RETURNS", "val": returns, "sm": True},
        ]
        crests, badge_text = [club_crest], "SUSPENDED"
        extra_html = '<div class="extra-card"></div>'
    elif tmpl == "manager":
        role = str(story.get("staff_role") or "MANAGER").upper()
        rows = [
            {"lbl": "CLUB", "val": club_name, "logo": club_crest},
            {"lbl": "ROLE", "val": role, "sm": True},
        ]
        crests, badge_text = [club_crest], "APPOINTED"
    else:  # renewal / stay
        rows = [
            {"lbl": "CLUB", "val": club_name, "logo": club_crest},
            {"lbl": "TERMS", "val": contract if contract != "TBD" else "NEW DEAL", "sm": True},
        ]
        crests, badge_text = [club_crest], ("STAYING" if story.get("event") == "stay" else "SIGNED")

    # player cutout (FPL photo → faded crest → 'V' placeholder)
    if photo_uri:
        photo_html = f'<img class="photo" src="{photo_uri}"/>'
    elif club_crest:
        photo_html = (f'<img class="photo" style="opacity:.5;bottom:60px;height:420px" '
                      f'src="{club_crest}"/>')
    else:
        photo_html = '<div class="photoph">V</div>'

    html = _build_broadcast_html(
        tmpl,
        logo_uri=logo_uri,
        player_name=player_name,
        rows_html=_bc_rows(rows),
        stats_html="".join(f'<div class="cell"><div class="cl">{l}</div>'
                           f'<div class="cv">{v}</div></div>'
                           for l, v in _card_stats(story, player_el)),
        photo_html=photo_html,
        logos_html=_bc_logos(crests, arrow),
        extra_html=extra_html,
        badge_text=badge_text,
        source_text=" · ".join(f"@{s}" for s in sources[:2]) if sources else "@FPLVortex",
        timestamp=_card_timestamp(story),
        footer_handle=(CHANNEL_HANDLE or "@FPLVortex").upper(),
    )
    return html


def create_transfer_image(story, sources, filename, collapsed=False):
    """Broadcast card for transfer-family events (confirmed / news / rumour /
    collapsed) plus suspension / renewal / manager, all sharing the frame."""
    if collapsed:
        story = {**story, "collapsed": True}
    html = _render_broadcast(story, sources, filename)
    if not _render_card(html, filename):
        Image.new("RGB", (1920, 1080), color=(10, 14, 26)).save(filename)


def create_injury_image(story, sources, filename):
    """Broadcast INJURY / SUSPENSION card. Shares the master frame; the
    template is picked from the event so suspensions render as SUSPENSION."""
    html = _render_broadcast(story, sources, filename)
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
