"""
market_intel_preview.py — free-tier preview of get_market_intel.

Phase ZZZZZ-round44 (2026-05-25). Brain shows get_market_intel is the
top blocked tool with 4,500 paywall hits / 30d (11% of all signals).
Fully blocking it loses ~50 conversions/year vs letting free tier see
ONE market × ONE metric. This endpoint provides a paywall-friendly
preview that returns just enough data to validate the tool's value.

Endpoint: GET /api/v1/market-intel-preview?market=<slug>
Returns: top-line metric + upgrade CTA pointing at /pricing/upgrade
"""
import os, datetime
from contextlib import contextmanager
from flask import Blueprint, jsonify, request
try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

market_intel_preview_bp = Blueprint("market_intel_preview", __name__)

def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()

@market_intel_preview_bp.route("/api/v1/market-intel-preview", methods=["GET"])
def preview():
    market = (request.args.get("market") or "ashburn").strip().lower()
    out = {
        "market": market,
        "preview": True,
        "note": "Free preview shows ONE metric. Full intel (supply/demand, pricing, vacancy, pipeline) requires Developer plan.",
        "upgrade_url": f"https://api.dchub.cloud/pricing/upgrade?tool=get_market_intel",
        "upgrade_tier": "developer",
        "upgrade_price": "$49/mo",
    }
    if not (_pg and _dsn()):
        out["data"] = None
        return jsonify(out), 200
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT city, state, COUNT(*) AS facility_count,
                       COALESCE(SUM(power_mw), 0)::numeric(10,1)::float AS total_mw,
                       COUNT(DISTINCT provider) AS operator_count
                FROM discovered_facilities
                WHERE LOWER(REPLACE(city, ' ', '-')) = %s
                  AND status = 'active'
                GROUP BY city, state LIMIT 1
            """, (market,))
            row = cur.fetchone()
        if not row:
            out["data"] = {"error": "market_not_found",
                            "suggestion": "Try: ashburn, santa-clara, dallas, chicago, atlanta"}
        else:
            out["data"] = {
                "city": row["city"],
                "state": row["state"],
                "facility_count": row["facility_count"],
                "total_mw": float(row["total_mw"]),
                "operator_count": row["operator_count"],
                "_locked_fields": ["supply_demand_score", "vacancy_rate", "avg_price_per_kw",
                                    "pipeline_mw_under_construction", "12mo_growth_rate",
                                    "regulatory_risk_score", "competitor_breakdown"],
                "_locked_count": 7,
            }
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:140]}"
    out["computed_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    return jsonify(out), 200
