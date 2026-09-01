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
import re

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
platform_attribution_bp = Blueprint("platform_attribution", __name__)

CANONICAL_VIEW = "mcp_calls_identity"

# ★CLASSIFIED FROM THE LIVE LIST, 2026-08-03. Every name below was observed
# in the 30d attribution run; nothing here is speculative.
#
# An AI agent answering end-user questions. anthropicapi and codex are agents
# built on a model API rather than a branded chat surface — still an assistant,
# still a licence conversation. connectors-manager is Anthropic's connector
# infrastructure fetching on a user's behalf.
ASSISTANT_PLATFORMS = (
    "meta-ai", "chatgpt", "claude", "gemini", "perplexity", "github-copilot",
    "copilot", "cursor", "cline", "phind", "groq", "grok", "mistral",
    "apple-intelligence", "codex", "anthropicapi", "connectors-manager",
)
# Developer tooling, registries and verifiers. Real traffic, NOT a platform
# integration. smithery/smitheryconnect are an MCP REGISTRY — its crawl is
# distribution plumbing, not a user; 2,518 calls from ONE agent says so.
TOOLING_PLATFORMS = (
    "curl", "postman", "insomnia", "node-http-client", "node-script",
    "mcp-inspector", "mcp-sdk", "n8n", "smithery", "smitheryconnect",
    "visualstudiocode", "liner-mcp-verifier",
)
# ★BULK HARVEST, its own kind. datacolo took 2,560 calls from 2 agents in a
# SINGLE day (loop-control lane 6 named the same signature). Folding that into
# any demand number would be the single largest distortion available.
# chain-hire is the SAME class, measured 2026-09-01: UA
# `chain-hire/1.0 (MCP client; doubao 2026)`, ONE IP (175.147.105.129), ONE
# tool (`search`, 1,473 of 1,475 calls), a flat 100-132 calls/hour for 14
# straight hours (08-29 11:24Z -> 08-30 00:59Z) and then nothing. 1,410 of
# those calls were served over the anonymous daily cap
# (mcp_call_log.status='anon_daily_cap', DCHUB_ANON_DAILY_CAP=30) -- i.e. the
# caller was told 1,410 times that it was past the free allowance and kept
# going at a fixed machine cadence. It carried NO api_key at any point.
# Left in the population it was 69.6% of the rolling-7d headline and the
# entire "+6.8% WoW" it appeared to produce; net of it the same window is 643
# calls from 39 agents, flat against the prior week's 331.
HARVESTER_PLATFORMS = ("datacolo", "chain-hire")
# ★THE BIGGEST BUCKET, AND STRUCTURALLY UNATTRIBUTABLE. `mcp` is the generic
# tag for a client that did not identify itself — 9,220 calls, 207 agents, 48
# tools over 24 days. It is NOT "unknown": we know it is an MCP client and we
# know we cannot say whose. It gets its own kind so it can never be quietly
# counted as either demand or noise, and so the size of the blind spot is
# always on the page.
UNATTRIBUTED_PLATFORMS = ("mcp",)
# Ours. Never reported as demand. reviewer-sim is our own harness.
INTERNAL_PLATFORMS = ("internal-dchub", "reviewer-sim", "dchub-internal",
                      "dchub-selfheal", "value-harness", "fixwave-probe")

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
    """assistant | tooling | harvester | unattributed | internal | unknown.

    Pure. ★NEITHER `unknown` NOR `unattributed` is ever folded into
    `assistant`. A user-agent we have not classified is not evidence of a
    platform integration, and `mcp` — a client that declined to identify
    itself — is not evidence of anything at all. A total that quietly absorbs
    either is the kind of number that falls apart under someone else's
    scrutiny."""
    n = str(name or "").strip().lower()
    if not n or n == "untagged":
        return "unknown"
    if n in INTERNAL_PLATFORMS:
        return "internal"
    if n in ASSISTANT_PLATFORMS:
        return "assistant"
    if n in TOOLING_PLATFORMS:
        return "tooling"
    if n in HARVESTER_PLATFORMS:
        return "harvester"
    if n in UNATTRIBUTED_PLATFORMS:
        return "unattributed"
    return "unknown"


# ── The de-loop name-space bridge ────────────────────────────────────────────
# ★★★ classify_platform() above reads the CANONICAL VIEW's platform names. The
# funnel's `calls_by_platform_30d` (flask_mcp_endpoints.py) groups by
# mcp_calls_deloop.PLATFORM_CASE instead, which is a DIFFERENT NAME-SPACE for
# the same platforms. Handing PLATFORM_CASE's names to classify_platform()
# unbridged returned `unknown` for 79.3% of live 30d volume (2026-08-27) —
# including the #1 and #2 rows, which are 74% of all calls on their own:
#
#     'smithery connect'   5,091  ->  TOOLING_PLATFORMS has 'smitheryconnect'
#     'mcp-generic-client' 4,962  ->  UNATTRIBUTED_PLATFORMS still says 'mcp'
#     'anthropic/api'        173  ->  ASSISTANT_PLATFORMS has 'anthropicapi'
#     'claude-code' / 'claude-ai' / 'anthropic/claudeai'
#
# That is a HALF-WORKING GUARD: right about datacolo, silently wrong about the
# majority of traffic. It reads as "classified" on a public dashboard when it
# mostly is not.
#
# ★ PLATFORM_CASE's vocabulary is OPEN, not enumerable. Its third branch is
# `WHEN NULLIF(LOWER(client_name),'') IS NOT NULL THEN LOWER(client_name)` —
# any client that sets clientInfo.name gets a bucket named after itself. So no
# fixed table can ever be complete, and `unknown` MUST stay the default. This
# bridge only TRANSLATES NAMES; every kind still comes from classify_platform()
# alone, so there remains exactly one place that decides what a kind means.
_DELOOP_TO_CANONICAL = {
    # PLATFORM_CASE split the old 'mcp' bucket into 'mcp-generic-client' on
    # 2026-07-28; UNATTRIBUTED_PLATFORMS was never updated to follow. A rename
    # we can point at in the source, not an inference.
    "mcp-generic-client": "mcp",
    # Same vendors already present in the tuples, spelled with a separator the
    # canonical space writes closed up.
    "smithery connect": "smithery",
    "anthropic/api": "anthropicapi",
    # Anthropic products. claude.ai and Claude Code are assistants answering an
    # end user, which is what ASSISTANT_PLATFORMS means.
    "anthropic/claudeai": "claude",
    "claude-ai": "claude",
    "claude-code": "claude",
}

# Separators are the ONLY systematic difference between the two spellings of
# the names above ('smithery connect' vs 'smitheryconnect'). Collapsing them is
# a general retry so a new spelling of an ALREADY-KNOWN vendor does not need a
# table entry; a genuinely unknown client still collapses to something absent
# from every tuple and stays `unknown`.
_SEPARATORS = re.compile(r"[\s/_.\-]+")


def classify_deloop_platform(name) -> str:
    """classify_platform(), but for mcp_calls_deloop.PLATFORM_CASE's names.

    ★ Kinds are NEVER decided here — each branch delegates to
    classify_platform(). This function only decides WHICH NAME to ask about.
    ★ Unrecognised client-supplied names return `unknown` by design. An
    unfamiliar clientInfo.name is not evidence of a platform integration.
    """
    n = str(name or "").strip().lower()
    kind = classify_platform(n)
    if kind != "unknown":
        return kind
    # A literal 'unknown'/'untagged'/empty bucket is already the honest answer;
    # do not let the retries below invent a platform for it.
    if n in ("", "unknown", "untagged"):
        return "unknown"
    mapped = _DELOOP_TO_CANONICAL.get(n)
    if mapped:
        return classify_platform(mapped)
    return classify_platform(_SEPARATORS.sub("", n))


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
        "kinds": ("assistant = an AI agent answering end-user questions; "
                  "tooling = developer tooling, registries and verifiers; "
                  "harvester = bulk scrape (never demand); unattributed = a "
                  "generic `mcp` client that did not identify itself — we know "
                  "it is an MCP client and cannot say whose, so it is neither "
                  "demand nor noise; internal = ours; unknown = unclassified, "
                  "NEVER folded into assistant."),
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
