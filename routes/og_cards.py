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
# Style 1: data_brutal — Bloomberg-terminal hero stat
# ---------------------------------------------------------------------------

def _draw_data_brutal(pr):
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)

    # Top brand bar
    d.rectangle([(0, 0), (W, 60)], fill=ACCENT)
    d.text((40, 16), 'DCHUB · DCPI INDEX', font=_mono(24), fill=BG)
    d.text((W - 220, 16), _safe_date_str(pr.get('date')), font=_mono(24), fill=BG)

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
        # Try to extract score from title via regex
        import re
        m = re.search(r'(\d+\.\d+)', title)
        if m: score = float(m.group(1))

    # Phase JJ (2026-05-14): typography polish. Hero number scaled
    # 200→340pt for fill-the-canvas presence. Market name 60→80pt.
    # All proportions tightened so the score dominates the composition
    # — was previously underwhelming with too much dead space.

    # Market name — large kicker
    d.text((60, 110), market_name.upper()[:20], font=_font(80), fill=TEXT)

    # The big number — terminal style, fills the middle ⅔ of the canvas
    if score:
        # Score: hero-scale, sub-pixel center using vertical alignment.
        d.text((60, 200), f'{score:.1f}', font=_mono(340), fill=ACCENT)
        d.text((60, H - 130), 'EXCESS POWER INDEX  ·  #1 NATIONAL',
               font=_mono(28), fill=MUTED)
    else:
        # No score → use subheadline as wrap, larger text
        sub = pr.get('subheadline', '')
        for i, line in enumerate(_wrap(sub, 30)[:4]):
            d.text((60, 240 + i * 80), line, font=_font(56), fill=TEXT)

    # Verdict badge (bigger, right-bottom). Was 200×60, now 260×84.
    verdict = _verdict_for(signals)
    vcol = _verdict_color(verdict)
    bw, bh = 260, 84
    d.rectangle([(W - bw - 40, H - bh - 50), (W - 40, H - 50)], fill=vcol)
    # Center text in badge
    d.text((W - bw - 20, H - bh - 36), verdict, font=_font(44), fill=BG)

    # Footer — slightly larger
    d.text((60, H - 50), 'dchub.cloud · DC Hub Daily Index',
           font=_mono(20), fill=MUTED)

    return img


# ---------------------------------------------------------------------------
# Style 2: editorial — gradient bg + clean magazine typography
# ---------------------------------------------------------------------------

def _draw_editorial(pr):
    """Phase JJ (2026-05-14): polished editorial. Headline grew 58→72pt,
    sub 24→32pt, CTA 28→36pt. Gradient deepened (darker bottom) for
    better text contrast. Margins tightened 80→64 to use canvas fully."""
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (W, H), (15, 23, 42))
    d = ImageDraw.Draw(img)

    # Deeper gradient for better readability — was flat near top
    for i in range(H):
        t = i / H
        r = int(10 + (50 - 10) * t)
        g = int(14 + (30 - 14) * t)
        b = int(28 + (120 - 28) * t)
        d.line([(0, i), (W, i)], fill=(r, g, b))

    # Kicker (top accent label)
    d.text((64, 70), '◆  DC HUB MEDIA  ·  DAILY POWER INDEX',
           font=_font(22), fill=ACCENT2)

    # Headline — large, word-wrapped, max 3 lines
    title = pr.get('title', '')[:200]
    lines = _wrap(title, 26)[:3]
    y = 140
    for line in lines:
        d.text((64, y), line, font=_font(72), fill=TEXT)
        y += 88

    # Subheadline (bigger, 2 lines max)
    sub = pr.get('subheadline', '')
    if sub:
        sublines = _wrap(sub, 52)[:2]
        sy = max(y + 36, 420)
        for s in sublines:
            d.text((64, sy), s, font=_font(28, bold=False), fill=MUTED)
            sy += 42

    # CTA
    d.text((64, H - 110), '→  dchub.cloud/news',
           font=_font(36), fill=ACCENT)
    d.text((64, H - 60),
           f'DC Hub Media  ·  {_safe_date_str(pr.get("date"), "%B %d, %Y")}',
           font=_font(20), fill=DIM)

    return img


# ---------------------------------------------------------------------------
# Style 3: infographic — bar chart of top 5 markets
# ---------------------------------------------------------------------------

def _draw_infographic(pr):
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)

    # Header strip (taller for more typographic weight)
    d.rectangle([(0, 0), (W, 96)], fill=PANEL)
    d.text((60, 32), 'DCPI EXCESS POWER  ·  TOP 5 MARKETS',
           font=_font(34), fill=ACCENT)
    d.text((W - 220, 36), _safe_date_str(pr.get('date')),
           font=_mono(26), fill=MUTED)

    # Pull top 5 from signals (support both production key set + legacy)
    signals = pr.get('signals', {}) if isinstance(pr.get('signals', {}), dict) else {}
    top_5 = (signals.get('top_build_markets') or [])[:5]
    top_5 = [m for m in top_5 if isinstance(m, dict)]
    # Pad with placeholders if fewer than 5
    if not top_5:
        title = pr.get('title', '')
        import re
        score = 0
        m = re.search(r'(\d+\.\d+)', title)
        if m: score = float(m.group(1))
        name = title.split(' Tops ')[0] if ' Tops ' in title else 'Top Market'
        top_5 = [{'market': name, 'excess': score, 'verdict': 'BUILD'}]

    max_score = max(
        [_market_score_of(m) for m in top_5] + [1.0],
    )

    # Phase JJ: bigger bars, taller rows, more legible labels.
    # bar_h 56→72 + gap 28→34 fills the 5-bar chart from ~420px to
    # ~530px — leaves cleaner vertical breathing at top/bottom.
    y_start = 138
    bar_h = 72
    gap = 22
    label_col_x = 380
    bar_start_x = 400
    bar_max_x = W - 200

    for i, m in enumerate(top_5):
        y = y_start + i * (bar_h + gap)
        name = _market_name_of(m)[:22]
        score = _market_score_of(m)
        is_top = (i == 0)
        color = ACCENT if is_top else (90, 130, 200)

        # Market name (right-aligned in left column) — bigger
        try:
            d.text((label_col_x - 20, y + 20), name,
                   font=_font(32), fill=TEXT, anchor='rt')
        except TypeError:
            # Older PIL without anchor support
            d.text((60, y + 20), name, font=_font(32), fill=TEXT)

        # Bar
        bar_w = int((score / max_score) * (bar_max_x - bar_start_x))
        d.rectangle([(bar_start_x, y), (bar_start_x + bar_w, y + bar_h)],
                    fill=color)

        # Score label at end of bar — bigger
        d.text((bar_start_x + bar_w + 18, y + 22), f'{score:.1f}',
               font=_font(36), fill=TEXT)

        # #1 arrow indicator
        if is_top:
            d.text((W - 90, y + 18), '▲', font=_font(36), fill=GREEN)

    # Verdict + summary at bottom — bigger badge
    verdict = _verdict_for(signals)
    vcol = _verdict_color(verdict)
    d.rectangle([(60, H - 100), (260, H - 30)], fill=vcol)
    d.text((84, H - 86), verdict, font=_font(44), fill=BG)

    if top_5:
        first = _market_name_of(top_5[0])[:30]
        d.text((290, H - 86), f'{first}  ·  ranked #1 nationally',
               font=_font(26), fill=TEXT)
    d.text((290, H - 48), 'dchub.cloud · DC Hub Media · Daily Power Index',
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

def _generate_workers_ai_image(prompt: str, slug: str):
    """Hit Cloudflare Workers AI SDXL endpoint. Returns PNG bytes or None
    if creds missing / API errored. Result cached per-day."""
    cache_key = (slug, datetime.datetime.utcnow().strftime('%Y%m%d'))
    if cache_key in _AI_IMAGE_CACHE:
        return _AI_IMAGE_CACHE[cache_key]

    account_id = os.environ.get('CF_ACCOUNT_ID', '')
    api_token  = os.environ.get('CF_API_TOKEN', '')
    if not (account_id and api_token):
        return None  # Not configured — caller falls back to gradient

    try:
        import requests as _rq
        # SDXL on Workers AI returns binary PNG when format is set right.
        url = (f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
               f"/ai/run/@cf/stabilityai/stable-diffusion-xl-base-1.0")
        resp = _rq.post(
            url,
            json={
                "prompt": prompt[:1500],
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

    ai_png = _generate_workers_ai_image(_build_sdxl_prompt(pr), slug)
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

        # Bottom gradient overlay for text legibility (60% opacity black
        # gradient covering bottom half)
        overlay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        for i in range(H // 2, H):
            alpha = int(180 * ((i - H // 2) / (H // 2)))
            odraw.line([(0, i), (W, i)], fill=(0, 0, 0, alpha))
        img = Image.alpha_composite(img.convert('RGBA'), overlay).convert('RGB')
        d = ImageDraw.Draw(img)

        # Brand chip (top-left)
        d.rectangle([(64, 60), (320, 112)], fill=BG)
        d.text((80, 72), 'DC HUB MEDIA · DAILY', font=_mono(24), fill=ACCENT)

        # Headline at the bottom (3 lines max, with shadow)
        title = pr.get('title', '')[:120]
        lines = _wrap(title, 22)[:3]
        line_height = 76
        total_height = line_height * len(lines)
        y_start = H - total_height - 130
        for line in lines:
            d.text((84, y_start + 4), line, font=_font(60), fill=(0, 0, 0))
            d.text((80, y_start), line, font=_font(60), fill=TEXT)
            y_start += line_height

        d.text((84, H - 76), '→ dchub.cloud/news', font=_font(36), fill=(0, 0, 0))
        d.text((80, H - 80), '→ dchub.cloud/news', font=_font(36), fill=TEXT)
        d.text((80, H - 36),
               f'DC Hub Media  ·  {_safe_date_str(pr.get("date"), "%B %d, %Y")}',
               font=_font(20), fill=TEXT)

        return img

    # Fallback: gradient placeholder (original Phase JJ batch 2 polish)
    img = Image.new('RGB', (W, H), (10, 14, 26))
    d = ImageDraw.Draw(img)

    # Dramatic vertical gradient — deep purple → vivid orange (sunrise)
    for i in range(H):
        t = i / H
        r = int(30 + (255 - 30) * t)
        g = int(12 + (130 - 12) * t)
        b = int(70 + (30 - 70) * t)
        d.line([(0, i), (W, i)], fill=(r, g, b))

    # Brand chip — larger
    d.rectangle([(64, 60), (320, 112)], fill=BG)
    d.text((80, 72), 'DC HUB MEDIA · DAILY', font=_mono(24), fill=ACCENT)

    # Headline — bigger with thicker drop shadow for legibility
    title = pr.get('title', '')[:120]
    lines = _wrap(title, 22)[:3]
    y = 180
    for line in lines:
        # 4px drop shadow for high contrast on gradient
        d.text((84, y + 4), line, font=_font(78), fill=(0, 0, 0))
        d.text((80, y), line, font=_font(78), fill=TEXT)
        y += 96

    # CTA — larger, with shadow
    d.text((84, H - 76), '→ dchub.cloud/news', font=_font(40), fill=(0, 0, 0))
    d.text((80, H - 80), '→ dchub.cloud/news', font=_font(40), fill=TEXT)
    d.text((80, H - 36),
           f'DC Hub Media  ·  {_safe_date_str(pr.get("date"), "%B %d, %Y")}',
           font=_font(20), fill=TEXT)

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

    Returns {style: {'views':int,'clicks':int,'posts':set(slugs)}} — or {}
    on any DB hiccup. The form factor a press release ran is derived from
    its publish weekday via DAILY_STYLES (same mapping og_performance uses).
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
                b = agg.setdefault(style, {'views': 0, 'clicks': 0, 'posts': set()})
                b['posts'].add(slug)
                if event_type == 'view':
                    b['views'] += int(n or 0)
                elif event_type in ('click_out', 'stripe_click'):
                    b['clicks'] += int(n or 0)
    except Exception:
        return {}
    finally:
        try: conn.close()
        except Exception: pass
    return agg


def smart_style():
    """Performance-aware form-factor pick. Falls back to todays_style()
    until there's enough engagement data to judge."""
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
    # Exploit — best measured click-through rate among the eligible cohort.
    best = max(eligible.items(), key=lambda kv: kv[1]['clicks'] / kv[1]['views'])
    return best[0]


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


def register_og_cards(app):
    app.register_blueprint(og_cards_bp)
    app.logger.info("✓ OG cards registered: GET /api/v1/og/<style>/<slug>.png")
    app.logger.info(f"  Today's rotation style: {todays_style()}")
