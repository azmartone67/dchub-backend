"""
brain_mcp_health.py — Continuous MCP discoverability health checker.
====================================================================

The MCP discovery ecosystem (Glama, Smithery, mcp.so, MCPHub, the Official MCP
Registry, awesome-mcp-servers PR, .well-known/* crawlers) indexes DC Hub from
SEVEN different surfaces, each with its own metadata copy that drifts:

  1. Live MCP server         https://dchub.cloud/mcp (Node, source-of-truth)
  2. Flask manifest          /api/v1/mcp/tools.json (must mirror live)
  3. Well-known mcp-tools    /.well-known/mcp-tools.json (registries crawl)
  4. Well-known mcp-server   /.well-known/mcp-server.json (server card)
  5. Node static manifest    dchub-mcp-server/mcp-server.json (some registries)
  6. Smithery manifest       dchub-mcp-server/smithery.yaml (Smithery indexes)
  7. README badges           tool count, status badges, pricing strings

When the static copies drift from the live server, agent-discovery surfaces
serve stale/incomplete catalogs and DC Hub looks weaker than it is. Manual
scrubs only catch it when someone notices.

This detector runs on every radar cycle (~30 min cached) and surfaces drift
into the brain's findings stream:

  - tool count drift between surfaces
  - weak tool descriptions (Glama-quality proxy: <80 chars)
  - missing pricing tier (Starter $9 must exist everywhere)
  - .well-known surface unreachable

Findings flow into `brain_issue_persistence` like every other brain finding.
Static-file fixes happen in the Node `dchub-mcp-server` repo (out of process),
so the detector FLAGS — it does not auto-push. A human/agent reads the brain
dashboard, sees the drift, syncs the static files.
"""
import os
import logging
from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)
brain_mcp_health_bp = Blueprint("brain_mcp_health", __name__)

# Tunables — tighter thresholds catch drift earlier; looser avoids noise.
_WEAK_DESC_THRESHOLD_CHARS = 80   # Glama scores <80 chars as C/D-grade.
_REQUIRED_PRICING_TIERS = {"starter", "developer", "pro"}  # must exist in /api/v1/tiers


def _flatten_tools(maybe_tools):
    """Tools may be a flat list OR a dict-by-category. Return a flat list."""
    if isinstance(maybe_tools, list):
        return maybe_tools
    if isinstance(maybe_tools, dict):
        out = []
        for v in maybe_tools.values():
            if isinstance(v, list):
                out.extend(v)
        return out
    return []


def _fetch_json(url, timeout=8):
    """Fetch JSON best-effort; returns (data, error_str)."""
    try:
        import requests
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "DCHub-BrainMcpHealth/1.0"})
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        return r.json(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"


def check_mcp_health() -> list[dict]:
    """Detector — runs as part of scan_all. Returns brain findings list.

    Each finding follows the standard radar shape:
      {issue, url, count, detail}
    """
    findings: list[dict] = []
    base = os.environ.get("MCP_HEALTH_BASE", "https://dchub.cloud")

    # ── 1. Live tool catalog (the canonical /api/v1/mcp/tools.json) ──
    live, err = _fetch_json(f"{base}/api/v1/mcp/tools.json")
    if err:
        findings.append({
            "issue":  "mcp_health_catalog_unreachable",
            "url":    f"{base}/api/v1/mcp/tools.json",
            "count":  1,
            "detail": f"Live MCP tool catalog unreachable ({err}). Registries "
                      f"crawling this URL see no manifest.",
        })
        return findings  # nothing else makes sense without the catalog

    live_tools = _flatten_tools(live.get("tools") if isinstance(live, dict) else live)
    live_count = len(live_tools)

    # ── 2. .well-known/mcp-tools.json count must match live ──
    wk, wk_err = _fetch_json(f"{base}/.well-known/mcp-tools.json")
    if wk_err:
        findings.append({
            "issue":  "mcp_health_wellknown_unreachable",
            "url":    f"{base}/.well-known/mcp-tools.json",
            "count":  1,
            "detail": f".well-known/mcp-tools.json unreachable ({wk_err}). "
                      f"Most registry crawlers use this URL.",
        })
    else:
        wk_count = len(_flatten_tools(wk.get("tools") if isinstance(wk, dict) else wk))
        if wk_count != live_count:
            findings.append({
                "issue":  "mcp_health_tool_count_drift",
                "url":    f"{base}/.well-known/mcp-tools.json",
                "count":  abs(wk_count - live_count),
                "detail": (f"Tool count drift: live catalog has {live_count} tools, "
                           f".well-known/mcp-tools.json has {wk_count}. "
                           f"Registry crawlers see inconsistent inventories."),
            })

    # ── 3. Tool description quality (Glama scores short = C/D) ──
    weak = []
    for t in live_tools:
        if not isinstance(t, dict):
            continue
        desc = (t.get("description") or "")
        if len(desc) < _WEAK_DESC_THRESHOLD_CHARS:
            weak.append({"name": t.get("name"), "chars": len(desc)})
    if weak:
        names = ", ".join(w["name"] for w in weak[:6] if w.get("name"))
        more = f" (+{len(weak)-6} more)" if len(weak) > 6 else ""
        findings.append({
            "issue":  "mcp_health_weak_tool_descriptions",
            "url":    f"{base}/api/v1/mcp/tools.json",
            "count":  len(weak),
            "detail": (f"{len(weak)} tool descriptions <{_WEAK_DESC_THRESHOLD_CHARS} "
                       f"chars (Glama scores these C/D, agents skip): "
                       f"{names}{more}. Edit dchub-mcp-server/server.mjs to expand."),
        })

    # ── 4. Pricing tier presence — Starter ($9) must exist everywhere ──
    tiers, t_err = _fetch_json(f"{base}/api/v1/tiers")
    if not t_err and isinstance(tiers, dict):
        limits = (tiers.get("limits") or {})
        present = {k.lower() for k in limits.keys()}
        missing = _REQUIRED_PRICING_TIERS - present
        if missing:
            findings.append({
                "issue":  "mcp_health_pricing_tier_missing",
                "url":    f"{base}/api/v1/tiers",
                "count":  len(missing),
                "detail": (f"Pricing tiers missing from /api/v1/tiers: "
                           f"{sorted(missing)}. MCP discovery surfaces + the "
                           f"agent-quotable upgrade hints may miss them, "
                           f"hurting conversion across the funnel."),
            })

    # ── 5. Server card metadata sanity (name/version/endpoint present) ──
    card, c_err = _fetch_json(f"{base}/.well-known/mcp-server.json")
    if not c_err and isinstance(card, dict):
        required = ("name", "description", "endpoint")
        missing_keys = [k for k in required if not card.get(k)]
        if missing_keys:
            findings.append({
                "issue":  "mcp_health_server_card_incomplete",
                "url":    f"{base}/.well-known/mcp-server.json",
                "count":  len(missing_keys),
                "detail": (f"MCP server card missing required keys: {missing_keys}. "
                           f"Some discovery surfaces reject incomplete cards."),
            })

    return findings


# ── On-demand endpoint: dashboard / debug surface ────────────────────
@brain_mcp_health_bp.route("/api/v1/brain/mcp-health", methods=["GET"])
def mcp_health_endpoint():
    """GET /api/v1/brain/mcp-health → the same findings the brain radar produces,
    on demand. Useful for debugging discovery drift after a static-file sync, or
    surfacing in the brain dashboard. Read-only, statement_timeout-guarded
    upstream (the checks themselves bound network calls to 8s)."""
    try:
        findings = check_mcp_health()
        from datetime import datetime, timezone
        return jsonify({
            "ok": True,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "findings_count": len(findings),
            "findings": findings,
            "note": ("MCP discoverability/health checks. Findings flow into the brain's "
                     "consistency-radar pipeline on every cycle (~30 min cached). "
                     "Static-file fixes live in the dchub-mcp-server repo."),
        }), 200
    except Exception as e:
        logger.warning("[brain-mcp-health] endpoint error: %s", e)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


def register_brain_mcp_health(app):
    app.register_blueprint(brain_mcp_health_bp)
    logger.info("🧠 Brain MCP-Health: ✅ Registered (/api/v1/brain/mcp-health)")
