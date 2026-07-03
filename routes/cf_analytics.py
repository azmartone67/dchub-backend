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

Requires Cloudflare API token(s):
  • Account rollup (requests/bytes/visits): CLOUDFLARE_API_TOKEN with
    Account → Account Analytics → Read (already provisioned).
  • Per-zone cache-hit-rate + error rates: a token with
    Zone → Analytics → Read for the dchub.cloud zone. Provide it as
    CF_ANALYTICS_READ_TOKEN (preferred) — falls back to
    CLOUDFLARE_API_TOKEN if that one carries the zone scope.
  IMPORTANT: this is a LIVE Flask route (runs on Railway) — the token
  must live in the RAILWAY env, not GitHub secrets (secrets only reach
  Actions workflows, never this process).

Endpoints:
  GET /api/v1/cf-analytics/health      — JSON: current cache/errors/etc.
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
# Dedicated read-only token for the ZONE-scoped cache/error query. The
# account token above lacks 'zone.analytics.read', so per-zone cache rate
# needs this; prefer the dedicated token, fall back to the account one.
_CF_ZONE_TOKEN = ((os.environ.get("CF_ANALYTICS_READ_TOKEN") or "").strip()
                  or _CF_API_TOKEN)


def _cf_graphql(query: str, variables: dict, token: str | None = None) -> dict | None:
    """POST to CF GraphQL Analytics endpoint. Returns parsed JSON or None.
    `token` overrides the default account token (used for the zone query)."""
    tok = (token or _CF_API_TOKEN)
    if not tok:
        return None
    try:
        import requests
        r = requests.post(
            "https://api.cloudflare.com/client/v4/graphql",
            headers={
                "Authorization": f"Bearer {tok}",
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


# Zone-scoped cache-hit-rate + error breakdown. Needs Zone→Analytics→Read
# (the account token lacks it — see _CF_ZONE_TOKEN). httpRequests1dGroups is
# the zone-scope group; sum.cachedRequests/requests give the cache rate,
# responseStatusMap gives the per-status counts for 4xx/5xx.
_ZONE_QUERY = """
query ZoneCache($zoneTag: String!, $since: Date!) {
  viewer {
    zones(filter: {zoneTag: $zoneTag}) {
      httpRequests1dGroups(
        filter: {date_geq: $since}
        limit: 30
      ) {
        sum {
          requests
          cachedRequests
          responseStatusMap { edgeResponseStatus requests }
        }
      }
    }
  }
}
"""


def _gather_zone_cache() -> dict:
    """Per-zone cache-hit-rate + 4xx/5xx rate over 7d. FAIL-SOFT: any
    missing token / scope / zone id returns {} so the account rollup still
    renders (cache_rate stays None rather than erroring the endpoint)."""
    if not _CF_ZONE_ID or not _CF_ZONE_TOKEN:
        return {}
    since = (_dt.datetime.utcnow().date() - _dt.timedelta(days=7)).isoformat()
    raw = _cf_graphql(_ZONE_QUERY, {"zoneTag": _CF_ZONE_ID, "since": since},
                      token=_CF_ZONE_TOKEN)
    if not raw or raw.get("errors"):
        # Most common cause: token lacks zone.analytics.read. Log once, degrade.
        if raw and raw.get("errors"):
            logger.info("CF zone cache query unavailable: %s",
                        (raw["errors"][0].get("message", "?"))[:160])
        return {}
    zones = (((raw.get("data") or {}).get("viewer") or {}).get("zones") or [])
    rows = (zones[0].get("httpRequests1dGroups") if zones else None) or []
    if not rows:
        return {}
    total = sum((r.get("sum") or {}).get("requests", 0) for r in rows)
    cached = sum((r.get("sum") or {}).get("cachedRequests", 0) for r in rows)
    err4xx = err5xx = 0
    for r in rows:
        for s in ((r.get("sum") or {}).get("responseStatusMap") or []):
            code = s.get("edgeResponseStatus", 0)
            if 400 <= code < 500:
                err4xx += s.get("requests", 0)
            elif code >= 500:
                err5xx += s.get("requests", 0)
    if total <= 0:
        return {}
    return {
        "cache_rate_pct": round(100.0 * cached / total, 2),
        "error_4xx_pct":  round(100.0 * err4xx / total, 2),
        "error_5xx_pct":  round(100.0 * err5xx / total, 2),
        "zone_requests_7d": total,
        "zone_cache_source": ("CF_ANALYTICS_READ_TOKEN"
                              if os.environ.get("CF_ANALYTICS_READ_TOKEN")
                              else "CLOUDFLARE_API_TOKEN"),
    }


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

    out = {
        "ok":               True,
        "window_days":      7,
        "total_requests":   total_req,
        "total_bytes":      total_bytes,
        "total_visits":     total_visits,
        "avg_response_kb":  round(total_bytes / max(total_req, 1) / 1024, 2),
        "data_points":      len(rows),
        # Filled from the zone query below when a zone-analytics token is
        # present; stays None otherwise so the brain skips the cache check
        # rather than false-firing.
        "cache_rate_pct":   None,
        "as_of":            _dt.datetime.utcnow().isoformat() + "Z",
    }
    # Additive, fail-soft: light up cache-hit-rate + error rates if a
    # zone-analytics token is wired (CF_ANALYTICS_READ_TOKEN in Railway env).
    zone = _gather_zone_cache()
    out.update(zone)
    out["note"] = (
        "Account rollup via httpRequestsAdaptiveGroups; per-zone cache/error "
        "rates via httpRequests1dGroups."
        if zone else
        "Account-level only. cache_rate_pct is null until CF_ANALYTICS_READ_TOKEN "
        "(Zone→Analytics→Read for dchub.cloud) is set in the RAILWAY env — GitHub "
        "secrets do not reach this live route."
    )
    return out


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
<div class="kpi"><div class="kpi-v">{(f"{data['cache_rate_pct']:.1f}%" if data.get('cache_rate_pct') is not None else '—')}</div>
  <div class="kpi-l">Cache hit rate</div></div>
<div class="kpi"><div class="kpi-v">{(f"{data['error_5xx_pct']:.2f}%" if data.get('error_5xx_pct') is not None else '—')}</div>
  <div class="kpi-l">5xx rate</div></div>
<p style="color:#6b7280;font-size:.85rem;margin-top:2rem">
{data.get('note','')}<br>
JSON: <a href="/api/v1/cf-analytics/health">/api/v1/cf-analytics/health</a> ·
brain auto-polls.</p>
</body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=600"})
