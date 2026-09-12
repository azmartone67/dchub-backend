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

sitemap_auto_bp = Blueprint("sitemap_auto", __name__)

BASE = "https://dchub.cloud"


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
    ("/pocket-listings", 0.7, "weekly"),
    ("/spare-capacity", 0.7, "weekly"),
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
    # /facilities/in/<cc> 676-shell lesson, and the same edge-routing lesson the
    # listings block below already learned (it emits ?l= because /listings/<slug>
    # 404s). Pinned by tests/test_sitemap_auto_no_site_id_urls.py.

    # Public pocket listings
    try:
        with _conn() as c, c.cursor() as cur:
            rows = _safe(cur, """
                SELECT slug, COALESCE(updated_at, created_at)
                  FROM exclusive_listings
                 WHERE status = 'public'
                 ORDER BY created_at DESC LIMIT 100""")
            # /listings/<slug> 404s at the edge (no _routes.json include
            # reaches the worker's SPA rewrite); the static page reads ?l=.
            from urllib.parse import quote as _quote
            for slug, last in rows:
                if slug:
                    lastmod = last.strftime("%Y-%m-%d") if last else now_iso
                    urls.append(_url_xml(
                        f"{BASE}/listings?l={_quote(slug, safe='')}", lastmod, 0.7, "weekly"))
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
            # in tests/test_sitemap_auto_no_site_id_urls.py now pins. Audited
            # removal: contracts/api_response_exceptions.json.
            cur.execute("""SELECT COUNT(*) FROM exclusive_listings WHERE status = 'public'""")
            out["public_listings"] = int(cur.fetchone()[0])
            cur.execute("""SELECT COUNT(*) FROM news
                             WHERE published_date > NOW() - INTERVAL '90 days'""")
            out["recent_news"] = int(cur.fetchone()[0])
    except Exception as e:
        out["error_partial"] = str(e)[:200]
    return jsonify(out), 200
