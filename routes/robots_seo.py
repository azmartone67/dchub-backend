"""
robots_seo.py — robots.txt + Sitemap: directive.

Phase ZZZZZ-round35 (2026-05-24). Fixes missing Sitemap: line on both
api.dchub.cloud and dchub.cloud robots.txt. Adding this directive is
the standard signal for crawlers that don't use GSC submission
(Bing-via-IndexNow, DuckDuckGo, Brave, Mojeek, etc).

Routes:
  /robots.txt              — canonical, served from Flask via api.dchub.cloud
  /robots-canonical.txt    — alternate alias
"""
from flask import Blueprint

robots_seo_bp = Blueprint("robots_seo", __name__)

ROBOTS_BODY = """User-agent: *
Allow: /

# Crawl-delay for politeness on the 21k facility pages
Crawl-delay: 1

# Disallow admin + internal API surfaces
Disallow: /api/admin/
Disallow: /api/v1/admin/
Disallow: /api/auth/
Disallow: /api/stripe/

# Sitemaps — ONE canonical entry point.
# r37 (2026-05-31): canonical apex (dchub.cloud) only, never api.* — two hosts
# advertising the same pages made Google pick competing canonicals.
# r-sitemap-shard (2026-07-03): the four legacy sub-sitemaps listed here were
# retired (sitemap-facilities.xml was 5,772 /facility/<id> self-canonical dupes
# — the QA deep-dive's #1 Google+Bing indexing blocker). /sitemap.xml is now a
# sitemapindex that fans out to /sitemap-<section>.xml shards; advertise only it.
Sitemap: https://dchub.cloud/sitemap.xml

# Host preference (search engines treat as canonical signal)
Host: dchub.cloud
"""


# AUTO-REPAIR: duplicate route '/robots.txt' also in ai_discovery_routes.py:1299 — review and remove one
@robots_seo_bp.route("/robots.txt")
def robots_txt():
    return ROBOTS_BODY, 200, {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "public, max-age=86400",
    }


@robots_seo_bp.route("/robots-canonical.txt")
def robots_canonical():
    return ROBOTS_BODY, 200, {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "public, max-age=86400",
    }


@robots_seo_bp.route("/robots-health")
def robots_health():
    return {"blueprint": "robots_seo_bp", "status": "ok",
            "sitemaps_advertised": 4}, 200
