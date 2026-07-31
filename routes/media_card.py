"""Branded LinkedIn stat cards — 2026-07-31.

Operator complaint: "the linkedin posts are all texts." The r64 fallback in
content_publisher fetches its card from https://dchub.cloud over the network,
so any CF/origin hiccup ships the post bare. This module renders the card
IN-PROCESS instead: a 1200x627 stat card (LinkedIn's 1.91:1 share size) built
from the post's own headline metric.

Contract:
  * render_stat_card(lead) is a PURE function — bytes in, PNG bytes out,
    deterministic (no clock, no randomness, no network, no DB), so the same
    lead always produces byte-identical output.
  * The lead dict comes from content_publisher._media_card_lead(), which reads
    the SAME _METRIC_PATTERNS the quality gate scores. A card can never show a
    number its post doesn't say, and it never touches the post text (the gate
    keeps scoring the wire output unchanged).
  * This module runs NO DDL and never reads or writes linkedin_posts.

Serving: GET /api/v1/media/card/<post_id>.png renders live from the
social_media_posts row so the approval loop can preview the exact card that
will attach at publish. Draft/unpublished rows are admin-gated (pre-approval
copy must not leak); published rows are public. save_card_to_static() is the
best-effort storage hook (Railway's FS is ephemeral — the endpoint above is
the durable URL).
"""
from flask import Blueprint, Response, request
import io
import os
import re

media_card_bp = Blueprint('media_card', __name__)

# LinkedIn's recommended share-image size (1.91:1).
CARD_W, CARD_H = 1200, 627

# House palette (operator-specified): near-black indigo canvas, the
# dchub.cloud indigo→violet gradient reserved for accents.
BG        = (10, 10, 18)      # #0a0a12
PANEL     = (22, 22, 42)      # chip fill
PANEL_LN  = (49, 50, 84)      # chip outline
GRAD_FROM = (99, 102, 241)    # #6366f1 indigo
GRAD_TO   = (168, 85, 247)    # #a855f7 violet
TEXT      = (244, 244, 250)
LABEL_C   = (203, 208, 222)
MUTED     = (148, 155, 178)
VIOLET_LT = (196, 181, 253)   # unit / accent text

# Bundled fonts (routes/fonts/) first — same durable fix as og_cards.py: the
# Railway image has no system TTFs, and ImageFont.load_default() ignores the
# requested size (the 2026-06-06 "empty cards" bug). Inter is tried first so
# dropping Inter-*.ttf into routes/fonts/ upgrades the face with no code
# change; DejaVu is the Inter-style stand-in that ships today.
_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')
_FONT_FELL_BACK = False


def _font(size, bold=True):
    from PIL import ImageFont
    global _FONT_FELL_BACK
    candidates = [
        os.path.join(_FONT_DIR, 'Inter-Bold.ttf' if bold else 'Inter-Regular.ttf'),
        os.path.join(_FONT_DIR, 'DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf'),
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold
            else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/System/Library/Fonts/Helvetica.ttc',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf' if bold
            else '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    if not _FONT_FELL_BACK:
        _FONT_FELL_BACK = True
        print(f"[media_card] FONT FALLBACK to load_default() — bundled font "
              f"missing at {_FONT_DIR}; cards will render tiny. CHECK THE DEPLOY.")
    return ImageFont.load_default()


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _grad_v(draw, x0, y0, x1, y1):
    """Vertical indigo→violet fill, top to bottom."""
    span = max(1, y1 - y0 - 1)
    for y in range(y0, y1):
        draw.line([(x0, y), (x1 - 1, y)], fill=_lerp(GRAD_FROM, GRAD_TO, (y - y0) / span))


def _grad_h(draw, x0, y0, x1, y1):
    """Horizontal indigo→violet fill, left to right."""
    span = max(1, x1 - x0 - 1)
    for x in range(x0, x1):
        draw.line([(x, y0), (x, y1 - 1)], fill=_lerp(GRAD_FROM, GRAD_TO, (x - x0) / span))


def _wrap(draw, text, font, maxw, max_lines=2):
    words = (text or '').split()
    lines, cur = [], ''
    for w in words:
        cand = f'{cur} {w}'.strip()
        if cur and draw.textlength(cand, font=font) > maxw:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                cur = ''
                break
        else:
            cur = cand
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if lines and ' '.join(lines) != ' '.join(words):
        last = lines[-1]
        while last and draw.textlength(last + '…', font=font) > maxw:
            last = last[:-1].rstrip()
        lines[-1] = (last + '…') if last else '…'
    return lines


def render_stat_card(lead):
    """Render the branded 1200x627 stat card. Pure + deterministic.

    lead keys (built by content_publisher._media_card_lead — numbers verbatim
    from the post text, never recomputed):
      headline (required)  — the big number, e.g. "142,318" / "$4.2B" / "7 of 7"
      unit     (optional)  — noun for the number, e.g. "AI tool calls"
      label    (optional)  — the post's own lead line, wrapped to two lines
      trend    (optional)  — e.g. "▲ up 18% week-over-week"
      kicker   (optional)  — top eyebrow, defaults to "DC HUB INTELLIGENCE"
    Returns PNG bytes. Raises ValueError on a missing headline (callers treat
    any exception as "no card" and fall through to the existing text paths).
    """
    from PIL import Image, ImageDraw
    headline = str((lead or {}).get('headline') or '').strip()
    if not headline:
        raise ValueError('lead.headline required')
    unit = str(lead.get('unit') or '').strip()
    label = str(lead.get('label') or '').strip()
    trend = str(lead.get('trend') or '').strip()
    kicker = str(lead.get('kicker') or 'DC HUB INTELLIGENCE').strip()

    img = Image.new('RGB', (CARD_W, CARD_H), BG)
    d = ImageDraw.Draw(img)

    # Left gradient rail.
    _grad_v(d, 0, 0, 12, CARD_H)

    x0 = 96
    maxw = CARD_W - x0 - 80

    # Kicker (eyebrow) with a small gradient tile.
    _grad_v(d, x0, 66, x0 + 16, 82)
    d.text((x0 + 28, 60), kicker.upper(), font=_font(26, bold=True), fill=MUTED)

    # Headline number + unit on a shared baseline; number autosizes to fit.
    baseline = 286
    f_unit = _font(46, bold=True)
    unit_w = (d.textlength(unit, font=f_unit) + 22) if unit else 0
    f_num, num_w = None, 0.0
    for size in range(168, 62, -7):
        f_num = _font(size, bold=True)
        num_w = d.textlength(headline, font=f_num)
        if num_w + unit_w <= maxw:
            break
    d.text((x0, baseline), headline, font=f_num, fill=TEXT, anchor='ls')
    if unit:
        d.text((x0 + num_w + 22, baseline), unit, font=f_unit,
               fill=VIOLET_LT, anchor='ls')

    # Gradient underline sized to the number.
    bar_w = int(min(max(num_w, 160), maxw))
    _grad_h(d, x0, baseline + 26, x0 + bar_w, baseline + 34)

    # Trend chip (optional).
    label_y = baseline + 62
    if trend:
        f_tr = _font(32, bold=True)
        tw = d.textlength(trend, font=f_tr)
        chip_y = baseline + 58
        d.rounded_rectangle([x0, chip_y, x0 + tw + 40, chip_y + 52],
                            radius=26, fill=PANEL, outline=PANEL_LN, width=1)
        d.text((x0 + 20, chip_y + 10), trend, font=f_tr, fill=VIOLET_LT)
        label_y = chip_y + 78

    # Post lead line, wrapped to two lines.
    if label:
        f_lb = _font(36, bold=False)
        y = label_y
        for ln in _wrap(d, label, f_lb, maxw, max_lines=2):
            d.text((x0, y), ln, font=f_lb, fill=LABEL_C)
            y += 48

    # Footer: gradient tile + dchub.cloud, right-aligned strapline.
    _grad_v(d, x0, 546, x0 + 18, 564)
    d.text((x0 + 30, 540), 'dchub.cloud', font=_font(34, bold=True), fill=TEXT)
    d.text((CARD_W - 80, 548), 'Real-time data center intelligence',
           font=_font(24, bold=False), fill=MUTED, anchor='ra')

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def save_card_to_static(png_bytes, key):
    """Best-effort storage hook: mirror a rendered card into static/media_cards/
    and return its served path ('/static/media_cards/<key>.png'), or None on any
    failure. Railway's filesystem is EPHEMERAL — this is a cache/debug artifact;
    the durable served URL is card_url_for() below, which re-renders on demand."""
    try:
        safe = re.sub(r'[^a-zA-Z0-9_-]+', '-', str(key)).strip('-')[:80] or 'card'
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'static', 'media_cards')
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, f'{safe}.png'), 'wb') as f:
            f.write(png_bytes)
        return f'/static/media_cards/{safe}.png'
    except Exception:
        return None


def card_url_for(post_id):
    """Served URL (path) for a draft row's card — renders on demand, so it
    survives redeploys and always reflects the row's CURRENT content."""
    return f'/api/v1/media/card/{int(post_id)}.png'


@media_card_bp.route('/api/v1/media/card/<int:post_id>.png')
def media_card_png(post_id):
    """Render the stat card for a social_media_posts row.

    Unpublished rows (draft/approved/rejected/failed) are pre-approval copy —
    admin-gated with the same key the content-queue UI already sends
    (?key=...). Published rows are public. 404s: unknown row, non-admin access
    to an unpublished row (indistinguishable on purpose), or a post whose text
    carries no recognisable headline metric (no number → no card)."""
    import content_publisher as _cp   # lazy — avoids any import-order coupling at boot
    row = None
    try:
        with _cp._db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, content, status FROM social_media_posts "
                        "WHERE id = %s LIMIT 1", (post_id,))
            row = cur.fetchone()
    except Exception:
        row = None
    if not row:
        return Response('not found', status=404, mimetype='text/plain')
    status = (row['status'] if hasattr(row, 'keys') else row[2]) or ''
    content = (row['content'] if hasattr(row, 'keys') else row[1]) or ''
    if status != 'published' and not _cp._check_admin(request):
        return Response('not found', status=404, mimetype='text/plain')
    lead = _cp._media_card_lead(content)
    if not lead:
        return Response('no headline metric in this post — no card',
                        status=404, mimetype='text/plain')
    png = render_stat_card(lead)
    return Response(png, mimetype='image/png',
                    headers={'Cache-Control': 'public, max-age=300'})
