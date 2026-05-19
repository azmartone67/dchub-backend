"""
Facility profile page — dynamic HTML renderer (2026-05-19).

Closes the gap user spotted: there are 2,002 static HTML files in
dchub-frontend/facilities/ but ~21,000 facilities in the DB. >90% of
facility profiles 404. Adding/discovering new facilities silently
broke their profiles.

This route renders any facility on demand:
  GET /facilities/<slug>      — HTML profile page
  GET /facilities/<slug>.html — same (handles .html-suffix from old links)

The renderer pulls facility data via the same query the
/api/v1/facilities/<slug> endpoint uses (slug = name-with-dashes +
8-char MD5 hash of id), then emits HTML that matches the existing
static file style.

CF Pages serves static files first via _routes.json. If a static file
exists for a facility, it wins. This route only fires when the static
file doesn't exist (CF Pages 404 falls through to the worker, which
forwards to backend via PHASE_282_RAILWAY_PATHS prefix match).
"""

import os
import logging
from flask import Blueprint, request, Response, jsonify
import datetime as _dt

logger = logging.getLogger(__name__)
facility_profile_bp = Blueprint("facility_profile", __name__)


def _fetch_facility_by_slug(slug: str) -> dict | None:
    """Same slug-hash lookup as /api/v1/facilities/<slug>."""
    parts = slug.rsplit("-", 1)
    if len(parts) != 2 or len(parts[1]) != 8:
        return None
    hash8 = parts[1]
    try:
        from main import get_read_db
        conn = get_read_db()
        if not conn: return None
        try:
            c = conn.cursor()
            c.execute("""
                SELECT id, name, provider, city, state, country,
                       market AS region, latitude, longitude,
                       power_mw, status, address
                FROM discovered_facilities
                WHERE LEFT(MD5(id::text), 8) = %s
                LIMIT 1
            """, (hash8,))
            row = c.fetchone()
            if not row: return None
            cols = [desc[0] for desc in c.description]
            return dict(zip(cols, row))
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"facility_profile fetch failed: {e}")
        return None


def _esc(s) -> str:
    """HTML-escape."""
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _render_profile(fac: dict, slug: str) -> str:
    """Server-rendered facility profile. Matches the static file
    visual style so transitions between static + dynamic are seamless."""
    name = fac.get("name") or "Data Center"
    provider = fac.get("provider") or "Operator"
    city = fac.get("city") or ""
    state = fac.get("state") or ""
    country = fac.get("country") or ""
    region = fac.get("region") or ""
    power = fac.get("power_mw")
    status = fac.get("status") or "Unknown"
    address = fac.get("address") or ""
    lat = fac.get("latitude")
    lng = fac.get("longitude")

    loc_short = ", ".join([p for p in (city, state, country) if p])
    title = f"{name} | DC Hub"
    desc = (f"{name} data center by {provider} in {loc_short}. "
            f"{f'Power capacity: {power} MW. ' if power else ''}"
            f"View specs, location, and connectivity on DC Hub.")

    canonical = f"https://dchub.cloud/facilities/{slug}"

    # Schema.org JSON-LD
    import json as _json
    schema = {
        "@context": "https://schema.org",
        "@type": "Place",
        "name": name,
        "description": f"Data center facility operated by {provider} in {loc_short}",
        "address": {
            "@type": "PostalAddress",
            "streetAddress": address or None,
            "addressLocality": city or None,
            "addressRegion": state or None,
            "addressCountry": country or None,
        },
    }
    if lat and lng:
        schema["geo"] = {
            "@type": "GeoCoordinates",
            "latitude": float(lat),
            "longitude": float(lng),
        }

    # Compact stats grid
    stats = []
    if power:    stats.append(("Power", f"{power} MW"))
    if status:   stats.append(("Status", status))
    if region:   stats.append(("Region", region))
    if lat and lng:
        stats.append(("Coordinates", f"{lat:.4f}, {lng:.4f}"))

    stats_html = "".join(
        f'<div class="stat-card"><div class="stat-label">{_esc(label)}</div>'
        f'<div class="stat-value">{_esc(value)}</div></div>'
        for label, value in stats
    )

    map_block = ""
    if lat and lng:
        # Cheap inline map preview via OpenStreetMap static tile
        bbox = f"{float(lng)-0.05},{float(lat)-0.04},{float(lng)+0.05},{float(lat)+0.04}"
        map_block = f"""
        <div class="map-block">
          <iframe width="100%" height="320" frameborder="0" scrolling="no"
            marginheight="0" marginwidth="0"
            src="https://www.openstreetmap.org/export/embed.html?bbox={bbox}&layer=mapnik&marker={lat},{lng}"
            style="border:1px solid #2a2a2e;border-radius:12px"
            loading="lazy"></iframe>
          <p style="margin-top:8px;color:#888;font-size:13px">
            <a href="https://www.openstreetmap.org/?mlat={lat}&amp;mlon={lng}#map=14/{lat}/{lng}"
               target="_blank" style="color:#6366f1">Open in OpenStreetMap →</a>
          </p>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_esc(title)}</title>
<meta name="description" content="{_esc(desc)}">
<meta name="robots" content="index, follow">
<link rel="canonical" href="{_esc(canonical)}">
<meta property="og:title" content="{_esc(name)} - Data Center">
<meta property="og:description" content="{_esc(desc)}">
<meta property="og:type" content="place">
<meta property="og:url" content="{_esc(canonical)}">
<meta property="og:site_name" content="DC Hub">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{_esc(name)}">
<meta name="twitter:description" content="{_esc(desc[:200])}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<script type="application/ld+json">{_json.dumps(schema, indent=2)}</script>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:'Instrument Sans',-apple-system,sans-serif;background:#09090b;color:#fafafa;line-height:1.6}}
  .header{{background:linear-gradient(135deg,#141417 0%,#09090b 100%);padding:20px;border-bottom:1px solid rgba(255,255,255,0.04)}}
  .header-inner{{max-width:1200px;margin:0 auto;display:flex;justify-content:space-between;align-items:center}}
  .logo{{font-size:24px;font-weight:700;color:#fbbf24;text-decoration:none}}
  .nav a{{color:#888;text-decoration:none;margin-left:24px}}
  .nav a:hover{{color:#fff}}
  .breadcrumb{{max-width:1200px;margin:20px auto;padding:0 20px;font-size:14px;color:#666}}
  .breadcrumb a{{color:#6366f1;text-decoration:none}}
  .container{{max-width:1200px;margin:0 auto;padding:20px}}
  .facility-header{{background:linear-gradient(135deg,#141417 0%,#0c0c0f 100%);border-radius:16px;padding:32px;margin-bottom:24px;border:1px solid rgba(255,255,255,0.04)}}
  .facility-title{{font-size:32px;font-weight:700;margin-bottom:8px}}
  .facility-provider{{color:#fbbf24;font-size:18px;margin-bottom:16px}}
  .facility-location{{color:#888;font-size:16px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
  .stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:24px}}
  .stat-card{{background:#141417;border-radius:12px;padding:20px;border:1px solid rgba(255,255,255,0.04)}}
  .stat-label{{color:#888;font-size:13px;text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px}}
  .stat-value{{font-size:24px;font-weight:600;color:#fafafa}}
  .map-block{{background:#141417;border-radius:12px;padding:16px;margin-bottom:24px;border:1px solid rgba(255,255,255,0.04)}}
  .data-source{{color:#666;font-size:13px;text-align:center;padding:24px 0;border-top:1px solid rgba(255,255,255,0.04);margin-top:32px}}
  .cta-bar{{background:linear-gradient(135deg,rgba(99,102,241,0.10) 0%,rgba(155,114,203,0.04) 100%);border:1px solid rgba(99,102,241,0.2);border-radius:12px;padding:20px;margin-bottom:24px;text-align:center}}
  .cta-bar a{{color:#a5b4fc;text-decoration:none;font-weight:600}}
  .nav-hidden{{display:none}}
</style>
</head>
<body>
  <header class="header">
    <div class="header-inner">
      <a href="/" class="logo">DC Hub</a>
      <nav class="nav">
        <a href="/land-power-map">Map</a>
        <a href="/markets">Markets</a>
        <a href="/intelligence">Intelligence</a>
        <a href="/api-docs">API</a>
      </nav>
    </div>
  </header>

  <div class="breadcrumb">
    <a href="/">Home</a> · <a href="/land-power-map">Map</a> · {_esc(name)}
  </div>

  <div class="container">
    <div class="facility-header">
      <h1 class="facility-title">{_esc(name)}</h1>
      <div class="facility-provider">{_esc(provider)}</div>
      <div class="facility-location">
        <span>📍 {_esc(loc_short)}</span>
        {f'<span style="color:#666">·</span><span>{_esc(address)}</span>' if address else ''}
      </div>
    </div>

    <div class="stats-grid">
      {stats_html}
    </div>

    {map_block}

    <div class="cta-bar">
      Need API access to this data? <a href="/ai">Get a free MCP key →</a> ·
      <a href="/cited-by">Cited by ChatGPT, Gemini + 13 AI platforms</a>
    </div>

    <div class="data-source">
      Data source: DC Hub global infrastructure database ·
      <a href="/api/v1/facilities/{_esc(slug)}" style="color:#6366f1">View raw JSON</a>
    </div>
  </div>

  <script src="/js/dchub-nav.js" defer></script>
</body>
</html>"""


@facility_profile_bp.route("/facilities/<path:slug>", methods=["GET"])
def render_facility_profile(slug):
    """Dynamic facility profile page. Falls back here when the static
    HTML file doesn't exist in CF Pages. Handles both .html-suffixed
    and bare slugs.
    """
    # Strip .html suffix (some old links include it)
    if slug.endswith(".html"):
        slug = slug[:-5]
    # Handle nested paths just in case
    slug = slug.split("/")[0]

    fac = _fetch_facility_by_slug(slug)
    if not fac:
        return Response(
            f"""<!DOCTYPE html><html><head>
<meta charset="utf-8"><title>Facility not found | DC Hub</title>
<meta name="robots" content="noindex"></head>
<body style="font-family:system-ui;background:#09090b;color:#fafafa;
text-align:center;padding:80px 20px">
<h1>Facility not found</h1>
<p style="color:#888">No facility matches slug <code>{_esc(slug)}</code>.</p>
<p><a href="/land-power-map" style="color:#6366f1">Browse the map</a> ·
<a href="/" style="color:#6366f1">Home</a></p>
</body></html>""",
            status=404, mimetype="text/html"
        )

    html = _render_profile(fac, slug)
    return Response(html, status=200, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=300",
                             "X-DC-Hub-Source": "facility-profile-dynamic"})
