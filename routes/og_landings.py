"""
og_landings.py — custom OG images for round 36 + 37 landing pages.

Phase ZZZZZ-round37 (2026-05-24). The r36 landings (/ai-capacity-index,
/hyperscaler-deals, /integrations/mcp, /AGENTS.md) all reference
/static/og/default.png — same generic card on every share. This module
generates landing-specific Pillow PNGs so social-share CTR is per-page.

Same 1200x630 Pillow template as routes/og_images.py — different copy.
"""
import io
import os
from collections import OrderedDict
from flask import Blueprint, send_file
from PIL import Image, ImageDraw, ImageFont

og_landings_bp = Blueprint("og_landings", __name__)

WIDTH, HEIGHT = 1200, 630
BG       = (12, 16, 24)
ACCENT   = (108, 207, 119)
WHITE    = (240, 245, 250)
GREY     = (140, 150, 165)
INDIGO   = (108, 99, 255)


def _font(size):
    for p in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        if os.path.exists(p):
            try: return ImageFont.truetype(p, size)
            except Exception: pass
    return ImageFont.load_default()


def _wrap(d, text, font, max_w, max_lines=2):
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if d.textbbox((0,0), test, font=font)[2] > max_w:
            if cur: lines.append(cur)
            cur = w
        else:
            cur = test
    if cur: lines.append(cur)
    return lines[:max_lines]


def _render(kicker, title, subtitle, stat_a=None, stat_b=None, badge=None):
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    d = ImageDraw.Draw(img)
    # accent stripe
    d.rectangle([(0,0),(WIDTH,8)], fill=ACCENT)
    # kicker
    d.text((60, 56), kicker.upper(), font=_font(26), fill=ACCENT)
    # badge top-right
    if badge:
        bf = _font(22)
        bb = d.textbbox((0,0), badge, font=bf)
        bw = bb[2]-bb[0]
        d.rounded_rectangle([(WIDTH-bw-100, 50),(WIDTH-60,92)], radius=8,
                             fill=(22,28,40), outline=INDIGO, width=2)
        d.text((WIDTH-bw-80, 58), badge, font=bf, fill=INDIGO)
    # title
    f_t = _font(74)
    y = 140
    for line in _wrap(d, title, f_t, WIDTH-120, 2):
        d.text((60, y), line, font=f_t, fill=WHITE)
        y += 86
    # subtitle
    d.text((60, y+10), subtitle or "", font=_font(30), fill=GREY)
    # stats (two big numbers)
    if stat_a or stat_b:
        sy = HEIGHT - 180
        sx = 60
        if stat_a:
            d.text((sx, sy),     stat_a[0], font=_font(56), fill=ACCENT)
            d.text((sx, sy+62), stat_a[1], font=_font(20), fill=GREY)
            sx += 380
        if stat_b:
            d.text((sx, sy),     stat_b[0], font=_font(56), fill=INDIGO)
            d.text((sx, sy+62), stat_b[1], font=_font(20), fill=GREY)
    # footer
    d.text((60, HEIGHT-60), "dchub.cloud", font=_font(22), fill=ACCENT)
    d.text((WIDTH-260, HEIGHT-60), "Free MCP · 24 tools",
           font=_font(20), fill=GREY)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


_CACHE = OrderedDict()
_CACHE_MAX = 50


def _serve(key, render_fn):
    if key in _CACHE:
        _CACHE.move_to_end(key)
        data = _CACHE[key]
    else:
        data = render_fn().getvalue()
        _CACHE[key] = data
        _CACHE.move_to_end(key)
        if len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return send_file(io.BytesIO(data), mimetype="image/png", max_age=86400)


# ── Specific landings ──────────────────────────────────────

@og_landings_bp.route("/static/og/landing-ai-capacity.png")
def og_ai_capacity():
    return _serve("ai-capacity", lambda: _render(
        kicker="AI COMPUTE CAPACITY INDEX",
        title="Where 100MW can land in 90 days.",
        subtitle="Weekly leaderboard · 286 markets · fused with DCPI + interconnect queue",
        stat_a=("286", "MARKETS RANKED"),
        stat_b=("90d", "PLANNING HORIZON"),
        badge="LIVE",
    ))


@og_landings_bp.route("/static/og/landing-hyperscaler-deals.png")
def og_hyperscaler():
    return _serve("hyperscaler", lambda: _render(
        kicker="HYPERSCALER AI DEAL TRACKER",
        title="Stargate. Oracle. CoreWeave. AMD. Live.",
        subtitle="$-figures + MW extracted automatically · 10-min refresh",
        stat_a=("$324B+", "M&A TRACKED"),
        stat_b=("10min", "REFRESH"),
        badge="TICKER",
    ))


@og_landings_bp.route("/static/og/landing-integrations-mcp.png")
def og_integrations():
    return _serve("integrations", lambda: _render(
        kicker="MCP · MODEL CONTEXT PROTOCOL",
        title="24 tools. 30 seconds. No signup.",
        subtitle="Claude · Cursor · Cline · Continue · Claude Desktop",
        stat_a=("24", "MCP TOOLS"),
        stat_b=("21,401", "FACILITIES"),
        badge="FREE TIER",
    ))


@og_landings_bp.route("/static/og/landing-agents.png")
def og_agents():
    return _serve("agents", lambda: _render(
        kicker="AGENTS.MD · A2A · OAUTH",
        title="Built for AI agents in 2026.",
        subtitle="MCP 2025-06-18 spec · A2A discovery · llms.txt · OpenAPI",
        stat_a=("6", "AGENT SKILLS"),
        stat_b=("100%", "DISCOVERABLE"),
        badge="STANDARDS",
    ))


@og_landings_bp.route("/static/og/landings-health")
def health():
    return {"blueprint": "og_landings_bp",
            "cache_size": len(_CACHE),
            "renders": ["ai-capacity","hyperscaler","integrations","agents"]}, 200
