"""routes/ai_platform_tool_tuner.py — Unlock 2: Per-platform MCP tool descriptions (2026-06-07).

Today's MCP tool descriptions are general-purpose. Different agents have
different conventions: Claude's tool-use cycle expects Anthropic-style
descriptions, Cursor reads them inside an IDE workflow, Cline expects
context-window-aware brevity, ChatGPT's plugin path leans on action
verbs, Perplexity surfaces them as citation-source titles.

This module:
  1. Stores per-(platform, tool_name) description overrides in
     `mcp_tool_descriptions_per_platform` (schema in schema_repair.py).
  2. Exposes a Claude-driven generator that produces the initial variants
     (top-10 tools × tuned platforms — 5 seeded 2026-06-07, expanded to 11
     on 2026-07-11: +gemini/grok/copilot/meta/deepseek/mistral, driven by
     the Agent-Enablement Portal's live request mix — Meta AI is the #3
     requester with zero prior enablement).
  3. Exposes a read endpoint /api/v1/mcp/tool-descriptions?platform=X
     that the MCP server hits at tool/list time. Falls back to the
     generic description when no override exists.

Schema (added to SCHEMA_STATEMENTS):
  mcp_tool_descriptions_per_platform(
    platform     TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    description  TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    generated_by TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(platform, tool_name)
  )

Endpoints:
  GET  /api/v1/mcp/tool-descriptions?platform=claude
  POST /api/v1/admin/mcp/tool-tuner/seed         — Claude-generate 50 variants
  POST /api/v1/admin/mcp/tool-tuner/upsert       — manual override
  GET  /api/v1/admin/mcp/tool-tuner/coverage     — what platforms × tools we have

Safety:
  * Kill switch: AI_AGENT_EXPANSION_DISABLE=1
  * Seed is bounded: at most len(TOP_10_TOOLS) × len(TUNED_PLATFORMS)
    Claude calls per /seed invocation (110 at 11 platforms)
  * Read endpoint is public (descriptions are not secret) but cached 5min
  * Generator is admin-keyed
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

from flask import Blueprint, jsonify, request

from internal_auth import accepted_internal_keys


logger = logging.getLogger(__name__)
ai_platform_tool_tuner_bp = Blueprint("ai_platform_tool_tuner", __name__)


# ── Auth ─────────────────────────────────────────────────────────────
_INTERNAL_KEYS = accepted_internal_keys()
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


def _disabled() -> bool:
    return os.environ.get("AI_AGENT_EXPANSION_DISABLE", "").strip() == "1"


# ── Config: top-10 tools + tuned platforms ───────────────────────────
TOP_10_TOOLS = [
    "search_facilities",
    "get_facility",
    "get_market_intel",
    "rank_markets",
    "get_grid_intelligence",
    "get_fiber_intel",
    "compare_isos",
    "compare_sites",
    "get_pipeline",
    "site_selection_canvas",
]

# Each entry: (canonical name, lowercase aliases for UA-sniff, voice cue)
# 2026-07-11 EXPANSION: 5 → 11 platforms (+gemini, grok, copilot, meta,
# deepseek, mistral). Rationale: live 7d request mix has Meta AI at #3
# (32K req/wk) with zero enablement, and the portal's weakest onboarding
# scores include Mistral (41.2), Grok (51.8), Gemini (55.0). Aliases must
# stay lowercase; they are matched BOTH as exact platform tags (see
# main.py MCP_PLATFORM_MAP canonicalization → lowercased in
# _outcome_signal) and as UA substrings.
TUNED_PLATFORMS = [
    ("claude",     ["claude", "anthropic", "claude-desktop", "claudebot"],
     "Anthropic Claude · concise, factual, terms-of-art aware; "
     "Claude expects 'tool that does X' phrasing."),
    ("cursor",     ["cursor", "cursor-ai"],
     "Cursor IDE · developer workflow inside a code editor; "
     "frame descriptions around inline code/comment lookups."),
    ("cline",      ["cline", "claude-dev"],
     "Cline VS Code agent · operates inside a coding agent with "
     "small context windows. Brevity matters — drop trailing prose."),
    ("chatgpt",    ["chatgpt", "openai", "gpt-4", "gpt4"],
     "OpenAI ChatGPT · plugin-style action verbs; descriptions read "
     "to the model like a function spec."),
    ("perplexity", ["perplexity", "perplexitybot"],
     "Perplexity Answer Engine · descriptions surface as citation "
     "source titles. Treat them like a 1-line search result."),
    # ── 2026-07-11 expansion wave ────────────────────────────────────
    ("gemini",     ["gemini", "google-gemini", "gemini-cli", "google-genai",
                    "vertex"],
     "Google Gemini · descriptions read like typed function declarations; "
     "state capability, scope, and what comes back in plain spec-like "
     "language — Gemini selects on explicit parameter/result clarity."),
    ("grok",       ["grok", "xai", "grokbot"],
     "xAI Grok · direct, no-hedge phrasing; lead with the live/real-time "
     "angle — Grok favors tools that promise current signals over archives."),
    ("copilot",    ["copilot", "ms-copilot", "microsoft-copilot", "bingchat"],
     "Microsoft Copilot · enterprise assistant inside Office/365 and Bing "
     "workflows; frame outputs as business-ready answers (markets, costs, "
     "risk) with plugin-style action verbs."),
    ("meta",       ["meta", "meta-ai", "metaai", "llama-stack",
                    "meta-externalagent", "meta-externalfetcher"],
     "Meta AI / Llama · llama-stack tool runtime feeds descriptions "
     "verbatim into small agent contexts; short, literal, verb-first — "
     "say exactly what the tool returns, no flourish."),
    ("deepseek",   ["deepseek", "deepseek-chat", "deepseek-coder"],
     "DeepSeek · OpenAI-compatible function calling with an engineering-"
     "heavy user base; terse spec-style wording with concrete nouns and "
     "counts beats marketing phrasing."),
    ("mistral",    ["mistral", "lechat", "le-chat", "le chat"],
     "Mistral Le Chat / Agents API · connector tools surface with minimal "
     "chrome; one compact sentence with explicit data scope wins tool "
     "selection."),
    # 2026-07-17: Moonshot/Kimi added (user-requested — partner key shared;
    # model-relations lane live the same day).
    ("kimi",       ["kimi", "moonshot", "kimi-k2", "moonshotai"],
     "Moonshot Kimi · OpenAI-compatible tool calling with long-context "
     "agentic use; literal spec-style descriptions with explicit counts "
     "and units — Kimi K2 selects tools on concrete capability claims."),
]

# Back-compat alias — the seed shipped 2026-06-07 as literally the top-5;
# keep the old name pointing at the full tuned list.
TOP_5_PLATFORMS = TUNED_PLATFORMS


PLATFORM_MAP = {alias: canon for canon, aliases, _ in TUNED_PLATFORMS for alias in aliases}


def _platform_from_ua(ua: str) -> str | None:
    """Lower-case substring match against TUNED_PLATFORMS."""
    if not ua:
        return None
    low = ua.lower()
    for canon, aliases, _ in TUNED_PLATFORMS:
        for a in aliases:
            if a in low:
                return canon
    return None


# ── DB ───────────────────────────────────────────────────────────────
def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _put_db(c):
    """Release a pooled connection from _get_db() BACK to the write pool.
    Without this the connection is held until the 60-89s FORCED RECLAIM
    (main.py) — and the read endpoint below is hit by the gateway's warm-cache
    refresher (5 platforms, every 30 min, via a ThreadPoolExecutor), so a burst
    of un-returned connections starves the pool and degrades unrelated internal
    fetches across the app. get_db()==get_pg_connection() (write pool), so the
    correct release is return_pg_connection(). Fail-soft."""
    if c is None:
        return
    try:
        from main import return_pg_connection
        return_pg_connection(c)
    except Exception:
        try:
            c.close()
        except Exception:
            pass


def _ensure_table(c) -> None:
    """Self-heal — create the table inline if schema_repair hasn't run."""
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mcp_tool_descriptions_per_platform (
                    id SERIAL PRIMARY KEY,
                    platform     TEXT NOT NULL,
                    tool_name    TEXT NOT NULL,
                    description  TEXT NOT NULL,
                    version      INTEGER NOT NULL DEFAULT 1,
                    generated_by TEXT,
                    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(platform, tool_name)
                )""")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_mtdpp_lookup "
                "ON mcp_tool_descriptions_per_platform(platform, tool_name)")
        try: c.commit()
        except Exception: pass
    except Exception as e:
        logger.warning("_ensure_table failed: %s", e)
        try: c.rollback()
        except Exception: pass


# ── Generic tool descriptions (fallback when no override exists) ─────
# Hardcoded snapshot of the live MCP /tools/list as of 2026-06-07.
# The MCP server itself remains the source of truth; this is just used
# as the source string for Claude's per-platform rewrite.
GENERIC_DESCRIPTIONS = {
    "search_facilities":      ("Search 15,000+ global data-center facilities by "
                               "city, operator, status, capacity, and more."),
    "get_facility":           ("Detailed profile of a single facility: capacity, "
                               "operator, location, power source, infrastructure."),
    "get_market_intel":       ("Market-level intelligence for any DCPI market: "
                               "supply, demand, vacancy, pipeline, and movers."),
    "rank_markets":           ("Rank DC markets by a composite score or any "
                               "single dimension (capacity, growth, power cost)."),
    "get_grid_intelligence":  ("Live grid signals for any ISO: headroom, queue, "
                               "fuel mix, pricing — the operator decision layer."),
    "get_fiber_intel":        ("Fiber and network reach for a market: providers, "
                               "long-haul routes, latency to major hubs."),
    "compare_isos":           ("Side-by-side comparison of 2+ ISOs across "
                               "headroom, interconnect cost, fuel mix, queue."),
    "compare_sites":          ("Side-by-side comparison of 2+ facility sites "
                               "with capacity, power, fiber, and risk overlays."),
    "get_pipeline":           ("Upcoming + announced data-center projects: who, "
                               "where, MW, expected COD, status."),
    "site_selection_canvas":  ("Multi-criteria site-selection workspace: weight "
                               "power, fiber, water, land, taxes, deliver ranks."),
}


# ── Canonical descriptions (live tools/list SoT) ─────────────────────
def _mcp_tools_list_url() -> str:
    """The live MCP endpoint to pull canonical tool descriptions from.
    Precedence: DCHUB_MCP_URL env > server.json remotes[0].url (the published
    SoT) > the known public remote."""
    env = (os.environ.get("DCHUB_MCP_URL") or "").strip()
    if env:
        return env
    try:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "server.json"), "r", encoding="utf-8") as fh:
            sj = json.load(fh)
        for r in (sj.get("remotes") or []):
            u = (r.get("url") or "").strip()
            if u:
                return u
    except Exception:
        pass
    return "https://dchub.cloud/mcp"


def _canonical_descriptions() -> dict:
    """2026-07-04 FIX: source the per-tool descriptions the seed feeds to Claude
    from the LIVE tools/list SoT instead of the frozen 2026-06-07
    GENERIC_DESCRIPTIONS snapshot. That snapshot overstated search_facilities'
    scope (an inflated facility count vs the canonical 15,000+) and the seed baked
    the stale figure into every per-platform rewrite.

    Returns {tool_name: description} for TOP_10_TOOLS, starting from
    GENERIC_DESCRIPTIONS and overlaying whatever the live endpoint returns.
    Fully fail-soft: any fetch/parse error leaves the frozen fallback in place so
    the seed still runs when the MCP server is unreachable."""
    out = dict(GENERIC_DESCRIPTIONS)
    try:
        import requests
        url = _mcp_tools_list_url()
        resp = requests.post(
            url,
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1, "params": {}},
            headers={"Accept": "application/json",
                     "User-Agent": "dchub-tool-tuner/1.0"},
            timeout=20)
        data = resp.json()
        tools = ((data.get("result") or {}).get("tools")) or []
        wanted = set(TOP_10_TOOLS)
        found = 0
        for t in tools:
            name = t.get("name")
            desc = (t.get("description") or "").strip()
            if name in wanted and desc:
                out[name] = desc
                found += 1
        logger.info("[tool-tuner] canonical descriptions: %d/%d tools from live "
                    "tools/list (%s)", found, len(TOP_10_TOOLS), url)
    except Exception as e:
        logger.warning("[tool-tuner] live tools/list fetch failed (%s); using "
                       "frozen GENERIC_DESCRIPTIONS", e)
    return out


# ── Claude-driven generator ──────────────────────────────────────────
def _outcome_signal(c) -> dict:
    """r-tuner-loop (2026-06-26): close the tuner loop. Per-(platform, tool)
    ADOPTION signal — calls in the last 30d — so the reseed tunes UNDER-adopted
    (platform, tool) cells toward more calls. A tool DESCRIPTION's job is
    ADOPTION (getting an agent to choose the tool); response-level retention/$10
    conversion is tuned separately in the gateway. Returns {(platform, tool):
    calls}. Fail-soft → {} so the seed loop falls back to the prior no-signal
    behavior and never breaks on this best-effort read.

    2026-07-04 FIX: was querying mcp_call_log (columns: "timestamp", tool) which
    has NO platform column — so the SELECT raised, the except swallowed it, and
    this returned {} on every reseed (a silent no-op that never surfaced). Repoint
    to mcp_tool_calls, whose platform + client_name are populated by the live MCP
    gateway (see main.py schema / flask_mcp_endpoints insert). Canonicalize the
    raw platform/client_name to one of the TUNED_PLATFORMS; rows we can't map
    are dropped (best-effort signal only), so an all-'mcp'/'unknown' table simply
    yields {} and the rewrites behave exactly as no-signal."""
    out: dict = {}
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT lower(coalesce(platform,'')) AS p, "
                "       lower(coalesce(client_name,'')) AS cn, "
                "       tool_name AS t, count(*) AS n "
                "FROM mcp_tool_calls "
                "WHERE created_at > now() - interval '30 days' "
                "  AND tool_name IS NOT NULL AND tool_name <> '' "
                "GROUP BY 1, 2, 3")
            for p, cn, t, n in cur.fetchall():
                if not t:
                    continue
                # Resolve to a canonical tuned platform: exact alias on the
                # platform tag, then substring, then the same on client_name.
                canon = (PLATFORM_MAP.get(p) or _platform_from_ua(p)
                         or PLATFORM_MAP.get(cn) or _platform_from_ua(cn))
                if not canon:
                    continue
                out[(canon, t)] = out.get((canon, t), 0) + int(n or 0)
    except Exception:
        try: c.rollback()
        except Exception: pass
        return {}
    return out


def _fmt_adoption(calls: int, tool_max: int, platform: str) -> str:
    """One (platform, tool) cell's adoption numbers → a short tuning instruction
    the rewrite prompt acts on. Empty when there's no signal yet (rewrite then
    behaves exactly as before)."""
    if not tool_max:
        return ""
    if calls <= max(2, int(tool_max * 0.1)):
        return (f"{platform} agents called this tool only {calls}x in 30d vs {tool_max}x "
                f"on its strongest platform — UNDER-ADOPTED on {platform}. Make the wording "
                f"unmistakably concrete and useful for a {platform} agent's workflow so more "
                f"of them choose to call it.")
    return (f"{platform} agents already call this tool {calls}x/30d — adopted here; keep "
            f"what works and only sharpen clarity.")


def _claude_rewrite(tool_name: str, generic_desc: str, platform: str,
                    voice_cue: str, outcome: str = "") -> str | None:
    """Generate a per-platform-tuned tool description via Claude. Returns
    None on failure (caller falls back to generic)."""
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return None
    try:
        from utils.anthropic_helper import anthropic_messages_url
        url = anthropic_messages_url()
    except Exception:
        url = "https://api.anthropic.com/v1/messages"
    # 2026-07-04 FIX: this used to seed resolve_chain() with the RETIRED,
    # hard-coded "claude-sonnet-5" (DCHUB_BRAIN_MODEL_ROUTINE /
    # DCHUB_BRAIN_MODEL are unset on the web service). That tag isn't a key in
    # brain_models._FALLBACK_CHAIN, so resolve_chain returned a DEAD single-
    # element list → every Anthropic call 404'd → the tuner failed 100% (and
    # the except below hid it). Resolve via brain_model_for("routine"), which
    # honours the env overrides AND reachability and defaults to a live model
    # (claude-sonnet-4-5), then walk its real fallback chain. Guarantee at least
    # one confirmed-live terminal rung so a stray retired env override can never
    # zero the tuner out again.
    try:
        from routes.brain_models import brain_model_for, resolve_chain
        models = resolve_chain(brain_model_for("routine")) or []
    except Exception as e:
        logger.warning("[tool-tuner] model resolution failed (%s); "
                       "using routine default", e)
        models = []
    for _live in ("claude-sonnet-4-5", "claude-haiku-4-5"):
        if _live not in models:
            models.append(_live)
    prompt = (
        f"Rewrite this MCP tool description tuned for {platform.upper()}.\n\n"
        f"TOOL NAME: {tool_name}\n"
        f"GENERIC DESCRIPTION: {generic_desc}\n\n"
        f"PLATFORM VOICE: {voice_cue}\n\n"
        "Constraints:\n"
        "  - 1-2 sentences\n"
        "  - 280 characters max\n"
        "  - Plain text, no markdown, no code fences\n"
        # 2026-07-25: agent stores (Microsoft Copilot Agent Store policy 1140.9)
        # reject tool descriptions containing URLs or emoji. Forbid both so every
        # tuned description is submission-safe across platforms, not just chat.
        "  - NO URLs / links and NO emoji (agent stores reject either)\n"
        "  - Preserve all data the generic claims (counts, scope)\n"
        "  - Lead with action (verb-first when natural)\n"
        "  - Do NOT include the tool name in the description\n\n"
        + (f"ADOPTION CONTEXT (use ONLY to decide emphasis/clarity — do NOT put any "
           f"numbers or this note in your output): {outcome}\n\n" if outcome else "")
        + "Output ONLY the rewritten description, nothing else."
    )
    for i, model in enumerate(models[:3]):
        try:
            body = json.dumps({
                "model": model,
                # 2026-07-10: 300 → 2000. brain_model_for('routine') can
                # resolve to a THINKING-tier model, and thinking tokens
                # count against max_tokens — at 300 the model's reasoning
                # ate the whole budget and the text block came back empty
                # (same trap as the brain's Fable restore). 2000 leaves
                # headroom for thinking + the ≤280-char description; the
                # [:280] clamp below still bounds the stored output.
                "max_tokens": 2000,
                "system": ("You are an MCP tool-description copywriter. You "
                           "tune one tool description at a time for a specific "
                           "AI platform. You output ONLY the description."),
                "messages": [{"role": "user", "content": prompt}],
            }).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
                "User-Agent": "dchub-tool-tuner/1.0",
                "Anthropic-Version": "2023-06-01",
            })
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.loads(r.read().decode("utf-8"))
            for block in (data.get("content") or []):
                if block.get("type") == "text":
                    text = (block.get("text") or "").strip().strip('"').strip("'")
                    if text:
                        return text[:280]
            return None
        except urllib.error.HTTPError as e:
            if e.code in (404, 400) and i + 1 < len(models):
                logger.warning("[tool-tuner] %s/%s model=%s HTTP %s — trying "
                               "next fallback rung", platform, tool_name, model, e.code)
                continue
            logger.warning("[tool-tuner] %s/%s model=%s HTTP %s — no more rungs, "
                           "giving up", platform, tool_name, model, e.code)
            return None
        except Exception as e:
            logger.warning("[tool-tuner] %s/%s model=%s rewrite error: %s",
                           platform, tool_name, model, e)
            return None
    return None


def _upsert(c, platform: str, tool_name: str, description: str,
            generated_by: str) -> None:
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO mcp_tool_descriptions_per_platform
                    (platform, tool_name, description, version, generated_by, updated_at)
                VALUES (%s, %s, %s, 1, %s, NOW() ON CONFLICT DO NOTHING)
                ON CONFLICT (platform, tool_name) DO UPDATE
                   SET description = EXCLUDED.description,
                       version = mcp_tool_descriptions_per_platform.version + 1,
                       generated_by = EXCLUDED.generated_by,
                       updated_at = NOW()
            """, (platform, tool_name, description, generated_by))
        try: c.commit()
        except Exception: pass
    except Exception as e:
        logger.warning("_upsert %s/%s failed: %s", platform, tool_name, e)
        try: c.rollback()
        except Exception: pass


# ── Endpoints ────────────────────────────────────────────────────────
# 5-minute in-memory cache. The MCP server reads this on every
# tool/list call, so a TTL avoids DB pressure under load.
_TUNED_CACHE: dict = {"data": {}, "at": 0.0}
_TUNED_TTL_S = 300


def _load_all_tuned(c) -> dict:
    """Returns {platform: {tool_name: description}}. Cached 5min."""
    now = time.time()
    if (now - _TUNED_CACHE["at"]) < _TUNED_TTL_S and _TUNED_CACHE["data"]:
        return _TUNED_CACHE["data"]
    out: dict = {}
    try:
        _ensure_table(c)
        with c.cursor() as cur:
            cur.execute("SELECT platform, tool_name, description, version, "
                        "       to_char(updated_at, 'YYYY-MM-DD\"T\"HH24:MI:SSOF') "
                        "FROM mcp_tool_descriptions_per_platform")
            for plat, tool, desc, ver, upd in cur.fetchall():
                out.setdefault(plat, {})[tool] = {
                    "description": desc, "version": ver, "updated_at": upd}
    except Exception as e:
        logger.warning("_load_all_tuned failed: %s", e)
        try: c.rollback()
        except Exception: pass
    _TUNED_CACHE["data"] = out
    _TUNED_CACHE["at"] = now
    return out


@ai_platform_tool_tuner_bp.route("/api/v1/mcp/tool-descriptions", methods=["GET"])
def get_tool_descriptions():
    """Public read endpoint. The MCP server hits this at tool/list time
    to overlay platform-tuned descriptions on the canonical tool list.

    Query params:
      ?platform=claude  — return only this platform's overrides
      (omitted)         — return all platforms
      ?ua=<user-agent>  — server-side UA-sniff fallback

    Response:
      {ok: true, platform: 'claude', overrides: {tool_name: 'desc', ...},
       fallback_generic: {tool_name: 'generic_desc', ...}}
    """
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        plat = (request.args.get("platform") or "").strip().lower()
        if not plat:
            ua = request.args.get("ua") or request.headers.get("User-Agent", "")
            plat = _platform_from_ua(ua) or ""
        plat = PLATFORM_MAP.get(plat, plat)
        all_tuned = _load_all_tuned(c)
        if plat:
            overrides = {k: v["description"]
                         for k, v in (all_tuned.get(plat) or {}).items()}
            return jsonify(ok=True, platform=plat, overrides=overrides,
                           fallback_generic=GENERIC_DESCRIPTIONS,
                           count=len(overrides))
        # No platform → return everything (admin/inspection use)
        return jsonify(ok=True, platform=None,
                       all_overrides=all_tuned,
                       fallback_generic=GENERIC_DESCRIPTIONS,
                       platforms_with_overrides=len(all_tuned))
    finally:
        _put_db(c)


@ai_platform_tool_tuner_bp.route("/api/v1/admin/mcp/tool-tuner/seed",
                                 methods=["POST"])
def seed_variants():
    """Generate the initial per-platform variants (top-10 tools × the
    TUNED_PLATFORMS list — 110 cells at 11 platforms). Bounded; idempotent;
    skips entries that already exist unless ?force=1 is set — so after the
    2026-07-11 platform expansion a plain (no-force) seed fills ONLY the
    six new platforms and leaves the 5 seeded ones untouched."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if _disabled():
        return jsonify(ok=True, skipped=True,
                       reason="AI_AGENT_EXPANSION_DISABLE=1"), 200
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        _ensure_table(c)
        force = (request.args.get("force") or "").strip() == "1"
        existing: set = set()
        try:
            with c.cursor() as cur:
                cur.execute("SELECT platform, tool_name "
                            "FROM mcp_tool_descriptions_per_platform")
                existing = {(p, t) for p, t in cur.fetchall()}
        except Exception:
            try: c.rollback()
            except Exception: pass

        started = time.time()
        written: list = []
        skipped: list = []
        failed: list = []
        api_key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))

        # r-tuner-loop: pull the per-(platform, tool) adoption signal ONCE, and the
        # per-tool max across platforms, so each rewrite knows whether its cell is
        # under-adopted on that platform. Fail-soft (empty → no-signal rewrites).
        _adopt = _outcome_signal(c)
        _tool_max: dict = {}
        for (_p, _t), _n in _adopt.items():
            if _n > _tool_max.get(_t, 0):
                _tool_max[_t] = _n

        # Pull canonical per-tool descriptions from the live tools/list SoT (falls
        # back to the frozen GENERIC_DESCRIPTIONS) so we never reseed the stale
        # inflated-count copy again.
        canonical = _canonical_descriptions()

        # Build the work list first (skip existing unless force).
        jobs = []  # (canon, tool_name, generic, voice)
        for tool_name in TOP_10_TOOLS:
            generic = canonical.get(tool_name, tool_name)
            for canon, _aliases, voice in TUNED_PLATFORMS:
                if (canon, tool_name) in existing and not force:
                    skipped.append({"platform": canon, "tool": tool_name,
                                    "reason": "already_exists"})
                    continue
                jobs.append((canon, tool_name, generic, voice))

        if not api_key_present:
            # Without Claude, write deterministic light variants so the table is at
            # least populated. Fast — do inline. Re-run once ANTHROPIC_API_KEY is set.
            for canon, tool_name, generic, _voice in jobs:
                deterministic = f"[{canon}] {generic}"[:280]
                _upsert(c, canon, tool_name, deterministic, "deterministic_no_claude")
                written.append({"platform": canon, "tool": tool_name,
                                "via": "deterministic"})
        elif jobs:
            # 2026-07-04: the Claude rewrites are the whole cost (~50 sequential
            # network calls → ~4 min). Run at ~230s the web worker gets recycled
            # mid-seed (observed: a run completed only 17/50 before a 502) and the
            # request can't return through Railway's edge. Fan the network-bound
            # rewrites out over a small pool so the whole seed finishes in ~30-40s —
            # short enough to return cleanly and never block the worker long enough
            # to be killed. DB writes stay sequential on this request's connection.
            from concurrent.futures import ThreadPoolExecutor

            def _rewrite_one(job):
                canon, tool_name, generic, voice = job
                tuned = _claude_rewrite(
                    tool_name, generic, canon, voice,
                    _fmt_adoption(_adopt.get((canon, tool_name), 0),
                                  _tool_max.get(tool_name, 0), canon))
                return (canon, tool_name, tuned)

            with ThreadPoolExecutor(max_workers=6) as ex:
                for canon, tool_name, tuned in ex.map(_rewrite_one, jobs):
                    if tuned:
                        _upsert(c, canon, tool_name, tuned, "claude")
                        written.append({"platform": canon, "tool": tool_name,
                                        "via": "claude"})
                    else:
                        failed.append({"platform": canon, "tool": tool_name})

        # Bust cache so the next read endpoint call sees fresh data.
        _TUNED_CACHE["data"] = {}
        _TUNED_CACHE["at"] = 0.0

    finally:
        _put_db(c)  # issue #1655: release on ALL paths — an exception mid-seed leaked the conn

    n_written, n_skipped, n_failed = len(written), len(skipped), len(failed)
    # 2026-07-04 FIX: this always returned 200 ok:true, even when written==0 and
    # failed==50 — so the 100% model-404 failure looked healthy for weeks. A run
    # that wrote NOTHING but had failures is a hard error: 500 ok:false so the
    # reseed workflow and any monitor go RED. (skipped-only, e.g. every cell
    # already existed without ?force=1, stays a healthy 200.)
    unhealthy = (n_written == 0 and n_failed > 0)
    payload = dict(
        ok=(not unhealthy),
        written=n_written,
        skipped=n_skipped,
        failed=n_failed,
        elapsed_s=round(time.time() - started, 2),
        anthropic_key_present=api_key_present,
        detail={"written": written[:30],
                "skipped": skipped[:30],
                "failed": failed[:30]},
    )
    if unhealthy:
        payload["error"] = "all_writes_failed"
        return jsonify(**payload), 500
    return jsonify(**payload), 200


@ai_platform_tool_tuner_bp.route("/api/v1/admin/mcp/tool-tuner/upsert",
                                 methods=["POST"])
def manual_upsert():
    """Operator override: POST JSON {platform, tool_name, description}."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    body = request.get_json(silent=True) or {}
    plat = (body.get("platform") or "").strip().lower()
    tool = (body.get("tool_name") or "").strip()
    desc = (body.get("description") or "").strip()
    if not plat or not tool or not desc:
        return jsonify(ok=False, error="missing_field",
                       required=["platform", "tool_name", "description"]), 400
    if len(desc) > 280:
        return jsonify(ok=False, error="description_too_long", max=280), 400
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        _ensure_table(c)
        _upsert(c, plat, tool, desc, body.get("generated_by") or "manual")
        _TUNED_CACHE["data"] = {}
        _TUNED_CACHE["at"] = 0.0
        return jsonify(ok=True, platform=plat, tool_name=tool, version_bumped=True)
    finally:
        _put_db(c)


@ai_platform_tool_tuner_bp.route("/api/v1/admin/mcp/tool-tuner/coverage",
                                 methods=["GET"])
def coverage():
    """Matrix of which (platform, tool) cells have an override."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    try:
        _ensure_table(c)
        all_tuned = _load_all_tuned(c)
        matrix = {}
        for canon, _, _ in TUNED_PLATFORMS:
            row = {}
            for tool in TOP_10_TOOLS:
                row[tool] = (canon in all_tuned and tool in all_tuned[canon])
            matrix[canon] = row
        total_cells = len(TUNED_PLATFORMS) * len(TOP_10_TOOLS)
        filled = sum(1 for canon in matrix for t in matrix[canon] if matrix[canon][t])
        # Per-platform freshness — the whole point of the reseed. A fresh, non-
        # cached read so a monitor can assert every platform's newest row is
        # recent (this tuner silently froze at version 1 for weeks).
        freshness: dict = {}
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT platform, count(*), "
                    "  to_char(min(updated_at),'YYYY-MM-DD\"T\"HH24:MI:SSOF'), "
                    "  to_char(max(updated_at),'YYYY-MM-DD\"T\"HH24:MI:SSOF'), "
                    "  min(version), max(version), "
                    "  round(extract(epoch from (now()-max(updated_at)))/3600.0, 1) "
                    "FROM mcp_tool_descriptions_per_platform GROUP BY platform")
                for plat, cnt, oldest, newest, vmin, vmax, age_h in cur.fetchall():
                    freshness[plat] = {"cells": int(cnt or 0),
                                       "oldest_updated_at": oldest,
                                       "newest_updated_at": newest,
                                       "min_version": vmin, "max_version": vmax,
                                       "newest_age_hours": float(age_h) if age_h is not None else None}
        except Exception as e:
            logger.warning("[tool-tuner] coverage freshness query failed: %s", e)
            try: c.rollback()
            except Exception: pass
        return jsonify(ok=True, total_cells=total_cells, filled=filled,
                       percent=round(100 * filled / max(total_cells, 1), 1),
                       matrix=matrix, freshness=freshness, top_tools=TOP_10_TOOLS,
                       top_platforms=[p for p, _, _ in TUNED_PLATFORMS])
    finally:
        _put_db(c)


# ── Smoke ────────────────────────────────────────────────────────────
def _smoke():
    logger.info("[ai_platform_tool_tuner] loaded · disabled=%s · %d cells "
                "(%d tools × %d platforms)", _disabled(),
                len(TOP_10_TOOLS) * len(TUNED_PLATFORMS),
                len(TOP_10_TOOLS), len(TUNED_PLATFORMS))


_smoke()
