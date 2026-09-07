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
  * Seed is bounded: at most len(TUNED_TOOLS) × len(TUNED_PLATFORMS)
    Claude calls per /seed invocation (132 at 11 tools x 12 platforms)
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
from ai_surface_canon import canon_text


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
# ★ RENAMED from TUNED_TOOLS (2026-09-06). The list is no longer ten, and a
# constant whose name states its own length becomes a lie the moment it grows —
# the next reader trusts the name over the contents.
TUNED_TOOLS = [
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
    # ── added 2026-09-06 ────────────────────────────────────────────────
    # get_market_dcpi_rank was the widest-reach UNTUNED tool. Measured over
    # mcp_tool_calls, 30d, net of chain-hire and our own buckets/registries:
    #
    #     clients  calls  tool                      tuned?
    #          13    135  search_facilities         yes
    #          10     92  get_market_dcpi_rank      NO   <-- this
    #           9    456  analyze_site              no (95% ONE caller)
    #           8     17  get_interconnection_queue NO
    #
    # ★ RANKED BY DISTINCT CLIENTS, NOT CALLS, and that is the whole point.
    # By raw volume the top tool was `search` at 1,481 calls — of which 1,473
    # were a single automated client (chain-hire). A description cannot change
    # the behaviour of a caller that already invokes the tool 1,473 times; it
    # can only influence an agent CHOOSING between tools. Breadth measures that,
    # volume measures whoever loops hardest. Two earlier rankings in this same
    # investigation were wrong because they used volume.
    #
    # get_market_dcpi_rank survives the decomposition: its 92 calls come from 10
    # distinct clients with the top one at 37% (grok 24, Anthropic/API 16,
    # mcp 13, claude 12) — genuinely distributed demand.
    #
    # It is also the tool with the least description leverage today: it answers
    # "should I build HERE" for one market with a BUILD/CAUTION/AVOID verdict, a
    # 0-100 composite, time_to_power_months AND a quotable ~100-word analyst
    # narrative — and none of that reaches a tuned platform, because the inline
    # 562-char fallback is what they receive.
    "get_market_dcpi_rank",
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

# ★ TOP_5_PLATFORMS deleted 2026-09-06. It was a back-compat alias for a list
# that has been 12 platforms since July, and it had ZERO references anywhere in
# any of the three repos — a dead constant whose name asserted a false count.
# Found by the guard added for the TUNED_TOOLS rename, which flagged it while I
# was only looking at the tools list. Same defect, same fix.


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
    "search_facilities":      (canon_text("Search {canon_facilities} global data-center facilities by "
                               "city, operator, status, capacity, and more.")),
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

    Returns {tool_name: description} for TUNED_TOOLS, starting from
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
        wanted = set(TUNED_TOOLS)
        found = 0
        for t in tools:
            name = t.get("name")
            desc = (t.get("description") or "").strip()
            if name in wanted and desc:
                out[name] = desc
                found += 1
        logger.info("[tool-tuner] canonical descriptions: %d/%d tools from live "
                    "tools/list (%s)", found, len(TUNED_TOOLS), url)
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


# ── description length ───────────────────────────────────────────────
_DESC_MAX = 280
# Ask the model for less than the hard cap. It routinely overshoots the number
# in the prompt, and every overshoot used to be resolved by a blind slice — so
# the ask and the clamp must not be the same number, or the clamp is the normal
# path rather than the exception.
_DESC_ASK = 240
# Below this fraction of the cap, falling back to the last sentence would throw
# away too much of the description, so we keep more text and end on a word.
_DESC_SENTENCE_FLOOR = 0.6


def _clamp_description(text: str, limit: int = _DESC_MAX) -> str:
    """Bound a description to `limit` WITHOUT cutting mid-word.

    ★ `text[:280]` guillotined 100 of the 132 stored cells mid-sentence —
    "Use for single-market b", "to answer investment feasibility questio".
    These strings are what an agent reads when choosing between tools, so a
    description that stops mid-word is worse than the generic it replaced: it
    reads as a broken tool. The cap is real (agent stores enforce it); what was
    wrong was resolving it with a blind slice.

    Prefer the last complete sentence inside the cap. If that would discard
    more than 1 - _DESC_SENTENCE_FLOOR of the budget, keep the longer text and
    end it on a word boundary instead, trimming any dangling punctuation.
    """
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    head = t[:limit]
    if head.endswith((".", "!", "?")):
        return head
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if cut >= limit * _DESC_SENTENCE_FLOOR:
        return head[:cut + 1]
    sp = head.rfind(" ")
    if sp > 0:
        head = head[:sp]
    return head.rstrip(" ,;:-\u2014\u2013")


def _claude_rewrite(tool_name: str, generic_desc: str, platform: str,
                    voice_cue: str, outcome: str = "") -> str | None:
    """Generate a per-platform-tuned tool description via Claude. Returns
    None on failure (caller falls back to generic)."""
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        return None
    try:
        from utils.anthropic_helper import anthropic_messages_url, http_error_detail
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
        f"  - {_DESC_ASK} characters max (hard limit {_DESC_MAX})\n"
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
                        return _clamp_description(text)
            return None
        except urllib.error.HTTPError as e:
            detail = http_error_detail(e)
            if e.code in (404, 400) and i + 1 < len(models):
                logger.warning("[tool-tuner] %s/%s model=%s HTTP %s %s — trying "
                               "next fallback rung", platform, tool_name, model,
                               e.code, detail)
                continue
            logger.warning("[tool-tuner] %s/%s model=%s HTTP %s %s — no more rungs, "
                           "giving up", platform, tool_name, model, e.code, detail)
            return None
        except Exception as e:
            logger.warning("[tool-tuner] %s/%s model=%s rewrite error: %s",
                           platform, tool_name, model, e)
            return None
    return None


# A rewrite written BY the revert path must not register a claim of its own —
# that would pre-register the restoration, refute it in turn, and revert the
# revert. Any generated_by under this prefix is a restoration, not a bet.
_REVERT_GENERATED_BY_PREFIX = "claim_ledger:"

# The adoption window a tool_copy claim is judged over. Mirrors
# claim_ledger.TOOL_COPY_WINDOW_DAYS; imported lazily there so neither module
# has to be deployed before the other.
_ADOPTION_DEFAULT_DAYS = 14
_ADOPTION_MAX_DAYS = 90

# ── DARK BY DEFAULT ──────────────────────────────────────────────────
# TOOL_COPY_CLAIMS_ENABLED must be exactly "1"; missing or any other value is
# OFF, and the tuner behaves exactly as it did before this module learned to
# register claims. Same convention as ACTION_CLASSES_ENABLED
# (routes/squasher_action_classes) — an operator arms this once, deliberately.
def _claims_enabled() -> bool:
    return os.environ.get("TOOL_COPY_CLAIMS_ENABLED", "").strip() == "1"


# ★ BOUNDED. A forced reseed is up to len(TUNED_TOOLS) x len(TUNED_PLATFORMS)
# = 120 upserts on ONE request, and register_claim opens its OWN connection per
# call (it must — it may not ride the producer's transaction). 120 sequential
# connect/close inside a 40s request is real pool pressure on a pool that has
# saturated at 80 before, so cap the registrations per run and say in the
# response how many were dropped. A silent cap reads as full coverage.
_CLAIM_CAP_PER_RUN = int(os.environ.get("TOOL_COPY_CLAIMS_MAX_PER_RUN", "25") or 25)
_CLAIM_RUN = {"registered": 0, "capped": 0}


def reset_claim_run() -> None:
    """Called at the top of a reseed so the cap is per-run, not per-process."""
    _CLAIM_RUN["registered"] = 0
    _CLAIM_RUN["capped"] = 0


def _adoption_calls(c, platform: str, tool_name: str, days: int):
    """Calls for ONE (platform, tool) over the trailing `days`, canonicalized
    the same way _outcome_signal does it.

    ★ Returns None — never 0 — when the read fails. A tool_copy claim compares
    `>= floor(0.6 x baseline)`, so a broken instrument answering 0 would CONFIRM
    every claim whose baseline rounded to 0. None reads as UNOBSERVED, which the
    verifier defers inside grace and never turns into a verdict."""
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT lower(coalesce(platform,'')) AS p, "
                "       lower(coalesce(client_name,'')) AS cn, "
                "       count(*) AS n "
                "FROM mcp_tool_calls "
                "WHERE created_at > now() - make_interval(days => %s) "
                "  AND tool_name = %s "
                "GROUP BY 1, 2", (int(days), tool_name))
            total = 0
            for p, cn, n in cur.fetchall():
                canon = (PLATFORM_MAP.get(p) or _platform_from_ua(p)
                         or PLATFORM_MAP.get(cn) or _platform_from_ua(cn))
                if canon == platform:
                    total += int(n or 0)
            return total
    except Exception as e:
        logger.warning("_adoption_calls %s/%s failed: %s", platform, tool_name, e)
        try: c.rollback()
        except Exception: pass
        return None


def _prior_description(c, platform: str, tool_name: str):
    """The override about to be overwritten, or None when there is none.
    mcp_tool_descriptions_per_platform is UNIQUE(platform, tool_name) and keeps
    no history, so this read is the only chance to capture what a revert would
    restore."""
    try:
        with c.cursor() as cur:
            cur.execute("SELECT description FROM mcp_tool_descriptions_per_platform"
                        " WHERE platform = %s AND tool_name = %s", (platform, tool_name))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        try: c.rollback()
        except Exception: pass
        return None


def _upsert(c, platform: str, tool_name: str, description: str,
            generated_by: str) -> None:
    is_revert = str(generated_by or "").startswith(_REVERT_GENERATED_BY_PREFIX)
    prior = None if is_revert else _prior_description(c, platform, tool_name)
    baseline = None
    if not is_revert:
        baseline = _adoption_calls(c, platform, tool_name, _ADOPTION_DEFAULT_DAYS)
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO mcp_tool_descriptions_per_platform
                    (platform, tool_name, description, version, generated_by, updated_at)
                VALUES (%s, %s, %s, 1, %s, NOW())
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
        return
    # The row is written — the rewrite has SHIPPED. Pre-register the claim that
    # judges it. Lazily imported and fully fail-soft: the ledger may not be
    # deployed, and a tuner reseed must never die with it.
    if is_revert or baseline is None or not _claims_enabled():
        return
    if _CLAIM_RUN["registered"] >= _CLAIM_CAP_PER_RUN:
        _CLAIM_RUN["capped"] += 1
        return
    try:
        from routes.claim_ledger import register_tool_copy_claim
        register_tool_copy_claim(platform, tool_name, description,
                                 prior_description=prior, baseline_calls=baseline)
        _CLAIM_RUN["registered"] += 1
    except Exception as e:  # noqa: BLE001
        logger.warning("tool_copy claim registration skipped for %s/%s: %s",
                       platform, tool_name, e)


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
    reset_claim_run()
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
        for tool_name in TUNED_TOOLS:
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
                deterministic = _clamp_description(f"[{canon}] {generic}")
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
        # A cap that does not say what it dropped reads as full coverage.
        claims_registered=_CLAIM_RUN["registered"],
        claims_capped=_CLAIM_RUN["capped"],
        claims_enabled=_claims_enabled(),
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
    if len(desc) > _DESC_MAX:
        return jsonify(ok=False, error="description_too_long",
                       max=_DESC_MAX), 400
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
            for tool in TUNED_TOOLS:
                row[tool] = (canon in all_tuned and tool in all_tuned[canon])
            matrix[canon] = row
        total_cells = len(TUNED_PLATFORMS) * len(TUNED_TOOLS)
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
                       matrix=matrix, freshness=freshness, top_tools=TUNED_TOOLS,
                       top_platforms=[p for p, _, _ in TUNED_PLATFORMS])
    finally:
        _put_db(c)


@ai_platform_tool_tuner_bp.route("/api/v1/admin/mcp/tool-tuner/adoption", methods=["GET"])
def adoption():
    """The instrument a `tool_copy` claim is judged by: calls for ONE
    (platform, tool) over the trailing `days`.

    Addressable by design. The claim ledger's `get:` scheme walks a dotted path
    into the response (claim_ledger.dig), and the tuner's other reads return a
    LIST of tools — addressable only by position, which reorders. This returns
    the one cell a claim named.

    ★ `calls` is null, never 0, when the read fails. `>= floor(0.6 x baseline)`
    would be SATISFIED by a zero from a broken instrument on any small cell;
    null resolves as UNOBSERVED and the verifier defers it instead of stamping
    a verdict it did not earn."""
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    platform = (request.args.get("platform") or "").strip().lower()
    tool = (request.args.get("tool") or "").strip()
    if not platform or not tool:
        return jsonify(ok=False, error="platform and tool required"), 400
    try:
        days = int(request.args.get("days", _ADOPTION_DEFAULT_DAYS))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="days must be an integer"), 400
    if not (1 <= days <= _ADOPTION_MAX_DAYS):
        return jsonify(ok=False,
                       error=f"days must be 1..{_ADOPTION_MAX_DAYS}"), 400
    c = _get_db()
    if c is None:
        return jsonify(ok=False, platform=platform, tool=tool, days=days,
                       calls=None, error="no database"), 200
    try:
        n = _adoption_calls(c, platform, tool, days)
    finally:
        _put_db(c)
    return jsonify(
        ok=n is not None, platform=platform, tool=tool, days=days, calls=n,
        instrument=("mcp_tool_calls grouped by (platform, tool_name), "
                    "canonicalized through PLATFORM_MAP — the same read "
                    "_outcome_signal tunes on"),
        reading=("calls=null means the instrument did not measure. It is NOT "
                 "zero, and a claim resolving against it reads unobserved."),
    ), 200


# ── Smoke ────────────────────────────────────────────────────────────
def _smoke():
    logger.info("[ai_platform_tool_tuner] loaded · disabled=%s · %d cells "
                "(%d tools × %d platforms)", _disabled(),
                len(TUNED_TOOLS) * len(TUNED_PLATFORMS),
                len(TUNED_TOOLS), len(TUNED_PLATFORMS))


_smoke()
