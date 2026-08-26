"""
seo_pages.py — SEO-indexable landing pages for facilities, markets, and ISO grids.

Phase ZZZZZ-round33 (2026-05-24). The biggest revenue lever still on the table:
15,000+ facilities × 1 landing page each = 21k high-value long-tail SEO targets.
At 1k pages indexed → ~500 organic visits/day → 1-2 paid MCP signups/day →
$50-150/mo recurring per landing-page wave.

Routes registered:
  GET /facility/<id>      — per-facility detail page (21k pages)
  GET /markets/<slug>     — per-market roll-up    (~50 pages)
  GET /grids/<iso>        — per-ISO roll-up       (16+ after intl expansion)
  (sitemaps moved 2026-07-03: /sitemap.xml in main.py is the canonical
   sitemapindex; the per-section shards live at /sitemap-<section>.xml)

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

from routes.facility_slug import hash_sql

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


# ── validated market links (2026-07-28) ─────────────────────────────────
# ROOT CAUSE of the /markets/* coverage churn. Facility pages emitted
#   href="/markets/{_slug(city + '-' + state)}"
# with NO check that the market exists. Market slugs are METRO/CITY keyed, so
# `dallas` and `dallas-fort-worth` are real and `dallas-texas` is not — every
# Dallas facility page linked to a URL that did not resolve. That is what put
# ~113/1000 of the GSC "Not found (404)" sample under /markets/, and it is why
# 2026-07-15 reached for a 302-to-hub (a soft 404) and why 2026-07-28 had to add
# a city-state -> city 301. Both patch the DESTINATION. This fixes the LINK.
#
# ★ Resolution order: `city-state` (a real metro slug like northern-virginia
#   wins outright) -> `city` -> no link at all. A page must never link to its
#   own 404.
# ★ ONE cached query, not one per render: the slug set is small, changes rarely,
#   and is refreshed on a TTL. A per-facility lookup would put a query on every
#   SEO page render — the sitemap-stampede failure mode.
# ★ FAIL-OPEN: if the set cannot be loaded we emit the legacy computed slug
#   rather than stripping navigation site-wide. The market route now resolves
#   city-state -> city with a 301, so a stale link still lands on the right page.
_MARKET_SLUGS_CACHE = {"at": 0.0, "slugs": None}
_MARKET_SLUGS_TTL_S = 900


def _valid_market_slugs():
    """Set of market slugs that actually resolve, or None when unknown."""
    import time as _t
    now = _t.time()
    if (_MARKET_SLUGS_CACHE["slugs"] is not None
            and now - _MARKET_SLUGS_CACHE["at"] < _MARKET_SLUGS_TTL_S):
        return _MARKET_SLUGS_CACHE["slugs"]
    try:
        c = _conn()
        if c is None:
            return _MARKET_SLUGS_CACHE["slugs"]
        try:
            with c.cursor() as cur:
                cur.execute("SELECT DISTINCT market_slug FROM market_power_scores "
                            "WHERE market_slug IS NOT NULL AND market_slug <> ''")
                got = {r[0] for r in (cur.fetchall() or []) if r and r[0]}
        finally:
            try: c.close()
            except Exception: pass
        if got:
            _MARKET_SLUGS_CACHE["slugs"] = got
            _MARKET_SLUGS_CACHE["at"] = now
        return _MARKET_SLUGS_CACHE["slugs"]
    except Exception:
        return _MARKET_SLUGS_CACHE["slugs"]      # stale beats wrong


def _market_slug_for(city, state):
    """Best REAL market slug for a city/state, or None when none exists."""
    city_s = _slug(city or '')
    if not city_s:
        return None
    combo = _slug(f"{city}-{state}") if state else city_s
    known = _valid_market_slugs()
    if known is None:                    # cache cold/unavailable -> legacy shape
        return combo
    for cand in (combo, city_s):
        if cand and cand in known:
            return cand
    return None


def _market_link(city, state, label_html, cls=''):
    """<a> to the real market, or plain text when there is no market to link."""
    slug = _market_slug_for(city, state)
    if not slug:
        return label_html
    _cls = f' class="{cls}"' if cls else ''
    return f'<a href="/markets/{_esc_attr(slug)}"{_cls}>{label_html}</a>'


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
# r-page-onramp (2026-07-04): crawl->tool crossover pack.
# Every crawled page carries (a) JSON-LD pointing at the LIVE query
# surfaces (SearchAction -> /api/v1/rag/search, Dataset distribution ->
# the /mcp endpoint), (b) a visible-but-subtle footer onramp line whose
# src=page-onramp param is the measurement marker (recorded into
# connect_landing_views by routes/mcp_connect.py), and (c) an X-Cite-As
# header with an as-of stamp. ASCII ONLY in headers — the industry-pulse
# em-dash made gunicorn 502 every response (routes/industry_pulse.py).
# ═════════════════════════════════════════════════════════════════════
MCP_ENDPOINT = "https://dchub.cloud/mcp"
RAG_SEARCH_URL_TEMPLATE = "https://dchub.cloud/api/v1/rag/search?q={search_term_string}"


def _search_action() -> dict:
    """SearchAction potentialAction node pointing at the keyless RAG search."""
    return {
        "@type": "SearchAction",
        "target": {"@type": "EntryPoint", "urlTemplate": RAG_SEARCH_URL_TEMPLATE},
        "query-input": "required name=search_term_string",
    }


def _mcp_dataset_node(name: str, canonical: str, description: str) -> dict:
    """Dataset node whose distribution points at the live MCP endpoint."""
    return {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": name,
        "description": description,
        "url": canonical,
        "isAccessibleForFree": True,
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "creator": {"@type": "Organization", "name": "DC Hub",
                    "url": "https://dchub.cloud"},
        "distribution": [{"@type": "DataDownload",
                          "encodingFormat": "application/json",
                          "contentUrl": MCP_ENDPOINT}],
        "potentialAction": _search_action(),
    }


def _onramp_footer_html(noun: str, entity: str) -> str:
    """One subtle footer line: the machine-readable MCP onramp hint."""
    if not entity:
        return ""
    u = f"https://dchub.cloud/connect?src=page-onramp&entity={entity}"
    return (f'<p class="dc-onramp" style="font-size:0.8rem">'
            f'Query this {_h(noun)} live via MCP: '
            f'<a href="{_esc_attr(u)}">{_h(u)}</a></p>')


def _ascii_header(v: Any) -> str:
    """ASCII-only header value (headers must be latin-1). Never raises."""
    try:
        return str(v or "").encode("ascii", "ignore").decode("ascii")
    except Exception:
        return ""


# ═════════════════════════════════════════════════════════════════════
# COMMON BASE TEMPLATE (used by all 3 page types)
# ═════════════════════════════════════════════════════════════════════
def _base_html(*, title: str, description: str, canonical: str,
               og_image: str, schema_jsonld: str, body_html: str,
               og_type: str = "website", extra_footer_html: str = "") -> str:
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
  <p class="dc-browse">Browse: <a href="/facilities">All facilities by country</a> · <a href="/dcpi">DC Hub Power Index</a> · <a href="/markets">Markets</a> · <a href="/grid">Grid</a></p>
  <p>{_CANON_FAC} facilities · 7 ISO grid feeds · {_CANON_DEALS} M&amp;A deals tracked · 540+ project pipeline</p>
{extra_footer_html}
</footer>
</body>
</html>"""


def _canonical_facility_slug(provider, name):
    """Canonical /facilities/<slug> path segment for a facility row, or None.

    r-lane5 (2026-07-31): DELEGATES to facility_slug_freeze.build_canonical_slug
    — the ONE composer, the same function the daily freeze stores. This helper
    used to hand-compose without the provider-prefix dedupe (and without ascii
    folding), so every not-yet-frozen row 301'd to a DOUBLED URL
    (iron-mountain-iron-mountain-lon-3-…) that silently MOVED when the freeze
    ran — the flywheel lane-5 drift. Resolution is unaffected either way: the
    /facilities/<slug> lookup falls back to the hash8 tail, which the dedupe
    never changes."""
    from routes.facility_slug_freeze import build_canonical_slug
    return build_canonical_slug(provider, name)


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
                       latitude, longitude, power_mw, status, canonical_slug,
                       source, source_url, confidence_score, last_updated
                  FROM discovered_facilities
                 WHERE (CAST(id AS TEXT) = %s)
                    OR (%s IS NOT NULL AND """ + hash_sql('') + """ = %s)
                    OR LOWER(REPLACE(REPLACE(COALESCE(name,''),' ','-'),',','')) = LOWER(%s)
                 LIMIT 1
            """, (id_or_slug, hash8, hash8, id_or_slug))
            row = cur.fetchone()

        # r-sitemap-404s (2026-07-01): the sitemap's collision-fallback URLs
        # (main.py serve_sitemap_xml) emit /facility/<id> for LEGACY
        # `facilities` rows too — their ids are TEXT (12.8k bare-16-hex +
        # 513 osm_*), which the discovered-only CAST(id AS TEXT) match above
        # can never resolve, so ~5,300 sitemap URLs 404'd live (GSC 21k
        # submitted / 0 indexed). Resolve by exact legacy id first (unique),
        # with the same hash8 fallback mirrored; exact-match wins the ordering
        # so a junk id whose tail happens to parse as 8-hex can't mis-route.
        # Legacy has the same columns plus sqft/tier (richer render).
        if not row:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, name, provider, address, city, state, country,
                           latitude, longitude, power_mw, status, sqft, tier,
                           canonical_slug,
                           source, source_url, confidence_score, last_updated
                      FROM facilities
                     WHERE (id = %s)
                        OR (%s IS NOT NULL AND """ + hash_sql('') + """ = %s)
                     ORDER BY (id = %s) DESC
                     LIMIT 1
                """, (id_or_slug, hash8, hash8, id_or_slug))
                row = cur.fetchone()

        if not row:
            return _error_page(f"Facility '{_h(id_or_slug)}' not found.", 404)

        # r-facility-301 (2026-07-03, QA deep-dive indexing blocker): every
        # resolvable /facility/<id> page is a THIN, self-canonical duplicate of
        # the canonical /facilities/<slug> profile (same facility, ~82 visible
        # words, zero inbound links). 5,772 of them sat in the sitemap next to
        # 14,411 slug pages — Google parked the dupes in "not indexed: Other",
        # Bing burned crawl quota on them. Consolidate: 301 to the canonical
        # slug URL (collision-losers land on the winner's page by design — the
        # slug is keyed on provider|name). Only when no canonical slug can be
        # built (name too short/empty) do we still render the page below.
        # /facility/aws-<code> + the ADDRESS_MAP landings have their own more-
        # specific routes, so they never reach this handler.
        # r-frozen-slug (2026-07-06): 301 to the STORED canonical_slug (the exact
        # slug the sitemap emits), not a live recompute. A recompute that drifted
        # from the frozen slug after re-ingestion was minting redirect/404/
        # canonical churn (~8k GSC pages not-indexed). Prefer stored; recompute
        # only for un-backfilled rows (name-too-short still returns None → render).
        _stored_slug = row.get('canonical_slug')
        _canon_slug = _stored_slug if _stored_slug else _canonical_facility_slug(row.get('provider'), row.get('name'))
        if _canon_slug:
            _resp = redirect(f"https://dchub.cloud/facilities/{_canon_slug}", code=301)
            _resp.headers['Cache-Control'] = 'public, max-age=86400'
            _resp.headers['X-DC-Page-Source'] = 'seo-facility-canonical-301'
            return _resp

        # Find similar facilities nearby (same city) for "related" section
        nearby = []
        try:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # CAST id for the != — legacy `facilities` ids are TEXT
                # (hex/osm_*), and comparing those against the integer
                # discovered_facilities.id raised, silently dropping the
                # whole nearby section for legacy-resolved pages.
                cur.execute("""
                    SELECT id, name, provider, power_mw
                      FROM discovered_facilities
                     WHERE city = %s AND state = %s AND CAST(id AS TEXT) != %s
                       AND COALESCE(is_duplicate, 0) = 0
                     ORDER BY power_mw DESC NULLS LAST
                     LIMIT 8
                """, (row['city'], row['state'], str(row['id'])))
                nearby = cur.fetchall()
        except Exception:
            pass
    except Exception as _fe:
        # r-facility-5xx (2026-07-14): the outer try had ONLY a finally — any DB
        # timeout / pooler reset in the resolver propagated uncaught as a Flask 500
        # (the ~3k /facility 5xx bucket). Degrade to a 503 so crawlers back off
        # instead of burning quota on 5xx. Not-found stays a clean 404 above.
        try:
            import logging as _lg; _lg.getLogger(__name__).warning(f"facility_page resolver failed for {id_or_slug!r}: {_fe}")
        except Exception:
            pass
        return _error_page("Facility temporarily unavailable — please retry shortly.", 503)
    finally:
        try: c.close()
        except Exception: pass

    return Response(
        _render_facility(row, nearby),
        mimetype="text/html",
        headers={
            "Cache-Control": "public, max-age=900, s-maxage=3600",
            "X-DC-Page-Source": "seo-facility",
            # r-page-onramp: as-of citation header, ASCII only (latin-1 trap)
            "X-Cite-As": _ascii_header(
                f"DC Hub Facility {row['id']} - as of "
                f"{_dt.date.today().isoformat()}"),
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

    # r-page-onramp (2026-07-04): schema built via json.dumps (the old manual
    # f-string could emit invalid JSON on quoted names) and extended with the
    # crawl->tool crossover nodes: SearchAction on the Place + a Dataset node
    # whose distribution points at the live MCP endpoint.
    import json as _json
    _place_node = {
        "@context": "https://schema.org",
        "@type": "Place",
        "name": name,
        "description": desc[:200],
        "url": canonical,
        "address": {
            "@type": "PostalAddress",
            "addressLocality": city,
            "addressRegion": state,
            "addressCountry": country,
        },
        "additionalType": "https://schema.org/DataCenter",
        "potentialAction": _search_action(),
    }
    if lat and lon:
        _place_node["geo"] = {"@type": "GeoCoordinates",
                              "latitude": lat, "longitude": lon}
    _ds_node = _mcp_dataset_node(
        f"{name} - facility intelligence record", canonical,
        f"Structured data-center facility record for {name} ({location}). "
        f"Queryable live via the DC Hub MCP endpoint.")
    schema = _json.dumps([_place_node, _ds_node], indent=2)

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
  <p>{_market_link(city, state, f"All {_h(city)} data centers &rarr;")}</p>"""

    map_link = ""
    if lat and lon:
        map_link = f'<a href="https://www.openstreetmap.org/?mlat={lat}&mlon={lon}&zoom=15" target="_blank" rel="noopener">View on map ↗</a>'

    # Breadcrumb now links UP to the /facilities hub (un-orphans this page for
    # crawl: facility → /facilities/in/<country> → /facilities).
    _country_crumb = (f' · <a href="/facilities/in/{_esc_attr(country.lower().strip())}">{_h(country)}</a>'
                      if country else '')
    # Onward links (analytics 2026-06-29: facility pages had pages/session=1 +
    # dead clicks on plain-text fields). Only ALWAYS-RESOLVING targets:
    #  - /markets/<city-state> (the market always has ≥1 facility — this one) ✓
    #  - /facilities/in/<country> (hub returns 200 for any country) ✓
    # NOT operator (/operators/<slug> 404s for long-tail ops) or /dcpi/<city>
    # (404 for non-DCPI cities) — verified — so those stay plain text.
    # ★ 2026-07-28: the note above claimed "the market always has >=1 facility
    # — this one ✓". That is the exact wrong assumption. The FACILITY exists;
    # the market SLUG built from city+state may not (dallas-texas vs dallas), so
    # this link 404'd for every facility in a city whose metro slug differs.
    # Third emitter of the same bug, and the one a grep for 'href="/markets/'
    # misses because it assembles the URL into a variable first.
    _loc_cell = _market_link(city, state, _h(location))
    _country_browse = (
        f'<p class="dc-browse" style="margin-top:14px">'
        f'<a href="/facilities/in/{_esc_attr(country.lower().strip())}">'
        f'← Browse all data centers in {_h(country)}</a></p>' if country else '')
    body = f"""<header class="dc-seo">
  <nav class="breadcrumb">
    <a href="/">DC Hub</a> · <a href="/facilities">Facilities</a>{_country_crumb} · {_market_link(city, state, f"{_h(city)}, {_h(state)}")} · {_h(name)}
  </nav>
  <h1>{_h(name)}</h1>
  <p class="lede">{_h(operator)} data center in {_h(location)}.</p>
  {badges_html}
</header>

<section id="overview">
  <h2>Overview</h2>
  <table>
    <tr><th>Operator</th><td>{_h(operator)}</td></tr>
    <tr><th>Location</th><td>{_loc_cell} {map_link}</td></tr>
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
  <p>This page shows the public summary. The full profile adds M&amp;A history, lease comparables, power profile, fiber carrier presence, water risk, and competitive analysis — live and machine-readable.</p>
  <a href="/connect?ref=facility-{_esc_attr(fac_id)}" class="cta">Get live data free via the DC Hub MCP API →</a>
  <a href="/pricing?ref=facility&tool={_esc_attr(fac_id)}" class="cta secondary">Or generate a full PDF report</a>
  {_country_browse}
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
        extra_footer_html=_onramp_footer_html("facility", str(fac_id)),
    )


# ═════════════════════════════════════════════════════════════════════
# MARKET PAGE — /markets/<slug>
# ═════════════════════════════════════════════════════════════════════
def _markets_dir_redirect():
    # r-markets-404 (2026-07-15): unknown /markets/<slug> used to hard-404
    # (~113/1000 of the GSC "Not found (404)" sample were dead /markets/*).
    # These are INTERNAL links — /facility/<id> pages emit href="/markets/<slug>"
    # for every facility, but (a) the city+state round-trip is lossy for
    # multi-word cities/states (champs-sur-marne-ile-de-france) and (b) legacy
    # facilities-table cities (Casper WY, Ottawa ON, Dubai) have no roll-up.
    # Send crawlers to the server-rendered markets hub instead of a dead end
    # (link equity → real market pages). 302 (not 301) + short cache so a market
    # self-heals to its own 200 page the moment a facility backfills.
    # ★★ REVERSED 2026-07-28. The 2026-07-15 note above argued this is "not a
    # soft-404 (a real redirect to a real 200 page)". Google disagrees, and GSC
    # now says so: 299 Soft 404s. Mass-redirecting many unrelated missing pages
    # to ONE generic hub is Google's own textbook definition of a soft 404 —
    # what matters is that the destination does not answer the request, not
    # that it returns 200.
    # ★The redirect only ever fired when the market has ZERO facilities, i.e.
    # there is genuinely nothing to serve. 404 is the honest answer.
    # Link equity is preserved by LINKING to the hub from the 404 body: a 404
    # page's links are still crawled for discovery, and an honest 404 costs far
    # less than a soft-404 flag across the whole /markets/ space.
    _r = _error_page(
        "That market has no data centers in DC Hub yet. "
        "Browse the full market directory for one that does.", 404)
    _r.headers['Cache-Control'] = 'public, max-age=3600'
    _r.headers['X-DC-Page-Source'] = 'seo-market-404'
    return _r


@seo_pages_bp.get("/markets/<slug>", strict_slashes=False)
def market_page(slug: str):
    slug = slug.strip().lower()
    if not slug or len(slug) > 100:
        abort(404)

    c = _conn()
    if c is None:
        return _error_page("Database temporarily unavailable", 503)

    parts = slug.replace('_', '-').split('-')
    if len(parts) < 2:
        return _markets_dir_redirect()
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
        return _markets_dir_redirect()

    return Response(
        _render_market(slug, city_guess, state_guess, facilities, stats),
        mimetype="text/html",
        headers={
            "Cache-Control": "public, max-age=1800, s-maxage=3600",
            "X-DC-Page-Source": "seo-market",
            # r-page-onramp: as-of citation header, ASCII only (latin-1 trap)
            "X-Cite-As": _ascii_header(
                f"DC Hub Market {slug} - as of "
                f"{_dt.date.today().isoformat()}"),
        },
    )


def _render_market(slug, city, state, facilities, stats) -> str:
    canonical = f"https://dchub.cloud/markets/{slug}"
    n_fac     = stats['facility_count'] if stats else len(facilities)
    total_mw  = _round(stats['total_mw'], 1) if stats and stats['total_mw'] else 0
    n_op      = stats['operator_count'] if stats else 0

    title = f"{city}, {state} Data Centers — {n_fac} facilities, {total_mw} MW | DC Hub"
    desc  = f"Complete {city}, {state} data center market intelligence. {n_fac} facilities, {total_mw}MW total capacity across {n_op} operators. Live power, fiber, M&A data."

    # r-page-onramp (2026-07-04): Place + SearchAction pointing at the live
    # RAG search, plus a Dataset node whose distribution is the MCP endpoint
    # (crawl->tool crossover). json.dumps guarantees the ld+json block parses.
    import json as _json
    _place_node = {
        "@context": "https://schema.org",
        "@type": "Place",
        "name": f"{city}, {state} Data Center Market",
        "description": desc[:200],
        "url": canonical,
        "potentialAction": _search_action(),
    }
    _ds_node = _mcp_dataset_node(
        f"{city}, {state} data center market intelligence", canonical,
        f"Structured market record for {city}, {state}: {n_fac} facilities, "
        f"{total_mw} MW total capacity, {n_op} operators. Queryable live via "
        f"the DC Hub MCP endpoint.")
    schema = _json.dumps([_place_node, _ds_node], indent=2)

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
  <p class="dc-maplink" style="margin:.4rem 0 0"><a href="/map" style="color:#3b82f6;font-weight:600;text-decoration:none">📍 See {_h(city)} data centers on the live facility map →</a></p>
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
  <p>DC Hub is the live infrastructure data layer for AI agents — and for the people who build data centers: live power, grid, fiber, gas, tenants &amp; site scores on {_h(city)}, cited and machine-readable. Plans from $9/mo · full market &amp; grid intelligence from $49/mo.</p>
  <a href="/pricing?ref=market&tool={_esc_attr(slug)}" class="cta">See plans — from $49/mo</a>
  <a href="/signup?from=market-{_esc_attr(slug)}" class="cta secondary">Or: free MCP API access</a>
</section>"""

    return _base_html(
        title=title, description=desc, canonical=canonical,
        og_image=f"https://dchub.cloud/static/og/market-{slug}.png",
        schema_jsonld=schema, body_html=body,
        extra_footer_html=_onramp_footer_html("market", slug),
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
# SITEMAPS — REMOVED (r-sitemap-shard 2026-07-03)
# ─────────────────────────────────────────────────────────────────────
# The five legacy sub-sitemaps that lived here (/sitemap-index.xml,
# /sitemap-facilities.xml, /sitemap-markets.xml, /sitemap-grids.xml,
# /sitemap-landings.xml) were retired: index/facilities/grids were already
# 410 at the CF worker (facilities emitted the 5,772 /facility/<id> dupes
# that blocked Google AND Bing indexing), landings was edge-unreachable,
# and markets is now served by the canonical sharded sitemap in main.py
# (serve_sitemap_shard — /sitemap.xml is a sitemapindex; the DB-driven
# /markets/<city-state> query moved there, the brief/answers landings
# moved into its static section). One sitemap system owns all shards.
# ═════════════════════════════════════════════════════════════════════


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

    body = canon_text("""<header class="dc-seo">
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
  <p>DC Hub is a data center intelligence platform tracking {canon_facilities} facilities, 7 ISO grids, and {canon_deals} M&amp;A deals. Our DCHubBot publishes daily market signals to Moltbook. Free MCP API for AI agents.</p>
  <a href="/signup?from=moltbook-docs" class="cta">Get free DC Hub MCP API key</a>
  <a href="https://www.moltbook.com" class="cta secondary" rel="nofollow noopener" target="_blank">Visit Moltbook &rarr;</a>
</section>""")

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
<section><a href="/" class="cta">Back to DC Hub home</a>
  <a href="/markets/directory" class="cta secondary">Browse all markets</a>
  <a href="/facilities" class="cta secondary">Browse all facilities</a></section>"""
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
                "/partners/moltbook-api"],
        legacy_redirects=["/aws/<code> -> /facility/aws-<code>",
                          "/address/<slug> -> /facility/<slug>",
                          "/interxion-frankfurt -> /markets/interxion-frankfurt",
                          "/moltbook-api-documentation -> /partners/moltbook-api"],
        db_ok=db_ok,
        iso_count=len(ISO_REGISTRY),
        aws_codes=list(AWS_REGION_MAP.keys()),
        addresses=list(ADDRESS_MAP.keys()),
    )


# ── FACILITIES DIRECTORY — server-rendered crawlable index (2026-06-29) ──
# Fixes "discovered – not indexed": the /facilities hub is a JS UI with 0
# server-rendered links, so the ~4,833 real (non-duplicate) facility pages were
# orphans Google deprioritized. This paginated directory links every non-dup
# facility via its CANONICAL /facilities/<slug> URL (the same form the sitemap +
# page canonical use), with full all-pages nav so Googlebot can reach the whole
# set from page 1. Added to the sitemap; linked from the facility breadcrumb.
import re as _re_dir
from routes.facility_slug import stable_hash8 as _stable_hash8
from ai_surface_canon import canon_text
_CANON_FAC = canon_text("{canon_facilities}")
# ★2026-08-23 — the footer and the Moltbook "About DC Hub" blurb both typed
# "4,000+ M&A deals": a floor over DUPLICATE deal ROWS (the AUTO id embeds the
# ingest date, so one deal accrues a row per day) against ~1,900 distinct, and
# a value already listed in ai_surface_canon.PINNED["stale_markers"]. Bind the
# canon phrase, never a number.
_CANON_DEALS = canon_text("{canon_deals}")

_DIR_PER_PAGE = 1000


def _fac_slugify(t):
    s = (t or "").lower().strip()
    s = _re_dir.sub("[^a-z0-9 -]", "", s)
    s = _re_dir.sub("[- ]+", "-", s)
    return s.strip("-")


def _facility_canonical_slug(provider, name):
    # r-lane5 (2026-07-31): delegate to the freeze builder (see
    # _canonical_facility_slug) — this twin composed the pre-dedupe form for
    # unfrozen directory rows, the same drift by a second copy.
    from routes.facility_slug_freeze import build_canonical_slug
    return build_canonical_slug(provider, name)


@seo_pages_bp.get("/facilities/directory", strict_slashes=False)
@seo_pages_bp.get("/facilities/directory/<int:page>", strict_slashes=False)
def facilities_directory(page: int = 1):
    page = max(1, int(page or 1))
    c = _conn()
    if c is None:
        return _error_page("Database temporarily unavailable", 503)
    total = 0
    rows = []
    try:
        import psycopg2.extras
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM discovered_facilities "
                        "WHERE name IS NOT NULL AND name <> '' "
                        "  AND COALESCE(is_duplicate,0) = 0")
            total = int((cur.fetchone() or [0])[0] or 0)
            cur.execute("""
                SELECT name, provider, city, state, country, canonical_slug
                  FROM discovered_facilities
                 WHERE name IS NOT NULL AND name <> ''
                   AND COALESCE(is_duplicate,0) = 0
                 ORDER BY power_mw DESC NULLS LAST, name ASC
                 LIMIT %s OFFSET %s
            """, (_DIR_PER_PAGE, (page - 1) * _DIR_PER_PAGE))
            rows = cur.fetchall()
    except Exception:
        return _error_page("Directory temporarily unavailable", 503)
    finally:
        try: c.close()
        except Exception: pass

    pages = max(1, (total + _DIR_PER_PAGE - 1) // _DIR_PER_PAGE)
    if page > pages:
        return _error_page("Page out of range", 404)
    items = []
    for name, provider, city, state, country, canonical_slug in rows:
        # r-frozen-slug (2026-07-15): serve the STORED canonical_slug (frozen,
        # immutable). Recomputing from live provider/name drifts the URL the
        # moment either string is cleaned on re-ingestion → a self-inflicted
        # 301 (the exact recurrence this closes). Fall back to a live compute
        # only for rows not yet frozen (canonical_slug NULL); the daily
        # slug-freeze cron backfills those set-once so the link converges.
        slug = (canonical_slug or "").strip() or _facility_canonical_slug(provider, name)
        if not slug:
            continue
        loc = ", ".join([x for x in (city, state, country) if x])
        prov = (provider or "").strip()
        meta = " · ".join([x for x in (prov, loc) if x and x.lower() != "unknown"])
        items.append(f'<li><a href="/facilities/{slug}">{_h(name)}</a>'
                     f'{(" — " + _h(meta)) if meta else ""}</li>')
    nav = []
    if page > 1:
        nav.append(f'<a href="/facilities/directory/{page-1}">← Prev</a>')
    if page < pages:
        nav.append(f'<a href="/facilities/directory/{page+1}">Next →</a>')
    allp = " · ".join(
        f'<a href="/facilities/directory/{p}">{p}</a>' if p != page else f"<strong>{p}</strong>"
        for p in range(1, pages + 1))
    canon = "https://dchub.cloud/facilities/directory" + (f"/{page}" if page > 1 else "")
    html_out = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Data Center Facilities Directory — page {page} of {pages} | DC Hub</title>
<meta name="description" content="Browse {total:,} data center facilities across 170+ countries — operator, location and capacity, each with a live DC Hub profile (grid, fiber, DCPI verdict).">
<link rel="canonical" href="{canon}">
</head><body>
<nav class="breadcrumb"><a href="/">DC Hub</a> · <a href="/facilities">Facilities</a> · Directory (page {page}/{pages})</nav>
<h1>Data Center Facilities Directory</h1>
<p>{total:,} tracked data center facilities — page {page} of {pages}. Each links to a live DC Hub profile with grid, fiber, power and DCPI data.</p>
<ul class="facility-list">{''.join(items)}</ul>
<nav class="pager">{' '.join(nav)}</nav>
<p style="font-size:.8rem;color:#64748b">All pages: {allp}</p>
</body></html>"""
    return Response(html_out, mimetype="text/html; charset=utf-8")


# ── MARKETS DIRECTORY — crawlable hub for /markets/<slug> (2026-06-29) ──
# /markets is a DEAD crawl path (the SPA emits the literal un-interpolated
# href="/markets/${slug}" token → crawlers reach ZERO market pages), yet the
# 100+ per-market roll-ups render fine server-side at /markets/<slug>. This
# server-rendered, paginated index links every market via its canonical URL —
# the markets twin of /facilities/directory. Added to the sitemap.
_MKT_DIR_PER_PAGE = 100

# r-directory-split-groups (2026-07-31), the /markets/directory twin of #2074.
#
# The slug IS this page's unit of identity — every listing is a link to
# /markets/<slug> — but the query built the slug from normalised columns and
# then grouped on the RAW ones. Case variants of one city therefore each got
# their own listing, so the public directory published TWO rows carrying the
# SAME href, the smaller one reading as an empty market. Measured on the read
# replica: 82 duplicate listings over 81 slugs, out of 2,438; 70 of the 81 had
# a sibling listing showing 0 MW.
#
#   ashburn-va   'Ashburn'/VA n=199 mw=6942  next to  'ASHBURN'/VA n=5 mw=0
#   sterling-va  'Sterling'/VA n=119 mw=2902 next to  'STERLING'/VA n=1 mw=0
#   dallas-tx, chicago-il, manassas-va, atlanta-ga, new-york-ny, las-vegas-nv:
#   all the same shape.
#
# Every split was pure letter-case: 67 city-case, 14 state-case, no other
# cause. This route already excludes blank and NULL state, so it needs none of
# #2074's conditional blank-state fold and none of its homonym guard — there is
# no arm of that fold to get wrong here.
#
# ★ Group on the slug EXPRESSION rather than on a normalised (city, state)
#   pair. Both erase the 82 duplicates, but a pair still has to be rendered
#   back into a slug, and that trip can re-collide: keying on
#   (LOWER(TRIM(city)), UPPER(TRIM(state))) sends 'Bakırköy' through
#   LOWER(UPPER(...)), which folds Turkish dotless ı to i and silently moves a
#   LIVE indexed URL (istanbul-bakırköy -> istanbul-bakirköy). Grouping on the
#   emitted expression makes one-row-per-slug true by construction, and changes
#   the URL of exactly ZERO facility rows. TRIM likewise moves nothing today
#   (0 rows differ); it is there so a whitespace variant cannot reopen the
#   split later.
#
# Unlike the preview this route emits a LIST, so the fix is a straight dedupe
# of the grouping key — nothing is picked and nothing is discarded. Totals are
# conserved exactly: 8,204 facilities and 128,419 MW before and after, no slug
# gained, none lost, no listing down a facility.
_MKT_SLUG_SQL = "LOWER(REPLACE(TRIM(city),' ','-')) || '-' || LOWER(TRIM(state))"

# r-directory-real-slugs (2026-08-26): the two halves above made the directory
# publish ONE row per market. They did not make the row's href resolve.
#
# The slug built above is a city+state STRING, not a market that exists. The
# route emitted one for every city/state group in discovered_facilities with a
# facility in it and never checked that a page was ever built at that path. The
# sitemap answers a different question entirely — bare city/metro slugs joined
# to market_power_scores — so the two namespaces did not overlap at all.
#
# MEASURED LIVE 2026-08-26, 609 of the 2,362 published links fetched across 8
# of the 24 directory pages:
#     404  446  (73%)   ->  ~1,729 dead links published site-wide
#     301  160  (26%)   ->  ~620 redirect hops, some two deep
#     200    3  (0.5%)  ->  ~11 links that land on a real page
# and 0 of 315 sampled hrefs appeared in sitemap-markets.xml. For contrast,
# /facilities/directory was 80/80 200 and sitemap-markets.xml 249/249 200.
#
# ★ THIS IS WHY THE Not-found BUCKET REFILLS. These are not stale index entries
#   that a validation pass can clear — /markets/directory is in the sitemap at
#   priority 0.8/weekly, so every crawl rediscovers all 2,362 dead ends. The
#   same class was fixed for the SITEMAP on 2026-07-28 (see
#   tests/test_market_link_validation.py::test_sitemap_only_lists_markets_that_exist,
#   which requires the JOIN); the directory route was simply never covered by it.
#
# The fix is the sitemap's: JOIN a real market table. Resolution mirrors
# _market_slug_for() — prefer the city+state slug when a market carries it,
# else the bare city slug, else emit nothing — and the GROUP BY moves onto the
# RESOLVED slug so the several city groups that map to one market (Ashburn ->
# northern-virginia) merge instead of publishing duplicate hrefs. That keeps
# the one-row-per-href invariant the shape fence exists to protect.
_MKT_CITY_SLUG_SQL = "LOWER(REPLACE(TRIM(city),' ','-'))"
_MKT_RESOLVED_SLUG_SQL = "COALESCE(mc.market_slug, mk.market_slug)"


@seo_pages_bp.get("/markets/directory", strict_slashes=False)
@seo_pages_bp.get("/markets/directory/<int:page>", strict_slashes=False)
def markets_directory(page: int = 1):
    page = max(1, int(page or 1))
    c = _conn()
    if c is None:
        return _error_page("Database temporarily unavailable", 503)
    markets = []
    try:
        with c.cursor() as cur:
            # City is reported as the most common spelling in the group that is
            # not all-caps, so merging leaves the page reading 'Ashburn', not
            # 'ASHBURN'. That matters on 9 listings where the shouty spelling is
            # the more common one (BOYDTON, LAKEWOOD, HOFFMAN ESTATES...); a
            # plain MODE() would have merged them DOWN to the shouty label the
            # page renders today alongside the good one. The COALESCE carries
            # the 49 groups that have no other spelling to fall back to. State
            # takes a plain MODE() — states are conventionally upper-case and
            # the caps-avoiding form changed 0 of them.
            # mkt is DISTINCT so neither LEFT JOIN can fan a facility row out
            # into several and inflate COUNT(*)/SUM(power_mw).
            cur.execute(f"""
                WITH mkt AS (
                    SELECT DISTINCT market_slug
                      FROM market_power_scores
                     WHERE market_slug IS NOT NULL AND market_slug <> ''
                ),
                fac AS (
                    SELECT city, state, power_mw,
                           {_MKT_SLUG_SQL}      AS combo_slug,
                           {_MKT_CITY_SLUG_SQL} AS city_slug
                      FROM discovered_facilities
                     WHERE city IS NOT NULL AND city <> ''
                       AND state IS NOT NULL AND state <> ''
                       AND COALESCE(is_duplicate,0) = 0
                )
                SELECT {_MKT_RESOLVED_SLUG_SQL} AS slug,
                       COALESCE(
                           MODE() WITHIN GROUP (ORDER BY NULLIF(city, UPPER(city))),
                           MODE() WITHIN GROUP (ORDER BY city))  AS city,
                       MODE() WITHIN GROUP (ORDER BY state)      AS state,
                       COUNT(*) AS n_fac, COALESCE(SUM(power_mw),0) AS total_mw
                  FROM fac
                  LEFT JOIN mkt mc ON mc.market_slug = fac.combo_slug
                  LEFT JOIN mkt mk ON mk.market_slug = fac.city_slug
                 WHERE {_MKT_RESOLVED_SLUG_SQL} IS NOT NULL
                 GROUP BY {_MKT_RESOLVED_SLUG_SQL}
                HAVING COUNT(*) >= 1
                 ORDER BY total_mw DESC NULLS LAST, COUNT(*) DESC
            """)
            markets = cur.fetchall()
    except Exception:
        return _error_page("Directory temporarily unavailable", 503)
    finally:
        try: c.close()
        except Exception: pass

    # Drop empty/trailing-hyphen slugs (thin-page guard, mirrors sitemap).
    markets = [m for m in markets if m[0] and not m[0].startswith('-') and not m[0].endswith('-') and len(m[0]) > 2]
    total = len(markets)
    pages = max(1, (total + _MKT_DIR_PER_PAGE - 1) // _MKT_DIR_PER_PAGE)
    if page > pages:
        return _error_page("Page out of range", 404)
    chunk = markets[(page - 1) * _MKT_DIR_PER_PAGE: page * _MKT_DIR_PER_PAGE]
    items = []
    for slug, city, state, n_fac, total_mw in chunk:
        mw = f"{int(total_mw):,} MW" if total_mw else ""
        meta = " · ".join([x for x in (f"{n_fac} facilities", mw) if x])
        items.append(f'<li><a href="/markets/{slug}">{_h(city)}, {_h(state)}</a>'
                     f'{(" — " + _h(meta)) if meta else ""}</li>')
    nav = []
    if page > 1:
        nav.append(f'<a href="/markets/directory/{page-1}">← Prev</a>')
    if page < pages:
        nav.append(f'<a href="/markets/directory/{page+1}">Next →</a>')
    allp = " · ".join(
        f'<a href="/markets/directory/{p}">{p}</a>' if p != page else f"<strong>{p}</strong>"
        for p in range(1, pages + 1))
    canon = "https://dchub.cloud/markets/directory" + (f"/{page}" if page > 1 else "")
    rel = ""
    if page > 1:
        rel += f'<link rel="prev" href="https://dchub.cloud/markets/directory{("/"+str(page-1)) if page>2 else ""}">'
    if page < pages:
        rel += f'<link rel="next" href="https://dchub.cloud/markets/directory/{page+1}">'
    html_out = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Data Center Markets Directory — page {page} of {pages} | DC Hub</title>
<meta name="description" content="Browse {total:,} data center markets — facility count, total MW capacity and live DCPI verdict, each with a live DC Hub market roll-up.">
<link rel="canonical" href="{canon}">{rel}
</head><body>
<nav class="breadcrumb"><a href="/">DC Hub</a> · <a href="/markets">Markets</a> · Directory (page {page}/{pages})</nav>
<h1>Data Center Markets Directory</h1>
<p>{total:,} tracked data center markets — page {page} of {pages}. Each links to a live DC Hub market roll-up (supply, pricing, operators, DCPI verdict).</p>
<ul class="facility-list">{''.join(items)}</ul>
<nav class="pager">{' '.join(nav)}</nav>
<p style="font-size:.8rem;color:#64748b">All pages: {allp}</p>
</body></html>"""
    return Response(html_out, mimetype="text/html; charset=utf-8")
