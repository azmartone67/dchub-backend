"""
Brain — Strategic Gaps bridge (r78, 2026-06-07)
================================================
The brain already collected three signals but never CONNECTED them:
  1. Competitor agent-readiness  (competitive_intel — 6 rivals × agent-readiness axes)
  2. Tool DEMAND                 (mcp_call_log — what agents actually call)
  3. Upgrade PRESSURE            (mcp_upgrade_signals — where they hit the paywall)
This fuses them into a ranked "build/deepen/convert X next to win" feed — i.e. the
brain now OFFERS product/gap recommendations, not just measures rivals.

Plus /conversions-since-fix: makes the NOW-unsealed funnel visible (the 2026-06-07
mcp_dev_keys pay->free fix). Read-only, fail-soft — any missing source is skipped,
never 500s.
"""
import os
from flask import Blueprint, jsonify

brain_strategic_gaps_bp = Blueprint("brain_strategic_gaps", __name__)

_TIER_FIX_DATE = "2026-06-07"  # day the mcp_dev_keys pay->free leak was sealed


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        import psycopg2
        return psycopg2.connect(db, connect_timeout=5)
    except Exception:
        return None


def _competitor_readiness():
    """How agent-ready are the tracked rivals? Sparse readiness = DC Hub's moat is
    distribution (ship before they wake up), not features. Cache-read only."""
    try:
        from routes import competitive_intel as ci
        axes_ready = {}
        n = 0
        for comp in ci._COMPETITORS:
            n += 1
            obs = ci.probe_competitor(comp["slug"]) or {}
            for axis, val in (obs.get("axes") or {}).items():
                ok = bool(val) and str(val).lower() not in ("no", "false", "none", "unknown", "", "0")
                axes_ready[axis] = axes_ready.get(axis, 0) + (1 if ok else 0)
        return {"n_rivals": n, "rivals_ready_per_axis": axes_ready}
    except Exception as e:
        return {"error": str(e)[:80]}


def compute_conversions_since_fix(cur):
    """The now-unsealed funnel, made visible."""
    out = {"tier_fix_date": _TIER_FIX_DATE}
    try:
        cur.execute(
            "SELECT plan, COUNT(*)::int FROM users "
            "WHERE plan IN ('pro','founding','enterprise') AND plan_updated_at >= %s "
            "GROUP BY plan", (_TIER_FIX_DATE,))
        out["users_upgraded_since_fix"] = {r[0]: r[1] for r in cur.fetchall()}
    except Exception as e:
        out["users_upgraded_since_fix"] = {"error": str(e)[:60]}
    try:
        cur.execute(
            "SELECT tier, COUNT(*)::int FROM mcp_dev_keys "
            "WHERE tier IN ('paid','enterprise') AND status='active' GROUP BY tier")
        out["paid_mcp_keys_now"] = {r[0]: r[1] for r in cur.fetchall()}
    except Exception as e:
        out["paid_mcp_keys_now"] = {"error": str(e)[:60]}
    try:
        # the leak guard — paying customers still stuck on free MCP (should be 0)
        cur.execute(
            "SELECT COUNT(*)::int FROM mcp_dev_keys k JOIN users u "
            "ON LOWER(k.email)=LOWER(u.email) WHERE k.status='active' "
            "AND u.plan IN ('pro','founding','enterprise') "
            "AND COALESCE(u.subscription_status,'')='active' "
            "AND COALESCE(k.tier,'free') NOT IN ('paid','enterprise')")
        out["still_stuck_on_free"] = cur.fetchone()[0]
    except Exception:
        out["still_stuck_on_free"] = None
    return out


def compute_strategic_gaps():
    c = _conn()
    if c is None:
        return {"ok": False, "error": "db_unavailable"}
    demand, pressure, conv = {}, {}, {}
    try:
        with c, c.cursor() as cur:
            # 1. DEMAND — top tools agents actually call (7d)
            try:
                cur.execute(
                    "SELECT tool, COUNT(*)::int, COUNT(DISTINCT api_key)::int "
                    "FROM mcp_call_log WHERE timestamp >= NOW() - INTERVAL '7 days' "
                    "AND tool IS NOT NULL GROUP BY tool ORDER BY 2 DESC LIMIT 8")
                demand = [{"tool": r[0], "calls_7d": r[1], "agents_7d": r[2]} for r in cur.fetchall()]
            except Exception as e:
                demand = {"error": str(e)[:60]}
            # 2. PRESSURE — where agents hit the paywall (DISTINCT callers, not raw)
            try:
                cur.execute(
                    "SELECT tool_requested, COUNT(DISTINCT caller_id)::int "
                    "FROM mcp_upgrade_signals WHERE created_at >= NOW() - INTERVAL '7 days' "
                    "AND tool_requested IS NOT NULL GROUP BY tool_requested ORDER BY 2 DESC LIMIT 8")
                pressure = [{"tool": r[0], "distinct_agents_7d": r[1]} for r in cur.fetchall()]
            except Exception as e:
                pressure = {"error": str(e)[:60]}
            conv = compute_conversions_since_fix(cur)
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}

    moat = _competitor_readiness()

    # ── SYNTHESIZE the ranked "build X to win" feed ──
    recs = []
    if isinstance(pressure, list):
        for p in pressure[:3]:
            recs.append({
                "priority": "HIGH", "type": "CONVERT", "area": p["tool"],
                "rationale": (f"{p['distinct_agents_7d']} distinct agents hit the paywall on "
                              f"{p['tool']} in 7d — the clearest upgrade intent on the platform. "
                              f"Make its paid value explicit in the free teaser + guarantee the "
                              f"1-click /upgrade?key= path. Highest-ROI conversion lever."),
                "signal": p})
    if isinstance(demand, list):
        for d in demand[:3]:
            recs.append({
                "priority": "MEDIUM", "type": "DEEPEN", "area": d["tool"],
                "rationale": (f"{d['tool']} is a top-called tool ({d['calls_7d']} calls / "
                              f"{d['agents_7d']} agents, 7d) — top-of-funnel hook. Keep it "
                              f"best-in-class with data rivals can't match; it earns the upgrade."),
                "signal": d})
    if isinstance(moat, dict) and "rivals_ready_per_axis" in moat:
        recs.append({
            "priority": "STRATEGIC", "type": "MOAT", "area": "agent-readiness lead",
            "rationale": (f"Across {moat.get('n_rivals', 6)} tracked rivals, agent-readiness "
                          f"(MCP / llms.txt / machine-readable) is sparse — your lead is "
                          f"DISTRIBUTION, not features. Ship the SDK + registries + framework "
                          f"packages before rivals wake up. Being present beats being better."),
            "signal": moat.get("rivals_ready_per_axis")})

    return {
        "ok": True,
        "generated_for": "founder strategic-gaps bridge (competitor + demand + pressure fusion)",
        "recommendations": recs,
        "demand_top": demand,
        "paywall_pressure_top": pressure,
        "competitor_moat": moat,
        "conversions_since_fix": conv,
    }


@brain_strategic_gaps_bp.route("/api/v1/brain/strategic-gaps", methods=["GET"])
def strategic_gaps():
    return jsonify(compute_strategic_gaps())


@brain_strategic_gaps_bp.route("/api/v1/brain/conversions-since-fix", methods=["GET"])
def conversions_since_fix():
    c = _conn()
    if c is None:
        return jsonify({"ok": False, "error": "db_unavailable"})
    try:
        with c, c.cursor() as cur:
            return jsonify({"ok": True, **compute_conversions_since_fix(cur)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:120]})
