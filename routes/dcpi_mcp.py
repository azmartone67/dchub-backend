"""Phase 109D — DCPI exposed as MCP tools for AI agents."""
from flask import Blueprint, request, jsonify
import os, psycopg2, psycopg2.extras

dcpi_mcp_bp = Blueprint("dcpi_mcp", __name__)

def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    return psycopg2.connect(db, sslmode="require")


def _dcpi_cap():
    """Caller's DCPI row cap (None == unlimited). Fails safe to the
    tightest (anonymous) cap so a resolve error never opens the gate."""
    try:
        from util.tier_gate import resolve_tier, dcpi_cap_for
        _tier, _ = resolve_tier()
        return dcpi_cap_for(_tier)
    except Exception:
        return 3

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

    # 2026-05-28 — was open: a caller could pass every market slug and pull
    # the whole dataset via enumeration. Cap the returned comparison rows by
    # tier so this can't be used as a bulk-exfil side door.
    _total = len(rows)
    _cap = _dcpi_cap()
    _gated = _cap is not None and _total > _cap
    if _gated:
        rows = rows[:_cap]
    out = {
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
    }
    if _gated:
        out["_gated"] = True
        out["_preview_only"] = True
        out["_total_available"] = _total
        out["_hidden_count"] = _total - _cap
        out["_upgrade_cta"] = (
            f"Comparing {_cap} of {_total} requested markets. Upgrade to "
            f"compare more — dchub.cloud/pricing")
        out["_pricing_url"] = "https://dchub.cloud/pricing"
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "private, max-age=120"
    resp.headers["Vary"] = "X-API-Key, Authorization"
    return resp, 200

@dcpi_mcp_bp.route("/api/v1/mcp/dcpi/movers", methods=["GET"])
def dcpi_movers():
    """MCP tool: getDCPIMovers — biggest 7-day score movers."""
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (market_slug)
                    market_slug, market_name, excess_power_score AS now_e,
                    constraint_score AS now_c, computed_at
                FROM market_power_scores
                ORDER BY market_slug, computed_at DESC
            ),
            week_ago AS (
                SELECT DISTINCT ON (market_slug)
                    market_slug, excess_power_score AS prev_e
                FROM market_power_scores
                WHERE computed_at < NOW() - INTERVAL '7 days'
                ORDER BY market_slug, computed_at DESC
            )
            SELECT l.market_slug, l.market_name, l.now_e, w.prev_e,
                   COALESCE(l.now_e - w.prev_e, 0) AS delta
            FROM latest l LEFT JOIN week_ago w ON l.market_slug=w.market_slug
            ORDER BY ABS(COALESCE(l.now_e - w.prev_e, 0)) DESC
            LIMIT 10
        """)
        rows = cur.fetchall()

    # 2026-05-28 — was open. Cap by tier like the other movers surface.
    _total = len(rows)
    _cap = _dcpi_cap()
    out = {"tool": "getDCPIMovers", "movers": rows,
           "source": "https://dchub.cloud/dcpi"}
    if _cap is not None and _total > _cap:
        out["movers"] = rows[:_cap]
        out["_gated"] = True
        out["_preview_only"] = True
        out["_total_available"] = _total
        out["_hidden_count"] = _total - _cap
        out["_upgrade_cta"] = (
            f"Showing {_cap} of {_total} movers. Upgrade for all — "
            f"dchub.cloud/pricing")
        out["_pricing_url"] = "https://dchub.cloud/pricing"
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "private, max-age=120"
    resp.headers["Vary"] = "X-API-Key, Authorization"
    return resp, 200
