"""Phase DDD (2026-05-16) — MCP as a living organism.

Three jobs:
  1. /api/v1/mcp/growth         — daily-snapshot pulse of the MCP server
                                   (calls, platforms, conversion ratio,
                                   top-demand tools, growth trend)
  2. /api/v1/mcp/demand-gaps    — what agents *wanted* that we don't have
                                   (404'd tool names, repeatedly-paywalled
                                   tools by same user, common args that
                                   returned empty results) → the "what
                                   should we build next" signal
  3. POST /api/v1/mcp/growth/snapshot — admin/cron entry; computes the
                                         daily snapshot, persists to
                                         mcp_growth_snapshots table

The brain consumes (1) for trend detection and (2) for autonomous tool-
suggestion findings. Layer 5's code-proposal loop can read demand-gaps
to draft tool additions.

Together with media_pulse.py, this turns MCP from a static catalog into
an organism that LEARNS what to build, MEASURES what's working, and
ROUTES failures to the brain for healing.
"""

from __future__ import annotations

import os
import json
import datetime
from flask import Blueprint, jsonify, request
import psycopg2
import psycopg2.extras


mcp_growth_bp = Blueprint("mcp_growth", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


# r37 (2026-05-25): same UA-based platform classifier the funnel
# endpoint uses (flask_mcp_endpoints._platform_case). Centralizing
# here so the brain L23 audit + dashboards see consistent platform
# names. Previously the platforms_24h list read mcp_call_log.platform
# directly — most rows hold "mcp" (generic write-time bucket) so the
# audit's WoW comparison looked like a single-platform collapse when
# it was actually a classification artifact.
_PLATFORM_CASE_TC = r"""
    CASE
        WHEN NULLIF(LOWER(client_name), '') IS NOT NULL
             AND client_name !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
             AND client_name NOT IN ('unknown', 'mcp')
            THEN LOWER(client_name)
        WHEN user_agent ILIKE '%dchub-%' OR user_agent ILIKE '%dchubhealer%'
            OR user_agent ILIKE '%brain-v2-headless%' OR user_agent ILIKE '%brain-radar%'
            OR user_agent ILIKE '%uptimerobot%'
            THEN 'internal-dchub'
        -- r43 (2026-05-25): claude-code is its own bucket (THE specific
        -- Anthropic CLI), separate from generic 'claude' SDK detection.
        WHEN user_agent ILIKE '%claude-code%' OR user_agent ILIKE '%claude_code%'
                                                 THEN 'claude-code'
        WHEN user_agent ILIKE '%@modelcontextprotocol/sdk%'
            OR user_agent ILIKE '%modelcontextprotocol%'
            THEN 'mcp-sdk'
        WHEN user_agent ILIKE '%mcp-inspector%'  THEN 'mcp-inspector'
        WHEN user_agent ILIKE '%n8n%'            THEN 'n8n'
        WHEN user_agent ILIKE '%smithery%'       THEN 'smithery'
        WHEN user_agent ILIKE '%chatgpt%' OR user_agent ILIKE '%openai%'
            OR user_agent ILIKE '%gpt-4%' OR user_agent ILIKE '%gpt4%'
                                                 THEN 'chatgpt'
        WHEN user_agent ILIKE '%claude%' OR user_agent ILIKE '%anthropic%'
                                                 THEN 'claude'
        WHEN user_agent ILIKE '%perplexity%'     THEN 'perplexity'
        WHEN user_agent ILIKE '%gemini%' OR user_agent ILIKE '%googleother%'
            OR user_agent ILIKE '%google-extended%'
                                                 THEN 'gemini'
        WHEN user_agent ILIKE '%groq%'           THEN 'groq'
        WHEN user_agent ILIKE '%cursor%'         THEN 'cursor'
        WHEN user_agent ILIKE '%windsurf%' OR user_agent ILIKE '%codeium%'
                                                 THEN 'windsurf'
        WHEN user_agent ILIKE '%continue%'       THEN 'continue.dev'
        WHEN user_agent ILIKE '%cody%' OR user_agent ILIKE '%sourcegraph%'
                                                 THEN 'sourcegraph-cody'
        WHEN user_agent ILIKE '%copilot%'        THEN 'github-copilot'
        WHEN user_agent ILIKE '%cline%'          THEN 'cline'
        WHEN user_agent ILIKE '%phind%'          THEN 'phind'
        WHEN user_agent ILIKE '%you.com%' OR user_agent ILIKE '%youbot%'
                                                 THEN 'you.com'
        WHEN user_agent ILIKE '%meta-external%' OR user_agent ILIKE '%llama%'
            OR user_agent ILIKE '%facebookbot%'  THEN 'meta-ai'
        WHEN user_agent ILIKE '%applebot-extended%' OR user_agent ILIKE '%apple-intelligence%'
                                                 THEN 'apple-intelligence'
        WHEN user_agent ILIKE '%mistral%'        THEN 'mistral'
        WHEN user_agent ILIKE '%deepseek%'       THEN 'deepseek'
        WHEN user_agent ILIKE '%grok%' OR user_agent ILIKE '%x-ai%' OR user_agent ILIKE '%xai%'
                                                 THEN 'grok'
        -- r43: Glean (enterprise AI search) + Tavily (research) + Cohere
        WHEN user_agent ILIKE '%glean%'          THEN 'glean'
        WHEN user_agent ILIKE '%tavily%'         THEN 'tavily'
        WHEN user_agent ILIKE '%cohere%'         THEN 'cohere'
        -- r43: Bedrock + Vertex AI surface as their cloud's marker
        WHEN user_agent ILIKE '%bedrock%' OR user_agent ILIKE '%aws-sdk%'
                                                 THEN 'aws-bedrock'
        WHEN user_agent ILIKE '%vertexai%' OR user_agent ILIKE '%vertex-ai%'
            OR user_agent ILIKE '%google-cloud%' THEN 'google-vertex'
        WHEN user_agent ILIKE '%curl%'           THEN 'curl'
        WHEN user_agent ILIKE '%python-httpx%' OR user_agent ILIKE '%httpx/%'
                                                 THEN 'python-httpx'
        WHEN user_agent ILIKE '%python%' OR user_agent ILIKE '%requests%'
                                                 THEN 'python-script'
        WHEN user_agent ILIKE '%node-fetch%' OR user_agent ILIKE '%undici%'
            OR user_agent ILIKE '%axios%' OR user_agent ILIKE '%got/%'
                                                 THEN 'node-http-client'
        WHEN user_agent ILIKE '%node%'           THEN 'node-script'
        WHEN user_agent ILIKE '%postman%'        THEN 'postman'
        WHEN user_agent ILIKE '%insomnia%'       THEN 'insomnia'
        -- r43 (2026-05-25): if UA is empty/missing but client_name
        -- looks like a session UUID, emit 'mcp-anon' (anonymous MCP
        -- session) instead of 'unknown' — that's the modern MCP
        -- pattern: clients connect with a session ID, send no UA,
        -- and we have NO way to know which vendor. Surface this
        -- explicitly so the brain knows to chase identification.
        WHEN client_name ~* '^[0-9a-f]{8}-[0-9a-f]{4}'
            AND (user_agent IS NULL OR user_agent = '')
                                                 THEN 'mcp-anon-session'
        ELSE 'unknown'
    END
"""


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS mcp_growth_snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    snapshot_date       DATE NOT NULL,
    tool_calls_7d       INT NOT NULL DEFAULT 0,
    tool_calls_30d      INT NOT NULL DEFAULT 0,
    unique_platforms_7d INT NOT NULL DEFAULT 0,
    unique_ips_7d       INT NOT NULL DEFAULT 0,
    upgrade_signals_7d  INT NOT NULL DEFAULT 0,
    conversions_7d      INT NOT NULL DEFAULT 0,
    conversion_ratio    TEXT,
    top_demand_tool     TEXT,
    top_demand_calls    INT,
    top_attributed_platform TEXT,
    unknown_platform_pct REAL,
    payload             JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_mcp_growth_snapshot_date
    ON mcp_growth_snapshots(snapshot_date DESC);

-- r44 (2026-05-25): add session_id column to mcp_tool_calls.
-- Modern MCP transport passes Mcp-Session-Id as an HTTP header;
-- we capture it at write time so unique_sessions_7d becomes a real
-- moat metric even when UA + clientInfo are empty.
ALTER TABLE mcp_tool_calls ADD COLUMN IF NOT EXISTS session_id TEXT;
CREATE INDEX IF NOT EXISTS ix_mcp_tool_calls_session_id
    ON mcp_tool_calls (session_id) WHERE session_id IS NOT NULL;
"""

def _ensure_schema():
    c = _conn()
    if c is None: return False
    try:
        with c.cursor() as cur: cur.execute(_SCHEMA_DDL)
        return True
    except Exception as e:
        print(f"[mcp_growth] schema: {e}")
        return False
    finally:
        try: c.close()
        except Exception: pass

try: _ensure_schema()
except Exception: pass


def _admin_ok() -> bool:
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")
    provided = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    return not expected or provided == expected


# ── Growth pulse ──────────────────────────────────────────────────────
def _compute_growth() -> dict:
    """One-shot growth snapshot. Read-only — never writes."""
    out = {
        "tool_calls_7d":       0,
        "tool_calls_30d":      0,
        "unique_platforms_7d": 0,
        "unique_ips_7d":       0,
        "upgrade_signals_7d":  0,
        "conversions_7d":      0,
        "conversion_ratio":    None,
        "top_demand_tools":    [],
        "top_converted_tools": [],
        "platforms_24h":       [],
        "unknown_platform_pct": None,
        "tools_with_zero_conversions": [],
        "computed_at":         datetime.datetime.utcnow().isoformat() + "Z",
    }
    c = _conn()
    if c is None: return out
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # ── Volume ──
            try:
                cur.execute("""
                    SELECT COUNT(*) AS n FROM mcp_call_log
                     WHERE timestamp >= NOW() - INTERVAL '7 days'
                """)
                out["tool_calls_7d"] = int((cur.fetchone() or {"n":0})["n"] or 0)
                cur.execute("""
                    SELECT COUNT(*) AS n FROM mcp_call_log
                     WHERE timestamp >= NOW() - INTERVAL '30 days'
                """)
                out["tool_calls_30d"] = int((cur.fetchone() or {"n":0})["n"] or 0)
            except Exception:
                pass

            # ── Platform attribution ──
            try:
                cur.execute("""
                    SELECT
                      COUNT(DISTINCT platform) FILTER (WHERE platform NOT IN ('','unknown')) AS attributed,
                      COUNT(*) FILTER (WHERE platform IN ('','unknown') OR platform IS NULL) AS unknown_calls,
                      COUNT(*) AS total_calls
                      FROM mcp_call_log
                     WHERE timestamp >= NOW() - INTERVAL '7 days'
                """)
                r = cur.fetchone() or {}
                out["unique_platforms_7d"] = int(r.get("attributed") or 0)
                t = int(r.get("total_calls") or 0)
                u = int(r.get("unknown_calls") or 0)
                if t > 0:
                    out["unknown_platform_pct"] = round(100.0 * u / t, 1)
            except Exception:
                pass

            # ── Unique IPs ──
            try:
                cur.execute("""
                    SELECT COUNT(DISTINCT ip_address) AS n FROM mcp_tool_calls
                     WHERE created_at >= NOW() - INTERVAL '7 days'
                """)
                out["unique_ips_7d"] = int((cur.fetchone() or {"n":0})["n"] or 0)
            except Exception:
                pass

            # ── r44 (2026-05-25): Unique MCP sessions (7d, 30d) ──
            # session_id now captured from Mcp-Session-Id HTTP header at
            # write time. Real moat metric independent of UA / clientInfo:
            # counts how many distinct MCP clients are pinging us, even
            # when none identify themselves. Filters null + obvious test
            # placeholders ('test','demo','anonymous').
            try:
                cur.execute("""
                    SELECT
                      COUNT(DISTINCT session_id) FILTER (
                        WHERE created_at >= NOW() - INTERVAL '7 days'
                          AND session_id IS NOT NULL
                          AND session_id NOT IN ('test','demo','anonymous','')
                      ) AS sessions_7d,
                      COUNT(DISTINCT session_id) FILTER (
                        WHERE created_at >= NOW() - INTERVAL '30 days'
                          AND session_id IS NOT NULL
                          AND session_id NOT IN ('test','demo','anonymous','')
                      ) AS sessions_30d
                      FROM mcp_tool_calls
                """)
                row = cur.fetchone() or {}
                out["unique_sessions_7d"] = int(row.get("sessions_7d") or 0)
                out["unique_sessions_30d"] = int(row.get("sessions_30d") or 0)
            except Exception:
                # Column won't exist until the ALTER TABLE in _SCHEMA_DDL
                # has run — degrade silently.
                out["unique_sessions_7d"] = None
                out["unique_sessions_30d"] = None

            # ── Signals + conversions ──
            try:
                cur.execute("""
                    SELECT COUNT(*) AS n FROM mcp_upgrade_signals
                     WHERE created_at >= NOW() - INTERVAL '7 days'
                """)
                out["upgrade_signals_7d"] = int((cur.fetchone() or {"n":0})["n"] or 0)
            except Exception:
                pass
            try:
                cur.execute("""
                    SELECT COUNT(*) AS n FROM mcp_pair_codes
                     WHERE redeemed_at IS NOT NULL
                       AND redeemed_at >= NOW() - INTERVAL '7 days'
                """)
                out["conversions_7d"] = int((cur.fetchone() or {"n":0})["n"] or 0)
            except Exception:
                pass
            # Phase HH (2026-05-17): augment legacy mcp_pair_codes count
            # with auto_trial_keys actually used (call_count > 0). Phase
            # DDDDD's auto-mint trial flow generates conversions that
            # never touch mcp_pair_codes — so the pre-HH dashboard
            # showed "0 conversions" while the auto-trial pipeline was
            # quietly working. Both numbers go into conversions_7d so
            # the brain's stale-conversion detector + this snapshot
            # measure the SAME thing (any agent that started using a
            # dchub-issued key in the last 7 days).
            out["auto_trial_conversions_7d"] = 0
            try:
                cur.execute("""
                    SELECT COUNT(*) AS n FROM auto_trial_keys
                     WHERE minted_at >= NOW() - INTERVAL '7 days'
                       AND call_count > 0
                """)
                atc = int((cur.fetchone() or {"n":0})["n"] or 0)
                out["auto_trial_conversions_7d"] = atc
                out["conversions_7d"] = (out.get("conversions_7d") or 0) + atc
            except Exception:
                pass

            sigs = out["upgrade_signals_7d"]
            convs = out["conversions_7d"]
            if sigs > 0:
                out["conversion_ratio"] = (
                    f"1:{int(sigs / max(1, convs))}" if convs > 0 else f"1:{sigs}+"
                )

            # ── Top demand tools (called the most) ──
            try:
                cur.execute("""
                    SELECT tool, COUNT(*) AS calls,
                           COUNT(DISTINCT api_key) AS users
                      FROM mcp_call_log
                     WHERE timestamp >= NOW() - INTERVAL '7 days'
                       AND tool IS NOT NULL
                     GROUP BY tool ORDER BY calls DESC LIMIT 10
                """)
                out["top_demand_tools"] = [
                    {"tool": r["tool"], "calls": int(r["calls"]), "users": int(r["users"])}
                    for r in cur.fetchall()
                ]
            except Exception:
                pass

            # ── Top platforms last 24h ──
            # r37 (2026-05-25): rewritten to read mcp_tool_calls
            # (which has user_agent + client_name) with the
            # _PLATFORM_CASE_TC classifier. Previously read
            # mcp_call_log.platform which is the raw write-time bucket
            # — most rows were lumped as 'mcp', producing the bogus
            # -29.2% WoW signal that L23 flagged.
            try:
                cur.execute(
                    f"""SELECT {_PLATFORM_CASE_TC} AS platform,
                              COUNT(*) AS calls
                         FROM mcp_tool_calls
                        WHERE created_at >= NOW() - INTERVAL '24 hours'
                        GROUP BY {_PLATFORM_CASE_TC}
                        ORDER BY calls DESC LIMIT 15"""
                )
                out["platforms_24h"] = [
                    {"platform": (r["platform"] or "unknown")[:30],
                     "calls": int(r["calls"])}
                    for r in cur.fetchall()
                ]
            except Exception as _e_p24:
                # Fall back to the legacy mcp_call_log query if
                # mcp_tool_calls is unavailable.
                try:
                    cur.execute("""
                        SELECT platform, COUNT(*) AS calls
                          FROM mcp_call_log
                         WHERE timestamp >= NOW() - INTERVAL '24 hours'
                           AND platform IS NOT NULL
                         GROUP BY platform ORDER BY calls DESC LIMIT 10
                    """)
                    out["platforms_24h"] = [
                        {"platform": r["platform"][:30],
                         "calls": int(r["calls"])}
                        for r in cur.fetchall()
                    ]
                    out["platforms_24h_source"] = "fallback_mcp_call_log"
                except Exception:
                    pass

            # ── Tools with 0 conversions despite 50+ paywall signals ──
            # These are the "demand-trapped" tools: agents keep trying
            # but nobody converts. Marketing/pricing/CTA fix needed.
            try:
                cur.execute("""
                    WITH paid_demand AS (
                      SELECT tool, COUNT(*) AS signals
                        FROM mcp_upgrade_signals
                       WHERE created_at >= NOW() - INTERVAL '30 days'
                         AND tool IS NOT NULL
                       GROUP BY tool
                      HAVING COUNT(*) >= 50
                    ),
                    converted AS (
                      SELECT tool_name AS tool, COUNT(*) AS convs
                        FROM mcp_pair_codes
                       WHERE redeemed_at IS NOT NULL
                         AND redeemed_at >= NOW() - INTERVAL '30 days'
                         AND tool_name IS NOT NULL
                       GROUP BY tool_name
                    )
                    SELECT p.tool, p.signals, COALESCE(c.convs, 0) AS convs
                      FROM paid_demand p
                      LEFT JOIN converted c USING (tool)
                     WHERE COALESCE(c.convs, 0) = 0
                     ORDER BY p.signals DESC LIMIT 5
                """)
                out["tools_with_zero_conversions"] = [
                    {"tool": r["tool"], "signals_30d": int(r["signals"]),
                     "conversions_30d": int(r["convs"])}
                    for r in cur.fetchall()
                ]
            except Exception:
                pass
    finally:
        try: c.close()
        except Exception: pass
    return out


@mcp_growth_bp.route("/api/v1/mcp/growth", methods=["GET"])
def api_growth():
    """The MCP organism's pulse. Public, 5min cached."""
    snapshot = _compute_growth()
    # Add growth-rate by comparing to last snapshot
    c = _conn()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT tool_calls_7d, snapshot_date
                      FROM mcp_growth_snapshots
                     WHERE snapshot_date < CURRENT_DATE - INTERVAL '6 days'
                     ORDER BY snapshot_date DESC LIMIT 1
                """)
                r = cur.fetchone()
                if r and r[0]:
                    prev = int(r[0])
                    cur_v = snapshot.get("tool_calls_7d") or 0
                    pct = round(100.0 * (cur_v - prev) / max(1, prev), 1)
                    snapshot["calls_wow_growth_pct"] = pct
                    snapshot["calls_7d_one_week_ago"] = prev
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass
    resp = jsonify(snapshot)
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@mcp_growth_bp.route("/api/v1/mcp/growth/snapshot", methods=["POST"])
def api_growth_snapshot():
    """Cron entry — persist today's snapshot. Admin gated."""
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    payload = _compute_growth()
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        td_tools = payload.get("top_demand_tools") or []
        top_tool = (td_tools[0]["tool"] if td_tools else None)
        top_calls = (td_tools[0]["calls"] if td_tools else 0)
        platforms24h = payload.get("platforms_24h") or []
        # Top attributed platform = first non-unknown
        top_attributed = next(
            (p["platform"] for p in platforms24h
             if p["platform"] not in ("", "unknown", "mcp-worker")),
            None,
        )
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO mcp_growth_snapshots
                    (snapshot_date, tool_calls_7d, tool_calls_30d,
                     unique_platforms_7d, unique_ips_7d, upgrade_signals_7d,
                     conversions_7d, conversion_ratio, top_demand_tool,
                     top_demand_calls, top_attributed_platform,
                     unknown_platform_pct, payload)
                VALUES (CURRENT_DATE, %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, %s::jsonb)
                ON CONFLICT DO NOTHING
            """, (
                payload.get("tool_calls_7d"), payload.get("tool_calls_30d"),
                payload.get("unique_platforms_7d"), payload.get("unique_ips_7d"),
                payload.get("upgrade_signals_7d"), payload.get("conversions_7d"),
                payload.get("conversion_ratio"), top_tool, top_calls,
                top_attributed, payload.get("unknown_platform_pct"),
                json.dumps(payload, default=str),
            ))
        return jsonify(ok=True, persisted=True, snapshot=payload), 200
    except Exception as e:
        return jsonify(error="snapshot_failed", detail=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass


# ── Demand-gaps inference ─────────────────────────────────────────────
@mcp_growth_bp.route("/api/v1/mcp/demand-gaps", methods=["GET"])
def api_demand_gaps():
    """What agents WANTED that we didn't have. Three signals:
      (1) tool names that returned status='error' or 'not_found'
      (2) tools that took >5 paywall-hits in 7d but had 0 conversions
      (3) tools called >100x but always with status NOT IN ('ok','success')
    Brain consumes this as `mcp_demand_gap_unaddressed` finding for the
    top entry. Layer 5 code-proposal can draft new tools from this."""
    out = {
        "unknown_tools_called":  [],
        "zero_conv_high_signal": [],
        "always_failing_tools":  [],
        "summary_top_gaps":      [],
        "computed_at":           datetime.datetime.utcnow().isoformat() + "Z",
    }
    c = _conn()
    if c is None: return jsonify(out), 200
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 1. Tool names returning error/not_found
            try:
                cur.execute("""
                    SELECT tool, COUNT(*) AS n
                      FROM mcp_call_log
                     WHERE timestamp >= NOW() - INTERVAL '14 days'
                       AND tool IS NOT NULL
                       AND status IN ('error','not_found','404','tool_not_found')
                     GROUP BY tool ORDER BY n DESC LIMIT 10
                """)
                out["unknown_tools_called"] = [
                    {"tool": r["tool"], "error_calls_14d": int(r["n"])}
                    for r in cur.fetchall()
                ]
            except Exception:
                pass

            # 2. Zero conv on high signal (already computed in growth)
            try:
                cur.execute("""
                    WITH paid_demand AS (
                      SELECT tool, COUNT(*) AS signals
                        FROM mcp_upgrade_signals
                       WHERE created_at >= NOW() - INTERVAL '7 days'
                         AND tool IS NOT NULL
                       GROUP BY tool HAVING COUNT(*) >= 5
                    ),
                    converted AS (
                      SELECT tool_name AS tool, COUNT(*) AS convs
                        FROM mcp_pair_codes
                       WHERE redeemed_at IS NOT NULL
                         AND redeemed_at >= NOW() - INTERVAL '7 days'
                       GROUP BY tool_name
                    )
                    SELECT p.tool, p.signals, COALESCE(c.convs, 0) AS convs
                      FROM paid_demand p
                      LEFT JOIN converted c USING (tool)
                     WHERE COALESCE(c.convs, 0) = 0
                     ORDER BY p.signals DESC LIMIT 10
                """)
                out["zero_conv_high_signal"] = [
                    {"tool": r["tool"], "signals_7d": int(r["signals"]),
                     "conversions_7d": int(r["convs"])}
                    for r in cur.fetchall()
                ]
            except Exception:
                pass

            # 3. Tools called repeatedly but NEVER succeeding
            try:
                cur.execute("""
                    SELECT tool, COUNT(*) AS total,
                           SUM(CASE WHEN status IN ('ok','success','200') THEN 1 ELSE 0 END) AS ok_n
                      FROM mcp_call_log
                     WHERE timestamp >= NOW() - INTERVAL '14 days'
                       AND tool IS NOT NULL
                     GROUP BY tool HAVING COUNT(*) >= 20
                """)
                rows = cur.fetchall()
                out["always_failing_tools"] = [
                    {"tool": r["tool"], "total_calls_14d": int(r["total"]),
                     "ok_calls_14d": int(r["ok_n"] or 0),
                     "ok_pct": round(100.0 * int(r["ok_n"] or 0) / int(r["total"]), 1)}
                    for r in rows
                    if int(r["ok_n"] or 0) / int(r["total"]) < 0.05
                ]
            except Exception:
                pass

        # Build summary: rank all gaps by impact
        # Simple ranking: signal volume × (1 - convertible rate)
        ranked = []
        for g in out["zero_conv_high_signal"][:5]:
            ranked.append({
                "kind":    "zero_conversion_paid_demand",
                "tool":    g["tool"],
                "metric":  f"{g['signals_7d']} paywall signals in 7d, {g['conversions_7d']} conversions",
                "leverage": g["signals_7d"],
                "suggested_action": "Lower the tier, improve paywall copy, add a Land+Power-style preview, or build the actual flagship that the demand maps to",
            })
        for g in out["unknown_tools_called"][:3]:
            ranked.append({
                "kind":    "tool_name_not_found",
                "tool":    g["tool"],
                "metric":  f"{g['error_calls_14d']} not-found calls in 14d",
                "leverage": g["error_calls_14d"],
                "suggested_action": f"Build the tool agents are reaching for: {g['tool']}",
            })
        for g in out["always_failing_tools"][:3]:
            ranked.append({
                "kind":    "tool_consistently_failing",
                "tool":    g["tool"],
                "metric":  f"{g['total_calls_14d']} calls in 14d, only {g['ok_pct']}% OK",
                "leverage": g["total_calls_14d"],
                "suggested_action": f"Tool {g['tool']} returns errors >95% of the time — fix it or remove it",
            })
        ranked.sort(key=lambda x: -x["leverage"])
        out["summary_top_gaps"] = ranked[:10]
    finally:
        try: c.close()
        except Exception: pass
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


# ── r37 (2026-05-25) ───────────────────────────────────────────────
# /api/v1/mcp/growth/by-platform — WoW delta per platform.
# Answers the L23 audit question "which platform throttled?" The
# /growth endpoint reports a single composite WoW number (-29.2%);
# this endpoint breaks it down so we know whether the dip is one
# platform (e.g. a specific crawler backing off) or systemic.

@mcp_growth_bp.route("/api/v1/mcp/growth/by-platform", methods=["GET"])
def api_growth_by_platform():
    """Per-platform call counts: this week vs. previous week + delta."""
    conn = _conn()
    if not conn:
        return jsonify({"ok": False, "error": "db_unavailable"}), 503
    rows: list[dict] = []
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""WITH this_wk AS (
                        SELECT {_PLATFORM_CASE_TC} AS platform,
                               COUNT(*) AS calls
                          FROM mcp_tool_calls
                         WHERE created_at >= NOW() - INTERVAL '7 days'
                         GROUP BY {_PLATFORM_CASE_TC}
                    ),
                    prev_wk AS (
                        SELECT {_PLATFORM_CASE_TC} AS platform,
                               COUNT(*) AS calls
                          FROM mcp_tool_calls
                         WHERE created_at >= NOW() - INTERVAL '14 days'
                           AND created_at <  NOW() - INTERVAL '7 days'
                         GROUP BY {_PLATFORM_CASE_TC}
                    )
                    SELECT COALESCE(t.platform, p.platform) AS platform,
                           COALESCE(t.calls, 0) AS calls_7d,
                           COALESCE(p.calls, 0) AS calls_prev_7d,
                           COALESCE(t.calls, 0) - COALESCE(p.calls, 0) AS delta
                      FROM this_wk t FULL OUTER JOIN prev_wk p
                        ON t.platform = p.platform
                     ORDER BY ABS(COALESCE(t.calls, 0)
                                  - COALESCE(p.calls, 0)) DESC
                     LIMIT 25"""
            )
            for r in cur.fetchall():
                cur_n = int(r["calls_7d"] or 0)
                prev_n = int(r["calls_prev_7d"] or 0)
                delta = int(r["delta"] or 0)
                pct = (
                    round(100.0 * delta / prev_n, 1) if prev_n > 0
                    else (None if cur_n == 0 else float("inf"))
                )
                rows.append({
                    "platform":      r["platform"] or "unknown",
                    "calls_7d":      cur_n,
                    "calls_prev_7d": prev_n,
                    "delta":         delta,
                    "wow_pct":       pct if pct != float("inf") else "new",
                })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 200

    # Identify the dominant mover (biggest contributor to the WoW
    # composite signal). Brain L23 audit can quote this when proposing.
    biggest_drop = next(
        (r for r in rows if isinstance(r["delta"], int) and r["delta"] < 0),
        None,
    )
    biggest_gain = next(
        (r for r in sorted(rows, key=lambda x: -(x["delta"] or 0))
         if isinstance(r["delta"], int) and r["delta"] > 0),
        None,
    )

    resp = jsonify({
        "ok": True,
        "tool": "getPlatformWoW",
        "platforms": rows,
        "biggest_drop_platform": biggest_drop["platform"] if biggest_drop else None,
        "biggest_drop_delta":    biggest_drop["delta"]    if biggest_drop else None,
        "biggest_gain_platform": biggest_gain["platform"] if biggest_gain else None,
        "biggest_gain_delta":    biggest_gain["delta"]    if biggest_gain else None,
        "window_this_week":      "last 7 days",
        "window_prev_week":      "7-14 days ago",
        "computed_at":           datetime.datetime.utcnow().isoformat() + "Z",
    })
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
