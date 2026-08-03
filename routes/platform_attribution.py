"""platform_attribution.py — the artifact you take to a platform (2026-08-03).

WHY THIS EXISTS
===============
The paid base is 37 developer keys and 3 enterprise seats, against ~1,236 tool
calls a day. A $49 developer key is the wrong instrument for a platform
answering user questions at volume: no agent holds a credit card, and metering a
partner is not the same motion as selling to one.

So this is not another funnel dashboard. It is ONE artifact, per platform,
built to survive being read by the counterparty's analyst: what they called,
how often, over what period, on a stated methodology, with the exclusions named.

★IT UNDERSTATES ON PURPOSE. This number will be quoted back at us in a
commercial conversation, so every ambiguity resolves DOWNWARD:

  · calls        EXACT. A row per call, no estimation.
  · agents       A FLOOR, and labelled as one. Identity is IP-derived
                 (mcp_calls_identity.agent_id hashes the first X-Forwarded-For
                 hop), so every one of a platform's users behind a shared
                 egress collapses into a single agent. The true number of end
                 users is HIGHER and we cannot prove by how much — so we claim
                 the floor and say why.
  · exclusions   the canonical view's own: Cloudflare POP first-hops, non-public
                 IPs, internal DC Hub traffic and registry crawlers. Named in
                 the payload, not buried here.

★ASSISTANT vs TOOLING. `curl`, `postman`, `insomnia`, `node-http-client` and
the MCP inspector are developer tooling, not an AI platform answering user
questions. Folding them into a platform total would inflate exactly the number
a counterparty would check first. They are reported SEPARATELY, never summed in.

Endpoints:
  GET /api/v1/admin/platform-attribution?days=30        JSON, all platforms
  GET /api/v1/admin/platform-attribution?platform=meta-ai   one platform + a
      paste-able statement paragraph for the commercial conversation

Auth: X-Admin-Key / ?admin_key= (DCHUB_ADMIN_KEY, falls back to
DCHUB_INTERNAL_KEY). Read-only — this module writes nothing.
Kill: PLATFORM_ATTRIBUTION_DISABLE=1
"""
from __future__ import annotations

import logging
import os

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
platform_attribution_bp = Blueprint("platform_attribution", __name__)

CANONICAL_VIEW = "mcp_calls_identity"

# AI platforms answering end-user questions — the ones a licence conversation
# is about. Taken from the platform tags the canonical view actually emits.
ASSISTANT_PLATFORMS = (
    "meta-ai", "chatgpt", "claude", "gemini", "perplexity", "github-copilot",
    "cursor", "cline", "phind", "groq", "apple-intelligence",
)
# Developer tooling. Real traffic, NOT a platform integration. Reported beside
# the assistants and never summed into them.
TOOLING_PLATFORMS = (
    "curl", "postman", "insomnia", "node-http-client", "node-script",
    "mcp-inspector", "mcp-sdk", "n8n", "smithery",
)
# Ours. Never reported as demand.
INTERNAL_PLATFORMS = ("internal-dchub",)

_EXCLUSIONS = (
    "Cloudflare POP first-hops (a POP is not an agent)",
    "non-public / private IPs",
    "internal DC Hub traffic and health probes",
    "registry crawlers excluded by the canonical view",
)


def _disabled() -> bool:
    return (os.environ.get("PLATFORM_ATTRIBUTION_DISABLE") or "").strip() == "1"


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    exp = ((os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == exp


def _conn():
    try:
        import psycopg2
        url = ((os.environ.get("NEON_REPLICA_URL") or "").strip()
               or (os.environ.get("DATABASE_URL") or "").strip()
               or (os.environ.get("NEON_DATABASE_URL") or "").strip())
        if not url:
            return None
        c = psycopg2.connect(url, connect_timeout=10)
        c.autocommit = True
        return c
    except Exception as e:  # noqa: BLE001
        logger.warning("[platform-attr] connect failed: %s", str(e)[:120])
        return None


def classify_platform(name) -> str:
    """assistant | tooling | internal | unknown. Pure.

    ★`unknown` is its own bucket and is NEVER folded into `assistant`. A new
    user-agent we have not classified yet is not evidence of a platform
    integration, and a total that quietly absorbs it is the kind of number that
    falls apart under someone else's scrutiny."""
    n = str(name or "").strip().lower()
    if not n:
        return "unknown"
    if n in INTERNAL_PLATFORMS:
        return "internal"
    if n in ASSISTANT_PLATFORMS:
        return "assistant"
    if n in TOOLING_PLATFORMS:
        return "tooling"
    return "unknown"


def platform_rows(cur, days: int = 30) -> dict:
    """Per-platform usage at the canonical grain. {ok, days, platforms:[...]}."""
    out = {"ok": False, "days": int(days), "source": CANONICAL_VIEW,
           "platforms": []}
    try:
        cur.execute("SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_name = %s AND column_name = 'agent_id'",
                    (CANONICAL_VIEW,))
        row = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        out["error"] = (f"information_schema unreadable ({str(e)[:80]}) — "
                        f"UNMEASURED, not zero")
        return out
    if not row or not int(row[0] or 0):
        out["error"] = (f"{CANONICAL_VIEW}.agent_id absent — the canonical "
                        f"identity view has not been applied. This module "
                        f"deliberately does NOT fall back to raw IP counting: "
                        f"an attribution number quoted to a counterparty must "
                        f"come from the canonical grain or not exist.")
        return out
    try:
        cur.execute("""
            SELECT COALESCE(NULLIF(platform, ''), 'untagged') AS platform,
                   COUNT(*)                        AS calls,
                   COUNT(DISTINCT agent_id)        AS agents,
                   COUNT(DISTINCT tool_name)       AS tools,
                   MIN(created_at)::date           AS first_seen,
                   MAX(created_at)::date           AS last_seen,
                   COUNT(DISTINCT created_at::date) AS active_days
              FROM mcp_calls_identity
             WHERE created_at >= NOW() - make_interval(days => %s)
               AND agent_id IS NOT NULL AND agent_id <> ''
               AND is_public_ip AND is_real_external
             GROUP BY 1
             ORDER BY calls DESC
        """, (int(days),))
        rows = cur.fetchall() or []
    except Exception as e:  # noqa: BLE001
        out["error"] = f"query_failed: {str(e)[:160]} — UNMEASURED, not zero"
        return out

    plats = []
    for r in rows:
        kind = classify_platform(r[0])
        plats.append({
            "platform": r[0], "kind": kind,
            "calls": int(r[1] or 0),
            "agents_floor": int(r[2] or 0),
            "distinct_tools": int(r[3] or 0),
            "first_seen": str(r[4]) if r[4] else None,
            "last_seen": str(r[5]) if r[5] else None,
            "active_days": int(r[6] or 0),
        })
    out["platforms"] = plats
    # Totals are per-KIND. There is deliberately no grand total: summing
    # assistants, tooling and untagged traffic into one headline is the
    # inflation this artifact exists to avoid.
    by_kind = {}
    for p in plats:
        k = by_kind.setdefault(p["kind"], {"calls": 0, "platforms": 0})
        k["calls"] += p["calls"]
        k["platforms"] += 1
    out["by_kind"] = by_kind
    out["ok"] = True
    out["methodology"] = {
        "grain": f"{CANONICAL_VIEW}.agent_id (IP-derived)",
        "calls": "EXACT — one row per call",
        "agents_floor": ("A FLOOR. Identity hashes the first X-Forwarded-For "
                         "hop, so a platform's users behind shared egress "
                         "collapse into one agent. True end-user count is "
                         "higher by an amount we cannot prove."),
        "excluded": list(_EXCLUSIONS),
        "kinds": ("assistant = an AI platform answering end-user questions; "
                  "tooling = developer tooling (curl/postman/SDK); internal = "
                  "ours; unknown = unclassified user-agent, NEVER folded into "
                  "assistant."),
    }
    return out


def statement_for(platform: str, row: dict, days: int) -> str:
    """The paste-able paragraph for the commercial conversation. Every number
    in it is one the counterparty can re-derive, and the floor is named as a
    floor — an artifact that overstates once is never trusted again."""
    if not row:
        return (f"No attributable {platform} traffic in the last {days} days at "
                f"the canonical grain.")
    return (
        f"Over the {days} days to {row['last_seen']}, {platform} accounted for "
        f"{row['calls']:,} DC Hub MCP tool calls across {row['distinct_tools']} "
        f"distinct tools on {row['active_days']} active days, first seen "
        f"{row['first_seen']}. Distinct agents observed: "
        f"{row['agents_floor']:,} — this is a FLOOR, not a user count: "
        f"identity is derived from the first X-Forwarded-For hop, so end users "
        f"sharing an egress collapse into one agent. Call volume is exact. "
        f"Excluded from all figures: "
        + "; ".join(_EXCLUSIONS) + ".")


@platform_attribution_bp.route("/api/v1/admin/platform-attribution",
                               methods=["GET"])
def platform_attribution():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    try:
        days = max(1, min(int(request.args.get("days", "30")), 365))
    except Exception:
        days = 30
    want = (request.args.get("platform") or "").strip().lower()
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="no_database"), 503
    try:
        with c.cursor() as cur:
            data = platform_rows(cur, days=days)
    finally:
        try:
            c.close()
        except Exception:
            pass
    if want and data.get("ok"):
        row = next((p for p in data["platforms"] if p["platform"] == want), None)
        data["platforms"] = [row] if row else []
        data["statement"] = statement_for(want, row, days)
    return jsonify(data)
