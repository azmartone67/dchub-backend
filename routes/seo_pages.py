"""
seo_pages.py — SEO-indexable landing pages for facilities, markets, and ISO grids.

Phase ZZZZZ-round33 (2026-05-24). The biggest revenue lever still on the table:
21,000+ facilities × 1 landing page each = 21k high-value long-tail SEO targets.
At 1k pages indexed → ~500 organic visits/day → 1-2 paid MCP signups/day →
$50-150/mo recurring per landing-page wave.

Routes registered:
  GET /facility/<id>      — per-facility detail page (21k pages)
  GET /markets/<slug>     — per-market roll-up    (~50 pages)
  GET /grids/<iso>        — per-ISO roll-up       (16+ after intl expansion)
  GET /sitemap-facilities.xml   — Google Search Console submission
  GET /sitemap-markets.xml
  GET /sitemap-grids.xml
  GET /sitemap-index.xml  — master sitemap pointer

Each page has:
  - Server-rendered HTML (fast first paint, perfect for crawlers)
  - Schema.org structured data (rich snippets in search)
  - Open Graph + Twitter cards (good link previews)
  - Canonical URL
  - Per-facility/market/iso meta description (auto-generated)
  - Internal links to related pages (improves crawl depth)
  - CTA at bottom: signup OR generate PDF report

Register in main.py:
    from routes.seo_pages import seo_pages_bp
    app.register_blueprint(seo_pages_bp)
"""
import os
import html
import datetime as _dt
from typing import Any
from flask import Blueprint, Response, abort, request

try:
    import psycopg2
    import psycopg2.extras
except Exception:  # pragma: no cover
    psycopg2 = None

seo_pages_bp = Blueprint("seo_pages", __name__)


# ─────────────────────────────────────────────────────────────────────
# DB helper (re-uses DATABASE_URL like the rest of the app)
# ─────────────────────────────────────────────────────────────────────
def _conn():
    if psycopg2 is None:
        return None
    dsn = (os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL")
           or os.environ.get("POSTGRES_URL"))
    if not dsn:
        return None
    try:
        return psycopg2.connect(dsn, connect_timeout=8)
    except Exception:
        return None


def _h(s: Any) -> str:
    """Escape HTML — never trust DB content."""
    return html.escape("" if s is None else str(s), quote=True)


def _esc_attr(s: Any) -> str:
    """Escape for HTML attribute (stricter)."""
    return html.escape("" if s is None else str(s), quote=True)


def _round(x, digits=2):
    try:
        return round(float(x), digits)
    except Exception:
        return None


def _slug(s: str) -> str:
    """Make a URL-safe slug — lowercase, dashes, no special chars."""
    if not s:
        return ""
    out = []
    for c in s.lower():
        if c.isalnum():
            out.append(c)
        elif c in (" ", "-", "_", "/"):
            out.append("-")
    return "".join(out).strip("-").replace("--", "-")


# ═════════════════════════════════════════════════════════════════════
# COMMON BASE TEMPLATE (used by all 3 page types)
# ═════════════════════════════════════════════════════════════════════
def _base_html(*, title: str, description: str, canonical: str,
               og_image: str, schema_jsonld: str, body_html: str,
               og_type: str = "website") -> str:
    """Wrap inner body in the canonical DC Hub layout."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_h(title)}</title>
<meta name="description" content="{_esc_attr(description)}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="index, follow, max-image-preview:large">

<link rel="canonical" href="{_esc_attr(canonical)}">

<meta property="og:title" content="{_esc_attr(title)}">
<meta property="og:description" content="{_esc_attr(description)}">
<meta property="og:url" content="{_esc_attr(canonical)}">
<meta property="og:type" content="{_esc_attr(og_type)}">
<meta property="og:image" content="{_esc_attr(og_image)}">
<meta property="og:site_name" content="DC Hub">

<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{_esc_attr(title)}">
<meta name="twitter:description" content="{_esc_attr(description)}">
<meta name="twitter:image" content="{_esc_attr(og_image)}">

<script type="application/ld+json">
{schema_jsonld}
</script>

<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://dchub.cloud/static/dchub-brand.css">
<style>
  body {{ font-family: 'Instrument Sans', system-ui, sans-serif; max-width: 880px; margin: 0 auto; padding: 24px; line-height: 1.55; color: #0a2540; }}
  header.dc-seo {{ border-bottom: 1px solid #e1e5ec; padding-bottom: 20px; margin-bottom: 28px; }}
  h1 {{ font-size: 2rem; margin: 0 0 8px; }}
  .lede {{ font-size: 1.05rem; color: #5a6b85; margin: 4px 0 20px; }}
  h2 {{ margin-top: 40px; padding-top: 16px; border-top: 1px solid #f0f2f6; font-size: 1.35rem; }}
  table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
  table th, table td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid #e9eef5; }}
  table th {{ font-weight: 600; color: #5a6b85; width: 38%; }}
  ul.facility-list, ol.facility-list {{ padding-left: 1.2em; }}
  ul.facility-list li, ol.facility-list li {{ margin: 6px 0; }}
  .badges span {{ display: inline-block; padding: 4px 10px; margin-right: 6px; border-radius: 4px; background: #eef2f8; font-size: 0.85rem; color: #1976d2; }}
  .cta {{ display: block; background: #1976d2; color: white; text-decoration: none; padding: 14px 24px; border-radius: 8px; margin: 24px 0 8px; font-weight: 600; text-align: center; }}
  .cta.secondary {{ background: #eef2f8; color: #1976d2; }}
  .cta:hover {{ filter: brightness(1.05); }}
  footer {{ margin-top: 60px; padding-top: 20px; border-top: 1px solid #e1e5ec; color: #5a6b85; font-size: 0.85rem; }}
  footer a {{ color: #1976d2; }}
  .breadcrumb {{ font-size: 0.85rem; color: #5a6b85; margin-bottom: 14px; }}
  .breadcrumb a {{ color: #5a6b85; text-decoration: none; }}
  .breadcrumb a:hover {{ color: #1976d2; }}
  .pill {{ display: inline-block; padding: 2px 8px; border-radius: 12px; background: #eef2f8; font-size: 0.78rem; color: #1976d2; margin-left: 6px; }}
</style>
</head>
<body>
{body_html}
<footer>
  <p>This data is provided by <a href="https://dchub.cloud">DC Hub Intelligence</a> — real-time data center market intelligence for AI agents and humans.
  Free MCP API: <code>https://dchub.cloud/mcp</code> · <a href="https://dchub.cloud/signup">Get free dev key</a></p>
  <p>21,000+ facilities · 7 ISO grid feeds · $324B+ M&amp;A tracked · 540+ project pipeline</p>
</footer>
</body>
</html>"""


# ═════════════════════════════════════════════════════════════════════
# FACILITY PAGE — /facility/<id>
# ═════════════════════════════════════════════════════════════════════
@seo_pages_bp.get("/facility/<id_or_slug>")
def facility_page(id_or_slug: str):
    """Server-rendered facility landing page. SEO-optimized for long-tail
    queries like 'Equinix DC15 Ashburn specs' or '<facility> data center'."""
    id_or_slug = id_or_slug.strip()
    if not id_or_slug or len(id_or_slug) > 200:
        abort(404)

    c = _conn()
    if c is None:
        return _error_page("Database temporarily unavailable", 503)

    row = None
    # r-facility-slug-md5 (2026-05-27): the map + explorer build slugs as
    # `<provider>-<name>-<MD5(id)[:8]>`. The previous name-slug match here
    # only resolved the middle portion ("switch-tahoe-reno"), not the full
    # hash-suffixed slug ("switch-ltd-switch-tahoe-reno-311abb49"). The
    # /api/v1/facility/<slug> JSON endpoint at main.py:13070 uses the
    # MD5-hash resolution. Mirror that here so the HTML facility page
    # works for the same slug clients send.
    hash8 = None
    try:
        _parts = id_or_slug.rsplit('-', 1)
        if len(_parts) == 2 and len(_parts[1]) == 8 and all(ch in '0123456789abcdef' for ch in _parts[1].lower()):
            hash8 = _parts[1].lower()
    except Exception:
        hash8 = None
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Round 33: query discovered_facilities (21k rows, primary table)
            # rather than legacy facilities (12k rows). discovered_facilities
            # has integer SERIAL IDs, no tier/sqft/certifications columns.
            # Match strategy (in order):
            #   1. integer id direct: /facility/3885
            #   2. MD5-hash slug suffix: /facility/anything-<8hex>
            #   3. name-slug fallback: /facility/switch-tahoe-reno
            cur.execute("""
                SELECT id, name, provider, address, city, state, country,
                       latitude, longitude, power_mw, status,
                       source, source_url, confidence_score, last_updated
                  FROM discovered_facilities
                 WHERE (CAST(id AS TEXT) = %s)
                    OR (%s IS NOT NULL AND LEFT(MD5(id::text), 8) = %s)
                    OR LOWER(REPLACE(REPLACE(COALESCE(name,''),' ','-'),',','')) = LOWER(%s)
                 LIMIT 1
            """, (id_or_slug, hash8, hash8, id_or_slug))
            row = cur.fetchone()

        if not row:
            return _error_page(f"Facility '{_h(id_or_slug)}' not found.", 404)

        # Find similar facilities nearby (same city) for "related" section
        nearby = []
        try:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, name, provider, power_mw
                      FROM discovered_facilities
                     WHERE city = %s AND state = %s AND id != %s
                       AND COALESCE(is_duplicate, 0) = 0
                     ORDER BY power_mw DESC NULLS LAST
                     LIMIT 8
                """, (row['city'], row['state'], row['id']))
                nearby = cur.fetchall()
        except Exception:
            pass
    finally:
        try: c.close()
        except Exception: pass

    return Response(
        _render_facility(row, nearby),
        mimetype="text/html",
        headers={
            "Cache-Control": "public, max-age=900, s-maxage=3600",
            "X-DC-Page-Source": "seo-facility",
        },
    )


def _render_facility(f: dict, nearby: list) -> str:
    name      = f['name'] or 'Unnamed facility'
    operator  = f.get('provider') or 'Unknown operator'
    city      = f.get('city') or ''
    state     = f.get('state') or ''
    country   = f.get('country') or ''
    location  = ", ".join([s for s in (city, state, country) if s]) or 'Location unknown'
    power_mw  = _round(f.get('power_mw'), 1)
    sqft      = f.get('sqft') or 0          # may be absent in discovered_facilities
    tier      = f.get('tier') or 0          # may be absent
    status    = f.get('status') or 'unknown'
    lat       = _round(f.get('latitude'), 5)
    lon       = _round(f.get('longitude'), 5)
    fac_id    = f['id']

    # SEO-optimized title + description
    title = f"{name} — {city}, {state} Data Center | DC Hub"
    if power_mw:
        desc = f"{name} in {location}. {power_mw}MW capacity, operated by {operator}. Live power/fiber/water data, similar facilities, market context — DC Hub Intelligence."
    else:
        desc = f"{name} data center in {location}. Operated by {operator}. Detailed power, fiber, and connectivity data on DC Hub."

    canonical = f"https://api.dchub.cloud/facility/{fac_id}"
    og_image  = f"https://dchub.cloud/static/og/facility-{fac_id}.png"  # generated lazily

    schema = f"""{{
  "@context": "https://schema.org",
  "@type": "Place",
  "name": "{_esc_attr(name)}",
  "description": "{_esc_attr(desc[:200])}",
  "url": "{canonical}",
  "address": {{
    "@type": "PostalAddress",
    "addressLocality": "{_esc_attr(city)}",
    "addressRegion": "{_esc_attr(state)}",
    "addressCountry": "{_esc_attr(country)}"
  }},
  "geo": {{"@type": "GeoCoordinates", "latitude": "{lat or ''}", "longitude": "{lon or ''}"}},
  "additionalType": "https://schema.org/DataCenter"
}}"""

    # Build the body
    badges = []
    if status == 'active':  badges.append('<span>Active</span>')
    if tier and int(tier) > 0: badges.append(f'<span>Tier {int(tier)}</span>')
    if power_mw:               badges.append(f'<span>{power_mw} MW</span>')
    badges_html = '<div class="badges">' + ''.join(badges) + '</div>' if badges else ''

    nearby_html = ""
    if nearby:
        items = []
        for n in nearby:
            n_mw = _round(n.get('power_mw'), 1)
            mw_str = f" — {n_mw}MW" if n_mw else ""
            items.append(f'<li><a href="/facility/{_esc_attr(n["id"])}">{_h(n["name"])}</a> · {_h(n.get("provider") or "Unknown")}{_h(mw_str)}</li>')
        nearby_html = f"""
  <h2>Other Data Centers in {_h(city)}, {_h(state)}</h2>
  <ul class="facility-list">
    {''.join(items)}
  </ul>
  <p><a href="/markets/{_esc_attr(_slug(city + '-' + state))}">All {_h(city)} data centers →</a></p>"""

    map_link = ""
    if lat and lon:
        map_link = f'<a href="https://www.openstreetmap.org/?mlat={lat}&mlon={lon}&zoom=15" target="_blank" rel="noopener">View on map ↗</a>'

    body = f"""<header class="dc-seo">
  <nav class="breadcrumb">
    <a href="/">DC Hub</a> · <a href="/markets/{_esc_attr(_slug(city + '-' + state))}">{_h(city)}, {_h(state)}</a> · {_h(name)}
  </nav>
  <h1>{_h(name)}</h1>
  <p class="lede">{_h(operator)} data center in {_h(location)}.</p>
  {badges_html}
</header>

<section id="overview">
  <h2>Overview</h2>
  <table>
    <tr><th>Operator</th><td>{_h(operator)}</td></tr>
    <tr><th>Location</th><td>{_h(location)} {map_link}</td></tr>
    <tr><th>Power capacity</th><td>{_h(str(power_mw) + ' MW' if power_mw else 'Not disclosed')}</td></tr>
    <tr><th>Floor space</th><td>{_h(f'{int(sqft):,} sq ft' if sqft else 'Not disclosed')}</td></tr>
    <tr><th>Tier</th><td>{_h('Tier ' + str(int(tier)) if tier and int(tier) > 0 else 'Not disclosed')}</td></tr>
    <tr><th>Status</th><td>{_h(status.title())}</td></tr>
    <tr><th>Coordinates</th><td>{lat or '?'}, {lon or '?'}</td></tr>
  </table>
</section>

{nearby_html}

<section id="cta">
  <h2>Get more facility intelligence</h2>
  <p>This page shows the public summary. The full facility profile includes M&amp;A history, lease comparables, power profile breakdown, fiber carrier presence, water risk score, and competitive analysis.</p>
  <a href="/api/v1/facility/{_esc_attr(fac_id)}/report" class="cta">Generate full PDF report</a>
  <a href="/signup?from=facility-{_esc_attr(fac_id)}" class="cta secondary">Or: free MCP API access</a>
</section>

<section id="api">
  <h2>For AI agents</h2>
  <p>This facility's data is available via the DC Hub MCP server. Query it programmatically:</p>
  <pre style="background:#f6f7f9;padding:14px;border-radius:6px;overflow-x:auto;"><code>POST https://dchub.cloud/mcp
{{
  "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": {{
    "name": "get_facility",
    "arguments": {{ "facility_id": "{_h(fac_id)}" }}
  }}
}}</code></pre>
</section>"""

    return _base_html(
        title=title, description=desc, canonical=canonical,
        og_image=og_image, schema_jsonld=schema, body_html=body,
        og_type="business.business",
    )


# ═════════════════════════════════════════════════════════════════════
# MARKET PAGE — /markets/<slug>
# ═════════════════════════════════════════════════════════════════════
@seo_pages_bp.get("/markets/<slug>")
def market_page(slug: str):
    slug = slug.strip().lower()
    if not slug or len(slug) > 100:
        abort(404)

    c = _conn()
    if c is None:
        return _error_page("Database temporarily unavailable", 503)

    parts = slug.replace('_', '-').split('-')
    if len(parts) < 2:
        return _error_page(f"Market slug '{_h(slug)}' invalid.", 404)
    state_guess = parts[-1].upper()
    city_guess = ' '.join(parts[:-1]).title()

    facilities = []
    stats = None
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, name, provider, power_mw, status
                  FROM discovered_facilities
                 WHERE (LOWER(city) = LOWER(%s) AND UPPER(state) = %s)
                    OR LOWER(COALESCE(city,'') || '-' || COALESCE(state,'')) = LOWER(%s)
                 ORDER BY power_mw DESC NULLS LAST
                 LIMIT 200
            """, (city_guess, state_guess, slug))
            facilities = cur.fetchall()

            if facilities:
                cur.execute("""
                    SELECT
                        COUNT(*)                       AS facility_count,
                        COALESCE(SUM(power_mw), 0)    AS total_mw,
                        COUNT(DISTINCT provider)      AS operator_count,
                        AVG(power_mw)                 AS avg_mw,
                        MAX(power_mw)                 AS max_mw
                      FROM discovered_facilities
                     WHERE LOWER(city) = LOWER(%s) AND UPPER(state) = %s
                """, (city_guess, state_guess))
                stats = cur.fetchone()
    finally:
        try: c.close()
        except Exception: pass

    if not facilities:
        return _error_page(
            f"Market '{_h(city_guess)}, {_h(state_guess)}' not found. "
            "Try a different city or check the URL.", 404)

    return Response(
        _render_market(slug, city_guess, state_guess, facilities, stats),
        mimetype="text/html",
        headers={
            "Cache-Control": "public, max-age=1800, s-maxage=3600",
            "X-DC-Page-Source": "seo-market",
        },
    )


def _render_market(slug, city, state, facilities, stats) -> str:
    canonical = f"https://api.dchub.cloud/markets/{slug}"
    n_fac     = stats['facility_count'] if stats else len(facilities)
    total_mw  = _round(stats['total_mw'], 1) if stats and stats['total_mw'] else 0
    n_op      = stats['operator_count'] if stats else 0

    title = f"{city}, {state} Data Centers — {n_fac} facilities, {total_mw} MW | DC Hub"
    desc  = f"Complete {city}, {state} data center market intelligence. {n_fac} facilities, {total_mw}MW total capacity across {n_op} operators. Live power, fiber, M&A data."

    schema = f"""{{
  "@context": "https://schema.org",
  "@type": "Place",
  "name": "{_esc_attr(city + ', ' + state + ' Data Center Market')}",
  "description": "{_esc_attr(desc[:200])}",
  "url": "{canonical}"
}}"""

    # Top operators in market
    from collections import Counter
    op_counter = Counter()
    op_mw = {}
    for f in facilities:
        op = f.get('provider') or 'Unknown'
        op_counter[op] += 1
        op_mw[op] = op_mw.get(op, 0) + (f.get('power_mw') or 0)
    top_ops = op_counter.most_common(10)
    ops_html = ""
    for op, cnt in top_ops:
        mw = _round(op_mw[op], 0)
        ops_html += f'<li><strong>{_h(op)}</strong> — {cnt} facility{"" if cnt==1 else "ies"}, {_h(mw)}MW</li>'

    # All facilities
    fac_html = ""
    for f in facilities[:50]:
        mw = _round(f.get('power_mw'), 1)
        mw_str = f" — {mw}MW" if mw else ""
        fac_html += f'<li><a href="/facility/{_esc_attr(f["id"])}">{_h(f["name"])}</a> · {_h(f.get("provider") or "Unknown")}{_h(mw_str)}</li>'

    body = f"""<header class="dc-seo">
  <nav class="breadcrumb"><a href="/">DC Hub</a> · Markets · {_h(city)}, {_h(state)}</nav>
  <h1>{_h(city)}, {_h(state)} — Data Center Market</h1>
  <p class="lede"><strong>{n_fac}</strong> facilities · <strong>{total_mw} MW</strong> total capacity · <strong>{n_op}</strong> operators</p>
</header>

<section id="top-operators">
  <h2>Top Operators</h2>
  <ol class="facility-list">{ops_html}</ol>
</section>

<section id="all-facilities">
  <h2>All Data Centers in {_h(city)}, {_h(state)}</h2>
  <ul class="facility-list">{fac_html}</ul>
  {('<p><em>Showing top 50 by capacity. ' + str(n_fac - 50) + ' more in dataset.</em></p>') if n_fac > 50 else ''}
</section>

<section id="cta">
  <h2>Get the {_h(city)} market report</h2>
  <p>The full report includes lease comparables, pipeline projects, grid capacity analysis, and competitive landscape.</p>
  <a href="/api/v1/market/{_esc_attr(slug)}/report" class="cta">Generate market report (PDF)</a>
  <a href="/signup?from=market-{_esc_attr(slug)}" class="cta secondary">Or: free MCP API access</a>
</section>"""

    return _base_html(
        title=title, description=desc, canonical=canonical,
        og_image=f"https://dchub.cloud/static/og/market-{slug}.png",
        schema_jsonld=schema, body_html=body,
    )


# ═════════════════════════════════════════════════════════════════════
# ISO / GRID PAGE — /grids/<code>
# ═════════════════════════════════════════════════════════════════════
ISO_REGISTRY = {
    'caiso':  ("California ISO",            "us",    ["California"]),
    'pjm':    ("PJM Interconnection",       "us",    ["VA", "PA", "NJ", "MD", "DC", "DE", "OH", "WV", "KY", "MI", "IN", "IL", "NC", "TN"]),
    'ercot':  ("ERCOT (Texas)",             "us",    ["TX"]),
    'miso':   ("Midcontinent ISO",          "us",    ["MN", "WI", "IA", "MO", "IL", "IN", "MI", "AR", "MS", "LA", "TX", "ND", "SD", "MT", "KY"]),
    'nyiso':  ("New York ISO",              "us",    ["NY"]),
    'spp':    ("Southwest Power Pool",      "us",    ["KS", "OK", "NE", "AR", "LA", "NM", "TX", "MS", "MO", "ND", "SD"]),
    'isone':  ("ISO New England",           "us",    ["MA", "CT", "RI", "VT", "NH", "ME"]),
    'hydroquebec': ("Hydro-Québec",         "ca",    ["QC"]),
    'aeso':   ("AESO (Alberta)",            "ca",    ["AB"]),
    'nordpool': ("Nord Pool (Nordics)",     "eu",    []),
    'uknationalgrid': ("National Grid UK",  "eu",    []),
    'aemo':   ("AEMO (Australia)",          "apac",  []),
    'japan':  ("Japan (TEPCO + KEPCO)",     "apac",  []),
    'germany': ("Germany",                  "eu",    []),
    'france': ("France (RTE)",              "eu",    []),
    'cenace': ("Mexico (CENACE)",           "americas", []),
}


@seo_pages_bp.get("/grids/<code>")
def iso_page(code: str):
    code = code.strip().lower()
    if code not in ISO_REGISTRY:
        return _error_page(f"Grid '{_h(code)}' not found. Try one of: " + ", ".join(ISO_REGISTRY.keys()), 404)
    display, region, states = ISO_REGISTRY[code]

    c = _conn()
    facility_count = 0
    total_mw = 0
    top_facs = []
    if c is not None and states:
        try:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT COUNT(*) AS n, COALESCE(SUM(power_mw),0) AS mw
                      FROM facilities
                     WHERE state = ANY(%s) AND country IN ('US','USA','United States')
                """, (states,))
                r = cur.fetchone()
                if r:
                    facility_count = r['n']
                    total_mw = _round(r['mw'], 0)
                cur.execute("""
                    SELECT id, name, provider, city, state, power_mw
                      FROM facilities
                     WHERE state = ANY(%s) AND country IN ('US','USA','United States')
                     ORDER BY power_mw DESC NULLS LAST
                     LIMIT 15
                """, (states,))
                top_facs = cur.fetchall()
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass

    canonical = f"https://api.dchub.cloud/grids/{code}"
    title = f"{display} — Grid + Data Center Intelligence | DC Hub"
    if facility_count:
        desc = f"Real-time {display} grid data + {facility_count} data centers totaling {total_mw}MW. Fuel mix, electricity prices, capacity scarcity, and renewable share."
    else:
        desc = f"Real-time {display} grid data: fuel mix, electricity prices, capacity scarcity, renewable share. DC Hub Intelligence."

    schema = f"""{{
  "@context": "https://schema.org",
  "@type": "GovernmentOrganization",
  "name": "{_esc_attr(display)}",
  "description": "{_esc_attr(desc[:200])}",
  "url": "{canonical}"
}}"""

    facs_html = ""
    for f in top_facs:
        mw = _round(f.get('power_mw'), 1)
        mw_str = f" — {mw}MW" if mw else ""
        facs_html += f'<li><a href="/facility/{_esc_attr(f["id"])}">{_h(f["name"])}</a> · {_h(f["city"])}, {_h(f["state"])} · {_h(f.get("provider") or "Unknown")}{_h(mw_str)}</li>'

    states_str = ", ".join(states) if states else "International — see overview"

    body = f"""<header class="dc-seo">
  <nav class="breadcrumb"><a href="/">DC Hub</a> · Grids · {_h(display)}</nav>
  <h1>{_h(display)}</h1>
  <p class="lede">Grid intelligence + {facility_count} data centers totaling {total_mw}MW.</p>
</header>

<section id="overview">
  <h2>Overview</h2>
  <table>
    <tr><th>Region</th><td>{_h(region.upper())}</td></tr>
    <tr><th>Coverage</th><td>{_h(states_str)}</td></tr>
    <tr><th>Facilities tracked</th><td>{facility_count:,}</td></tr>
    <tr><th>Total capacity</th><td>{total_mw} MW</td></tr>
    <tr><th>Live grid data API</th><td><code>GET /api/v1/grid/{code}</code></td></tr>
  </table>
</section>

<section id="top-facilities">
  <h2>Top Data Centers in {_h(display)}</h2>
  <ul class="facility-list">{facs_html if facs_html else '<li><em>No facility data available yet for this grid region.</em></li>'}</ul>
</section>

<section id="cta">
  <h2>Live {_h(display)} intelligence</h2>
  <p>Real-time fuel mix, LMP, demand, renewable share via DC Hub MCP API or this dashboard.</p>
  <a href="/grid-intelligence?iso={_esc_attr(code)}" class="cta">View live grid dashboard</a>
  <a href="/signup?from=grids-{_esc_attr(code)}" class="cta secondary">Free MCP API access</a>
</section>

<section id="api">
  <h2>For AI agents</h2>
  <pre style="background:#f6f7f9;padding:14px;border-radius:6px;overflow-x:auto;"><code>POST https://dchub.cloud/mcp
{{
  "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": {{
    "name": "get_grid_data",
    "arguments": {{ "iso": "{_h(code)}", "metric": "fuel_mix" }}
  }}
}}</code></pre>
</section>"""

    return _base_html(
        title=title, description=desc, canonical=canonical,
        og_image=f"https://dchub.cloud/static/og/grid-{code}.png",
        schema_jsonld=schema, body_html=body,
    )


# ═════════════════════════════════════════════════════════════════════
# SITEMAPS — submit to Google Search Console
# ═════════════════════════════════════════════════════════════════════
@seo_pages_bp.get("/sitemap-index.xml")
def sitemap_index():
    today = _dt.date.today().isoformat()
    # Round 34 fix: point at api.dchub.cloud (where Flask serves these).
    # dchub.cloud/sitemap-*.xml is shadowed by CF Pages → 404.
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://api.dchub.cloud/sitemap-facilities.xml</loc><lastmod>{today}</lastmod></sitemap>
  <sitemap><loc>https://api.dchub.cloud/sitemap-markets.xml</loc><lastmod>{today}</lastmod></sitemap>
  <sitemap><loc>https://api.dchub.cloud/sitemap-grids.xml</loc><lastmod>{today}</lastmod></sitemap>
</sitemapindex>"""
    return Response(xml, mimetype='application/xml',
                     headers={'Cache-Control': 'public, max-age=3600'})


@seo_pages_bp.get("/sitemap-facilities.xml")
def sitemap_facilities():
    c = _conn()
    urls = []
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT id, last_updated FROM discovered_facilities
                     WHERE COALESCE(is_duplicate, 0) = 0
                       AND latitude IS NOT NULL
                     ORDER BY power_mw DESC NULLS LAST
                     LIMIT 50000
                """)
                urls = cur.fetchall()
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass

    items = []
    for fid, lastmod in urls:
        lastmod_str = ""
        if lastmod:
            try: lastmod_str = f"<lastmod>{str(lastmod)[:10]}</lastmod>"
            except Exception: pass
        items.append(f'  <url><loc>https://api.dchub.cloud/facility/{fid}</loc>{lastmod_str}<changefreq>monthly</changefreq><priority>0.7</priority></url>')

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + '\n'.join(items) + '\n</urlset>'
    return Response(xml, mimetype='application/xml',
                     headers={'Cache-Control': 'public, max-age=3600'})


@seo_pages_bp.get("/sitemap-markets.xml")
def sitemap_markets():
    c = _conn()
    markets = []
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT LOWER(REPLACE(city,' ','-') || '-' || LOWER(state)) AS slug
                      FROM discovered_facilities
                     WHERE city IS NOT NULL AND state IS NOT NULL
                       AND COALESCE(is_duplicate, 0) = 0
                       AND country IN ('US','USA','United States')
                     GROUP BY city, state
                    HAVING COUNT(*) >= 3
                """)
                markets = [r[0] for r in cur.fetchall()]
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass

    items = '\n'.join(
        f'  <url><loc>https://api.dchub.cloud/markets/{slug}</loc><changefreq>weekly</changefreq><priority>0.8</priority></url>'
        for slug in markets
    )
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{items}\n</urlset>'
    return Response(xml, mimetype='application/xml',
                     headers={'Cache-Control': 'public, max-age=3600'})


@seo_pages_bp.get("/sitemap-grids.xml")
def sitemap_grids():
    items = '\n'.join(
        f'  <url><loc>https://api.dchub.cloud/grids/{code}</loc><changefreq>daily</changefreq><priority>0.9</priority></url>'
        for code in ISO_REGISTRY.keys()
    )
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{items}\n</urlset>'
    return Response(xml, mimetype='application/xml',
                     headers={'Cache-Control': 'public, max-age=3600'})


# ═════════════════════════════════════════════════════════════════════
# ERROR PAGE
# ═════════════════════════════════════════════════════════════════════
def _error_page(message: str, code: int = 404) -> Response:
    body = f"""<header class="dc-seo">
  <nav class="breadcrumb"><a href="/">DC Hub</a></nav>
  <h1>{('Page not found' if code == 404 else 'Service issue')}</h1>
  <p class="lede">{_h(message)}</p>
</header>
<section><a href="/" class="cta">Back to DC Hub home</a></section>"""
    return Response(
        _base_html(
            title=f"DC Hub — {'Not found' if code == 404 else 'Error'}",
            description=message[:160],
            canonical="https://dchub.cloud",
            og_image="https://dchub.cloud/static/og/default.png",
            schema_jsonld='{"@context":"https://schema.org","@type":"WebPage"}',
            body_html=body,
        ),
        status=code,
        mimetype="text/html",
        headers={"Cache-Control": "no-cache"},
    )


# ═════════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ═════════════════════════════════════════════════════════════════════
@seo_pages_bp.get("/api/v1/seo-pages/health")
def seo_health():
    from flask import jsonify
    c = _conn()
    db_ok = c is not None
    if c:
        try: c.close()
        except Exception: pass
    return jsonify(
        ok=True,
        version="round-33-seo-pages-v1",
        routes=["/facility/<id>", "/markets/<slug>", "/grids/<code>",
                "/sitemap-index.xml", "/sitemap-facilities.xml",
                "/sitemap-markets.xml", "/sitemap-grids.xml"],
        db_ok=db_ok,
        iso_count=len(ISO_REGISTRY),
    )
