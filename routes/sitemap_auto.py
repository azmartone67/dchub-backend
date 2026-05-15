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
    ("/markets/", 0.9, "daily"),
    ("/dc-hub-media", 0.9, "daily"),
    ("/by-the-numbers", 0.9, "daily"),
    ("/cited-by", 0.8, "weekly"),
    ("/sites/", 0.8, "daily"),
    ("/listings", 0.8, "daily"),
    ("/transactions", 0.8, "daily"),
    ("/capacity-pipeline", 0.8, "daily"),
    ("/rankings", 0.8, "weekly"),
    ("/brain", 0.6, "daily"),
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

    # Top facilities by power_mw — sites/<id> deep pages
    try:
        with _conn() as c, c.cursor() as cur:
            rows = _safe(cur, """
                SELECT id, COALESCE(updated_at, first_seen)
                  FROM facilities
                 WHERE power_mw IS NOT NULL
                 ORDER BY power_mw DESC NULLS LAST
                 LIMIT 200""")
            for fid, last in rows:
                lastmod = last.strftime("%Y-%m-%d") if last else now_iso
                urls.append(_url_xml(
                    f"{BASE}/sites/{fid}", lastmod, 0.6, "weekly"))
    except Exception:
        pass

    # Public pocket listings
    try:
        with _conn() as c, c.cursor() as cur:
            rows = _safe(cur, """
                SELECT slug, COALESCE(updated_at, created_at)
                  FROM exclusive_listings
                 WHERE status = 'public'
                 ORDER BY created_at DESC LIMIT 100""")
            for slug, last in rows:
                if slug:
                    lastmod = last.strftime("%Y-%m-%d") if last else now_iso
                    urls.append(_url_xml(
                        f"{BASE}/listings/{slug}", lastmod, 0.7, "weekly"))
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
            cur.execute("""SELECT COUNT(*) FROM facilities WHERE power_mw IS NOT NULL""")
            out["facilities_with_power"] = int(cur.fetchone()[0])
            cur.execute("""SELECT COUNT(*) FROM exclusive_listings WHERE status = 'public'""")
            out["public_listings"] = int(cur.fetchone()[0])
            cur.execute("""SELECT COUNT(*) FROM news
                             WHERE published_date > NOW() - INTERVAL '90 days'""")
            out["recent_news"] = int(cur.fetchone()[0])
    except Exception as e:
        out["error_partial"] = str(e)[:200]
    return jsonify(out), 200
