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


def _cf_get(path: str) -> dict:
    """Helper: GET against CF API. Returns parsed JSON or error dict."""
    if not _CF_API_TOKEN:
        return {"ok": False, "error": "CLOUDFLARE_API_TOKEN not set"}
    try:
        import requests
        r = requests.get(
            f"https://api.cloudflare.com/client/v4{path}",
            headers={"Authorization": f"Bearer {_CF_API_TOKEN}"},
            timeout=12,
        )
        return r.json() if r.headers.get("content-type","").startswith("application/json") else {"raw": r.text[:500]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


@cf_purge_bp.route("/api/v1/cf/inspect/routes-and-rules", methods=["GET"])
def inspect_routes_and_rules():
    """Phase ZZZZ-cf-inspect (2026-05-18): aggregate view of everything
    that could be intercepting requests for dchub.cloud, so we can find
    what's catching /markets and what's serving the 4.8.3 worker error
    on /api/v1/marketing/publish-now.

    Returns:
      - workers in account (list of all worker scripts)
      - worker routes (which patterns map to which scripts)
      - pages projects
      - zone DNS records (looks for hostname intercepts)
      - bulk redirects + ruleset rules

    Public read-only — no admin gate. Token must have Workers/Pages/Zone
    read perms.
    """
    acct = (os.environ.get("CLOUDFLARE_ACCOUNT_ID")
            or "4bb33ec40ef02f9f4b41dc97668d5a52").strip()
    zone = (os.environ.get("CLOUDFLARE_ZONE_ID") or "").strip()
    out = {"account": acct, "zone_id": zone or "(unset — set CLOUDFLARE_ZONE_ID)"}

    # All worker scripts in the account
    workers = _cf_get(f"/accounts/{acct}/workers/scripts")
    out["workers_scripts"] = [
        {"id": w.get("id"),
         "created_on": w.get("created_on"),
         "modified_on": w.get("modified_on"),
         "logpush": w.get("logpush"),
         "placement_mode": w.get("placement", {}).get("mode") if isinstance(w.get("placement"), dict) else None}
        for w in (workers.get("result") or [])
    ] if workers.get("success") else {"_error": workers.get("errors", workers)}

    # Pages projects
    pages = _cf_get(f"/accounts/{acct}/pages/projects")
    out["pages_projects"] = [
        {"name": p.get("name"),
         "subdomain": p.get("subdomain"),
         "domains": p.get("domains"),
         "production_branch": p.get("production_branch"),
         "latest_deployment": (p.get("latest_deployment") or {}).get("created_on")}
        for p in (pages.get("result") or [])
    ] if pages.get("success") else {"_error": pages.get("errors", pages)}

    # Zone-scoped data only if ZONE_ID set
    if zone:
        # Worker routes on this zone — THE key data
        routes = _cf_get(f"/zones/{zone}/workers/routes")
        out["worker_routes"] = [
            {"pattern": r.get("pattern"), "script": r.get("script"), "id": r.get("id")}
            for r in (routes.get("result") or [])
        ] if routes.get("success") else {"_error": routes.get("errors", routes)}

        # Ruleset entries (transform rules, redirect rules, etc.)
        rulesets = _cf_get(f"/zones/{zone}/rulesets")
        out["rulesets"] = [
            {"id": rs.get("id"), "name": rs.get("name"), "phase": rs.get("phase"),
             "kind": rs.get("kind")}
            for rs in (rulesets.get("result") or [])
        ] if rulesets.get("success") else {"_error": rulesets.get("errors", rulesets)}

        # Page Rules
        page_rules = _cf_get(f"/zones/{zone}/pagerules")
        out["page_rules"] = [
            {"targets": [t.get("constraint", {}).get("value")
                         for t in (pr.get("targets") or [])],
             "actions": [a.get("id") for a in (pr.get("actions") or [])],
             "status": pr.get("status")}
            for pr in (page_rules.get("result") or [])
        ] if page_rules.get("success") else {"_error": page_rules.get("errors", page_rules)}
    else:
        out["worker_routes"] = "(need CLOUDFLARE_ZONE_ID)"

    return jsonify(out), 200
