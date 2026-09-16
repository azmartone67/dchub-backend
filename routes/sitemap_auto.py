"""
sitemap_auto.py — Phase GG (2026-05-15) Bundle 6A item 7.

The current /sitemap.xml is dated Feb 1, 2026 — 3+ months stale, missing
every page shipped since (markets/<slug> pages, sites/<slug>, listings,
brain dashboard, media). This endpoint generates a FRESH sitemap from
live DB rows + the static page registry, cached at the edge for 1 hour.

Frontend served at https://dchub.cloud/sitemap.xml is the static file
in CF Pages. We expose this DYNAMIC sitemap at /api/v1/sitemap.xml.
A _redirects rewrite (added separately in frontend bundle) makes
/sitemap.xml proxy to /api/v1/sitemap.xml so external crawlers always
hit fresh content.
"""
import os
from datetime import datetime, timezone
from html import escape

from flask import Blueprint, Response, request

# ONE liveness rule for Capacity Source listings, imported rather than retyped.
# routes.exclusive_listings._LIVE_WHERE is what the /listings feed, its counts
# and GET /api/v1/listings/summary all filter on (status public or pocket, and
# not past expires_at), so the sitemap lists exactly the listings whose teaser
# cards are public on /listings — and a listing that stops being live leaves
# every one of those surfaces on the same build. A second hand-typed copy here
# is how the two lists would drift apart.
from routes.exclusive_listings import _LIVE_WHERE as _LISTING_LIVE_WHERE

sitemap_auto_bp = Blueprint("sitemap_auto", __name__)

BASE = "https://dchub.cloud"

# SELECT … FROM in one uppercase literal (scripts/dataset_inventory.py reads
# the table this module asks for out of the AST); the shared predicate is
# appended, never re-spelled.
_LIVE_LISTINGS_SQL = (
    "SELECT slug, updated_at FROM exclusive_listings WHERE "
    + " AND ".join(_LISTING_LIVE_WHERE)
    + " ORDER BY updated_at DESC LIMIT 200")
_LIVE_LISTINGS_COUNT_SQL = (
    "SELECT COUNT(*) FROM exclusive_listings WHERE "
    + " AND ".join(_LISTING_LIVE_WHERE))


def _conn():
    import psycopg2
    c = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)
    c.autocommit = True
    return c


# Static high-priority pages that always go in the sitemap.
_STATIC_PAGES = [
    ("/", 1.0, "daily"),
    ("/pricing", 0.9, "weekly"),
    ("/api-docs", 0.9, "weekly"),
    # query-win wave (2026-08-02) — keep in lockstep with main.py static_pages.
    ("/grid/queue/ercot", 0.8, "daily"),
    ("/us-data-center-map", 0.8, "weekly"),
    ("/markets/", 0.9, "daily"),
    ("/dc-hub-media", 0.9, "daily"),
    ("/by-the-numbers", 0.9, "daily"),
    ("/cited-by", 0.8, "weekly"),
    # ── /sites/ is DELIBERATELY ABSENT (2026-09-12) ─────────────────────────
    # In lockstep with main.py's static_pages, which withholds it, and with
    # WITHHELD in tests/test_sitemap_covers_linked_pages.py: robots.txt carries
    # `Disallow: /sites/`, and a sitemap entry for a robots-blocked URL is
    # reported as "Submitted URL blocked by robots.txt" against the whole
    # sitemap. This list shipped it at 0.8/daily while main.py withheld it —
    # one policy, two hand-typed lists, only one of them guarded. Both are
    # checked now.
    # ★ The SERVED robots.txt also carries `Allow: /sites/$`, which exempts THIS
    #   exact landing page (not /sites/value, not /sites/<id>), so re-listing it
    #   is defensible. But that file belongs to dchub-frontend and this repo
    #   deliberately does not model it — see
    #   test_this_repo_does_not_pretend_to_own_robots_txt. Change it there, then
    #   update WITHHELD and ROBOTS_BLOCKED_PREFIXES in the same PR.
    ("/listings", 0.8, "daily"),
    ("/transactions", 0.8, "daily"),
    ("/capacity-pipeline", 0.8, "daily"),
    ("/rankings", 0.8, "weekly"),
    ("/dcpi/leaderboard", 0.9, "daily"),    # 2026-06-08: canonical ranked-market leaderboard (structured-data page)
    ("/mcp-standing", 0.8, "weekly"),       # MCP adoption / registry-standing page
    ("/brain-live", 0.6, "daily"),  # r-brain-public (2026-06-18): /brain is now admin-only (403 to crawlers); /brain-live is the PUBLIC sanitized transparency page (index,follow). Sitemapping /brain was a crawl error.
    ("/gdci", 0.7, "weekly"),
    ("/about", 0.6, "monthly"),
    ("/tax-incentives", 0.7, "weekly"),
    ("/land-power", 0.7, "weekly"),
    ("/fiber", 0.6, "weekly"),
    ("/ai", 0.7, "weekly"),
    ("/ai-agents", 0.6, "weekly"),
    ("/ecosystem", 0.6, "weekly"),
    ("/news", 0.8, "daily"),
    ("/announcements", 0.8, "daily"),
    ("/glossary", 0.5, "monthly"),
    ("/faq", 0.5, "monthly"),
    # ── Phase JJ (2026-05-17): 11 Phase 282 paths newly reachable
    # after Phase FF-2 unblocked the worker routing. Previously all
    # 404'd from CF Pages so they were intentionally omitted; now
    # backend serves them and AI agents can discover them.
    ("/operators", 0.8, "weekly"),
    ("/transparency", 0.7, "weekly"),
    ("/sentinel", 0.6, "daily"),
    ("/vs", 0.8, "weekly"),
    ("/bs-translator", 0.7, "weekly"),
    ("/intelligence", 0.7, "weekly"),
    ("/power-totals", 0.7, "daily"),
    ("/events", 0.7, "weekly"),
    # r70 (2026-06-03): the 3 new flagship products — live + nav/ticker/MCP-wired
    # but missing from the sitemap, so crawlers + AI agents couldn't discover them.
    ("/site-selection", 0.8, "weekly"),
    ("/grid-transition", 0.8, "weekly"),
    ("/deal-autopsy", 0.8, "weekly"),
    # /transactions is already listed above
]


def _safe(cur, sql, params=()):
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    except Exception:
        return []


def _url_xml(loc, lastmod=None, priority=0.5, changefreq="weekly"):
    parts = [f"  <url><loc>{escape(loc)}</loc>"]
    if lastmod:
        parts.append(f"<lastmod>{escape(lastmod)}</lastmod>")
    parts.append(f"<changefreq>{changefreq}</changefreq>")
    parts.append(f"<priority>{priority:.2f}</priority>")
    parts.append("</url>")
    return "".join(parts)


def _generate_sitemap():
    """Build the full XML. Pure compute — caller wraps in Response."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    urls = []

    # Static pages
    for path, prio, freq in _STATIC_PAGES:
        urls.append(_url_xml(BASE + path, now_iso, prio, freq))

    # DCPI markets — one URL per scored market
    try:
        with _conn() as c, c.cursor() as cur:
            rows = _safe(cur, """
                SELECT DISTINCT ON (market_slug) market_slug,
                       GREATEST(computed_at, NOW() - INTERVAL '7 days')
                  FROM market_power_scores
                 WHERE market_slug IS NOT NULL
                 ORDER BY market_slug, computed_at DESC""")
            for slug, last in rows:
                if slug:
                    lastmod = last.strftime("%Y-%m-%d") if last else now_iso
                    urls.append(_url_xml(
                        f"{BASE}/markets/{slug}", lastmod, 0.8, "daily"))
    except Exception:
        pass

    # ── r-sites-dead (2026-09-11): the /sites/<id> block is GONE ────────────
    # It selected `id, COALESCE(updated_at, first_seen) FROM facilities` and
    # appended 200 `/sites/<facilities.id>` URLs. Both halves were broken:
    #   * `facilities` has NO `updated_at` column — GET /api/health/diag reports
    #     the live information_schema for it: 45 columns, `first_seen` and
    #     `last_updated` both TEXT, no `updated_at`, no `created_at`. The SELECT
    #     raised on every build and `_safe` returned [], so this block has
    #     emitted ZERO URLs for as long as the column has been absent, while
    #     /api/v1/sitemap/health counted 2,103 rows with power_mw. Naming
    #     first_seen instead would not have rescued it either: it is TEXT, and
    #     the loop called .strftime() on it, which the `except` below swallows.
    #   * Every `/sites/<id>` URL is ROBOTS-BLOCKED. Live robots.txt carries
    #     `Disallow: /sites/` with `Allow: /sites/$`, so the bare landing page
    #     is crawlable and the per-id pages are not. A sitemap entry for a
    #     robots-blocked URL is reported as "Submitted URL blocked by
    #     robots.txt" against the whole sitemap, which is why main.py's
    #     static_pages withholds these paths. They also answer 200 from the
    #     static shell for ANY id (`/sites/zzz-not-a-facility-zzz` included),
    #     canonicalising to `/sites/` whatever the id.
    # ★ CORRECTION (2026-09-12). The first version of this comment claimed the
    #   page's data call 404s and the page type was dead. That was a bad probe:
    #   the endpoint is `/api/v1/sites/<id_or_slug>/capacity-report` — the
    #   /api/v1 prefix was missing — and it answers 200 with a Site Capacity
    #   Report teaser for a real slug, 404 `site_not_found` for junk. The page
    #   WORKS and is withheld from crawlers on purpose. `/site*` IS in the
    #   frontend's _routes.json include.
    # Reviving the block would publish 200 robots-blocked soft-404s: the
    # /facilities/in/<cc> 676-shell lesson. Pinned by
    # tests/test_sitemap_auto_no_site_id_urls.py.

    # ── Live Capacity Source listings ───────────────────────────────────────
    # Every LIVE listing, by the feed's own rule (_LIVE_LISTINGS_SQL above),
    # at the per-listing path.
    #
    # ★ CORRECTION (2026-09-16). This block used to point at the listings index
    #   with the slug in a query string, under a comment asserting the
    #   per-listing path was not served at the edge, and it filtered
    #   `status = 'public'` while every live listing is `pocket` — so it
    #   published ZERO listing URLs. Both halves are fixed here:
    #     * The per-listing path IS SERVED. dchub-frontend#1491 (live worker
    #       5.0.0, 2026-09-16) ships a crawlable server-rendered teaser page
    #       per listing: 200 with `x-robots-tag: index, follow` for a live
    #       slug, and a real not-found with `noindex` for an unknown slug.
    #       Verified live that day. The old claim is retired, not softened.
    #     * `status = 'public'` is NOT the liveness rule. The feed's rule is
    #       _LIVE_WHERE, and the owner's decision is that all live listings
    #       belong here, because their teaser cards are already public on
    #       /listings.
    #   lastmod is the listing's own updated_at, so a re-verified listing
    #   re-dates itself.
    #   Pinned by tests/test_sitemap_lists_live_listings.py.
    try:
        with _conn() as c, c.cursor() as cur:
            rows = _safe(cur, _LIVE_LISTINGS_SQL)
            from urllib.parse import quote as _quote
            for slug, last in rows:
                if slug:
                    lastmod = last.strftime("%Y-%m-%d") if last else now_iso
                    urls.append(_url_xml(
                        f"{BASE}/listings/{_quote(slug, safe='')}", lastmod, 0.7, "weekly"))
    except Exception:
        pass

    # Recent news (last 90 days)
    try:
        with _conn() as c, c.cursor() as cur:
            rows = _safe(cur, """
                SELECT url, published_date
                  FROM news
                 WHERE published_date > NOW() - INTERVAL '90 days'
                   AND url IS NOT NULL AND url <> ''
                 ORDER BY published_date DESC LIMIT 200""")
            for url, last in rows:
                # only include internal news pages
                if url and url.startswith("https://dchub.cloud/"):
                    lastmod = last.strftime("%Y-%m-%d") if last else now_iso
                    urls.append(_url_xml(url, lastmod, 0.6, "monthly"))
    except Exception:
        pass

    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           + "\n".join(urls)
           + "\n</urlset>\n")
    return xml


@sitemap_auto_bp.route("/api/v1/sitemap.xml", methods=["GET"])
def sitemap_xml():
    """Dynamic sitemap.xml. Built from live DB rows + static page registry."""
    xml = _generate_sitemap()
    resp = Response(xml, mimetype="application/xml")
    resp.headers["Cache-Control"] = ("public, max-age=3600, "
                                     "s-maxage=3600, stale-while-revalidate=7200")
    resp.headers["X-DC-Sitemap"] = "auto"
    return resp


@sitemap_auto_bp.route("/api/v1/sitemap/health", methods=["GET"])
def sitemap_health():
    """Reports counts and last-modified summary."""
    from flask import jsonify
    out = {"ok": True, "static_pages": len(_STATIC_PAGES)}
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(DISTINCT market_slug) FROM market_power_scores")
            out["dcpi_markets"] = int(cur.fetchone()[0])
            # `facilities_with_power` lived here until 2026-09-12. It counted
            # the population behind the /sites/<id> block removed in #4436, so
            # it described nothing this generator emits — every other count
            # here maps to a block above. With it gone, this module does not
            # read `facilities` at all, which is what the module-wide AST check
            # in tests/test_sitemap_auto_no_site_id_urls.py now pins.
            # `public_listings` (COUNT of status='public') went the same way on
            # 2026-09-16: it described a filter the generator no longer uses —
            # the listings block emits every LIVE listing — and the count it
            # published was 0 while both live listings were `pocket`. Every
            # count here maps to a block above; this one counts exactly the
            # population _LIVE_LISTINGS_SQL lists.
            # Both audited removals: contracts/api_response_exceptions.json.
            cur.execute(_LIVE_LISTINGS_COUNT_SQL)
            out["live_listings"] = int(cur.fetchone()[0])
            cur.execute("""SELECT COUNT(*) FROM news
                             WHERE published_date > NOW() - INTERVAL '90 days'""")
            out["recent_news"] = int(cur.fetchone()[0])
    except Exception as e:
        out["error_partial"] = str(e)[:200]
    return jsonify(out), 200
