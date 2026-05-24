"""
og_images.py — Open Graph image generation for SEO landing pages.

Phase ZZZZZ-round35 (2026-05-24). Fixes broken og:image references on
2,031 facility/market/grid landing pages — previously pointed at
dchub.cloud/static/og/facility-<id>.png (returns 404 from Pages).

Generates 1200x630 PNGs with:
  - Brand kicker stripe (DC HUB green)
  - Title (facility / market / grid name, auto-wrap up to 3 lines)
  - Subtitle (location / capacity / verdict)
  - Footer (dchub.cloud)

In-memory LRU cache (max 200 images). Pillow >=10.0.0 (already in
requirements.txt line 32). Worker v4.9.6 also caches these in KV with
24h stale-while-error so the Pillow render rarely runs.
"""
import io
import os
from collections import OrderedDict
from contextlib import contextmanager

from flask import Blueprint, send_file, abort
from PIL import Image, ImageDraw, ImageFont

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

og_images_bp = Blueprint("og_images", __name__)

WIDTH, HEIGHT = 1200, 630
BG_COLOR  = (12, 16, 24)
PANEL     = (22, 28, 40)
ACCENT    = (108, 207, 119)   # dchub green
WHITE     = (240, 245, 250)
GREY      = (140, 150, 165)


def _font(size):
    for p in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ):
        if os.path.exists(p):
            try: return ImageFont.truetype(p, size)
            except Exception: pass
    return ImageFont.load_default()


def _wrap(d, text, font, max_width, max_lines=3):
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        bbox = d.textbbox((0, 0), test, font=font)
        if (bbox[2] - bbox[0]) > max_width:
            if cur: lines.append(cur)
            cur = w
        else:
            cur = test
    if cur: lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:42].rstrip() + "…"
    return lines


def _render(title, subtitle, kicker="DC HUB", badge=None):
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    d = ImageDraw.Draw(img)
    # accent stripe
    d.rectangle([(0, 0), (WIDTH, 8)], fill=ACCENT)
    # kicker
    d.text((60, 56), kicker.upper(), font=_font(28), fill=ACCENT)
    # badge (top right)
    if badge:
        bf = _font(22)
        bb = d.textbbox((0, 0), badge, font=bf)
        bw = bb[2] - bb[0]
        d.rounded_rectangle(
            [(WIDTH - bw - 100, 50), (WIDTH - 60, 92)],
            radius=8, fill=PANEL, outline=ACCENT, width=2,
        )
        d.text((WIDTH - bw - 80, 58), badge, font=bf, fill=ACCENT)
    # title
    f_title = _font(72)
    lines = _wrap(d, title or "DC HUB", f_title, WIDTH - 120)
    y = 150
    for line in lines:
        d.text((60, y), line, font=f_title, fill=WHITE)
        y += 84
    # subtitle
    d.text((60, HEIGHT - 150), subtitle or "", font=_font(36), fill=GREY)
    # footer
    d.text((60, HEIGHT - 60), "dchub.cloud", font=_font(22), fill=ACCENT)
    d.text((WIDTH - 280, HEIGHT - 60),
           "Data Center Intelligence", font=_font(22), fill=GREY)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


_CACHE = OrderedDict()
_CACHE_MAX = 200


def _serve(key, title, subtitle, kicker="DC HUB", badge=None):
    if key in _CACHE:
        _CACHE.move_to_end(key)
        data = _CACHE[key]
    else:
        rendered = _render(title, subtitle, kicker, badge)
        data = rendered.getvalue()
        _CACHE[key] = data
        _CACHE.move_to_end(key)
        if len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return send_file(io.BytesIO(data), mimetype="image/png", max_age=86400)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try:
        yield c
    finally:
        c.close()


@og_images_bp.route("/static/og/facility-<int:fid>.png")
def og_facility(fid):
    title = f"Facility #{fid}"
    subtitle = "dchub.cloud"
    badge = None
    if _pg and _dsn():
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute(
                    "SELECT name, city, state, country, power_mw, operator "
                    "FROM discovered_facilities WHERE id=%s", (fid,))
                row = cur.fetchone()
            if row:
                name, city, state, country, mw, op = row
                title = name or title
                loc_parts = [x for x in [city, state, country] if x]
                sub_parts = []
                if loc_parts: sub_parts.append(", ".join(loc_parts))
                if op: sub_parts.append(op)
                subtitle = " · ".join(sub_parts) if sub_parts else subtitle
                if mw: badge = f"{int(mw)} MW"
        except Exception:
            pass
    return _serve(f"f{fid}", title, subtitle, "DC HUB · FACILITY", badge)


@og_images_bp.route("/static/og/market-<slug>.png")
def og_market(slug):
    title = slug.replace("-", " ").title()
    return _serve(f"m{slug}", title, "Data Center Market Roll-up",
                  "DC HUB · MARKET", None)


@og_images_bp.route("/static/og/grid-<code>.png")
def og_grid(code):
    title = code.upper()
    return _serve(f"g{code}", title, "ISO/RTO Grid Intelligence",
                  "DC HUB · GRID", None)


@og_images_bp.route("/static/og/default.png")
def og_default():
    return _serve("default", "DC HUB",
                  "21,000+ Data Centers · 7 Grids · Live MCP",
                  "DC HUB", None)


@og_images_bp.route("/static/og/health")
def og_health():
    return {"blueprint": "og_images_bp", "status": "ok",
            "cache_size": len(_CACHE), "cache_max": _CACHE_MAX,
            "pillow_available": True}, 200
