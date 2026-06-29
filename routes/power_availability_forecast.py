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
    "quarterly_deliverable_mw (8q projection) — needs per-project queue modeling",
    "per_market_queue_mw — queue depth is ISO-level; per-market project geometry pending",
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
    queue_by_iso = {}
    err = None
    try:
        with _conn() as c, c.cursor() as cur:
            if market:
                cur.execute("""
                    SELECT market_slug, market_name, iso, excess_power_score,
                           constraint_score, verdict
                      FROM market_power_scores
                     WHERE COALESCE(published, true) = true
                       AND lower(market_slug) = %s
                     LIMIT 1
                """, (market,))
            else:
                # Rank by readiness: most excess power, least constrained first.
                cur.execute("""
                    SELECT market_slug, market_name, iso, excess_power_score,
                           constraint_score, verdict
                      FROM market_power_scores
                     WHERE COALESCE(published, true) = true
                       AND excess_power_score IS NOT NULL
                     ORDER BY excess_power_score DESC NULLS LAST,
                              constraint_score ASC NULLS LAST
                     LIMIT %s
                """, (limit,))
            rows = cur.fetchall() or []
            # 2026-06-28: wire in the LIVE interconnection-queue ingest
            # (iso_queue_snapshots, daily cron). Latest snapshot per ISO →
            # queued total GW + the data-center-specific GW. This closes the
            # interconnection_queue_mw data gap the v1 forecast flagged.
            cur.execute("""
                SELECT iso, as_of, queued_load_total_gw, queued_load_data_center_gw
                  FROM iso_queue_snapshots
                 WHERE (iso, as_of) IN (
                    SELECT iso, MAX(as_of) FROM iso_queue_snapshots GROUP BY iso)
            """)
            for q in cur.fetchall() or []:
                queue_by_iso[(q["iso"] or "").upper()] = q
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
        iso = (r.get("iso") or "").upper()
        q = queue_by_iso.get(iso)
        interconnection = None
        if q:
            total_gw = q.get("queued_load_total_gw")
            dc_gw = q.get("queued_load_data_center_gw")
            interconnection = {
                "iso": iso,
                "queued_total_mw": round(float(total_gw) * 1000, 1) if total_gw is not None else None,
                "queued_data_center_mw": round(float(dc_gw) * 1000, 1) if dc_gw is not None else None,
                "as_of": str(q.get("as_of")) if q.get("as_of") else None,
                "source": "ISO interconnection queue (dchub.cloud/interconnection-queues)",
            }
        forecasts.append({
            "market_slug":   r["market_slug"],
            "market":        r["market_name"],
            "iso":           iso or None,
            "excess_power_score":  r["excess_power_score"],
            "constraint_score":    r["constraint_score"],
            "verdict":             r["verdict"],
            "availability_outlook": _outlook(r["excess_power_score"]),
            "power_to_pad_lead_time_months": _lead_time_months(r["constraint_score"]),
            # NOW queue-backed at the ISO level (per-market project geometry is
            # the remaining gap — see data_gaps).
            "interconnection_queue": interconnection,
            "confidence": ("moderate (DCPI signals + ISO interconnection-queue depth)"
                           if interconnection else
                           "low (DCPI-derived; no ISO queue match)"),
            "as_of": now,
            "source": "DC Hub Power Index (dchub.cloud/dcpi)",
        })

    return jsonify({
        "ok": True,
        "version": "v2-queue-backed",
        "forecast_basis": ("DCPI power signals + a heuristic power-to-pad lead-time, "
                           "NOW backed by live ISO interconnection-queue depth (total + "
                           "data-center MW). Per-market project geometry + the forward "
                           "quarterly deliverable-MW projection remain — see data_gaps."),
        "data_gaps": _DATA_GAPS,
        "count": len(forecasts),
        "forecasts": forecasts if not market else (forecasts[0] if forecasts else None),
        "as_of": now,
        "cite": "DC Hub (dchub.cloud)",
    }), 200
