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
    # 2026-05-29: default was https://dchub.cloud — but the brain runs INSIDE
    # Railway, so self-probes through CF round-trip to the edge and back, AND
    # trip CF's rate limiter when 6 sequential probes fire (the burst from a
    # single radar cycle was getting 429'd, hiding all downstream findings
    # behind a single "catalog unreachable" headline). Default to the Railway-
    # direct hostname so the detector talks to the same Flask app directly,
    # no CF round-trip, no rate limit. Override via env if the public-edge
    # behavior is what's being audited.
    base = os.environ.get(
        "MCP_HEALTH_BASE",
        "https://dchub-backend-production.up.railway.app",
    )

    # ── 1. Live tool catalog (the canonical /api/v1/mcp/tools.json) ──
    # 2026-05-29: was `return findings` on err — that silenced ALL other
    # checks (pricing, server card, DCPI movers) whenever the catalog
    # briefly returned 429 from a probe burst. Now we set live_tools=[]
    # and SKIP the checks that need it, but let the rest run. The
    # catalog-unreachable finding still surfaces as the headline issue.
    live, err = _fetch_json(f"{base}/api/v1/mcp/tools.json")
    if err:
        findings.append({
            "issue":  "mcp_health_catalog_unreachable",
            "url":    f"{base}/api/v1/mcp/tools.json",
            "count":  1,
            "detail": f"Live MCP tool catalog unreachable ({err}). Registries "
                      f"crawling this URL see no manifest.",
        })
        live_tools, live_count = [], None
    else:
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
    elif live_count is not None:
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

    # ── 6a. AI testimonials freshness — the /dc-hub-media citations rail ──
    # 2026-05-29: investigation showed every row in /api/v1/testimonials/live
    # has posted_at=2026-03-06 (~84 days old). The auto-ingest cron
    # (HackerNews + Reddit + MCP-derived → ai_testimonials_auto) stopped
    # producing fresh items. The dchub_media_hub aggregator's merge+sort
    # now picks the freshest of what exists, but if all rows are stale
    # the citations rail still shows ancient quotes. Flag the ingest gap.
    tt, tt_err = _fetch_json(f"{base}/api/v1/testimonials/live?limit=1")
    if not tt_err and isinstance(tt, dict):
        items = tt.get("items") or []
        if items:
            newest_ts = (items[0].get("posted_at")
                         or items[0].get("captured_at")
                         or items[0].get("created_at") or "")
            try:
                import datetime as _dt
                # Strip Z + parse — we only need rough day-staleness
                ts = newest_ts.split("+")[0].rstrip("Z").split(".")[0]
                newest = _dt.datetime.fromisoformat(ts) if ts else None
                if newest:
                    age_days = (_dt.datetime.utcnow() - newest).days
                    if age_days > 14:
                        findings.append({
                            "issue":  "testimonials_auto_ingest_stale",
                            "url":    f"{base}/api/v1/testimonials/live",
                            "count":  age_days,
                            "detail": (f"Newest AI testimonial is {age_days} days "
                                       f"old (2026-03-06 area). The auto-ingest "
                                       f"cron (HackerNews/Reddit/MCP-derived → "
                                       f"ai_testimonials_auto) appears stuck. "
                                       f"The citations rail on /dc-hub-media is "
                                       f"now sort-correct but has no fresh items "
                                       f"to surface. Check the ingest job + "
                                       f"upstream source matches for 'dchub'."),
                        })
            except Exception:
                pass

    # ── 6b. DCPI movers freshness — the /dc-hub-media "Movers" rail ──
    # 2026-05-29: investigation showed /api/v1/dcpi/movers returns every
    # market with excess_delta_7d=0.0 and prev_excess=null. The week_ago
    # CTE filters market_power_scores for rows with computed_at < NOW()-7d
    # and finds none — the recompute job UPDATEs the table in place rather
    # than INSERTing snapshot rows. Result: empty movers feed + no DCPI
    # alert press releases. Surface as a brain finding so the snapshot
    # gap is visible while we design the snapshot cron + history table.
    mv, mv_err = _fetch_json(f"{base}/api/v1/dcpi/movers")
    if not mv_err and isinstance(mv, dict):
        movers = mv.get("movers") or []
        if movers:
            null_prev = sum(1 for m in movers if m.get("prev_excess") is None)
            zero_delta = sum(1 for m in movers if (m.get("excess_delta_7d") or 0) == 0)
            # If EVERY mover has null prev_excess, the snapshot is missing
            if null_prev == len(movers) and len(movers) >= 5:
                findings.append({
                    "issue":  "dcpi_snapshot_history_missing",
                    "url":    f"{base}/api/v1/dcpi/movers",
                    "count":  len(movers),
                    "detail": (f"DCPI movers feed returns {len(movers)} markets but "
                               f"every prev_excess is null — market_power_scores "
                               f"has no rows older than 7 days. The recompute job "
                               f"updates in place; need a weekly snapshot cron + "
                               f"history table so WoW deltas can compute. Empty "
                               f"movers feed → empty 'Biggest Movers' rail on "
                               f"/dc-hub-media + zero DCPI-alert auto-press."),
                })
            elif zero_delta == len(movers):
                # Snapshot exists but all deltas are zero (DCPI genuinely flat)
                findings.append({
                    "issue":  "dcpi_movers_flat_week",
                    "url":    f"{base}/api/v1/dcpi/movers",
                    "count":  len(movers),
                    "detail": (f"DCPI movers all zero this week ({len(movers)} markets "
                               f"checked). Either DCPI is genuinely flat (no action) "
                               f"OR the recompute job didn't run. Verify "
                               f"market_power_scores has fresh computed_at values."),
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
