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
from flask import Blueprint, Response, abort, redirect, request

try:
    import psycopg2
    import psycopg2.extras
except Exception:  # pragma: no cover
    psycopg2 = None

seo_pages_bp = Blueprint("seo_pages", __name__)


# ─────────────────────────────────────────────────────────────────────
# CF-routable prefix guard (Phase HJ, 2026-06-05)
# ─────────────────────────────────────────────────────────────────────
#
# The CF zone-level Workers Route uses an ALLOWLIST: paths under
# certain prefixes 403 with "DNS points to prohibited IP" (Error 1000)
# at the edge regardless of what Render origin serves. Verified
# 2026-06-05 via live curl:
#
#   ALLOWED (return 200): /markets/* /facility/* /facilities/*
#                         /partners/* /sites/* /reports/* /grid/*
#                         /dcpi/* /operators/* /listings/* /iso/*
#                         /state-of-power/* /ai-capacity-index
#                         /hyperscaler-deals /freshness /enterprise
#                         /vertex (added today)
#
#   BLOCKED (403 Err 1000): /aws/* /docs/* /research/* /address/*
#                           /interxion-* /moltbook-* /api-* etc.
#
# Every new SEO landing MUST register under an allowed prefix or it
# will silently 403 on the edge for hours/days before being caught.
# Autopilot bit this bug today — created 7 landings under /aws/*,
# /address/*, /interxion-*, /moltbook-* — they all 403'd before SEO
# noticed.
#
# Use _assert_cf_routable_path() inside any new SEO landing route to
# fail fast at startup. Returns False (and logs a warning) for paths
# outside the allowlist; the autopilot's pre-commit fence reads the
# warning log and refuses to ship.

_CF_ALLOWED_PREFIXES = (
    "/markets/", "/operators/",
    "/facility/", "/facilities/",
    "/partners/",
    "/sites/", "/listings/",
    "/reports/",
    "/grid/", "/iso/",
    "/dcpi/",
    "/state-of-power/",
)
_CF_ALLOWED_EXACT = {
    "/markets", "/operators", "/partners", "/sites", "/listings",
    "/reports", "/grid", "/iso", "/dcpi",
    "/state-of-power", "/ai-capacity-index", "/hyperscaler-deals",
    "/freshness", "/enterprise", "/coverage", "/transparency",
    "/grid-intelligence", "/grid-transition",
    "/site-selection", "/deal-autopsy",
    "/pipeline-report", "/dcgi",
    "/vertex",
}


def _assert_cf_routable_path(path: str, source: str = "seo_pages") -> bool:
    """Return True iff `path` is under a CF-routable prefix.

    Logs an explicit error when the path would 403 at the CF edge.
    Used by SEO landing creators + brain autopilot to refuse to
    register a new public URL that's behind the zone-worker shadow.
    """
    if not path or not path.startswith("/"):
        return False
    if path in _CF_ALLOWED_EXACT:
        return True
    for pfx in _CF_ALLOWED_PREFIXES:
        if path.startswith(pfx):
            return True
    try:
        print(
            f"[seo_pages][CF-ROUTABLE-GUARD] REFUSING path={path!r} from "
            f"{source}: not under CF-allowed prefixes "
            f"({sorted(_CF_ALLOWED_EXACT)[:6]}... + "
            f"{list(_CF_ALLOWED_PREFIXES)}). This would 403 at the edge "
            f"with CF Error 1000. Re-route under /facility/* or /markets/* "
            f"or /partners/* instead.",
            flush=True,
        )
    except Exception:
        pass
    return False


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
  <p>21,000+ facilities · 7 ISO grid feeds · 2,000+ M&amp;A deals tracked · 540+ project pipeline</p>
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

    canonical = f"https://dchub.cloud/facility/{fac_id}"
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
  <a href="/pricing?ref=facility-{_esc_attr(fac_id)}-report" class="cta">Generate full PDF report</a>
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
    canonical = f"https://dchub.cloud/markets/{slug}"
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
  <a href="/pricing?ref=market-{_esc_attr(slug)}-report" class="cta">Generate market report (PDF)</a>
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

    canonical = f"https://dchub.cloud/grids/{code}"
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
    # Round 34 fix: point at dchub.cloud (where Flask serves these).
    # dchub.cloud/sitemap-*.xml is shadowed by CF Pages → 404.
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://dchub.cloud/sitemap-facilities.xml</loc><lastmod>{today}</lastmod></sitemap>
  <sitemap><loc>https://dchub.cloud/sitemap-markets.xml</loc><lastmod>{today}</lastmod></sitemap>
  <sitemap><loc>https://dchub.cloud/sitemap-grids.xml</loc><lastmod>{today}</lastmod></sitemap>
  <sitemap><loc>https://dchub.cloud/sitemap-landings.xml</loc><lastmod>{today}</lastmod></sitemap>
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
        items.append(f'  <url><loc>https://dchub.cloud/facility/{fid}</loc>{lastmod_str}<changefreq>monthly</changefreq><priority>0.7</priority></url>')

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
                       AND TRIM(city) <> '' AND TRIM(state) <> ''
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

    # r71-seo: skip malformed slugs. Empty/whitespace city or state rows produced
    # junk like /markets/- and /markets/abu-dhabi- (trailing hyphen) — thin pages
    # that dilute crawl budget. Don't advertise them in the sitemap.
    def _valid_market_slug(s):
        s = (s or "").strip()
        return (len(s) >= 3 and not s.startswith("-")
                and not s.endswith("-") and any(ch.isalnum() for ch in s))
    items = '\n'.join(
        f'  <url><loc>https://dchub.cloud/markets/{slug}</loc><changefreq>weekly</changefreq><priority>0.8</priority></url>'
        for slug in markets if _valid_market_slug(slug)
    )
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{items}\n</urlset>'
    return Response(xml, mimetype='application/xml',
                     headers={'Cache-Control': 'public, max-age=3600'})


@seo_pages_bp.get("/sitemap-grids.xml")
def sitemap_grids():
    items = '\n'.join(
        f'  <url><loc>https://dchub.cloud/grids/{code}</loc><changefreq>daily</changefreq><priority>0.9</priority></url>'
        for code in ISO_REGISTRY.keys()
    )
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{items}\n</urlset>'
    return Response(xml, mimetype='application/xml',
                     headers={'Cache-Control': 'public, max-age=3600'})


# round-34: high-intent SEO landings sitemap (AWS region codes, addresses,
# Interxion Frankfurt, Moltbook docs). These are zero-click queries with
# strong impression volume — sitemapped + linked to from the homepage so
# Google indexes them on the next crawl.
@seo_pages_bp.get("/sitemap-landings.xml")
def sitemap_landings():
    # round-35: advertise the un-blocked /facility/aws-<code>, /facility/<addr>,
    # /markets/interxion-frankfurt, /partners/moltbook-api paths. The legacy
    # /aws/*, /address/*, single-segment paths are CF zone-worker 403-blocked
    # ("DNS points to prohibited IP"); sitemapping them was wasting crawl budget.
    urls = []
    for code in AWS_REGION_MAP.keys():
        urls.append(f'  <url><loc>https://dchub.cloud/facility/aws-{code}</loc><changefreq>monthly</changefreq><priority>0.85</priority></url>')
    for slug in ADDRESS_MAP.keys():
        urls.append(f'  <url><loc>https://dchub.cloud/facility/{slug}</loc><changefreq>monthly</changefreq><priority>0.85</priority></url>')
    urls.append('  <url><loc>https://dchub.cloud/markets/interxion-frankfurt</loc><changefreq>weekly</changefreq><priority>0.85</priority></url>')
    urls.append('  <url><loc>https://dchub.cloud/partners/moltbook-api</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>')
    # Market Brief v1 (2026-06-06): 15 seed markets get sitemapped on day 1
    # (wave 1 = 5 markets + wave 2 = 10 markets added same day). Beyond
    # these the surface auto-renders for any market_power_scores row, but
    # only the seed fifteen are hand-QA'd + pre-warmed by the cron.
    # Kept in lock-step with routes.market_brief.SEED_MARKETS.
    for _mb_slug in (
        # Wave 1
        'northern-virginia', 'dallas', 'phoenix', 'atlanta', 'chicago',
        # Wave 2
        'silicon-valley', 'new-york', 'portland', 'hillsboro', 'reno',
        'columbus', 'salt-lake-city', 'charlotte', 'denver', 'madison',
    ):
        urls.append(f'  <url><loc>https://dchub.cloud/markets/{_mb_slug}/brief</loc><changefreq>daily</changefreq><priority>0.9</priority></url>')
    items = '\n'.join(urls)
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{items}\n</urlset>'
    return Response(xml, mimetype='application/xml',
                     headers={'Cache-Control': 'public, max-age=3600'})


# ═════════════════════════════════════════════════════════════════════
# HIGH-INTENT QUERY LANDINGS (round-34 SEO, 2026-06-04)
# ─────────────────────────────────────────────────────────────────────
# Google Search Console showed 7 high-intent queries with hundreds of
# impressions and ZERO clicks because no targeted page existed:
#   - AWS region codes (iad36, db1, kix10, sjc29) — operators searching
#     for specific AWS facility identifiers; Digital Realty/Equinix host
#     these campuses, we already have the facilities indexed but the
#     existing pages don't include the AWS code in title/h1/meta.
#   - "1725 comstock street" — Digital Realty SJC 1725 Comstock St campus.
#   - "interxion frankfurt status" — operators researching the Digital
#     Realty/Interxion Frankfurt campus and grid stability.
#   - "moltbook api documentation" — moltbook.com is a real Reddit-like
#     platform DC Hub integrates with (moltbook_integration.py); operators
#     looking for integration docs land here.
#
# Each landing puts the EXACT search query in <title>, <h1>, meta
# description, AND the first paragraph — the four signals Google's
# scorer weighs most for query-page relevance.
#
# All landings 200 fast (no DB call in the request path for the static
# AWS / address / interxion / moltbook maps; the canonical facility
# id resolution is on-demand and cached).
# ═════════════════════════════════════════════════════════════════════

# AWS region code → (display_query, h1, canonical_facility_slug, operator,
#                    market_city, market_state, market_slug, country, summary)
# The slug is the existing static facility URL slug (see
# dchub-frontend/facilities/ for the canonical files).
AWS_REGION_MAP = {
    "iad36": {
        "query":      "iad36 data center",
        "h1":         "IAD36 Data Center — Digital Realty Northern Virginia (Ashburn)",
        "facility_slug": "digital-realty-digital-realty-northern-virginia-iad36-cc7fa9f2",
        "operator":   "Digital Realty",
        "city":       "Ashburn",
        "state":      "VA",
        "country":    "United States",
        "market_slug": "ashburn-va",
        "lat":        39.0015493,
        "lon":        -77.4793749,
        "aws_region": "us-east-1 (N. Virginia)",
        "summary": (
            "IAD36 is a Digital Realty Northern Virginia data center campus in "
            "Ashburn, VA, identified by the AWS region code IAD36 (us-east-1). "
            "Part of the largest data center market in the world."
        ),
    },
    "db1": {
        "query":      "db1 data center",
        "h1":         "DB1 Data Center — Equinix Dublin DB1 (Ireland)",
        "facility_slug": "equinix-equinix-dublin-db1-3be55f49",
        "operator":   "Equinix",
        "city":       "Dublin",
        "state":      "",
        "country":    "Ireland",
        "market_slug": "dublin-ie",
        "lat":        53.34,
        "lon":        -6.42,
        "aws_region": "eu-west-1 (Ireland)",
        "summary": (
            "DB1 is the Equinix Dublin DB1 data center in Citywest, Dublin, "
            "Ireland — the most-cited AWS Dublin (eu-west-1) carrier-neutral "
            "interconnect campus and a hub for European hyperscale capacity."
        ),
    },
    "kix10": {
        "query":      "kix10 data center",
        "h1":         "KIX10 Data Center — Digital Realty Osaka (Japan)",
        "facility_slug": "digital-realty-digital-realty-osaka-kix10-bdaf59fb",
        "operator":   "Digital Realty",
        "city":       "Osaka",
        "state":      "",
        "country":    "Japan",
        "market_slug": "osaka-jp",
        "lat":        34.6937,
        "lon":        135.5023,
        "aws_region": "ap-northeast-3 (Osaka)",
        "summary": (
            "KIX10 is a Digital Realty Osaka data center serving the AWS "
            "Osaka region (ap-northeast-3). KIX is the IATA code for Kansai "
            "International Airport — the standard AWS naming for the Osaka "
            "metro."
        ),
    },
    "sjc29": {
        "query":      "sjc29 data center",
        "h1":         "SJC29 Data Center — Digital Realty Silicon Valley (San Jose)",
        "facility_slug": "digital-realty-digital-realty-silicon-valley-sjc29-0e364091",
        "operator":   "Digital Realty",
        "city":       "San Jose",
        "state":      "CA",
        "country":    "United States",
        "market_slug": "san-jose-ca",
        "lat":        37.3382,
        "lon":        -121.8863,
        "aws_region": "us-west-1 (N. California)",
        "summary": (
            "SJC29 is a Digital Realty Silicon Valley data center in San "
            "Jose, CA serving the AWS us-west-1 region. Part of Digital "
            "Realty's San Jose / Santa Clara campus cluster."
        ),
    },
}

# Address → existing static facility URL slug
ADDRESS_MAP = {
    "1725-comstock-st-san-jose": {
        "query":      "1725 Comstock Street San Jose data center",
        "h1":         "Data Center at 1725 Comstock Street, San Jose, CA — Digital Realty SJC Campus",
        "address":    "1725 Comstock St, San Jose, CA 95054",
        "facility_slug": "digital-realty-digital-realty-sjc-1725-comstock-st-292e0cd6",
        "operator":   "Digital Realty",
        "city":       "San Jose",
        "state":      "CA",
        "country":    "United States",
        "market_slug": "san-jose-ca",
        "lat":        37.3855,
        "lon":        -121.9655,
        "summary": (
            "1725 Comstock Street, San Jose, CA is a Digital Realty data "
            "center — part of the company's Silicon Valley SJC campus on "
            "Comstock Street alongside 1201 and 1525 Comstock (the same "
            "Santa Clara / North San Jose data center corridor)."
        ),
    },
}


def _aws_landing_html(meta: dict, code: str) -> str:
    """Render a Google-optimized landing page for an AWS region code."""
    q = meta["query"]
    # Title-cased query but keep AWS code in UPPER ("IAD36 Data Center", not "Iad36").
    q_display = code.upper() + " Data Center"
    title = f"{q_display} — {meta['operator']} {meta['city']} | DC Hub"
    desc = (
        f"{q_display}: {meta['summary']} View live power, fiber, water, "
        f"and grid intelligence for the {meta['operator']} {meta['city']} "
        f"campus on DC Hub."
    )
    # round-35: moved off /aws/<code> (CF edge-blocked 403 "DNS points to
    # prohibited IP") onto the un-blocked /facility/aws-<code> prefix.
    # SEO content body unchanged.
    canonical = f"https://dchub.cloud/facility/aws-{code}"
    location = ", ".join([s for s in (meta["city"], meta["state"], meta["country"]) if s])

    schema = f"""{{
  "@context": "https://schema.org",
  "@type": "Place",
  "name": "{_esc_attr(meta['h1'])}",
  "alternateName": "{_esc_attr(code.upper())}",
  "description": "{_esc_attr(desc[:200])}",
  "url": "{canonical}",
  "address": {{
    "@type": "PostalAddress",
    "addressLocality": "{_esc_attr(meta['city'])}",
    "addressRegion": "{_esc_attr(meta['state'])}",
    "addressCountry": "{_esc_attr(meta['country'])}"
  }},
  "geo": {{"@type": "GeoCoordinates", "latitude": "{meta['lat']}", "longitude": "{meta['lon']}"}},
  "additionalType": "https://schema.org/DataCenter"
}}"""

    body = f"""<header class="dc-seo">
  <nav class="breadcrumb">
    <a href="/">DC Hub</a> · <a href="/markets/{_esc_attr(meta['market_slug'])}">{_h(meta['city'])}, {_h(meta['state'] or meta['country'])}</a> · {_h(code.upper())}
  </nav>
  <h1>{_h(meta['h1'])}</h1>
  <p class="lede"><strong>{_h(code.upper())}</strong> is the AWS facility code for {_h(meta['operator'])}'s {_h(meta['city'])} campus serving the AWS region <strong>{_h(meta['aws_region'])}</strong>.</p>
  <div class="badges">
    <span>{_h(code.upper())}</span>
    <span>{_h(meta['operator'])}</span>
    <span>{_h(location)}</span>
    <span>AWS {_h(meta['aws_region'])}</span>
  </div>
</header>

<section id="summary">
  <h2>What is {_h(code.upper())}?</h2>
  <p>{_h(meta['summary'])}</p>
  <table>
    <tr><th>AWS facility code</th><td><strong>{_h(code.upper())}</strong></td></tr>
    <tr><th>AWS region</th><td>{_h(meta['aws_region'])}</td></tr>
    <tr><th>Operator</th><td>{_h(meta['operator'])}</td></tr>
    <tr><th>Location</th><td>{_h(location)}</td></tr>
    <tr><th>Coordinates</th><td>{meta['lat']}, {meta['lon']}</td></tr>
    <tr><th>Canonical facility page</th><td><a href="/facilities/{_esc_attr(meta['facility_slug'])}.html">View full facility profile →</a></td></tr>
  </table>
</section>

<section id="market">
  <h2>{_h(meta['city'])} Data Center Market Context</h2>
  <p>The {_h(code.upper())} campus is one of many data centers in the {_h(meta['city'])} market. See all operators, total MW, and capacity rankings:</p>
  <p><a href="/markets/{_esc_attr(meta['market_slug'])}" class="cta secondary">All {_h(meta['city'])} data centers →</a></p>
</section>

<section id="cta">
  <h2>Get {_h(code.upper())} intelligence</h2>
  <p>Full facility profile includes power profile, fiber carriers on-net, water risk, M&amp;A history, and live grid scarcity for the campus's ISO/TSO. Free MCP API access for AI agents.</p>
  <a href="/facilities/{_esc_attr(meta['facility_slug'])}.html" class="cta">View {_h(code.upper())} full profile</a>
  <a href="/signup?from=aws-{_esc_attr(code)}" class="cta secondary">Free MCP API access</a>
</section>

<section id="api">
  <h2>For AI agents</h2>
  <pre style="background:#f6f7f9;padding:14px;border-radius:6px;overflow-x:auto;"><code>POST https://dchub.cloud/mcp
{{
  "jsonrpc": "2.0", "id": 1, "method": "tools/call",
  "params": {{
    "name": "search_facilities",
    "arguments": {{ "q": "{_h(code.upper())}" }}
  }}
}}</code></pre>
</section>"""

    return _base_html(
        title=title, description=desc, canonical=canonical,
        og_image=f"https://dchub.cloud/static/og/aws-{code}.png",
        schema_jsonld=schema, body_html=body,
        og_type="business.business",
    )


@seo_pages_bp.get("/facility/aws-<code>")
def aws_region_landing(code: str):
    """AWS region/facility code landing (e.g. /facility/aws-iad36 → IAD36 page).

    round-35 (2026-06-05): moved from /aws/<code> to /facility/aws-<code>
    because the CF zone-worker returns HTTP 403 "DNS points to prohibited IP"
    for the /aws/* prefix (same pattern as the /research/* Error 1000 trap).
    The /facility/* prefix is on the CF allow-list. Static-prefix match on
    `aws-` wins over the catch-all /facility/<id_or_slug> in werkzeug routing.
    """
    code = (code or "").strip().lower()
    # tolerate aws-iad-36 typo style
    code = code.replace("-", "").replace("_", "")
    if code not in AWS_REGION_MAP:
        return _error_page(
            f"AWS facility code '{_h(code)}' not in DC Hub's targeted list. "
            "Known codes: " + ", ".join(c.upper() for c in AWS_REGION_MAP.keys()),
            404)
    return Response(
        _aws_landing_html(AWS_REGION_MAP[code], code),
        mimetype="text/html",
        headers={
            "Cache-Control": "public, max-age=3600, s-maxage=86400",
            "X-DC-Page-Source": "seo-aws-landing",
        },
    )


# round-35: 301 redirect from CF-blocked /aws/<code> path so any external
# bookmarks/inbound links still work IF the CF block is ever lifted. While
# CF blocks the prefix, this redirect never reaches a real user — but it's
# zero-cost future-proofing and keeps Google's URL canonicalization clean.
@seo_pages_bp.get("/aws/<code>")
def aws_region_landing_legacy_redirect(code: str):
    code = (code or "").strip().lower().replace("-", "").replace("_", "")
    return redirect(f"/facility/aws-{code}", code=301)


# Address landing — /address/<slug>
def _address_landing_html(meta: dict, slug: str) -> str:
    q = meta["query"]
    title = f"{q} | DC Hub"
    desc = (
        f"{meta['address']}. {meta['summary']} View facility specs, "
        f"operator, power capacity, and Silicon Valley market context on "
        f"DC Hub."
    )
    # round-35: moved off /address/<slug> (CF edge-blocked 403 "DNS points to
    # prohibited IP") onto the un-blocked /facility/<slug> prefix. SEO content
    # body (h1, meta description, first paragraph with verbatim address query)
    # unchanged.
    canonical = f"https://dchub.cloud/facility/{slug}"

    schema = f"""{{
  "@context": "https://schema.org",
  "@type": "Place",
  "name": "{_esc_attr(meta['h1'])}",
  "description": "{_esc_attr(desc[:200])}",
  "url": "{canonical}",
  "address": {{
    "@type": "PostalAddress",
    "streetAddress": "{_esc_attr(meta['address'])}",
    "addressLocality": "{_esc_attr(meta['city'])}",
    "addressRegion": "{_esc_attr(meta['state'])}",
    "addressCountry": "{_esc_attr(meta['country'])}"
  }},
  "geo": {{"@type": "GeoCoordinates", "latitude": "{meta['lat']}", "longitude": "{meta['lon']}"}},
  "additionalType": "https://schema.org/DataCenter"
}}"""

    body = f"""<header class="dc-seo">
  <nav class="breadcrumb">
    <a href="/">DC Hub</a> · <a href="/markets/{_esc_attr(meta['market_slug'])}">{_h(meta['city'])}, {_h(meta['state'])}</a> · {_h(meta['address'])}
  </nav>
  <h1>{_h(meta['h1'])}</h1>
  <p class="lede">Data center at <strong>{_h(meta['address'])}</strong>, operated by {_h(meta['operator'])}.</p>
</header>

<section id="summary">
  <h2>About {_h(meta['address'])}</h2>
  <p>{_h(meta['summary'])}</p>
  <table>
    <tr><th>Street address</th><td>{_h(meta['address'])}</td></tr>
    <tr><th>Operator</th><td>{_h(meta['operator'])}</td></tr>
    <tr><th>City</th><td>{_h(meta['city'])}, {_h(meta['state'])}</td></tr>
    <tr><th>Coordinates</th><td>{meta['lat']}, {meta['lon']}</td></tr>
    <tr><th>Canonical facility page</th><td><a href="/facilities/{_esc_attr(meta['facility_slug'])}.html">View full facility profile →</a></td></tr>
  </table>
</section>

<section id="related">
  <h2>Nearby Comstock Street Data Centers</h2>
  <p>Digital Realty's San Jose campus on Comstock Street includes multiple co-located buildings:</p>
  <ul class="facility-list">
    <li><a href="/facilities/digital-realty-digital-realty-sjc-1201-comstock-st-7a4315af.html">1201 Comstock St</a> — Digital Realty SJC</li>
    <li><a href="/facilities/digital-realty-digital-realty-sjc-1525-comstock-st-f9a76d78.html">1525 Comstock St</a> — Digital Realty SJC</li>
    <li><strong>1725 Comstock St</strong> — Digital Realty SJC (this page)</li>
  </ul>
  <p><a href="/markets/{_esc_attr(meta['market_slug'])}">All San Jose data centers →</a></p>
</section>

<section id="cta">
  <h2>Get the full {_h(meta['address'])} profile</h2>
  <p>Full profile includes power capacity, fiber on-net carriers, water risk, M&amp;A history, and live CAISO grid intelligence.</p>
  <a href="/facilities/{_esc_attr(meta['facility_slug'])}.html" class="cta">View full facility profile</a>
  <a href="/signup?from=address-{_esc_attr(slug)}" class="cta secondary">Free MCP API access</a>
</section>"""

    return _base_html(
        title=title, description=desc, canonical=canonical,
        og_image=f"https://dchub.cloud/static/og/address-{slug}.png",
        schema_jsonld=schema, body_html=body,
        og_type="business.business",
    )


# round-35: per-address static routes for /facility/<address-slug>.
# Registered as STATIC paths (one route per ADDRESS_MAP key) so they win
# specificity over the catch-all /facility/<id_or_slug> in werkzeug routing.
# Moved off /address/<slug> because the CF zone-worker returns 403 "DNS
# points to prohibited IP" for the /address/* prefix.
def _address_landing_response(slug: str) -> Response:
    if slug not in ADDRESS_MAP:
        return _error_page(
            f"Address '{_h(slug)}' not in DC Hub's targeted list.", 404)
    return Response(
        _address_landing_html(ADDRESS_MAP[slug], slug),
        mimetype="text/html",
        headers={
            "Cache-Control": "public, max-age=3600, s-maxage=86400",
            "X-DC-Page-Source": "seo-address-landing",
        },
    )


@seo_pages_bp.get("/facility/1725-comstock-st-san-jose")
def address_landing_1725_comstock():
    """Street-address landing for 1725 Comstock St, San Jose."""
    return _address_landing_response("1725-comstock-st-san-jose")


# round-35: 301 redirect from CF-blocked /address/<slug> path for any
# external bookmarks (no-op while CF blocks the prefix, but future-proof).
@seo_pages_bp.get("/address/<slug>")
def address_landing_legacy_redirect(slug: str):
    slug = (slug or "").strip().lower()
    return redirect(f"/facility/{slug}", code=301)


# Interxion Frankfurt landing — /markets/interxion-frankfurt
# round-35: moved from single-segment /interxion-frankfurt (CF zone-worker
# 403 "DNS points to prohibited IP" — single-segment paths are also
# edge-blocked) to /markets/interxion-frankfurt. Static path wins specificity
# over the catch-all /markets/<slug>. SEO content (title/h1/meta/first
# paragraph with verbatim 'Interxion Frankfurt status' query) unchanged.
@seo_pages_bp.get("/markets/interxion-frankfurt")
def interxion_frankfurt_landing():
    """Targets 'interxion frankfurt status' — operators researching the
    Digital Realty / Interxion Frankfurt campus + Frankfurt grid status."""
    title = "Interxion Frankfurt Status — Digital Realty Campus, Frankfurt Data Center Market | DC Hub"
    desc = (
        "Interxion Frankfurt status: live Digital Realty / Interxion campus "
        "data, Frankfurt data center market intelligence, grid status, and "
        "operator capacity rankings. 7 FRA campuses tracked. DC Hub."
    )
    canonical = "https://dchub.cloud/markets/interxion-frankfurt"
    schema = """{
  "@context": "https://schema.org",
  "@type": "Place",
  "name": "Interxion Frankfurt — Digital Realty Campus",
  "alternateName": "Interxion FRA",
  "description": "Digital Realty / Interxion Frankfurt data center campus, FRA market intelligence.",
  "url": "https://dchub.cloud/markets/interxion-frankfurt",
  "address": {"@type":"PostalAddress","addressLocality":"Frankfurt","addressCountry":"Germany"},
  "additionalType": "https://schema.org/DataCenter"
}"""

    body = """<header class="dc-seo">
  <nav class="breadcrumb">
    <a href="/">DC Hub</a> · Markets · Frankfurt · Interxion
  </nav>
  <h1>Interxion Frankfurt Status &mdash; Digital Realty Campus</h1>
  <p class="lede"><strong>Interxion Frankfurt status</strong>: live Digital Realty / Interxion Frankfurt data center campus intelligence and FRA market overview. Interxion is a Digital Realty company; Frankfurt is the FRA1&ndash;FRA29+ campus cluster, one of Europe's largest interconnect hubs.</p>
  <div class="badges">
    <span>Digital Realty / Interxion</span>
    <span>Frankfurt (FRA)</span>
    <span>Germany</span>
  </div>
</header>

<section id="overview">
  <h2>Interxion Frankfurt Overview</h2>
  <p>Interxion (acquired by Digital Realty in 2020) operates one of the largest data center campuses in Europe at Frankfurt, anchored by FRA1&ndash;FRA17 and the newer FRA28/FRA29 builds. This is a primary European interconnect hub serving DE-CIX, Equinix CH/FR/NL fabrics, and EU-Central hyperscaler regions.</p>
  <table>
    <tr><th>Operator</th><td>Digital Realty (Interxion brand)</td></tr>
    <tr><th>Market</th><td>Frankfurt, Germany (FRA)</td></tr>
    <tr><th>Campus count</th><td>17+ FRA buildings tracked on DC Hub</td></tr>
    <tr><th>Grid / TSO</th><td>50Hertz / Amprion (German national grid)</td></tr>
    <tr><th>Status</th><td>Operational &middot; live grid + market data</td></tr>
  </table>
</section>

<section id="campuses">
  <h2>Interxion / Digital Realty Frankfurt Campuses</h2>
  <ul class="facility-list">
    <li><a href="/facilities/digital-realty-interxion-frankfurt-0e94f4d2.html">Interxion Frankfurt (FRA1+ campus)</a></li>
    <li><a href="/facilities/digital-realty-digital-realty-frankfurt-fra1-16-3ab4cbfd.html">Digital Realty Frankfurt FRA1&ndash;16</a></li>
    <li><a href="/facilities/digital-realty-digital-realty-frankfurt-fra28-94e84073.html">Digital Realty Frankfurt FRA28</a></li>
    <li><a href="/facilities/digital-realty-digital-realty-frankfurt-fra29-32-26e01077.html">Digital Realty Frankfurt FRA29&ndash;32</a></li>
  </ul>
  <p><a href="/markets/frankfurt-de">All Frankfurt data centers (full market roll-up) &rarr;</a></p>
</section>

<section id="grid">
  <h2>Frankfurt Grid Status</h2>
  <p>Live German national grid intelligence (50Hertz / Amprion TSOs): fuel mix, renewable share, capacity scarcity, and pricing trends.</p>
  <p><a href="/grid-intelligence?iso=germany" class="cta secondary">Live Germany grid dashboard &rarr;</a></p>
</section>

<section id="cta">
  <h2>Get Interxion / Frankfurt market intelligence</h2>
  <p>Full reports include campus-by-campus power profiles, fiber carrier maps, hyperscaler capacity, lease comparables, and M&amp;A activity across the entire Frankfurt market.</p>
  <a href="/markets/frankfurt-de" class="cta">View Frankfurt market report</a>
  <a href="/signup?from=interxion-frankfurt" class="cta secondary">Free MCP API access</a>
</section>"""

    return Response(
        _base_html(
            title=title, description=desc, canonical=canonical,
            og_image="https://dchub.cloud/static/og/interxion-frankfurt.png",
            schema_jsonld=schema, body_html=body,
            og_type="business.business",
        ),
        mimetype="text/html",
        headers={
            "Cache-Control": "public, max-age=3600, s-maxage=86400",
            "X-DC-Page-Source": "seo-interxion-frankfurt",
        },
    )


# round-35: 301 redirect from CF-blocked /interxion-frankfurt single-segment
# path. No-op while CF blocks the prefix, future-proof for when it's lifted.
@seo_pages_bp.get("/interxion-frankfurt")
def interxion_frankfurt_legacy_redirect():
    return redirect("/markets/interxion-frankfurt", code=301)


# Moltbook API documentation landing — /partners/moltbook-api
# round-35: moved from single-segment /moltbook-api-documentation (CF
# zone-worker 403 "DNS points to prohibited IP") to /partners/moltbook-api.
# Static path wins specificity over the catch-all /partners/<slug> in
# partner_landing.py. SEO content (title/h1/meta/first paragraph with
# verbatim 'Moltbook API documentation' query) unchanged.
@seo_pages_bp.get("/partners/moltbook-api")
def moltbook_api_landing():
    """Targets 'moltbook api documentation' — moltbook.com is a real
    Reddit-like agent platform DC Hub integrates with (see
    moltbook_integration.py). High-intent: developers/agent operators
    looking for Moltbook API docs and reference integrations."""
    title = "Moltbook API Documentation — DC Hub Integration Reference"
    desc = (
        "Moltbook API documentation and integration reference. DC Hub's "
        "DCHubBot is a verified Moltbook agent — full request/response "
        "examples, auth headers, posting + commenting endpoints, and "
        "rate-limit handling for moltbook.com/api/v1."
    )
    canonical = "https://dchub.cloud/partners/moltbook-api"
    schema = """{
  "@context": "https://schema.org",
  "@type": "TechArticle",
  "headline": "Moltbook API Documentation — DC Hub Integration Reference",
  "description": "Moltbook API integration reference using DC Hub's DCHubBot as a worked example.",
  "url": "https://dchub.cloud/partners/moltbook-api",
  "about": {"@type":"SoftwareApplication","name":"Moltbook API","applicationCategory":"SocialNetworking"}
}"""

    body = """<header class="dc-seo">
  <nav class="breadcrumb">
    <a href="/">DC Hub</a> &middot; Integrations &middot; Moltbook API Documentation
  </nav>
  <h1>Moltbook API Documentation</h1>
  <p class="lede"><strong>Moltbook API documentation</strong> &mdash; reference integration using DC Hub's <code>DCHubBot</code>, a verified Moltbook agent posting data center market intelligence. This page covers the moltbook.com/api/v1 endpoints, auth, and rate-limit patterns we use in production.</p>
  <div class="badges">
    <span>moltbook.com/api/v1</span>
    <span>Bearer auth</span>
    <span>Reference: DCHubBot</span>
  </div>
</header>

<section id="base">
  <h2>Base URL &amp; Auth</h2>
  <table>
    <tr><th>Base URL</th><td><code>https://www.moltbook.com/api/v1</code> (must use <code>www</code>)</td></tr>
    <tr><th>Auth header</th><td><code>Authorization: Bearer &lt;MOLTBOOK_API_KEY&gt;</code></td></tr>
    <tr><th>Content-Type</th><td><code>application/json</code></td></tr>
    <tr><th>Timeout</th><td>15s recommended</td></tr>
  </table>
</section>

<section id="register">
  <h2>1. Agent Registration</h2>
  <pre style="background:#f6f7f9;padding:14px;border-radius:6px;overflow-x:auto;"><code>POST https://www.moltbook.com/api/v1/agents/register
Content-Type: application/json

{
  "name": "DCHubBot",
  "description": "Data center intelligence agent..."
}

# Returns: { agent: { api_key, claim_url, verification_code } }</code></pre>
</section>

<section id="endpoints">
  <h2>2. Core Endpoints</h2>
  <table>
    <tr><th><code>GET /agents/status</code></th><td>Check claim status</td></tr>
    <tr><th><code>GET /agents/me</code></th><td>Get bot profile</td></tr>
    <tr><th><code>GET /submolts</code></th><td>List all submolts (Reddit-style communities)</td></tr>
    <tr><th><code>POST /submolts</code></th><td>Create submolt</td></tr>
    <tr><th><code>POST /submolts/&lt;name&gt;/subscribe</code></th><td>Subscribe</td></tr>
    <tr><th><code>POST /posts</code></th><td>Create post (rate-limited)</td></tr>
    <tr><th><code>POST /comments</code></th><td>Create comment (daily quota)</td></tr>
  </table>
</section>

<section id="ratelimits">
  <h2>3. Rate Limits (observed)</h2>
  <ul class="facility-list">
    <li><strong>Posts:</strong> hourly cool-off &mdash; throttle to 1/hr per submolt</li>
    <li><strong>Comments:</strong> daily quota &mdash; track <code>_daily_comment_count</code> client-side</li>
    <li><strong>HTTP 429:</strong> back off exponentially, max 1 hour</li>
  </ul>
</section>

<section id="reference">
  <h2>4. Reference Implementation</h2>
  <p>DC Hub's full Python integration (registration, claiming, submolt management, posting, comment quotas, exponential backoff) is open-source in our backend:</p>
  <ul class="facility-list">
    <li><code>moltbook_integration.py</code> &mdash; full reference client</li>
    <li><code>/moltbook/dashboard</code> &mdash; admin UI for credential management</li>
  </ul>
</section>

<section id="cta">
  <h2>About DC Hub</h2>
  <p>DC Hub is a data center intelligence platform tracking 21,000+ facilities, 7 ISO grids, and 2,000+ M&amp;A deals. Our DCHubBot publishes daily market signals to Moltbook. Free MCP API for AI agents.</p>
  <a href="/signup?from=moltbook-docs" class="cta">Get free DC Hub MCP API key</a>
  <a href="https://www.moltbook.com" class="cta secondary" rel="nofollow noopener" target="_blank">Visit Moltbook &rarr;</a>
</section>"""

    return Response(
        _base_html(
            title=title, description=desc, canonical=canonical,
            og_image="https://dchub.cloud/static/og/moltbook-api.png",
            schema_jsonld=schema, body_html=body,
            og_type="article",
        ),
        mimetype="text/html",
        headers={
            "Cache-Control": "public, max-age=3600, s-maxage=86400",
            "X-DC-Page-Source": "seo-moltbook-docs",
        },
    )


# round-35: 301 redirect from CF-blocked /moltbook-api-documentation
# single-segment path. No-op while CF blocks the prefix, future-proof.
@seo_pages_bp.get("/moltbook-api-documentation")
def moltbook_api_landing_legacy_redirect():
    return redirect("/partners/moltbook-api", code=301)


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
        # round-35 (2026-06-05): the 4 high-intent landing prefixes (/aws/*,
        # /address/*, single-segment /interxion-frankfurt + /moltbook-api-
        # documentation) were CF zone-worker 403'd "DNS points to prohibited
        # IP". Re-routed under un-blocked /facility/aws-*, /facility/<addr>,
        # /markets/interxion-frankfurt, /partners/moltbook-api. Legacy paths
        # kept as 301 redirects (no-op while CF blocks, future-proof).
        version="round-35-seo-pages-cf-edge-safe-prefixes",
        routes=["/facility/<id>", "/markets/<slug>", "/grids/<code>",
                "/facility/aws-<code>",
                "/facility/1725-comstock-st-san-jose",
                "/markets/interxion-frankfurt",
                "/partners/moltbook-api",
                "/sitemap-index.xml", "/sitemap-facilities.xml",
                "/sitemap-markets.xml", "/sitemap-grids.xml",
                "/sitemap-landings.xml"],
        legacy_redirects=["/aws/<code> -> /facility/aws-<code>",
                          "/address/<slug> -> /facility/<slug>",
                          "/interxion-frankfurt -> /markets/interxion-frankfurt",
                          "/moltbook-api-documentation -> /partners/moltbook-api"],
        db_ok=db_ok,
        iso_count=len(ISO_REGISTRY),
        aws_codes=list(AWS_REGION_MAP.keys()),
        addresses=list(ADDRESS_MAP.keys()),
    )
