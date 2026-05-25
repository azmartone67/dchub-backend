"""Phase ZZZZZ-round40 — AI Compute Capacity Index daily reading.

Item #6 moat-extension: a citable single-number daily reading (think
VIX for data centers). Distribute via JSON + plaintext + SVG badge so
ChatGPT/Claude can cite it, journalists can grab the number, and any
blog can embed the badge.

Wiring (main.py):
    from routes.ai_capacity_daily import ai_capacity_daily_bp
    app.register_blueprint(ai_capacity_daily_bp)
"""
import os
from datetime import datetime, timezone
from flask import Blueprint, jsonify, Response
import psycopg

ai_capacity_daily_bp = Blueprint("ai_capacity_daily", __name__)
NEON_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")


def _compute():
    if not NEON_URL:
        return {"error": "db_unavailable"}
    try:
        with psycopg.connect(NEON_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT
                  COALESCE(SUM(power_mw) FILTER (WHERE status='Operational'        AND power_mw>=100),0)::int AS op_mw,
                  COALESCE(SUM(power_mw) FILTER (WHERE status='Under Construction' AND power_mw>=100),0)::int AS uc_mw,
                  COALESCE(COUNT(*)      FILTER (WHERE status='Operational'        AND power_mw>=100),0)::int AS op_n,
                  COALESCE(COUNT(*)      FILTER (WHERE status='Under Construction' AND power_mw>=100),0)::int AS uc_n
                FROM facilities
            """)
            op_mw, uc_mw, op_n, uc_n = cur.fetchone() or (0, 0, 0, 0)
        idx = min(100, round((op_mw + uc_mw * 0.5) / 1000))
        return {
            "index_value": idx,
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "components": {
                "operational_mw":  op_mw,
                "under_construction_mw": uc_mw,
                "operational_count": op_n,
                "under_construction_count": uc_n,
            },
            "methodology": "operational_mw + 0.5*under_construction_mw, scaled to 0-100",
            "source": "https://dchub.cloud/ai-capacity-index",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"error": "compute_failed", "detail": str(e)}


@ai_capacity_daily_bp.route("/ai-capacity-index/today.json")
def index_json():
    return jsonify(_compute())


@ai_capacity_daily_bp.route("/ai-capacity-index/today.txt")
def index_text():
    d = _compute()
    if "error" in d:
        return Response(f"AI Compute Capacity Index unavailable: {d.get('detail', d['error'])}\n",
                        mimetype="text/plain; charset=utf-8", status=503)
    c = d.get("components", {})
    txt = (
        f"DC Hub AI Compute Capacity Index — {d['as_of']}\n"
        f"Index value: {d['index_value']}/100\n"
        f"\n"
        f"Operational hyperscale (>=100 MW): {c.get('operational_mw',0):,} MW across {c.get('operational_count',0)} facilities\n"
        f"Under construction (>=100 MW):     {c.get('under_construction_mw',0):,} MW across {c.get('under_construction_count',0)} facilities\n"
        f"\n"
        f"Methodology: {d['methodology']}\n"
        f"Source: {d['source']}\n"
    )
    return Response(txt, mimetype="text/plain; charset=utf-8",
                    headers={"Cache-Control": "public, max-age=3600"})


@ai_capacity_daily_bp.route("/ai-capacity-index/badge.svg")
def index_badge():
    d = _compute()
    val = d.get("index_value", "—")
    color = "#10b981" if isinstance(val, int) and val >= 50 else "#f59e0b"
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="220" height="36" role="img" '
        'aria-label="DC Hub AI Capacity Index">'
        '<linearGradient id="b" x2="0" y2="100%">'
        '<stop offset="0" stop-opacity=".1" stop-color="#fff"/>'
        '<stop offset="1" stop-opacity=".1"/></linearGradient>'
        '<rect width="220" height="36" rx="6" fill="#0f172a"/>'
        f'<rect x="148" width="72" height="36" rx="6" fill="{color}"/>'
        '<rect width="220" height="36" rx="6" fill="url(#b)"/>'
        '<g font-family="-apple-system,system-ui,sans-serif" font-size="12" fill="#fff" text-anchor="middle">'
        '<text x="74" y="22">AI CAPACITY INDEX</text>'
        f'<text x="184" y="22" font-weight="700">{val}</text></g></svg>'
    )
    return Response(svg, mimetype="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})
