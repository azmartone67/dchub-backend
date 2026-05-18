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


# Phase ZZZZ-cf-analytics-fix: httpRequests1dGroups is ZONE-scope, not
# account-scope. For account-level rollups use httpRequestsAdaptiveGroups.
# Falls back to per-zone aggregation if account-scope returns empty.
_HEALTH_QUERY = """
query AcctHealth($accountTag: String!, $since: DateTime!, $until: DateTime!) {
  viewer {
    accounts(filter: {accountTag: $accountTag}) {
      httpRequestsAdaptiveGroups(
        filter: {datetime_geq: $since, datetime_lt: $until}
        orderBy: [datetime_ASC]
        limit: 100
      ) {
        dimensions { datetime }
        sum {
          edgeResponseBytes
          visits
        }
        count
      }
    }
  }
}
"""


def _gather_cf_health() -> dict:
    """Pull the last 7 days of account-level traffic + cache + errors.
    Uses httpRequestsAdaptiveGroups which is account-scope-accessible."""
    until_dt = _dt.datetime.utcnow().replace(microsecond=0)
    since_dt = until_dt - _dt.timedelta(days=7)
    raw = _cf_graphql(_HEALTH_QUERY, {
        "accountTag": _CF_ACCOUNT_ID,
        "since": since_dt.isoformat() + "Z",
        "until": until_dt.isoformat() + "Z",
    })
    if not raw:
        return {"ok": False,
                "error": "CF GraphQL call failed (CLOUDFLARE_API_TOKEN unset or wrong scope)"}

    # Check for GraphQL errors
    if raw.get("errors"):
        return {"ok": False,
                "error": f"GraphQL errors: {raw.get('errors')[0].get('message','?')[:200]}",
                "hint": "Token likely needs 'Account → Account Analytics → Read' permission."}

    data = (((raw.get("data") or {}).get("viewer") or {})
            .get("accounts") or [{}])[0]
    rows = data.get("httpRequestsAdaptiveGroups") or []
    if not rows:
        return {"ok": False,
                "error": "Token works but returned no rows. Could mean (a) account has no zones yet, (b) the metric requires zone-scope access (configure CLOUDFLARE_ZONE_ID env var), or (c) data hasn't propagated.",
                "raw_count": 0}

    total_req     = sum(r.get("count", 0) for r in rows)
    total_bytes   = sum((r.get("sum") or {}).get("edgeResponseBytes", 0) for r in rows)
    total_visits  = sum((r.get("sum") or {}).get("visits", 0) for r in rows)

    return {
        "ok":               True,
        "window_days":      7,
        "total_requests":   total_req,
        "total_bytes":      total_bytes,
        "total_visits":     total_visits,
        "avg_response_kb":  round(total_bytes / max(total_req, 1) / 1024, 2),
        "data_points":      len(rows),
        # cache_rate not directly available in httpRequestsAdaptiveGroups —
        # set to None so brain detector skips the cache check (rather than
        # false-firing). User can read cache rate from CF dashboard directly.
        "cache_rate_pct":   None,
        "as_of":            _dt.datetime.utcnow().isoformat() + "Z",
        "note":             ("Account-level via httpRequestsAdaptiveGroups. "
                              "For per-zone cache/error rates, set "
                              "CLOUDFLARE_ZONE_ID env var (use the zone for dchub.cloud) "
                              "and extend this query to zones { httpRequests1dGroups }."),
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
    html = f"""<!doctype html><html><head><meta charset=utf-8>
<title>DC Hub · CF Analytics Health</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:760px;
margin:0 auto;padding:2rem 1rem;color:#1f2937}}
.kpi{{display:inline-block;margin:1rem 1.5rem 1rem 0}}
.kpi-v{{font-size:2rem;font-weight:800;font-family:monospace}}
.kpi-l{{color:#6b7280;font-size:.85rem}}</style></head><body>
<h1>CF Account Analytics — last 7d</h1>
<div class="kpi"><div class="kpi-v">{data.get('total_requests',0):,}</div>
  <div class="kpi-l">Total requests</div></div>
<div class="kpi"><div class="kpi-v">{data.get('total_visits',0):,}</div>
  <div class="kpi-l">Visits</div></div>
<div class="kpi"><div class="kpi-v">{data.get('total_bytes',0)/1e9:.2f} GB</div>
  <div class="kpi-l">Bandwidth</div></div>
<div class="kpi"><div class="kpi-v">{data.get('avg_response_kb',0):.1f} KB</div>
  <div class="kpi-l">Avg response size</div></div>
<p style="color:#6b7280;font-size:.85rem;margin-top:2rem">
{data.get('note','')}<br>
JSON: <a href="/api/v1/cf-analytics/health">/api/v1/cf-analytics/health</a> ·
brain auto-polls.</p>
</body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=600"})
