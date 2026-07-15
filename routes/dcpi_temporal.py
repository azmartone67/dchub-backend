"""dcpi_temporal.py — DCPI history + change-feed (v1).

Ships two of the brain's most-repeated L23 capability proposals
(get_dcpi_history #1131, track_dcpi_changes #1138/#1122). The brain's
thesis: every current tool returns a point-in-time SNAPSHOT an LLM can
memorize and a competitor can scrape — the durable moat is TEMPORAL data
(history + change-feed + forecast). This is the history/change half;
power-availability-forecast (#404) is the forecast half (already shipped).

The L23 challenger flagged a real risk: history is only meaningful if the
snapshot record is DENSE. So these are DEPTH-HONEST — every response
carries `snapshot_dates` (how many distinct days exist) and the actual
date range, and never fabricates a trend over a single point. As the
daily dcpi snapshot cron accumulates, the tools enrich automatically.

Data source: dcpi_daily_snapshots (snapshot_date, market_slug, market_name,
excess_power_score, constraint_score, verdict) — written daily by
routes/dcpi.py.

Endpoints (open — discovery surfaces, cite "DC Hub (dchub.cloud)"):
  GET /api/v1/dcpi/history?market=<slug>&days=90   per-market time-series
  GET /api/v1/dcpi/changes?days=30&limit=25        markets that moved, newest vs prior snapshot
"""
from __future__ import annotations

import os
import datetime as _dt

from flask import Blueprint, jsonify, request

dcpi_temporal_bp = Blueprint("dcpi_temporal", __name__)


def _conn():
    import psycopg2
    import psycopg2.extras
    du = (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")).strip()
    return psycopg2.connect(du, connect_timeout=8,
                            cursor_factory=psycopg2.extras.RealDictCursor)


def _depth_meta(cur) -> dict:
    """How dense is the snapshot record overall? (depth-honesty)."""
    cur.execute("SELECT COUNT(DISTINCT snapshot_date) AS d, MIN(snapshot_date) AS lo, "
                "MAX(snapshot_date) AS hi FROM dcpi_daily_snapshots")
    r = cur.fetchone() or {}
    return {"snapshot_dates": int(r.get("d") or 0),
            "earliest": str(r.get("lo")) if r.get("lo") else None,
            "latest": str(r.get("hi")) if r.get("hi") else None}


# AUTO-REPAIR: duplicate route '/api/v1/dcpi/history' also in routes/dcpi.py:5569 — review and remove one
@dcpi_temporal_bp.route("/api/v1/dcpi/history", methods=["GET"])
def dcpi_history():
    """Per-market DCPI time-series — excess/constraint/verdict over time."""
    market = (request.args.get("market") or "").strip().lower()
    if not market:
        # r-hist-shadow (2026-07-11, frontend#1063): this blueprint registers
        # BEFORE routes/dcpi.py's dcpi_bp in main.py (~line 1869 vs ~31574),
        # so this rule SHADOWS the legacy all-markets handler
        # (dcpi.api_history) on the identical path. The /dcpi landing page's
        # 30-day chart fetches /api/v1/dcpi/history with NO params and
        # expects {series:{slug:{name,data}}} — the hard 400 here broke that
        # chart on every headless-QA run. No market param = the legacy
        # all-markets contract -> delegate (its anon paywall nulling stays
        # intact). ?market=<slug> keeps the per-market temporal contract
        # documented in AGENTS.md / skill.json.
        from routes.dcpi import api_history as _legacy_all_markets_history
        return _legacy_all_markets_history()
    try:
        days = max(1, min(730, int(request.args.get("days", 90))))
    except (TypeError, ValueError):
        days = 90
    try:
        with _conn() as c, c.cursor() as cur:
            meta = _depth_meta(cur)
            cur.execute("""
                SELECT snapshot_date, market_name, excess_power_score,
                       constraint_score, verdict
                  FROM dcpi_daily_snapshots
                 WHERE lower(market_slug) = %s
                   AND snapshot_date >= CURRENT_DATE - %s::int
                 ORDER BY snapshot_date ASC
            """, (market, days))
            rows = cur.fetchall() or []
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {str(e)[:140]}"}), 503

    if not rows:
        return jsonify({"ok": False, "error": "no_history",
                        "market": market, "snapshot_record": meta,
                        "hint": "No snapshots for this market in range. The daily "
                                "snapshot cron accumulates history going forward."}), 404

    series = [{
        "date": str(r["snapshot_date"]),
        "excess_power_score": r["excess_power_score"],
        "constraint_score": r["constraint_score"],
        "verdict": r["verdict"],
    } for r in rows]
    first, last = rows[0], rows[-1]

    def _delta(a, b):
        try:
            return round(float(b) - float(a), 2)
        except (TypeError, ValueError):
            return None

    return jsonify({
        "ok": True,
        "market": last["market_name"],
        "market_slug": market,
        "points": len(series),
        "range": {"from": series[0]["date"], "to": series[-1]["date"]},
        "net_change": {
            "excess_power_score": _delta(first["excess_power_score"], last["excess_power_score"]),
            "constraint_score": _delta(first["constraint_score"], last["constraint_score"]),
            "verdict_now": last["verdict"], "verdict_then": first["verdict"],
        },
        "series": series,
        "snapshot_record": meta,            # depth-honesty: how deep history goes
        "as_of": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "cite": "DC Hub (dchub.cloud)",
    }), 200


def _linfit(xs, ys):
    """Least-squares slope+intercept. Returns (slope_per_day, intercept, resid_std)."""
    n = len(xs)
    if n < 2:
        return 0.0, (ys[-1] if ys else 0.0), 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0, my, 0.0
    slope = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / den
    intercept = my - slope * mx
    resid = [ys[i] - (slope * xs[i] + intercept) for i in range(n)]
    var = sum(r * r for r in resid) / n
    return slope, intercept, var ** 0.5


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


@dcpi_temporal_bp.route("/api/v1/dcpi/forecast", methods=["GET"])
def dcpi_forecast():
    """Forward DCPI-score projection (L23 proposal get_dcpi_forecast #1139).

    HONEST: this is a TREND EXTRAPOLATION over the available snapshot history,
    not a guarantee. Projects excess_power_score / constraint_score forward by
    quarter with a confidence band that widens with horizon. Distinct from
    /api/v1/power-availability-forecast (#404), which forecasts deliverable MW
    timing; this forecasts the DCPI score trajectory."""
    market = (request.args.get("market") or "").strip().lower()
    if not market:
        return jsonify({"ok": False, "error": "market_required",
                        "hint": "Pass ?market=<dcpi slug> (see /api/v1/dcpi/scores)."}), 400
    try:
        horizon_q = max(1, min(8, int(request.args.get("horizon", 4))))  # quarters
    except (TypeError, ValueError):
        horizon_q = 4
    try:
        with _conn() as c, c.cursor() as cur:
            meta = _depth_meta(cur)
            cur.execute("""
                SELECT snapshot_date, market_name, excess_power_score, constraint_score
                  FROM dcpi_daily_snapshots
                 WHERE lower(market_slug) = %s
                 ORDER BY snapshot_date ASC
            """, (market,))
            rows = cur.fetchall() or []
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {str(e)[:140]}"}), 503

    pts = [(r["snapshot_date"], r["excess_power_score"], r["constraint_score"]) for r in rows
           if r["excess_power_score"] is not None]
    if len(pts) < 3:
        return jsonify({"ok": False, "error": "insufficient_history",
                        "market": market, "snapshot_record": meta,
                        "points_available": len(pts),
                        "hint": "Need ≥3 snapshots to extrapolate a trend. The daily "
                                "snapshot cron is accumulating; forecast unlocks as "
                                "history deepens."}), 422

    d0 = pts[0][0]
    xs = [(p[0] - d0).days for p in pts]
    exc = [float(p[1]) for p in pts]
    con = [float(p[2]) if p[2] is not None else 0.0 for p in pts]
    span_days = xs[-1] - xs[0] or 1
    e_slope, e_int, e_resid = _linfit(xs, exc)
    c_slope, c_int, c_resid = _linfit(xs, con)
    last_x = xs[-1]

    projection = []
    for q in range(1, horizon_q + 1):
        days_ahead = q * 91
        fx = last_x + days_ahead
        # band widens with horizon AND with how far we extrapolate beyond observed span
        widen = (1.0 + days_ahead / max(span_days, 1)) ** 0.5
        e_band = round((e_resid + 1.0) * widen, 1)
        c_band = round((c_resid + 1.0) * widen, 1)
        e_val = _clamp(e_slope * fx + e_int)
        c_val = _clamp(c_slope * fx + c_int)
        projection.append({
            "quarter_out": q,
            "excess_power_score": round(e_val, 1),
            "excess_power_band": [round(_clamp(e_val - e_band), 1), round(_clamp(e_val + e_band), 1)],
            "constraint_score": round(c_val, 1),
            "constraint_band": [round(_clamp(c_val - c_band), 1), round(_clamp(c_val + c_band), 1)],
        })

    trend = ("improving" if e_slope > 0.02 else "declining" if e_slope < -0.02 else "flat")
    return jsonify({
        "ok": True,
        "market": pts[-1] and rows[-1]["market_name"],
        "market_slug": market,
        "method": "linear trend extrapolation (least-squares) over snapshot history",
        "basis": {
            "history_points": len(pts),
            "history_span_days": span_days,
            "excess_power_slope_per_day": round(e_slope, 4),
            "current_excess_power_score": round(exc[-1], 1),
            "trend": trend,
        },
        "horizon_quarters": horizon_q,
        "projection": projection,
        "caveat": ("Trend extrapolation, NOT a guarantee — confidence band widens with "
                   "horizon. Short history → wide bands. Re-query as snapshots deepen."),
        "snapshot_record": meta,
        "as_of": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "cite": "DC Hub (dchub.cloud)",
    }), 200


@dcpi_temporal_bp.route("/api/v1/dcpi/changes", methods=["GET"])
def dcpi_changes():
    """Change-feed: markets that moved between the two most recent snapshot dates."""
    try:
        limit = max(1, min(100, int(request.args.get("limit", 25))))
    except (TypeError, ValueError):
        limit = 25
    try:
        with _conn() as c, c.cursor() as cur:
            meta = _depth_meta(cur)
            if (meta.get("snapshot_dates") or 0) < 2:
                return jsonify({"ok": True, "changes": [], "count": 0,
                                "snapshot_record": meta,
                                "note": "Need ≥2 snapshot dates to compute deltas. "
                                        "The daily snapshot cron is accumulating; "
                                        "the change-feed fills in as days pass."}), 200
            # Two most recent distinct snapshot dates.
            cur.execute("SELECT DISTINCT snapshot_date FROM dcpi_daily_snapshots "
                        "ORDER BY snapshot_date DESC LIMIT 2")
            dates = [r["snapshot_date"] for r in cur.fetchall()]
            new_d, old_d = dates[0], dates[1]
            cur.execute("""
                SELECT n.market_slug, n.market_name,
                       o.excess_power_score AS old_excess, n.excess_power_score AS new_excess,
                       o.constraint_score   AS old_constraint, n.constraint_score AS new_constraint,
                       o.verdict AS old_verdict, n.verdict AS new_verdict
                  FROM dcpi_daily_snapshots n
                  JOIN dcpi_daily_snapshots o
                    ON o.market_slug = n.market_slug AND o.snapshot_date = %s
                 WHERE n.snapshot_date = %s
                   AND ( n.excess_power_score IS DISTINCT FROM o.excess_power_score
                      OR n.constraint_score   IS DISTINCT FROM o.constraint_score
                      OR n.verdict            IS DISTINCT FROM o.verdict )
            """, (old_d, new_d))
            rows = cur.fetchall() or []
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {str(e)[:140]}"}), 503

    def _d(a, b):
        try:
            return round(float(b) - float(a), 2)
        except (TypeError, ValueError):
            return None

    changes = []
    for r in rows:
        changes.append({
            "market": r["market_name"], "market_slug": r["market_slug"],
            "excess_power_delta": _d(r["old_excess"], r["new_excess"]),
            "constraint_delta": _d(r["old_constraint"], r["new_constraint"]),
            "verdict_change": (None if r["old_verdict"] == r["new_verdict"]
                               else {"from": r["old_verdict"], "to": r["new_verdict"]}),
        })
    # Biggest movers first (by absolute excess-power delta).
    changes.sort(key=lambda x: abs(x["excess_power_delta"] or 0), reverse=True)

    return jsonify({
        "ok": True,
        "between": {"from": str(old_d), "to": str(new_d)},
        "count": len(changes),
        "changes": changes[:limit],
        "snapshot_record": meta,
        "as_of": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "cite": "DC Hub (dchub.cloud)",
    }), 200
