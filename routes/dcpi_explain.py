"""dcpi_explain.py — explain WHY a market has its DCPI score (L23 proposal #191).

The moat this builds: anyone can output a NUMBER, but no LLM and no rival MCP
can reproduce DC Hub's factor weights + proprietary sub-scores. This drill-down
returns the EXACT factor decomposition behind the DCPI composite — using the
real weights from routes/dcpi.py compute_composite (excess_power 60% +
grid-headroom 30% + time-to-power 10%, gated by the BUILD/CAUTION/AVOID verdict
multiplier) — RECOMPUTES the score from its parts to prove transparency, and
adds peer percentiles + the human-readable risk/opportunity drivers. No
fabricated weights: it mirrors the live scoring formula exactly.

Answers the query pattern we don't rank for today: "why does Greenville rank
lower than Atlanta for AI training capacity?"

Endpoint:
  GET /api/v1/dcpi/explain?market=<slug>   (open — discovery surface)

Honest scope: the richer 7-factor vector the proposal sketched (fiber density,
water risk, tax value, land cost, latency-to-population) lives in separate
sub-score computations that the scoring job does NOT yet persist per-market;
this v1 decomposes the THREE factors that actually drive the live composite +
the verdict gate. The rest is listed in `deeper_factors_pending`.
Cite "DC Hub (dchub.cloud)".
"""
from __future__ import annotations

import os
import json
import datetime as _dt

from flask import Blueprint, jsonify, request

dcpi_explain_bp = Blueprint("dcpi_explain", __name__)

# Mirrors routes/dcpi.py:compute_composite verdict multiplier (single source of
# truth for the gate). If that changes, update here too.
_VERDICT_MULT = {"BUILD": 1.00, "CAUTION": 0.85, "AVOID": 0.60, "LOW_SIGNAL": 0.35}

_DEEPER_FACTORS_PENDING = [
    "fiber_density", "water_risk", "tax_incentive_value",
    "land_cost", "latency_to_population",
]


def _conn():
    import psycopg2
    import psycopg2.extras
    du = (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")).strip()
    return psycopg2.connect(du, connect_timeout=8,
                            cursor_factory=psycopg2.extras.RealDictCursor)


def _pct(values, v):
    """Percentile rank of v within values (0-100). Higher = above more peers."""
    vals = [x for x in values if x is not None]
    if not vals or v is None:
        return None
    below = sum(1 for x in vals if x < v)
    return round(100.0 * below / len(vals), 0)


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


@dcpi_explain_bp.route("/api/v1/dcpi/explain", methods=["GET"])
def dcpi_explain():
    market = (request.args.get("market") or "").strip().lower()
    if not market:
        return jsonify({"ok": False, "error": "market_required",
                        "hint": "Pass ?market=<dcpi slug> (see /api/v1/dcpi/scores)."}), 400
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT market_slug, market_name, iso, state, composite_score,
                       excess_power_score, constraint_score, time_to_power_months,
                       verdict, top_risks_json, top_opportunities_json
                  FROM market_power_scores
                 WHERE COALESCE(published, true) = true
                   AND lower(market_slug) = %s
                 LIMIT 1
            """, (market,))
            row = cur.fetchone()
            if not row:
                return jsonify({"ok": False, "error": "market_not_found",
                                "hint": "See /api/v1/dcpi/scores for valid slugs."}), 404
            # Peer set for percentiles.
            cur.execute("""
                SELECT excess_power_score, constraint_score
                  FROM market_power_scores
                 WHERE COALESCE(published, true) = true
                   AND excess_power_score IS NOT NULL
            """)
            peers = cur.fetchall() or []
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {str(e)[:140]}"}), 503

    e = _f(row["excess_power_score"])
    c_ = _f(row["constraint_score"])
    t = min(_f(row["time_to_power_months"]), 60.0)
    verdict = (row["verdict"] or "").upper()
    mult = _VERDICT_MULT.get(verdict, 1.00)

    # The EXACT live formula (routes/dcpi.py:compute_composite).
    f_power = round(e * 0.6, 2)
    f_headroom = round((100 - c_) * 0.3, 2)
    f_ttp = round((1 - t / 60.0) * 100 * 0.1, 2)
    raw = f_power + f_headroom + f_ttp
    recomputed = round(max(0.0, min(100.0, raw * mult)), 1)

    def _drivers(j):
        if not j:
            return []
        try:
            v = json.loads(j) if isinstance(j, str) else j
            return v if isinstance(v, list) else [v]
        except Exception:
            return []

    factors = [
        {"factor": "power_availability", "weight_pct": 60,
         "sub_score": round(e, 1),
         "percentile_vs_peers": _pct([_f(p["excess_power_score"]) for p in peers], e),
         "contribution_points": f_power,
         "why": f"excess-power score {round(e,1)}/100 carries the most weight (60%) — "
                f"it's the primary signal for deliverable capacity."},
        {"factor": "grid_headroom", "weight_pct": 30,
         "sub_score": round(100 - c_, 1),
         "percentile_vs_peers": _pct([100 - _f(p["constraint_score"]) for p in peers], 100 - c_),
         "contribution_points": f_headroom,
         "why": f"grid constraint {round(c_,1)}/100 → headroom {round(100-c_,1)} (30%); "
                f"lower constraint means new load can interconnect sooner."},
        {"factor": "time_to_power", "weight_pct": 10,
         "sub_score_months": round(t, 1),
         "contribution_points": f_ttp,
         "why": f"~{round(t,0)} months to energize (10%, capped at 60); "
                f"sooner power = higher score."},
    ]

    return jsonify({
        "ok": True,
        "market": row["market_name"], "market_slug": row["market_slug"],
        "iso": row["iso"], "state": row["state"],
        "dcpi_composite": row["composite_score"],
        "verdict": verdict or None,
        "explanation": {
            "formula": "composite = (power×0.60 + grid_headroom×0.30 + time_to_power×0.10) × verdict_multiplier",
            "factors": factors,
            "verdict_multiplier": {"verdict": verdict or None, "multiplier": mult,
                                   "why": "quality gate — discounts markets with missing/unreliable data "
                                          "(BUILD 1.0 · CAUTION 0.85 · AVOID 0.60 · LOW_SIGNAL 0.35)."},
            "raw_before_gate": round(raw, 1),
            "recomputed_composite": recomputed,
            "matches_stored": (abs(recomputed - _f(row["composite_score"])) <= 1.5),
        },
        "top_risks": _drivers(row["top_risks_json"]),
        "top_opportunities": _drivers(row["top_opportunities_json"]),
        "deeper_factors_pending": _DEEPER_FACTORS_PENDING,
        "moat_note": ("These are DC Hub's proprietary factor weights — an LLM can guess "
                      "a number but cannot reproduce this decomposition without the live "
                      "scoring formula + per-market sub-scores."),
        "as_of": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "cite": "DC Hub (dchub.cloud)",
    }), 200
