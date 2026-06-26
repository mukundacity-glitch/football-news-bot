# src/renderer.py
import threading
from pathlib import Path
from src.constants import CLUB_COLORS, FPL_LOGO_IDS

def _render_html_sync(html_content, filename):
    """Synchronous worker window for Playwright layout snapping."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1380, "height": 776})
        page.set_content(html_content)
        page.wait_for_timeout(500)
        page.screenshot(path=filename)
        browser.close()

def create_adaptive_transfer_image(story, sources, filename, fpl_data):
    """Generates an verified data card using HTML/CSS layout grids via Playwright."""
    from src.fpl_feed import find_player_in_fpl
    
    player_el = find_player_in_fpl(story.get("player"), fpl_data)
    player_name = (player_el["web_name"] if player_el else story.get("player")) or "PLAYER"
    
    # UI Copy Transformation: Fallback phrasing based on event classification
    event_type = story.get("event", "transfer").upper()
    status_msg = story.get("body_summary") or story.get("body", "Transfer market updates ongoing.")
    
    # Base background styles dynamically resolved off validated parent club accent
    club_color = "rgb(84, 224, 124)"  # Default Vortex Green
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@700;900&display=swap');
            body {{
                margin: 0; padding: 0; width: 1380px; height: 776px;
                background: linear-gradient(135deg, #0b1220 0%, #1c2846 100%);
                font-family: 'Montserrat', sans-serif; color: white;
                display: flex; overflow: hidden; position: relative;
            }}
            .container {{ display: flex; width: 100%; height: 100%; padding: 60px; box-sizing: border-box; }}
            .left-column {{ flex: 1; display: flex; flex-direction: column; justify-content: center; }}
            .wordmark {{ font-size: 46px; font-weight: 900; margin-bottom: 25px; }}
            .wordmark span {{ color: #54e07c; }}
            .event-badge {{
                display: inline-block; background: #e31e24; color: white;
                padding: 12px 24px; font-size: 32px; font-weight: 900; border-radius: 8px;
                letter-spacing: 2px; margin-bottom: 25px; text-transform: uppercase;
            }}
            .player-name {{
                font-size: 100px; font-weight: 900; line-height: 1.05; text-transform: uppercase;
                margin-bottom: 40px; white-space: nowrap; width: 100%; overflow: hidden;
            }}
            .status-container {{ font-size: 38px; font-weight: 700; line-height: 1.4; color: #bec8dc; max-width: 800px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="left-column">
                <div class="wordmark">FPL <span>VORTEX</span></div>
                <div><div class="event-badge">{event_type}</div></div>
                <div id="playerName" class="player-name">{player_name}</div>
                <div class="status-container">{status_msg}</div>
            </div>
        </div>
        <script>
            const el = document.getElementById('playerName');
            let size = 100;
            while(el.scrollWidth > el.clientWidth && size > 40) {{
                size -= 2;
                el.style.fontSize = size + 'px';
            }}
        </script>
    </body>
    </html>
    """
    
    # Enforce background worker thread context handling
    t = threading.Thread(target=_render_html_sync, args=(html_content, filename))
    t.start()
    t.join()
