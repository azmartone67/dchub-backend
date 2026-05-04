"""Phase 23 — /api/v1/grid/<iso>/card.png

Server-side PNG generation for LinkedIn / OG cards. 1200x1200, branded.
Plugs into the Phase 21 OG-image flow.
"""
from flask import Blueprint, request, Response, send_file
import io, datetime, requests

grid_card_bp = Blueprint('grid_card', __name__)


@grid_card_bp.route('/api/v1/grid/<iso>/card.png', methods=['GET'])
def card(iso):
    iso = iso.upper()
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return Response('PIL not installed', status=500)

    try:
        r = requests.get(f'http://127.0.0.1:8080/api/v1/grid/intelligence/{iso}', timeout=6)
        d = r.json().get('data', {}) if r.ok else {}
    except Exception:
        d = {}

    demand = int(d.get('current_demand_mw', 0) or 0)
    headroom = float(d.get('headroom_pct', 0) or 0)
    cap = int(d.get('total_capacity_mw', 0) or 0)

    W, H = 1200, 1200
    img = Image.new('RGB', (W, H), (10, 14, 26))
    draw = ImageDraw.Draw(img)

    def font(size):
        for path in [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            '/System/Library/Fonts/Helvetica.ttc',
            '/Library/Fonts/Arial Bold.ttf',
        ]:
            try: return ImageFont.truetype(path, size)
            except Exception: continue
        return ImageFont.load_default()

    # Header bar
    draw.rectangle([(0, 0), (W, 60)], fill=(255, 107, 53))
    draw.text((40, 14), 'DC HUB · GRID INTELLIGENCE', font=font(24), fill=(10, 14, 26))
    today = datetime.datetime.utcnow().strftime('%b %d, %Y')
    draw.text((W - 220, 14), today, font=font(24), fill=(10, 14, 26))

    # ISO badge
    draw.text((60, 120), iso, font=font(180), fill=(255, 107, 53))

    # Big demand number
    draw.text((60, 360), f'{demand:,}', font=font(220), fill=(230, 233, 240))
    draw.text((60, 580), 'MW serving now', font=font(48), fill=(154, 165, 190))

    # Headroom bar
    bar_x, bar_y, bar_w, bar_h = 60, 720, W - 120, 80
    draw.rectangle([(bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h)], fill=(35, 43, 65))
    fill_w = int(bar_w * max(0, min(headroom / 100, 1)))
    color = (76, 175, 80) if headroom > 30 else ((255, 193, 7) if headroom > 15 else (244, 67, 54))
    draw.rectangle([(bar_x, bar_y), (bar_x + fill_w, bar_y + bar_h)], fill=color)
    draw.text((60, 820), f'{headroom:.0f}% HEADROOM', font=font(56), fill=(230, 233, 240))
    draw.text((60, 890), f'{cap:,} MW total capacity', font=font(36), fill=(154, 165, 190))

    # Footer
    draw.text((60, H - 100), 'Live EIA data · dchub.cloud/grid', font=font(36), fill=(154, 165, 190))

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return Response(buf.read(), mimetype='image/png',
                    headers={'Cache-Control': 'public, max-age=300'})
