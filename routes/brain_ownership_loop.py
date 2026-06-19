"""
brain_ownership_loop.py — Brain v4 (2026-06-18): the OWNERSHIP LOOP (SHADOW phase).

Crawl → walk → run. The brain already has the PARTS of a proactive owner
(strategic planner = goals, L22 = ships code, self-perception = reflects, the
media organism = tells the story) but they're four disconnected reflexes, not an
owner. This wires them into ONE goal-directed ownership loop across the domains
the brain owns:

    goal → read current metric → decide next move → (ARMED: act) → verify real
           effect → adjust → (media broadcasts the win)

That brain→ship→media→reach loop IS "a proactive owner, like Claude, with media
telling the story." RUN = this loop armed across every domain, setting its own
priorities.

★ SHADOW PHASE (this build): the loop DECIDES + surfaces its next move per
domain but EXECUTES NOTHING. There is deliberately NO code path from here to an
action — it is read-only by construction (GET-only internal reads, no writes, no
autopilot/L22/publish calls). Arming execution is a SEPARATE, reliability-gated
step (`BRAIN_OWNERSHIP_ARMED`, default OFF) wired per-domain, SAFEST domain
first (site_integrity → media → growth), and only after the autopilot
fix_success_rate clears 50% (the recovery watch). This lets the owner-brain's
reasoning be VISIBLE with zero risk before it is ever handed the controls.

Endpoint:
  GET /api/v1/brain/ownership   — per-domain owner cockpit (goal · current · next move)
"""
from __future__ import annotations
import os, json, urllib.request
from flask import Blueprint, jsonify

brain_ownership_bp = Blueprint("brain_ownership", __name__)

_BASE = "http://127.0.0.1:8080"


def _armed() -> bool:
    """Master arm flag. SHADOW-ONLY in this build: even flipped, nothing here
    executes — arming is per-domain and wired in a later, reliability-gated step."""
    return str(os.environ.get("BRAIN_OWNERSHIP_ARMED", "")).lower() in ("1", "true", "yes")


def _get(path: str, timeout: int = 8) -> dict:
    """Read-only internal GET. Fail-soft to {} so one slow source can't break the
    cockpit. This module NEVER issues a write or a state-changing call."""
    try:
        req = urllib.request.Request(
            _BASE + path, method="GET",
            headers={"User-Agent": "dchub-ownership-loop/shadow"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return {}


# ── The domains the brain OWNS (each becomes a closed loop when armed) ──────

def _domain_site_integrity() -> dict:
    s = _get("/api/v1/sentinel/scan")
    unhealthy = s.get("unhealthy") or []
    orphans = sum(1 for u in unhealthy if isinstance(u, dict)
                  and "orphan" in str(u.get("reason", "")).lower())
    aq = _get("/api/v1/brain/action-queue").get("queue") or []
    site_moves = [q.get("issue") for q in aq if isinstance(q, dict)
                  and any(k in str(q.get("issue", "")).lower()
                          for k in ("page", "orphan", "nav", "schema", "brand", "alias", "404"))]
    return {
        "domain": "site_integrity",
        "owns": "every page navigable, schema-complete, alive",
        "goal": "0 orphan pages; 100% nav + schema coverage",
        "current": {"pages_total": s.get("total"), "healthy": s.get("healthy"),
                    "orphan_pages": orphans},
        "proposed_next_move": site_moves[:3] or ["no open site-integrity actions"],
        "arm_order": 1, "arm_risk": "low (bounded recipes, reversible)",
        "broadcast": None,
    }


def _domain_growth() -> dict:
    r = _get("/api/v1/mcp/retention").get("summary") or {}
    return {
        "domain": "growth",
        "owns": "the reach/retention/conversion flywheel",
        "goal": "returning agents 1 → 10+/wk; key-reuse 8% → 25%+",
        "current": {"returning_ips_wk": r.get("latest_returning_ips"),
                    "new_ips_wk": r.get("latest_new_ips"),
                    "pct_reused_30d": r.get("pct_reused_30d")},
        "proposed_next_move": [
            "measure the r-return retention hook effect (1–2 wk)",
            "then: durable identity / OAuth connectors (1c)",
            "owner levers (human): Anthropic Connector dir, Smithery repoint",
        ],
        "arm_order": 3, "arm_risk": "high (touches paywall/funnel — revenue)",
        "broadcast": "media broadcasts each conversion / retention win",
    }


def _domain_media() -> dict:
    o = _get("/api/v1/media/organism")
    p = _get("/api/v1/media/pulse").get("components") or {}
    weakest = o.get("weakest_channel")
    return {
        "domain": "media",
        "owns": "telling DC Hub's story to the world",
        "goal": "broadcast every shipped win; grow distinct-agent reach",
        "current": {"vitality": o.get("vitality_score"), "weakest_channel": weakest,
                    "press_7d": (p.get("press") or {}).get("count_7d"),
                    "linkedin_7d": (p.get("linkedin") or {}).get("sent_7d")},
        "proposed_next_move": [
            f"strengthen weakest channel: {weakest}",
            "broadcast the latest brain-shipped win",
        ],
        "arm_order": 2, "arm_risk": "medium (content; quality gates already exist)",
        "broadcast": "this IS the broadcast arm",
    }


@brain_ownership_bp.route("/api/v1/brain/ownership", methods=["GET"])
def ownership():
    """SHADOW owner cockpit: per owned domain — goal, current metric, next move.
    Read-only by construction; executes nothing. See module docstring."""
    domains = []
    for fn in (_domain_site_integrity, _domain_growth, _domain_media):
        try:
            domains.append(fn())
        except Exception as e:
            domains.append({"domain": fn.__name__, "error": str(e)[:120]})
    return jsonify(
        layer="Brain v4 — ownership loop",
        phase="shadow",
        armed=_armed(),
        decides=True, executes=False,
        note=("The brain DECIDES its next move per owned domain but executes "
              "NOTHING here (read-only by construction). Arming is per-domain + "
              "reliability-gated (autopilot fix_success_rate >= 50%, the recovery "
              "watch); arm_order shows the sequence: site_integrity → media → growth."),
        crawl_walk_run="walking = these loops armed end-to-end, safest domain first",
        domains=domains,
    ), 200
