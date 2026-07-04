"""SEO facilities hub (2026-06-29).

Fixes /facilities (was 503 / backend-404) and un-orphans the ~13.8K
/facilities/<slug> pages Google had "Discovered – currently not indexed":
they were only in the sitemap, with no crawlable hub and 0 homepage links.

Geography hierarchy — path-based (robots.txt disallows ?params):
  /facilities                       → countries index (by facility count)
  /facilities/in/<country>          → facilities in a country, grouped by market
  /facilities/in/<country>/page/<n> → path-based pagination (no ?params)

Links are built with the SAME canonical slug the sitemap + live facility pages
use (discovered_facilities + slugify + stable_hash8) so every link resolves 200.
Cached in-process (1h) to spare the 1-replica backend from crawler hammering.
"""
import re
import html as _html
from flask import Blueprint, Response, request

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


def _country_name(code: str) -> str:
    c = (code or "").strip()
    return _COUNTRY_NAMES.get(c.upper(), c.upper() if len(c) <= 3 else c)


def _slugify(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"[-\s]+", "-", s).strip("-")


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


def _shell(title, desc, canonical, breadcrumb_html, body_html):
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
<style>body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:1100px;margin:0 auto;padding:24px;line-height:1.5;color:#0f172a}}
a{{color:#1d4ed8;text-decoration:none}}a:hover{{text-decoration:underline}}
nav.bc{{font-size:14px;color:#64748b;margin-bottom:16px}}
h1{{font-size:26px;margin:.2em 0}}h2{{font-size:18px;margin:1.2em 0 .4em;border-bottom:1px solid #e2e8f0;padding-bottom:4px}}
ul.grid{{list-style:none;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:6px 24px}}
.muted{{color:#64748b;font-size:13px}}
footer{{margin-top:40px;padding-top:16px;border-top:1px solid #e2e8f0;font-size:14px;color:#64748b}}</style>
</head><body>
<nav class="bc">{breadcrumb_html}</nav>
{body_html}
<footer>
<a href="{SITE}/">DC Hub</a> · <a href="{SITE}/facilities">All facilities</a> ·
<a href="{SITE}/facilities/directory">Facility Directory</a> ·
<a href="{SITE}/dcpi">DC Hub Power Index</a> · <a href="{SITE}/markets">Markets</a> ·
<a href="{SITE}/grid">Grid</a>
<div class="muted" style="margin-top:8px">DC Hub — live data-center infrastructure intelligence across 170+ countries.</div>
</footer>
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
             GROUP BY country
             ORDER BY n DESC, country
        """)
        rows = cur.fetchall() or []
        conn.close()
    except Exception:
        rows = []
    total = sum(int(n or 0) for _, n in rows)
    items = []
    for code, n in rows:
        nm = _country_name(code)
        items.append(
            f'<li><a href="{SITE}/facilities/in/{_e(str(code).lower())}">{_e(nm)}</a>'
            f' <span class="muted">({int(n or 0):,})</span></li>'
        )
    body = (
        f"<h1>Data Center Facilities by Country</h1>"
        f'<p class="muted">{total:,} tracked data-center facilities across '
        f"{len(rows)} countries. Browse by country, then by market, to every "
        f"facility profile.</p>"
        f'<ul class="grid">{"".join(items)}</ul>'
    )
    page = _shell(
        "Data Center Facilities by Country | DC Hub",
        f"Browse {total:,} data-center facilities across {len(rows)} countries — "
        "by country and market, with power, location and operator detail. DC Hub.",
        f"{SITE}/facilities",
        f'<a href="{SITE}/">Home</a> › Facilities',
        body,
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
             ORDER BY grp, name
        """, (country,))
        rows = cur.fetchall() or []
        conn.close()
    except Exception:
        rows = []

    # Build (grp, slug, name, loc) for every facility with a valid slug.
    facs = []
    for name, provider, grp, city, state, power in rows:
        slug = _fac_slug(provider, name)
        if not slug:
            continue
        loc = ", ".join([x for x in (city, state) if x and str(x).strip()])
        facs.append((grp or "Other", slug, name, loc))

    cname = _country_name(country)
    if not facs:
        body = (f"<h1>Data Centers in {_e(cname)}</h1>"
                f'<p class="muted">No facility profiles found for this country yet. '
                f'Browse <a href="{SITE}/facilities">all countries</a> or the '
                f'<a href="{SITE}/dcpi">DC Hub Power Index</a>.</p>')
        page_html = _shell(
            f"Data Centers in {cname} | DC Hub",
            f"Data-center facilities in {cname} — DC Hub.",
            f"{SITE}/facilities/in/{country}",
            f'<a href="{SITE}/">Home</a> › <a href="{SITE}/facilities">Facilities</a> › {_e(cname)}',
            body,
        )
        return _respond(ck, _store(ck, page_html), status=200)

    total = len(facs)
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = min(page, pages)
    start = (page - 1) * PAGE_SIZE
    chunk = facs[start:start + PAGE_SIZE]

    # Group this page's slice by market heading.
    out, cur_grp = [], None
    for grp, slug, name, loc in chunk:
        if grp != cur_grp:
            if cur_grp is not None:
                out.append("</ul>")
            out.append(f"<h2>{_e(grp)}</h2><ul class=\"grid\">")
            cur_grp = grp
        loc_s = f' <span class="muted">— {_e(loc)}</span>' if loc else ""
        out.append(f'<li><a href="{SITE}/facilities/{_e(slug)}">{_e(name)}</a>{loc_s}</li>')
    if cur_grp is not None:
        out.append("</ul>")

    # Path-based pager (no ?params — robots disallows them).
    base = f"{SITE}/facilities/in/{country}"
    pager = []
    if page > 1:
        prev = base if page == 2 else f"{base}/page/{page-1}"
        pager.append(f'<a href="{prev}">‹ Prev</a>')
    if page < pages:
        pager.append(f'<a href="{base}/page/{page+1}">Next ›</a>')
    pager_html = (f'<p class="muted">Page {page} of {pages}</p>'
                  f'<p>{" · ".join(pager)}</p>') if pages > 1 else ""

    body = (
        f"<h1>Data Centers in {_e(cname)}</h1>"
        f'<p class="muted">{total:,} tracked data-center facilities in {_e(cname)}, '
        f"grouped by market. Each links to a full profile with power, location and "
        f"operator detail.</p>"
        f'{"".join(out)}{pager_html}'
    )
    canonical = base if page == 1 else f"{base}/page/{page}"
    page_html = _shell(
        f"Data Centers in {cname} ({total:,}) | DC Hub",
        f"All {total:,} tracked data-center facilities in {cname}, by market — "
        "power, location and operator detail. DC Hub.",
        canonical,
        f'<a href="{SITE}/">Home</a> › <a href="{SITE}/facilities">Facilities</a> › {_e(cname)}',
        body,
    )
    return _respond(ck, _store(ck, page_html))


def register_facilities_hub(app):
    app.register_blueprint(facilities_hub_bp)
