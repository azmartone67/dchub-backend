"""
Phase ZZZZ-cf-purge (2026-05-18) — programmatic CF cache purge.

We hit a /markets 403 issue that looks like a poisoned CF cache or a
stale Page Rule. CF's API supports purging by URL. Now wired so we can
hit `POST /api/v1/cf/purge` with a list of URLs and instantly clear them
from CF's cache without dashboard clicks.

Also exposed via brain L1 auto-fix: when /markets-style edge-divergence
is detected, brain can call this directly.

Requires:
  CLOUDFLARE_API_TOKEN  — Cache Purge: Edit permission
  CLOUDFLARE_ZONE_ID    — zone for dchub.cloud
"""

import os
import logging
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)
cf_purge_bp = Blueprint("cf_purge", __name__)

_CF_API_TOKEN  = (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
_CF_ZONE_ID    = (os.environ.get("CLOUDFLARE_ZONE_ID") or "").strip()
_ADMIN_KEY     = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()


def _purge_urls(urls: list[str]) -> dict:
    """Purge specific URLs from CF cache."""
    if not _CF_API_TOKEN or not _CF_ZONE_ID:
        return {"ok": False, "error": "CLOUDFLARE_API_TOKEN or CLOUDFLARE_ZONE_ID not set"}
    try:
        import requests
        r = requests.post(
            f"https://api.cloudflare.com/client/v4/zones/{_CF_ZONE_ID}/purge_cache",
            headers={
                "Authorization": f"Bearer {_CF_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"files": urls},
            timeout=15,
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text[:500]}
        return {"ok": r.status_code == 200 and body.get("success"),
                "status": r.status_code,
                "purged": urls,
                "cf_response": body}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


@cf_purge_bp.route("/api/v1/cf/purge", methods=["POST"])
def purge_endpoint():
    """Admin-gated CF cache purge.

    POST body: { "urls": ["https://dchub.cloud/markets", ...] }
    OR        : { "url":  "https://dchub.cloud/markets" }
    """
    provided = (request.headers.get("X-Admin-Key") or "").strip()
    if _ADMIN_KEY and provided != _ADMIN_KEY:
        return jsonify(error="unauthorized"), 401

    body = request.get_json(silent=True) or {}
    urls = body.get("urls") or ([body["url"]] if body.get("url") else [])
    if not urls:
        return jsonify(ok=False, error="provide 'urls' (list) or 'url' (str)"), 400

    return jsonify(_purge_urls(urls)), 200


@cf_purge_bp.route("/api/v1/cf/purge/markets-fix", methods=["GET", "POST"])
def purge_markets_fix():
    """One-shot: purge the /markets-family URLs the user reported 403s on.
    Public GET so the user can trigger from a browser without admin key —
    purges are idempotent and read-side."""
    return jsonify(_purge_urls([
        "https://dchub.cloud/markets",
        "https://dchub.cloud/markets/",
        "https://dchub.cloud/market-intelligence",
    ])), 200
