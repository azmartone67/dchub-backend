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


def _market_dcpi(city: str, state: str) -> dict | None:
    """Best-effort DCPI verdict for the facility's market (by city/state) so
    the profile shows real intelligence, not just sparse metadata."""
    cands = []
    if city:
        cands.append(city.lower().replace(" ", "-"))
        cands.append(city.lower().split(",")[0].strip().replace(" ", "-"))
    if state:
        cands.append(state.lower())
    cands = [c for c in cands if c]
    if not cands:
        return None
    try:
        from main import get_read_db
        conn = get_read_db()
        if not conn:
            return None
        try:
            c = conn.cursor()
            c.execute("""
                SELECT market_slug, market_name, iso, verdict,
                       excess_power_score, constraint_score, time_to_power_months
                  FROM market_power_scores
                 WHERE LOWER(market_slug) = ANY(%s) OR LOWER(state) = ANY(%s)
                 ORDER BY computed_at DESC LIMIT 1
            """, (cands, cands))
            row = c.fetchone()
            if not row:
                return None
            return dict(zip([d[0] for d in c.description], row))
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"facility_profile dcpi failed: {e}")
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

    # Enriched stat cards — only render values we actually have (sparse rows
    # with power_mw=0 / blank fields used to render a wall of empties).
    def _has(v):
        return v not in (None, "", 0, 0.0, "0", "Unknown", "unknown")
    stats = []
    if _has(power):                    stats.append(("Power", f"{power} MW"))
    if _has(status):                   stats.append(("Status", str(status).title()))
    if _has(region):                   stats.append(("Market", region))
    if _has(city) and city != region:  stats.append(("City", city))
    if _has(state):                    stats.append(("State", state))
    if _has(country):                  stats.append(("Country", country))
    if lat and lng:                    stats.append(("Coordinates", f"{float(lat):.4f}, {float(lng):.4f}"))
    if _has(address):                  stats.append(("Address", address))
    stats_html = "".join(
        f'<div class="stat-card"><div class="stat-label">{_esc(label)}</div>'
        f'<div class="stat-value">{_esc(value)}</div></div>'
        for label, value in stats
    )

    # DCPI market-intelligence block (best-effort — this is an intelligence
    # platform, so a facility page should carry its market's DCPI verdict).
    _dcpi = _market_dcpi(city, state)
    dcpi_html = ""
    if _dcpi:
        _verdict = (_dcpi.get("verdict") or "").upper()
        _vcolor = "#10b981" if _verdict == "BUILD" else ("#ef4444" if _verdict == "AVOID" else "#f59e0b")
        _mslug = _dcpi.get("market_slug") or ""
        _mname = _dcpi.get("market_name") or region or "this market"
        _chips = []
        if _dcpi.get("iso"):                              _chips.append(("ISO", _esc(_dcpi.get("iso"))))
        if _dcpi.get("excess_power_score") is not None:   _chips.append(("Excess-power", _esc(_dcpi.get("excess_power_score"))))
        if _dcpi.get("constraint_score") is not None:     _chips.append(("Constraint", _esc(_dcpi.get("constraint_score"))))
        if _dcpi.get("time_to_power_months") is not None: _chips.append(("Time-to-power", f'{_esc(_dcpi.get("time_to_power_months"))} mo'))
        _chips_html = "".join(
            f'<div class="chip"><span class="chip-l">{l}</span><span class="chip-v">{v}</span></div>'
            for l, v in _chips)
        _dlink = f'<a href="/dcpi/{_esc(_mslug)}" class="link">Full DCPI breakdown &rarr;</a>' if _mslug else ""
        dcpi_html = (
            '<div class="section"><div class="section-head">'
            '<h2>Market intelligence</h2>'
            f'<span class="verdict" style="color:{_vcolor};border-color:{_vcolor}">{_esc(_verdict or "NEUTRAL")}</span>'
            '</div>'
            f'<p class="section-sub">Data Center Power Index verdict for {_esc(_mname)} &mdash; the market this facility sits in.</p>'
            f'<div class="chips">{_chips_html}</div>{_dlink}</div>'
        )

    map_block = ""
    if lat and lng:
        # Cheap inline map preview via OpenStreetMap static tile
        bbox = f"{float(lng)-0.05},{float(lat)-0.04},{float(lng)+0.05},{float(lat)+0.04}"
        map_block = f"""
        <div class="section">
          <div class="section-head"><h2>Location</h2></div>
          <iframe width="100%" height="320" frameborder="0" scrolling="no" loading="lazy"
            marginheight="0" marginwidth="0"
            src="https://www.openstreetmap.org/export/embed.html?bbox={bbox}&layer=mapnik&marker={lat},{lng}"
            style="border:1px solid var(--b);border-radius:12px;display:block;margin-top:8px"></iframe>
          <p style="margin-top:10px">
            <a href="https://www.openstreetmap.org/?mlat={lat}&amp;mlon={lng}#map=14/{lat}/{lng}"
               target="_blank" class="link" style="margin-top:0">Open in OpenStreetMap &rarr;</a>
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
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script type="application/ld+json">{_json.dumps(schema, indent=2)}</script>
<style>
  :root{{--bg:#0a0a0f;--surf:#131319;--surf2:#1a1a22;--b:rgba(255,255,255,0.08);--tx:#fafafa;--mut:#a1a1aa;--dim:#71717a;--ind:#818cf8;--indd:#6366f1;--vio:#a855f7;--grad:linear-gradient(135deg,#6366f1,#a855f7)}}
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,sans-serif;background:var(--bg);color:var(--tx);line-height:1.6;-webkit-font-smoothing:antialiased}}
  .header{{border-bottom:1px solid var(--b);padding:16px 0;position:sticky;top:0;background:rgba(10,10,15,0.85);backdrop-filter:blur(10px);z-index:10}}
  .header-inner,.container,.breadcrumb{{max-width:1080px;margin:0 auto;padding:0 24px}}
  .header-inner{{display:flex;justify-content:space-between;align-items:center}}
  .logo{{font-size:21px;font-weight:700;color:var(--tx);text-decoration:none;letter-spacing:-.02em}}
  .logo span{{background:var(--grad);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}}
  .nav a{{color:var(--mut);text-decoration:none;margin-left:22px;font-size:14px;font-weight:500}}
  .nav a:hover{{color:var(--tx)}}
  .breadcrumb{{margin:22px auto 0;font-size:12px;color:var(--dim);font-family:'JetBrains Mono',monospace}}
  .breadcrumb a{{color:var(--ind);text-decoration:none}}
  .container{{padding-top:4px;padding-bottom:64px}}
  .hero{{padding:34px 0 6px}}
  .hero h1{{font-size:34px;font-weight:700;letter-spacing:-.02em;margin-bottom:6px}}
  .hero .prov{{color:var(--ind);font-size:16px;font-weight:500;margin-bottom:12px}}
  .hero .loc{{color:var(--mut);font-size:15px}}
  .stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin:24px 0}}
  .stat-card{{background:var(--surf);border:1px solid var(--b);border-radius:14px;padding:18px 20px}}
  .stat-label{{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px;font-family:'JetBrains Mono',monospace}}
  .stat-value{{font-size:19px;font-weight:600;font-family:'JetBrains Mono',monospace;word-break:break-word}}
  .section{{background:var(--surf);border:1px solid var(--b);border-radius:16px;padding:24px;margin:18px 0}}
  .section-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:6px}}
  .section-head h2{{font-size:18px;font-weight:600}}
  .section-sub{{color:var(--mut);font-size:14px;margin-bottom:16px}}
  .verdict{{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;letter-spacing:.06em;padding:5px 12px;border:1px solid;border-radius:999px;white-space:nowrap}}
  .chips{{display:flex;flex-wrap:wrap;gap:10px}}
  .chip{{background:var(--surf2);border:1px solid var(--b);border-radius:10px;padding:10px 14px;min-width:118px}}
  .chip-l{{display:block;color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em;font-family:'JetBrains Mono',monospace}}
  .chip-v{{display:block;font-size:18px;font-weight:600;font-family:'JetBrains Mono',monospace;margin-top:4px}}
  .link{{display:inline-block;margin-top:16px;color:var(--ind);text-decoration:none;font-weight:600;font-size:14px}}
  .map-block{{padding:0;overflow:hidden}}
  .cta{{background:linear-gradient(135deg,rgba(99,102,241,0.12),rgba(168,85,247,0.06));border:1px solid rgba(99,102,241,0.25);border-radius:16px;padding:22px 24px;margin:18px 0;display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;justify-content:center;text-align:center}}
  .cta a{{color:var(--ind);text-decoration:none;font-weight:600;font-size:14px}}
  .cta .primary{{background:var(--grad);color:#fff;padding:10px 18px;border-radius:9px}}
  .foot{{color:var(--dim);font-size:13px;text-align:center;padding-top:24px;border-top:1px solid var(--b);margin-top:30px}}
  .foot a{{color:var(--ind);text-decoration:none}}
</style>
</head>
<body>
  <header class="header">
    <div class="header-inner">
      <a href="/" class="logo">DC<span>Hub</span></a>
      <nav class="nav">
        <a href="/land-power-map">Map</a>
        <a href="/markets">Markets</a>
        <a href="/dcpi">DCPI</a>
        <a href="/api-docs">API</a>
      </nav>
    </div>
  </header>

  <div class="breadcrumb">
    <a href="/">Home</a> · <a href="/land-power-map">Map</a> · {_esc(name)}
  </div>

  <div class="container">
    <div class="hero">
      <h1>{_esc(name)}</h1>
      {f'<div class="prov">{_esc(provider)}</div>' if (provider and provider.strip().lower() != name.strip().lower()) else ''}
      <div class="loc">📍 {_esc(loc_short)}</div>
    </div>

    <div class="stats-grid">{stats_html}</div>

    {dcpi_html}

    {map_block}

    <div class="cta">
      <a class="primary" href="/sites/{_esc(slug)}">View full capacity report &rarr;</a>
      <a href="/ai">Get a free MCP key</a>
      <a href="/cited-by">Cited by ChatGPT, Gemini + 13 AI platforms</a>
    </div>

    <div class="foot">
      Data: DC Hub global infrastructure database ·
      <a href="/api/v1/facilities/{_esc(slug)}">Raw JSON</a>
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
