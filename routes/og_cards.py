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
from utc_clock import utc_now

og_cards_bp = Blueprint('og_cards', __name__)

# LinkedIn/Twitter/OG standard
W, H = 1200, 630

# DC Hub brand palette — synced to dchub-brand.css (2026-06-05 visual rebuild).
# Was: orange-on-navy. Now: deep navy + brand purple + cyan accents + green/red/amber verdicts.
# 2026-09-05: the note below claimed the purple/cyan combo 'matches the site'. It did
# not — measured against the live stylesheet, not one token agreed. Superseded.
# Reasoning (historical): the purple/cyan combo matches the site (/sites/value, /premium, /dcpi) and
# tests dramatically better on LinkedIn/X feeds than the previous orange treatment, which
# competed visually with every other ad/post and read as "alert," not "premium."
# ── dchub.cloud's PUBLISHED tokens (2026-09-05) ────────────────────────────
# Until today this file ran on default Tailwind slate + sky while the website
# ran on a near-black ground with an indigo->violet accent. Not one token
# matched, which is why every card read as off-brand no matter how it was
# tweaked. These values are the site's own custom properties, verbatim:
#
#   --bg #0a0a0f  --surface #131319  --surface-2 #1a1a22
#   --text #f5f5f7  --text-dim #a1a1aa  --text-faint #71717a
#   --indigo #6366f1  --violet #a855f7
#   accent  linear-gradient(135deg, #6366f1 0%, #a855f7 100%)
#
# Change these ONLY to follow the site. tests/test_card_brand_tokens.py pins
# them against the published values.
BG          = (10, 10, 15)       # --bg        #0a0a0f
BG_DEEP     = (6, 6, 10)         # deeper ground for gradient bottoms
PANEL       = (19, 19, 25)       # --surface   #131319
PANEL_HI    = (26, 26, 34)       # --surface-2 #1a1a22
INDIGO      = (99, 102, 241)     # --indigo    #6366f1  (accent start)
VIOLET      = (168, 85, 247)     # --violet    #a855f7  (accent end)
WHITE       = (255, 255, 255)
TEXT        = (245, 245, 247)    # --text       #f5f5f7
MUTED       = (161, 161, 170)    # --text-dim   #a1a1aa
DIM         = (113, 113, 122)    # --text-faint #71717a

# Verdict colours are SEMANTIC, not brand accent — they encode BUILD/CAUTION/
# AVOID and are deliberately outside the indigo->violet ramp so the accent
# never reads as a verdict.
GREEN       = (16, 185, 129)     # BUILD
RED         = (239, 68, 68)      # AVOID
AMBER       = (245, 158, 11)     # CAUTION

# Back-compat aliases. The old names are used in ~90 call sites; repointing
# them is what moves the whole fleet onto the brand in one change. CYAN in
# particular no longer means cyan — the site has no cyan.
PURPLE     = INDIGO
PURPLE_LT  = VIOLET
CYAN       = INDIGO
ACCENT     = INDIGO
ACCENT2    = VIOLET


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


def _font(size, bold=True, weight=None):
    """The BRAND face first (Instrument Sans — what dchub.cloud serves), then
    the bundled DejaVu fallback, then system fonts, then a LOUD default.

    `weight` optionally picks a specific Instrument Sans cut ('Bold',
    'SemiBold', 'Medium', 'Regular'); omitted, `bold` selects Bold/Regular.
    DejaVu has no matching cuts, so the fallback collapses to bold/regular —
    which is the point: it is a fallback, not a design."""
    from PIL import ImageFont
    global _FONT_FELL_BACK
    cut = weight or ('Bold' if bold else 'Regular')
    candidates = [
        os.path.join(_FONT_DIR, f'InstrumentSans-{cut}.ttf'),
        os.path.join(_FONT_DIR, 'InstrumentSans-Bold.ttf' if bold else 'InstrumentSans-Regular.ttf'),
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
    """JetBrains Mono — the site's --mono face. Labels, eyebrows and
    figures only; it must never set a headline again."""
    from PIL import ImageFont
    for path in [
        os.path.join(_FONT_DIR, 'JetBrainsMono-Bold.ttf'),
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


def _has_market_verdict(signals) -> bool:
    """2026-07-16: True ONLY when a REAL market verdict is present (an explicit
    BUILD/CAUTION/AVOID on a market entry). The DCPI verdict pill is a MARKET
    signal — on a capability/platform card it's nonsense (the "BUILD" pill on the
    error-contract press card the operator flagged). Draw the pill only when this
    is true, so non-market cards never get a spurious verdict."""
    if not isinstance(signals, dict):
        return False
    top = (signals.get('top_build_markets') or [])
    if not (top and isinstance(top, list) and isinstance(top[0], dict)):
        return False
    v = str(top[0].get('verdict') or '').strip().upper()
    return v in ('BUILD', 'CAUTION', 'AVOID')


def _safe_date_str(pr_date, fmt='%Y-%m-%d'):
    """Format a date-or-None pr['date'] value. Falls back to UTC today
    if missing/null so cards never show an empty timestamp line."""
    if pr_date and hasattr(pr_date, 'strftime'):
        return pr_date.strftime(fmt)
    return utc_now().strftime(fmt)


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


def _brand_gradient(size):
    """linear-gradient(135deg, #6366f1, #a855f7) — the site's accent, as an
    image. Pure PIL, no numpy."""
    from PIL import Image
    w, h = size
    g = Image.new('RGB', (w, h))
    px = g.load()
    for y in range(h):
        for x in range(w):
            t = ((x / max(w - 1, 1)) + (y / max(h - 1, 1))) / 2
            px[x, y] = (
                round(INDIGO[0] + (VIOLET[0] - INDIGO[0]) * t),
                round(INDIGO[1] + (VIOLET[1] - INDIGO[1]) * t),
                round(INDIGO[2] + (VIOLET[2] - INDIGO[2]) * t),
            )
    return g


def _grad_text(img, xy, text, font):
    """Fill glyphs with the brand gradient — PIL's equivalent of the site's
    `.grad` class (background-clip:text). Renders the text to an alpha mask and
    pastes the gradient through it, so the letterforms are identical to a
    normal draw; only the fill changes."""
    from PIL import Image, ImageDraw
    if not text:
        return
    m = ImageDraw.Draw(Image.new('L', (1, 1)))
    w = int(m.textlength(text, font=font)) + 8
    asc, desc = font.getmetrics()
    h = asc + desc + 8
    mask = Image.new('L', (w, h), 0)
    ImageDraw.Draw(mask).text((0, 0), text, font=font, fill=255)
    img.paste(_brand_gradient((w, h)), (int(xy[0]), int(xy[1])), mask)


def _draw_brand_strip(d, y_top=0, y_bot=8):
    """Thin purple accent strip — typically across the very top of the
    canvas. Visual signature that ties all 4 styles together. Replaces
    the old chunky 60px orange bar with something more refined."""
    d.rectangle([(0, y_top), (W, y_bot)], fill=PURPLE)


def _draw_brand_footer(d, y, mark='dchub.cloud', date_str=None, kicker='DC HUB MEDIA',
                       chip=True):
    """Standardized footer block. Brand monogram on the LEFT, kicker text
    aligned, URL on the RIGHT. Replaces the inconsistent 'dchub.cloud · DC Hub Daily Index'
    / '→ dchub.cloud/news' / 'dchub.cloud · DC Hub Media · Daily Power Index' strings
    that varied across styles in v1."""
    # chip=False when the header already carries the mark — the card had TWO
    # DC HUB logos, one at each corner, which is a template tell not branding.
    _x = 130 if chip else 64
    if chip:
        _brand_chip(d, 60, y - 10, size=52)
    if kicker:
        d.text((_x, y - 4),  kicker, font=_font(20),                fill=VIOLET)
    d.text((_x, y + 20),     mark,   font=_mono(18),                fill=MUTED)
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

    v3 (2026-07-02, operator "graphics are lame" directive) — MARKET
    SCORECARD. The v2 card was a lone 240pt mono number with a hardcoded
    '#1 NATIONALLY' caption that shipped even on a 45.0 AVOID market —
    incoherent, and DejaVu-Mono digits read as broken glyphs at that size.
    v3 renders like a product screenshot: market name + verdict pill,
    hero score in the display sans, and labeled 0-100 GAUGE BARS for
    Excess Power (+ Grid Constraint when available). Every element is
    driven by the actual data — no canned superlatives.

    2026-07-03: sits on a heavily-dimmed topical photo (transmission / grid)
    instead of a flat navy slab, so a non-ai_hero pick is no longer a flat
    dark logo card. The photo is a texture behind the type, not the subject."""
    from PIL import Image, ImageDraw
    img = _photo_backdrop(pr, dim=0.80)
    d = ImageDraw.Draw(img)

    # Brand accent strip + kicker row
    _draw_brand_strip(d)
    d.text((60, 34), 'DC HUB POWER INDEX  ·  LIVE', font=_mono(22), fill=CYAN)
    date_str = _safe_date_str(pr.get('date'), '%b %d, %Y').upper()
    dtw, _ = _text_size(d, date_str, _mono(22))
    d.text((W - dtw - 60, 34), date_str, font=_mono(22), fill=MUTED)

    # Extract the hero stat from signals
    signals = pr.get('signals', {})
    top = (signals.get('top_build_markets') or [{}])[0] if isinstance(signals, dict) else {}
    if not isinstance(top, dict): top = {}
    market_name = _market_name_of(top)
    score = _market_score_of(top)
    constraint = top.get('constraint')
    if constraint is None: constraint = top.get('constraint_score')
    try: constraint = float(constraint) if constraint is not None else None
    except (ValueError, TypeError): constraint = None
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

    # Market name row — tight bold display + color-coded verdict pill
    # sitting inline right after the name (not orphaned in a far corner).
    # Ellipsize the name by PIXEL width so it never runs under the pill.
    name_txt = market_name.upper()
    name_font = _font(76)
    max_name_w = W - 60 - 300     # reserve pill + margins on the right
    if _text_size(d, name_txt, name_font)[0] > max_name_w:
        while name_txt and _text_size(d, name_txt + '…', name_font)[0] > max_name_w:
            name_txt = name_txt[:-1].rstrip()
        name_txt += '…'
    d.text((60, 104), name_txt, font=name_font, fill=WHITE)
    verdict = _verdict_for(signals)
    nw, _ = _text_size(d, name_txt, name_font)
    _verdict_pill(d, x=min(60 + nw + 36, W - 260), y=118, verdict=verdict,
                  font_size=36, pad_x=32, pad_y=14)

    if score:
        # Hero score — display sans (clean digits), purple, with the scale
        # caption anchored to its baseline so the number is self-explaining.
        score_str = f'{score:.0f}' if float(score).is_integer() else f'{score:.1f}'
        score_font = _font(170)
        d.text((60, 218), score_str, font=score_font, fill=PURPLE_LT)
        sw, _ = _text_size(d, score_str, score_font)
        d.text((60 + sw + 22, 322), '/100', font=_font(44), fill=DIM)
        d.text((60 + sw + 22, 284), 'GRID HEADROOM', font=_mono(24), fill=MUTED)

        # Gauge bars — the data-viz backbone of the card. Track in deep
        # slate, fill in brand purple (excess) / cyan (constraint).
        bar_x, bar_w, bar_h = 60, W - 120 - 150, 26
        gauges = [('EXCESS POWER', score, PURPLE)]
        if constraint is not None:
            gauges.append(('GRID CONSTRAINT', constraint, CYAN))
        gy = 436
        for label, val, color in gauges:
            d.text((bar_x, gy - 30), label, font=_mono(20), fill=MUTED)
            _rounded_rect(d, [(bar_x, gy), (bar_x + bar_w, gy + bar_h)],
                          radius=bar_h // 2, fill=(30, 41, 59))
            fill_w = int(bar_w * max(0.0, min(float(val), 100.0)) / 100.0)
            if fill_w > bar_h:
                _rounded_rect(d, [(bar_x, gy), (bar_x + fill_w, gy + bar_h)],
                              radius=bar_h // 2, fill=color)
            d.text((bar_x + bar_w + 24, gy - 4), f'{val:.0f}',
                   font=_font(30), fill=TEXT)
            gy += 74
    else:
        # No score → render the subheadline at large display size instead
        sub = pr.get('subheadline', '') or pr.get('title', '')
        for i, line in enumerate(_wrap_px(sub, _font(54), W - 120, max_lines=4)):
            d.text((60, 230 + i * 76), line, font=_font(54), fill=TEXT)

    # Footer
    _draw_brand_footer(d, y=H - 64, mark='dchub.cloud/dcpi',
                       kicker='DC HUB  ·  MARKET INTELLIGENCE')

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
    cyan separator rule above the subhead to give it editorial weight.

    2026-07-03: now sits on a dimmed topical photo (campus / grid) rather
    than a flat navy fill, so even a non-ai_hero editorial pick reads as a
    publication card, not a dark logo slab."""
    from PIL import Image, ImageDraw
    img = _photo_backdrop(pr, dim=0.82)
    d = ImageDraw.Draw(img)

    _draw_brand_strip(d)

    # Kicker row — brand chip + DC HUB + date (chip beats the tiny diamond
    # glyph, which rendered as a barely-visible speck at 22pt)
    _brand_chip(d, 64, 44, size=40)
    # 2026-09-05: the date is stamped only on DATED editorial (a press release).
    # /og/dynamic.png cards are evergreen marketing copy — a date there ages the
    # card the moment it is posted AND changes the bytes daily, which defeats
    # the 7-day edge cache the card is now served under.
    _evergreen = str(pr.get('slug') or '').startswith('dyn-')
    _kick = 'DC HUB'
    if not _evergreen:
        _kick += '  ·  ' + _safe_date_str(pr.get('date'), '%b %d, %Y').upper()
    d.text((120, 52), _kick, font=_font(24), fill=INDIGO)

    # Verdict pill — top-right, if signals carry one
    signals = pr.get('signals', {}) if isinstance(pr.get('signals', {}), dict) else {}
    if signals.get('top_build_markets'):
        verdict = _verdict_for(signals)
        _verdict_pill(d, x=W - 280, y=44, verdict=verdict, font_size=36, pad_x=32, pad_y=14)

    # Headline — 3 lines max, pixel-accurate wrap (the char-count wrap
    # either overflowed long words or left half the canvas empty)
    title = pr.get('title', '')[:200]
    hfont = _font(72)
    lines = _wrap_px(title, hfont, W - 128, max_lines=3)
    y = 136
    # The site's hero sets one line white and the next in the indigo->violet
    # gradient (`.grad`). Mirror it: the LAST line carries the gradient, so the
    # card is recognisably ours before the headline is even read. A one-line
    # headline stays white — a lone gradient line reads as an error, not a
    # treatment.
    _grad_line = len(lines) - 1 if len(lines) > 1 else -1
    for i, line in enumerate(lines):
        if i == _grad_line:
            _grad_text(img, (64, y), line, hfont)
        else:
            d.text((64, y), line, font=hfont, fill=WHITE)
        y += 90

    # Purple rule + stat strip — the supporting number deserves editorial
    # weight, not 26pt muted whisper text
    sub = pr.get('subheadline', '')
    if sub:
        sep_y = min(max(y + 26, 400), 470)
        img.paste(_brand_gradient((80, 5)), (64, sep_y))
        sfont = _font(32)
        # 3-line headline leaves room for only one stat line above the footer
        _max_sub = 1 if len(lines) >= 3 else 2
        sublines = _wrap_px(sub, sfont, W - 128 - 220, max_lines=_max_sub)
        sy = sep_y + 26
        for s in sublines:
            d.text((64, sy), s, font=sfont, fill=TEXT)
            sy += 44

    # 2026-09-05: the three ascending bars that used to sit bottom-right were
    # removed. They encoded NOTHING — a decorative chart on a card from a
    # company whose product is data reads as clip-art, and it collided with the
    # photo. If a card wants a chart it should plot something true; see the
    # grid-inventory style.

    # Footer — no second chip (the header already carries the mark) and no
    # repeated 'DC HUB' kicker.
    _draw_brand_footer(d, y=H - 64, mark='dchub.cloud/news',
                       kicker=None, chip=False)

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

# ---------------------------------------------------------------------------
# Curated photo library background (2026-07-03) — the ALWAYS-AVAILABLE
# photographic source. The good "Brookfield" editorial card looked great
# because it composited a real photo (SDXL); the rest of the fleet fell to
# flat-navy logo slabs because SDXL was the ONLY photo source and it's
# unconfigured by default. This wires the curated Unsplash library
# (services/image_matcher + data/images.json — transmission-at-sunset,
# renewable, campus-exterior, servers, construction) as a zero-cost photo
# floor so EVERY hero card is photographic, with SDXL kept as the premium
# path when CF creds are live.
_LIB_IMAGE_CACHE = {}            # image_id → cropped 1200x630 RGB PIL image
_LIB_IMAGE_CACHE_MAX = 24


def _library_photo_url(pr: dict):
    """Pick the most topical curated photo for this press release using the
    existing ImageMatcher (services/image_matcher). Returns a resolved
    Unsplash delivery URL (with sizing params) or None on any problem.

    The matcher scores by tag-in-text + category, so we feed it the title +
    subheadline + topic. Unsplash serves an on-the-fly resized JPEG when we
    append ?w=&q=&fit=crop, which keeps the fetch small and predictable."""
    try:
        from services.image_matcher import get_matcher
        title = (pr.get('title') or '')
        blob = ' '.join([
            title,
            (pr.get('subheadline') or ''),
            (pr.get('topic') or ''),
        ]).strip()
        res = get_matcher().match(title, blob)
        img = (res or {}).get('image') or {}
        base = (img.get('url') or '').strip()
        if not base:
            return None, None
        # Unsplash dynamic sizing — request a landscape crop near our canvas
        # size so the download is small (~80-180KB) and already ~1200x630.
        sep = '&' if '?' in base else '?'
        url = f"{base}{sep}w={W}&h={H}&fit=crop&crop=entropy&q=72&auto=format"
        return url, (img.get('id') or base)
    except Exception as e:
        print(f"[og_cards] library match failed: {e}")
        return None, None


def _crop_to_canvas(bg):
    """Resize+center-crop a PIL image to exactly W x H (1200x630), preserving
    aspect ratio (cover-fit). Same treatment the SDXL branch uses."""
    from PIL import Image
    bg = bg.convert('RGB')
    # Scale so the image COVERS the canvas, then center-crop the overflow.
    scale = max(W / bg.width, H / bg.height)
    new_w = max(1, int(round(bg.width * scale)))
    new_h = max(1, int(round(bg.height * scale)))
    bg = bg.resize((new_w, new_h), Image.LANCZOS)
    left = max(0, (new_w - W) // 2)
    top = max(0, (new_h - H) // 2)
    return bg.crop((left, top, left + W, top + H))


def _library_bg(pr: dict):
    """Fetch (and cache) a curated library photo as a 1200x630 RGB PIL image.
    This is the always-on photographic background — no SDXL/CF creds needed.
    Returns None on any problem (network, decode) so callers fall back."""
    url, cache_id = _library_photo_url(pr)
    if not url:
        return None
    if cache_id in _LIB_IMAGE_CACHE:
        return _LIB_IMAGE_CACHE[cache_id].copy()
    try:
        import requests as _rq
        from io import BytesIO
        from PIL import Image
        resp = _rq.get(url, timeout=12, headers={
            'User-Agent': 'dchub-og-cards/1.0 (+https://dchub.cloud)'})
        if resp.status_code != 200 or not resp.content:
            return None
        bg = Image.open(BytesIO(resp.content))
        bg = _crop_to_canvas(bg)
        # Cache the cropped canvas-sized image (small, ~1-2MB decoded) so
        # repeated LinkedIn/OG scrapes of the same card don't re-fetch.
        _LIB_IMAGE_CACHE[cache_id] = bg
        if len(_LIB_IMAGE_CACHE) > _LIB_IMAGE_CACHE_MAX:
            _LIB_IMAGE_CACHE.pop(next(iter(_LIB_IMAGE_CACHE)), None)
        return bg.copy()
    except Exception as e:
        print(f"[og_cards] library bg fetch failed: {e}")
        return None


def _photo_backdrop(pr, dim=0.78, base=None):
    """Return a HEAVILY-dimmed library photo sized to the canvas, to sit
    behind editorial/data-brutal type so those cards are no longer flat navy
    slabs. Blends the photo toward the brand navy by `dim` (0=photo,
    1=solid navy) so the typography still reads at full contrast — the photo
    is a texture, not the subject. Falls back to a flat navy canvas (with the
    subtle gradient) when no photo is available."""
    from PIL import Image
    base = base if base is not None else BG
    try:
        bg = _library_bg(pr)
    except Exception:
        bg = None
    if bg is None:
        img = Image.new('RGB', (W, H), base)
        _subtle_gradient(img, base, BG_DEEP, falloff=1.0)
        return img
    # Blend photo → navy by `dim`
    navy = Image.new('RGB', (W, H), base)
    try:
        img = Image.blend(bg.convert('RGB'), navy, max(0.0, min(1.0, dim)))
    except Exception:
        img = Image.new('RGB', (W, H), base)
        _subtle_gradient(img, base, BG_DEEP, falloff=1.0)
        return img
    # Darken the bottom third further so the footer + gauges stay legible.
    from PIL import ImageDraw
    overlay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    start_y = int(H * 0.55)
    for i in range(start_y, H):
        a = int(120 * ((i - start_y) / (H - start_y)))
        od.line([(0, i), (W, i)], fill=(base[0], base[1], base[2], a))
    img = Image.alpha_composite(img.convert('RGBA'), overlay).convert('RGB')
    return img

def _generate_workers_ai_image(prompt: str, slug: str, variant: int = 0):
    """Hit Cloudflare Workers AI SDXL endpoint. Returns PNG bytes or None
    if creds missing / API errored. Cached per (slug, day, variant) so the
    review-retry loop can request a genuinely different image for variant>0."""
    cache_key = (slug, utc_now().strftime('%Y%m%d'), variant)
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


def _compose_photo_hero(bg, pr):
    """Composite the EDITORIAL hero layout (the good "Brookfield" card) onto a
    photographic background: dark bottom gradient scrim → brand strip → brand
    chip + DC HUB MEDIA kicker → optional verdict pill → bottom headline →
    brand footer. Shared by BOTH photo sources (SDXL and the curated library)
    so the aesthetic is identical no matter which one produced the photo.

    `bg` is any RGB PIL image at ANY size — it's cover-fit cropped to the
    1200x630 canvas here. Returns a finished RGB image.
    """
    from PIL import Image, ImageDraw
    bg = _crop_to_canvas(bg)
    img = bg
    d = ImageDraw.Draw(img)

    # Strong bottom gradient — protects ALL text below the midline.
    # (alpha 220, starts at 40% so headline+pill+CTA sit on safe contrast.)
    overlay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    start_y = int(H * 0.40)
    for i in range(start_y, H):
        alpha = int(220 * ((i - start_y) / (H - start_y)))
        odraw.line([(0, i), (W, i)], fill=(0, 0, 0, alpha))
    # A lighter TOP scrim too, so the brand chip + date read cleanly over a
    # bright sky (the sunset/solar photos are light at the top).
    for i in range(0, int(H * 0.22)):
        alpha = int(150 * (1 - i / (H * 0.22)))
        odraw.line([(0, i), (W, i)], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img.convert('RGBA'), overlay).convert('RGB')
    d = ImageDraw.Draw(img)

    # Brand accent strip (purple) — same anchor as all other styles
    _draw_brand_strip(d)

    # Brand chip + DC HUB MEDIA label (top-left, like every other style)
    _brand_chip(d, 60, 56, size=52)
    # Over a PHOTO the label is near-white, not violet. The brand accent is a
    # saturated mid-tone: it reads fine on the #0a0a0f ground but loses contrast
    # against a bright sky, and only a thin top scrim protects this row. The
    # indigo chip beside it already carries the brand colour.
    d.text((130, 56),  'DC HUB MEDIA', font=_font(20), fill=TEXT)
    d.text((130, 84),  _safe_date_str(pr.get('date'), '%b %d, %Y').upper(),
           font=_mono(16), fill=MUTED)

    # Verdict pill — top-right ONLY for real market verdicts (2026-07-16: never on
    # capability/platform cards — that was the nonsense "BUILD" pill the operator
    # flagged on the error-contract press card).
    signals = pr.get('signals', {}) if isinstance(pr.get('signals', {}), dict) else {}
    if _has_market_verdict(signals):
        verdict = _verdict_for(signals)
        _verdict_pill(d, x=W - 280, y=44, verdict=verdict,
                      font_size=36, pad_x=32, pad_y=14)

    # Headline at the bottom — clean white on the gradient-darkened backdrop.
    title = pr.get('title', '')[:120]
    hf = _font(60)
    lines = _wrap_px(title, hf, max_w=W - 120, max_lines=3)
    line_height = 76
    total_height = line_height * len(lines)
    y_start = H - total_height - 100
    for line in lines:
        d.text((60, y_start), line, font=hf, fill=WHITE)
        y_start += line_height

    # Optional supporting line just under the headline (editorial subhead).
    sub = (pr.get('subheadline') or '').strip()
    if sub and len(lines) <= 2:
        sfont = _font(26)
        for s in _wrap_px(sub, sfont, W - 120, max_lines=1):
            d.text((60, y_start + 6), s, font=sfont, fill=(226, 232, 240))

    # Footer — the exact honest-brand footer the good Brookfield card used.
    _draw_brand_footer(d, y=H - 64, mark='dchub.cloud/news',
                       kicker='DC HUB MEDIA  ·  AUTONOMOUS PRESS')

    return img


def _draw_ai_hero(pr):
    """Photographic editorial hero — the DEFAULT premium card.

    2026-07-03 upgrade: the photographic background now ALWAYS resolves.
    Source order:
      1. CF Workers AI SDXL — premium, atmospheric, per-headline (only when
         CF_ACCOUNT_ID + CF_API_TOKEN are set).
      2. Curated photo library (services/image_matcher + data/images.json) —
         the zero-cost, always-available floor. A topical transmission /
         renewable / campus / server photo, matched to the headline.
      3. Flat brand panel — true last resort only if BOTH photo sources fail
         (e.g. no network + no SDXL creds).
    Every photo path is composited by the SAME _compose_photo_hero() layout,
    so the fleet now matches the good editorial card by default.
    """
    from PIL import Image, ImageDraw
    slug = pr.get('slug', '')
    if not slug:
        # Try to derive a slug from the title for cache keying
        slug = (pr.get('title') or 'unknown').lower().replace(' ', '-')[:60]

    # 1) Premium path — SDXL when configured.
    ai_png = None
    _prompt = _build_sdxl_prompt(pr)
    ai_png = _generate_workers_ai_image(_prompt, slug, 0)
    # Review-retry (opt-in via DCHUB_MEDIA_AI_REVIEW): if the first image fails
    # the vision check, take ONE fresh variant. Bounded → at most 2 gens.
    if ai_png and not _ai_review_ok(ai_png):
        ai_png = _generate_workers_ai_image(_prompt, slug, 1) or ai_png
    if ai_png:
        try:
            from io import BytesIO
            bg = Image.open(BytesIO(ai_png)).convert('RGB')
            return _compose_photo_hero(bg, pr)
        except Exception as e:
            print(f"[ai_hero] SDXL composite failed, trying library: {e}")

    # 2) Always-available floor — curated library photo.
    try:
        lib_bg = _library_bg(pr)
    except Exception as e:
        print(f"[ai_hero] library bg error: {e}")
        lib_bg = None
    if lib_bg is not None:
        try:
            return _compose_photo_hero(lib_bg, pr)
        except Exception as e:
            print(f"[ai_hero] library composite failed: {e}")

    # 3) Fallback when NEITHER photo source worked — premium brand panel (no garish
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

    # Verdict pill — only for a real market verdict (never on capability cards)
    signals = pr.get('signals', {}) if isinstance(pr.get('signals', {}), dict) else {}
    if _has_market_verdict(signals):
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
# 2026-07-03: photographic ai_hero is now the DEFAULT surface. The old
# rotation landed the good photo card only on Thursday and served flat
# logo/number slabs the other 6 days. ai_hero always resolves a real photo
# now (library floor, SDXL premium), so it's the everyday card. data_brutal
# (the DCPI score gauge) is kept twice a week for the pure-number stories
# where the big score IS the story.
DAILY_STYLES = {
    0: 'ai_hero',       # Monday
    1: 'ai_hero',       # Tuesday
    2: 'data_brutal',   # Wednesday  — mid-week DCPI score card
    3: 'ai_hero',       # Thursday
    4: 'ai_hero',       # Friday
    5: 'ai_hero',       # Saturday
    6: 'data_brutal',   # Sunday     — weekend DCPI score card
}


# ---------------------------------------------------------------------------
# DATA CARD (2026-07-14) — stat-forward branded card for the capability /
# platform-update stories (the editorial `cap_*` kinds). A huge hero number +
# unit + a kind-specific mini-viz (ratio bar / chips / stat grid) on the
# violet-cyan brand. Numbers come from the lead's LIVE canonical values
# (pr['card']['nums']); the per-kind layout + copy live here. This replaces the
# generic ai_hero/fallback card for capability posts (the "ugly gray" the
# operator flagged). Standalone-styled so it never depends on a photo backdrop.
# ---------------------------------------------------------------------------
_DC_BORDER = (40, 50, 78)
_DC_CHIP   = (30, 38, 66)
_DC_BG     = (9, 12, 24)


def _dc_hgrad(img, x0, y0, x1, y1, cl, cr):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img); n = max(1, x1 - x0)
    for i in range(n):
        t = i / n
        c = tuple(int(cl[k] + (cr[k] - cl[k]) * t) for k in range(3))
        d.line([(x0 + i, y0), (x0 + i, y1)], fill=c)


def _dc_glow(img, cx, cy, r, color, a=46):
    from PIL import Image, ImageDraw
    g = Image.new("RGBA", img.size, (0, 0, 0, 0)); gd = ImageDraw.Draw(g); steps = 40
    for i in range(steps, 0, -1):
        rr = int(r * i / steps); aa = int(a * (1 - i / steps))
        gd.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=color + (aa,))
    img.alpha_composite(g)


def _dc_bolt(img, x, y, size):
    """Canonical DC Hub logo — a violet lightning bolt in a navy circle, matching
    dchub-frontend/icons/dchub-logo.svg (the site's single-source brand mark:
    bolt path on a #1a1a2e disc, indigo→violet gradient #6366f1→#a855f7, #a78bfa
    stroke). Replaces the flat "DC" chip so the real website logo rides every card.
    The SVG path is authored in a 36×36 box and scales cleanly at any size."""
    from PIL import Image, ImageDraw
    # Supersample 3× and downscale (LANCZOS) so the disc + bolt edges are crisp
    # at 44–60px instead of jagged (ellipse/polygon aren't antialiased natively).
    ss = 3
    sz = size * ss
    logo = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    ld = ImageDraw.Draw(logo)
    ld.ellipse([0, 0, sz - 1, sz - 1], fill=(26, 26, 46, 255))        # #1a1a2e disc
    s = sz / 36.0
    # bolt path M20.5 6 L10 20 h7 l-2 10 L27 16 h-7.5 l1-10 z (36×36 space)
    pts36 = [(20.5, 6), (10, 20), (17, 20), (15, 30), (27, 16), (19.5, 16), (20.5, 6)]
    pts = [(px * s, py * s) for (px, py) in pts36]
    grad = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for i in range(sz):
        t = i / max(1, sz - 1)
        gd.line([(0, i), (sz, i)],
                fill=(int(99 + 69 * t), int(102 - 17 * t), int(241 + 6 * t), 255))
    mask = Image.new("L", (sz, sz), 0)
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    logo.alpha_composite(Image.composite(grad, Image.new("RGBA", (sz, sz), (0, 0, 0, 0)), mask))
    try:
        ld.line(pts, fill=(167, 139, 250, 255), width=max(2, sz // 30), joint="curve")
    except TypeError:
        ld.line(pts, fill=(167, 139, 250, 255), width=max(2, sz // 30))
    try:
        _rs = Image.LANCZOS
    except AttributeError:
        _rs = Image.Resampling.LANCZOS
    img.alpha_composite(logo.resize((size, size), _rs), (x, y))


def _dc_tw(d, t, f, tr=0):
    b = d.textbbox((0, 0), t, font=f)
    if tr == 0:
        return b[2] - b[0], b[3] - b[1]
    w = 0
    for ch in t:
        bb = d.textbbox((0, 0), ch, font=f); w += (bb[2] - bb[0]) + tr
    return w, b[3] - b[1]


def _dc_text(d, xy, t, f, fill, tr=0):
    if tr == 0:
        d.text(xy, t, font=f, fill=fill); return
    x, y = xy
    for ch in t:
        d.text((x, y), ch, font=f, fill=fill)
        bb = d.textbbox((0, 0), ch, font=f); x += (bb[2] - bb[0]) + tr


def _dc_wrap(d, text, f, maxw):
    out, cur = [], ""
    for w_ in (text or "").split():
        cand = (cur + " " + w_).strip()
        if _dc_tw(d, cand, f)[0] <= maxw:
            cur = cand
        else:
            if cur:
                out.append(cur)
            cur = w_
    if cur:
        out.append(cur)
    return out


def _dc_chrome(img, spec):
    from PIL import ImageDraw
    _dc_hgrad(img, 0, 0, W, 7, PURPLE, CYAN)
    d = ImageDraw.Draw(img)
    M = 68
    _dc_bolt(img, M, 44, 60)
    _dc_text(d, (M + 78, 52), "DC HUB", _mono(30), CYAN, tr=4)
    _dc_text(d, (M + 78, 88), "THE LIVE DATA LAYER FOR AI AGENTS", _mono(15), DIM, tr=2)
    eb = (spec.get("eyebrow") or "").upper(); ef = _mono(18)
    ew, _ = _dc_tw(d, eb, ef, tr=2); px1 = W - M; px0 = px1 - (ew + 44)
    try:
        d.rounded_rectangle([px0, 52, px1, 90], radius=19, outline=PURPLE_LT, width=2)
    except Exception:
        d.rectangle([px0, 52, px1, 90], outline=PURPLE_LT, width=2)
    _dc_text(d, (px0 + 22, 61), eb, ef, PURPLE_LT, tr=2)
    fy = 556
    d.line([(M, fy - 12), (W - M, fy - 12)], fill=_DC_BORDER, width=1)
    _dc_bolt(img, M, fy, 44)
    _dc_text(d, (M + 58, fy + 3), "dchub.cloud", _mono(22), TEXT, tr=1)
    _dc_text(d, (M + 58, fy + 29), "CITE AS DC HUB (dchub.cloud)", _mono(13), DIM, tr=1)
    tag = spec.get("footer_tag", "")
    if tag:
        tf = _mono(17); tgw, _ = _dc_tw(d, tag, tf, tr=1)
        _dc_text(d, (W - M - tgw - 6, fy + 13), tag, tf, PURPLE_LT, tr=1)


def _dc_ratio(img, vx0, vy, viz):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img); vx1 = W - 68
    _dc_text(d, (vx0, vy), viz["label"].upper(), _mono(15), MUTED, tr=2)
    by = vy + 32; bw = vx1 - vx0
    _rounded_rect(d, [vx0, by, vx1, by + 30], 8, PANEL)
    frac = max(0.05, min(1.0, viz["filled"] / max(1, viz["total"]))); fw = int(bw * frac)
    _dc_hgrad(img, vx0, by + 1, vx0 + fw, by + 29, PURPLE, CYAN)
    d = ImageDraw.Draw(img)
    try:
        d.rounded_rectangle([vx0, by, vx1, by + 30], radius=8, outline=_DC_BORDER, width=1)
    except Exception:
        d.rectangle([vx0, by, vx1, by + 30], outline=_DC_BORDER, width=1)
    d.text((vx0, by + 42), f"{viz['filled']:,}", font=_font(22), fill=CYAN)
    tt = f"{viz['total']:,} tracked"; tf = _font(20, bold=False); tgw, _ = _dc_tw(d, tt, tf)
    d.text((vx1 - tgw, by + 44), tt, font=tf, fill=MUTED)


def _dc_chips(img, vx0, vy, viz):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img); vx1 = W - 68
    _dc_text(d, (vx0, vy), viz["label"].upper(), _mono(15), MUTED, tr=2)
    y = vy + 32
    for c in viz["chips"]:
        cf = _mono(20); cwd, _ = _dc_tw(d, c, cf)
        _rounded_rect(d, [vx0, y, min(vx1, vx0 + cwd + 40), y + 44], 10, _DC_CHIP)
        try:
            d.rounded_rectangle([vx0, y, min(vx1, vx0 + cwd + 40), y + 44], radius=10, outline=PURPLE, width=1)
        except Exception:
            pass
        d.text((vx0 + 20, y + 11), c, font=cf, fill=PURPLE_LT); y += 58


def _dc_statgrid(img, x0, y0, x1, stats, cols=2, big=False):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    gap = 22; cellw = (x1 - x0 - gap * (cols - 1)) // cols
    nf = _font(52 if big else 40); lf = _mono(16 if big else 14)
    ch = 100 if big else 82; rh = 118 if big else 96
    for i, s in enumerate(stats):
        r = i // cols; c = i % cols
        cx = x0 + c * (cellw + gap); cy = y0 + r * rh
        _rounded_rect(d, [cx, cy, cx + cellw, cy + ch], 14, PANEL)
        try:
            d.rounded_rectangle([cx, cy, cx + cellw, cy + ch], radius=14, outline=_DC_BORDER, width=1)
        except Exception:
            pass
        d.rectangle([cx, cy + 14, cx + 4, cy + ch - 14], fill=CYAN)
        d.text((cx + 22, cy + (16 if big else 12)), s["n"], font=nf, fill=WHITE)
        _dc_text(d, (cx + 24, cy + (74 if big else 58)), s["label"].upper(), lf, MUTED, tr=1)


def _dc_nums(nums):
    base = {"d": 18000, "v": 4923, "t": 21958, "m": 316, "dl": 4135, "c": 181, "tl": 73}
    for k in base:
        try:
            if nums and nums.get(k) not in (None, ""):
                base[k] = int(float(nums[k]))
        except Exception:
            pass
    return base


def _dc_spec(kind, nums):
    """Concrete card spec for a cap_* kind, numbers filled from live values."""
    n = _dc_nums(nums)
    d, v, t, m, dl, c, tl = n["d"], n["v"], n["t"], n["m"], n["dl"], n["c"], n["tl"]
    specs = {
        "provenance_envelope": {
            "eyebrow": "Platform update", "hero": "num", "number": f"{d:,}",
            "unit": "distinct facilities · provenance-stamped",
            "descriptor": ("Every record ships source, method, as-of and a CC-BY-4.0 "
                           "citation, so agents cite live data with a stated confidence."),
            "viz": {"type": "ratio", "label": "distinct buildings inside the source-record frontier",
                    "filled": d, "total": t},
            "footer_tag": "PROVENANCE ENVELOPE v1"},
        "intl_grid_telemetry": {
            "eyebrow": "Live grid telemetry", "hero": "num", "number": "5",
            "unit": "continents · one live scoreboard",
            "descriptor": ("Japan, South Korea and Brazil now rank beside the US ISOs, EU, "
                           "Great Britain and Taiwan on one renewable-share scale — keyless."),
            "viz": {"type": "chips", "label": "newly on the board",
                    "chips": ["JAPAN — OCCTO", "S. KOREA — KPX", "BRAZIL — ONS"]},
            "footer_tag": "GET_GRID_SCOREBOARD · KEYLESS"},
        "agent_memory": {
            "eyebrow": "New capability", "hero": "num", "number": "2",
            "unit": "new primitives · agents now remember",
            "descriptor": ("save_site builds a durable shortlist; get_changes returns "
                           "per-site deltas next session — not the whole planet."),
            "viz": {"type": "chips", "label": "the memory loop",
                    "chips": ["save_site  →  shortlist", "get_changes  →  deltas"]},
            "footer_tag": "AGENT MEMORY"},
        "error_envelope": {
            "eyebrow": "Error contract", "hero": "grid",
            "kicker": "error_version:1 — one in-band, versioned error contract",
            "stats": [{"n": f"{dl:,}", "label": "deals"}, {"n": f"{m:,}", "label": "markets"},
                      {"n": f"{d:,}", "label": "facilities"}, {"n": f"{tl:,}", "label": "tools covered"}],
            "descriptor": ("A bad parameter returns a deterministic recovery hint with a "
                           "severity class — the agent auto-corrects instead of dead-ending."),
            "footer_tag": "/docs/error-codes"},
        "tool_catalog": {
            "eyebrow": "MCP surface", "hero": "num", "number": f"{tl:,}",
            "unit": "live agent tools · +2 this ship",
            "descriptor": ("Retirement headroom (filed US retirements + nearest substations) "
                           "and physics-bounded latency clustering just shipped."),
            "viz": {"type": "chips", "label": "newest primitives",
                    "chips": ["get_retirement_headroom", "cluster_sites_by_latency"]},
            "footer_tag": "/capabilities · %d TOOLS" % tl},
        "weekly_ledger": {
            "eyebrow": "The DC Hub ledger", "hero": "grid",
            "kicker": "One live, machine-readable layer — refreshed daily",
            "stats": [{"n": f"{d:,}", "label": "facilities"},
                      {"n": f"{dl:,}", "label": "deals tracked"},
                      {"n": f"{m:,}", "label": "DCPI markets"},
                      {"n": f"{c:,}+", "label": "countries"}],
            "descriptor": (f"deduplicated from {t:,} source records · open under "
                           f"CC-BY-4.0 · cite as DC Hub (dchub.cloud)"),
            "footer_tag": "REFRESHED DAILY · CC-BY-4.0"},
    }
    return specs.get(kind)


def _dc_gauge(img, x, y, w, label, value, maxv, color):
    """A labeled 0-maxv gauge bar (violet→color fill) with the value below."""
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    _dc_text(d, (x, y), (label or "").upper(), _mono(15), MUTED, tr=2)
    by = y + 30
    _rounded_rect(d, [x, by, x + w, by + 26], 7, PANEL)
    frac = max(0.03, min(1.0, (value or 0) / max(1, maxv)))
    _dc_hgrad(img, x, by + 1, x + int(w * frac), by + 25, PURPLE, color)
    d = ImageDraw.Draw(img)
    try:
        d.rounded_rectangle([x, by, x + w, by + 26], radius=7, outline=_DC_BORDER, width=1)
    except Exception:
        pass
    d.text((x, by + 36), f"{int(value or 0)}", font=_font(30), fill=WHITE)
    sw, _ = _dc_tw(d, f"{int(value or 0)}", _font(30))
    d.text((x + sw + 4, by + 50), f"/{int(maxv)}", font=_font(18, bold=False), fill=MUTED)


def _dc_draw_market(card):
    """Branded DCPI MARKET scorecard (2026-07-16) — market name + REAL verdict pill
    + Excess-Power / Grid-Constraint gauges + time-to-power, on the violet-cyan
    card. Replaces the sparse data_brutal for market posts; the verdict pill is the
    market's actual verdict, so the card can never contradict the post text."""
    from PIL import Image, ImageDraw
    def _n(k, dv=0):
        v = card.get(k)
        try:
            return float(v) if v not in (None, "") else dv
        except Exception:
            return dv
    market = str(card.get("market") or "This market")[:32]
    iso = str(card.get("iso") or "").upper()[:14]
    verdict = str(card.get("verdict") or "BUILD").upper()[:10]
    excess, constraint, ttp = _n("excess"), _n("constraint"), int(_n("ttp"))
    descriptor = str(card.get("descriptor") or "")
    img = Image.new("RGBA", (W, H), _DC_BG + (255,))
    _dc_glow(img, 120, 90, 520, PURPLE, 46)
    _dc_glow(img, 1120, 600, 460, CYAN, 28)
    _dc_chrome(img, {"eyebrow": card.get("eyebrow") or "DCPI · Power Index",
                     "footer_tag": card.get("footer_tag") or ""})
    d = ImageDraw.Draw(img); M = 68
    nf = _font(58); d.text((M, 150), market, font=nf, fill=WHITE)
    nw, _ = _dc_tw(d, market, nf)
    vc = _verdict_color(verdict); vf = _mono(22); vw, _ = _dc_tw(d, verdict, vf)
    vx = M + nw + 26
    _rounded_rect(d, [vx, 158, vx + vw + 40, 200], 21, vc)
    d.text((vx + 20, 166), verdict, font=vf, fill=BG)
    _dc_text(d, (M, 222), (f"{iso} · DC HUB POWER INDEX" if iso else "DC HUB POWER INDEX"),
             _mono(17), CYAN, tr=2)
    gy = 280; gw = int((W - 2 * M - 60) / 2)
    _dc_gauge(img, M, gy, gw, "Excess power", excess, 100, CYAN)
    _dc_gauge(img, M + gw + 60, gy, gw, "Grid constraint", constraint, 100, PURPLE_LT)
    d = ImageDraw.Draw(img)
    ty = gy + 128
    _rounded_rect(d, [M, ty, M + 270, ty + 64], 12, PANEL)
    d.rectangle([M, ty + 12, M + 4, ty + 52], fill=CYAN)
    d.text((M + 20, ty + 8), f"{ttp} mo", font=_font(34), fill=WHITE)
    _dc_text(d, (M + 22, ty + 46), "TIME TO POWER", _mono(13), MUTED, tr=1)
    if descriptor:
        df = _font(27, bold=False); dy = ty - 4
        for l in _dc_wrap(d, descriptor, df, W - 2 * M - 300)[:3]:
            d.text((M + 300, dy), l, font=df, fill=TEXT); dy += 35
    return img.convert("RGB")


def _draw_data_card(pr):
    """Stat-forward capability card. Reads pr['card']={'kind':..,'nums':{..}};
    falls back to a generic number card from the title if the kind is unknown so
    the route never breaks (crawlers must never get a 4xx)."""
    from PIL import Image, ImageDraw
    card = (pr or {}).get("card") or {}
    kind = card.get("kind") or (pr or {}).get("topic") or ""
    if kind == "market":
        return _dc_draw_market(card)
    spec = _dc_spec(kind, card.get("nums"))
    if spec is None:
        # generic fallback spec from the title/subheadline
        title = str((pr or {}).get("title") or "DC Hub")[:140]
        spec = {"eyebrow": "DC Hub Media", "hero": "num", "number": "",
                "unit": (pr or {}).get("subheadline") or "the live data layer for AI agents",
                "descriptor": title, "footer_tag": "dchub.cloud"}

    img = Image.new("RGBA", (W, H), _DC_BG + (255,))
    _dc_glow(img, 120, 90, 520, PURPLE, 46)
    _dc_glow(img, 1120, 600, 460, CYAN, 28)
    _dc_chrome(img, spec)
    d = ImageDraw.Draw(img); M = 68

    if spec.get("hero") == "grid":
        _dc_text(d, (M, 150), (spec.get("kicker") or "").upper(), _mono(19), CYAN, tr=2)
        _dc_statgrid(img, M, 192, W - M, spec["stats"], cols=2, big=True)
        d = ImageDraw.Draw(img)
        dy = 458
        for l in _dc_wrap(d, spec.get("descriptor", ""), _font(24, bold=False), W - 2 * M)[:2]:
            d.text((M, dy), l, font=_font(24, bold=False), fill=MUTED); dy += 32
        return img.convert("RGB")

    # number hero (left) + optional viz (upper-right) + full-width descriptor
    ny = 166
    num = spec.get("number") or ""
    if num:
        nf = _font(150); d.text((M - 4, ny), num, font=nf, fill=WHITE)
        nw, _ = _dc_tw(d, num, nf)
        d.rectangle([M, ny + 168, M + min(nw, 360), ny + 176], fill=CYAN)
    if spec.get("unit"):
        _dc_text(d, (M, ny + 190), spec["unit"].upper(), _mono(25), CYAN, tr=2)
    viz = spec.get("viz")
    if viz:
        if viz["type"] == "ratio":
            _dc_ratio(img, 720, 206, viz)
        elif viz["type"] == "chips":
            _dc_chips(img, 720, 196, viz)
        d = ImageDraw.Draw(img)
    dy = 398; df = _font(29, bold=False)
    for l in _dc_wrap(d, spec.get("descriptor", ""), df, W - 2 * M)[:3]:
        d.text((M, dy), l, font=df, fill=TEXT); dy += 39
    return img.convert("RGB")


def todays_style():
    """The style for today (UTC), per user-chosen Mon-Sun rotation.

    Exposed as a module-level helper so marketing_engine can build the
    LinkedIn post copy variants matched to the same card.
    """
    return DAILY_STYLES.get(datetime.datetime.utcnow().weekday(), 'data_brutal')


# ---------------------------------------------------------------------------
# GRID INVENTORY card (2026-09-05) — the graphic IS the data.
#
# Every other style illustrates a headline with a stock photograph. This one
# draws the inventory itself: ONE MARK PER ASSET, counted live out of the grid
# layer for a coordinate. It is free, unique per market, impossible for anyone
# without the dataset to copy, and it varies because the DATA varies — which is
# also the standing answer to the media desk's repetition problem.
#
# Three rules keep it honest, and they are what the tests pin:
#   1. A FAILED READ IS NEVER A ZERO. Any class whose query raises comes back
#      None and is omitted from the card. A card that silently draws "0
#      substations" because a connection dropped is worse than no card.
#   2. THE RADIUS CLAIM MUST BE TRUE. The existing grid_intelligence counts use
#      `ABS(lat - x) < deg` — a bounding BOX. This card says "within N km", so
#      it bbox-prefilters (index-friendly) and then applies real haversine.
#   3. THE CARD NAMES ITS BASIS. Radius, and the fact that the transmission
#      layer is the GEOCODED SNAPSHOT (transmission_lines_eia, ~56k rows) and
#      not the maintained ~95k layer, are printed on the card.
_GRID_CLASSES = (
    # key, table, label, colour-role
    ('transmission_lines', 'transmission_lines_eia', 'TRANSMISSION LINES', 'line'),
    ('substations',        'substations',            'SUBSTATIONS',        'dot'),
    ('power_plants',       'power_plants_eia',       'POWER PLANTS',       'dot'),
)

_GRID_COUNT_SQL = """
    SELECT COUNT(*) FROM {table}
     WHERE lat IS NOT NULL AND lng IS NOT NULL
       AND lat BETWEEN %(lat_lo)s AND %(lat_hi)s
       AND lng BETWEEN %(lng_lo)s AND %(lng_hi)s
       AND 6371.0 * 2 * asin(sqrt(
             power(sin(radians(lat - %(lat)s) / 2), 2)
             + cos(radians(%(lat)s)) * cos(radians(lat))
             * power(sin(radians(lng - %(lng)s) / 2), 2)
           )) <= %(radius_km)s
"""


def _grid_counts(lat, lon, radius_km=50.0):
    """One count per asset class within a TRUE radius. Value is None — never 0
    — for any class whose read failed, so the card can omit it rather than
    publish a zero it did not measure."""
    import math
    out = {k: None for k, _t, _l, _s in _GRID_CLASSES}
    url = os.environ.get('DATABASE_URL')
    if not url:
        return out
    deg_lat = radius_km / 111.0
    deg_lng = radius_km / max(111.0 * math.cos(math.radians(lat)), 1e-6)
    params = {
        'lat': lat, 'lng': lon, 'radius_km': radius_km,
        'lat_lo': lat - deg_lat, 'lat_hi': lat + deg_lat,
        'lng_lo': lon - deg_lng, 'lng_hi': lon + deg_lng,
    }
    conn = None
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = 6000")
            for key, table, _label, _shape in _GRID_CLASSES:
                try:
                    cur.execute(_GRID_COUNT_SQL.format(table=table), params)
                    row = cur.fetchone()
                    out[key] = int(row[0]) if row and row[0] is not None else None
                except Exception as e:
                    conn.rollback()          # keep the txn usable for the next class
                    print(f"[og_cards] grid count failed for {table}: {e}")
    except Exception as e:
        print(f"[og_cards] grid counts unavailable: {e}")
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass
    return out


def _unit_field(img, d, x0, y0, n, colour, shape, cols=22, gap=19):
    """One mark per asset. Returns the y below the field."""
    for i in range(n):
        cx = x0 + (i % cols) * gap + 4
        cy = y0 + (i // cols) * gap + 8
        if shape == 'line':
            d.line([(cx, cy - 6), (cx, cy + 6)], fill=colour, width=3)
        else:
            d.ellipse([(cx - 4, cy - 4), (cx + 4, cy + 4)], fill=colour)
    rows = -(-n // cols) if n else 0
    return y0 + rows * gap


def _draw_grid_inventory(pr):
    """Data-native card: the right panel is the asset inventory itself.

    Needs `card` = {lat, lon, place, radius_km}. With no coordinate, or with no
    class that returned a count, it falls back to the editorial style rather
    than inventing a graphic."""
    from PIL import Image, ImageDraw
    spec = pr.get('card') or {}
    try:
        lat = float(spec.get('lat')); lon = float(spec.get('lon'))
    except (TypeError, ValueError):
        return _draw_editorial(pr)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return _draw_editorial(pr)
    try:
        radius_km = float(spec.get('radius_km') or 50)
    except (TypeError, ValueError):
        radius_km = 50.0
    radius_km = max(5.0, min(radius_km, 250.0))
    place = (str(spec.get('place') or '').strip() or 'this coordinate')[:42]

    counts = _grid_counts(lat, lon, radius_km)
    live = [(k, t, l, s) for (k, t, l, s) in _GRID_CLASSES if counts.get(k) is not None]
    if not live:
        # Nothing measured — do NOT draw an empty grid and call it an inventory.
        return _draw_editorial(pr)
    total = sum(counts[k] for k, _t, _l, _s in live)

    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)

    # ── right: the inventory panel ─────────────────────────────────────────
    PX = 690
    d.rectangle([(PX, 0), (W, H)], fill=PANEL)
    d.line([(PX, 0), (PX, H)], fill=PANEL_HI, width=2)
    X0 = PX + 40
    d.text((X0, 46), f'ASSETS WITHIN {int(radius_km)} KM', font=_mono(14), fill=DIM)

    shades = {'line': (58, 60, 110), 'dot': INDIGO}
    y = 84
    for idx, (key, _table, label, shape) in enumerate(live):
        n = counts[key]
        colour = VIOLET if idx == len(live) - 1 else shades[shape]
        d.text((X0, y), f'{n:,}', font=_mono(22), fill=TEXT)
        nw, _ = _text_size(d, f'{n:,}', _mono(22))
        if shape == 'line':
            d.line([(X0 + nw + 16, y + 4), (X0 + nw + 16, y + 18)], fill=colour, width=3)
        else:
            d.ellipse([(X0 + nw + 12, y + 6), (X0 + nw + 22, y + 16)], fill=colour)
        d.text((X0 + nw + 34, y + 5), label, font=_mono(12), fill=MUTED)
        # Cap the drawn marks so a dense metro cannot overflow the panel — and
        # SAY SO on the card rather than silently drawing a wrong quantity.
        drawn = min(n, 22 * 8)
        y = _unit_field(img, d, X0, y + 32, drawn, colour, shape) + 26
        if drawn < n:
            d.text((X0, y - 20), f'showing {drawn:,} of {n:,}', font=_mono(11), fill=DIM)
            y += 8

    # ── left: type, in the site's hero treatment ───────────────────────────
    img.paste(_brand_gradient((5, H)), (0, 0))
    _brand_chip(d, 64, 44, size=40)
    d.text((120, 52), 'DC HUB  ·  GRID INTELLIGENCE', font=_font(22), fill=INDIGO)

    hf = _font(58)
    lines = _wrap_px(f'{total:,} grid assets within {int(radius_km)} km of {place}',
                     hf, 560, max_lines=3)
    ty = 150
    grad_i = len(lines) - 1 if len(lines) > 1 else -1
    for i, line in enumerate(lines):
        if i == grad_i:
            _grad_text(img, (64, ty), line, hf)
        else:
            d.text((64, ty), line, font=hf, fill=WHITE)
        ty += 70

    img.paste(_brand_gradient((80, 5)), (64, ty + 22))
    sy = ty + 58
    for line in _wrap_px('Every mark is one asset in the DC Hub grid layer '
                         '— counted, not illustrated.', _font(21, bold=False), 560,
                         max_lines=2):
        d.text((64, sy), line, font=_font(21, bold=False), fill=MUTED)
        sy += 30

    d.text((64, H - 62), 'dchub.cloud', font=_font(20, weight='SemiBold'), fill=TEXT)
    d.text((64, H - 34), 'HIFLD + EIA  ·  transmission = geocoded snapshot',
           font=_mono(11), fill=DIM)
    return img


STYLE_MAP = {
    'grid_inventory': _draw_grid_inventory,
    'data_brutal': _draw_data_brutal,
    'editorial':   _draw_editorial,
    'infographic': _draw_infographic,
    'ai_hero':     _draw_ai_hero,
    'data_card':   _draw_data_card,
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
    day = utc_now().strftime('%Y-%m-%d')
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
    _brand_chip(d, 60, 130, size=170)
    d.text((270, 152), 'DC HUB', font=_font(78), fill=WHITE)
    d.text((272, 244), 'THE LIVE DATA LAYER FOR AI AGENTS', font=_mono(22), fill=CYAN)

    # Pillars + honest-numbers stat line (canonical 2026-07: 311 markets,
    # 178 countries; facilities floor 21,800+)
    d.text((60, 386), 'POWER  ·  GRID  ·  FIBER  ·  GAS  ·  DEALS',
           font=_mono(22), fill=MUTED)
    d.text((60, 428), '21,800+ facilities  ·  311 markets  ·  178 countries',
           font=_font(26), fill=TEXT)

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
        entry = {
            'market': market or (args.get('title') or '')[:30],
            'excess': sc,
            # 2026-07-16: do NOT default to BUILD — a missing verdict means "not a
            # market-verdict post", so the ai_hero pill (gated on _has_market_verdict)
            # stays off. data_brutal still falls back to BUILD via _verdict_for.
            'verdict': verdict,
        }
        # v3 scorecard: optional 0-100 constraint → second gauge bar
        try:
            if args.get('constraint') not in (None, ''):
                entry['constraint'] = float(args.get('constraint'))
        except Exception:
            pass
        signals = {'top_build_markets': [entry]}

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

    # data_card (2026-07-14): capability/platform stat card. `kind` selects the
    # per-kind layout; the 6 canonical numbers ride along as params so the card
    # shows the lead's LIVE values (v=verified, t=tracked, m=markets, dl=deals,
    # c=countries, tl=tools). Absent numbers fall back to canonical constants.
    # grid_inventory (2026-09-05): the data-native card. Coordinate in, real
    # counts out — see _draw_grid_inventory. No coordinate ⇒ it falls back to
    # editorial rather than drawing an inventory it did not measure.
    if style == 'grid_inventory':
        _gi = {}
        for _k in ('lat', 'lon', 'place', 'radius_km'):
            _vv = args.get(_k)
            if _vv not in (None, ''):
                _gi[_k] = str(_vv)[:64]
        pr['card'] = _gi

    _kind = (args.get('kind') or '').strip()[:48]
    if style == 'data_card' or _kind:
        if _kind == 'market':
            # 2026-07-16: branded DCPI MARKET scorecard params.
            _card = {'kind': 'market'}
            for _k in ('market', 'iso', 'verdict', 'excess', 'constraint', 'ttp',
                       'descriptor', 'eyebrow', 'footer_tag'):
                _vv = args.get(_k)
                if _vv not in (None, ''):
                    _card[_k] = str(_vv)[:220]
            pr['card'] = _card
        else:
            _nums = {}
            for _k in ('d', 'v', 't', 'm', 'dl', 'c', 'tl'):
                _vv = args.get(_k)
                if _vv not in (None, ''):
                    _nums[_k] = _vv
            pr['card'] = {'kind': _kind, 'nums': _nums}

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
