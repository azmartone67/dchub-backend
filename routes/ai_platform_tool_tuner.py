"""routes/ai_platform_tool_tuner.py — Unlock 2: Per-platform MCP tool descriptions (2026-06-07).

Today's MCP tool descriptions are general-purpose. Different agents have
different conventions: Claude's tool-use cycle expects Anthropic-style
descriptions, Cursor reads them inside an IDE workflow, Cline expects
context-window-aware brevity, ChatGPT's plugin path leans on action
verbs, Perplexity surfaces them as citation-source titles.

This module:
  1. Stores per-(platform, tool_name) description overrides in
     `mcp_tool_descriptions_per_platform` (schema in schema_repair.py).
  2. Exposes a Claude-driven generator that produces 50 initial variants
     (top-10 tools × top-5 platforms).
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
  * Seed is bounded: at most 50 Claude calls per /seed invocation
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


# ── Config: top-10 tools + top-5 platforms ───────────────────────────
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
TOP_5_PLATFORMS = [
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
]


PLATFORM_MAP = {alias: canon for canon, aliases, _ in TOP_5_PLATFORMS for alias in aliases}


def _platform_from_ua(ua: str) -> str | None:
    """Lower-case substring match against TOP_5_PLATFORMS."""
    if not ua:
        return None
    low = ua.lower()
    for canon, aliases, _ in TOP_5_PLATFORMS:
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
    "search_facilities":      ("Search 50,000+ global data-center facilities by "
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


# ── Claude-driven generator ──────────────────────────────────────────
def _claude_rewrite(tool_name: str, generic_desc: str, platform: str,
                    voice_cue: str) -> str | None:
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
    try:
        from routes.brain_models import resolve_chain
        models = resolve_chain(os.environ.get("DCHUB_BRAIN_MODEL_ROUTINE") or
                               os.environ.get("DCHUB_BRAIN_MODEL") or
                               "claude-sonnet-4-20250514")
    except Exception:
        models = ["claude-sonnet-4-20250514"]
    prompt = (
        f"Rewrite this MCP tool description tuned for {platform.upper()}.\n\n"
        f"TOOL NAME: {tool_name}\n"
        f"GENERIC DESCRIPTION: {generic_desc}\n\n"
        f"PLATFORM VOICE: {voice_cue}\n\n"
        "Constraints:\n"
        "  - 1-2 sentences\n"
        "  - 280 characters max\n"
        "  - Plain text, no markdown, no code fences\n"
        "  - Preserve all data the generic claims (counts, scope)\n"
        "  - Lead with action (verb-first when natural)\n"
        "  - Do NOT include the tool name in the description\n\n"
        "Output ONLY the rewritten description, nothing else."
    )
    for i, model in enumerate(models[:3]):
        try:
            body = json.dumps({
                "model": model,
                "max_tokens": 300,
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
                continue
            return None
        except Exception:
            return None
    return None


def _upsert(c, platform: str, tool_name: str, description: str,
            generated_by: str) -> None:
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
            cur.execute("SELECT platform, tool_name, description, version "
                        "FROM mcp_tool_descriptions_per_platform")
            for plat, tool, desc, ver in cur.fetchall():
                out.setdefault(plat, {})[tool] = {"description": desc, "version": ver}
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


@ai_platform_tool_tuner_bp.route("/api/v1/admin/mcp/tool-tuner/seed",
                                 methods=["POST"])
def seed_variants():
    """Generate the 50 initial per-platform variants (top-10 tools × top-5
    platforms). Bounded; idempotent; skips entries that already exist
    unless ?force=1 is set."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if _disabled():
        return jsonify(ok=True, skipped=True,
                       reason="AI_AGENT_EXPANSION_DISABLE=1"), 200
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
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

    for tool_name in TOP_10_TOOLS:
        generic = GENERIC_DESCRIPTIONS.get(tool_name, tool_name)
        for canon, _aliases, voice in TOP_5_PLATFORMS:
            key = (canon, tool_name)
            if key in existing and not force:
                skipped.append({"platform": canon, "tool": tool_name,
                                "reason": "already_exists"})
                continue
            if not api_key_present:
                # Without Claude, write a deterministic light variant so
                # the table is at least populated. The seed task should
                # be re-run once ANTHROPIC_API_KEY is set.
                deterministic = f"[{canon}] {generic}"[:280]
                _upsert(c, canon, tool_name, deterministic, "deterministic_no_claude")
                written.append({"platform": canon, "tool": tool_name,
                                "via": "deterministic"})
                continue
            tuned = _claude_rewrite(tool_name, generic, canon, voice)
            if tuned:
                _upsert(c, canon, tool_name, tuned, "claude")
                written.append({"platform": canon, "tool": tool_name,
                                "via": "claude"})
            else:
                failed.append({"platform": canon, "tool": tool_name})
            # Tiny pause to stay polite to upstream
            time.sleep(0.1)

    # Bust cache so the next read endpoint call sees fresh data.
    _TUNED_CACHE["data"] = {}
    _TUNED_CACHE["at"] = 0.0

    return jsonify(ok=True,
                   written=len(written),
                   skipped=len(skipped),
                   failed=len(failed),
                   elapsed_s=round(time.time() - started, 2),
                   anthropic_key_present=api_key_present,
                   detail={"written": written[:30],
                           "skipped": skipped[:30],
                           "failed": failed[:30]})


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
    _ensure_table(c)
    _upsert(c, plat, tool, desc, body.get("generated_by") or "manual")
    _TUNED_CACHE["data"] = {}
    _TUNED_CACHE["at"] = 0.0
    return jsonify(ok=True, platform=plat, tool_name=tool, version_bumped=True)


@ai_platform_tool_tuner_bp.route("/api/v1/admin/mcp/tool-tuner/coverage",
                                 methods=["GET"])
def coverage():
    """Matrix of which (platform, tool) cells have an override."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None:
        return jsonify(ok=False, error="no_db"), 503
    _ensure_table(c)
    all_tuned = _load_all_tuned(c)
    matrix = {}
    for canon, _, _ in TOP_5_PLATFORMS:
        row = {}
        for tool in TOP_10_TOOLS:
            row[tool] = (canon in all_tuned and tool in all_tuned[canon])
        matrix[canon] = row
    total_cells = len(TOP_5_PLATFORMS) * len(TOP_10_TOOLS)
    filled = sum(1 for canon in matrix for t in matrix[canon] if matrix[canon][t])
    return jsonify(ok=True, total_cells=total_cells, filled=filled,
                   percent=round(100 * filled / max(total_cells, 1), 1),
                   matrix=matrix, top_tools=TOP_10_TOOLS,
                   top_platforms=[c for c, _, _ in TOP_5_PLATFORMS])


# ── Smoke ────────────────────────────────────────────────────────────
def _smoke():
    logger.info("[ai_platform_tool_tuner] loaded · disabled=%s · 50 cells "
                "(%d tools × %d platforms)", _disabled(),
                len(TOP_10_TOOLS), len(TOP_5_PLATFORMS))


_smoke()
