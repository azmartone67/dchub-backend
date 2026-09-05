"""Phase 23 — public grid intelligence routes.

These render server-side HTML for SEO. Each page has Schema.org JSON-LD,
OG meta tags, and pulls live data from /api/v1/grid/intelligence/<iso>.
 [phase68_gating_applied]"""
from flask import Blueprint, render_template, jsonify, request, Response, redirect
import json, datetime, requests
from utc_clock import utc_now

grid_public_bp = Blueprint('grid_public', __name__)

ISOS = {
    'PJM':    {'name': 'PJM Interconnection',   'states': '13 states + DC',         'tagline': 'Largest US grid operator'},
    'MISO':   {'name': 'Midcontinent ISO',      'states': '15 states + Manitoba',   'tagline': 'Industrial heartland grid'},
    'ERCOT':  {'name': 'Electric Reliability Council of Texas', 'states': 'Texas',  'tagline': 'Texas independent grid'},
    'CAISO':  {'name': 'California ISO',        'states': 'California',             'tagline': 'Renewable-heavy western grid'},
    'NYISO':  {'name': 'New York ISO',          'states': 'New York',               'tagline': 'Dense urban grid operator'},
    'ISONE':  {'name': 'ISO New England',       'states': '6 NE states',            'tagline': 'New England grid operator'},
    'SPP':    {'name': 'Southwest Power Pool',  'states': '14 states',              'tagline': 'Wind-rich plains grid'},
    # #60 (2026-06-02): first LIVE international grids (tokenless real feeds).
    'NGESO':  {'name': 'Great Britain (NESO)',  'states': 'England, Scotland, Wales', 'tagline': 'Live UK grid — Elexon Insights'},
    'AEMO':   {'name': 'Australia NEM (AEMO)',  'states': 'NSW, QLD, SA, TAS, VIC',  'tagline': 'Live AU national electricity market'},
}

# Free tier sees only these — paid tiers unlock all 7
FREE_TIER_ISOS = {'PJM', 'ERCOT'}


def _user_tier(req):
    """Resolve the caller's REAL plan tier for the paid-ISO gate.

    r-gridauth (2026-06-23): the old impl trusted `req.headers.get('X-Tier')` and a
    plaintext `dch_tier` cookie — BOTH client-spoofable. Anyone could send
    `X-Tier: pro` (or `Cookie: dch_tier=pro`) and unlock every paid ISO's live grid
    render directly (a trivial paywall bypass; it's also what let the edge/worker cache
    get poisoned with a full render and replayed to anon). Now resolve from real,
    unforgeable credentials only:
      1. paid MCP key  → mcp_dev_keys via _resolve_key_tier (free/identified → None)
      2. signed session → dchub_token JWT via _detect_caller_tier (JWT_SECRET-verified)
    Everything else (incl. a bare X-Tier header or plaintext dch_tier cookie) = 'free'.
    Returns 'free' for all free-class callers so the `tier == 'free'` gate fires.
    """
    # 1. Paid API key (canonical MCP table)
    try:
        api_key = (req.headers.get('X-API-Key') or req.args.get('api_key') or '').strip()
        if api_key:
            from api_data_protection import _resolve_key_tier
            t = _resolve_key_tier(api_key)
            if t:
                return str(t).lower()
    except Exception:
        pass
    # 2. Signed logged-in session (JWT cookie) — verified, not a plaintext tier cookie
    try:
        from map_tier_gating import _detect_caller_tier

        def _dec(_t):
            try:
                import jwt as _j
                from main import JWT_SECRET
                return _j.decode(_t, JWT_SECRET, algorithms=['HS256'])
            except Exception:
                return None
        ct, _ = _detect_caller_tier(decode_jwt_func=_dec)
        ctl = str(ct or '').lower()
        if ctl and ctl not in (
                '', 'anonymous', 'anon', 'free', 'identified',
                'trial', 'trial_taste', 'trial_preview', 'preview'):
            return ctl
    except Exception:
        pass
    return 'free'


# Phase JJ (2026-05-14): in-process TTL cache for _fetch_live.
# CAISO's upstream API responds in ~10s right at the CF Worker edge
# timeout. Without caching, every page load eats that latency; with
# 5min TTL we serve cached on subsequent requests within the window.
# Simple dict, no Redis required — gunicorn workers each maintain
# their own cache, which is acceptable for "show some data fast"
# semantics. Real time-critical paths still hit the API directly.
_LIVE_CACHE: dict = {}            # iso → (timestamp_epoch, response_dict)
_LIVE_TTL_SECONDS = 5 * 60        # 5 minutes

# RENDER-PERF (2026-06-01): the per-worker _LIVE_CACHE above is wiped on every
# gunicorn worker recycle and isn't shared across workers/replicas, so a cold
# /grid or /grid/<ISO> render re-eats the slow upstream ISO fetch. Front it with
# the already-connected, cross-worker Redis helper (redis_cache.py — same
# best-effort pattern report_narrative.py uses) so a warm ISO payload survives a
# recycle. Redis is BEST-EFFORT in front of the dict: any import/connection/
# serialization error falls through to the dict path unchanged. cache_get/set
# already swallow their own errors and no-op when REDIS_URL is unset, so these
# wrappers stay quiet too. We store ONLY the response dict (NOT the process-local
# epoch timestamp) — Redis setex governs expiry via _LIVE_TTL_SECONDS.
def _redis_get_live(iso):
    """Return a cached ISO payload dict from Redis, or None on miss/any error."""
    try:
        from redis_cache import cache_get
        payload = cache_get(f"grid_live:{iso}")
        if isinstance(payload, dict) and payload:
            return payload
    except Exception:
        pass
    return None


def _redis_set_live(iso, inner) -> None:
    """Best-effort write an ISO payload dict to Redis with the module TTL.
    No-op on any error (incl. REDIS_URL unset)."""
    try:
        from redis_cache import cache_set
        cache_set(f"grid_live:{iso}", inner, ttl=_LIVE_TTL_SECONDS)
    except Exception:
        pass


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

    # RENDER-PERF: cross-worker Redis layer (survives gunicorn recycle). On a
    # hit, warm the local dict with a fresh local timestamp so subsequent
    # same-worker reads stay dict-fast; we deliberately DON'T trust the
    # original epoch (Redis setex already enforced freshness on its side).
    _r_inner = _redis_get_live(iso)
    if _r_inner:
        _LIVE_CACHE[iso] = (now, _r_inner)
        if len(_LIVE_CACHE) > 100:
            oldest_key = min(_LIVE_CACHE, key=lambda k: _LIVE_CACHE[k][0])
            _LIVE_CACHE.pop(oldest_key, None)
        return _r_inner

    # #60 (2026-06-02): international live grids (GB/AU) are served by their own
    # in-process modules (Elexon / AEMO), not the US /grid/intelligence loopback.
    # Flatten their live snapshot to the {demand_mw, renewable_pct, …} shape the
    # grid pages + scoreboard expect. Live-only — empty dict if the feed is down.
    if iso in ("NGESO", "AEMO"):
        try:
            if iso == "NGESO":
                from routes.iso_uk_elexon import _live_snapshot as _intl_snap
            else:
                from routes.iso_au_aemo import _live_snapshot as _intl_snap
            snap = _intl_snap()
            if snap:
                inner = {k: v.get("value") for k, v in snap.items()
                         if isinstance(v, dict)}
                _LIVE_CACHE[iso] = (now, inner)
                _redis_set_live(iso, inner)
                return inner
        except Exception:
            pass
        return cached[1] if cached else {}

    # r49-grid-perf (2026-05-31): the PUBLIC Railway edge URL was tried
    # FIRST with the default python-requests User-Agent. That request
    # re-enters our own CF/edge → anonymous rate limiter (20rpm) and gets
    # 429'd, burning ~2s per ISO and NEVER caching (r.ok is False on 429).
    # On the 1-replica backend this self-inflicted egress storm is one of
    # the documented worker-pool-starvation paths (PJM timeouts, 5-14s
    # /grid loads). FIX: call ONLY loopback (in-process WSGI, no edge, no
    # rate limiter) and send self-identifying headers so the rate limiter's
    # X-DC-Probe / internal-UA bypass fires even on the loopback path.
    # A successful loopback response is still cached in _LIVE_CACHE below.
    port = _os.environ.get('PORT', '8080')
    urls = [
        f'http://127.0.0.1:{port}/api/v1/grid/intelligence/{iso}',
    ]
    _self_headers = {'User-Agent': 'dchub-grid/1.0', 'X-DC-Probe': 'self-heal'}
    # r-gridkey (2026-07-31): /api/v1/grid/intelligence is METERED
    # (free_tier_gate.METERED_MAP_PREFIXES) and that gate runs at
    # before_request — before any route-level UA / X-DC-Probe bypass — so the
    # self-identifying headers above never privilege this call. The identical
    # loopback in routes/radar.py was 402'd by our own paywall and pinned
    # /radar to 07-16 baselines for 15 days (PR #2018); loopback remote_addr
    # alone is fragile (a dual-stack listener reports '::ffff:127.0.0.1').
    # X-Internal-Key clears every before_request gate regardless of socket
    # family. Guard: tests/test_grid_public_selfcall.py.
    _ikey = (_os.environ.get('DCHUB_INTERNAL_KEY')
             or _os.environ.get('DCHUB_SYNC_KEY') or '').strip()
    if _ikey:
        _self_headers['X-Internal-Key'] = _ikey
    for u in urls:
        try:
            r = requests.get(u, timeout=2, headers=_self_headers)  # r33: 8s→2s
            if not r.ok:
                # A swallowed non-200 is how the radar drift stayed invisible
                # for 15 days — the miss must be loud before the stale-cache
                # fallback quietly papers over it.
                import logging as _logging
                _logging.getLogger('grid_public').warning(
                    "grid _fetch_live %s: loopback HTTP %s from %s",
                    iso, r.status_code, u)
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
                    # RENDER-PERF: write-through to the cross-worker Redis layer
                    # so the next worker/replica skips the slow upstream fetch.
                    _redis_set_live(iso, inner)
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


def _demand_capacity_headroom(live):
    """Derive (demand, 24h-peak, headroom%) from ONE /api/v1/grid/intelligence
    payload — the single place this math lives.

    r-gridheadroom (2026-07-31): `headroom_pct` has NEVER existed in that
    payload (grep main.py: 0 hits), yet the /grid hub cards read it and
    rendered '0% headroom' on every card 24/7 — the radar renewable_share_pct
    ghost-field class (PR #2018). `total_capacity_mw` is the same ghost. The
    real fields are `demand_mw` (string or int; `current_demand_mw` is the
    legacy shape) and the `demand_24h` series, so headroom is DERIVED exactly
    as render_grid_iso_html always did: percent below the 24h peak.

    Returns (demand, peak, headroom); peak/headroom are None when the 24h
    series is absent or demand is unknown — NGESO/AEMO flatten Elexon/AEMO
    snapshots that carry no demand_24h, and None must render '—', never a
    structural 0%. A genuine 0.0 (demand AT the 24h peak, e.g. evening peak
    hours) still comes back as a number.
    Guard: tests/test_grid_headroom_ghostfield.py.
    """
    live = live if isinstance(live, dict) else {}
    demand = _to_int(live.get('demand_mw') or live.get('current_demand_mw') or 0)
    h24 = live.get('demand_24h') or []
    peaks = [_to_int(row.get('mw')) for row in h24 if isinstance(row, dict)]
    peak = max(peaks) if peaks else None
    if peak and demand > 0:
        headroom = max(0.0, (peak - demand) / peak * 100.0)
    else:
        headroom = None
    return demand, peak, headroom


@grid_public_bp.route('/grid', methods=['GET'])
def grid_hub():
    """Public hub page showing all 7 ISOs at a glance.

    r33-grid-perf (2026-05-21): /grid was taking 112s — killing Railway
    in a restart loop. Root cause: sequential _fetch_live() across 7
    ISOs, each with 8s timeout, each falling back to a SECOND URL with
    another 8s. Worst case 7 × 2 × 8 = 112s exactly matches Railway
    SLOW REQUEST logs. AND the first URL was localhost, which created
    a recursive self-call that hangs when the worker is already busy
    serving /grid.

    Fix: parallelize the 7 fetches with ThreadPoolExecutor (wall time
    becomes max single-fetch, not sum). Each individual _fetch_live
    keeps its 8s timeout for healthy paths, but the page now responds
    in <10s even when every ISO is slow. The recursive localhost call
    is left intact for the cache-hit fast path (it's an in-process
    Python call after the first fill) but the 8s ceiling prevents the
    runaway timeouts.
    """
    tier = _user_tier(request)
    cards = []

    # Fetch all 7 ISOs in parallel. 4 workers is enough — most ISOs
    # are cache hits after the first request, parallel only matters
    # on cold start or after the 5min cache expires.
    import concurrent.futures as _cf
    iso_keys = list(ISOS.keys())
    live_by_iso: dict = {}
    try:
        with _cf.ThreadPoolExecutor(max_workers=4,
                                     thread_name_prefix='grid-iso') as ex:
            futs = {ex.submit(_fetch_live, iso): iso for iso in iso_keys}
            # Hard ceiling: 10s for ALL 7 fetches combined. If one
            # ISO's upstream hangs, we just leave that card stale —
            # never block the whole page.
            for fut in _cf.as_completed(futs, timeout=10):
                iso = futs[fut]
                try:
                    live_by_iso[iso] = fut.result(timeout=1) or {}
                except Exception:
                    live_by_iso[iso] = {}
    except (_cf.TimeoutError, Exception):
        # Timeout on the whole batch — fill in empties from cache or {}.
        for iso in iso_keys:
            if iso not in live_by_iso:
                live_by_iso[iso] = _LIVE_CACHE.get(iso, (0, {}))[1] or {}

    for iso, meta in ISOS.items():
        live = live_by_iso.get(iso, {})
        gated = (tier == 'free' and iso not in FREE_TIER_ISOS)
        # r-gridheadroom (2026-07-31): derive headroom with the SAME helper
        # the /grid/<iso> page uses — live_by_iso[iso] holds the full payload
        # incl. demand_24h. The old card read live.get('headroom_pct'), a
        # field the intelligence payload never shipped, so every card said
        # '0% headroom' 24/7. None = underivable → the card renders '—'.
        demand, _peak, headroom = _demand_capacity_headroom(live)
        cards.append({
            'iso': iso,
            'name': meta['name'],
            'states': meta['states'],
            'tagline': meta['tagline'],
            'demand_mw': demand if not gated else None,
            'headroom': headroom if not gated else None,
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
    # ★ SAME GUARD ITS OWN CHILDREN ALREADY CARRY — the hub was missed by the
    # r-tierleak pass (2026-06-23) that hardened /grid/<iso> above.
    #
    # This body is tier-varying: `tier = _user_tier(request)` above gates 7 of
    # the 9 ISO cards for free callers (FREE_TIER_ISOS = PJM, ERCOT), nulling
    # demand_mw / headroom_pct / gen_mix. Returning it with no Cache-Control let
    # the main.py catch-all stamp `public, max-age=300, s-maxage=300` — a
    # BLANKET DEFAULT for handlers that set nothing, not a decision about this
    # page — and the edge then stored one entry per URL.
    #
    # Measured live before this fix, on a fresh cache-busted URL:
    #     seed WITH  X-API-Key  -> cf-cache-status: MISS   (populates)
    #     then ANON  same URL   -> cf-cache-status: HIT age:0
    # so a Pro key holder's full render was served to anonymous visitors. The
    # zone's tier-varying bypass (rule e30fab55) cannot prevent this: it keys on
    # the dchub_token / dchub_refresh COOKIE, while `_user_tier` resolves paid
    # tier from the X-API-Key HEADER first — the exact caller shape an MCP or
    # API client uses.
    #
    # Control proving the fix: /grid/<paid-iso> sets these same headers and
    # stays DYNAMIC on the identical probe.
    return Response(html, mimetype='text/html',
                    headers={"Cache-Control": "private, no-store, max-age=0",
                             "CDN-Cache-Control": "no-store"})


@grid_public_bp.route('/grid/<iso>', methods=['GET'])
@grid_public_bp.route('/grid/<iso>/', methods=['GET'])
def grid_iso(iso):
    """Per-ISO deep page."""
    _raw = iso
    iso = iso.upper()
    if iso not in ISOS:
        return Response('<h1>Unknown ISO</h1>', status=404, mimetype='text/html')
    # r-grid-canoncase (2026-07-04): the sitemap lists /grid/pjm (lowercase) but
    # this page self-canonicalized to /grid/PJM (uppercase) → GSC "alternate page
    # with proper canonical". Lowercase is now the ONE canonical form: 301 any
    # other casing to it, and every canonical/og/schema URL below is lowercase.
    if _raw != iso.lower():
        return redirect(f"/grid/{iso.lower()}", code=301)
    tier = _user_tier(request)
    if tier == 'free' and iso not in FREE_TIER_ISOS:
        # r-tierleak (2026-06-23): paywall vs full render share the /grid/<iso> cache
        # key; public-caching either cross-serves tiers (a free visitor gets a cached
        # paid full render, or a paid visitor gets a cached paywall). Paid-only ISOs are
        # tier-varying → private/no-store (after_request honors it, main.py ~9848).
        # r-grid-paywall-noindex (2026-08-27): the sitemap lists all seven
        # /grid/<iso> URLs, so Google was being asked to index five 38-word
        # interstitials that differ only in the ISO name and carried NO
        # canonical — a near-identical cluster with no self-selected
        # representative, which is exactly the "duplicate without
        # user-selected canonical" shape. Measured: /grid/caiso, isone, miso,
        # nyiso and spp render ~2.9 KB / 38 words against ~6.4 KB / 106 words
        # for the free-tier PJM and ERCOT pages.
        #
        # ★ noindex, NOT a canonical and NOT a sitemap edit. A canonical would
        #   only move the cluster into "Google chose a different canonical" —
        #   the page genuinely has nothing to represent. And the GATE is the
        #   single source of truth for which ISOs are paid: pruning the
        #   sitemap instead would drift the moment FREE_TIER_ISOS changes,
        #   whereas this flips automatically with the tier. Google reports
        #   these as "Excluded by noindex", which is the honest state.
        #
        # ★★ `follow` is deliberate — the interstitial's whole job is to send
        #    the reader to /pricing, and that link should still carry equity.
        return Response(render_paywall_html(iso, ISOS[iso]), mimetype='text/html',
                        headers={"Cache-Control": "private, no-store, max-age=0",
                                 "CDN-Cache-Control": "no-store",
                                 "X-Robots-Tag": "noindex, follow"})

    meta = ISOS[iso]
    live = _fetch_live(iso)

    schema = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": f"{iso} Real-Time Grid Intelligence",
        "description": f"Live demand, generation mix, and headroom for {meta['name']} ({meta['states']}).",
        "url": f"https://dchub.cloud/grid/{iso.lower()}",
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "creator": {"@type": "Organization", "name": "DC Hub"},
        "isAccessibleForFree": iso in FREE_TIER_ISOS,
        "temporalCoverage": str(datetime.datetime.utcnow().isoformat()) + 'Z',
    }

    html = render_grid_iso_html(iso, meta, live, schema)
    # Free-tier ISOs render identically for everyone → keep edge-cacheable (SEO/perf).
    # Paid-only ISOs are tier-gated (free gets the paywall above) → never edge-share the
    # full render or it'd be served to a free visitor on the same /grid/<iso> path.
    if iso in FREE_TIER_ISOS:
        return Response(html, mimetype='text/html')
    return Response(html, mimetype='text/html',
                    headers={"Cache-Control": "private, no-store, max-age=0",
                             "CDN-Cache-Control": "no-store"})


@grid_public_bp.route('/grid/sitemap.xml', methods=['GET'])
def sitemap():
    """Grid-specific sitemap (ISO hub URLs only).

    Phase FF+25-followup (2026-05-20): renamed from `/sitemap.xml` to
    `/grid/sitemap.xml`. Previously this 7-URL grid sitemap was shadowing
    the comprehensive 15,000-URL sitemap defined at main.py:16297
    (serve_sitemap_xml). Flask picked whichever loaded first, so on
    some boots Googlebot received only the 7 grid URLs instead of the
    full facility/market sitemap — silent SEO disaster.

    The grid-specific sitemap is still useful as a niche resource at
    /grid/sitemap.xml; main.py:16297 owns the canonical `/sitemap.xml`.
    """
    today = utc_now().strftime('%Y-%m-%d')
    base = 'https://dchub.cloud'
    urls = [
        ('/grid', '0.9', 'hourly'),
    ]
    for iso in ISOS:
        urls.append((f'/grid/{iso}', '0.8', 'hourly'))
    for slug in _QUEUE_PAGE_ISOS:
        urls.append((f'/grid/queue/{slug}', '0.8', 'daily'))
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
            # None = headroom underivable from the payload (no demand_24h
            # series — NGESO/AEMO flattened snapshots). Render '—', never a
            # fake 0%; 0% is reserved for demand genuinely AT the 24h peak.
            headroom = c.get('headroom')
            headroom_txt = '—' if headroom is None else f'{headroom:.0f}%'
            top_fuel = ''
            if c.get('gen_mix'):
                gm = c['gen_mix']
                if isinstance(gm, dict) and gm:
                    # r32-cf-audit-fix (2026-05-20): defensive coercion.
                    # gen_mix used to be {fuel: mw_int} but upstream data
                    # now sometimes ships {fuel: {mw: N, pct: P}}. The
                    # max() with `kv[1] or 0` compared dicts to dicts
                    # and threw 'unsupported >'. Coerce each value to a
                    # number — pull from .get('mw') if nested, else use
                    # the value directly when numeric, else 0.
                    def _num(v):
                        if isinstance(v, dict):
                            for k in ('mw', 'value', 'amount', 'gen_mw'):
                                if k in v:
                                    try: return float(v[k] or 0)
                                    except (TypeError, ValueError): pass
                            return 0
                        try: return float(v or 0)
                        except (TypeError, ValueError): return 0
                    try:
                        top_fuel = max(gm.items(), key=lambda kv: _num(kv[1]))[0]
                    except (ValueError, TypeError):
                        top_fuel = ''
            cards_html.append(f'''
            <a class="grid-card" href="/grid/{c['iso'].lower()}">  <!-- phase26_lowercase_links -->
              <div class="iso-badge">{c['iso']}</div>
              <h3>{c['name']}</h3>
              <div class="states">{c['states']}</div>
              <div class="metrics">
                <div class="metric"><div class="num">{demand:,}</div><div class="lbl">MW now</div></div>
                <div class="metric"><div class="num">{headroom_txt}</div><div class="lbl">headroom vs 24h peak</div></div>
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
    <p>Data: EIA Open Data API · Updated {utc_now().strftime('%Y-%m-%d %H:%M UTC')}</p>
    <p><a href="/" style="color:#ff6b35">← DC Hub home</a> · <a href="/api/docs" style="color:#ff6b35">API access</a></p>
  </footer>
  <!-- 2026-05-24: site_sentinel nav_missing finding — include dchub-nav.js
       so users can navigate away from /grid without back button. -->
  <script src="/js/dchub-nav.js" defer></script>
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

    r-gridheadroom (2026-07-31): that derivation now lives in
    _demand_capacity_headroom so the /grid hub cards share the exact
    same math (they'd regressed to reading the ghost headroom_pct
    field → '0% headroom' on every card). When the 24h series is
    absent (NGESO/AEMO flattened snapshots) peak/headroom are None and
    render as '—' — never a structural 0%.
    """
    demand, peak, headroom = _demand_capacity_headroom(live)
    headroom_txt = '—' if headroom is None else f'{headroom:.0f}%'
    peak_txt = '—' if peak is None else f'{peak:,}'
    # Meta/OG copy: drop the headroom clause entirely when underivable —
    # "Headroom: —" in an OG description reads as broken, not honest.
    desc_headroom = '' if headroom is None else f' Headroom vs 24h peak: {headroom:.0f}%.'
    og_headroom = '' if headroom is None else f'{headroom:.0f}% headroom vs 24h peak · '

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

    fuel_rows = ''
    if isinstance(gen_mix, dict):
        for fuel, mw in sorted(gen_mix.items(), key=lambda kv: -(kv[1] or 0)):
            pct = (mw / demand * 100) if demand and mw else 0
            fuel_rows += f'<tr><td>{fuel}</td><td style="text-align:right">{int(mw or 0):,} MW</td><td style="text-align:right">{pct:.1f}%</td></tr>'

    # Cross-link to the queue dashboard for ISOs that have one (2026-08-02).
    queue_link = (f' · <a href="/grid/queue/{iso.lower()}">{iso} interconnection queue</a>'
                  if iso.lower() in _QUEUE_PAGE_ISOS else '')

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{iso} Grid — Live Demand, Generation Mix & Headroom | DC Hub</title>
  <meta name="description" content="{meta['name']} ({meta['states']}). Live demand: {demand:,} MW.{desc_headroom} Real-time generation mix updated every 5 minutes.">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta property="og:title" content="{iso} Grid: {demand:,} MW | DC Hub">
  <meta property="og:description" content="{meta['tagline']} · {og_headroom}live EIA data.">
  <meta property="og:image" content="https://dchub.cloud/api/v1/grid/{iso}/card.png">
  <meta property="og:url" content="https://dchub.cloud/grid/{iso.lower()}">
  <meta name="twitter:card" content="summary_large_image">
  <link rel="canonical" href="https://dchub.cloud/grid/{iso.lower()}">
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
  <div class="nav"><a href="/grid">← All ISOs</a> · <a href="/">DC Hub</a>{queue_link}</div>
  <div class="container">
    <h1>{iso} — {meta['name']}</h1>
    <p class="subtitle">{meta['states']} · {meta['tagline']}</p>
    <div class="stat-grid">
      <div class="stat-card"><div class="num">{demand:,}</div><div class="lbl">MW serving now</div></div>
      <div class="stat-card"><div class="num">{peak_txt}</div><div class="lbl">MW 24h peak</div></div>
      <div class="stat-card"><div class="num">{headroom_txt}</div><div class="lbl">headroom vs 24h peak</div></div>
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
<meta name="robots" content="noindex, follow">
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


# ─────────────────────────────────────────────────────────────────────
# /grid/queue/<iso> — server-rendered interconnection-queue SEO pages
# (2026-08-02 query-win wave: "ercot interconnection queue" is a measured
# winnable SERP — tiny niche dashboards rank top-6 while we hold the
# all-7-ISO interconnect_queue table + live telemetry they don't have.)
#
# Same data as the MCP tools get_interconnection_queue / get_refined_queue
# (routes/interconnection_queues.py). ERCOT ships first; adding another ISO
# is one entry in _QUEUE_PAGE_ISOS. Reads go through main.get_read_db (the
# replica-routed wrapper), 8s statement timeout, 1h in-process cache with
# stale-serve fallback — this page can never pin the primary pool or 500.
# ─────────────────────────────────────────────────────────────────────

# public slug -> interconnect_queue ISO value. Ship ERCOT first; the other
# six are one entry each once the SERP result is proven.
_QUEUE_PAGE_ISOS = {
    'ercot': 'ERCOT',
}

# Same active/dead semantics as routes/interconnection_queues.py
# (_DEAD_STATUS_RE): "active" is defined by EXCLUDING terminal statuses —
# ISOs label live projects "IA FULLY EXECUTED", "DISIS STAGE", etc., so
# matching the word "active" undercounts.
_QUEUE_DEAD_STATUS_RE = r"(withdraw|cancel|terminat|suspend|commercial operation|deactivat)"

# Data-center-load classifier — mirrors routes/depth_master_shell.py.
_QUEUE_DC_LOAD_RE = r"data ?cent|hyperscale|colocation|server farm|compute campus|AI data"

_QUEUE_PAGE_CACHE: dict = {}      # path -> (html, ts); stale entries are
_QUEUE_PAGE_TTL = 3600            # re-served when the DB is unavailable.


def _queue_page_data(iso_value):
    """Aggregates + top projects for one ISO from interconnect_queue.

    Returns a dict, or None when the DB is unavailable (caller serves the
    stale cache or a no-numbers shell — numbers are never fabricated).
    NOTE: the live table has no created_at (predates the repo DDL) and no
    commercial-operation dates; freshness is MAX(queue_date) by design.
    """
    try:
        from main import get_read_db   # lazy: replica-routed, avoids circular import
        conn = get_read_db()
    except Exception:
        return None
    try:
        cur = conn.cursor()
        try:
            cur.execute("SET LOCAL statement_timeout = 8000")
        except Exception:
            pass
        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(capacity_mw),0) "
            "FROM interconnect_queue WHERE upper(iso) = %s", (iso_value,))
        total_count, total_mw = cur.fetchone() or (0, 0)
        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(capacity_mw),0) "
            "FROM interconnect_queue WHERE upper(iso) = %s "
            "AND COALESCE(queue_status,'') !~* %s",
            (iso_value, _QUEUE_DEAD_STATUS_RE))
        active_count, active_mw = cur.fetchone() or (0, 0)
        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(capacity_mw),0) "
            "FROM interconnect_queue WHERE upper(iso) = %s "
            "AND (fuel_type ILIKE 'load' OR project_name ~* %s)",
            (iso_value, _QUEUE_DC_LOAD_RE))
        dc_count, dc_mw = cur.fetchone() or (0, 0)
        cur.execute(
            "SELECT COALESCE(NULLIF(trim(fuel_type),''),'Unknown') AS fuel, "
            "COUNT(*), COALESCE(SUM(capacity_mw),0) "
            "FROM interconnect_queue WHERE upper(iso) = %s "
            "GROUP BY 1 ORDER BY 3 DESC LIMIT 12", (iso_value,))
        fuels = cur.fetchall() or []
        cur.execute(
            "SELECT COALESCE(NULLIF(trim(queue_status),''),'Unknown') AS st, "
            "COUNT(*), COALESCE(SUM(capacity_mw),0) "
            "FROM interconnect_queue WHERE upper(iso) = %s "
            "GROUP BY 1 ORDER BY 3 DESC LIMIT 10", (iso_value,))
        statuses = cur.fetchall() or []
        cur.execute(
            "SELECT project_name, county, state, fuel_type, capacity_mw, "
            "queue_status, queue_date "
            "FROM interconnect_queue WHERE upper(iso) = %s "
            "ORDER BY capacity_mw DESC NULLS LAST LIMIT 25", (iso_value,))
        top = cur.fetchall() or []
        cur.execute(
            "SELECT MIN(queue_date), MAX(queue_date) "
            "FROM interconnect_queue WHERE upper(iso) = %s", (iso_value,))
        qmin, qmax = cur.fetchone() or (None, None)
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not total_count:
        return None
    return {
        'total_count': int(total_count), 'total_mw': float(total_mw or 0),
        'active_count': int(active_count), 'active_mw': float(active_mw or 0),
        'dc_count': int(dc_count), 'dc_mw': float(dc_mw or 0),
        'fuels': fuels, 'statuses': statuses, 'top': top,
        'queue_date_min': qmin, 'queue_date_max': qmax,
    }


def _queue_ttp_months(iso_value):
    """Typical filings-derived time-to-power for the ISO, if known."""
    try:
        from routes.interconnection_queues import _ISO_TTP_MONTHS
        return _ISO_TTP_MONTHS.get(iso_value)
    except Exception:
        return None


def _esc(v):
    import html as _h
    return _h.escape(str(v if v is not None else '—'))


def render_grid_queue_html(slug, iso_value, meta, d):
    """Dark DC Hub brand shell (facilities_hub palette) + crawlable tables."""
    year = datetime.datetime.utcnow().year
    total_gw = d['total_mw'] / 1000.0
    active_gw = d['active_mw'] / 1000.0
    dc_gw = d['dc_mw'] / 1000.0
    ttp = _queue_ttp_months(iso_value)
    qmax = d.get('queue_date_max')
    fresh = f"Queue positions through {qmax}." if qmax else ""
    title = (f"{iso_value} Interconnection Queue — {d['total_count']:,} "
             f"Projects, {total_gw:,.0f} GW Tracked ({year}) | DC Hub")
    desc = (f"Live {iso_value} interconnection queue dashboard: "
            f"{d['total_count']:,} queued projects, {total_gw:,.0f} GW total, "
            f"{active_gw:,.0f} GW active, {dc_gw:,.1f} GW classified as "
            f"data-center load. Fuel mix, status breakdown and the 25 largest "
            f"projects — refreshed from the same feed our API and MCP tools "
            f"serve.")
    fuel_rows = "".join(
        f"<tr><td>{_esc(f[0])}</td><td>{int(f[1]):,}</td>"
        f"<td>{float(f[2] or 0)/1000.0:,.1f} GW</td></tr>"
        for f in d['fuels'])
    status_rows = "".join(
        f"<tr><td>{_esc(s[0])}</td><td>{int(s[1]):,}</td>"
        f"<td>{float(s[2] or 0)/1000.0:,.1f} GW</td></tr>"
        for s in d['statuses'])
    top_rows = "".join(
        f"<tr><td>{_esc(t[0])}</td><td>{_esc(t[1])}</td>"
        f"<td>{_esc(t[3])}</td>"
        f"<td>{(f'{float(t[4]):,.0f}' if t[4] is not None else '—')}</td>"
        f"<td>{_esc(t[5])}</td><td>{_esc(t[6])}</td></tr>"
        for t in d['top'])
    ttp_card = (
        f'<div class="stat"><span class="num">{ttp:g} mo</span>'
        f'<span class="lbl">Typical time-to-power (filings-derived)</span></div>'
        if ttp else "")
    schema = json.dumps({
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": f"{iso_value} Interconnection Queue",
        "description": (f"{iso_value} generator + load interconnection queue: "
                        f"{d['total_count']:,} projects, "
                        f"{total_gw:,.0f} GW. Project name, county, fuel, MW, "
                        "status and queue date."),
        "url": f"https://dchub.cloud/grid/queue/{slug}",
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "isAccessibleForFree": True,
        "creator": {"@type": "Organization", "name": "DC Hub",
                    "url": "https://dchub.cloud"},
        "distribution": [{
            "@type": "DataDownload",
            "encodingFormat": "application/json",
            "contentUrl": ("https://dchub.cloud/api/v1/interconnection-queue/"
                           f"by-iso?iso={iso_value}"),
        }],
        "temporalCoverage": (f"{d.get('queue_date_min') or ''}/"
                             f"{d.get('queue_date_max') or ''}"),
    }, ensure_ascii=False)
    return f'''<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
<meta name="robots" content="index, follow">
<link rel="canonical" href="https://dchub.cloud/grid/queue/{slug}">
<meta property="og:type" content="website">
<meta property="og:url" content="https://dchub.cloud/grid/queue/{slug}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:site_name" content="DC Hub">
<meta name="twitter:card" content="summary_large_image">
<link rel="alternate" type="application/mcp+json" href="https://dchub.cloud/mcp" title="DC Hub MCP">
<meta name="dchub:resource-type" content="interconnection-queue">
<meta name="dchub:mcp-tools" content="get_interconnection_queue,get_refined_queue,get_grid_data,analyze_site">
<script type="application/ld+json">{schema}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap">
<style>
:root{{--bg:#0a0a0f;--surf:#131319;--b:rgba(255,255,255,0.08);--tx:#fafafa;--mut:#a1a1aa;--dim:#71717a;--ind:#818cf8;--grad:linear-gradient(135deg,#6366f1,#a855f7)}}
body{{font-family:'Instrument Sans',-apple-system,sans-serif;background:var(--bg);color:var(--tx);margin:0;line-height:1.6}}
.wrap{{max-width:1080px;margin:0 auto;padding:2.5rem 1.4rem 5rem}}
.nav{{font-size:.9rem;margin-bottom:1.6rem}}.nav a{{color:var(--ind);text-decoration:none}}
h1{{font-size:2.4rem;font-weight:800;letter-spacing:-.02em;margin:0 0 .4rem;line-height:1.1}}
h2{{font-size:1.4rem;font-weight:700;margin:2.6rem 0 .7rem}}
.sub{{color:var(--mut);max-width:76ch;margin:0 0 1.2rem}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:1.4rem 0}}
.stat{{background:var(--surf);border:1px solid var(--b);border-left:3px solid #6366f1;border-radius:10px;padding:14px}}
.num{{font-size:1.55rem;font-weight:800;display:block}}
.lbl{{font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;color:var(--dim);letter-spacing:.06em}}
table{{width:100%;border-collapse:collapse;background:var(--surf);border:1px solid var(--b);border-radius:10px;overflow:hidden;margin:.5rem 0 1rem;font-size:.92rem}}
th{{text-align:left;padding:10px 12px;color:var(--dim);font-size:.74rem;text-transform:uppercase;letter-spacing:.06em}}
td{{padding:10px 12px;border-top:1px solid rgba(255,255,255,.05)}}
.tablewrap{{overflow-x:auto}}
.note{{color:var(--dim);font-size:.86rem}}
.cta{{background:linear-gradient(135deg,rgba(99,102,241,.12),rgba(168,85,247,.12));border:1px solid rgba(129,140,248,.4);border-radius:12px;padding:1.3rem 1.5rem;margin:2rem 0}}
.cta a.btn{{display:inline-block;background:#6366f1;color:#fff;padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:700;margin-right:10px}}
.cta a{{color:var(--ind)}}
.foot{{color:var(--dim);font-size:.85rem;margin-top:2.6rem;border-top:1px solid var(--b);padding-top:1.1rem}}
.foot a{{color:var(--ind);text-decoration:none}}
</style></head><body>
<div class="wrap">
  <div class="nav"><a href="/grid">← Grid hub</a> · <a href="/grid/{slug}">{iso_value} live telemetry</a> · <a href="/interconnection-queues">All ISO queues</a></div>
  <h1>{iso_value} Interconnection Queue</h1>
  <p class="sub">{_esc(meta.get('name',''))} · every queued generator and load position, from the same
  <code>interconnect_queue</code> feed our REST API and MCP tools serve. {fresh}</p>
  <div class="stats">
    <div class="stat"><span class="num">{d['total_count']:,}</span><span class="lbl">Queued projects</span></div>
    <div class="stat"><span class="num">{total_gw:,.0f} GW</span><span class="lbl">Total queued capacity</span></div>
    <div class="stat"><span class="num">{active_gw:,.0f} GW</span><span class="lbl">Active (non-terminal status)</span></div>
    <div class="stat"><span class="num">{dc_gw:,.1f} GW</span><span class="lbl">Classified data-center load</span></div>
    {ttp_card}
  </div>
  <p class="note">Methodology: counts include every queued position; historically most queued MW
  never reaches commercial operation. "Active" excludes withdrawn / cancelled / terminated /
  suspended / in-service statuses. This feed publishes no commercial-operation dates — we do not
  fabricate them.</p>

  <h2>Queued capacity by fuel / type</h2>
  <div class="tablewrap"><table>
    <thead><tr><th>Fuel / type</th><th>Projects</th><th>Capacity</th></tr></thead>
    <tbody>{fuel_rows}</tbody>
  </table></div>

  <h2>Queue status breakdown</h2>
  <div class="tablewrap"><table>
    <thead><tr><th>Status</th><th>Projects</th><th>Capacity</th></tr></thead>
    <tbody>{status_rows}</tbody>
  </table></div>

  <h2>25 largest queued projects</h2>
  <div class="tablewrap"><table>
    <thead><tr><th>Project</th><th>County</th><th>Fuel</th><th>MW</th><th>Status</th><th>Queue date</th></tr></thead>
    <tbody>{top_rows}</tbody>
  </table></div>

  <div class="cta">
    <strong>Query this queue programmatically.</strong>
    <p class="sub" style="margin:.4rem 0 1rem">REST: <code>GET /api/v1/interconnection-queue/by-iso?iso={iso_value}</code> ·
    MCP: <code>get_interconnection_queue</code> / <code>get_refined_queue</code> at <code>dchub.cloud/mcp</code>.
    Free tier works keyless; <a href="/pricing">public pricing</a> raises the caps, or
    <a href="/enterprise">talk to sales</a> for bulk export.</p>
    <a class="btn" href="/api-docs">API docs</a>
    <a class="btn" style="background:transparent;border:1px solid rgba(129,140,248,.5)" href="/grid/{slug}">Live {iso_value} telemetry →</a>
  </div>

  <p class="foot">
    <a href="/facilities">Facilities</a> · <a href="/dcpi">Power Index</a> ·
    <a href="/markets">Markets</a> · <a href="/grid">Grid</a> ·
    <a href="/interconnection-queues">Interconnection queues</a> ·
    DC Hub · <a href="/">dchub.cloud</a>
  </p>
</div>
<script src="/js/dchub-nav.js" defer></script>
</body></html>'''


def _render_queue_fallback(slug, iso_value, meta):
    """DB-unavailable shell — real links, NO fabricated numbers."""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{iso_value} Interconnection Queue | DC Hub</title>'
            f'<meta name="robots" content="index, follow">'
            f'<link rel="canonical" href="https://dchub.cloud/grid/queue/{slug}">'
            f'</head><body style="font-family:sans-serif;background:#0a0a0f;'
            f'color:#fafafa;max-width:760px;margin:2rem auto;padding:1rem">'
            f'<h1>{iso_value} Interconnection Queue</h1>'
            f'<p>{meta.get("name","")} queue dashboard — live figures are '
            f'refreshing. Query the feed directly: '
            f'<code>GET /api/v1/interconnection-queue/by-iso?iso={iso_value}</code>'
            f' or the <code>get_interconnection_queue</code> MCP tool.</p>'
            f'<p><a href="/interconnection-queues" style="color:#818cf8">All ISO queues</a> · '
            f'<a href="/grid/{slug}" style="color:#818cf8">{iso_value} live telemetry</a></p>'
            f'</body></html>')


@grid_public_bp.route('/grid/queue', methods=['GET'])
@grid_public_bp.route('/grid/queue/', methods=['GET'])
def grid_queue_hub():
    """Bare /grid/queue → the existing all-ISO landing (Flask would otherwise
    route it into /grid/<iso> as iso='queue' and 404)."""
    return redirect('/interconnection-queues', code=302)


@grid_public_bp.route('/grid/queue/<iso>', methods=['GET'])
@grid_public_bp.route('/grid/queue/<iso>/', methods=['GET'])
def grid_queue_iso(iso):
    """Per-ISO interconnection-queue SEO page. NEVER 500s."""
    try:
        raw = (iso or '').strip()
        slug = raw.lower()
        if slug not in _QUEUE_PAGE_ISOS:
            return redirect('/interconnection-queues', code=302)
        if raw != slug:
            # lowercase is the one canonical form (same as /grid/<iso>)
            return redirect(f'/grid/queue/{slug}', code=301)
        iso_value = _QUEUE_PAGE_ISOS[slug]
        path_key = f'/grid/queue/{slug}'
        import time as _t
        hit = _QUEUE_PAGE_CACHE.get(path_key)
        if hit and (_t.time() - hit[1]) < _QUEUE_PAGE_TTL:
            return Response(hit[0], mimetype='text/html',
                            headers={'Cache-Control': 'public, max-age=3600'})
        d = _queue_page_data(iso_value)
        if d is None:
            if hit:   # stale-serve beats a numberless shell
                return Response(hit[0], mimetype='text/html',
                                headers={'Cache-Control': 'public, max-age=600'})
            return Response(
                _render_queue_fallback(slug, iso_value, ISOS.get(iso_value, {})),
                mimetype='text/html',
                headers={'Cache-Control': 'public, max-age=300'})
        html = render_grid_queue_html(slug, iso_value, ISOS.get(iso_value, {}), d)
        _QUEUE_PAGE_CACHE[path_key] = (html, _t.time())
        return Response(html, mimetype='text/html',
                        headers={'Cache-Control': 'public, max-age=3600'})
    except Exception:
        try:
            return Response(_render_queue_fallback('ercot', 'ERCOT', {}),
                            mimetype='text/html', status=200)
        except Exception:
            return Response('<!doctype html><title>DC Hub</title>'
                            '<p><a href="/interconnection-queues">Interconnection'
                            ' queues</a>', mimetype='text/html', status=200)
