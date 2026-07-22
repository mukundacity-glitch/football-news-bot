import os
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont

# --- THEME STYLING CONFIGURATION ---
# Label/pill glyphs are limited to characters covered by standard system fonts
# (DejaVu etc.) — colour emoji like ✅/🔥/❌ render as blank "tofu" boxes on the
# card, so font-safe equivalents (✓ ▲ ✗ ▮) are used instead.
THEMES = {
    "official": {"color": (0, 168, 93), "label": "✓ CONFIRMED TRANSFER", "pill": "✓ OFFICIAL"},
    "agreed": {"color": (0, 168, 93), "label": "✓ CONFIRMED TRANSFER", "pill": "✓ AGREED"},
    "rumour": {"color": (238, 118, 0), "label": "▲ STRONG RUMOUR", "pill": "▲ DEVELOPING"},
    "collapsed": {"color": (218, 37, 29), "label": "✗ DEAL COLLAPSED", "pill": "✗ MOVE OFF"},
    "injury": {"color": (255, 186, 0), "label": "   INJURY UPDATE", "pill": "+ OUT"},
    "suspension": {"color": (218, 37, 29), "label": "   SUSPENSION", "pill": "▮ SUSPENDED"}
}

WIDTH, HEIGHT = 1920, 1080
BG_DARK = (10, 16, 26)  # Dark deep navy gradient base

_DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

def _load_font(size, candidates):
    """Try each candidate font path at the requested size; if none exist, fall
    back to Pillow's scalable default so the 1080p layout keeps its proportions
    (the old tiny bitmap default made every card's text unreadably small)."""
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    try:
        return ImageFont.load_default(size)  # Pillow >= 10.1: scalable default
    except TypeError:
        return ImageFont.load_default()

def load_fonts():
    """Loads fonts safely with proportional sizing for 1080p canvas."""
    name_font = _load_font(96, ["assets/fonts/Montserrat-BoldItalic.ttf", _DEJAVU_BOLD])
    lbl_font = _load_font(36, ["assets/fonts/Montserrat-Medium.ttf", _DEJAVU])
    val_font = _load_font(46, ["assets/fonts/Montserrat-Bold.ttf", _DEJAVU_BOLD])
    footer_font = _load_font(26, ["assets/fonts/Montserrat-Medium.ttf", _DEJAVU])
    return name_font, lbl_font, val_font, footer_font

def draw_background_gradient(img, theme_color):
    """Draws a premium dark base canvas with a right-aligned subtle theme color aura."""
    draw = ImageDraw.Draw(img)
    # Base dark fill
    draw.rectangle([(0, 0), (WIDTH, HEIGHT)], fill=BG_DARK)
    
    # Simple blend overlay to create a clean right-side color aura glow
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    ol_draw = ImageDraw.Draw(overlay)
    
    for r in range(600, 0, -5):
        alpha = int((1 - (r / 600)) * 35)
        ol_draw.ellipse(
            [(WIDTH - 200 - r, (HEIGHT // 2) - r), (WIDTH - 200 + r, (HEIGHT // 2) + r)],
            fill=(theme_color[0], theme_color[1], theme_color[2], alpha)
        )
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

def draw_watermark_v(draw):
    """Draws the massive geometric structural 'V' watermark on the right canvas panel."""
    # Drawn using polygons with a subtle low-opacity dark slate color matching your design
    v_color = (40, 52, 68, 25)
    # Left wing of V
    draw.polygon([(1420, 490), (1485, 490), (1570, 610), (1505, 610)], fill=v_color)
    # Right wing of V
    draw.polygon([(1570, 610), (1505, 610), (1590, 490), (1655, 490)], fill=v_color)

def draw_header(draw, title_text, theme_color, font_lbl):
    """Renders the top primary branding header bar with the signature red angle flare."""
    # Top Brand Red Flare Bar
    draw.polygon([(0, 0), (1100, 0), (1050, 110), (0, 110)], fill=(195, 16, 25))
    draw.text((100, 30), "FPL VORTEX", font=font_lbl, fill=(255, 255, 255))
    
    # Subheader Premier League Title Text Tag
    draw.text((WIDTH - 300, 30), "PREMIER\nLEAGUE", font=font_lbl, fill=(0, 255, 168), align="right")
    
    # Main Categorization Event Badge
    badge_y = 140
    draw.polygon([(60, badge_y), (720, badge_y), (690, badge_y + 65), (60, badge_y + 65)], fill=theme_color)
    draw.text((90, badge_y + 10), title_text.upper(), font=font_lbl, fill=(255, 255, 255))
    
    # Specific icon decorations inside the text badge for injuries/suspensions
    if "INJURY" in title_text:
        draw.ellipse([(85, badge_y + 18), (115, badge_y + 48)], fill=(255,255,255))
        draw.rectangle([(97, badge_y + 23), (103, badge_y + 43)], fill=theme_color)
        draw.rectangle([(87, badge_y + 30), (107, badge_y + 36)], fill=theme_color)
    elif "SUSPENSION" in title_text:
        draw.rounded_rectangle([(90, badge_y + 20), (110, badge_y + 46)], radius=3, fill=(255,255,255))

def draw_right_side_graphics(draw, theme_key, theme_color, story):
    """Draws custom structural right-side graphics dynamically based on theme properties."""
    center_x, center_y = 1530, 550
    
    if theme_key == "suspension":
        # Floating Red Card Asset Design
        card = Image.new("RGBA", (140, 220), (0,0,0,0))
        c_draw = ImageDraw.Draw(card)
        c_draw.rounded_rectangle([(0,0), (140, 220)], radius=15, fill=(218, 37, 29))
        rotated_card = card.rotate(15, expand=True)
        return rotated_card
        
    elif theme_key == "injury":
        # Gold Medical Plus Circle Icon Component
        draw.ellipse([(center_x - 60, center_y - 60), (center_x + 60, center_y + 60)], fill=theme_color)
        draw.rectangle([(center_x - 12, center_y - 40), (center_x + 12, center_y + 40)], fill=(255, 255, 255))
        draw.rectangle([(center_x - 40, center_y - 12), (center_x + 40, center_y + 12)], fill=(255, 255, 255))
        
    elif theme_key == "rumour":
        # Block-segment Probability Progress Indicator Visualizer
        prob_str = str(story.get("probability", "85")).replace("%", "")
        try: prob_val = int(prob_str)
        except: prob_val = 85
        
        start_x = center_x - 150
        for b in range(10):
            box_x = start_x + (b * 26)
            fill_color = theme_color if (b * 10) < prob_val else (40, 50, 65)
            draw.rectangle([(box_x, center_y), (box_x + 20, center_y + 35)], fill=fill_color)
            
    return None

def draw_stats_footer_bar(draw, stats, theme_color, font_lbl, font_val):
    """Draws the 5 aligned, structured data compartments along the lower canvas quadrant."""
    box_w, box_h = 280, 95
    start_x, start_y = 60, HEIGHT - 190
    
    for i, (key, value) in enumerate(stats.items()):
        x = start_x + (i * (box_w + 12))
        # Container Bounds Box Outline
        draw.rectangle([(x, start_y), (x + box_w, start_y + box_h)], outline=(30, 45, 65), width=2)
        # Inner Header Parameter Tag
        draw.text((x + 15, start_y + 8), key.upper(), font=font_lbl, fill=(120, 140, 160))
        # Paramater Evaluated Output Text Block
        display_val = str(value).upper() if value and str(value).strip() != "" else "—"
        draw.text((x + 15, start_y + 42), display_val, font=font_val, fill=(255, 255, 255))

def draw_data_fields(draw, fields, theme_color, font_lbl, font_val):
    """Draws the informative text list items detailing primary story points."""
    # 5 fields must fit above the stats footer bar (top edge at HEIGHT-190=890):
    # last label lands at 430+4*90=790, its value at ~832-878 — no overlap.
    start_x, start_y = 60, 430
    line_gap = 90
    
    for i, (label, val) in enumerate(fields.items()):
        current_y = start_y + (i * line_gap)
        draw.text((start_x, current_y), label.upper(), font=font_lbl, fill=theme_color)
        
        display_val = str(val).upper() if val and str(val).strip() != "" else "UNDISCLOSED"
        draw.text((start_x, current_y + 42), display_val, font=font_val, fill=(255, 255, 255))

def get_theme_mode(story):
    """Determines exact rendering style configuration based on incoming parsed values."""
    ev = story.get("event")
    if story.get("collapsed", False): return "collapsed"
    if ev == "injury": return "injury"
    if ev == "suspension": return "suspension"
    
    mode_lbl = str(story.get("mode", "rumour")).upper()
    if "OFFICIAL" in mode_lbl or "CONFIRMED" in mode_lbl: return "official"
    if "AGREED" in mode_lbl: return "agreed"
    return "rumour"

def render_core_card(story, sources, output_path):
    """Main execution pipeline constructing the visual canvas assets."""
    # 1. Canvas Setup
    base_img = Image.new("RGB", (WIDTH, HEIGHT), color=BG_DARK)
    theme_key = get_theme_mode(story)
    theme = THEMES[theme_key]
    
    # 2. Add Background Depth Auras
    img = draw_background_gradient(base_img, theme["color"])
    draw = ImageDraw.Draw(img)
    
    name_font, lbl_font, val_font, footer_font = load_fonts()
    
    # 3. Structural Underlays
    draw_watermark_v(draw)
    
    # 4. Right side Overlay Items
    rotated_layer = draw_right_side_graphics(draw, theme_key, theme["color"], story)
    if rotated_layer:
        img.paste(rotated_layer, (1450, 420), rotated_layer)
        
    # 5. Dynamic Left-Hand Side Fields Mapping
    fields = {}
    if theme_key in ["official", "agreed", "rumour"]:
        fields["From"] = story.get("from_club", "Undisclosed")
        fields["To"] = story.get("to_club", "Undisclosed")
        fields["Fee"] = story.get("fee", "Undisclosed")
        fields["Contract"] = story.get("contract", "TBD")
        fields["Status"] = story.get("mode", "Agreed" if theme_key == "agreed" else "Official")
    elif theme_key == "collapsed":
        fields["Buying Club"] = story.get("to_club", "Undisclosed")
        fields["Selling Club"] = story.get("from_club", "Undisclosed")
        fields["Reason"] = story.get("reason", "Personal Terms")
        fields["Status"] = "Deal Collapsed"
    elif theme_key == "injury":
        fields["Club"] = story.get("from_club") or story.get("to_club") or "Premier League"
        fields["Injury"] = story.get("diagnosis", "Assessing Situation")
        fields["Expected Return"] = story.get("expected_return", "Unknown Duration")
        fields["Availability"] = "Unavailable"
    elif theme_key == "suspension":
        fields["Club"] = story.get("from_club") or story.get("to_club") or "Premier League"
        fields["Reason"] = story.get("diagnosis", "Straight Red Card")
        fields["Matches"] = story.get("matches_slashed", "3 Matches")
        fields["Returns"] = story.get("expected_return", "Gameweek TBD")

    draw_data_fields(draw, fields, theme["color"], lbl_font, val_font)
    
    # 6. Player Primary Identification Typography Text
    player_name = str(story.get("player", "PLAYER")).upper()
    draw.text((60, 240), player_name, font=name_font, fill=(255, 255, 255))
    
    # 7. Low Compartment Stats Mapping
    stats = {
        "Age": story.get("age", "—"),
        "Position": story.get("position", "—"),
        "Nationality": story.get("nationality", "—"),
        "Market Value": story.get("market_value", "—"),
        "Contract Until": story.get("contract_until", "—")
    }
    # Footer-size header tags: 36px labels like "CONTRACT UNTIL" overflow the
    # 280px compartments, the 26px footer font fits every label.
    draw_stats_footer_bar(draw, stats, theme["color"], footer_font, val_font)
    
    # 8. Draw Top Branding Layer Elements
    draw_header(draw, theme["label"], theme["color"], lbl_font)
    
    # 9. Bottom Right Context State Pill Box Indicator
    # Pill width follows the text so longer statuses ("▲ DEVELOPING") never overflow.
    pill_text = theme["pill"].upper()
    pill_w = max(320, int(draw.textlength(pill_text, font=val_font)) + 70)
    pill_h = 75
    # Sits just ABOVE the stats bar (top edge HEIGHT-190) so a wide pill can
    # never cover the right-most stats compartment.
    pill_x, pill_y = WIDTH - pill_w - 60, HEIGHT - 285
    draw.rounded_rectangle([(pill_x, pill_y), (pill_x + pill_w, pill_y + pill_h)], radius=12, fill=theme["color"])
    draw.text((pill_x + 35, pill_y + 15), pill_text, font=val_font, fill=(255, 255, 255))
    
    # 10. Outer Frame Footer Meta Descriptions Text Lines
    footer_y = HEIGHT - 55
    draw.line([(0, footer_y - 15), (WIDTH, footer_y - 15)], fill=(25, 35, 50), width=1)
    
    src_txt = f"SOURCE: {str(sources[0] if sources else 'Aggregator').upper()}"
    time_txt = f"UPDATED: {datetime.now(timezone.utc).strftime('%d %b %Y | %H:%M UTC')}"
    draw.text((60, footer_y), src_txt, font=footer_font, fill=(100, 120, 140))
    draw.text((600, footer_y), time_txt, font=footer_font, fill=(100, 120, 140))
    
    # Precise Account Handle Placement
    draw.text((WIDTH - 320, footer_y), "▶ @FPLVortexM", font=footer_font, fill=(230, 30, 40))
    
    # Save Image out to disk
    img.save(output_path, "PNG")
    print(f"🎨 Image output saved: {output_path}")

# Interface abstraction hooks matching old pipeline configurations
def create_transfer_image(item, sources, image_path, collapsed=False):
    item["collapsed"] = collapsed
    render_core_card(item, sources, image_path)

def create_injury_image(item, sources, image_path):
    render_core_card(item, sources, image_path)

def _create_fallback_card(item, sources, image_path):
    render_core_card(item, sources, image_path)
