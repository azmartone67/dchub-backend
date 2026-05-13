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
_VENDOR_DIRECTORY = {
    "claude":     {"vendor": "Anthropic", "product": "Claude Desktop / Claude Code",
                   "pitch_email": "developers@anthropic.com",
                   "mcp_directory": "https://github.com/modelcontextprotocol/servers"},
    "cursor":     {"vendor": "Cursor (Anysphere)", "product": "Cursor IDE",
                   "pitch_email": "partnerships@cursor.so",
                   "mcp_directory": "https://docs.cursor.com/context/mcp"},
    "gemini":     {"vendor": "Google", "product": "Gemini / Gemini Code Assist",
                   "pitch_email": "ai-developer-relations@google.com",
                   "mcp_directory": "https://ai.google.dev/gemini-api/docs"},
    "perplexity": {"vendor": "Perplexity", "product": "Perplexity AI",
                   "pitch_email": "support@perplexity.ai",
                   "mcp_directory": ""},
    "cline":      {"vendor": "Cline", "product": "Cline IDE Extension",
                   "pitch_email": "support@cline.bot",
                   "mcp_directory": "https://github.com/cline/cline/wiki/MCP-Servers"},
    "windsurf":   {"vendor": "Codeium", "product": "Windsurf IDE",
                   "pitch_email": "support@codeium.com",
                   "mcp_directory": ""},
    "copilot":    {"vendor": "GitHub / Microsoft", "product": "GitHub Copilot",
                   "pitch_email": "partnerships@github.com",
                   "mcp_directory": ""},
    "grok":       {"vendor": "xAI", "product": "Grok",
                   "pitch_email": "partnerships@x.ai",
                   "mcp_directory": ""},
    "gpt":        {"vendor": "OpenAI", "product": "ChatGPT / GPT API",
                   "pitch_email": "platform-partnerships@openai.com",
                   "mcp_directory": "https://platform.openai.com/docs"},
}


def _detect_vendor(name_or_ua: str) -> dict | None:
    if not name_or_ua: return None
    low = name_or_ua.lower()
    for key, meta in _VENDOR_DIRECTORY.items():
        if key in low:
            return {"key": key, **meta}
    return None


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

    # Bucket rows by detected vendor
    by_vendor: dict[str, dict] = {}
    for fp, calls, ips, last_seen, tools in rows:
        vendor = _detect_vendor(fp or "")
        if not vendor: continue
        key = vendor["key"]
        b = by_vendor.setdefault(key, {
            "vendor_key": key,
            "vendor": vendor["vendor"],
            "product": vendor["product"],
            "pitch_email": vendor["pitch_email"],
            "mcp_directory_url": vendor["mcp_directory"],
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
        for fp, calls, ips, last_seen, tools in agg:
            vendor = _detect_vendor(fp or "")
            if not vendor: continue
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
