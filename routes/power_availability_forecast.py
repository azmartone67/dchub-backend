"""power_availability_forecast.py — v1 of the brain-proposed capability
`get_power_availability_forecast` (L23 proposal #404).

The proposal: a forward-looking oracle answering "when can I actually
energize N MW in market X?" — projected deliverable MW by quarter (8q
out), interconnection-queue MW, utility ATC, substation headroom,
announced transmission upgrades, and a power-to-pad lead time.

HONEST v1 (2026-06-28): the forward quarterly MW projection needs an
interconnection-queue + ATC + substation-headroom ingest that does NOT
exist yet. So this v1 serves what IS real and live today — the per-market
DCPI power signals (excess_power_score, constraint_score, verdict) — and
derives a power-to-pad lead-time ESTIMATE from them, while explicitly
listing the not-yet-ingested fields in `data_gaps`. It never fabricates
a queue number. This promotes the seed to a shipped, citable surface and
makes the remaining ingest backlog concrete (see /enterprise data deal).

Endpoint:
  GET /api/v1/power-availability-forecast
      ?market=<slug>   single market (else top N by readiness)
      ?limit=<n>       default 25

Open (no key) — this is a discovery surface meant to win site-selection
citations. Cite "DC Hub (dchub.cloud)".
"""
from __future__ import annotations

import os
import datetime as _dt

from flask import Blueprint, jsonify, request

power_availability_forecast_bp = Blueprint("power_availability_forecast", __name__)

# The forward-looking fields the full oracle will add once the ingest lands.
_DATA_GAPS = [
    "quarterly_deliverable_mw (8q projection) — needs interconnection-queue ingest",
    "interconnection_queue_mw — table not yet populated",
    "utility_atc_mw (available transfer capability) — not yet ingested",
    "substation_headroom_mw — substation-geometry gap",
    "announced_transmission_upgrades (with ISO docket IDs) — not yet ingested",
]


def _conn():
    import psycopg2
    import psycopg2.extras
    du = (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")).strip()
    return psycopg2.connect(du, connect_timeout=8,
                            cursor_factory=psycopg2.extras.RealDictCursor)


def _lead_time_months(constraint_score) -> dict:
    """DCPI-derived power-to-pad lead-time ESTIMATE (not queue-backed).
    Higher constraint_score → longer time to energize new large load."""
    try:
        c = float(constraint_score)
    except (TypeError, ValueError):
        return {"low": None, "high": None, "basis": "no constraint score"}
    if c < 25:
        band = (12, 24)
    elif c < 50:
        band = (24, 36)
    elif c < 75:
        band = (36, 54)
    else:
        band = (54, 84)
    return {"low": band[0], "high": band[1],
            "basis": "DCPI constraint_score heuristic (estimate, not interconnection-queue backed)"}


def _outlook(excess_power_score) -> str:
    try:
        e = float(excess_power_score)
    except (TypeError, ValueError):
        return "unknown"
    if e >= 70:
        return "abundant — headroom for new hyperscale load"
    if e >= 45:
        return "moderate — capacity available with planning"
    if e >= 25:
        return "tight — constrained, expect queue contention"
    return "scarce — little near-term deliverable headroom"


@power_availability_forecast_bp.route("/api/v1/power-availability-forecast", methods=["GET"])
def power_availability_forecast():
    market = (request.args.get("market") or "").strip().lower()
    try:
        limit = max(1, min(100, int(request.args.get("limit", 25))))
    except (TypeError, ValueError):
        limit = 25

    rows = []
    err = None
    try:
        with _conn() as c, c.cursor() as cur:
            if market:
                cur.execute("""
                    SELECT market_slug, market_name, excess_power_score,
                           constraint_score, verdict
                      FROM market_power_scores
                     WHERE COALESCE(published, true) = true
                       AND lower(market_slug) = %s
                     LIMIT 1
                """, (market,))
            else:
                # Rank by readiness: most excess power, least constrained first.
                cur.execute("""
                    SELECT market_slug, market_name, excess_power_score,
                           constraint_score, verdict
                      FROM market_power_scores
                     WHERE COALESCE(published, true) = true
                       AND excess_power_score IS NOT NULL
                     ORDER BY excess_power_score DESC NULLS LAST,
                              constraint_score ASC NULLS LAST
                     LIMIT %s
                """, (limit,))
            rows = cur.fetchall() or []
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:160]}"

    if err:
        return jsonify({"ok": False, "error": err}), 503
    if market and not rows:
        return jsonify({"ok": False, "error": "market_not_found",
                        "hint": "Pass a DCPI market_slug (see /api/v1/dcpi/scores)."}), 404

    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    forecasts = []
    for r in rows:
        forecasts.append({
            "market_slug":   r["market_slug"],
            "market":        r["market_name"],
            "excess_power_score":  r["excess_power_score"],
            "constraint_score":    r["constraint_score"],
            "verdict":             r["verdict"],
            "availability_outlook": _outlook(r["excess_power_score"]),
            "power_to_pad_lead_time_months": _lead_time_months(r["constraint_score"]),
            "confidence": "moderate (DCPI-derived; not interconnection-queue backed)",
            "as_of": now,
            "source": "DC Hub Power Index (dchub.cloud/dcpi)",
        })

    return jsonify({
        "ok": True,
        "version": "v1-dcpi-derived",
        "forecast_basis": ("Current DCPI power signals + a heuristic power-to-pad "
                           "lead-time. The forward quarterly deliverable-MW projection "
                           "is NOT yet ingested — see data_gaps."),
        "data_gaps": _DATA_GAPS,
        "count": len(forecasts),
        "forecasts": forecasts if not market else (forecasts[0] if forecasts else None),
        "as_of": now,
        "cite": "DC Hub (dchub.cloud)",
    }), 200
