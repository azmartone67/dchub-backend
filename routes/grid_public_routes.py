"""Phase 23 — public grid intelligence routes.

These render server-side HTML for SEO. Each page has Schema.org JSON-LD,
OG meta tags, and pulls live data from /api/v1/grid/intelligence/<iso>.
 [phase68_gating_applied]"""
from flask import Blueprint, render_template, jsonify, request, Response
import json, datetime, requests

grid_public_bp = Blueprint('grid_public', __name__)

ISOS = {
    'PJM':    {'name': 'PJM Interconnection',   'states': '13 states + DC',         'tagline': 'Largest US grid operator'},
    'MISO':   {'name': 'Midcontinent ISO',      'states': '15 states + Manitoba',   'tagline': 'Industrial heartland grid'},
    'ERCOT':  {'name': 'Electric Reliability Council of Texas', 'states': 'Texas',  'tagline': 'Texas independent grid'},
    'CAISO':  {'name': 'California ISO',        'states': 'California',             'tagline': 'Renewable-heavy western grid'},
    'NYISO':  {'name': 'New York ISO',          'states': 'New York',               'tagline': 'Dense urban grid operator'},
    'ISONE':  {'name': 'ISO New England',       'states': '6 NE states',            'tagline': 'New England grid operator'},
    'SPP':    {'name': 'Southwest Power Pool',  'states': '14 states',              'tagline': 'Wind-rich plains grid'},
}

# Free tier sees only these — paid tiers unlock all 7
FREE_TIER_ISOS = {'PJM', 'ERCOT'}


def _user_tier(req):
    """Best-effort tier detection. Default 'free'."""
    # Cookie / header / session-based — adapt to existing auth
    tier = req.cookies.get('dch_tier') or req.headers.get('X-Tier') or 'free'
    return tier.lower()


# Phase JJ (2026-05-14): in-process TTL cache for _fetch_live.
# CAISO's upstream API responds in ~10s right at the CF Worker edge
# timeout. Without caching, every page load eats that latency; with
# 5min TTL we serve cached on subsequent requests within the window.
# Simple dict, no Redis required — gunicorn workers each maintain
# their own cache, which is acceptable for "show some data fast"
# semantics. Real time-critical paths still hit the API directly.
_LIVE_CACHE: dict = {}            # iso → (timestamp_epoch, response_dict)
_LIVE_TTL_SECONDS = 5 * 60        # 5 minutes

def _fetch_live(iso):
    """Internal call to /api/v1/grid/intelligence/<iso>.

    Phase II (2026-05-14): three bugs were burning every /grid/<ISO>
    page to '0 MW' despite the underlying EIA API returning fine —
    see comments below. Phase JJ added the in-process TTL cache.
    """
    import os as _os, time as _time

    # Cache check FIRST — if we have a fresh response, return it
    now = _time.time()
    cached = _LIVE_CACHE.get(iso)
    if cached and (now - cached[0]) < _LIVE_TTL_SECONDS:
        return cached[1]

    port = _os.environ.get('PORT', '8080')
    urls = [
        f'http://127.0.0.1:{port}/api/v1/grid/intelligence/{iso}',
        f'https://dchub-backend-production.up.railway.app/api/v1/grid/intelligence/{iso}',
    ]
    for u in urls:
        try:
            r = requests.get(u, timeout=8)
            if r.ok:
                payload = r.json()
                # Prefer 'data' key if present; fall back to payload itself
                inner = payload.get('data', None) if isinstance(payload, dict) else None
                if inner is None or not isinstance(inner, dict) or not inner:
                    inner = payload if isinstance(payload, dict) else {}
                # Sanity check: must have at least one real metric
                if inner.get('demand_mw') or inner.get('current_demand_mw') or inner.get('demand_24h'):
                    # Store in cache before returning
                    _LIVE_CACHE[iso] = (now, inner)
                    # Cap cache size — drop oldest if >100 entries
                    if len(_LIVE_CACHE) > 100:
                        oldest_key = min(_LIVE_CACHE, key=lambda k: _LIVE_CACHE[k][0])
                        _LIVE_CACHE.pop(oldest_key, None)
                    return inner
        except Exception:
            continue
    # On total miss, return stale cache if present (better than 0 MW)
    if cached:
        return cached[1]
    return {}


def _to_int(v):
    """Defensive int coercion — EIA API returns demand_mw as STRING
    ('88907') in some response shapes. Without this, format-strings
    like {demand:,} would emit '88,907' for ints but crash on strings,
    and arithmetic comparisons would fail silently."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


@grid_public_bp.route('/grid', methods=['GET'])
def grid_hub():
    """Public hub page showing all 7 ISOs at a glance."""
    tier = _user_tier(request)
    cards = []
    for iso, meta in ISOS.items():
        live = _fetch_live(iso)
        gated = (tier == 'free' and iso not in FREE_TIER_ISOS)
        cards.append({
            'iso': iso,
            'name': meta['name'],
            'states': meta['states'],
            'tagline': meta['tagline'],
            # Phase II: same field-read fix as render_grid_iso_html.
            # API field is `demand_mw` (not current_demand_mw).
            'demand_mw': _to_int(live.get('demand_mw') or live.get('current_demand_mw') or 0) if not gated else None,
            'headroom_pct': live.get('headroom_pct') if not gated else None,
            'gen_mix': live.get('generation_mix', {}) if not gated else {},
            'gated': gated,
        })

    schema = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": "US Grid Intelligence — Live ISO Demand & Headroom",
        "description": "Real-time demand, generation mix, and headroom across all 7 US ISOs (PJM, MISO, ERCOT, CAISO, NYISO, ISO-NE, SPP).",
        "url": "https://dchub.cloud/grid",
        "publisher": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
    }

    html = render_grid_hub_html(cards, schema, tier)
    return Response(html, mimetype='text/html')


@grid_public_bp.route('/grid/<iso>', methods=['GET'])
@grid_public_bp.route('/grid/<iso>/', methods=['GET'])
def grid_iso(iso):
    """Per-ISO deep page."""
    iso = iso.upper()
    if iso not in ISOS:
        return Response('<h1>Unknown ISO</h1>', status=404, mimetype='text/html')
    tier = _user_tier(request)
    if tier == 'free' and iso not in FREE_TIER_ISOS:
        return Response(render_paywall_html(iso, ISOS[iso]), mimetype='text/html')

    meta = ISOS[iso]
    live = _fetch_live(iso)

    schema = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": f"{iso} Real-Time Grid Intelligence",
        "description": f"Live demand, generation mix, and headroom for {meta['name']} ({meta['states']}).",
        "url": f"https://dchub.cloud/grid/{iso}",
        "creator": {"@type": "Organization", "name": "DC Hub"},
        "isAccessibleForFree": iso in FREE_TIER_ISOS,
        "temporalCoverage": str(datetime.datetime.utcnow().isoformat()) + 'Z',
    }

    html = render_grid_iso_html(iso, meta, live, schema)
    return Response(html, mimetype='text/html')


# AUTO-REPAIR: duplicate route '/sitemap.xml' also in main.py:15927 — review and remove one
@grid_public_bp.route('/sitemap.xml', methods=['GET'])
def sitemap():
    """Sitemap including grid intel URLs."""
    today = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    base = 'https://dchub.cloud'
    urls = [
        ('/grid', '0.9', 'hourly'),
    ]
    for iso in ISOS:
        urls.append((f'/grid/{iso}', '0.8', 'hourly'))
    body = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, prio, freq in urls:
        body.append(f'  <url><loc>{base}{path}</loc><lastmod>{today}</lastmod>'
                    f'<changefreq>{freq}</changefreq><priority>{prio}</priority></url>')
    body.append('</urlset>')
    return Response('\n'.join(body), mimetype='application/xml')


def render_grid_hub_html(cards, schema, tier):
    """Server-side HTML for /grid hub — SEO-indexable."""
    cards_html = []
    for c in cards:
        if c['gated']:
            cards_html.append(f'''
            <div class="grid-card gated">
              <div class="iso-badge">{c['iso']}</div>
              <h3>{c['name']}</h3>
              <div class="states">{c['states']}</div>
              <div class="tagline">{c['tagline']}</div>
              <div class="paywall">
                <div class="lock">🔒</div>
                <div>Available on <a href="/pricing">Pro tier</a></div>
              </div>
            </div>''')
        else:
            demand = c.get('demand_mw') or 0
            headroom = c.get('headroom_pct') or 0
            top_fuel = ''
            if c.get('gen_mix'):
                gm = c['gen_mix']
                if isinstance(gm, dict) and gm:
                    top_fuel = max(gm.items(), key=lambda kv: kv[1] or 0)[0]
            cards_html.append(f'''
            <a class="grid-card" href="/grid/{c['iso'].lower()}">  <!-- phase26_lowercase_links -->
              <div class="iso-badge">{c['iso']}</div>
              <h3>{c['name']}</h3>
              <div class="states">{c['states']}</div>
              <div class="metrics">
                <div class="metric"><div class="num">{demand:,}</div><div class="lbl">MW now</div></div>
                <div class="metric"><div class="num">{headroom:.0f}%</div><div class="lbl">headroom</div></div>
              </div>
              <div class="top-fuel">Lead fuel: {top_fuel or '—'}</div>
              <div class="cta">View live →</div>
            </a>''')

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>US Grid Intelligence — Live ISO Demand & Headroom | DC Hub</title>
  <meta name="description" content="Real-time demand, generation mix, and headroom across all 7 US ISOs (PJM, MISO, ERCOT, CAISO, NYISO, ISO-NE, SPP). Updated every 5 minutes from EIA.">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta property="og:title" content="US Grid Intelligence | DC Hub">
  <meta property="og:description" content="Live demand and headroom across all 7 US ISOs.">
  <meta property="og:image" content="https://dchub.cloud/api/v1/social/grid-card.png">
  <meta property="og:url" content="https://dchub.cloud/grid">
  <meta name="twitter:card" content="summary_large_image">
  <link rel="canonical" href="https://dchub.cloud/grid">
  <script type="application/ld+json">{json.dumps(schema)}</script>
  <style>
    body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; background: #0a0e1a; color: #e6e9f0; }}
    .hero {{ padding: 4rem 2rem; text-align: center; background: linear-gradient(180deg, #0a0e1a 0%, #141b2e 100%); }}
    .hero h1 {{ font-size: 3rem; margin: 0 0 1rem; }}
    .hero p {{ font-size: 1.25rem; color: #9aa5be; max-width: 720px; margin: 0 auto; }}
    .container {{ max-width: 1200px; margin: 0 auto; padding: 2rem; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1.25rem; }}
    .grid-card {{ background: #141b2e; border: 1px solid #232b41; border-radius: 12px; padding: 1.5rem; text-decoration: none; color: inherit; transition: transform .15s, border-color .15s; display: block; }}
    .grid-card:hover {{ transform: translateY(-2px); border-color: #ff6b35; }}
    .grid-card.gated {{ opacity: 0.7; }}
    .iso-badge {{ display: inline-block; background: #ff6b35; color: #0a0e1a; font-weight: 700; padding: .25rem .6rem; border-radius: 6px; font-size: .8rem; letter-spacing: .05em; }}
    .grid-card h3 {{ margin: .75rem 0 .25rem; font-size: 1.1rem; }}
    .states {{ font-size: .85rem; color: #9aa5be; margin-bottom: 1rem; }}
    .metrics {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin: 1rem 0; }}
    .metric .num {{ font-size: 1.6rem; font-weight: 700; color: #ff6b35; }}
    .metric .lbl {{ font-size: .75rem; color: #9aa5be; text-transform: uppercase; }}
    .top-fuel {{ font-size: .85rem; color: #9aa5be; }}
    .cta {{ margin-top: 1rem; font-size: .9rem; color: #ff6b35; font-weight: 600; }}
    .paywall {{ text-align: center; padding: 1rem 0; }}
    .lock {{ font-size: 2rem; }}
    .paywall a {{ color: #ff6b35; }}
    footer {{ padding: 2rem; text-align: center; color: #6b7593; font-size: .85rem; }}
    .free-banner {{ background: #ff6b35; color: #0a0e1a; padding: .75rem; text-align: center; font-weight: 600; }}
    .free-banner a {{ color: #0a0e1a; text-decoration: underline; }}
  </style>
  <script src="/static/gating.js" defer></script>
</head>
<body>
  {'<div class="free-banner">Free tier viewing PJM + ERCOT only. <a href="/pricing">Unlock all 7 ISOs →</a></div>' if tier == 'free' else ''}
  <div class="hero">
    <h1>US Grid Intelligence — Live</h1>
    <p>Real-time demand, generation mix, and headroom across all 7 US ISOs.
       Updated every 5 minutes from EIA. Trusted by site selectors,
       data center operators, and energy traders.</p>
  </div>
  <div class="container">
    <div class="grid">
      {''.join(cards_html)}
    </div>
  </div>
  <footer>
    <p>Data: EIA Open Data API · Updated {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
    <p><a href="/" style="color:#ff6b35">← DC Hub home</a> · <a href="/api/docs" style="color:#ff6b35">API access</a></p>
  </footer>
</body>
</html>'''


def render_grid_iso_html(iso, meta, live, schema):
    """Server-side HTML for /grid/<iso> deep page.

    Phase II (2026-05-14): field-name fix. The previous code read
    `current_demand_mw` and `total_capacity_mw` — but the underlying
    /api/v1/grid/intelligence/<iso> API returns `demand_mw` and uses
    `demand_24h` peak as a capacity proxy. Result: every /grid/<iso>
    page showed '0 MW' and '0% headroom' in the OG tags + body even
    though the API was returning correct values like demand_mw=88907.

    Same field-resolution pattern as routes/grid_card_routes.py:50
    (which renders the OG card PNG and was already doing it right).
    """
    # `demand_mw` is the canonical field (string or int from EIA).
    # Fall back to current_demand_mw for legacy shapes.
    demand = _to_int(live.get('demand_mw') or live.get('current_demand_mw') or 0)

    # 24h peak as capacity proxy
    _h24 = live.get('demand_24h', []) or []
    _peaks = [_to_int(row.get('mw')) for row in _h24 if isinstance(row, dict)]
    capacity = max(_peaks) if _peaks else _to_int(live.get('total_capacity_mw') or demand)

    # Headroom: percent below 24h peak
    if capacity > 0 and demand > 0:
        headroom = max(0.0, (capacity - demand) / capacity * 100.0)
    else:
        headroom = float(live.get('headroom_pct', 0) or 0)

    gen_mix_raw = live.get('generation_mix', {}) or {}
    # The API returns generation_mix as {fuel: {"mw": "N", "period": "..."}}.
    # Normalize to {fuel: int_mw} for the rendering loop.
    gen_mix = {}
    if isinstance(gen_mix_raw, dict):
        for fuel, val in gen_mix_raw.items():
            if isinstance(val, dict):
                gen_mix[fuel] = _to_int(val.get('mw'))
            else:
                gen_mix[fuel] = _to_int(val)
    demand_24h = _h24

    fuel_rows = ''
    if isinstance(gen_mix, dict):
        for fuel, mw in sorted(gen_mix.items(), key=lambda kv: -(kv[1] or 0)):
            pct = (mw / demand * 100) if demand and mw else 0
            fuel_rows += f'<tr><td>{fuel}</td><td style="text-align:right">{int(mw or 0):,} MW</td><td style="text-align:right">{pct:.1f}%</td></tr>'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{iso} Grid — Live Demand, Generation Mix & Headroom | DC Hub</title>
  <meta name="description" content="{meta['name']} ({meta['states']}). Live demand: {demand:,} MW. Headroom: {headroom:.0f}%. Real-time generation mix updated every 5 minutes.">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta property="og:title" content="{iso} Grid: {demand:,} MW | DC Hub">
  <meta property="og:description" content="{meta['tagline']} · {headroom:.0f}% headroom · live EIA data.">
  <meta property="og:image" content="https://dchub.cloud/api/v1/grid/{iso}/card.png">
  <meta property="og:url" content="https://dchub.cloud/grid/{iso}">
  <meta name="twitter:card" content="summary_large_image">
  <link rel="canonical" href="https://dchub.cloud/grid/{iso}">
  <script type="application/ld+json">{json.dumps(schema)}</script>
  <style>
    body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; background: #0a0e1a; color: #e6e9f0; }}
    .nav {{ padding: 1rem 2rem; border-bottom: 1px solid #232b41; }}
    .nav a {{ color: #ff6b35; text-decoration: none; }}
    .container {{ max-width: 1200px; margin: 0 auto; padding: 2rem; }}
    h1 {{ font-size: 2.5rem; margin: 0 0 .5rem; }}
    .subtitle {{ color: #9aa5be; margin-bottom: 2rem; }}
    .stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin: 2rem 0; }}
    .stat-card {{ background: #141b2e; border: 1px solid #232b41; border-radius: 12px; padding: 1.5rem; }}
    .stat-card .num {{ font-size: 2rem; font-weight: 700; color: #ff6b35; }}
    .stat-card .lbl {{ font-size: .85rem; color: #9aa5be; text-transform: uppercase; margin-top: .25rem; }}
    table {{ width: 100%; background: #141b2e; border: 1px solid #232b41; border-radius: 12px; border-collapse: separate; border-spacing: 0; }}
    th, td {{ padding: .75rem 1rem; border-bottom: 1px solid #232b41; }}
    th {{ text-align: left; color: #9aa5be; font-weight: 600; font-size: .85rem; text-transform: uppercase; }}
    tr:last-child td {{ border-bottom: none; }}
    section {{ margin: 3rem 0; }}
    section h2 {{ font-size: 1.5rem; margin-bottom: 1rem; }}
    .api-box {{ background: #141b2e; border: 1px solid #232b41; border-left: 4px solid #ff6b35; padding: 1.5rem; border-radius: 8px; font-family: ui-monospace, monospace; font-size: .9rem; overflow-x: auto; }}
  </style>
</head>
<body>
  <div class="nav"><a href="/grid">← All ISOs</a> · <a href="/">DC Hub</a></div>
  <div class="container">
    <h1>{iso} — {meta['name']}</h1>
    <p class="subtitle">{meta['states']} · {meta['tagline']}</p>
    <div class="stat-grid">
      <div class="stat-card"><div class="num">{demand:,}</div><div class="lbl">MW serving now</div></div>
      <div class="stat-card"><div class="num">{capacity:,}</div><div class="lbl">MW capacity</div></div>
      <div class="stat-card"><div class="num">{headroom:.0f}%</div><div class="lbl">headroom</div></div>
      <div class="stat-card"><div class="num">{len(gen_mix)}</div><div class="lbl">fuel sources</div></div>
    </div>
    <section>
      <h2>Generation Mix (real-time)</h2>
      <table>
        <thead><tr><th>Fuel</th><th style="text-align:right">Output</th><th style="text-align:right">% of demand</th></tr></thead>
        <tbody>{fuel_rows or '<tr><td colspan="3" style="text-align:center;color:#6b7593">No mix data available</td></tr>'}</tbody>
      </table>
    </section>
    <section>
      <h2>Use this data via API</h2>
      <div class="api-box">GET https://dchub.cloud/api/v1/grid/intelligence/{iso}</div>
      <p style="color:#9aa5be;margin-top:.75rem">Authenticated requests get higher rate limits and queue analytics. <a href="/pricing" style="color:#ff6b35">See pricing →</a></p>
    </section>
  </div>
</body>
</html>'''


def render_paywall_html(iso, meta):
    return f'''<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<title>{iso} — Pro Tier Required | DC Hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; background: #0a0e1a; color: #e6e9f0; min-height: 100vh; display: flex; align-items: center; justify-content: center; }}
.box {{ max-width: 480px; background: #141b2e; border: 1px solid #232b41; border-radius: 12px; padding: 3rem; text-align: center; }}
.lock {{ font-size: 3rem; }}
h1 {{ margin: 1rem 0 .5rem; }}
p {{ color: #9aa5be; }}
a.btn {{ display: inline-block; background: #ff6b35; color: #0a0e1a; padding: .85rem 1.75rem; border-radius: 8px; text-decoration: none; font-weight: 600; margin-top: 1.5rem; }}
</style></head><body><div class="box">
<div class="lock">🔒</div>
<h1>{iso} Grid Intelligence</h1>
<p>{meta['name']} live data is available on the Pro tier. Free tier covers PJM and ERCOT.</p>
<a class="btn" href="/pricing">Unlock all 7 ISOs →</a>
<p style="margin-top:2rem"><a href="/grid" style="color:#ff6b35">← Back to grid hub</a></p>
</div></body></html>'''
