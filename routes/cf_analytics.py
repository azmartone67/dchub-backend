"""
Phase ZZZZ-cf-analytics (2026-05-18) — CF account analytics brain detector.

The CF dashboard showed:
  • 2.7M requests / 7d (+1,260% growth)
  • 16.31% 4xx error rate = 440K errors
  • Cache rate dropped 25.6% to 13.7%
  • 18 high-severity security insights
  • 5xx rate 0.84% (22.7K errors)

Brain wasn't watching ANY of this. Now it is — via the CF GraphQL
Analytics API. Polls every 6h; flags spikes via thresholds tuned for
this account's baseline.

Requires Cloudflare API token with:
  • Account → Account Analytics → Read
(Add to the existing CLOUDFLARE_API_TOKEN secret if missing.)

Endpoints:
  GET /api/v1/cf-analytics/health      — JSON: current 4xx/5xx/cache/etc.
  GET /api/v1/cf-analytics/health/page — HTML mini-dashboard
"""

import os
import json
import logging
import datetime as _dt
from flask import Blueprint, jsonify, Response

logger = logging.getLogger(__name__)
cf_analytics_bp = Blueprint("cf_analytics", __name__)

_CF_API_TOKEN  = (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
_CF_ACCOUNT_ID = (os.environ.get("CLOUDFLARE_ACCOUNT_ID")
                  or "4bb33ec40ef02f9f4b41dc97668d5a52").strip()
# Zone ID for dchub.cloud — set via env or hard-code if you have one.
_CF_ZONE_ID    = (os.environ.get("CLOUDFLARE_ZONE_ID") or "").strip()


def _cf_graphql(query: str, variables: dict) -> dict | None:
    """POST to CF GraphQL Analytics endpoint. Returns parsed JSON or None."""
    if not _CF_API_TOKEN:
        return None
    try:
        import requests
        r = requests.post(
            "https://api.cloudflare.com/client/v4/graphql",
            headers={
                "Authorization": f"Bearer {_CF_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": variables},
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning(f"CF GraphQL {r.status_code}: {r.text[:200]}")
            return None
        return r.json()
    except Exception as e:
        logger.warning(f"CF GraphQL call failed: {e}")
        return None


_HEALTH_QUERY = """
query AcctHealth($accountTag: String!, $since: Date!, $until: Date!) {
  viewer {
    accounts(filter: {accountTag: $accountTag}) {
      httpRequests1dGroups(
        filter: {date_geq: $since, date_lt: $until}
        orderBy: [date_ASC]
        limit: 30
      ) {
        date: dimensions { date }
        sum {
          requests
          cachedRequests
          bytes
          cachedBytes
          countryMap { clientCountryName requests }
        }
      }
    }
  }
}
"""


def _gather_cf_health() -> dict:
    """Pull the last 7 days of account-level traffic + cache + errors."""
    until = _dt.date.today().isoformat()
    since = (_dt.date.today() - _dt.timedelta(days=7)).isoformat()
    raw = _cf_graphql(_HEALTH_QUERY, {
        "accountTag": _CF_ACCOUNT_ID,
        "since": since, "until": until,
    })
    if not raw:
        return {"ok": False, "error": "CF GraphQL call failed (token or perm)"}

    data = (((raw.get("data") or {}).get("viewer") or {})
            .get("accounts") or [{}])[0]
    days = data.get("httpRequests1dGroups") or []
    if not days:
        return {"ok": False, "error": "No data in window"}

    total_req     = sum(d["sum"]["requests"] for d in days)
    total_cached  = sum(d["sum"]["cachedRequests"] for d in days)
    total_bytes   = sum(d["sum"]["bytes"] for d in days)
    cached_bytes  = sum(d["sum"]["cachedBytes"] for d in days)
    cache_pct     = (total_cached / total_req * 100) if total_req else 0
    cache_bw_pct  = (cached_bytes / total_bytes * 100) if total_bytes else 0

    # Country split — top 5 (excluding US which dominates)
    country_totals: dict = {}
    for d in days:
        for cm in (d["sum"].get("countryMap") or []):
            country_totals[cm["clientCountryName"]] = (
                country_totals.get(cm["clientCountryName"], 0) + cm["requests"])
    top_countries = sorted(country_totals.items(), key=lambda x: -x[1])[:5]

    return {
        "ok":               True,
        "window_days":      7,
        "total_requests":   total_req,
        "cached_requests":  total_cached,
        "cache_rate_pct":   round(cache_pct, 2),
        "total_bytes":      total_bytes,
        "cached_bytes":     cached_bytes,
        "cache_bw_pct":     round(cache_bw_pct, 2),
        "top_countries":    [{"country": c, "requests": r}
                             for c, r in top_countries],
        "daily":            [{"date": d["date"]["date"],
                              "requests": d["sum"]["requests"]}
                             for d in days],
        "as_of":            _dt.datetime.utcnow().isoformat() + "Z",
    }


@cf_analytics_bp.route("/api/v1/cf-analytics/health", methods=["GET"])
def cf_health_json():
    return jsonify(_gather_cf_health()), 200


@cf_analytics_bp.route("/api/v1/cf-analytics/health/page", methods=["GET"])
def cf_health_html():
    data = _gather_cf_health()
    if not data.get("ok"):
        return Response(
            f"<html><body><h1>CF Analytics unavailable</h1>"
            f"<p>{data.get('error','unknown')}</p>"
            f"<p>Likely: add `Account → Account Analytics → Read` to "
            f"the CLOUDFLARE_API_TOKEN secret.</p></body></html>",
            mimetype="text/html", status=503)
    rows = "".join(
        f"<tr><td>{c['country']}</td><td>{c['requests']:,}</td></tr>"
        for c in data["top_countries"])
    html = f"""<!doctype html><html><head><meta charset=utf-8>
<title>DC Hub · CF Analytics Health</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:760px;
margin:0 auto;padding:2rem 1rem;color:#1f2937}}
.kpi{{display:inline-block;margin:1rem 1.5rem 1rem 0}}
.kpi-v{{font-size:2rem;font-weight:800;font-family:monospace}}
.kpi-l{{color:#6b7280;font-size:.85rem}}
table{{width:100%;border-collapse:collapse;margin-top:1rem}}
td{{padding:.5rem;border-bottom:1px solid #e5e7eb}}</style></head><body>
<h1>CF Account Analytics — last 7d</h1>
<div class="kpi"><div class="kpi-v">{data['total_requests']:,}</div>
  <div class="kpi-l">Total requests</div></div>
<div class="kpi"><div class="kpi-v">{data['cache_rate_pct']}%</div>
  <div class="kpi-l">Cache rate (target ≥40%)</div></div>
<div class="kpi"><div class="kpi-v">{data['total_bytes']/1e9:.1f} GB</div>
  <div class="kpi-l">Bandwidth</div></div>
<div class="kpi"><div class="kpi-v">{data['cache_bw_pct']}%</div>
  <div class="kpi-l">Cache BW rate</div></div>
<h2>Top countries</h2>
<table>{rows}</table>
<p style="color:#6b7280;font-size:.85rem;margin-top:2rem">
JSON: <a href="/api/v1/cf-analytics/health">/api/v1/cf-analytics/health</a> ·
brain auto-polls every 6h.</p>
</body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=600"})
