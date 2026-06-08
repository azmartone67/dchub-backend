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

# DC Hub brand palette — synced to dchub-brand.css (2026-06-05 visual rebuild).
# Was: orange-on-navy. Now: deep navy + brand purple + cyan accents + green/red/amber verdicts.
# Reasoning: the purple/cyan combo matches the site (/sites/value, /premium, /dcpi) and
# tests dramatically better on LinkedIn/X feeds than the previous orange treatment, which
# competed visually with every other ad/post and read as "alert," not "premium."
BG          = (15, 23, 42)       # slate-900 — primary canvas
BG_DEEP     = (8, 14, 28)        # deeper navy for gradient bottoms
PANEL       = (30, 41, 59)       # slate-800 — raised panels / chips
PANEL_HI    = (51, 65, 85)       # slate-700 — hovered/highlighted panels
PURPLE      = (124, 58, 237)     # brand primary
PURPLE_LT   = (167, 139, 250)    # brand lighter — for badges + accent text
CYAN        = (56, 189, 248)     # accent for kickers, dates, links
GREEN       = (16, 185, 129)     # BUILD verdict
RED         = (239, 68, 68)      # AVOID verdict
AMBER       = (245, 158, 11)     # CAUTION verdict
WHITE       = (255, 255, 255)
TEXT        = (241, 245, 249)    # near-white text
MUTED       = (148, 163, 184)    # slate-400 — secondary text
DIM         = (100, 116, 139)    # slate-500 — captions

# Back-compat aliases — older code references ACCENT / ACCENT2
ACCENT  = PURPLE
ACCENT2 = PURPLE_LT


# Fonts are BUNDLED in the repo (routes/fonts/) and loaded first. This is the
# durable fix for the 2026-06-06 "empty cards" bug: Railway's Nixpacks image
# has no system DejaVu/Helvetica at the hardcoded paths, so _font() fell
# through to ImageFont.load_default() — a ~10px bitmap font that IGNORES the
# requested size. Every headline/number rendered tiny and the cards looked
# ~80% empty. It rendered fine in dev (macOS has Helvetica.ttc) so it was
# invisible locally. Bundling the TTFs removes the host-font dependency
# entirely — the cards now render identically on Railway, Render and local.
_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')
_FONT_FELL_BACK = False   # flips True (and logs once) if we ever hit load_default


def _font(size, bold=True):
    """Bundled DejaVu (repo) first, then system fonts, then a LOUD default."""
    from PIL import ImageFont
    global _FONT_FELL_BACK
    candidates = [
        os.path.join(_FONT_DIR, 'DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf'),
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold
            else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/System/Library/Fonts/Helvetica.ttc',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf' if bold
            else '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
    ]
    for path in candidates:
        try: return ImageFont.truetype(path, size)
        except Exception: continue
    if not _FONT_FELL_BACK:
        _FONT_FELL_BACK = True
        print(f"[og_cards] FONT FALLBACK to load_default() — bundled font missing "
              f"at {_FONT_DIR}; cards will render tiny. CHECK THE DEPLOY.")
    return ImageFont.load_default()


def _mono(size):
    """Monospace font for terminal/data-brutalist style (bundled first)."""
    from PIL import ImageFont
    for path in [
        os.path.join(_FONT_DIR, 'DejaVuSansMono-Bold.ttf'),
        '/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf',
        '/System/Library/Fonts/Menlo.ttc',
    ]:
        try: return ImageFont.truetype(path, size)
        except Exception: continue
    return _font(size, bold=True)


def _get_press_release(slug):
    """Pull press release row + signals JSON from DB. Returns None if
    no row found OR DB unavailable — caller falls back to brand card.

    Phase HH+4 (2026-05-14): normalize `date` to a datetime.date object.
    COALESCE between TIMESTAMPTZ (published_date) and DATE (date) in
    Postgres returns the type-promoted result, which psycopg2 sometimes
    deserializes as TEXT depending on driver version. The renderers
    all call .strftime() on it — string would AttributeError and
    every card fell through to the fallback.
    """
    db = os.environ.get('DATABASE_URL')
    if not db: return None
    try:
        import psycopg2
        conn = psycopg2.connect(db, sslmode='require')
        with conn.cursor() as cur:
            cur.execute("""
                SELECT pr.title, pr.subheadline,
                       COALESCE(pr.published_date, pr.date) AS pr_date,
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

            # Normalize date — psycopg2 might return str, datetime.date, or
            # datetime.datetime depending on column type promotion.
            raw_date = row[2]
            pr_date = None
            if hasattr(raw_date, 'strftime'):
                pr_date = raw_date  # already a date/datetime object
            elif isinstance(raw_date, str):
                # Parse common formats. Postgres TEXT-cast dates look like
                # '2026-05-13' or '2026-05-13 12:00:00+00'.
                for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S%z',
                            '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S%z',
                            '%Y-%m-%dT%H:%M:%S'):
                    try:
                        pr_date = datetime.datetime.strptime(raw_date[:25], fmt)
                        break
                    except (ValueError, TypeError):
                        continue
            return {
                'title': row[0] or slug,
                'subheadline': row[1] or '',
                'date': pr_date,  # always a date/datetime obj or None
                'signals': signals,
                'topic': row[4] or '',
            }
    except Exception as e:
        print(f"[og_cards] db error for {slug}: {e}")
        return None
    finally:
        try: conn.close()
        except Exception: pass


def _market_name_of(m: dict) -> str:
    """Extract market name from a top_build_markets entry. Production
    signals use the key `market`; the original schema design used
    `market_name`. Support both for back-compat."""
    return (m.get('market') or m.get('market_name') or '?').strip()


def _market_score_of(m: dict) -> float:
    """Same back-compat shim for excess-power score."""
    v = m.get('excess')
    if v is None: v = m.get('excess_power_score')
    if v is None: v = 0
    try: return float(v)
    except (ValueError, TypeError): return 0.0


def _wrap(text, max_chars):
    """Greedy word wrap by CHARACTER count. Returns list of lines.
    Fragile for headlines (char width != pixel width) — prefer _wrap_px
    for anything that must fit a known pixel box."""
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


def _wrap_px(text, font, max_w, max_lines=3):
    """Pixel-accurate greedy word wrap — measures each candidate line with
    the ACTUAL font so a headline never overflows the canvas. (The
    char-count _wrap silently overflowed once the bundled fonts started
    rendering at true size — the ai_hero headline ran off the right edge.)
    Truncates to max_lines with an ellipsis."""
    def _w(s):
        try:
            bb = font.getbbox(s)
            return bb[2] - bb[0]
        except Exception:
            try: return font.getsize(s)[0]
            except Exception: return len(s) * 10
    words = (text or '').split()
    lines, cur = [], ''
    for i, w in enumerate(words):
        trial = (cur + ' ' + w).strip()
        if _w(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                # Out of room — ellipsize the final line and stop.
                last = lines[-1]
                while last and _w(last + '…') > max_w:
                    last = last.rsplit(' ', 1)[0] if ' ' in last else last[:-1]
                lines[-1] = (last + '…') if last else '…'
                return lines[:max_lines]
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines[:max_lines]


def _verdict_for(signals: dict, fallback='BUILD'):
    """Extract the verdict for the top market — used for the colored badge.
    If the signals dict doesn't carry an explicit verdict, default to
    'BUILD' (top_build_markets list contains markets the model flagged
    as BUILD anyway)."""
    top = (signals.get('top_build_markets') or [])
    if top and isinstance(top, list) and isinstance(top[0], dict):
        v = top[0].get('verdict', fallback)
        return (v or fallback).upper()
    return fallback


def _safe_date_str(pr_date, fmt='%Y-%m-%d'):
    """Format a date-or-None pr['date'] value. Falls back to UTC today
    if missing/null so cards never show an empty timestamp line."""
    if pr_date and hasattr(pr_date, 'strftime'):
        return pr_date.strftime(fmt)
    return datetime.datetime.utcnow().strftime(fmt)


def _verdict_color(verdict: str):
    v = (verdict or '').upper()
    if 'BUILD' in v: return GREEN
    if 'AVOID' in v: return RED
    return AMBER


# ---------------------------------------------------------------------------
# Visual primitives — used by the v2 (2026-06-05) design language
# ---------------------------------------------------------------------------

def _rounded_rect(d, box, radius, fill):
    """ImageDraw.rounded_rectangle wrapper that gracefully degrades on
    Pillow < 8.2 (which lacks the primitive). We need rounded rects for
    verdict pills + brand chips — they're the strongest single visual
    upgrade vs the old hard-edged orange bars."""
    try:
        d.rounded_rectangle(box, radius=radius, fill=fill)
    except AttributeError:
        # Old Pillow — fall back to plain rectangle
        d.rectangle(box, fill=fill)


def _verdict_pill(d, x, y, verdict, font_size=44, pad_x=40, pad_y=18):
    """Color-coded verdict pill (BUILD green / CAUTION amber / AVOID red).
    Returns the (right_edge, bottom_edge) so the caller can position
    adjacent text. This is the SAME pill we use in every card so the
    visual language is consistent across the rotation."""
    verdict = (verdict or 'BUILD').upper()
    color = _verdict_color(verdict)
    font = _font(font_size)
    try:
        bbox = d.textbbox((0, 0), verdict, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
    except AttributeError:
        text_w, text_h = font.getsize(verdict)
    pill_w = text_w + pad_x * 2
    pill_h = text_h + pad_y * 2
    _rounded_rect(d, [(x, y), (x + pill_w, y + pill_h)], radius=pill_h // 2, fill=color)
    d.text((x + pad_x, y + pad_y - 4), verdict, font=font, fill=BG)
    return (x + pill_w, y + pill_h)


def _brand_chip(d, x, y, size=56):
    """DC HUB brand mark — a purple rounded-square chip with a white "DC"
    wordmark centered inside. Matches the favicon's visual weight better
    than a stylized lightning bolt (which became a thin slash at large
    chip sizes; the bolt polygon didn't scale). Always sits at top-left
    of the card so DC Hub branding is unmistakable on a thumb swipe even
    before the headline is read."""
    _rounded_rect(d, [(x, y), (x + size, y + size)],
                  radius=max(8, size // 6), fill=PURPLE)
    # "DC" wordmark inside the chip — tight bold, proportionally sized
    font_size = max(int(size * 0.42), 14)
    f = _font(font_size)
    try:
        bbox = d.textbbox((0, 0), 'DC', font=f)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
    except AttributeError:
        text_w, text_h = f.getsize('DC')
    # Center the text inside the chip
    tx = x + (size - text_w) // 2
    ty = y + (size - text_h) // 2 - int(size * 0.05)  # nudge up slightly
    d.text((tx, ty), 'DC', font=f, fill=WHITE)


def _subtle_gradient(img, top_color, bottom_color, falloff=1.0):
    """Linear top-to-bottom gradient on the existing image — used VERY
    sparingly. Subtle, not the loud purple→orange of the v1 fallback.
    falloff=1.0 = full canvas; 0.5 = only top half graduates.
    Implemented as per-line .line() ImageDraw rather than np for zero deps."""
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    bands = int(H * falloff)
    for i in range(bands):
        t = i / max(bands - 1, 1)
        r = int(top_color[0] + (bottom_color[0] - top_color[0]) * t)
        g = int(top_color[1] + (bottom_color[1] - top_color[1]) * t)
        b = int(top_color[2] + (bottom_color[2] - top_color[2]) * t)
        d.line([(0, i), (W, i)], fill=(r, g, b))


def _draw_brand_strip(d, y_top=0, y_bot=8):
    """Thin purple accent strip — typically across the very top of the
    canvas. Visual signature that ties all 4 styles together. Replaces
    the old chunky 60px orange bar with something more refined."""
    d.rectangle([(0, y_top), (W, y_bot)], fill=PURPLE)


def _draw_brand_footer(d, y, mark='dchub.cloud', date_str=None, kicker='DC HUB MEDIA'):
    """Standardized footer block. Brand monogram on the LEFT, kicker text
    aligned, URL on the RIGHT. Replaces the inconsistent 'dchub.cloud · DC Hub Daily Index'
    / '→ dchub.cloud/news' / 'dchub.cloud · DC Hub Media · Daily Power Index' strings
    that varied across styles in v1."""
    _brand_chip(d, 60, y - 10, size=52)
    d.text((130, y - 4),     kicker, font=_font(20),               fill=PURPLE_LT)
    d.text((130, y + 20),    mark,   font=_mono(18),                fill=MUTED)
    if date_str:
        try:
            bbox = d.textbbox((0, 0), date_str, font=_mono(20))
            tw = bbox[2] - bbox[0]
        except AttributeError:
            tw = len(date_str) * 12
        d.text((W - tw - 60, y + 6), date_str, font=_mono(20), fill=CYAN)


def _text_size(d, text, font):
    """Measure rendered text — handles old/new Pillow APIs uniformly."""
    try:
        bbox = d.textbbox((0, 0), text, font=font)
        return (bbox[2] - bbox[0], bbox[3] - bbox[1])
    except AttributeError:
        return font.getsize(text)


# ---------------------------------------------------------------------------
# Style 1: data_brutal — Bloomberg-terminal hero stat
# ---------------------------------------------------------------------------

def _draw_data_brutal(pr):
    """v2 (2026-06-05) — premium Bloomberg-terminal composition.

    Layout (1200x630):
      ┌─────────────────────────────────────────────────────────┐
      │ ▔▔▔▔▔ purple accent strip (8px)                          │
      │  DCPI · LIVE · DAILY                       Jun 05, 2026   │
      │                                                           │
      │  CHEYENNE                                  [BUILD pill]   │
      │   69.5                                                    │
      │  ── purple underline ──                                   │
      │  EXCESS POWER · #1 NATIONALLY                             │
      │                                                           │
      │  ◆ DC HUB MEDIA      dchub.cloud/dcpi      → today       │
      └─────────────────────────────────────────────────────────┘

    Whitespace is the new feature. The ugly hard-edged orange brand bar
    is gone; a thin purple line replaces it. The big score is now
    accent-COLORED purple (was ACCENT/orange), with a 4px purple
    underline that visually anchors the headline number — small detail,
    huge readability win on a LinkedIn thumb-scroll."""
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)

    # Brand accent strip + kicker row
    _draw_brand_strip(d)
    d.text((60, 32), 'DCPI  ·  LIVE  ·  DAILY POWER INDEX',
           font=_mono(22), fill=CYAN)
    date_str = _safe_date_str(pr.get('date'), '%b %d, %Y').upper()
    dtw, _ = _text_size(d, date_str, _mono(22))
    d.text((W - dtw - 60, 32), date_str, font=_mono(22), fill=MUTED)

    # Extract the hero stat from signals
    signals = pr.get('signals', {})
    top = (signals.get('top_build_markets') or [{}])[0] if isinstance(signals, dict) else {}
    if not isinstance(top, dict): top = {}
    market_name = _market_name_of(top)
    score = _market_score_of(top)
    if market_name == '?':
        # Fall back to parsing the title
        title = pr.get('title', '')
        if ' Tops ' in title:
            market_name = title.split(' Tops ')[0]
        elif ' Leads ' in title:
            market_name = title.split(' Leads ')[0]
        else:
            market_name = title.split(',')[0] if ',' in title else title[:30]
        import re
        m = re.search(r'(\d+\.\d+)', title)
        if m: score = float(m.group(1))

    # Market name — tight bold display, all caps, white
    d.text((60, 110), market_name.upper()[:22], font=_font(72), fill=TEXT)

    # Hero number — massive, brand purple, monospace.
    # Position the underline using FONT SIZE (em-box), not textbbox —
    # textbbox returns the tight glyph bbox which doesn't include the
    # font's descender region, so a measurement-based underline cuts
    # THROUGH the number. font.size + 0.10 padding clears all descenders.
    if score:
        score_str = f'{score:.1f}'
        FONT_PT = 240
        score_y = 178
        score_font = _mono(FONT_PT)
        d.text((60, score_y), score_str, font=score_font, fill=PURPLE)
        # Em-box bottom = top + (font_pt * roughly 1.0); add 8% breathing
        underline_y = score_y + int(FONT_PT * 1.08)
        sw, _ = _text_size(d, score_str, score_font)
        d.rectangle([(60, underline_y), (60 + min(sw, 720), underline_y + 5)],
                    fill=PURPLE_LT)
        d.text((60, underline_y + 22), 'EXCESS POWER  ·  #1 NATIONALLY',
               font=_mono(22), fill=MUTED)
    else:
        # No score → render the subheadline at large display size instead
        sub = pr.get('subheadline', '') or pr.get('title', '')
        for i, line in enumerate(_wrap(sub, 28)[:4]):
            d.text((60, 220 + i * 76), line, font=_font(54), fill=TEXT)

    # Verdict pill — top-right, color-coded
    verdict = _verdict_for(signals)
    _verdict_pill(d, x=W - 280, y=110, verdict=verdict, font_size=40)

    # Footer
    _draw_brand_footer(d, y=H - 64, mark='dchub.cloud/dcpi',
                       kicker='DC HUB MEDIA  ·  AUTONOMOUS PRESS')

    return img


# ---------------------------------------------------------------------------
# Style 2: editorial — gradient bg + clean magazine typography
# ---------------------------------------------------------------------------

def _draw_editorial(pr):
    """v2 (2026-06-05) — premium magazine editorial.

    Layout (1200x630):
      ┌─────────────────────────────────────────────────────────┐
      │ ▔▔▔▔▔ purple accent strip                                │
      │                                                           │
      │  ◆ DC HUB MEDIA  ·  JUN 05               [BUILD pill]    │
      │                                                           │
      │  HEADLINE GOES HERE                                       │
      │  WRAPS UP TO THREE LINES                                  │
      │  WITH GENEROUS LEADING                                    │
      │                                                           │
      │   ─── thin cyan rule ───                                  │
      │   Supporting subheadline copy in muted slate text.        │
      │                                                           │
      │  ◆ DC HUB MEDIA      dchub.cloud/news     → today        │
      └─────────────────────────────────────────────────────────┘

    Cleaner than v1's purple→orange gradient (which read as a sunset, not
    a publication). v2 uses a subtle navy-to-deeper-navy gradient ONLY
    in the bottom third, so the top stays clean and high-contrast.
    Headline grew 72→76pt, switched to brand-purple kicker, added a
    cyan separator rule above the subhead to give it editorial weight."""
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (W, H), BG)
    # Subtle gradient — only the bottom 40%, navy → deeper navy
    _subtle_gradient(img, BG, BG_DEEP, falloff=1.0)
    d = ImageDraw.Draw(img)

    _draw_brand_strip(d)

    # Kicker row — diamond + DC HUB MEDIA + dot + date, in cyan
    date_full = _safe_date_str(pr.get('date'), '%b %d, %Y').upper()
    kicker = f'◆  DC HUB MEDIA  ·  {date_full}'
    d.text((64, 56), kicker, font=_font(22), fill=CYAN)

    # Verdict pill — top-right, if signals carry one
    signals = pr.get('signals', {}) if isinstance(pr.get('signals', {}), dict) else {}
    if signals.get('top_build_markets'):
        verdict = _verdict_for(signals)
        _verdict_pill(d, x=W - 280, y=44, verdict=verdict, font_size=36, pad_x=32, pad_y=14)

    # Headline — 3 lines max, tight bold display
    title = pr.get('title', '')[:200]
    lines = _wrap(title, 24)[:3]
    y = 130
    for line in lines:
        d.text((64, y), line, font=_font(74), fill=WHITE)
        y += 92

    # Cyan separator rule + subheadline
    sub = pr.get('subheadline', '')
    if sub:
        sep_y = max(y + 14, 412)
        d.rectangle([(64, sep_y), (124, sep_y + 3)], fill=CYAN)
        sublines = _wrap(sub, 60)[:2]
        sy = sep_y + 20
        for s in sublines:
            d.text((64, sy), s, font=_font(26, bold=False), fill=MUTED)
            sy += 38

    # Footer
    _draw_brand_footer(d, y=H - 64, mark='dchub.cloud/news',
                       kicker='DC HUB MEDIA  ·  EDITORIAL')

    return img


# ---------------------------------------------------------------------------
# Style 3: infographic — bar chart of top 5 markets
# ---------------------------------------------------------------------------

def _draw_infographic(pr):
    """v2 (2026-06-05) — premium chart card.

    Layout (1200x630):
      ┌──────────────────────────────────────────────────────────┐
      │ ▔▔▔▔▔ purple accent strip                                 │
      │  TOP 5 BUILD MARKETS  ·  EXCESS POWER       JUN 05 LIVE   │
      │                                                            │
      │  CHEYENNE         ▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰  69.5  ▲           │
      │  MONTRÉAL         ▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰    65.2              │
      │  LA VISTA         ▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰     59.0              │
      │  LENEXA           ▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰     59.0              │
      │  OKLAHOMA CITY    ▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰     59.0              │
      │                                                            │
      │  ◆ DC HUB MEDIA      [BUILD]  Cheyenne #1 nationally      │
      └──────────────────────────────────────────────────────────┘

    Upgrades over v1:
    - Rounded bars (rounded_rectangle radius 6) instead of hard edges
    - #1 bar in brand purple, #2-5 graded through brand-purple-light
    - Right-aligned market names in tight bold caps; left-aligned scores
      at the bar tail (was a non-anchored single rendering — gone now)
    - Verdict pill at footer is rounded too, color-coded
    - Removed the chunky 96px header strip — replaced with a 22pt
      kicker on the canvas itself for proper editorial weight
    """
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)

    _draw_brand_strip(d)

    # Header / kicker row
    d.text((60, 36), 'TOP 5 BUILD MARKETS  ·  EXCESS POWER',
           font=_font(30), fill=WHITE)
    date_str = _safe_date_str(pr.get('date'), '%b %d').upper() + '  ·  LIVE'
    dtw, _ = _text_size(d, date_str, _mono(22))
    d.text((W - dtw - 60, 44), date_str, font=_mono(22), fill=CYAN)

    # Pull top 5 from signals (support both production key set + legacy)
    signals = pr.get('signals', {}) if isinstance(pr.get('signals', {}), dict) else {}
    top_5 = (signals.get('top_build_markets') or [])[:5]
    top_5 = [m for m in top_5 if isinstance(m, dict)]
    if not top_5:
        title = pr.get('title', '')
        import re
        score = 0
        m = re.search(r'(\d+\.\d+)', title)
        if m: score = float(m.group(1))
        name = title.split(' Tops ')[0] if ' Tops ' in title else 'Top Market'
        top_5 = [{'market': name, 'excess': score, 'verdict': 'BUILD'}]

    max_score = max([_market_score_of(m) for m in top_5] + [1.0])

    # Bar layout — slightly taller bars + tighter gap
    y_start = 130
    bar_h = 60
    gap = 22
    label_col_x = 360
    bar_start_x = 380
    bar_max_x = W - 220

    # Color gradient: #1 purple, then graded through purple-light to slate
    bar_colors = [PURPLE, PURPLE_LT, (130, 105, 215), (110, 95, 190), (95, 85, 165)]

    for i, m in enumerate(top_5):
        y = y_start + i * (bar_h + gap)
        name = _market_name_of(m).upper()[:22]
        score = _market_score_of(m)
        is_top = (i == 0)
        color = bar_colors[min(i, len(bar_colors) - 1)]

        # Market name — right-aligned in left gutter, tight bold caps
        try:
            d.text((label_col_x - 20, y + bar_h // 2), name,
                   font=_font(28), fill=TEXT, anchor='rm')
        except TypeError:
            tw, th = _text_size(d, name, _font(28))
            d.text((label_col_x - 20 - tw, y + (bar_h - th) // 2),
                   name, font=_font(28), fill=TEXT)

        # Rounded bar
        bar_w = int((score / max_score) * (bar_max_x - bar_start_x))
        _rounded_rect(d, [(bar_start_x, y), (bar_start_x + bar_w, y + bar_h)],
                      radius=8, fill=color)

        # Score at bar tail
        d.text((bar_start_x + bar_w + 16, y + bar_h // 2 - 18),
               f'{score:.1f}', font=_font(32), fill=WHITE)

        # #1 indicator — "#1" label in green, no Unicode glyph deps
        if is_top:
            d.text((W - 90, y + bar_h // 2 - 16), '#1',
                   font=_font(28), fill=GREEN)

    # Footer with verdict pill
    verdict = _verdict_for(signals)
    _verdict_pill(d, x=60, y=H - 100, verdict=verdict, font_size=32, pad_x=28, pad_y=12)
    if top_5:
        first = _market_name_of(top_5[0])[:30]
        d.text((290, H - 88), f'{first}  ·  ranked #1 nationally',
               font=_font(24), fill=TEXT)
    d.text((290, H - 54), 'dchub.cloud/dcpi  ·  DC Hub Media',
           font=_mono(18), fill=MUTED)

    return img


# ---------------------------------------------------------------------------
# Style 4: ai_hero — placeholder for SDXL (Workers AI). Falls back to
# editorial for now. Phase HH+1 wires Cloudflare Workers AI.
# ---------------------------------------------------------------------------

# Phase JJ batch 4 (2026-05-14): real AI hero via Cloudflare Workers AI SDXL.
# Generates a topical 1024x1024 image from the press release title +
# topic, then composites the headline + brand chip + CTA on top.
#
# Env-gated. If CF_ACCOUNT_ID + CF_API_TOKEN aren't set on Railway, falls
# back to the gradient placeholder. Per-(slug, day) cached in-process so
# popular posts don't regenerate (or pay) for every LinkedIn scrape.
#
# Requires: CF API token with "Workers AI - Read" permission on the
# DC Hub account. Generation cost ~$0.0003/image, latency 5-10s.

_AI_IMAGE_CACHE = {}             # (slug, yyyymmdd) → png bytes
_AI_IMAGE_CACHE_MAX = 50

def _generate_workers_ai_image(prompt: str, slug: str, variant: int = 0):
    """Hit Cloudflare Workers AI SDXL endpoint. Returns PNG bytes or None
    if creds missing / API errored. Cached per (slug, day, variant) so the
    review-retry loop can request a genuinely different image for variant>0."""
    cache_key = (slug, datetime.datetime.utcnow().strftime('%Y%m%d'), variant)
    if cache_key in _AI_IMAGE_CACHE:
        return _AI_IMAGE_CACHE[cache_key]

    # Accept either the dedicated CF_* vars or the broader CLOUDFLARE_* vars
    # (the account often already has CLOUDFLARE_API_TOKEN/ACCOUNT_ID set for
    # Pages/Wrangler). The token must have the "Workers AI" permission.
    account_id = (os.environ.get('CF_ACCOUNT_ID')
                  or os.environ.get('CLOUDFLARE_ACCOUNT_ID') or '')
    api_token  = (os.environ.get('CF_API_TOKEN')
                  or os.environ.get('CLOUDFLARE_API_TOKEN') or '')
    if not (account_id and api_token):
        return None  # Not configured — caller falls back to the gradient card

    try:
        import requests as _rq
        # SDXL on Workers AI returns binary PNG when format is set right.
        url = (f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
               f"/ai/run/@cf/stabilityai/stable-diffusion-xl-base-1.0")
        # variant>0 nudges the composition so a review-retry yields a genuinely
        # different image, not the same dud regenerated.
        _nudge = ["", ", dramatic wide-angle composition, volumetric golden light",
                  ", elevated aerial perspective, moody overcast sky"][variant % 3]
        resp = _rq.post(
            url,
            json={
                "prompt": (prompt[:1400] + _nudge)[:1500],
                # Negative prompt (2026-06-06) kills the common SDXL duds that
                # made some heroes look off: garbled text, watermarks/logos,
                # blur, deformed structures, cartoonish/oversaturated output.
                "negative_prompt": ("text, words, letters, watermark, logo, signature, "
                                    "blurry, low quality, jpeg artifacts, distorted, deformed, "
                                    "ugly, oversaturated, cartoon, illustration, frame, border, people, faces"),
                # Wider aspect to better match our 1200x630 final canvas
                "width": 1024, "height": 576,
                "num_steps": 20,           # 20 is the sweet spot for SDXL
                "guidance": 7.5,
            },
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"[ai_hero] CF Workers AI {resp.status_code}: {resp.text[:200]}")
            return None
        png_bytes = resp.content
        # Sanity check — PNG magic header
        if not png_bytes.startswith(b'\x89PNG'):
            return None
        _AI_IMAGE_CACHE[cache_key] = png_bytes
        # Cap cache
        if len(_AI_IMAGE_CACHE) > _AI_IMAGE_CACHE_MAX:
            oldest = min(_AI_IMAGE_CACHE)
            _AI_IMAGE_CACHE.pop(oldest, None)
        return png_bytes
    except Exception as e:
        print(f"[ai_hero] generation failed: {e}")
        return None


def _build_sdxl_prompt(pr: dict) -> str:
    """Compose an SDXL prompt from the press release. Aim for atmospheric,
    technical, infrastructure-themed images that pair with DC Hub's voice.
    """
    title = (pr.get('title') or '').strip()
    sub   = (pr.get('subheadline') or '').strip()
    topic = (pr.get('topic') or 'data center infrastructure').strip()
    # Extract the geographic anchor from title if present
    geo_hint = ''
    for state_marker in [', WY', ', TX', ', VA', ', CA', ', AZ', ', GA',
                         ' WY ', ' TX ', ' VA ', ' CA ']:
        if state_marker in title:
            geo_hint = 'mountainous high desert' if 'WY' in state_marker else (
                'industrial Texas plains' if 'TX' in state_marker else
                'mid-Atlantic woodland' if 'VA' in state_marker else
                'California coastal' if 'CA' in state_marker else '')
            break
    return (
        f"Cinematic editorial photograph of a modern data center facility, "
        f"{geo_hint or 'wide American landscape'}, evening golden-hour light, "
        f"transmission lines and substations on the horizon, dramatic sky, "
        f"high contrast, photorealistic, no text, no watermarks, no logos, "
        f"shot on 35mm, depth of field, hyper-detailed. "
        f"Theme: {topic[:80]}. Subject: {title[:140]}"
    )


def _ai_review_ok(png_bytes: bytes) -> bool:
    """Opt-in (DCHUB_MEDIA_AI_REVIEW=1) vision check: ask a cheap Claude model
    whether the generated image is a clean, photographic, on-brand
    infrastructure/landscape image — NOT a garbled SDXL dud. Fail-OPEN (returns
    True) when review is off, no API key, or any error, so it never blocks a post."""
    if os.environ.get("DCHUB_MEDIA_AI_REVIEW", "").lower() not in ("1", "true", "yes"):
        return True
    api_key = (os.environ.get("ANTHROPIC_API_KEY")
               or os.environ.get("DCHUB_ANTHROPIC_API_KEY") or "")
    if not api_key or not png_bytes:
        return True
    try:
        import base64
        import urllib.request as _u
        from utils.anthropic_helper import anthropic_messages_url
        b64 = base64.b64encode(png_bytes).decode()
        body = json.dumps({
            "model": "claude-haiku-4-5",   # cheap + fast vision judge
            "max_tokens": 8,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": "image/png", "data": b64}},
                {"type": "text", "text":
                 "This is an auto-generated background photo for a data-center "
                 "intelligence brand. Is it a clean, photographic, on-brand "
                 "infrastructure or landscape image with NO garbled text and no "
                 "obvious AI artifacts? Answer only GOOD or BAD."},
            ]}],
        }).encode()
        req = _u.Request(anthropic_messages_url(), data=body, headers={
            "Content-Type": "application/json", "X-API-Key": api_key,
            "anthropic-version": "2023-06-01"})
        with _u.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
        txt = "".join(p.get("text", "") for p in d.get("content", [])).strip().upper()
        ok = "BAD" not in txt
        if not ok:
            print("[ai_hero] vision review flagged image BAD → retrying a variant")
        return ok
    except Exception as e:
        print(f"[ai_hero] review skipped ({e}) — fail-open GOOD")
        return True


def _draw_ai_hero(pr):
    """Phase JJ (2026-05-14): real AI-generated hero via CF Workers AI
    SDXL when CF_ACCOUNT_ID + CF_API_TOKEN are set. Falls back to the
    polished gradient placeholder (batch 2 typography: 78pt headline,
    4px drop shadow, deep purple → vivid orange gradient).
    """
    from PIL import Image, ImageDraw
    slug = pr.get('slug', '')
    if not slug:
        # Try to derive a slug from the title for cache keying
        slug = (pr.get('title') or 'unknown').lower().replace(' ', '-')[:60]

    _prompt = _build_sdxl_prompt(pr)
    ai_png = _generate_workers_ai_image(_prompt, slug, 0)
    # Review-retry (opt-in via DCHUB_MEDIA_AI_REVIEW): if the first image fails
    # the vision check, take ONE fresh variant. Bounded → at most 2 gens.
    if ai_png and not _ai_review_ok(ai_png):
        ai_png = _generate_workers_ai_image(_prompt, slug, 1) or ai_png
    if ai_png:
        # Real AI image — composite headline overlay
        from io import BytesIO
        bg = Image.open(BytesIO(ai_png)).convert('RGB')
        # SDXL gives us 1024x576; resize/crop to our 1200x630 canvas
        # while preserving aspect ratio as much as possible.
        bg = bg.resize((W, int(W * bg.height / bg.width)))
        # Top-crop or pad to 630
        if bg.height > H:
            top = (bg.height - H) // 2
            bg = bg.crop((0, top, W, top + H))
        elif bg.height < H:
            canvas = Image.new('RGB', (W, H), (10, 14, 26))
            canvas.paste(bg, (0, (H - bg.height) // 2))
            bg = canvas

        img = bg
        d = ImageDraw.Draw(img)

        # Strong bottom gradient — protects ALL text below the midline.
        # v2 (2026-06-05): alpha bumped 180→220 and gradient starts
        # higher (40% vs 50%) so headline+pill+CTA all sit on safe contrast.
        overlay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        start_y = int(H * 0.40)
        for i in range(start_y, H):
            alpha = int(220 * ((i - start_y) / (H - start_y)))
            odraw.line([(0, i), (W, i)], fill=(0, 0, 0, alpha))
        img = Image.alpha_composite(img.convert('RGBA'), overlay).convert('RGB')
        d = ImageDraw.Draw(img)

        # Brand accent strip (purple) — same anchor as all other styles
        _draw_brand_strip(d)

        # Brand chip + DC HUB MEDIA label (top-left, like every other style)
        _brand_chip(d, 60, 56, size=52)
        d.text((130, 56),  'DC HUB MEDIA', font=_font(20), fill=PURPLE_LT)
        d.text((130, 84),  _safe_date_str(pr.get('date'), '%b %d, %Y').upper(),
               font=_mono(16), fill=CYAN)

        # Verdict pill — top-right if applicable
        signals = pr.get('signals', {}) if isinstance(pr.get('signals', {}), dict) else {}
        if signals.get('top_build_markets'):
            verdict = _verdict_for(signals)
            _verdict_pill(d, x=W - 280, y=44, verdict=verdict,
                          font_size=36, pad_x=32, pad_y=14)

        # Headline at the bottom — no double-shadow hack, just clean
        # white on the gradient-darkened SDXL backdrop.
        title = pr.get('title', '')[:120]
        hf = _font(60)
        lines = _wrap_px(title, hf, max_w=W - 120, max_lines=3)
        line_height = 76
        total_height = line_height * len(lines)
        y_start = H - total_height - 100
        for line in lines:
            d.text((60, y_start), line, font=hf, fill=WHITE)
            y_start += line_height

        # Footer
        _draw_brand_footer(d, y=H - 64, mark='dchub.cloud/news',
                           kicker='DC HUB MEDIA  ·  AUTONOMOUS PRESS')

        return img

    # Fallback when SDXL isn't configured — premium brand panel (no garish
    # sunset gradient). v2 (2026-06-05): replaces the purple→orange "sunrise"
    # the user explicitly flagged as lame. Now it's a calm deep-navy canvas
    # with a SINGLE large brand glyph + headline + verdict pill.
    img = Image.new('RGB', (W, H), BG)
    _subtle_gradient(img, BG, BG_DEEP, falloff=1.0)
    d = ImageDraw.Draw(img)

    _draw_brand_strip(d)

    # Hero brand mark — oversized chip on the LEFT (decorative anchor)
    _brand_chip(d, 60, 130, size=240)
    # "DC HUB" wordmark right of the giant chip
    d.text((330, 168), 'DC HUB', font=_font(72), fill=WHITE)
    d.text((330, 248), 'MEDIA  ·  DAILY POWER INDEX',
           font=_mono(22), fill=CYAN)

    # Verdict pill — only if signals present
    signals = pr.get('signals', {}) if isinstance(pr.get('signals', {}), dict) else {}
    if signals.get('top_build_markets'):
        verdict = _verdict_for(signals)
        _verdict_pill(d, x=W - 280, y=44, verdict=verdict, font_size=36, pad_x=32, pad_y=14)

    # Headline — sits BELOW the brand block, no overlap, no shadows needed.
    # Pixel-wrapped (not char-count) so the long deal-headline titles wrap
    # cleanly instead of running off the right edge.
    title = pr.get('title', '')[:140]
    hf = _font(44)
    lines = _wrap_px(title, hf, max_w=W - 120, max_lines=3)
    y = 408
    for line in lines:
        d.text((60, y), line, font=hf, fill=TEXT)
        y += 58

    # Footer
    _draw_brand_footer(d, y=H - 64, mark='dchub.cloud/news',
                       kicker='DC HUB MEDIA  ·  AUTONOMOUS PRESS',
                       date_str=_safe_date_str(pr.get('date'), '%b %d, %Y').upper())

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


# ─────────────────────────────────────────────────────────────────────
# Phase GG (2026-05-14): smart_style() — DC Hub Media as an independent
# intelligent worker.
#
# todays_style() is a FIXED weekday rotation: it never learns. smart_style()
# closes the loop — it reads how each form factor has actually performed
# (click-through on the press releases it ran) and uses epsilon-greedy
# selection: most of the time it picks the measured best performer, but
# EXPLORE_RATE of the time it deliberately picks a different one so every
# form factor keeps accumulating data and one lucky post can't permanently
# lock out the rest. When there isn't enough engagement data yet, it falls
# back cleanly to the deterministic weekday rotation.
#
# The choice is seeded by the UTC date so it's STABLE within a day (the
# og:image must not flicker between requests) but adapts day to day.
# ─────────────────────────────────────────────────────────────────────

import random as _random

_OG_EXPLORE_RATE = float(os.environ.get('DCHUB_OG_EXPLORE_RATE', '0.30'))
_OG_SMART_MIN_POSTS_PER_STYLE = int(os.environ.get('DCHUB_OG_MIN_POSTS', '2'))
_OG_SMART_MIN_TOTAL_VIEWS = int(os.environ.get('DCHUB_OG_MIN_VIEWS', '20'))


def _style_performance():
    """Per-form-factor engagement over the last 60 days.

    Returns {style: {'views':int,'clicks':int,'posts':set(slugs),
                     'li_impressions':int,'li_engagements':int,'li_posts':int}}
    — or {} on any DB hiccup. The form factor a press release ran is derived
    from its publish weekday via DAILY_STYLES (same mapping og_performance uses).

    LinkedIn factor (2026-06-05): joins linkedin_posts by slug for the same
    window. Falls back gracefully (li_* counters stay 0) when linkedin_posts
    has no slug column yet (Phase A+B not landed) or no impressions data.
    Best-effort: never raises.
    """
    try:
        import psycopg2
        conn = psycopg2.connect(os.environ.get('DATABASE_URL'), connect_timeout=8)
    except Exception:
        return {}
    agg = {}
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT a.slug, a.generated_at, e.event_type, COUNT(e.id)
                FROM auto_press_releases a
                LEFT JOIN press_engagement e ON e.slug = a.slug
                WHERE a.generated_at > NOW() - INTERVAL '60 days'
                GROUP BY a.slug, a.generated_at, e.event_type
            """)
            for slug, gen_at, event_type, n in cur.fetchall():
                if not gen_at:
                    continue
                style = DAILY_STYLES.get(gen_at.weekday(), 'data_brutal')
                b = agg.setdefault(style, {
                    'views': 0, 'clicks': 0, 'posts': set(),
                    'li_impressions': 0, 'li_engagements': 0, 'li_posts': 0,
                })
                b['posts'].add(slug)
                if event_type == 'view':
                    b['views'] += int(n or 0)
                elif event_type in ('click_out', 'stripe_click'):
                    b['clicks'] += int(n or 0)

            # LinkedIn engagement factor — joined on slug. Fail-soft: if the
            # linkedin_posts.slug column doesn't exist yet (pre Phase A+B URN
            # capture), skip the LinkedIn factor entirely. Same for missing
            # impressions/shares columns.
            try:
                # Pre-flight (2026-06-08): linkedin_posts.slug may not exist yet
                # (pre URN-capture). Check the column FIRST so the JOIN below does
                # not throw UndefinedColumn on every run — that was spamming the
                # live logs: "[smart-style] LinkedIn factor skipped: UndefinedColumn
                # column p.slug does not exist". When absent, skip cleanly.
                cur.execute("""SELECT 1 FROM information_schema.columns
                                WHERE table_name='linkedin_posts'
                                  AND column_name='slug' LIMIT 1""")
                if cur.fetchone():
                    cur.execute("""
                        SELECT a.slug, a.generated_at,
                               COALESCE(p.impressions, 0),
                               COALESCE(p.likes, 0),
                               COALESCE(p.comments, 0),
                               COALESCE(p.shares, 0)
                          FROM auto_press_releases a
                          LEFT JOIN linkedin_posts p ON p.slug = a.slug
                         WHERE a.generated_at > NOW() - INTERVAL '60 days'
                           AND p.impressions IS NOT NULL
                    """)
                    for slug, gen_at, imps, likes, comments, shares in cur.fetchall():
                        if not gen_at:
                            continue
                        style = DAILY_STYLES.get(gen_at.weekday(), 'data_brutal')
                        b = agg.setdefault(style, {
                            'views': 0, 'clicks': 0, 'posts': set(),
                            'li_impressions': 0, 'li_engagements': 0, 'li_posts': 0,
                        })
                        b['li_impressions'] += int(imps or 0)
                        b['li_engagements'] += int((likes or 0) + (comments or 0) + (shares or 0))
                        b['li_posts'] += 1
            except Exception as li_err:
                # linkedin_posts.slug / impressions / shares column missing,
                # or table doesn't exist — degrade gracefully. Operators see
                # the skip in stderr; pick still works on pure site CTR.
                import sys as _sys
                print(f"[smart-style] LinkedIn factor skipped: {type(li_err).__name__}: {str(li_err)[:160]}",
                      file=_sys.stderr)
    except Exception:
        return {}
    finally:
        try: conn.close()
        except Exception: pass
    return agg


def smart_style():
    """Performance-aware form-factor pick. Falls back to todays_style()
    until there's enough engagement data to judge.

    Combined score (2026-06-05): site CTR + LinkedIn engagement rate, weighted
    by DCHUB_STYLE_SITE_WEIGHT (default 0.60) and DCHUB_STYLE_LI_WEIGHT
    (default 0.40), both read at call time so they can be tweaked without
    redeploy. Styles with no LinkedIn data are scored on pure site CTR
    (no penalty for newer styles)."""
    try:
        agg = _style_performance()
    except Exception:
        return todays_style()

    eligible = {s: b for s, b in agg.items()
                if len(b['posts']) >= _OG_SMART_MIN_POSTS_PER_STYLE and b['views'] > 0}
    total_views = sum(b['views'] for b in agg.values())
    if len(eligible) < 2 or total_views < _OG_SMART_MIN_TOTAL_VIEWS:
        # Not enough signal yet — deterministic rotation keeps coverage even.
        return todays_style()

    # Deterministic-per-day RNG so the card is stable within a UTC day.
    day = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    rng = _random.Random('og-smart-' + day)
    all_styles = list(STYLE_MAP.keys())

    if rng.random() < _OG_EXPLORE_RATE:
        # Explore — pick uniformly so every form factor keeps gathering data.
        return rng.choice(all_styles)

    # Exploit — best COMBINED score (site CTR + LinkedIn engagement). Env
    # tunables read here so an operator can shift the mix without redeploy.
    site_w = float(os.environ.get('DCHUB_STYLE_SITE_WEIGHT', '0.60'))
    li_w = float(os.environ.get('DCHUB_STYLE_LI_WEIGHT', '0.40'))

    def _combined(b):
        site_ctr = (b['clicks'] / b['views']) if b['views'] > 0 else 0.0
        li_imps = b.get('li_impressions', 0)
        if li_imps > 0:
            li_eng_rate = b.get('li_engagements', 0) / li_imps
            # Normalize LinkedIn eng rate to 0..1; ~10% is exceptional, clamp.
            li_norm = min(li_eng_rate * 10.0, 1.0)
            return site_w * site_ctr + li_w * li_norm, site_ctr, li_eng_rate
        # No LinkedIn signal for this style yet — pure site CTR, no penalty.
        return site_ctr, site_ctr, 0.0

    scored = {s: _combined(b) for s, b in eligible.items()}
    best = max(scored.items(), key=lambda kv: kv[1][0])
    style, (score, site_ctr, li_eng_rate) = best

    # Stderr log so operators can see the math behind today's pick.
    try:
        import sys as _sys
        print(
            f"[smart-style] eligible={len(eligible)} picked={style} "
            f"score={score:.4f} site_ctr={site_ctr:.4f} li_eng_rate={li_eng_rate:.4f} "
            f"weights=site:{site_w:.2f}/li:{li_w:.2f}",
            file=_sys.stderr,
        )
    except Exception:
        pass

    return style


def _draw_fallback(slug):
    """Last-resort card if DB unavailable or generator throws. Never 404 —
    LinkedIn / Twitter aggressively drop link-card previews if og:image
    returns 4xx, and we want SOME card no matter what.

    v2 (2026-06-05): rebuilt to match the v2 brand language (deep navy,
    brand chip, cyan kicker) so the fallback no longer looks like an
    error page. It IS the worst-case render, so it has to still look
    intentional and premium."""
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (W, H), BG)
    _subtle_gradient(img, BG, BG_DEEP, falloff=1.0)
    d = ImageDraw.Draw(img)

    _draw_brand_strip(d)

    # Hero brand mark centered-left
    _brand_chip(d, 60, 140, size=200)
    d.text((300, 168), 'DC HUB', font=_font(78), fill=WHITE)
    d.text((300, 256), 'DATA CENTER INTELLIGENCE', font=_mono(22), fill=CYAN)

    # Pillars
    d.text((60, 400), 'POWER  ·  PIPELINE  ·  PRICING  ·  ISO GRID',
           font=_mono(22), fill=MUTED)
    d.text((60, 442), 'Live since 2024  ·  21,400 facilities  ·  170+ countries',
           font=_font(22), fill=TEXT)

    _draw_brand_footer(d, y=H - 64, mark='dchub.cloud',
                       kicker='DC HUB  ·  CITED BY CLAUDE, CHATGPT, PERPLEXITY')

    return img


@og_cards_bp.route('/api/v1/og/<style>/<slug>.png', methods=['GET'])
def og_card(style, slug):
    """Generate the PNG. `style=today` resolves to today's rotation.
    `slug` should match a press_releases.slug row.

    Phase HH+2 (2026-05-13): switched from <path:slug> to default
    <slug> converter. The path converter is greedy and was consuming
    the trailing '.png' as part of the slug variable, so DB lookups
    queried for 'auto-...-power.png' instead of 'auto-...-power'.
    Default string converter (`[^/]+`) handles dots in slug correctly
    while still treating `.png` as a literal route suffix.
    """
    from flask import request as _req
    # Debug mode: ?debug=1 returns JSON instead of PNG so we can see
    # what's happening in the lookup pipeline.
    debug = _req.args.get('debug') == '1'

    # Phase GG (2026-05-14): `today` now resolves through smart_style() —
    # the performance-aware, self-learning pick. `smart` is an explicit
    # alias; `rotation` forces the old fixed weekday rotation.
    if style in ('today', 'smart'):
        style = smart_style()
    elif style == 'rotation':
        style = todays_style()

    pr = _get_press_release(slug)
    if debug:
        # Try to actually render and capture any exception, so debug
        # mode shows us WHY the renderer falls through to the fallback.
        from flask import jsonify
        import traceback as _tb
        render_err = None
        if pr is not None:
            try:
                fn = STYLE_MAP.get(style, _draw_data_brutal)
                _ = fn(pr)
            except Exception as e:
                render_err = f"{type(e).__name__}: {str(e)[:300]}"
                tb = _tb.format_exc()
                render_err += "\n" + tb[-500:]
        return jsonify(
            style=style, slug=slug,
            pr_found=pr is not None,
            pr_title=(pr or {}).get('title'),
            pr_date_str=str((pr or {}).get('date')),
            has_signals=bool((pr or {}).get('signals')),
            signals_keys=list((pr or {}).get('signals') or {})[:10],
            top_build_first=(((pr or {}).get('signals') or {}).get('top_build_markets') or [{}])[0],
            todays_style=todays_style(),
            smart_style=smart_style(),
            render_error=render_err,
        )

    try:
        if pr is None:
            img = _draw_fallback(slug)
        else:
            fn = STYLE_MAP.get(style, _draw_data_brutal)
            img = fn(pr)
    except Exception as e:
        import traceback as _tb
        print(f"[og_cards] render error for {style}/{slug}: {e}\n{_tb.format_exc()}")
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


@og_cards_bp.route('/api/v1/og/dynamic.png', methods=['GET'])
def og_card_dynamic():
    """Render a PREMIUM card from query params — no stored press release needed.

    2026-06-06: every DC Hub Media LinkedIn post should ship the same rich,
    headline-driven card as auto-press. Before this, the quad-daily and daily
    digest publishers pointed at STATIC `landing-*.png` files that were frozen
    blank during the 2026-06-06 font-fallback bug — so 4 of 5 posts looked
    empty ("bush league"). This route lets ANY publisher build a live card
    from the post's actual headline + stat, rendered by the same (now
    font-fixed) draw functions that produce the editorial cards.

    Query params:
      style        editorial | data_brutal | infographic | today | smart
                   (default editorial — always populated, never blank)
      title        the headline (drives the card; required for a good card)
      subheadline  supporting line under the headline / cyan rule
      market       optional market name → data_brutal big-number + verdict pill
      score        optional float → the giant hero number (data_brutal)
      verdict      BUILD | CAUTION | AVOID → pill + color
      date         optional ISO date (defaults to today)
    """
    from flask import request as _req
    args = _req.args

    style = (args.get('style') or 'editorial').strip().lower()
    if style in ('today', 'smart'):
        style = smart_style()
    elif style == 'rotation':
        style = todays_style()

    # Date (date object so _safe_date_str's strftime path works)
    date_val = None
    ds = (args.get('date') or '').strip()
    if ds:
        try:
            date_val = datetime.date.fromisoformat(ds[:10])
        except Exception:
            date_val = None
    if date_val is None:
        date_val = datetime.datetime.utcnow().date()

    # Synthetic signals so the data_brutal hero number + verdict pill light up
    market = (args.get('market') or '').strip()
    score_raw = args.get('score')
    verdict = (args.get('verdict') or '').strip().upper()
    signals = {}
    if market or (score_raw not in (None, '')):
        try:
            sc = float(score_raw) if score_raw not in (None, '') else 0.0
        except Exception:
            sc = 0.0
        signals = {'top_build_markets': [{
            'market': market or (args.get('title') or '')[:30],
            'excess': sc,
            'verdict': verdict or 'BUILD',
        }]}

    _title = (args.get('title') or 'DC Hub Media').strip()[:200]
    # Per-content slug so the ai_hero SDXL cache (keyed by slug+day) produces a
    # DISTINCT image per headline — otherwise every dynamic AI card would share
    # one image for the whole day.
    import hashlib as _hl
    _slug = 'dyn-' + _hl.sha1((_title + '|' + (args.get('market') or '')).encode()).hexdigest()[:12]
    pr = {
        'title':       _title,
        'subheadline': (args.get('subheadline') or '').strip()[:300],
        'date':        date_val,
        'signals':     signals,
        'slug':        _slug,
        'topic':       (args.get('topic') or '').strip()[:80],
    }

    try:
        fn = STYLE_MAP.get(style, _draw_editorial)
        img = fn(pr)
    except Exception as e:
        import traceback as _tb
        print(f"[og_cards] dynamic render error ({style}): {e}\n{_tb.format_exc()}")
        img = _draw_fallback('dynamic')

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    buf.seek(0)
    return Response(
        buf.read(),
        mimetype='image/png',
        headers={
            # Unique param-sets → unique cards; cache a day at the edge so
            # LinkedIn's scraper gets a stable image per post.
            'Cache-Control': 'public, max-age=86400, s-maxage=86400',
            'X-DC-Card-Style': style,
            'X-DC-Card-Dynamic': '1',
        },
    )


def register_og_cards(app):
    app.register_blueprint(og_cards_bp)
    app.logger.info("✓ OG cards registered: GET /api/v1/og/<style>/<slug>.png")
    app.logger.info("✓ OG dynamic card: GET /api/v1/og/dynamic.png?title=…&style=…")
    app.logger.info(f"  Today's rotation style: {todays_style()}")
