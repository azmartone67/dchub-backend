"""Phase HH (2026-05-13) — dynamic OG card generator for press releases.

GET /api/v1/og/<style>/<slug>.png   →  1200x630 branded social card
GET /api/v1/og/today/<slug>.png     →  picks style by day-of-week rotation

Style rotation:
    Mon, Fri → data_brutal  (Bloomberg-terminal hero stat)
    Tue, Sat → editorial    (magazine card, gradient bg)
    Wed, Sun → infographic  (bar chart of top 5 markets)
    Thu      → ai_hero      (placeholder → SDXL via Workers AI, follow-up)

Cards are LinkedIn-/X-/OG-standard 1200x630 PNG. The CF Worker's
buildPressReleaseHtml points og:image at /api/v1/og/today/<slug>.png
so each day's auto-press gets the day-of-week-appropriate visual.

Failures fall back to a minimal "DC Hub" card so we never serve a 404
to crawlers (would break the link preview entirely on LinkedIn).
"""
from flask import Blueprint, Response
import io, datetime, json, os

og_cards_bp = Blueprint('og_cards', __name__)

# LinkedIn/Twitter/OG standard
W, H = 1200, 630

# DC Hub brand palette
BG       = (10, 14, 26)
PANEL    = (20, 27, 50)
ACCENT   = (255, 107, 53)    # orange — DC Hub primary
ACCENT2  = (255, 165, 79)    # softer orange — kickers
TEXT     = (230, 233, 240)
MUTED    = (154, 165, 190)
DIM      = (90, 100, 120)
GREEN    = (104, 211, 145)   # BUILD verdict
RED      = (239, 68, 68)     # AVOID verdict
AMBER    = (245, 158, 11)    # CAUTION


def _font(size, bold=True):
    """Find a system font. Linux servers have DejaVu; macOS dev has Helvetica."""
    from PIL import ImageFont
    candidates = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold
            else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/System/Library/Fonts/Helvetica.ttc',
        '/Library/Fonts/Arial Bold.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf' if bold
            else '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
    ]
    for path in candidates:
        try: return ImageFont.truetype(path, size)
        except Exception: continue
    return ImageFont.load_default()


def _mono(size):
    """Monospace font for terminal/data-brutalist style."""
    from PIL import ImageFont
    for path in [
        '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf',
        '/System/Library/Fonts/Menlo.ttc',
        '/Library/Fonts/Courier New Bold.ttf',
    ]:
        try: return ImageFont.truetype(path, size)
        except Exception: continue
    return _font(size, bold=True)


def _get_press_release(slug):
    """Pull press release row + signals JSON from DB. Returns None if
    no row found OR DB unavailable — caller falls back to brand card."""
    db = os.environ.get('DATABASE_URL')
    if not db: return None
    try:
        import psycopg2
        conn = psycopg2.connect(db, sslmode='require')
        with conn.cursor() as cur:
            cur.execute("""
                SELECT pr.title, pr.subheadline, pr.published_date,
                       apr.source_data, apr.source_topic
                FROM press_releases pr
                LEFT JOIN auto_press_releases apr
                  ON apr.press_release_id = pr.id
                WHERE pr.slug = %s
                LIMIT 1
            """, (slug,))
            row = cur.fetchone()
            if not row: return None
            signals = {}
            if row[3]:
                try:
                    signals = json.loads(row[3]) if isinstance(row[3], str) else row[3]
                except Exception:
                    signals = {}
            return {
                'title': row[0] or slug,
                'subheadline': row[1] or '',
                'date': row[2],
                'signals': signals,
                'topic': row[4] or '',
            }
    except Exception as e:
        print(f"[og_cards] db error for {slug}: {e}")
        return None
    finally:
        try: conn.close()
        except Exception: pass


def _wrap(text, max_chars):
    """Greedy word wrap. Returns list of lines."""
    words = (text or '').split()
    lines, cur = [], ''
    for w in words:
        if len(cur) + len(w) + 1 < max_chars:
            cur = (cur + ' ' + w).strip()
        else:
            if cur: lines.append(cur)
            cur = w
    if cur: lines.append(cur)
    return lines


def _verdict_for(signals: dict, fallback='BUILD'):
    """Extract the verdict for the top market — used for the colored badge."""
    top = (signals.get('top_build_markets') or [])
    if top and isinstance(top, list):
        v = top[0].get('verdict', fallback)
        return (v or fallback).upper()
    return fallback


def _verdict_color(verdict: str):
    v = (verdict or '').upper()
    if 'BUILD' in v: return GREEN
    if 'AVOID' in v: return RED
    return AMBER


# ---------------------------------------------------------------------------
# Style 1: data_brutal — Bloomberg-terminal hero stat
# ---------------------------------------------------------------------------

def _draw_data_brutal(pr):
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)

    # Top brand bar
    d.rectangle([(0, 0), (W, 60)], fill=ACCENT)
    d.text((40, 16), 'DCHUB · DCPI INDEX', font=_mono(24), fill=BG)
    date_str = (pr['date'].strftime('%Y-%m-%d')
                if pr.get('date') else datetime.datetime.utcnow().strftime('%Y-%m-%d'))
    d.text((W - 220, 16), date_str, font=_mono(24), fill=BG)

    # Extract the hero stat from signals
    signals = pr.get('signals', {})
    top = (signals.get('top_build_markets') or [{}])[0]
    market_name = top.get('market_name', '').strip()
    score = top.get('excess_power_score', 0)
    if not market_name:
        # Fall back to parsing the title
        title = pr.get('title', '')
        if ' Tops ' in title:
            market_name = title.split(' Tops ')[0]
        elif ' Leads ' in title:
            market_name = title.split(' Leads ')[0]
        else:
            market_name = title.split(',')[0] if ',' in title else title[:30]
        # Try to extract score from title via regex
        import re
        m = re.search(r'(\d+\.\d+)', title)
        if m: score = float(m.group(1))

    # Market name — top-left
    d.text((60, 130), market_name.upper()[:24], font=_font(60), fill=TEXT)

    # The big number — terminal style
    if score:
        d.text((60, 220), f'{score:.1f}', font=_mono(200), fill=ACCENT)
        d.text((60, 460), 'EXCESS POWER INDEX  ·  #1 NATIONAL',
               font=_mono(24), fill=MUTED)
    else:
        # No score → use subheadline truncated
        sub = pr.get('subheadline', '')
        for i, line in enumerate(_wrap(sub, 38)[:4]):
            d.text((60, 250 + i * 60), line, font=_font(38), fill=TEXT)

    # Verdict badge (right-bottom)
    verdict = _verdict_for(signals)
    vcol = _verdict_color(verdict)
    bw = 200
    d.rectangle([(W - bw - 40, H - 110), (W - 40, H - 50)], fill=vcol)
    d.text((W - bw - 20, H - 100), verdict, font=_font(36), fill=BG)

    # Footer
    d.text((60, H - 50), 'dchub.cloud · DC Hub Daily Index',
           font=_mono(18), fill=MUTED)

    return img


# ---------------------------------------------------------------------------
# Style 2: editorial — gradient bg + clean magazine typography
# ---------------------------------------------------------------------------

def _draw_editorial(pr):
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (W, H), (15, 23, 42))
    d = ImageDraw.Draw(img)

    # Vertical gradient
    for i in range(H):
        t = i / H
        r = int(15 + (60 - 15) * t)
        g = int(23 + (40 - 23) * t)
        b = int(42 + (110 - 42) * t)
        d.line([(0, i), (W, i)], fill=(r, g, b))

    # Kicker (top accent label)
    d.text((80, 80), '◆  DC HUB  ·  DAILY POWER INDEX',
           font=_font(20), fill=ACCENT2)

    # Headline — large, word-wrapped, max 3 lines
    title = pr.get('title', '')[:200]
    lines = _wrap(title, 28)[:3]
    y = 150
    for line in lines:
        d.text((80, y), line, font=_font(58), fill=TEXT)
        y += 72

    # Subheadline (smaller, muted, 2 lines max)
    sub = pr.get('subheadline', '')
    if sub:
        sublines = _wrap(sub, 60)[:2]
        sy = max(y + 30, 380)
        for s in sublines:
            d.text((80, sy), s, font=_font(24, bold=False), fill=MUTED)
            sy += 36

    # CTA
    d.text((80, H - 100), '→  dchub.cloud/news',
           font=_font(28), fill=ACCENT)
    date_str = (pr['date'].strftime('%B %d, %Y')
                if pr.get('date') else datetime.datetime.utcnow().strftime('%B %d, %Y'))
    d.text((80, H - 60), f'DC Hub  ·  {date_str}',
           font=_font(18), fill=DIM)

    return img


# ---------------------------------------------------------------------------
# Style 3: infographic — bar chart of top 5 markets
# ---------------------------------------------------------------------------

def _draw_infographic(pr):
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)

    # Header strip
    d.rectangle([(0, 0), (W, 80)], fill=PANEL)
    d.text((60, 26), 'DCPI EXCESS POWER  ·  TOP 5 MARKETS',
           font=_font(28), fill=ACCENT)
    date_str = (pr['date'].strftime('%Y-%m-%d')
                if pr.get('date') else datetime.datetime.utcnow().strftime('%Y-%m-%d'))
    d.text((W - 200, 30), date_str, font=_mono(22), fill=MUTED)

    # Pull top 5 from signals
    signals = pr.get('signals', {})
    top_5 = (signals.get('top_build_markets') or [])[:5]
    # Pad with placeholders if fewer than 5
    if not top_5:
        # Heuristic fallback from title — at least show the hero market
        title = pr.get('title', '')
        import re
        score = 0
        m = re.search(r'(\d+\.\d+)', title)
        if m: score = float(m.group(1))
        name = title.split(' Tops ')[0] if ' Tops ' in title else 'Top Market'
        top_5 = [{'market_name': name, 'excess_power_score': score, 'verdict': 'BUILD'}]

    max_score = max(
        [m.get('excess_power_score', 0) or 0 for m in top_5] + [1],
    )

    y_start = 140
    bar_h = 56
    gap = 28
    label_col_x = 360
    bar_start_x = 380
    bar_max_x = W - 180

    for i, m in enumerate(top_5):
        y = y_start + i * (bar_h + gap)
        name = (m.get('market_name') or '?')[:24]
        score = m.get('excess_power_score', 0) or 0
        is_top = (i == 0)
        color = ACCENT if is_top else (90, 130, 200)

        # Market name (right-aligned in left column)
        try:
            d.text((label_col_x - 20, y + 14), name,
                   font=_font(26), fill=TEXT, anchor='rt')
        except TypeError:
            # Older PIL without anchor support
            d.text((60, y + 14), name, font=_font(26), fill=TEXT)

        # Bar
        bar_w = int((score / max_score) * (bar_max_x - bar_start_x))
        d.rectangle([(bar_start_x, y), (bar_start_x + bar_w, y + bar_h)],
                    fill=color)

        # Score label at end of bar
        d.text((bar_start_x + bar_w + 16, y + 16), f'{score:.1f}',
               font=_font(28), fill=TEXT)

        # #1 arrow indicator
        if is_top:
            d.text((W - 80, y + 16), '▲', font=_font(28), fill=GREEN)

    # Verdict + summary at bottom
    verdict = _verdict_for(signals)
    vcol = _verdict_color(verdict)
    d.rectangle([(60, H - 80), (220, H - 30)], fill=vcol)
    d.text((76, H - 70), verdict, font=_font(32), fill=BG)

    if top_5:
        first = (top_5[0].get('market_name') or '?')[:30]
        d.text((240, H - 70), f'{first} ranked #1 nationally',
               font=_font(22), fill=TEXT)
    d.text((240, H - 38), 'dchub.cloud  ·  DC Hub Daily Power Index',
           font=_mono(16), fill=MUTED)

    return img


# ---------------------------------------------------------------------------
# Style 4: ai_hero — placeholder for SDXL (Workers AI). Falls back to
# editorial for now. Phase HH+1 wires Cloudflare Workers AI.
# ---------------------------------------------------------------------------

def _draw_ai_hero(pr):
    # TODO Phase HH+1: call CF Workers AI SDXL with prompt derived from
    # pr.title + pr.topic. For now, render editorial style with a
    # different gradient so the rotation is visible.
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (W, H), (10, 14, 26))
    d = ImageDraw.Draw(img)

    # Dramatic vertical gradient — purple → orange (sunrise)
    for i in range(H):
        t = i / H
        r = int(40 + (255 - 40) * t)
        g = int(20 + (140 - 20) * t)
        b = int(80 + (40 - 80) * t)
        d.line([(0, i), (W, i)], fill=(r, g, b))

    # Brand chip
    d.rectangle([(60, 60), (260, 100)], fill=BG)
    d.text((76, 70), 'DCHUB · DAILY', font=_mono(20), fill=ACCENT)

    # Headline
    title = pr.get('title', '')[:120]
    lines = _wrap(title, 24)[:3]
    y = 200
    for line in lines:
        # Drop shadow for legibility on gradient
        d.text((82, y + 3), line, font=_font(60), fill=(0, 0, 0))
        d.text((80, y), line, font=_font(60), fill=TEXT)
        y += 76

    # CTA
    d.text((80, H - 80), '→ dchub.cloud/news',
           font=_font(32), fill=TEXT)
    date_str = (pr['date'].strftime('%B %d, %Y')
                if pr.get('date') else datetime.datetime.utcnow().strftime('%B %d, %Y'))
    d.text((80, H - 42), f'DC Hub  ·  {date_str}',
           font=_font(18), fill=TEXT)

    return img


# ---------------------------------------------------------------------------
# Day-of-week rotation
# ---------------------------------------------------------------------------

# Monday=0 ... Sunday=6
DAILY_STYLES = {
    0: 'data_brutal',   # Monday
    1: 'editorial',     # Tuesday
    2: 'infographic',   # Wednesday
    3: 'ai_hero',       # Thursday
    4: 'data_brutal',   # Friday
    5: 'editorial',     # Saturday
    6: 'infographic',   # Sunday
}

def todays_style():
    """The style for today (UTC), per user-chosen Mon-Sun rotation.

    Exposed as a module-level helper so marketing_engine can build the
    LinkedIn post copy variants matched to the same card.
    """
    return DAILY_STYLES.get(datetime.datetime.utcnow().weekday(), 'data_brutal')


STYLE_MAP = {
    'data_brutal': _draw_data_brutal,
    'editorial':   _draw_editorial,
    'infographic': _draw_infographic,
    'ai_hero':     _draw_ai_hero,
}


def _draw_fallback(slug):
    """Last-resort card if DB unavailable or generator throws. Never 404 —
    LinkedIn / Twitter aggressively drop link-card previews if og:image
    returns 4xx, and we want SOME card no matter what."""
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([(0, 0), (W, 80)], fill=ACCENT)
    d.text((60, 26), 'DC HUB', font=_font(32), fill=BG)
    d.text((60, 200), 'Data Center Intelligence',
           font=_font(52), fill=TEXT)
    d.text((60, 270), 'Power · Pipeline · Pricing · ISO Grid',
           font=_font(32), fill=MUTED)
    d.text((60, H - 50), 'dchub.cloud', font=_mono(22), fill=ACCENT)
    return img


@og_cards_bp.route('/api/v1/og/<style>/<path:slug>.png', methods=['GET'])
def og_card(style, slug):
    """Generate the PNG. `style=today` resolves to today's rotation.
    `slug` should match a press_releases.slug row."""
    if style == 'today':
        style = todays_style()

    pr = _get_press_release(slug)
    try:
        if pr is None:
            img = _draw_fallback(slug)
        else:
            fn = STYLE_MAP.get(style, _draw_data_brutal)
            img = fn(pr)
    except Exception as e:
        print(f"[og_cards] render error for {style}/{slug}: {e}")
        img = _draw_fallback(slug)

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    buf.seek(0)
    return Response(
        buf.read(),
        mimetype='image/png',
        headers={
            # Cache for an hour at the edge — same slug+style produces
            # the same card. Forces refresh when style rotates.
            'Cache-Control': 'public, max-age=3600, s-maxage=3600',
            'X-DC-Card-Style': style,
            'X-DC-Card-Slug': slug,
        },
    )


def register_og_cards(app):
    app.register_blueprint(og_cards_bp)
    app.logger.info("✓ OG cards registered: GET /api/v1/og/<style>/<slug>.png")
    app.logger.info(f"  Today's rotation style: {todays_style()}")
