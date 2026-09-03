"""SEO facilities hub (2026-06-29).

Fixes /facilities (was 503 / backend-404) and un-orphans the ~13.8K
/facilities/<slug> pages Google had "Discovered – currently not indexed":
they were only in the sitemap, with no crawlable hub and 0 homepage links.

Geography hierarchy — path-based (robots.txt disallows ?params):
  /facilities                          → countries index (by facility count)
  /facilities/in/<country>             → facilities in a country, grouped by market
  /facilities/in/<country>/page/<n>    → path-based pagination (no ?params)
  /facilities/in/us/<state>            → US per-state pages (r-seo-0801)
  /facilities/in/us/<state>/page/<n>   → state pagination

Links are built with the SAME canonical slug the sitemap + live facility pages
use (discovered_facilities + slugify + stable_hash8) so every link resolves 200.
Cached in-process (1h) to spare the 1-replica backend from crawler hammering.

r-seo-0801 (SEO diagnosis 2026-08-01): these pages get real Google clicks but
were naked — 32% of top organic entries landed here with ZERO money path. This
wave adds: Pricing in the nav + the facility-profile onramp CTA (money path),
BreadcrumbList + ItemList JSON-LD (the ranking page type had no schema at all),
validated /markets + /dcpi links on the market group headers (never a 404
link), US per-state pages (/facilities/in/us had 4,779 facilities behind a
24-hop single-Next chain), and numbered pagination.
"""
import json
import re
import html as _html
from flask import Blueprint, Response, redirect, request

from routes.facility_slug import stable_hash8

facilities_hub_bp = Blueprint("facilities_hub", __name__)

SITE = "https://dchub.cloud"
PAGE_SIZE = 200
_CACHE: dict = {}          # path -> (xml/html, ts)
_CACHE_TTL = 3600

# ISO-2 (and a few common variants) → display name. Fallback = code as-is.
_COUNTRY_NAMES = {
    "US": "United States", "GB": "United Kingdom", "DE": "Germany",
    "FR": "France", "JP": "Japan", "CA": "Canada", "NL": "Netherlands",
    "AU": "Australia", "SG": "Singapore", "IN": "India", "BR": "Brazil",
    "CN": "China", "IE": "Ireland", "SE": "Sweden", "IT": "Italy",
    "ES": "Spain", "CH": "Switzerland", "ZA": "South Africa", "MX": "Mexico",
    "PL": "Poland", "BE": "Belgium", "FI": "Finland", "NO": "Norway",
    "DK": "Denmark", "AT": "Austria", "HK": "Hong Kong", "KR": "South Korea",
    "AE": "United Arab Emirates", "ID": "Indonesia", "MY": "Malaysia",
    "TH": "Thailand", "NZ": "New Zealand", "RU": "Russia", "TR": "Turkey",
    "PT": "Portugal", "CZ": "Czechia", "RO": "Romania", "GR": "Greece",
    "IL": "Israel", "AR": "Argentina", "CL": "Chile", "CO": "Colombia",
    "SA": "Saudi Arabia", "TW": "Taiwan", "VN": "Vietnam", "PH": "Philippines",
}

# US state code → display name (50 states + DC + PR). The state column stores
# either the 2-letter code or the full name depending on the ingest source, so
# both forms resolve. Canonical URL uses the full-name slug
# (/facilities/in/us/virginia), matching the "Data Centers in Virginia (N)"
# title format that wins these SERPs.
_US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia", "PR": "Puerto Rico",
}


def _country_name(code: str) -> str:
    c = (code or "").strip()
    return _COUNTRY_NAMES.get(c.upper(), c.upper() if len(c) <= 3 else c)


def _slugify(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"[-\s]+", "-", s).strip("-")


# slug ('virginia') → (code, name); built once from _US_STATES.
_US_STATE_BY_SLUG = {_slugify(v): (k, v) for k, v in _US_STATES.items()}


def us_state_slug(value):
    """Canonical state-page slug for a raw `state` column value, or None.
    Accepts the 2-letter code ('VA') or the full name ('Virginia')."""
    v = (value or "").strip() if isinstance(value, str) else ""
    if not v:
        return None
    if len(v) == 2 and v.upper() in _US_STATES:
        return _slugify(_US_STATES[v.upper()])
    s = _slugify(v)
    return s if s in _US_STATE_BY_SLUG else None


def _fac_slug(provider, name):
    """Canonical facility slug — byte-identical to the sitemap + live pages."""
    name_slug = _slugify(name)
    if len(name_slug) < 3:
        return None
    provider_slug = _slugify(provider)
    h = stable_hash8(provider, name)
    return f"{provider_slug}-{name_slug}-{h}" if provider_slug else f"{name_slug}-{h}"


def _conn():
    from db_utils import get_read_db
    return get_read_db()


def _e(s) -> str:
    return _html.escape(str(s if s is not None else ""))


# ── money path (r-seo-0801) ────────────────────────────────────────────
def _cta_html():
    """The facility-profile onramp CTA (routes/facility_profile_page.py), on
    the hub/country/state pages too — these are 32% of top organic entries
    and had zero path to /pricing."""
    try:
        from ai_surface_canon import PINNED as _CANON
        n = _CANON["public"]["facilities"]
    except Exception:
        # This handler exists for "ai_surface_canon is unreadable", so it must
        # not reach back into that module for a value. Degrade to a COUNT-FREE
        # string — canon_text's own contract: never a wrong number, only a
        # missing one.
        n = ""
    return (
        f'<div class="cta">'
        f'<a class="primary" href="{SITE}/pricing">Get all {_e(n)} facilities + power scores '
        f'&amp; site-selection tools &mdash; DC Hub from $49/mo &rarr;</a>'
        f'<a href="{SITE}/ai">Free MCP key (AI agents)</a>'
        f'</div>'
    )


# ── validated market-group interlinks (r-seo-0801) ─────────────────────
# The h2 market groupings were plain text; link them to /markets/<metro> and
# /dcpi/<city> — but ONLY where the slug actually resolves. Reuses the
# validated-resolver slug set from routes/seo_pages.py (one TTL-cached query,
# the same set the facility pages' market links are validated against). A hub
# page must never link to its own 404, so this fails CLOSED (plain heading)
# when the set is unavailable — unlike the facility pages' fail-open, there is
# no legacy link shape to preserve here.
#
# Slug vocab (two families, a recurring 404 source): /markets is METRO-keyed,
# /dcpi is CITY-keyed. _MARKETS_METRO_CANON mirrors
# market_deep_dive._CANONICAL_REDIRECT (which lives inside a view function, so
# it cannot be imported) to link the metro directly instead of bouncing
# through its 301.
_MARKETS_METRO_CANON = {
    "ashburn": "northern-virginia",
    "nova": "northern-virginia",
    "dfw": "dallas",
}


def _group_links(grp):
    """(markets_href, dcpi_href) for a market-group heading — either may be None."""
    try:
        from routes.seo_pages import _valid_market_slugs
        from util.market_aliases import canonical_slug, REDUNDANT_TWIN_SLUGS
    except Exception:
        return None, None
    try:
        known = _valid_market_slugs()
    except Exception:
        known = None
    if not known:
        return None, None
    g = _slugify(grp)
    if not g or g == "other":
        return None, None
    # /dcpi: alias twins (northern-virginia, dallas-fort-worth, …) are
    # unpublished rows — canonicalize to the published city slug first.
    d = canonical_slug(g) or g
    dcpi_href = (f"{SITE}/dcpi/{d}"
                 if d in known and d not in REDUNDANT_TWIN_SLUGS else None)
    # /markets: the metro form serves directly; the city form of the big three
    # 301s, so emit the metro. Valid when either slug form is in the set.
    metro = _MARKETS_METRO_CANON.get(g, g)
    markets_href = (f"{SITE}/markets/{metro}"
                    if (g in known or d in known) else None)
    return markets_href, dcpi_href


def _group_heading(grp):
    mhref, dhref = _group_links(grp)
    head = f'<a href="{mhref}">{_e(grp)}</a>' if mhref else _e(grp)
    if dhref:
        head += f' <a class="h2x" href="{dhref}">Power Index &rarr;</a>'
    return f"<h2>{head}</h2>"


# ── JSON-LD (r-seo-0801) ───────────────────────────────────────────────
# The site has 243 breadcrumb + 219 dataset rich results, but the RANKING page
# type (this hub) had zero schema. Pattern from routes/facility_profile_page.py.
def _ld_breadcrumb(crumbs):
    """crumbs = [(name, url), ...] → BreadcrumbList."""
    return {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": n, "item": u}
            for i, (n, u) in enumerate(crumbs)
        ],
    }


def _ld_itemlist(name, entries):
    """entries = [(name, url), ...] → ItemList of the links already rendered."""
    return {
        "@context": "https://schema.org", "@type": "ItemList", "name": name,
        "numberOfItems": len(entries),
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": n, "url": u}
            for i, (n, u) in enumerate(entries)
        ],
    }


def _shell(title, desc, canonical, breadcrumb_html, body_html, jsonld=None):
    # Dark DC Hub brand — matches the facility profile pages
    # (routes/facility_profile_page.py): Instrument Sans + JetBrains Mono,
    # #0a0a0f background, indigo→violet gradient wordmark.
    ld_html = "".join(
        '<script type="application/ld+json">'
        + json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
        + "</script>"
        for o in (jsonld or [])
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<meta name="description" content="{_e(desc)}">
<link rel="canonical" href="{canonical}">
<meta name="robots" content="index, follow">
<meta property="og:title" content="{_e(title)}">
<meta property="og:description" content="{_e(desc)}">
<meta property="og:url" content="{canonical}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dchub-brand.css">
{ld_html}
<style>
:root{{--bg:#0a0a0f;--surf:#131319;--surf2:#1a1a22;--b:rgba(255,255,255,0.08);--tx:#fafafa;--mut:#a1a1aa;--dim:#71717a;--ind:#818cf8;--indd:#6366f1;--vio:#a855f7;--grad:linear-gradient(135deg,#6366f1,#a855f7)}}
*{{box-sizing:border-box}}
body{{font-family:'Instrument Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--tx);line-height:1.6;-webkit-font-smoothing:antialiased;margin:0}}
.header{{border-bottom:1px solid var(--b);padding:14px 0;position:sticky;top:0;background:rgba(10,10,15,.85);backdrop-filter:blur(10px);z-index:10}}
.wrap{{max-width:1100px;margin:0 auto;padding:0 24px}}
.logo{{font-weight:700;font-size:18px;letter-spacing:-.01em;text-decoration:none;color:var(--tx)}}
.logo span{{background:var(--grad);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}}
.header nav a{{color:var(--mut);text-decoration:none;font-size:14px;margin-left:18px}}
.header nav a:hover{{color:var(--tx)}}
.header nav a.px{{color:var(--ind);font-weight:600}}
main{{max-width:1100px;margin:0 auto;padding:24px}}
a{{color:var(--ind);text-decoration:none}}a:hover{{text-decoration:underline}}
nav.bc{{font-size:12px;color:var(--dim);margin-bottom:18px;font-family:'JetBrains Mono',monospace}}
nav.bc a{{color:var(--dim)}}nav.bc a:hover{{color:var(--mut)}}
h1{{font-size:30px;font-weight:700;letter-spacing:-.02em;margin:.1em 0 .3em}}
h2{{font-size:16px;font-weight:600;margin:1.6em 0 .5em;color:var(--tx);border-bottom:1px solid var(--b);padding-bottom:6px}}
h2 a{{color:var(--tx)}}h2 a:hover{{color:var(--ind);text-decoration:none}}
h2 a.h2x{{color:var(--ind);font-weight:500;font-size:12px;margin-left:10px;font-family:'JetBrains Mono',monospace}}
ul.grid{{list-style:none;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px 28px}}
ul.grid li{{padding:2px 0}}
.muted{{color:var(--mut);font-size:14px}}
.cta{{background:linear-gradient(135deg,rgba(99,102,241,0.12),rgba(168,85,247,0.06));border:1px solid rgba(99,102,241,0.25);border-radius:16px;padding:22px 24px;margin:18px 0;display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;justify-content:center;text-align:center}}
.cta a{{color:var(--ind);text-decoration:none;font-weight:600;font-size:14px}}
.cta .primary{{background:var(--grad);color:#fff;padding:10px 18px;border-radius:9px}}
nav.pager{{margin:26px 0 8px;font-size:14px}}
nav.pager a,nav.pager span.cur{{display:inline-block;padding:2px 9px;margin:0 2px;border-radius:6px}}
nav.pager span.cur{{background:var(--surf2);color:var(--tx)}}
footer{{max-width:1100px;margin:48px auto 0;padding:20px 24px 40px;border-top:1px solid var(--b);font-size:14px;color:var(--dim)}}
footer a{{color:var(--mut)}}
</style>
</head><body>
<!-- canonical nav is injected by /js/dchub-nav.js, loaded before </body> -->
<main>
<nav class="bc">{breadcrumb_html}</nav>
{body_html}
</main>
<footer>
<a href="{SITE}/">DC Hub</a> · <a href="{SITE}/facilities">All facilities</a> ·
<a href="{SITE}/facilities/directory">Facility Directory</a> ·
<a href="{SITE}/dcpi">DC Hub Power Index</a> · <a href="{SITE}/markets">Markets</a> ·
<a href="{SITE}/grid">Grid</a> · <a href="{SITE}/pricing">Pricing</a>
<div class="muted" style="margin-top:8px;color:var(--dim)">DC Hub — live data-center infrastructure intelligence across 170+ countries.</div>
</footer>
<script src="/js/dchub-nav.js" defer></script>
</body></html>"""


def _respond(path_key, html_str, status=200):
    resp = Response(html_str, status=status, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    resp.headers["x-dc-hub-source"] = "facilities-hub"
    return resp


def _cached(path_key):
    import time
    hit = _CACHE.get(path_key)
    if hit and (time.time() - hit[1]) < _CACHE_TTL:
        return hit[0]
    return None


def _store(path_key, html_str):
    import time
    _CACHE[path_key] = (html_str, time.time())
    return html_str


# ── shared listing renderer (country + state pages) ────────────────────
def _rows_to_facs(rows):
    """(grp, slug, name, loc) for every facility row with a valid slug."""
    facs = []
    for name, provider, grp, city, state, _power in rows:
        slug = _fac_slug(provider, name)
        if not slug:
            continue
        loc = ", ".join([x for x in (city, state) if x and str(x).strip()])
        facs.append((grp or "Other", slug, name, loc))
    return facs


def _pager_html(base, page, pages):
    """Numbered path-based pager (no ?params — robots disallows them).
    Was single-Next only: /facilities/in/us put facility #4,779 behind a
    24-hop chain Google will not walk. Every page is now ≤1 hop away."""
    if pages <= 1:
        return ""
    nums = []
    for p in range(1, pages + 1):
        if p == page:
            nums.append(f'<span class="cur">{p}</span>')
        else:
            href = base if p == 1 else f"{base}/page/{p}"
            nums.append(f'<a href="{href}">{p}</a>')
    return (f'<nav class="pager"><span class="muted">Page {page} of {pages}:</span> '
            f'{"".join(nums)}</nav>')


def _render_listing(ck, facs, page, base, place_name, crumbs, extra_block=""):
    """Grouped facility listing page (country + US state views).
    crumbs = [(name, url), ...] ending with this page's own entry."""
    total = len(facs)
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = min(page, pages)
    start = (page - 1) * PAGE_SIZE
    chunk = facs[start:start + PAGE_SIZE]

    # Group this page's slice by market heading (validated interlinks).
    out, cur_grp = [], None
    for grp, slug, name, loc in chunk:
        if grp != cur_grp:
            if cur_grp is not None:
                out.append("</ul>")
            out.append(f'{_group_heading(grp)}<ul class="grid">')
            cur_grp = grp
        loc_s = f' <span class="muted">— {_e(loc)}</span>' if loc else ""
        out.append(f'<li><a href="{SITE}/facilities/{_e(slug)}">{_e(name)}</a>{loc_s}</li>')
    if cur_grp is not None:
        out.append("</ul>")

    body = (
        f"<h1>Data Centers in {_e(place_name)}</h1>"
        f'<p class="muted">{total:,} tracked data-center facilities in {_e(place_name)}, '
        f"grouped by market. Each links to a full profile with power, location and "
        f"operator detail.</p>"
        f"{_cta_html()}"
        f"{extra_block}"
        f'{"".join(out)}{_pager_html(base, page, pages)}'
    )
    canonical = base if page == 1 else f"{base}/page/{page}"
    # Count-in-title (datacentermap's "N Facilities from M Operators" format
    # wins these SERPs); page 2+ gets a distinct title, same h1.
    title = f"Data Centers in {place_name} ({total:,}) | DC Hub"
    if page > 1:
        title = f"Data Centers in {place_name} ({total:,}) — Page {page} | DC Hub"
    bc_html = " › ".join(
        [f'<a href="{u}">{_e(n)}</a>' for n, u in crumbs[:-1]] + [_e(crumbs[-1][0])]
    )
    jsonld = [
        _ld_breadcrumb(crumbs),
        _ld_itemlist(f"Data centers in {place_name}",
                     [(n, f"{SITE}/facilities/{s}") for _g, s, n, _l in chunk]),
    ]
    page_html = _shell(
        title,
        f"All {total:,} tracked data-center facilities in {place_name}, by market — "
        "power, location and operator detail. DC Hub.",
        canonical, bc_html, body, jsonld=jsonld,
    )
    return _respond(ck, _store(ck, page_html))


# ── /facilities — countries index ─────────────────────────────────────
@facilities_hub_bp.route("/facilities")
@facilities_hub_bp.route("/facilities/")
def facilities_index():
    ck = "/facilities"
    cached = _cached(ck)
    if cached:
        return _respond(ck, cached)
    rows = []
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT country, COUNT(*) AS n
              FROM discovered_facilities
             WHERE name IS NOT NULL AND name <> '' AND char_length(name) >= 3
               AND country IS NOT NULL AND btrim(country) <> ''
               AND duplicate_of_id IS NULL
             GROUP BY country
             ORDER BY n DESC, country
        """)
        rows = cur.fetchall() or []
        conn.close()
    except Exception:
        rows = []
    total = sum(int(n or 0) for _, n in rows)
    items, ld_entries = [], []
    for code, n in rows:
        nm = _country_name(code)
        url = f"{SITE}/facilities/in/{str(code).lower()}"
        ld_entries.append((f"Data centers in {nm}", url))
        items.append(
            f'<li><a href="{_e(url)}">{_e(nm)}</a>'
            f' <span class="muted">({int(n or 0):,})</span></li>'
        )
    body = (
        f"<h1>Data Center Facilities by Country</h1>"
        f'<p class="muted">{total:,} tracked data-center facilities across '
        f"{len(rows)} countries. Browse by country, then by market, to every "
        f"facility profile.</p>"
        f"{_cta_html()}"
        f"<h2>Browse by country</h2>"
        f'<ul class="grid">{"".join(items)}</ul>'
    )
    crumbs = [("Home", f"{SITE}/"), ("Facilities", f"{SITE}/facilities")]
    jsonld = [_ld_breadcrumb(crumbs),
              _ld_itemlist("Data center facilities by country", ld_entries)]
    page = _shell(
        "Data Center Facilities by Country | DC Hub",
        f"Browse {total:,} data-center facilities across {len(rows)} countries — "
        "by country and market, with power, location and operator detail. DC Hub.",
        f"{SITE}/facilities",
        f'<a href="{SITE}/">Home</a> › Facilities',
        body,
        jsonld=jsonld,
    )
    return _respond(ck, _store(ck, page))


# ── /facilities/in/<country> — facilities in a country ─────────────────
@facilities_hub_bp.route("/facilities/in/<country>")
@facilities_hub_bp.route("/facilities/in/<country>/page/<int:page>")
def facilities_in_country(country, page=1):
    country = (country or "").strip().lower()
    page = max(1, int(page or 1))
    ck = f"/facilities/in/{country}/page/{page}"
    cached = _cached(ck)
    if cached:
        return _respond(ck, cached)

    rows = []
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT name, provider,
                   COALESCE(NULLIF(btrim(market),''), NULLIF(btrim(state),''),
                            NULLIF(btrim(city),''), 'Other') AS grp,
                   city, state, power_mw
              FROM discovered_facilities
             WHERE LOWER(btrim(country)) = %s
               AND name IS NOT NULL AND name <> ''
               AND duplicate_of_id IS NULL
             ORDER BY grp, name
        """, (country,))
        rows = cur.fetchall() or []
        conn.close()
    except Exception:
        rows = []

    facs = _rows_to_facs(rows)
    cname = _country_name(country)
    if not facs:
        # Honest 404 with onward links — the same rule the US-state branch
        # below already applies, and the same /markets lesson. This branch
        # answered 200 instead, which made an UNBOUNDED indexable space:
        # measured 2026-08-27, all 676 two-letter codes returned 200 with
        # `index, follow` and a self-canonical, and 498 of them held zero
        # facilities — 425 of those are not country codes at all
        # (/facilities/in/qz, /facilities/in/ww, /facilities/in/oa …), each
        # a ~14 KB near-identical shell. Same defect class as the /news/
        # digest that answered 200 for any slug (frontend #1255).
        #
        # ★ SAFE FOR INTERNAL LINKS. The only emitters of /facilities/in/<cc>
        #   are the facility page's country breadcrumb and its "Browse all
        #   data centers in X" link, and both use the facility's OWN country
        #   — which therefore has at least one row in this very query.
        #   Verified live over the 62 distinct country codes appearing across
        #   500 sampled facility pages: none pointed at an empty hub. The
        #   page also carries no cross-links to other country hubs (0 found
        #   across all 676), and no /facilities/in/ URL is in any sitemap.
        #
        # ★★ DELIBERATELY NOT _store()d. _cached() returns a body only and
        #    _respond() defaults to status=200, so a cached 404 body would be
        #    replayed as a 200 on the very next request and re-open the hole.
        body = (f"<h1>No data centers listed for “{_e(country.upper())}”</h1>"
                f'<p class="muted">DC Hub has no facility profiles under this '
                f'country code. Browse <a href="{SITE}/facilities">all countries</a> '
                f'or the <a href="{SITE}/dcpi">DC Hub Power Index</a>.</p>')
        page_html = _shell(
            f"No data centers listed for {country.upper()} | DC Hub",
            f"No data-center facilities listed under {country.upper()} — DC Hub.",
            f"{SITE}/facilities/in/{country}",
            f'<a href="{SITE}/">Home</a> › <a href="{SITE}/facilities">Facilities</a> › {_e(cname)}',
            body,
        )
        return _respond(ck, page_html, status=404)

    # US crawl depth (r-seo-0801): a "Browse by state" block linking the new
    # /facilities/in/us/<state> pages. Counted over the same slug-valid rows
    # the state pages themselves serve, so the (N)s agree.
    extra_block = ""
    if country == "us":
        st_counts: dict = {}
        for name, provider, _grp, _city, state, _power in rows:
            if not _fac_slug(provider, name):
                continue
            ss = us_state_slug(state)
            if ss:
                st_counts[ss] = st_counts.get(ss, 0) + 1
        if st_counts:
            st_items = "".join(
                f'<li><a href="{SITE}/facilities/in/us/{ss}">{_e(_US_STATE_BY_SLUG[ss][1])}</a>'
                f' <span class="muted">({st_counts[ss]:,})</span></li>'
                for ss in sorted(st_counts, key=lambda k: _US_STATE_BY_SLUG[k][1])
            )
            extra_block = (f"<h2>Browse by state</h2>"
                           f'<ul class="grid">{st_items}</ul>')

    crumbs = [("Home", f"{SITE}/"), ("Facilities", f"{SITE}/facilities"),
              (cname, f"{SITE}/facilities/in/{country}")]
    return _render_listing(ck, facs, page, f"{SITE}/facilities/in/{country}",
                           cname, crumbs, extra_block=extra_block)


# ── /facilities/in/us/<state> — US per-state pages (r-seo-0801) ─────────
@facilities_hub_bp.route("/facilities/in/us/<st>")
@facilities_hub_bp.route("/facilities/in/us/<st>/page/<int:page>")
def facilities_in_us_state(st, page=1):
    st = (st or "").strip().lower()
    page = max(1, int(page or 1))

    # 2-letter code → 301 to the canonical full-name slug (va → virginia).
    if len(st) == 2 and st.upper() in _US_STATES:
        target = _slugify(_US_STATES[st.upper()])
        suffix = f"/page/{page}" if page > 1 else ""
        return redirect(f"/facilities/in/us/{target}{suffix}", code=301)

    hit = _US_STATE_BY_SLUG.get(st)
    if not hit:
        # Honest 404 with onward links (never a soft-404 — the /markets lesson).
        body = (f"<h1>Unknown US state</h1>"
                f'<p class="muted">No such state page. Browse '
                f'<a href="{SITE}/facilities/in/us">all US data centers</a> or '
                f'<a href="{SITE}/facilities">all countries</a>.</p>')
        page_html = _shell(
            "Unknown US state | DC Hub", "Unknown US state — DC Hub.",
            f"{SITE}/facilities/in/us",
            f'<a href="{SITE}/">Home</a> › <a href="{SITE}/facilities">Facilities</a> › '
            f'<a href="{SITE}/facilities/in/us">United States</a>',
            body,
        )
        return _respond(f"/facilities/in/us/{st}", page_html, status=404)

    code, sname = hit
    ck = f"/facilities/in/us/{st}/page/{page}"
    cached = _cached(ck)
    if cached:
        return _respond(ck, cached)

    rows = []
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT name, provider,
                   COALESCE(NULLIF(btrim(market),''),
                            NULLIF(btrim(city),''), 'Other') AS grp,
                   city, state, power_mw
              FROM discovered_facilities
             WHERE LOWER(btrim(country)) = 'us'
               AND (UPPER(btrim(state)) = %s OR LOWER(btrim(state)) = %s)
               AND name IS NOT NULL AND name <> ''
               AND duplicate_of_id IS NULL
             ORDER BY grp, name
        """, (code, sname.lower()))
        rows = cur.fetchall() or []
        conn.close()
    except Exception:
        rows = []

    facs = _rows_to_facs(rows)
    if not facs:
        body = (f"<h1>Data Centers in {_e(sname)}</h1>"
                f'<p class="muted">No facility profiles found for this state yet. '
                f'Browse <a href="{SITE}/facilities/in/us">all US data centers</a> or the '
                f'<a href="{SITE}/dcpi">DC Hub Power Index</a>.</p>')
        page_html = _shell(
            f"Data Centers in {sname} | DC Hub",
            f"Data-center facilities in {sname} — DC Hub.",
            f"{SITE}/facilities/in/us/{st}",
            f'<a href="{SITE}/">Home</a> › <a href="{SITE}/facilities">Facilities</a> › '
            f'<a href="{SITE}/facilities/in/us">United States</a> › {_e(sname)}',
            body,
        )
        return _respond(ck, _store(ck, page_html), status=200)

    crumbs = [("Home", f"{SITE}/"), ("Facilities", f"{SITE}/facilities"),
              ("United States", f"{SITE}/facilities/in/us"),
              (sname, f"{SITE}/facilities/in/us/{st}")]
    return _render_listing(ck, facs, page, f"{SITE}/facilities/in/us/{st}",
                           sname, crumbs)


# ── sitemap support (r-seo-0801) ───────────────────────────────────────
def hub_sitemap_counts():
    """(country → n, us-state-slug → n) with the SAME filters the hub pages
    serve, so sitemap-emitted /page/N and /in/us/<state> URLs can never drift
    from what actually renders. main.py's fac_rows unions the legacy
    `facilities` table, which these pages do NOT serve — counting from it
    would emit pagination past the real last page."""
    countries: dict = {}
    states: dict = {}
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT LOWER(btrim(country)) AS cc, COUNT(*) AS n
              FROM discovered_facilities
             WHERE name IS NOT NULL AND name <> ''
               AND country IS NOT NULL AND btrim(country) <> ''
               AND duplicate_of_id IS NULL
             GROUP BY 1
        """)
        for cc, n in (cur.fetchall() or []):
            if cc:
                countries[cc] = int(n or 0)
        cur.execute("""
            SELECT btrim(state) AS st, COUNT(*) AS n
              FROM discovered_facilities
             WHERE LOWER(btrim(country)) = 'us'
               AND name IS NOT NULL AND name <> ''
               AND duplicate_of_id IS NULL
             GROUP BY 1
        """)
        for stv, n in (cur.fetchall() or []):
            ss = us_state_slug(stv)
            if ss:
                states[ss] = states.get(ss, 0) + int(n or 0)
        conn.close()
    except Exception:
        pass
    return countries, states


def _ymd(value):
    """'YYYY-MM-DD' from a date/datetime/str, or None when it is not one."""
    txt = str(value or "")[:10]
    return txt if re.match(r"^\d{4}-\d{2}-\d{2}$", txt) else None


def hub_sitemap_lastmod():
    """(country → 'YYYY-MM-DD', us-state-slug → 'YYYY-MM-DD'): MAX(first_seen)
    over EXACTLY the rows hub_sitemap_counts counts — the same three filters,
    so the date describes the page that renders.

    seo F11 (2026-09-02): sitemap-static.xml carried 559/560 entries pinned
    <lastmod>2026-08-19</lastmod>, including every /facilities/in/<cc> hub
    whose membership changes daily. The pin is right for hand-curated
    pages (main.py _STATIC_LASTMOD) and wrong for DB-driven ones: an
    always-stale date is the mirror image of the always-"today" date the
    r-lastmod-honesty note retired — both teach Google to ignore the signal.
    Pattern lifted from the city-state markets shard (MAX(f.first_seen)).
    Fail-soft: ({}, {}) on any error, and the caller keeps the pin.
    """
    countries: dict = {}
    states: dict = {}
    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT LOWER(btrim(country)) AS cc, MAX(first_seen) AS lm
              FROM discovered_facilities
             WHERE name IS NOT NULL AND name <> ''
               AND country IS NOT NULL AND btrim(country) <> ''
               AND duplicate_of_id IS NULL
             GROUP BY 1
        """)
        for cc, lm in (cur.fetchall() or []):
            d = _ymd(lm)
            if cc and d:
                countries[cc] = d
        cur.execute("""
            SELECT btrim(state) AS st, MAX(first_seen) AS lm
              FROM discovered_facilities
             WHERE LOWER(btrim(country)) = 'us'
               AND name IS NOT NULL AND name <> ''
               AND duplicate_of_id IS NULL
             GROUP BY 1
        """)
        for stv, lm in (cur.fetchall() or []):
            ss = us_state_slug(stv)
            d = _ymd(lm)
            if ss and d:
                # 'TX' and 'Texas' both map to 'texas' — keep the newest
                states[ss] = max(states.get(ss, ""), d)
        conn.close()
    except Exception:
        pass
    return countries, states


def register_facilities_hub(app):
    app.register_blueprint(facilities_hub_bp)
