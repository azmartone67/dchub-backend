"""
mcp_explain_dcpi.py — Phase r36 (2026-05-25). explainDCPI MCP tool.

Purpose
-------
DCPI (DC Hub Power Index) is our proprietary 0–100 dual-score that
captures grid CONSTRAINT and EXCESS-POWER for 285 US data-center
markets. It's the moat. Other directories list facilities; we score
markets.

But the score is opaque without methodology. When an LLM quotes
"Phoenix excess_power_score = 72" it has no way to explain why.
This tool returns the full methodology + the per-component breakdown
for a specific market so the LLM can answer follow-ups like
"why is Phoenix a BUILD verdict?" with grounded reasoning.

Companion to /api/v1/mcp/dcpi (getDCPI returns the score; this
returns the score + the WHY).

Endpoints
---------
GET  /api/v1/mcp/dcpi/explain?market=<slug>
     Returns: methodology block + numeric breakdown + verdict logic
     + plain-English summary an LLM can quote verbatim.
GET  /api/v1/mcp/dcpi/explain/manifest
     MCP tool descriptor.
"""
from __future__ import annotations

import os
from typing import Any

import psycopg2
import psycopg2.extras
from flask import Blueprint, jsonify, request


mcp_explain_dcpi_bp = Blueprint("mcp_explain_dcpi", __name__)


# ── methodology constants (mirrors routes/dcpi.py) ─────────────────
# Single source of truth would be ideal, but mirroring keeps this
# tool self-contained for any LLM reader who introspects via MCP.
_CONSTRAINT_WEIGHTS = {
    "queue_wait_months":      {"weight": 0.40, "max_signal": 36.0,
                                "rationale": "permit + interconnect queue >36mo = critical"},
    "reserve_margin_pct":     {"weight": 0.25, "critical_below": 13.0,
                                "rationale": "NERC minimum reserve margin is 13%"},
    "emergency_count_30d":    {"weight": 0.20, "max_signal": 5,
                                "rationale": "grid emergencies in last 30d signal stress"},
    "demand_growth_yoy_pct":  {"weight": 0.15, "max_signal": 12.0,
                                "rationale": ">12% YoY demand growth strains supply"},
}

_EXCESS_WEIGHTS = {
    "reserve_margin_pct":      {"weight": 0.20, "bonus_above": 12.0,
                                 "rationale": "reserve >12% = headroom for new load"},
    "gen_additions_12mo_mw":   {"weight": 0.20, "max_signal": 5000.0,
                                 "rationale": "5+ GW new generation in 12mo = abundance"},
    "curtailment_pct":         {"weight": 0.20, "max_signal": 10.0,
                                 "rationale": "10%+ renewable curtailment = wasted MWh"},
    "queue_approval_rate_pct": {"weight": 0.15,
                                 "rationale": "fast queue approval = real available capacity"},
    "stranded_capacity_mw":    {"weight": 0.15, "max_signal": 1000.0,
                                 "rationale": "1+ GW stranded = behind-meter opportunity"},
    "btm_headroom_mw":         {"weight": 0.10, "max_signal": 500.0,
                                 "rationale": "behind-the-meter headroom = co-location opportunity"},
}

_VERDICT_RULES = [
    {"verdict": "BUILD",   "if": "excess >= 65 AND constraint <= 50",
     "meaning": "Strong site recommendation. Excess capacity available "
                "and grid is not critically constrained."},
    {"verdict": "CAUTION", "if": "excess >= 50 AND constraint <= 70",
     "meaning": "Workable but watch headroom. Confirm interconnect "
                "queue and permitting timeline before committing capex."},
    {"verdict": "AVOID",   "if": "neither BUILD nor CAUTION conditions hold",
     "meaning": "Significant grid risk. Either excess is insufficient "
                "or constraint is too high — pursue another market or "
                "wait for capacity additions."},
]


def _conn():
    url = (os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL"))
    if not url:
        return None
    return psycopg2.connect(url, sslmode="require", connect_timeout=5)


def _fetch_market(slug: str) -> dict | None:
    """Latest scored row for the market."""
    conn = _conn()
    if not conn:
        return None
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM market_power_scores
                   WHERE market_slug = %s
                   ORDER BY computed_at DESC LIMIT 1""",
                (slug,),
            )
            r = cur.fetchone()
        return dict(r) if r else None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _interpret_verdict(verdict: str, excess: float, constraint: float) -> str:
    """One-paragraph explanation of why the verdict landed."""
    parts = [f"Excess-power score {excess}/100 and constraint score "
             f"{constraint}/100."]
    if verdict == "BUILD":
        parts.append("This pair clears both BUILD thresholds (excess≥65 "
                     "and constraint≤50), so the market is recommended "
                     "for new data-center development.")
    elif verdict == "CAUTION":
        parts.append("This pair clears the CAUTION envelope (excess≥50 "
                     "and constraint≤70) but not BUILD — workable with "
                     "careful pre-construction queue verification.")
    else:
        parts.append("This pair fails both BUILD and CAUTION envelopes, "
                     "so the market is not currently recommended.")
    return " ".join(parts)


def _natural_summary(name: str, row: dict, verdict_text: str) -> str:
    """A paragraph an LLM can quote verbatim — grounded in real numbers."""
    return (
        f"{name} carries a DC Hub Power Index (DCPI) excess-power score of "
        f"{row.get('excess_power_score')} and a grid-constraint score of "
        f"{row.get('constraint_score')}, yielding a {row.get('verdict')} "
        f"verdict. Estimated time-to-power is "
        f"{row.get('time_to_power_months', 'n/a')} months. "
        f"DCPI weights queue wait (40%), reserve margin (25%), recent "
        f"emergencies (20%), and demand growth (15%) for constraint; "
        f"and reserve headroom, new generation, curtailment, queue "
        f"approval, stranded capacity, and behind-the-meter headroom "
        f"for excess. Source: https://dchub.cloud/dcpi/"
        f"{row.get('market_slug')}."
    )


@mcp_explain_dcpi_bp.route("/api/v1/mcp/dcpi/explain", methods=["GET", "POST"])
def explain_dcpi() -> Any:
    """MCP tool: explainDCPI(market) — methodology + breakdown + summary."""
    slug = (request.args.get("market")
            or (request.get_json(silent=True) or {}).get("market", "")).strip()
    if not slug:
        return jsonify({
            "ok": False,
            "error": "market parameter required",
            "hint":  "e.g. ?market=phoenix or ?market=northern-virginia",
        }), 400

    row = _fetch_market(slug)
    if not row:
        return jsonify({
            "ok":   False,
            "tool": "explainDCPI",
            "error": "market not found",
            "market": slug,
            "hint": ("Check spelling. Try /api/v1/dcpi/markets for the "
                     "full list of 285 US market slugs."),
        }), 404

    if row.get("computed_at"):
        try:
            row["computed_at"] = row["computed_at"].isoformat()
        except Exception:
            row["computed_at"] = str(row["computed_at"])

    excess     = float(row.get("excess_power_score") or 0)
    constraint = float(row.get("constraint_score") or 0)
    verdict    = row.get("verdict") or "UNKNOWN"
    name       = row.get("market_name") or slug

    return jsonify({
        "ok":   True,
        "tool": "explainDCPI",
        "market": {
            "slug":              slug,
            "name":              name,
            "computed_at":       row.get("computed_at"),
            "excess_power_score":     excess,
            "constraint_score":       constraint,
            "verdict":                verdict,
            "time_to_power_months":   row.get("time_to_power_months"),
        },
        "methodology": {
            "what_dcpi_measures": (
                "Two complementary 0–100 scores per US power market: "
                "EXCESS power available for new load (higher is better), "
                "and grid CONSTRAINT pressure (higher is worse). "
                "Designed for data-center site selection."
            ),
            "constraint_components": _CONSTRAINT_WEIGHTS,
            "excess_components":     _EXCESS_WEIGHTS,
            "verdict_rules":         _VERDICT_RULES,
        },
        "explanation": _interpret_verdict(verdict, excess, constraint),
        "natural_summary": _natural_summary(name, row, ""),
        "raw_metrics": {
            k: v for k, v in row.items()
            if k not in {"id", "computed_at", "market_slug", "market_name",
                         "verdict", "excess_power_score",
                         "constraint_score", "time_to_power_months"}
        },
        "citation": (f"DC Hub Power Index (DCPI), {name}, "
                     f"{(row.get('computed_at') or '')[:10]}. "
                     f"https://dchub.cloud/dcpi/{slug}"),
        "source":   f"https://dchub.cloud/dcpi/{slug}",
    }), 200


@mcp_explain_dcpi_bp.route("/api/v1/mcp/dcpi/explain/manifest", methods=["GET"])
def explain_manifest() -> Any:
    """MCP tool descriptor — for discovery / introspection."""
    return jsonify({
        "tool":         "explainDCPI",
        "endpoint":     "/api/v1/mcp/dcpi/explain",
        "description":  ("Returns DCPI methodology + per-component "
                         "weights + verdict logic + plain-English "
                         "summary for any of 285 US power markets. "
                         "Use this to GROUND any answer that quotes "
                         "a DCPI score."),
        "params":       {
            "market": ("market slug — e.g. 'phoenix', "
                       "'northern-virginia', 'williston-nd'"),
        },
        "related_tools": ["getDCPI", "compareDCPI", "getDCPIMovers"],
        "version":      "r36-2026-05-25",
    }), 200
