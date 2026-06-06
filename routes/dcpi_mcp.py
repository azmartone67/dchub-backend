"""Phase 109D — DCPI exposed as MCP tools for AI agents."""
from flask import Blueprint, request, jsonify
import os, psycopg2, psycopg2.extras

dcpi_mcp_bp = Blueprint("dcpi_mcp", __name__)

def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    return psycopg2.connect(db, sslmode="require")

@dcpi_mcp_bp.route("/api/v1/mcp/dcpi", methods=["GET", "POST"])
def get_dcpi():
    """MCP tool: getDCPI(market_slug) — returns full DCPI scoring for a market."""
    slug = request.args.get("market") or (request.get_json(silent=True) or {}).get("market", "")
    if not slug:
        return jsonify(error="market parameter required",
                       hint="e.g. ?market=phoenix or ?market=williston-nd"), 400
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT * FROM market_power_scores
                       WHERE market_slug = %s ORDER BY computed_at DESC LIMIT 1""", (slug,))
        r = cur.fetchone()
    if not r: return jsonify(error="market not found", slug=slug), 404
    if r.get("computed_at"): r["computed_at"] = r["computed_at"].isoformat()
    return jsonify({
        "tool": "getDCPI",
        "market": slug,
        "scores": {
            "excess_power": r["excess_power_score"],
            "constraint": r["constraint_score"],
            "verdict": r["verdict"],
            "time_to_power_months": r["time_to_power_months"],
        },
        "details": r,
        "citation": f"DC Hub Power Index — {r['market_name']} updated {r['computed_at'][:10]}",
        "source": "https://dchub.cloud/dcpi/" + slug,
    }), 200

@dcpi_mcp_bp.route("/api/v1/mcp/dcpi/compare", methods=["GET", "POST"])
def compare_dcpi():
    """MCP tool: compareDCPI(markets[]) — side-by-side DCPI."""
    markets = request.args.get("markets") or (request.get_json(silent=True) or {}).get("markets")
    if isinstance(markets, str):
        markets = [m.strip() for m in markets.split(",") if m.strip()]
    if not markets:
        return jsonify(error="markets required (comma-separated slugs)"), 400
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT DISTINCT ON (market_slug) * FROM market_power_scores
                       WHERE market_slug = ANY(%s) ORDER BY market_slug, computed_at DESC""",
                    (markets,))
        rows = cur.fetchall()
    for r in rows:
        if r.get("computed_at"): r["computed_at"] = r["computed_at"].isoformat()
    rows.sort(key=lambda r: -(r.get("excess_power_score") or 0))
    return jsonify({
        "tool": "compareDCPI",
        "markets_requested": markets,
        "ranked_by_excess": [
            {"market": r["market_slug"], "name": r["market_name"],
             "excess": r["excess_power_score"], "constraint": r["constraint_score"],
             "verdict": r["verdict"]}
            for r in rows
        ],
        "winner": rows[0]["market_name"] if rows else None,
        "source": "https://dchub.cloud/dcpi",
    }), 200

@dcpi_mcp_bp.route("/api/v1/mcp/dcpi/movers", methods=["GET"])
def dcpi_movers():
    """MCP tool: getDCPIMovers — biggest 7-day score movers.

    r-fix (2026-06-06): sourced from dcpi_daily_snapshots (the history-
    preserving table), NOT market_power_scores. The old query self-joined
    market_power_scores on `computed_at < NOW()-7d`, but that table re-stamps
    computed_at on every recompute, so there was never a 7-day-ago row — every
    "mover" came back with prev=NULL and delta=0 (10 fabricated non-movers).
    Now we diff the latest snapshot against the most recent snapshot on/before
    7 days ago and return ONLY real movers (|delta| >= 0.5). Empty until the
    table has >=7 days of daily history (honest), then real deltas appear."""
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (market_slug)
                    market_slug, market_name, excess_power_score AS now_e,
                    constraint_score AS now_c, snapshot_date
                FROM dcpi_daily_snapshots
                ORDER BY market_slug, snapshot_date DESC
            ),
            week_ago AS (
                SELECT DISTINCT ON (market_slug)
                    market_slug, excess_power_score AS prev_e, snapshot_date AS prev_date
                FROM dcpi_daily_snapshots
                WHERE snapshot_date <= CURRENT_DATE - 7
                ORDER BY market_slug, snapshot_date DESC
            )
            SELECT l.market_slug, l.market_name, l.now_e, w.prev_e,
                   ROUND((l.now_e - w.prev_e)::numeric, 1) AS delta,
                   (CURRENT_DATE - w.prev_date) AS window_days
            FROM latest l JOIN week_ago w ON l.market_slug = w.market_slug
            WHERE ABS(l.now_e - w.prev_e) >= 0.5
            ORDER BY ABS(l.now_e - w.prev_e) DESC
            LIMIT 10
        """)
        rows = cur.fetchall()
    return jsonify({"tool": "getDCPIMovers", "movers": rows,
                    "window": "~7d (from dcpi_daily_snapshots)",
                    "note": "empty until >=7 days of daily snapshot history exists",
                    "source": "https://dchub.cloud/dcpi"}), 200
