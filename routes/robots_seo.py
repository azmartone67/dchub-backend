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

# Sitemaps — multi-property roll-up index
Sitemap: https://api.dchub.cloud/sitemap-index.xml
Sitemap: https://api.dchub.cloud/sitemap-facilities.xml
Sitemap: https://api.dchub.cloud/sitemap-markets.xml
Sitemap: https://api.dchub.cloud/sitemap-grids.xml

# Host preference (search engines treat as canonical signal)
Host: dchub.cloud
"""


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
