"""
agent_onboarding_master_shell.py — AI-platform onboarding master shell (2026-07-03).
=====================================================================================

WHY
---
DC Hub is reachable by AI agents three ways — an MCP server (dchub.cloud/mcp),
a REST/OpenAPI surface, and GEO-crawlable web — and it has been onboarded, at
varying depth, across a dozen platforms (Claude, ChatGPT, Gemini, Grok,
Perplexity, Copilot, Mistral/Le Chat, Hugging Face, Poe, …). The 2026 reality:
every major platform made MCP a first-class integration path, but almost NONE
run a public data-provider marketplace — connection is per-user/developer PULL.
So the onboarding game is (1) make wiring trivial (published recipes + A2A card),
(2) get into the few real directories (ChatGPT App Directory, Copilot Agent
Store), and (3) feed each platform's retrieval/citation engine. This shell keeps
one honest, per-platform, 0-100 onboarding scoreboard so the trend is trackable
and the single next action per platform is always surfaced.

WHAT
----
POST /api/v1/admin/agent-onboarding/master-tick runs three tiers:
  · TIER 1 — MEASURE:  live global probes — /mcp reachable + tool count +
                       transports, /.well-known A2A card, /robots.txt AI-bot
                       allowlist, /api/v1/reach real per-platform traffic.
  · TIER 2 — SCORE:    per-platform readiness (reachable, transport, auth, crawl,
                       discovery, traffic) blended 0-100 against a curated roster;
                       overall reach-weighted score; a ranked NEXT-ACTION worklist
                       (code-actionable vs owner/BD-gated, with effort).
  · TIER 3 — PERSIST:  one snapshot row per tick (agent_onboarding_snapshots).
GET /api/v1/admin/agent-onboarding/state returns the latest snapshot + trend.

MEASURES-AND-REPORTS ONLY. The only write is its own snapshot table (direct
cursor execute + commit — db_utils safe_db SILENTLY SKIPS DDL). It never submits
to a directory or mutates production data; owner/BD-gated actions are emitted as
a worklist for a human, exactly like the distribution shell's brief.

Auth: X-Admin-Key (DCHUB_ADMIN_KEY / DCHUB_INTERNAL_KEY). Fail-closed.
Kill switch: AGENT_ONBOARDING_MASTER_DISABLED=1.
"""
from __future__ import annotations

import hmac
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

agent_onboarding_master_shell_bp = Blueprint("agent_onboarding_master_shell", __name__)

_BASE = os.environ.get(
    "DCHUB_BACKEND_BASE",
    "https://dchub-backend-production.up.railway.app",
)
# The public origin (CF Pages / zone worker) serves robots.txt + /.well-known.
_PUBLIC = os.environ.get("DCHUB_PUBLIC_BASE", "https://dchub.cloud")
# MCP is a SEPARATE service (server.mjs) — must hit the canonical public endpoint.
_MCP_ENDPOINT = os.environ.get("DCHUB_MCP_ENDPOINT", "https://dchub.cloud/mcp")


# ── auth (mirrors agent_usefulness_master_shell) ──────────────────────
def _admin_key() -> str | None:
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    expected = (_admin_key() or "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, expected)


def _disabled() -> bool:
    return str(os.environ.get("AGENT_ONBOARDING_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


def _pct(numer: int, denom: int) -> float | None:
    return round(100.0 * numer / denom, 2) if denom else None


def _close(conn) -> None:
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        from main import return_pg_connection
        return_pg_connection(conn)
    except Exception:
        pass


# ── generic fail-soft HTTP GET (returns status, text, headers) ────────
def _get(url: str, headers: dict | None = None, timeout: int = 20) -> tuple[int, str, dict]:
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "dchub-agent-onboarding-orchestrator/1.0")
        req.add_header("X-DC-Probe", "agent-onboarding-tick")  # rate-limiter bypass
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace"), dict(e.headers or {})
        except Exception:
            return e.code, "", {}
    except Exception:
        return 0, "", {}


# ══════════════════════════════════════════════════════════════════════
# THE ROSTER — curated per-platform known-state (source: research 2026-07-03
# + internal onboarding records). Live probes overwrite the *measured* dims;
# the *curated* dims (directory listed / connect path / recipe published) are
# updated by a human as BD lands. reach_weight = relative opportunity (drives
# the overall reach-weighted score + worklist ranking).
#   dims applicable per platform are inferred from `paths`:
#     "mcp"  → reachable, transport, auth apply
#     "crawl"→ crawl_declared applies (bots[] checked against robots.txt)
#   discovery scores from: directory_listed (1.0) > recipe_published (0.6)
#     > self_serve-but-undocumented (0.3) > none (0.0)
# ══════════════════════════════════════════════════════════════════════
PLATFORMS: list[dict] = [
    {
        "key": "claude", "name": "Claude / Anthropic", "reach_weight": 1.0,
        "paths": ["mcp", "crawl"], "reach_token": "claude", "reach_aliases": ["anthropic", "claude-code", "claude-desktop"],
        "bots": ["ClaudeBot", "anthropic-ai", "Claude-User"],
        "required_auth": "none", "required_transport": "streamable",
        "directory": "Anthropic Connectors Directory", "directory_listed": False,
        "connect_path": "review", "recipe_published": True,
        "next_action": "Reviewer key ISSUED 2026-07-27 (enterprise, partner_slug 'anthropic-connectors-review', in ~/.dchub_reviewer_key) and VERIFIED ungated through /mcp — grid_intelligence 4.7KB->7.5KB, fiber_intel 2.8KB->1.53MB, rank_markets no longer previewed. The paywall blocker is GONE; what remains is BD/paperwork — the directory has no public self-serve form (owner_gated), so the submission goes through an Anthropic contact with the endpoint + reviewer key.",
        "owner_gated": True, "effort": "BD/paperwork",
    },
    {
        "key": "chatgpt", "name": "ChatGPT / OpenAI", "reach_weight": 1.0,
        "paths": ["mcp", "crawl"], "reach_token": "chatgpt", "reach_aliases": ["openai", "openai-mcp", "gpt"],
        "bots": ["OAI-SearchBot", "ChatGPT-User", "GPTBot"],
        "required_auth": "none", "required_transport": "streamable",
        "directory": "ChatGPT App Directory (Apps SDK)", "directory_listed": False,
        "connect_path": "review", "recipe_published": True,
        "next_action": "Submit the Apps-SDK app to the ChatGPT App Directory (needs business verification + a CSP on /mcp + a global-residency project). Custom GPT already live.",
        "owner_gated": True, "effort": "BD + light dev (CSP)",
    },
    {
        "key": "perplexity", "name": "Perplexity", "reach_weight": 0.8,
        "paths": ["mcp", "crawl"], "reach_token": "perplexity",
        "bots": ["PerplexityBot", "Perplexity-User"],
        "required_auth": "bearer", "required_transport": "both",
        "directory": "Perplexity curated App Connectors (400+)", "directory_listed": False,
        # recipe published 2026-07-17: /integrations/perplexity one-pager
        # (custom connector steps + Sonar/Search-API llms.txt grounding section).
        "connect_path": "partnership", "recipe_published": True,
        "next_action": "Recipe live at /integrations/perplexity (custom connector + Sonar/llms.txt grounding). Verify a live Perplexity connector session end-to-end, then BD for the curated App Connectors list.",
        "owner_gated": False, "effort": "low (doc) + BD",
    },
    {
        "key": "gemini", "name": "Gemini / Google", "reach_weight": 0.8,
        "paths": ["mcp", "crawl"], "reach_token": "gemini", "reach_aliases": ["google"],
        "bots": ["Google-Extended"],
        "required_auth": "oauth", "required_transport": "streamable",
        "directory": "Gemini prebuilt connectors / Spark", "directory_listed": False,
        "connect_path": "partnership", "recipe_published": False,
        "next_action": "Publish a modern A2A agent-card.json + test /mcp as a Gemini Enterprise Custom MCP data store AND a Gemini Spark custom connection. NB Google ignores llms.txt — rely on schema+freshness for citations.",
        "owner_gated": False, "effort": "dev (self-serve)",
    },
    {
        "key": "grok", "name": "Grok / xAI", "reach_weight": 0.7,
        "paths": ["mcp"], "reach_token": "grok",
        "bots": [],
        "required_auth": "bearer", "required_transport": "both",
        "directory": "(no public directory)", "directory_listed": None,
        # recipe published 2026-07-17: /integrations/grok one-pager
        # (consumer Custom connector + API Remote MCP block, both Bearer).
        "connect_path": "self_serve", "recipe_published": True,
        "next_action": "Recipe live at /integrations/grok (consumer Custom connector + API Remote MCP block, Bearer). Verify SSE works end-to-end, then unblock X — Grok uniquely cites the X firehose.",
        "owner_gated": False, "effort": "dev + X unblock (owner)",
    },
    {
        "key": "copilot", "name": "Microsoft Copilot", "reach_weight": 0.9,
        "paths": ["mcp", "crawl"], "reach_token": "copilot", "reach_aliases": ["microsoft", "github-copilot"],
        "bots": [],
        "required_auth": "oauth", "required_transport": "streamable",
        "directory": "Copilot Agent Store (Partner Center)", "directory_listed": False,
        # recipe published 2026-07-03: MCP-native wizard runbook + certifiable
        # connector YAML at /integrations/copilot/* + /connect section rewrite.
        "connect_path": "review", "recipe_published": True,
        "next_action": "Wire /mcp into Copilot Studio as a custom MCP server (GA), then submit to the Agent Store via Partner Center. Biggest genuinely-missing enterprise door.",
        "owner_gated": True, "effort": "medium (verification + review)",
    },
    {
        "key": "mistral", "name": "Mistral / Le Chat", "reach_weight": 0.6,
        "paths": ["mcp"], "reach_token": "mistral",
        "bots": [],
        "required_auth": "bearer", "required_transport": "streamable",
        "directory": "Le Chat Connectors (60+)", "directory_listed": False,
        # recipe published 2026-07-17: /integrations/mistral one-pager
        # (Bearer-only — page states explicitly that Le Chat ignores X-API-Key).
        "connect_path": "partnership", "recipe_published": True,
        "next_action": "Recipe live at /integrations/mistral (Bearer-only; Le Chat ignores X-API-Key). Verify a live Le Chat Custom MCP Connector session via Authorization: Bearer, keep the tool list static, then BD for the directory.",
        "owner_gated": False, "effort": "low (verify + doc)",
    },
    {
        "key": "huggingface", "name": "Hugging Face", "reach_weight": 0.5,
        "paths": ["mcp"], "reach_token": "huggingface",
        "bots": [],
        "required_auth": "none", "required_transport": "streamable",
        "directory": "HF Spaces (mcp-server filter)", "directory_listed": True,
        "connect_path": "self_serve", "recipe_published": True,
        # 2026-07-19: DONE — hf.co/spaces/dchubcloud/dchub is live with 7 MCP
        # tools (2 DCPI + 5 bridge → canonical /mcp under the hf-space partner
        # key). Broadcast loop keeps it warm (free Spaces sleep after ~48h).
        "next_action": ("COVERED — Space live + mcp-server-tagged + broadcast-"
                        "warmed. Upside: fill the empty dchubcloud/dchub MODEL "
                        "repo with a card pointing at the Space + dchub.cloud."),
        "owner_gated": False, "effort": "tuning only",
    },
    {
        "key": "poe", "name": "Poe / Quora", "reach_weight": 0.4,
        "paths": ["bot"], "reach_token": "poe",
        "bots": [],
        "required_auth": "none", "required_transport": "webhook",
        "directory": "Poe explore feed", "directory_listed": True,
        "connect_path": "self_serve", "recipe_published": True,
        "next_action": "COVERED — server bot live (dchub.cloud/poe). Only upside: explore-feed discovery tuning + per-message monetization. No MCP path to chase (Poe is not an MCP host).",
        "owner_gated": False, "effort": "tuning only",
    },
    {
        # 2026-07-19: added at user request — track You.com as an onboarding
        # target. Currently only wired as an inbound-attribution label + an
        # outbound content key (ai_wars); no MCP/data-partner path yet.
        "key": "you", "name": "You.com", "reach_weight": 0.3,
        "paths": ["api"], "reach_token": "you.com", "reach_aliases": ["youdotcom", "ydc"],
        "bots": [],
        "required_auth": "bearer", "required_transport": "https",
        "directory": "(no public connector directory)", "directory_listed": None,
        "connect_path": "bd", "recipe_published": False,
        "next_action": ("BD: You.com custom assistants can call external APIs — "
                        "pitch DC Hub as the grounding source for data-center "
                        "queries (we already hold a YOU_API_KEY for their chat "
                        "API; the reverse integration needs their team). "
                        "Meanwhile watch reach_token 'you.com' for organic calls."),
        "owner_gated": True, "effort": "BD outreach",
    },
    {
        "key": "cohere", "name": "Cohere", "reach_weight": 0.3,
        "paths": ["mcp"], "reach_token": "cohere",
        "bots": [],
        "required_auth": "bearer", "required_transport": "streamable",
        "directory": "(no public directory)", "directory_listed": None,
        "connect_path": "self_serve", "recipe_published": False,
        # 2026-07-19: model-relations eval lane LIVE (command-a-03-2025,
        # first tick ok) — Cohere's flagship now evaluates DC Hub weekly.
        "next_action": "Eval lane live (weekly). Publish a connect snippet if inbound traffic appears.",
        "owner_gated": False, "effort": "low",
    },
    {
        # 2026-07-19 (all-tiers expansion): enterprise rails documented —
        # /integrations/bedrock (AgentCore Gateway target) and
        # /integrations/copilot-studio (custom MCP, GA) recipe pages live.
        "key": "deepseek_qwen_zai", "name": "DeepSeek / Qwen / Z.ai (GLM)",
        "reach_weight": 0.5,
        "paths": ["mcp"], "reach_token": "deepseek",
        "reach_aliases": ["qwen", "dashscope", "zai", "glm"],
        "bots": [],
        "required_auth": "bearer", "required_transport": "streamable",
        "directory": "ModelScope (listed via @DCHUBCLOUD/dchub)", "directory_listed": True,
        "connect_path": "self_serve", "recipe_published": False,
        "next_action": ("Eval lanes PRE-WIRED (model_relations: deepseek/qwen/"
                        "zai, fail-soft). Owner: add DEEPSEEK_API_KEY (a few $), "
                        "DASHSCOPE_API_KEY (free tier), or ZAI_API_KEY on "
                        "Railway → that lane starts evaluating weekly."),
        "owner_gated": True, "effort": "keys only",
    },
    {
        "key": "ide_agents", "name": "Cursor / Windsurf / Cline / OpenCode", "reach_weight": 0.6,
        "paths": ["mcp"], "reach_token": "cursor", "reach_aliases": ["windsurf", "cline", "opencode"],
        "bots": [],
        "required_auth": "bearer", "required_transport": "streamable",
        "directory": "Windsurf/Cline marketplaces + registries", "directory_listed": True,
        "connect_path": "self_serve", "recipe_published": True,
        "next_action": "COVERED via registries (Smithery #1) + connect kits. Verify presence in Windsurf's native MCP marketplace + Cline's list; submit if absent.",
        "owner_gated": False, "effort": "low (verify)",
    },
    {
        # 2026-07-17: added at user request — Moonshot API key shared;
        # model_relations "moonshot" eval lane shipped the same day.
        "key": "moonshot", "name": "Moonshot / Kimi", "reach_weight": 0.5,
        "paths": ["mcp"], "reach_token": "kimi", "reach_aliases": ["moonshot", "moonshotai"],
        "bots": [],
        "required_auth": "bearer", "required_transport": "streamable",
        "directory": "(no public connector directory yet)", "directory_listed": None,
        "connect_path": "self_serve", "recipe_published": False,
        "next_action": "Set MOONSHOT_API_KEY on Railway → model-relations lane goes live. Verify Kimi K2 tool-calls /mcp (OpenAI-compatible, Bearer), then publish an /integrations/kimi recipe; K2's agentic long-context push makes it a natural MCP consumer.",
        "owner_gated": False, "effort": "low (key + verify + doc)",
    },
    {
        "key": "bedrock", "name": "Amazon Bedrock AgentCore", "reach_weight": 0.3,
        "paths": ["mcp"], "reach_token": "bedrock",
        "bots": [],
        "required_auth": "oauth", "required_transport": "streamable",
        "directory": "(AgentCore Gateway target — no consumer store)", "directory_listed": None,
        "connect_path": "partnership", "recipe_published": False,
        "next_action": "Keep /mcp OAuth/Bearer-clean so any AWS customer can register it as a Gateway target; document it. Defer BD (enterprise plumbing, no self-serve distribution).",
        "owner_gated": True, "effort": "low (docs), defer BD",
    },
    {
        # 2026-07-18 (webmcp-proto): prototype SHIPPED — /radar, /phx and
        # /integrations/mcp now serve the origin-trial meta tag + register
        # 3 per-page tools each (routes/_webmcp.py) thin-wrapping the same
        # public REST those pages render; the frontend's js/dchub-webmcp.js
        # already covers /markets|/facilities|/dcpi|/grid + static pages.
        "key": "webmcp", "name": "WebMCP (browser agents)", "reach_weight": 0.5,
        "paths": [], "reach_token": None,
        "bots": [],
        "required_auth": "none", "required_transport": "webmcp",
        "directory": "(emerging W3C standard — no directory yet)", "directory_listed": None,
        "connect_path": "standard",
        # 2026-07-18: flipped — with no WebMCP directory, the in-page tools
        # ARE the connect surface (registered via document.modelContext,
        # Chrome 149-156 origin trial).
        "recipe_published": True,
        "next_action": "Prototype LIVE (2026-07-18): /radar, /phx, /integrations/mcp register page tools via routes/_webmcp.py; frontend families via js/dchub-webmcp.js. Next: verify tools surface in Chrome 149+ (Gemini-in-Chrome / model-context inspector) against dchub.cloud, watch platform 'webmcp' attribution + /admin/webmcp lanes, widen to /research + market pages if traffic appears.",
        "owner_gated": False, "effort": "low (verify + widen)",
    },
]

_DIM_WEIGHTS = {
    "reachable": 20,       # our /mcp is up (mcp paths)
    "transport": 10,       # required transport supported
    "auth": 10,            # required auth mode accepted
    "crawl_declared": 15,  # platform's AI bots declared in robots.txt
    "discovery": 30,       # listed in a directory OR a connect recipe published
    "traffic": 15,         # real 7d agents from /api/v1/reach
}


# ══════════════════════════════════════════════════════════════════════
# TIER 1 — MEASURE (live global probes; every one fail-soft)
# ══════════════════════════════════════════════════════════════════════
def _measure_mcp(timeout: int = 20) -> dict:
    """Reachability + tool count + which transports the /mcp handshake speaks."""
    out = {"reachable": False, "tool_count": 0,
           "supports_streamable": False, "supports_sse": False,
           "bearer_accepted": None}
    hdr = {"Content-Type": "application/json",
           "Accept": "application/json, text/event-stream"}
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "clientInfo": {"name": "dchub-onboarding-probe", "version": "1"}}}).encode()
    sid = None
    try:
        req = urllib.request.Request(_MCP_ENDPOINT, data=init, headers=hdr)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            sid = r.headers.get("mcp-session-id")
            out["reachable"] = r.status == 200
            out["supports_sse"] = "text/event-stream" in ctype
            out["supports_streamable"] = ("application/json" in ctype) or bool(sid) or out["supports_sse"]
            body = r.read().decode("utf-8", "replace")
        # tools/list
        hdr2 = dict(hdr)
        if sid:
            hdr2["mcp-session-id"] = sid
        tl = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode()
        req2 = urllib.request.Request(_MCP_ENDPOINT, data=tl, headers=hdr2)
        with urllib.request.urlopen(req2, timeout=timeout) as r2:
            txt = r2.read().decode("utf-8", "replace")
        for ln in txt.splitlines():
            if ln.startswith("data: "):
                ln = ln[6:]
            if ln.startswith("{") and '"id":2' in ln.replace(" ", ""):
                d = json.loads(ln)
                out["tool_count"] = len((d.get("result") or {}).get("tools", []) or [])
                break
    except Exception:
        pass
    # Bearer-auth acceptance: a header-key platform (Le Chat/Grok/Cohere) must be
    # able to authenticate via Authorization: Bearer. Probe with a key from env if
    # present; success = the handshake still 200s with the header attached.
    key = os.environ.get("DCHUB_API_KEY") or os.environ.get("MCP_API_KEY")
    if key:
        try:
            hb = dict(hdr)
            hb["Authorization"] = f"Bearer {key}"
            reqb = urllib.request.Request(_MCP_ENDPOINT, data=init, headers=hb)
            with urllib.request.urlopen(reqb, timeout=timeout) as rb:
                out["bearer_accepted"] = rb.status == 200
        except Exception:
            out["bearer_accepted"] = False
    return out


def _measure_a2a(timeout: int = 15) -> dict:
    """A2A discovery card presence — legacy + modern spec paths."""
    out = {"legacy_agent_json": False, "modern_agent_card_json": False}
    for path, kk in (("/.well-known/agent.json", "legacy_agent_json"),
                     ("/.well-known/agent-card.json", "modern_agent_card_json")):
        try:
            code, body, _ = _get(_PUBLIC.rstrip("/") + path, timeout=timeout)
            out[kk] = bool(code == 200 and body.strip().startswith("{"))
        except Exception:
            pass
    return out


def _measure_robots(timeout: int = 15) -> dict:
    """Which AI bots are DECLARED in robots.txt (explicit mention ≈ allowlisted)."""
    tokens: dict[str, bool] = {}
    txt = ""
    try:
        code, txt, _ = _get(_PUBLIC.rstrip("/") + "/robots.txt", timeout=timeout)
        if code != 200:
            txt = ""
    except Exception:
        txt = ""
    all_bots = sorted({b for p in PLATFORMS for b in p.get("bots", [])})
    low = txt.lower()
    for b in all_bots:
        tokens[b] = b.lower() in low
    return {"robots_ok": bool(txt), "bot_declared": tokens}


def _measure_reach(timeout: int = 20) -> dict:
    """Real per-platform 7d agent counts from the canonical reach endpoint."""
    out: dict[str, int] = {}
    try:
        code, body, _ = _get(_BASE.rstrip("/") + "/api/v1/reach", timeout=timeout)
        if code == 200 and body.strip().startswith("{"):
            d = json.loads(body)
            plats = d.get("platforms_7d") or []
            if isinstance(plats, dict):
                for k, v in plats.items():
                    out[str(k).lower()] = int((v or {}).get("agents", v) if isinstance(v, dict) else v or 0)
            elif isinstance(plats, list):
                for item in plats:
                    if isinstance(item, dict):
                        name = str(item.get("platform") or item.get("name") or "").lower()
                        n = item.get("agents") or item.get("real_agents") or item.get("count") or 0
                        if name:
                            out[name] = int(n or 0)
    except Exception:
        pass
    return out


def tier1_measure() -> dict:
    return {
        "mcp": _measure_mcp(),
        "a2a": _measure_a2a(),
        "robots": _measure_robots(),
        "reach": _measure_reach(),
    }


# ══════════════════════════════════════════════════════════════════════
# TIER 2 — SCORE (per-platform readiness + overall + worklist)
# ══════════════════════════════════════════════════════════════════════
def _reach_for(p: dict, reach: dict) -> int:
    """Real 7d agents for a platform. Reach attribution keys don't always match
    our roster key (ChatGPT traffic lands as 'openai-mcp', Claude as
    'claude-code', …), so match on any alias and sum each reach key at most once."""
    toks = [t for t in ([p.get("reach_token")] + (p.get("reach_aliases") or [])) if t]
    toks = [str(t).lower() for t in toks]
    if not toks:
        return 0
    total = 0
    for k, v in reach.items():
        kl = str(k).lower()
        # match only when a token equals the reach key or is a substring of it
        # (token ⊆ key). NOT key ⊆ token — that lets the generic "mcp" bucket
        # leak into "openai-mcp" and mis-credit ChatGPT.
        if any(t == kl or t in kl for t in toks):
            total += int(v or 0)
    return total


def _discovery_frac(p: dict) -> float:
    if p.get("directory_listed") is True:
        return 1.0
    if p.get("recipe_published"):
        return 0.6
    if p.get("connect_path") == "self_serve":
        return 0.3   # works today but undocumented
    return 0.0


def _score_platform(p: dict, m: dict) -> dict:
    mcp = m.get("mcp") or {}
    robots = (m.get("robots") or {}).get("bot_declared") or {}
    reach = m.get("reach") or {}
    paths = p.get("paths") or []
    is_mcp = "mcp" in paths
    is_crawl = "crawl" in paths

    dims: dict[str, float] = {}      # dim -> earned fraction 0..1
    applicable: dict[str, int] = {}  # dim -> weight

    if is_mcp:
        applicable["reachable"] = _DIM_WEIGHTS["reachable"]
        dims["reachable"] = 1.0 if mcp.get("reachable") else 0.0

        applicable["transport"] = _DIM_WEIGHTS["transport"]
        req_t = p.get("required_transport")
        if req_t == "both":
            dims["transport"] = 1.0 if (mcp.get("supports_streamable") and mcp.get("supports_sse")) else \
                (0.5 if mcp.get("supports_streamable") else 0.0)
        elif req_t == "sse":
            dims["transport"] = 1.0 if mcp.get("supports_sse") else 0.0
        else:  # streamable
            dims["transport"] = 1.0 if mcp.get("supports_streamable") else 0.0

        applicable["auth"] = _DIM_WEIGHTS["auth"]
        if p.get("required_auth") in ("none", "oauth"):
            # no header-key dependency (oauth is per-user, not our concern to serve)
            dims["auth"] = 1.0 if mcp.get("reachable") else 0.0
        else:  # bearer required
            ba = mcp.get("bearer_accepted")
            dims["auth"] = 1.0 if ba else (0.5 if ba is None else 0.0)

    if is_crawl and p.get("bots"):
        applicable["crawl_declared"] = _DIM_WEIGHTS["crawl_declared"]
        bots = p["bots"]
        declared = sum(1 for b in bots if robots.get(b))
        dims["crawl_declared"] = (declared / len(bots)) if bots else 0.0

    # discovery + traffic apply to every platform
    applicable["discovery"] = _DIM_WEIGHTS["discovery"]
    dims["discovery"] = _discovery_frac(p)

    applicable["traffic"] = _DIM_WEIGHTS["traffic"]
    agents = _reach_for(p, reach)
    dims["traffic"] = 1.0 if agents >= 5 else (0.5 if agents >= 1 else 0.0)

    wsum = sum(applicable.values()) or 1
    earned = sum(dims[d] * applicable[d] for d in applicable)
    score = round(100.0 * earned / wsum, 1)

    # weakest applicable dim (lowest earned fraction) → the concrete lever
    weakest = min(applicable, key=lambda d: dims[d]) if applicable else None

    return {
        "key": p["key"], "name": p["name"], "score": score,
        "reach_weight": p["reach_weight"], "agents_7d": agents,
        "dims": {d: round(dims[d], 2) for d in applicable},
        "weakest_dim": weakest,
        "directory": p.get("directory"), "directory_listed": p.get("directory_listed"),
        "connect_path": p.get("connect_path"), "recipe_published": p.get("recipe_published"),
        "next_action": p.get("next_action"),
        "owner_gated": p.get("owner_gated"), "effort": p.get("effort"),
    }


# ── RAG lesson grounding (r-rag-onboarding 2026-07-04) ────────────────
# Attach recalled past-outcome lessons to the top worklist items so each next-
# action carries WHY / prior-art. Fail-soft → []. Gate on cosine (score is the
# rerank scale, r-rag-cosine-passthrough). retrieve_lessons embeds per call, so
# only the highest-priority items are grounded per tick.
def _rag_lessons_for(platform, weakest_dim, k: int = 3) -> list:
    import os as _os
    if _os.getenv("RAG_TOOLDATA_DISABLED") == "1":  # one lever kills all RAG grounding
        return []
    try:
        from routes.brain_rag import retrieve_lessons
    except Exception:
        return []
    q = f"{platform} onboarding {weakest_dim or ''} agent discovery listing outcome".strip()
    try:
        hits = retrieve_lessons(q, k=k) or []
    except Exception:
        return []
    out = []
    for h in hits:
        try:
            if float(h.get("cosine") or 0) < 0.30:
                continue
        except Exception:
            continue
        out.append({
            "kind": h.get("kind"),
            "cosine": round(float(h.get("cosine") or 0), 3),
            "text": (h.get("text") or "").strip()[:240],
        })
    return out


def tier2_score(m: dict) -> dict:
    per = [_score_platform(p, m) for p in PLATFORMS]

    # overall reach-weighted score (opportunity-weighted) + simple mean
    wsum = sum(x["reach_weight"] for x in per) or 1
    weighted = round(sum(x["score"] * x["reach_weight"] for x in per) / wsum, 1)
    simple = round(sum(x["score"] for x in per) / len(per), 1) if per else 0.0

    # worklist: skip already-covered (>=90); rank by opportunity × gap
    worklist = sorted(
        [x for x in per if x["score"] < 90],
        key=lambda x: -(x["reach_weight"] * (100 - x["score"])),
    )
    worklist = [{
        "platform": x["name"], "key": x["key"], "score": x["score"],
        "weakest_dim": x["weakest_dim"], "action": x["next_action"],
        "owner_gated": x["owner_gated"], "effort": x["effort"],
        "priority": round(x["reach_weight"] * (100 - x["score"]), 1),
    } for x in worklist]

    # ground the highest-priority items with recalled lessons (cost-capped to top 3)
    for _it in worklist[:3]:
        _it["rag_lessons"] = _rag_lessons_for(_it.get("platform"), _it.get("weakest_dim"))

    covered = [x["name"] for x in per if x["score"] >= 90]

    return {
        "onboarding_score": weighted,
        "onboarding_score_unweighted": simple,
        "platforms": per,
        "worklist": worklist,
        "covered": covered,
        "platform_count": len(per),
    }


# ══════════════════════════════════════════════════════════════════════
# TIER 3 — PERSIST (direct cursor DDL + INSERT; read-only elsewhere)
# ══════════════════════════════════════════════════════════════════════
def _persist(score: float | None, scored: dict, measures: dict) -> bool:
    conn = None
    try:
        from main import get_pg_connection
    except Exception:
        return False
    try:
        conn = get_pg_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_onboarding_snapshots (
                id             BIGSERIAL PRIMARY KEY,
                score          REAL,
                platforms_json JSONB,
                worklist_json  JSONB,
                measures_json  JSONB,
                created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute(
            "INSERT INTO agent_onboarding_snapshots (score, platforms_json, worklist_json, measures_json) "
            "VALUES (%s, %s, %s, %s)",
            (score, json.dumps(scored.get("platforms") or []),
             json.dumps(scored.get("worklist") or []),
             json.dumps(measures or {})),
        )
        conn.commit()
        return True
    except Exception:
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        return False
    finally:
        _close(conn)


# ── endpoints ─────────────────────────────────────────────────────────
@agent_onboarding_master_shell_bp.route(
    "/api/v1/admin/agent-onboarding/master-tick", methods=["POST", "GET"])
def master_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="AGENT_ONBOARDING_MASTER_DISABLED"), 200
    started = time.time()
    measures = tier1_measure()
    scored = tier2_score(measures)
    persisted = _persist(scored.get("onboarding_score"), scored, measures)
    return jsonify(
        ok=True,
        ms=int((time.time() - started) * 1000),
        onboarding_score=scored.get("onboarding_score"),
        onboarding_score_unweighted=scored.get("onboarding_score_unweighted"),
        covered=scored.get("covered"),
        worklist=scored.get("worklist"),
        platforms=scored.get("platforms"),
        measures=measures,
        persisted=persisted,
        generated_at=datetime.now(timezone.utc).isoformat(),
    ), 200


@agent_onboarding_master_shell_bp.route(
    "/api/v1/admin/agent-onboarding/state", methods=["GET"])
def state():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    conn = None
    try:
        from main import get_pg_connection
    except Exception:
        return jsonify(error="db_unavailable"), 503
    try:
        conn = get_pg_connection()
        cur = conn.cursor()
        try:
            cur.execute("SET LOCAL statement_timeout = 8000")
        except Exception:
            pass
        latest = None
        try:
            cur.execute(
                "SELECT id, score, platforms_json, worklist_json, created_at "
                "FROM agent_onboarding_snapshots ORDER BY id DESC LIMIT 1")
            r = cur.fetchone()
            if r:
                latest = {"id": r[0], "score": r[1], "platforms": r[2],
                          "worklist": r[3], "created_at": str(r[4])}
        except Exception:
            pass
        trend = []
        try:
            cur.execute(
                "SELECT score, created_at FROM agent_onboarding_snapshots "
                "ORDER BY id DESC LIMIT 30")
            trend = [{"score": row[0], "created_at": str(row[1])}
                     for row in (cur.fetchall() or [])]
            trend.reverse()
        except Exception:
            pass
        return jsonify(latest=latest, trend=trend, count=len(trend)), 200
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {str(e)[:120]}"), 500
    finally:
        _close(conn)
