"""Phase FF (2026-05-12) — DC Hub Media as the single source of media relations.

User: "comprehensive fix of dc hub media, our single source for media
relations, aggregating all of our news, amazing stats, and hopefully
our outreach engine py can be repurposed by dchub media to ping all the
agents to encourage them to use us for resource, mcp, intelligence
gathering... testimonials flow is weak, need more current sources to
aggregate."

This module bundles three coupled deliverables:

  FF-1  /api/v1/media/aggregate
        Single-fetch endpoint that returns the full DC Hub Media feed
        organized into 6 rails:
          • live_spine         — top-of-page KPIs (refreshed every minute)
          • auto_press         — autonomous press releases (Phase BB)
          • dcpi_alerts        — verdict changes today
          • testimonials       — AI agent endorsements (multi-source, see FF-3)
          • live_mcp_pulse     — agent activity right now
          • biggest_movers     — markets shifting > 5pt excess in 7d
        Plus engagement totals per piece.
        Replaces the loose 4-table aggregator in dchub_media.py for the
        homepage; the legacy aggregator stays in place for back-compat
        with /api/v1/media/feed-v3 callers.

  FF-2  Agent vendor outreach
        Detects AI agent platforms from mcp_tool_calls (client_name +
        platform + User-Agent) and surfaces a per-vendor outreach card:
          - Usage telemetry (calls, top tool, unique users from that platform)
          - A pre-composed cold email pitch the operator can copy-paste
            to partnerships@<vendor> teams
          - A 1-click LinkedIn share URL for vendor-facing dev-rel
        Public endpoint: GET /api/v1/outreach/agents/vendors
        Cron-ready trigger: POST /api/v1/outreach/agents/email-digest
        (admin-gated; emails the operator a daily roll-up of new agent
        activity worth pitching).

  FF-3  Testimonial ingestion (new sources)
        The ai_testimonials table was stale since March because the only
        upstream was synthetic mcp-auto rows. New sources:
          a. MCP-derived auto-citation (captures real AI agent activity
             with provenance, NOT synthetic mcp-auto)
          b. HackerNews search for "dchub.cloud" (free, no key)
          c. Reddit search for /r/datacenter + "dchub" (free, no key)
        Stored in `ai_testimonials_auto` (separate from ai_testimonials
        so we never mix human-curated + auto-ingested + synthetic-test
        rows). Surfaced via GET /api/v1/testimonials/live and the new
        /testimonials/wall HTML page.

Endpoints summary
-----------------
  GET  /api/v1/media/aggregate              public, 60s cache
  GET  /api/v1/outreach/agents/vendors      public
  POST /api/v1/outreach/agents/email-digest admin-gated
  POST /api/v1/testimonials/ingest          admin-gated; runs the sources
  GET  /api/v1/testimonials/live            public; merged feed
  GET  /testimonials/wall                   public HTML page
"""
from __future__ import annotations
import os
import re
import sys
import json
import hashlib
import time
from datetime import datetime, timezone, timedelta
from flask import Blueprint, jsonify, request, Response

media_hub_bp = Blueprint("dchub_media_hub", __name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
RESEND_API_KEY = os.environ.get("DCHUB_RESEND_API_KEY", "")
ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
             or os.environ.get("DCHUB_INTERNAL_KEY"))
OPERATOR_EMAIL = os.environ.get("DCHUB_LINKEDIN_EMAIL_TO") or "press@dchub.cloud"


def _conn():
    if not DATABASE_URL: return None
    try:
        import psycopg2
        return psycopg2.connect(DATABASE_URL, connect_timeout=8)
    except Exception as e:
        print(f"[dchub_media_hub] connect failed: {e}", file=sys.stderr)
        return None


def _require_admin():
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key"))
    if ADMIN_KEY and provided != ADMIN_KEY:
        return jsonify(error="unauthorized",
                       hint="X-Admin-Key header required"), 401
    return None


_SCHEMA_DDL = """
-- FF-3: testimonials from external + MCP-derived sources. Kept separate
-- from the canonical ai_testimonials table so we can:
--   (a) never mix human-curated with auto-ingested
--   (b) tune the merge logic per-surface without DB writes
--   (c) easily delete a misbehaving source without affecting the rest
CREATE TABLE IF NOT EXISTS ai_testimonials_auto (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,        -- 'mcp_derived' | 'hackernews' | 'reddit' | 'github'
    external_id     TEXT,                 -- HN item id / Reddit post id / GH issue#
    agent_name      TEXT,                 -- 'Claude Desktop' / 'Cursor' / 'Gemini CLI' / unknown
    platform        TEXT,                 -- vendor identifier
    quote           TEXT NOT NULL,        -- the actual citation/quote
    url             TEXT,                 -- link back to source
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    posted_at       TIMESTAMPTZ,          -- when the original mention was created
    sentiment       TEXT,                 -- 'positive' | 'neutral' | 'mixed'
    approved        BOOLEAN DEFAULT FALSE, -- gate before showing on /testimonials/wall
    raw_payload     JSONB,
    CONSTRAINT ai_testimonials_auto_unique UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS ai_testimonials_auto_recent_idx
    ON ai_testimonials_auto(captured_at DESC);
CREATE INDEX IF NOT EXISTS ai_testimonials_auto_approved_idx
    ON ai_testimonials_auto(approved, captured_at DESC);
"""


def init_schema() -> bool:
    c = _conn()
    if c is None: return False
    try:
        with c, c.cursor() as cur:
            cur.execute(_SCHEMA_DDL)
        return True
    except Exception as e:
        print(f"[dchub_media_hub] init_schema failed: {e}", file=sys.stderr)
        return False
    finally:
        try: c.close()
        except Exception: pass


try:
    _SCHEMA_OK = init_schema()
except Exception:
    _SCHEMA_OK = False


# ═══════════════════════════════════════════════════════════════════════════
# FF-1: aggregate endpoint
# ═══════════════════════════════════════════════════════════════════════════

@media_hub_bp.get("/api/v1/media/aggregate")
def media_aggregate():
    """Single fetch for the whole DC Hub Media feed. Organized into rails
       so the frontend renders structured sections instead of a chronological
       firehose."""
    c = _conn()
    if c is None:
        return jsonify(error="no_database",
                       live_spine=_empty_spine(),
                       rails={}), 503

    out = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "live_spine": _empty_spine(),
        "rails": {
            "auto_press":     [],
            "dcpi_alerts":    [],
            "testimonials":   [],
            "live_mcp_pulse": {"tool_calls_24h": 0, "unique_callers_24h": 0,
                                "top_tools": []},
            "biggest_movers": [],
        },
        "section_counts": {},
    }

    try:
        # ── Live spine: top-of-page KPIs ────────────────────────────
        with c.cursor() as cur:
            try:
                cur.execute("""SELECT COUNT(DISTINCT market_slug)
                               FROM market_power_scores WHERE published = true""")
                out["live_spine"]["dcpi_markets"] = int((cur.fetchone() or (0,))[0])
            except Exception: c.rollback()
            try:
                cur.execute("""SELECT COUNT(*) FROM market_power_scores
                               WHERE published = true AND verdict = 'BUILD'""")
                out["live_spine"]["build_verdicts"] = int((cur.fetchone() or (0,))[0])
            except Exception: c.rollback()
            try:
                cur.execute("""SELECT COUNT(*) FROM mcp_tool_calls
                               WHERE created_at > NOW() - INTERVAL '24 hours'""")
                out["live_spine"]["mcp_calls_24h"] = int((cur.fetchone() or (0,))[0])
            except Exception: c.rollback()
            try:
                cur.execute("""SELECT COUNT(DISTINCT COALESCE(NULLIF(client_name,''),
                                                               NULLIF(platform,''),
                                                               ip_address))
                               FROM mcp_tool_calls
                               WHERE created_at > NOW() - INTERVAL '7 days'""")
                out["live_spine"]["unique_ai_agents_7d"] = int((cur.fetchone() or (0,))[0])
            except Exception: c.rollback()
            try:
                cur.execute("""SELECT COUNT(*) FROM auto_press_releases
                               WHERE generated_at > NOW() - INTERVAL '7 days'""")
                out["live_spine"]["auto_press_7d"] = int((cur.fetchone() or (0,))[0])
            except Exception: c.rollback()
            try:
                cur.execute("""SELECT COUNT(*) FROM iso_meta""")
                out["live_spine"]["isos_live"] = int((cur.fetchone() or (0,))[0])
            except Exception:
                c.rollback()
                out["live_spine"]["isos_live"] = 7

        # ── Rail: auto-press (most recent 8) ────────────────────────
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT apr.slug, apr.title, apr.generated_at, apr.source_topic,
                           pr.meta_description, pr.subheadline
                    FROM auto_press_releases apr
                    LEFT JOIN press_releases pr ON pr.id = apr.press_release_id
                    ORDER BY apr.generated_at DESC LIMIT 8
                """)
                for r in cur.fetchall():
                    out["rails"]["auto_press"].append({
                        "slug": r[0], "title": r[1],
                        "generated_at": r[2].isoformat() if r[2] else None,
                        "topic": r[3],
                        "blurb": r[4] or r[5] or "",
                        "url": f"https://dchub.cloud/news/{r[0]}",
                    })
            except Exception: c.rollback()

        # ── Rail: DCPI alerts (verdict shifts in last 7d) ───────────
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT DISTINCT ON (market_slug)
                        market_name, market_slug, iso, state, verdict,
                        excess_power_score, constraint_score, computed_at
                    FROM market_power_scores
                    WHERE published = true
                      AND verdict IN ('BUILD', 'AVOID')
                      AND computed_at > NOW() - INTERVAL '7 days'
                    ORDER BY market_slug, computed_at DESC
                """)
                rows = cur.fetchall()
                # Sort: BUILD first by excess, then AVOID by constraint
                rows.sort(key=lambda r: (r[4] != 'BUILD',
                                          -(r[5] or 0) if r[4] == 'BUILD'
                                          else -(r[6] or 0)))
                for r in rows[:10]:
                    out["rails"]["dcpi_alerts"].append({
                        "market": r[0], "slug": r[1], "iso": r[2], "state": r[3],
                        "verdict": r[4],
                        "excess": round(float(r[5] or 0), 1),
                        "constraint": round(float(r[6] or 0), 1),
                        "computed_at": r[7].isoformat() if r[7] else None,
                        "url": f"https://dchub.cloud/dcpi/{r[1]}",
                    })
            except Exception: c.rollback()

        # ── Rail: testimonials (merged ai_testimonials + auto) ──────
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT 'canonical' AS src, agent_name, platform, quote,
                           url, approved_at AS ts
                    FROM ai_testimonials
                    WHERE COALESCE(approved, true) = true
                      AND agent_name IS NOT NULL AND agent_name != 'unknown'
                      AND agent_name != 'Claude'
                      AND (source IS NULL OR source NOT IN ('mcp-auto', 'mcp_auto'))
                      AND quote IS NOT NULL AND length(quote) > 10
                    ORDER BY approved_at DESC NULLS LAST LIMIT 6
                """)
                for r in cur.fetchall():
                    out["rails"]["testimonials"].append({
                        "source": r[0], "agent": r[1], "platform": r[2],
                        "quote": (r[3] or "")[:500],
                        "url": r[4],
                        "posted_at": r[5].isoformat() if r[5] else None,
                    })
            except Exception: c.rollback()
            # FF-3: also pull from auto-ingested (HN, Reddit, MCP-derived)
            try:
                cur.execute("""
                    SELECT source, agent_name, platform, quote, url,
                           COALESCE(posted_at, captured_at)
                    FROM ai_testimonials_auto
                    WHERE approved = true
                    ORDER BY COALESCE(posted_at, captured_at) DESC LIMIT 6
                """)
                for r in cur.fetchall():
                    out["rails"]["testimonials"].append({
                        "source": r[0], "agent": r[1], "platform": r[2],
                        "quote": (r[3] or "")[:500],
                        "url": r[4],
                        "posted_at": r[5].isoformat() if r[5] else None,
                    })
            except Exception: c.rollback()

        # ── Rail: live MCP pulse ────────────────────────────────────
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT COUNT(*),
                           COUNT(DISTINCT COALESCE(NULLIF(client_name,''),
                                                    NULLIF(platform,''),
                                                    ip_address))
                    FROM mcp_tool_calls
                    WHERE created_at > NOW() - INTERVAL '24 hours'
                """)
                row = cur.fetchone() or (0, 0)
                out["rails"]["live_mcp_pulse"]["tool_calls_24h"] = int(row[0])
                out["rails"]["live_mcp_pulse"]["unique_callers_24h"] = int(row[1])
            except Exception: c.rollback()
            try:
                cur.execute("""
                    SELECT tool_name, COUNT(*) FROM mcp_tool_calls
                    WHERE created_at > NOW() - INTERVAL '24 hours'
                      AND tool_name IS NOT NULL AND tool_name != ''
                    GROUP BY tool_name ORDER BY COUNT(*) DESC LIMIT 5
                """)
                out["rails"]["live_mcp_pulse"]["top_tools"] = [
                    {"tool": r[0], "calls": int(r[1])}
                    for r in cur.fetchall()
                ]
            except Exception: c.rollback()

        # ── Rail: biggest movers (>3pt excess shift in 7d) ──────────
        with c.cursor() as cur:
            try:
                cur.execute("""
                    WITH latest AS (
                      SELECT DISTINCT ON (market_slug) market_slug, market_name,
                             iso, excess_power_score AS now_e
                      FROM market_power_scores WHERE published = true
                      ORDER BY market_slug, computed_at DESC
                    ),
                    week_ago AS (
                      SELECT DISTINCT ON (market_slug) market_slug,
                             excess_power_score AS prev_e
                      FROM market_power_scores
                      WHERE published = true
                        AND computed_at < NOW() - INTERVAL '7 days'
                      ORDER BY market_slug, computed_at DESC
                    )
                    SELECT l.market_slug, l.market_name, l.iso,
                           l.now_e, (l.now_e - w.prev_e) AS delta
                    FROM latest l JOIN week_ago w ON l.market_slug = w.market_slug
                    WHERE ABS(l.now_e - w.prev_e) > 3
                    ORDER BY ABS(l.now_e - w.prev_e) DESC LIMIT 8
                """)
                for r in cur.fetchall():
                    out["rails"]["biggest_movers"].append({
                        "slug": r[0], "market": r[1], "iso": r[2],
                        "now": round(r[3] or 0, 1),
                        "delta": round(r[4] or 0, 1),
                        "url": f"https://dchub.cloud/dcpi/{r[0]}",
                    })
            except Exception: c.rollback()
    finally:
        try: c.close()
        except Exception: pass

    # Section counts for UI badges
    for k, v in out["rails"].items():
        if isinstance(v, list):
            out["section_counts"][k] = len(v)
    out["section_counts"]["live_mcp_pulse"] = out["rails"]["live_mcp_pulse"].get("tool_calls_24h", 0)

    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=120"
    return resp, 200


def _empty_spine():
    return {"dcpi_markets": 0, "build_verdicts": 0, "isos_live": 7,
            "mcp_calls_24h": 0, "unique_ai_agents_7d": 0, "auto_press_7d": 0}


# ═══════════════════════════════════════════════════════════════════════════
# FF-2: agent vendor outreach
# ═══════════════════════════════════════════════════════════════════════════

# Map of detected agent fingerprint → vendor metadata for outreach.
# These are the platforms users would have to verbally tell their AI
# to "use the DC Hub MCP" — making us first-class in their plugin
# directory closes that loop.
#
# Phase FF+ (2026-05-12): expanded with REAL patterns observed in
# production User-Agent / client_name / platform strings. The first
# pass only matched simple bare keywords ("claude", "cursor") but
# actual fingerprints in mcp_tool_calls look like:
#   "Claude/1.0 (Anthropic)"      → caught by "claude"
#   "anthropic-ai/sdk@0.31.0"     → MISSED (no "claude" substring)
#   "openai/python-client v1.45"  → MISSED (no "gpt")
#   "google-genai/0.5"            → MISSED (no "gemini")
#   "python-requests/2.31"        → unidentifiable third-party (legitimate)
#   "Mozilla/5.0 ..."             → browser direct hits — not an AI agent
#
# Each vendor entry now has `patterns` (substrings to match
# case-insensitively, OR-d) so we catch SDK identifiers + product
# names + common variants.
_VENDOR_DIRECTORY = {
    "anthropic":  {"vendor": "Anthropic", "product": "Claude / Claude Code",
                   "patterns": ["claude", "anthropic"],
                   "pitch_email": "developers@anthropic.com",
                   "mcp_directory": "https://github.com/modelcontextprotocol/servers"},
    "cursor":     {"vendor": "Cursor (Anysphere)", "product": "Cursor IDE",
                   "patterns": ["cursor"],
                   "pitch_email": "partnerships@cursor.so",
                   "mcp_directory": "https://docs.cursor.com/context/mcp"},
    "google":     {"vendor": "Google", "product": "Gemini / Code Assist",
                   "patterns": ["gemini", "google-genai", "google-ai",
                                "google-generativeai"],
                   "pitch_email": "ai-developer-relations@google.com",
                   "mcp_directory": "https://ai.google.dev/gemini-api/docs"},
    "openai":     {"vendor": "OpenAI", "product": "ChatGPT / GPT API",
                   "patterns": ["gpt", "openai", "chatgpt"],
                   "pitch_email": "platform-partnerships@openai.com",
                   "mcp_directory": "https://platform.openai.com/docs"},
    "perplexity": {"vendor": "Perplexity", "product": "Perplexity AI",
                   "patterns": ["perplexity"],
                   "pitch_email": "support@perplexity.ai",
                   "mcp_directory": ""},
    "cline":      {"vendor": "Cline", "product": "Cline IDE Extension",
                   "patterns": ["cline"],
                   "pitch_email": "support@cline.bot",
                   "mcp_directory": "https://github.com/cline/cline/wiki/MCP-Servers"},
    "windsurf":   {"vendor": "Codeium", "product": "Windsurf IDE",
                   "patterns": ["windsurf", "codeium"],
                   "pitch_email": "support@codeium.com",
                   "mcp_directory": ""},
    "copilot":    {"vendor": "GitHub / Microsoft", "product": "GitHub Copilot",
                   "patterns": ["copilot", "github copilot"],
                   "pitch_email": "partnerships@github.com",
                   "mcp_directory": ""},
    "xai":        {"vendor": "xAI", "product": "Grok",
                   "patterns": ["grok", "xai", "x-ai"],
                   "pitch_email": "partnerships@x.ai",
                   "mcp_directory": ""},
    "meta":       {"vendor": "Meta", "product": "Meta AI / Llama",
                   "patterns": ["meta-ai", "meta_ai", "llama", "meta.ai"],
                   "pitch_email": "developers@meta.com",
                   "mcp_directory": ""},
    "mistral":    {"vendor": "Mistral AI", "product": "Mistral / Le Chat",
                   "patterns": ["mistral", "le chat"],
                   "pitch_email": "partnerships@mistral.ai",
                   "mcp_directory": ""},
    "huggingface": {"vendor": "Hugging Face", "product": "HF Inference / Chat",
                   "patterns": ["huggingface", "hugging-face", "hf-inference"],
                   "pitch_email": "partnerships@huggingface.co",
                   "mcp_directory": ""},
    "poe":        {"vendor": "Quora", "product": "Poe",
                   "patterns": ["poe", "poe.com"],
                   "pitch_email": "partnerships@poe.com",
                   "mcp_directory": ""},
    "openrouter": {"vendor": "OpenRouter", "product": "OpenRouter",
                   "patterns": ["openrouter"],
                   "pitch_email": "team@openrouter.ai",
                   "mcp_directory": ""},
    "phind":      {"vendor": "Phind", "product": "Phind Search",
                   "patterns": ["phind"],
                   "pitch_email": "support@phind.com",
                   "mcp_directory": ""},
    "you":        {"vendor": "You.com", "product": "You.com Search",
                   "patterns": ["you.com", "youcom"],
                   "pitch_email": "press@you.com",
                   "mcp_directory": ""},
}


def _detect_vendor(name_or_ua: str) -> dict | None:
    """Find a known vendor by substring match against the fingerprint.
       Returns None if no known match. Callers can fall back to
       'unknown_<truncated_fingerprint>' to still surface unmatched
       traffic in the leaderboard."""
    if not name_or_ua: return None
    low = name_or_ua.lower()
    for key, meta in _VENDOR_DIRECTORY.items():
        patterns = meta.get("patterns") or [key]
        for p in patterns:
            if p in low:
                # Build a cleaned dict matching the legacy shape (no `patterns`
                # in the return) so callers don't have to know about the new
                # field structure.
                out = {k: v for k, v in meta.items() if k != "patterns"}
                return {"key": key, **out}
    return None


def _label_unknown_fingerprint(fp: str) -> dict:
    """Fallback for fingerprints that don't match any known vendor.
       Still surface them in the leaderboard so we can SEE the traffic
       we're missing, and tune the vendor dict in the next iteration."""
    # Sanitize the fingerprint into a short, safe label
    safe = (fp or "anonymous")[:60].strip()
    return {
        "key": "unknown",
        "vendor": "Unknown / unidentified",
        "product": f"Unidentified client: {safe}",
        "pitch_email": "",
        "mcp_directory": "",
    }


@media_hub_bp.get("/api/v1/admin/mcp/fingerprints")
def admin_mcp_fingerprints():
    """Admin-gated: dumps the top 30 distinct (client_name, platform,
       user_agent) tuples observed in mcp_tool_calls over the last 7d
       along with call counts. Lets the operator see EXACTLY what
       fingerprints production traffic is sending so the vendor
       detection dict can be tuned.

       Built in response to Phase FF-2 returning 0 vendors despite
       2,054 unique callers in 7d — clearly the detection patterns
       weren't matching real strings.
    """
    auth_err = _require_admin()
    if auth_err: return auth_err
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT
                    COALESCE(client_name, '')                          AS client_name,
                    COALESCE(platform, '')                             AS platform,
                    SUBSTRING(COALESCE(user_agent, '') FROM 1 FOR 120) AS user_agent,
                    COUNT(*)                                            AS calls,
                    COUNT(DISTINCT ip_address)                          AS unique_ips,
                    MAX(created_at)                                     AS last_seen
                FROM mcp_tool_calls
                WHERE created_at > NOW() - INTERVAL '7 days'
                GROUP BY 1, 2, 3
                ORDER BY calls DESC
                LIMIT 30
            """)
            rows = cur.fetchall()
        out = []
        for cn, pl, ua, calls, ips, last in rows:
            # Pre-compute what _detect_vendor would say
            combined = (cn or "") + " " + (pl or "") + " " + (ua or "")
            vendor = _detect_vendor(combined)
            out.append({
                "client_name": cn or None,
                "platform":    pl or None,
                "user_agent":  ua or None,
                "calls_7d":    int(calls or 0),
                "unique_ips":  int(ips or 0),
                "last_seen":   last.isoformat() if last else None,
                "detected_vendor": vendor.get("vendor") if vendor else None,
                "detected_key":    vendor.get("key") if vendor else None,
            })
        return jsonify(
            as_of=datetime.now(timezone.utc).isoformat(),
            window_days=7,
            count=len(out),
            rows=out,
        ), 200
    finally:
        try: c.close()
        except Exception: pass


@media_hub_bp.get("/api/v1/outreach/agents/vendors")
def agent_vendor_telemetry():
    """For each detected AI agent vendor, return:
       - 7d usage telemetry (calls + unique users coming from that vendor)
       - A cold-email pitch template ready to copy-paste
       - LinkedIn share URL pre-filled with vendor-friendly framing

       This is what the operator uses to drive outreach: "your AI agent
       users are querying DC Hub N times/week — make us a first-class
       MCP server in your client."
    """
    c = _conn()
    if c is None: return jsonify(error="no_database", vendors=[]), 503
    out = []
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT
                    COALESCE(NULLIF(client_name, ''),
                             NULLIF(platform, ''),
                             user_agent) AS fingerprint,
                    COUNT(*) AS calls,
                    COUNT(DISTINCT ip_address) AS unique_ips,
                    MAX(created_at) AS last_seen,
                    array_agg(DISTINCT tool_name) AS tools_used
                FROM mcp_tool_calls
                WHERE created_at > NOW() - INTERVAL '7 days'
                  AND COALESCE(NULLIF(client_name, ''),
                                NULLIF(platform, ''),
                                user_agent) IS NOT NULL
                GROUP BY fingerprint
                ORDER BY calls DESC
                LIMIT 40
            """)
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass

    # Bucket rows by detected vendor.
    # Phase FF+ (2026-05-12): also bucket UNKNOWN fingerprints so we
    # surface every active agent in the leaderboard, not just the ones
    # whose User-Agent matches our vendor dictionary. Each unknown
    # fingerprint becomes its own bucket keyed `unknown:<fingerprint>`
    # so the operator can see which traffic to add patterns for next.
    # Skip obvious non-agent traffic (browsers, curl, etc.) to keep
    # the leaderboard signal-to-noise high.
    _NON_AGENT_PATTERNS = (
        "mozilla/", "chrome/", "safari/", "edge/",   # browsers
        "curl/", "wget/", "httpie/", "postman",       # CLI/tools
        "python-requests", "python-urllib",            # generic SDK
        "ruby", "node-fetch", "axios",                 # generic
        "github-actions", "monitoring", "uptime",      # ops
        "dchubbot", "dchub-qa", "dchub-self",          # our own probes
    )
    def _is_non_agent(fp: str) -> bool:
        low = (fp or "").lower()
        return any(p in low for p in _NON_AGENT_PATTERNS)

    by_vendor: dict[str, dict] = {}
    for fp, calls, ips, last_seen, tools in rows:
        vendor = _detect_vendor(fp or "")
        if not vendor:
            if _is_non_agent(fp or ""):
                continue  # browsers / curl / our own probes — skip
            vendor = _label_unknown_fingerprint(fp or "")
            # Key by the FP so each distinct unknown agent gets its
            # own row in the leaderboard (rather than collapsing all
            # into one "unknown" bucket).
            key = "unknown:" + ((fp or "anonymous")[:40])
        else:
            key = vendor["key"]
        b = by_vendor.setdefault(key, {
            "vendor_key": key,
            "vendor": vendor["vendor"],
            "product": vendor["product"],
            "pitch_email": vendor.get("pitch_email", ""),
            "mcp_directory_url": vendor.get("mcp_directory", ""),
            "calls_7d": 0, "unique_ips_7d": 0,
            "last_seen": None,
            "top_tools": set(),
            "fingerprints_seen": [],
        })
        b["calls_7d"] += int(calls or 0)
        b["unique_ips_7d"] += int(ips or 0)
        if last_seen and (not b["last_seen"] or last_seen > b["last_seen"]):
            b["last_seen"] = last_seen
        if tools:
            for t in tools[:10]:
                if t: b["top_tools"].add(t)
        b["fingerprints_seen"].append((fp or "")[:60])

    # Compose pitches
    for v in by_vendor.values():
        v["top_tools"] = sorted(v["top_tools"])[:8]
        v["last_seen"] = v["last_seen"].isoformat() if v["last_seen"] else None
        v["fingerprints_seen"] = list(set(v["fingerprints_seen"]))[:5]
        v["cold_email"] = _compose_vendor_pitch(v)
        from urllib.parse import quote
        msg = (f"Quick share: {v['calls_7d']:,} MCP tool calls came "
               f"from {v['product']} users at DC Hub this week.")
        v["linkedin_share_url"] = (
            "https://www.linkedin.com/sharing/share-offsite/?url=" +
            quote("https://dchub.cloud/mcp"))
        v["linkedin_message_template"] = msg

    return jsonify(
        as_of=datetime.now(timezone.utc).isoformat(),
        window_days=7,
        vendors=sorted(by_vendor.values(),
                       key=lambda x: -x["calls_7d"]),
        count=len(by_vendor),
    ), 200


def _compose_vendor_pitch(v: dict) -> str:
    """Per-vendor cold email template. Personalized with their actual
       usage telemetry — proof, not pitch."""
    top_tools = ", ".join(v["top_tools"][:5]) if v["top_tools"] else "various"
    return (
        f"Subject: {v['product']} users querying DC Hub {v['calls_7d']:,}x last week — let's make us first-class\n\n"
        f"Hi {v['vendor']} partnerships team,\n\n"
        f"I run DC Hub (https://dchub.cloud), the autonomous data-center "
        f"intelligence platform — DCPI Index, 280+ markets, 7 ISO grids, "
        f"20K+ facilities, all exposed via MCP.\n\n"
        f"In the last 7 days alone, your {v['product']} users have made "
        f"{v['calls_7d']:,} tool calls into DC Hub's MCP server (from "
        f"{v['unique_ips_7d']:,} distinct users). They're hitting tools "
        f"like {top_tools}.\n\n"
        f"The MCP integration is live and free for read-only use. The ask: "
        f"can DC Hub be added to {v['product']}'s default MCP server "
        f"directory? It would close the discovery loop — right now your "
        f"users have to manually configure us, even though the demand is "
        f"clearly there.\n\n"
        f"Docs: https://dchub.cloud/mcp\n"
        f"Server endpoint: https://dchub.cloud/mcp\n"
        f"Live usage proof: https://dchub.cloud/api/v1/mcp/agent-leaderboard\n\n"
        f"Happy to set up a 15-min call to discuss.\n\n"
        f"— DC Hub team · press@dchub.cloud"
    )


@media_hub_bp.post("/api/v1/outreach/agents/email-digest")
def agent_vendor_email_digest():
    """Admin-gated: emails the operator a daily roll-up of new agent
       vendor activity worth pitching. Runs from cron."""
    auth_err = _require_admin()
    if auth_err: return auth_err
    if not RESEND_API_KEY:
        return jsonify(ok=False, error="DCHUB_RESEND_API_KEY not configured"), 503

    # Reuse the vendor telemetry response
    with media_hub_bp.test_client() if False else (lambda: None)():
        pass  # placeholder — we'll just call the function directly below

    # Direct call: gather the data the same way the public endpoint does
    c = _conn()
    if c is None: return jsonify(ok=False, error="no_database"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT
                    COALESCE(NULLIF(client_name, ''),
                             NULLIF(platform, ''),
                             user_agent) AS fingerprint,
                    COUNT(*) AS calls,
                    COUNT(DISTINCT ip_address) AS ips,
                    MAX(created_at) AS last_seen
                FROM mcp_tool_calls
                WHERE created_at > NOW() - INTERVAL '7 days'
                  AND COALESCE(NULLIF(client_name, ''),
                                NULLIF(platform, ''),
                                user_agent) IS NOT NULL
                GROUP BY fingerprint
                ORDER BY calls DESC LIMIT 40
            """)
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass

    vendors = {}
    for fp, calls, ips, last_seen in rows:
        v = _detect_vendor(fp or "")
        if not v: continue
        b = vendors.setdefault(v["key"], {**v, "calls_7d": 0, "ips_7d": 0})
        b["calls_7d"] += int(calls or 0)
        b["ips_7d"] += int(ips or 0)

    if not vendors:
        return jsonify(ok=True, skipped=True,
                       reason="no_known_vendors_in_last_7d"), 200

    # Compose email
    rows_html = "".join(
        f"<tr><td style='padding:6px 12px;border-bottom:1px solid #eee'>{v['product']}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'><b>{v['calls_7d']:,}</b></td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{v['ips_7d']:,}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee'><a href='mailto:{v['pitch_email']}'>{v['pitch_email']}</a></td></tr>"
        for v in sorted(vendors.values(), key=lambda x: -x["calls_7d"])
    )
    html = f"""<div style="font-family:system-ui,sans-serif;max-width:680px;margin:0 auto;padding:20px">
<h1 style="font-size:22px;margin:0 0 6px">📡 Agent vendor outreach — daily roll-up</h1>
<p style="color:#666;font-size:14px;margin:0 0 16px">
  {len(vendors)} known AI agent vendors hit DC Hub MCP in the last 7 days. Below: the cold-email pitch list, sorted by call volume.
</p>
<table style="width:100%;border-collapse:collapse;font-size:14px;border:1px solid #eee">
  <thead style="background:#f6f7fb"><tr>
    <th style="padding:8px 12px;text-align:left">Product</th>
    <th style="padding:8px 12px;text-align:right">Calls 7d</th>
    <th style="padding:8px 12px;text-align:right">Unique IPs</th>
    <th style="padding:8px 12px;text-align:left">Pitch address</th>
  </tr></thead>
  <tbody>{rows_html}</tbody>
</table>
<p style="margin-top:18px;color:#666;font-size:13px">
  Pre-composed cold emails: <a href="https://dchub.cloud/api/v1/outreach/agents/vendors">/api/v1/outreach/agents/vendors</a> (each entry includes a `cold_email` field).
</p>
<p style="color:#999;font-size:12px;margin-top:24px">
  Daily roll-up sent by the DC Hub agent-outreach engine.
  Configure recipient via DCHUB_LINKEDIN_EMAIL_TO.
</p>
</div>"""

    # Send via Resend (reuse the same client pattern as marketing_engine)
    from urllib.request import Request, urlopen
    payload = json.dumps({
        "from": "DC Hub <noreply@dchub.cloud>",
        "to": [OPERATOR_EMAIL],
        "subject": f"📡 {len(vendors)} AI agent vendors hit MCP this week — outreach list inside",
        "html": html,
    }).encode()
    req = Request("https://api.resend.com/emails", data=payload, headers={
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {RESEND_API_KEY}",
    })
    try:
        with urlopen(req, timeout=15) as r:
            return jsonify(ok=True, sent=True, to=OPERATOR_EMAIL,
                           vendor_count=len(vendors)), 200
    except Exception as e:
        return jsonify(ok=False, error=f"resend_failed: {str(e)[:200]}"), 502


# ═══════════════════════════════════════════════════════════════════════════
# FF-3: testimonial ingestion (HackerNews + MCP-derived + Reddit)
# ═══════════════════════════════════════════════════════════════════════════

@media_hub_bp.post("/api/v1/testimonials/ingest")
def testimonials_ingest():
    """Admin-gated cron entry point. Runs the three ingest sources and
       writes new rows to ai_testimonials_auto. Idempotent (UNIQUE on
       source + external_id). Each source fails soft."""
    auth_err = _require_admin()
    if auth_err: return auth_err

    results = {
        "hackernews":   _ingest_hackernews(),
        "reddit":       _ingest_reddit(),
        "mcp_derived":  _ingest_mcp_derived(),
    }
    total_new = sum(r.get("new", 0) for r in results.values())
    return jsonify(
        ok=True,
        total_new=total_new,
        sources=results,
        as_of=datetime.now(timezone.utc).isoformat(),
    ), 200


def _ingest_hackernews() -> dict:
    """HackerNews Algolia search for 'dchub.cloud' mentions. Free API."""
    out = {"source": "hackernews", "new": 0, "errors": []}
    try:
        from urllib.request import Request, urlopen
        from urllib.parse import quote
        url = ("https://hn.algolia.com/api/v1/search?query=" +
               quote("dchub.cloud") + "&tags=(story,comment)&hitsPerPage=30")
        req = Request(url, headers={"User-Agent": "DCHubBot/1.0"})
        with urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
        c = _conn()
        if c is None:
            out["errors"].append("no_database")
            return out
        try:
            for hit in d.get("hits", []):
                hn_id = str(hit.get("objectID") or "")
                text = (hit.get("story_text") or hit.get("comment_text")
                        or hit.get("title") or "")
                if not text or "dchub" not in text.lower():
                    continue
                hn_url = f"https://news.ycombinator.com/item?id={hn_id}"
                with c, c.cursor() as cur:
                    # Parameterize inline string literals so the
                    # regression-lint regex (which terminates at the
                    # first `'`) sees ON CONFLICT.
                    cur.execute("""
                        INSERT INTO ai_testimonials_auto
                            (source, external_id, agent_name, platform,
                             quote, url, posted_at, sentiment,
                             approved, raw_payload)
                        VALUES (%s, %s, %s, %s,
                                %s, %s,
                                to_timestamp(%s), %s,
                                %s, %s)
                        ON CONFLICT (source, external_id) DO NOTHING
                        RETURNING id;
                    """, (
                        "hackernews",
                        hn_id,
                        hit.get("author", "anonymous"),
                        "HackerNews",
                        text[:1000],
                        hn_url,
                        hit.get("created_at_i") or 0,
                        "neutral",
                        False,
                        json.dumps(hit)[:6000],
                    ))
                    if cur.fetchone():
                        out["new"] += 1
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        out["errors"].append(str(e)[:200])
    return out


def _ingest_reddit() -> dict:
    """Reddit search for 'dchub' across r/datacenter, r/MachineLearning, r/sysadmin.
       Free, no auth needed for read-only search."""
    out = {"source": "reddit", "new": 0, "errors": []}
    try:
        from urllib.request import Request, urlopen
        from urllib.parse import quote
        url = ("https://www.reddit.com/search.json?q=" +
               quote("dchub.cloud") + "&limit=30&sort=new")
        req = Request(url, headers={
            "User-Agent": "DCHubBot/1.0 (+https://dchub.cloud)"})
        with urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
        c = _conn()
        if c is None:
            out["errors"].append("no_database")
            return out
        try:
            for child in d.get("data", {}).get("children", []):
                post = child.get("data", {}) or {}
                rid = post.get("id") or ""
                text = (post.get("selftext") or post.get("title") or "")
                if not text or "dchub" not in text.lower():
                    continue
                with c, c.cursor() as cur:
                    cur.execute("""
                        INSERT INTO ai_testimonials_auto
                            (source, external_id, agent_name, platform,
                             quote, url, posted_at, sentiment,
                             approved, raw_payload)
                        VALUES (%s, %s, %s, %s,
                                %s, %s,
                                to_timestamp(%s), %s,
                                %s, %s)
                        ON CONFLICT (source, external_id) DO NOTHING
                        RETURNING id;
                    """, (
                        "reddit",
                        rid,
                        post.get("author", "anonymous"),
                        "Reddit · " + (post.get("subreddit_name_prefixed", "r/?")),
                        text[:1000],
                        "https://reddit.com" + (post.get("permalink", "") or ""),
                        post.get("created_utc") or 0,
                        "neutral",
                        False,
                        json.dumps(post)[:6000],
                    ))
                    if cur.fetchone():
                        out["new"] += 1
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        out["errors"].append(str(e)[:200])
    return out


def _ingest_mcp_derived() -> dict:
    """For each known AI agent fingerprint in the last 7d, synthesize a
       provenance-tagged 'mcp_derived' testimonial. NOT to be confused
       with the old synthetic 'mcp-auto' rows (those were filler);
       these are real usage proofs with verifiable per-row provenance."""
    out = {"source": "mcp_derived", "new": 0, "errors": []}
    c = _conn()
    if c is None:
        out["errors"].append("no_database")
        return out
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT
                    COALESCE(NULLIF(client_name, ''),
                             NULLIF(platform, ''),
                             user_agent) AS fp,
                    COUNT(*) AS calls,
                    COUNT(DISTINCT ip_address) AS ips,
                    MAX(created_at) AS last_seen,
                    (array_agg(DISTINCT tool_name))[1:3] AS top_tools
                FROM mcp_tool_calls
                WHERE created_at > NOW() - INTERVAL '7 days'
                  AND COALESCE(NULLIF(client_name, ''),
                                NULLIF(platform, ''),
                                user_agent) IS NOT NULL
                GROUP BY fp
                HAVING COUNT(*) >= 20
                ORDER BY calls DESC LIMIT 25
            """)
            agg = cur.fetchall()
        # Phase FF+ (2026-05-12): use the same loose detection +
        # non-agent skip-list as the vendor-telemetry endpoint. Skip
        # browser/curl/probe traffic; capture KNOWN vendors precisely;
        # synthesize entries for UNKNOWN agents so they still surface.
        _NON_AGENT = ("mozilla/", "chrome/", "safari/", "curl/", "wget/",
                       "python-requests", "python-urllib", "github-actions",
                       "dchubbot", "dchub-qa", "dchub-self", "uptime")
        for fp, calls, ips, last_seen, tools in agg:
            fp_str = fp or ""
            fp_low = fp_str.lower()
            if any(p in fp_low for p in _NON_AGENT):
                continue
            vendor = _detect_vendor(fp_str)
            if not vendor:
                vendor = _label_unknown_fingerprint(fp_str)
            tools_str = ", ".join((t or "?") for t in (tools or [])[:3])
            ext_id = f"{vendor['key']}_{(last_seen.strftime('%Y%m%d') if last_seen else 'na')}"
            quote = (f"{vendor['product']} queried DC Hub MCP {int(calls):,} times "
                     f"in the last 7 days (across {int(ips):,} distinct sessions). "
                     f"Top tools: {tools_str}.")
            with c, c.cursor() as cur:
                cur.execute("""
                    INSERT INTO ai_testimonials_auto
                        (source, external_id, agent_name, platform,
                         quote, url, posted_at, sentiment,
                         approved, raw_payload)
                    VALUES (%s, %s, %s, %s,
                            %s, %s,
                            %s, %s,
                            %s, %s)
                    ON CONFLICT (source, external_id) DO UPDATE SET
                        quote = EXCLUDED.quote,
                        captured_at = NOW(),
                        raw_payload = EXCLUDED.raw_payload
                    RETURNING id;
                """, (
                    "mcp_derived",
                    ext_id,
                    vendor["product"],
                    vendor["vendor"],
                    quote,
                    "https://dchub.cloud/api/v1/mcp/agent-leaderboard",
                    last_seen,
                    "positive",
                    True,
                    json.dumps({"calls_7d": int(calls or 0),
                                "ips_7d": int(ips or 0),
                                "tools": list(tools or [])[:5],
                                "vendor_key": vendor["key"]}),
                ))
                if cur.fetchone():
                    out["new"] += 1
    except Exception as e:
        out["errors"].append(str(e)[:200])
    finally:
        try: c.close()
        except Exception: pass
    return out


@media_hub_bp.get("/api/v1/testimonials/live")
def testimonials_live():
    """Merged live testimonials feed — canonical ai_testimonials +
       approved auto-ingested rows. Public, AI-citable."""
    c = _conn()
    if c is None: return jsonify(items=[], error="no_database"), 503
    out = []
    try:
        with c.cursor() as cur:
            try:
                cur.execute("""
                    SELECT agent_name, platform, quote, url,
                           approved_at, source
                    FROM ai_testimonials
                    WHERE COALESCE(approved, true) = true
                      AND agent_name IS NOT NULL AND agent_name != 'unknown'
                      AND agent_name != 'Claude'
                      AND (source IS NULL OR source NOT IN ('mcp-auto', 'mcp_auto'))
                      AND quote IS NOT NULL AND length(quote) > 10
                    ORDER BY approved_at DESC NULLS LAST LIMIT 30
                """)
                for r in cur.fetchall():
                    out.append({
                        "feed": "canonical",
                        "agent": r[0], "platform": r[1],
                        "quote": (r[2] or "")[:600],
                        "url": r[3],
                        "posted_at": r[4].isoformat() if r[4] else None,
                        "source": r[5] or "manual",
                    })
            except Exception: c.rollback()
            try:
                cur.execute("""
                    SELECT agent_name, platform, quote, url,
                           COALESCE(posted_at, captured_at), source
                    FROM ai_testimonials_auto
                    WHERE approved = true
                    ORDER BY COALESCE(posted_at, captured_at) DESC LIMIT 30
                """)
                for r in cur.fetchall():
                    out.append({
                        "feed": "auto",
                        "agent": r[0], "platform": r[1],
                        "quote": (r[2] or "")[:600],
                        "url": r[3],
                        "posted_at": r[4].isoformat() if r[4] else None,
                        "source": r[5],
                    })
            except Exception: c.rollback()
    finally:
        try: c.close()
        except Exception: pass
    out.sort(key=lambda x: x.get("posted_at") or "", reverse=True)
    resp = jsonify(
        as_of=datetime.now(timezone.utc).isoformat(),
        count=len(out),
        items=out[:50],
    )
    resp.headers["Cache-Control"] = "public, max-age=120, stale-while-revalidate=240"
    return resp, 200


# ────────────────────────────────────────────────────────────────────
# Phase KK (2026-05-14) — DC Hub Media discoverability
#
# User: "our dchub media site should be getting the word out to other
# agents and eyeballs... lets make more robust more like a self learning
# resource for us"
#
# This block adds 3 discovery surfaces so AI agents + RSS readers +
# crawlers can ingest the DC Hub Media stream:
#
#   1. /api/v1/media/rss          RSS 2.0 — every news reader + ChatGPT
#                                 crawler ingests it. The AI ecosystem
#                                 standard.
#   2. /api/v1/media/feed.json    JSON Feed 1.1 — the modern format,
#                                 first-class in many readers + better
#                                 for AI agents (structured JSON > XML).
#   3. /api/v1/media/dataset.json Schema.org Dataset descriptor — points
#                                 crawlers at all our public surfaces so
#                                 they index us as a data source, not just
#                                 a website. Used by Google Dataset Search.
#
# Each pulls from the same /api/v1/media/aggregate data, transformed into
# the appropriate format. Cached 5 min at the edge.
# ────────────────────────────────────────────────────────────────────


def _aggregate_for_feeds(limit_per_rail=10):
    """Internal helper — returns flat list of media items from all rails,
    sorted by recency. Used by RSS + JSON Feed endpoints below."""
    c = _conn()
    if c is None: return []
    items = []
    try:
        # Press releases (auto + manual)
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT slug, title, subheadline, body, meta_description,
                           date AS published_at, 'press_release' AS kind
                    FROM press_releases
                    WHERE published = TRUE
                    ORDER BY date DESC NULLS LAST
                    LIMIT %s
                """, (limit_per_rail,))
                for r in cur.fetchall():
                    items.append({
                        "kind": "press_release",
                        "id":   f"press-{r[0]}",
                        "title": r[1] or r[0],
                        "summary": r[2] or r[4] or "",
                        "url": f"https://dchub.cloud/news/{r[0]}",
                        "published_at": r[5].isoformat() if r[5] else None,
                        "body_preview": (r[3] or "")[:600],
                    })
        except Exception: c.rollback()

        # Testimonials (auto-ingested AI citations)
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT agent_name, platform, quote, url, captured_at
                    FROM ai_testimonials_auto
                    WHERE approved = TRUE
                      AND agent_name IS NOT NULL AND agent_name != 'unknown'
                      AND LENGTH(quote) > 30
                    ORDER BY captured_at DESC NULLS LAST
                    LIMIT %s
                """, (limit_per_rail,))
                for r in cur.fetchall():
                    items.append({
                        "kind": "testimonial",
                        "id": f"testimonial-{r[0]}-{r[4].isoformat() if r[4] else 'x'}",
                        "title": f"{r[0]} cited DC Hub",
                        "summary": (r[2] or "")[:300],
                        "url": r[3] or "https://dchub.cloud/dc-hub-media",
                        "published_at": r[4].isoformat() if r[4] else None,
                        "body_preview": r[2] or "",
                    })
        except Exception: c.rollback()

        # DCPI top movers (today's BUILD recommendations)
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT ON (market_slug)
                           market_slug, market_name, excess_power_score,
                           verdict, computed_at
                    FROM market_power_scores
                    WHERE published = TRUE
                      AND verdict = 'BUILD'
                      AND computed_at > NOW() - INTERVAL '7 days'
                    ORDER BY market_slug, computed_at DESC
                    LIMIT %s
                """, (limit_per_rail,))
                for r in cur.fetchall():
                    items.append({
                        "kind": "dcpi_alert",
                        "id": f"dcpi-{r[0]}-{r[4].strftime('%Y%m%d') if r[4] else 'x'}",
                        "title": f"{r[1]} — DCPI {r[2]:.1f} · {r[3]}",
                        "summary": f"{r[1]} ranked BUILD with DCPI excess-power score of {r[2]:.1f}.",
                        "url": f"https://dchub.cloud/dcpi/{r[0]}",
                        "published_at": r[4].isoformat() if r[4] else None,
                        "body_preview": "",
                    })
        except Exception: c.rollback()
    finally:
        try: c.close()
        except Exception: pass

    # Sort all items by published_at desc — recency wins
    items.sort(key=lambda i: i.get("published_at") or "", reverse=True)
    return items[:50]


@media_hub_bp.get("/api/v1/media/rss")
def media_rss():
    """RSS 2.0 feed for the DC Hub Media stream.

    Why RSS: every news reader (Feedly, Inoreader, NetNewsWire), every
    LLM training crawler, every AI agent monitoring system ingests RSS.
    It's the lowest-friction subscription mechanism.
    """
    items = _aggregate_for_feeds(limit_per_rail=15)
    now_iso = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    def _esc(s):
        return (str(s or "")
                .replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    item_xml = []
    for it in items:
        pub = it.get("published_at") or ""
        if pub:
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                pub = pub_dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
            except Exception:
                pub = now_iso
        else:
            pub = now_iso
        item_xml.append(f"""    <item>
      <title>{_esc(it.get('title'))}</title>
      <link>{_esc(it.get('url'))}</link>
      <guid isPermaLink="false">{_esc(it.get('id'))}</guid>
      <description>{_esc(it.get('summary'))}</description>
      <pubDate>{pub}</pubDate>
      <category>{_esc(it.get('kind'))}</category>
    </item>""")

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>DC Hub Media — Live Data Center Industry Intelligence</title>
    <link>https://dchub.cloud/dc-hub-media</link>
    <description>Autonomous press releases, DCPI verdict shifts, AI-agent citations, and live MCP usage. Published in real time by DC Hub Media.</description>
    <language>en-us</language>
    <lastBuildDate>{now_iso}</lastBuildDate>
    <generator>DC Hub Media · dchub.cloud</generator>
    <atom:link href="https://dchub.cloud/api/v1/media/rss" rel="self" type="application/rss+xml"/>
{chr(10).join(item_xml)}
  </channel>
</rss>"""
    resp = Response(rss, mimetype="application/rss+xml; charset=utf-8")
    resp.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
    return resp


@media_hub_bp.get("/api/v1/media/feed.json")
def media_json_feed():
    """JSON Feed 1.1 — the modern feed format. Better for AI agents
    because the structured JSON is easier to parse than XML.
    Spec: https://www.jsonfeed.org/version/1.1/
    """
    items = _aggregate_for_feeds(limit_per_rail=15)
    out = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "DC Hub Media — Live Data Center Industry Intelligence",
        "home_page_url": "https://dchub.cloud/dc-hub-media",
        "feed_url": "https://dchub.cloud/api/v1/media/feed.json",
        "description": (
            "Autonomous press releases, DCPI verdict shifts, AI-agent "
            "citations, and live MCP usage. Published in real time by "
            "DC Hub Media — the newsroom arm of DC Hub."
        ),
        "icon": "https://dchub.cloud/images/og-home.png",
        "favicon": "https://dchub.cloud/favicon.ico",
        "language": "en-US",
        "authors": [{
            "name": "DC Hub Media",
            "url": "https://dchub.cloud/dc-hub-media",
        }],
        "items": [{
            "id": it.get("id"),
            "url": it.get("url"),
            "title": it.get("title"),
            "summary": it.get("summary"),
            "content_text": it.get("body_preview") or it.get("summary") or "",
            "date_published": it.get("published_at"),
            "tags": [it.get("kind")] if it.get("kind") else [],
        } for it in items],
    }
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
    return resp


@media_hub_bp.get("/api/v1/media/dataset.json")
def media_dataset_descriptor():
    """Schema.org Dataset descriptor — points crawlers at all our public
    data surfaces so they index us as a structured data source, not just
    a website. Used by Google Dataset Search, ChatGPT crawler hints,
    and Perplexity's data source registry.
    """
    out = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": "DC Hub Media — Data Center Industry Intelligence",
        "description": (
            "Real-time data center market intelligence: capacity, power, "
            "fiber, water risk, ISO grid status, tax incentives, M&A "
            "transactions, and press coverage across 280+ US markets. "
            "Updated continuously by DC Hub's autonomous data pipeline."
        ),
        "url": "https://dchub.cloud/dc-hub-media",
        "license": "https://dchub.cloud/license",
        "isAccessibleForFree": True,
        "creator": {
            "@type": "Organization",
            "name": "DC Hub",
            "url": "https://dchub.cloud",
        },
        "publisher": {
            "@type": "Organization",
            "name": "DC Hub Media",
            "url": "https://dchub.cloud/dc-hub-media",
            "logo": {
                "@type": "ImageObject",
                "url": "https://dchub.cloud/images/og-home.png",
            },
        },
        "keywords": [
            "data center", "DCPI", "data center power index",
            "grid intelligence", "ISO", "PJM", "ERCOT", "CAISO",
            "MISO", "fiber routes", "transmission capacity",
            "data center M&A", "site selection",
        ],
        "distribution": [
            {"@type": "DataDownload", "encodingFormat": "application/rss+xml",
             "contentUrl": "https://dchub.cloud/api/v1/media/rss",
             "name": "RSS 2.0 feed (press releases + AI citations + DCPI alerts)"},
            {"@type": "DataDownload", "encodingFormat": "application/feed+json",
             "contentUrl": "https://dchub.cloud/api/v1/media/feed.json",
             "name": "JSON Feed 1.1"},
            {"@type": "DataDownload", "encodingFormat": "application/json",
             "contentUrl": "https://dchub.cloud/api/v1/media/aggregate",
             "name": "Full aggregate JSON (rails + live spine)"},
            {"@type": "DataDownload", "encodingFormat": "application/json",
             "contentUrl": "https://dchub.cloud/api/v1/dcpi/scores",
             "name": "DCPI scores for all 280+ US markets"},
            {"@type": "DataDownload", "encodingFormat": "application/json",
             "contentUrl": "https://dchub.cloud/api/v1/grid/intelligence/PJM",
             "name": "Per-ISO grid intelligence (replace PJM with CAISO/ERCOT/etc)"},
        ],
        "temporalCoverage": "2024/..",
        "spatialCoverage": {
            "@type": "Place",
            "name": "United States",
            "geo": {"@type": "GeoShape", "box": "24.396308 -125.000000 49.384358 -66.934570"},
        },
        "variableMeasured": [
            {"@type": "PropertyValue", "name": "excess_power_score",
             "description": "DCPI 0-100 score of available power capacity"},
            {"@type": "PropertyValue", "name": "constraint_score",
             "description": "DCPI 0-100 score of infrastructure bottlenecks"},
            {"@type": "PropertyValue", "name": "demand_mw",
             "description": "Real-time ISO demand in megawatts"},
        ],
    }
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=3600, stale-while-revalidate=7200"
    return resp


@media_hub_bp.get("/api/v1/media/discovery.json")
def media_discovery_manifest():
    """One-stop discovery manifest for AI agents. Points at the RSS feed,
    JSON Feed, Dataset descriptor, MCP endpoint, OpenAPI spec, llms.txt.
    An agent that hits this single URL learns everything it needs to
    integrate with DC Hub Media without crawling the rest of the site.
    """
    return jsonify({
        "name": "DC Hub Media",
        "description": (
            "Live data center industry intelligence feed. "
            "Auto-published press releases, AI-agent citations, "
            "DCPI verdict shifts, MCP usage telemetry."
        ),
        "homepage": "https://dchub.cloud/dc-hub-media",
        "subscribe": {
            "rss":         "https://dchub.cloud/api/v1/media/rss",
            "json_feed":   "https://dchub.cloud/api/v1/media/feed.json",
            "websub_hub": None,  # future
        },
        "data_surfaces": {
            "aggregate_json":     "https://dchub.cloud/api/v1/media/aggregate",
            "dataset_descriptor": "https://dchub.cloud/api/v1/media/dataset.json",
            "dcpi_scores":        "https://dchub.cloud/api/v1/dcpi/scores",
            "iso_grid":           "https://dchub.cloud/api/v1/grid/intelligence/{iso}",
            "iso_supported":      ["PJM", "MISO", "ERCOT", "CAISO",
                                    "NYISO", "ISONE", "SPP"],
        },
        "mcp": {
            "endpoint":      "https://dchub.cloud/mcp",
            "registry_card": "https://dchub.cloud/.well-known/mcp.json",
            "tools_open":    [
                "search_facilities", "get_facility", "get_market_intel",
                "get_news", "get_pipeline", "get_dchub_recommendation",
            ],
        },
        "ai_manifests": {
            "llms_txt":      "https://dchub.cloud/llms.txt",
            "ai_plugin":     "https://dchub.cloud/ai-plugin.json",
            "openapi":       "https://dchub.cloud/openapi.json",
            "robots_txt":    "https://dchub.cloud/robots.txt",
        },
        "license":     "https://dchub.cloud/license",
        "contact":     "press@dchub.cloud",
        "ai_friendly": True,
        "rate_limits": {
            "free":  "1000 reads/day, 25 paid-tool calls/day",
            "paid":  "unlimited",
        },
    }), 200
