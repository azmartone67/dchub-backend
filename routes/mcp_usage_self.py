"""
mcp_usage_self.py — Phase r67 (2026-05-26).

The MCP funnel showed:
  - 25,073 tool calls in 7d
  - 3,387 upgrade signals in 7d
  - 9 conversions in 30d (~0.06% rate)
  - 114 distinct callers hit get_grid_intelligence (paid) 5,382 times
  - 115 distinct callers hit get_fiber_intel (paid) 4,946 times

That's the addressable conversion pool: ~115 active near-converters.
The current upgrade hint is generic ("$199/mo Pro"); it doesn't tell
the caller "you've called get_grid_intelligence 47 times this month —
$199 unblocks all 47." This module surfaces that data.

Endpoints:

  GET /api/v1/mcp/usage/me
      Public — returns the requesting caller's per-tool call counts
      for the last 30 days, with status-code breakdown so paywall
      hits are visible. Keyed by hash(ip + ua) — same identity used
      by the A/B variant chooser.

  GET /api/v1/mcp/usage/me/tool/<tool>
      Public — call count + 403 count for a specific tool.

  GET /api/v1/admin/funnel/near-converters
      Admin — top 30 callers ranked by paid-tool 403 count in last
      30d. Each entry includes a personalized outreach-draft body the
      operator can paste into email or LinkedIn DM. The 115
      near-converters are your highest-ROI sales targets.

  GET /api/v1/admin/funnel/near-converters/<hash_id>
      Admin — deep-dive on one caller: full per-tool breakdown,
      first/last seen, platform sniff, and the draft pitch.
"""
from __future__ import annotations

import datetime
import hashlib
import os

from flask import Blueprint, jsonify, request


mcp_usage_self_bp = Blueprint("mcp_usage_self", __name__)


# Same paid-tool list paywall_hint_middleware uses for the
# "Pro-only tools" copy. Centralize here so r67-b reads from it.
PAID_TOOLS = {
    "get_grid_intelligence",
    "get_fiber_intel",
    "analyze_site",
    "compare_sites",
    "get_dchub_recommendation",
}


def _db_conn():
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        return psycopg2.connect(url, connect_timeout=5) if url else None
    except Exception:
        return None


def _caller_hash_id() -> tuple[str, str, str]:
    """(ip_hash, ua_hash, combined_hash) — used to key usage rows."""
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "0.0.0.0")
    ua = (request.headers.get("User-Agent") or "")[:200]
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16]
    ua_hash = hashlib.sha256(ua.encode()).hexdigest()[:16]
    combined = hashlib.sha256(f"{ip}|{ua}".encode()).hexdigest()[:16]
    return ip_hash, ua_hash, combined


def _admin_authorized() -> bool:
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "")
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    return bool(expected) and provided == expected


def _fetch_usage_for_caller(ip: str, ua: str, days: int = 30) -> dict:
    """Query mcp_connections for this exact ip + ua over the window.
    Returns the aggregate + per-tool breakdown + 403 counts."""
    c = _db_conn()
    if not c:
        return {"error": "db_unavailable"}
    try:
        with c.cursor() as cur:
            # Per-tool counts + 403 counts in one pass
            cur.execute("""
                SELECT
                    tool_name,
                    COUNT(*)                                    AS total_calls,
                    COUNT(*) FILTER (WHERE status_code = 403)   AS forbidden,
                    COUNT(*) FILTER (WHERE status_code = 429)   AS rate_limited,
                    COUNT(*) FILTER (WHERE status_code IN (200,201)) AS ok,
                    MIN(created_at)                             AS first_seen,
                    MAX(created_at)                             AS last_seen
                FROM mcp_connections
                WHERE ip_address = %s
                  AND user_agent = %s
                  AND created_at > NOW() - (%s || ' days')::interval
                GROUP BY tool_name
                ORDER BY total_calls DESC
                LIMIT 50
            """, (ip, ua, str(days)))
            rows = cur.fetchall() or []
    except Exception as e:
        try: c.close()
        except Exception: pass
        return {"error": str(e)[:200]}
    finally:
        try: c.close()
        except Exception: pass

    by_tool = []
    total = 0
    total_403 = 0
    total_429 = 0
    paid_403 = 0
    paid_403_by_tool: dict[str, int] = {}
    for r in rows:
        tool, calls, forbidden, rate_lim, ok, first, last = r
        total += int(calls or 0)
        total_403 += int(forbidden or 0)
        total_429 += int(rate_lim or 0)
        is_paid = tool in PAID_TOOLS
        if is_paid and forbidden:
            paid_403 += int(forbidden or 0)
            paid_403_by_tool[tool] = int(forbidden or 0)
        by_tool.append({
            "tool":          tool,
            "is_paid":       is_paid,
            "total_calls":   int(calls or 0),
            "ok":            int(ok or 0),
            "forbidden_403": int(forbidden or 0),
            "rate_limit_429": int(rate_lim or 0),
            "first_seen":    first.isoformat() if first else None,
            "last_seen":     last.isoformat() if last else None,
        })

    return {
        "window_days":     days,
        "total_calls":     total,
        "total_403":       total_403,
        "total_429":       total_429,
        "paid_tool_403":   paid_403,
        "paid_403_by_tool": paid_403_by_tool,
        "by_tool":         by_tool,
        "is_near_converter": paid_403 >= 5,
    }


# ── Endpoints ───────────────────────────────────────────────────────

@mcp_usage_self_bp.route("/api/v1/mcp/usage/me", methods=["GET"])
def usage_me():
    """Per-caller usage snapshot. Public (caller's own data only)."""
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "0.0.0.0")
    ua = (request.headers.get("User-Agent") or "")[:200]
    days = int(request.args.get("days") or 30)
    days = max(1, min(days, 90))
    data = _fetch_usage_for_caller(ip, ua, days)
    ip_hash, ua_hash, combined = _caller_hash_id()
    return jsonify({
        "ok":         True,
        "as_of":      datetime.datetime.utcnow().isoformat() + "Z",
        "caller_id":  combined,
        **data,
        "upgrade_url": ("https://dchub.cloud/pricing"
                         if data.get("is_near_converter")
                         else None),
        "note":       ("This endpoint reports YOUR usage only "
                         "(keyed on ip+ua hash, no auth required). "
                         "For account-wide stats add ?days=N "
                         "(default 30, max 90)."),
    }), 200


@mcp_usage_self_bp.route(
    "/api/v1/mcp/usage/me/tool/<tool>", methods=["GET"]
)
def usage_me_for_tool(tool):
    """Single-tool count + 403 count for this caller."""
    ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "0.0.0.0")
    ua = (request.headers.get("User-Agent") or "")[:200]
    c = _db_conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*)                                  AS total_calls,
                    COUNT(*) FILTER (WHERE status_code = 403) AS forbidden,
                    MIN(created_at)                           AS first_seen,
                    MAX(created_at)                           AS last_seen
                FROM mcp_connections
                WHERE ip_address = %s AND user_agent = %s
                  AND tool_name = %s
                  AND created_at > NOW() - INTERVAL '30 days'
            """, (ip, ua, tool))
            r = cur.fetchone() or (0, 0, None, None)
    except Exception as e:
        try: c.close()
        except Exception: pass
        return jsonify({"ok": False, "error": str(e)[:200]}), 200
    finally:
        try: c.close()
        except Exception: pass

    return jsonify({
        "ok":              True,
        "tool":            tool,
        "is_paid_tool":    tool in PAID_TOOLS,
        "total_calls_30d": int(r[0] or 0),
        "forbidden_30d":   int(r[1] or 0),
        "first_seen":      r[2].isoformat() if r[2] else None,
        "last_seen":       r[3].isoformat() if r[3] else None,
        "upgrade_pitch":   (f"You've called {tool} {int(r[0] or 0)} times "
                              f"in the last 30 days, hitting the paywall "
                              f"{int(r[1] or 0)} times. Upgrade at "
                              f"https://dchub.cloud/pricing to unblock all "
                              f"future calls.")
                              if (tool in PAID_TOOLS and int(r[1] or 0) > 0)
                              else None,
    }), 200


# ── Admin: near-converters list ─────────────────────────────────────

def _fetch_near_converters(min_paid_403: int = 5, limit: int = 30, days: int = 30) -> list[dict]:
    """r68-c (2026-05-26): query mcp_upgrade_signals (the right table).
    The MCP funnel dashboard reads from here — that's why my prior
    query against mcp_connections returned 0 (status_code filtering
    isn't how this table records paywall hits). mcp_upgrade_signals
    columns: signal_type, tool_requested, mcp_client, message_shown,
    created_at — caller identity lives in mcp_client (UA-like string).

    A "near-converter" = an mcp_client with ≥min_paid_403 paywall_hit
    signals on Pro-only tools in the last N days.
    """
    c = _db_conn()
    if not c: return []
    paid_tools_sql = "(" + ",".join(f"'{t}'" for t in PAID_TOOLS) + ")"
    try:
        with c.cursor() as cur:
            cur.execute(f"""
                WITH per_caller AS (
                    SELECT
                        mcp_client,
                        COUNT(*) FILTER (WHERE signal_type IN ('paywall_hit','paid_tool_blocked')
                                            AND tool_requested IN {paid_tools_sql})
                            AS paid_403,
                        COUNT(*) FILTER (WHERE tool_requested IN {paid_tools_sql})
                            AS paid_signals,
                        COUNT(*) AS all_signals,
                        MIN(created_at) AS first_seen,
                        MAX(created_at) AS last_seen
                      FROM mcp_upgrade_signals
                     WHERE created_at > NOW() - (%s || ' days')::interval
                       AND mcp_client IS NOT NULL
                       AND mcp_client != ''
                     GROUP BY mcp_client
                )
                SELECT *
                  FROM per_caller
                 WHERE paid_403 >= %s
                 ORDER BY paid_403 DESC, paid_signals DESC
                 LIMIT %s
            """, (str(days), min_paid_403, limit))
            rows = cur.fetchall() or []

            out = []
            for r in rows:
                mcp_client, p403, psigs, allsigs, first, last = r
                # caller_id = hash of mcp_client (UA-like string) so
                # admin can deep-dive via /near-converters/<id>
                combined = hashlib.sha256((mcp_client or "").encode()).hexdigest()[:16]

                # Per-tool paywall-hit breakdown for THIS caller
                cur.execute(f"""
                    SELECT tool_requested, COUNT(*) AS n
                      FROM mcp_upgrade_signals
                     WHERE mcp_client = %s
                       AND signal_type IN ('paywall_hit','paid_tool_blocked')
                       AND tool_requested IN {paid_tools_sql}
                       AND created_at > NOW() - (%s || ' days')::interval
                     GROUP BY tool_requested
                     ORDER BY n DESC
                """, (mcp_client, str(days)))
                paid_403_by_tool = {row[0]: int(row[1] or 0)
                                       for row in (cur.fetchall() or [])}

                # Sniff platform from mcp_client UA
                platform = _sniff_platform(mcp_client or "")

                out.append({
                    "caller_id":         combined,
                    "platform":          platform,
                    "client_name":       (mcp_client or "")[:120],
                    "user_agent":        (mcp_client or "")[:120],
                    "paid_403_count":    int(p403 or 0),
                    "paid_calls":        int(psigs or 0),
                    "all_calls":         int(allsigs or 0),
                    "first_seen":        first.isoformat() if first else None,
                    "last_seen":         last.isoformat() if last else None,
                    "paid_403_by_tool":  paid_403_by_tool,
                })
            return out
    except Exception:
        return []
    finally:
        try: c.close()
        except Exception: pass


def _sniff_platform(ua: str) -> str:
    """Cheap platform classifier from the mcp_client string."""
    ua_l = (ua or "").lower()
    if "claude" in ua_l: return "Claude"
    if "chatgpt" in ua_l or "openai" in ua_l: return "ChatGPT / OpenAI"
    if "gemini" in ua_l or "google-aip" in ua_l: return "Gemini"
    if "perplexity" in ua_l: return "Perplexity"
    if "groq" in ua_l: return "Groq"
    if "cursor" in ua_l: return "Cursor"
    if "cody" in ua_l: return "Cody"
    if "mcp" in ua_l: return "Generic MCP client"
    if "python" in ua_l: return "Python script"
    if "node" in ua_l: return "Node.js client"
    if "curl" in ua_l: return "curl"
    return "Unknown"


def _draft_near_converter_pitch(nc: dict) -> str:
    """Build a personalized email body for one near-converter."""
    p403 = nc["paid_403_count"]
    top_tool = (max(nc["paid_403_by_tool"].items(), key=lambda x: x[1])[0]
                 if nc["paid_403_by_tool"] else "get_grid_intelligence")
    top_n = nc["paid_403_by_tool"].get(top_tool, p403)
    client = nc.get("client_name") or "your AI agent"
    platform = nc.get("platform") or "MCP client"

    # Days active
    try:
        first = datetime.datetime.fromisoformat(
            (nc.get("first_seen") or "").replace("Z","+00:00"))
        last = datetime.datetime.fromisoformat(
            (nc.get("last_seen") or "").replace("Z","+00:00"))
        days_active = max(1, (last - first).days + 1)
    except Exception:
        days_active = "30"

    return f"""Hi —

I noticed {client} (via {platform}) called DC Hub's `{top_tool}` tool
{top_n} times in the last {days_active} days, hitting the paywall on
every one. You're clearly using DC Hub for real work — and the 403s
mean every call is a wasted round-trip for your agent.

A Pro tier ($199/mo) unblocks `{top_tool}` plus the other 3 Pro-only
tools (`get_fiber_intel`, `analyze_site`, `compare_sites`). At your
current cadence ({p403} blocked calls in {days_active} days) that's
$<$4/blocked-call already — and the friction stops the moment you
swap your X-API-Key header.

If $199 is heavier than you need, the $9/mo Starter unlocks all OTHER
tools (10,000 calls/day) and you can dip into Pro just for the four
gated ones — total still under $50/mo for most use cases.

Two paths:
  1. Upgrade in 30 sec: https://dchub.cloud/pricing
  2. Free 30-min Pro trial — reply to this and I'll comp you one.

What's blocking the upgrade?

Best,
Jonathan
Founder, DC Hub
jonathan@dchub.cloud · dchub.cloud/pricing
"""


@mcp_usage_self_bp.route(
    "/api/v1/admin/funnel/near-converters", methods=["GET"]
)
def near_converters():
    """Top 30 callers with ≥5 paid-tool 403s in last 30d, each with
    a personalized outreach-draft body ready to send."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401

    try:
        limit = max(1, min(int(request.args.get("limit") or 30), 100))
        min_p403 = max(1, int(request.args.get("min_paid_403") or 5))
        days = max(1, min(int(request.args.get("days") or 30), 90))
    except Exception:
        limit, min_p403, days = 30, 5, 30

    rows = _fetch_near_converters(min_p403, limit, days)
    # Attach personalized draft to each
    for r in rows:
        r["outreach_draft"] = _draft_near_converter_pitch(r)
        r["outreach_subject"] = (
            f"DC Hub Pro — you've been calling "
            f"{next(iter(r.get('paid_403_by_tool') or {'get_grid_intelligence':0}))} "
            f"a lot lately"
        )

    return jsonify({
        "ok":              True,
        "as_of":           datetime.datetime.utcnow().isoformat() + "Z",
        "window_days":     days,
        "min_paid_403":    min_p403,
        "near_converter_count": len(rows),
        "near_converters": rows,
        "paid_tools_in_scope": sorted(PAID_TOOLS),
        "note":            ("These callers are your highest-ROI conversion "
                              "targets. Each entry includes a personalized "
                              "draft. To track sent: POST /api/v1/admin/funnel/"
                              "near-converters/<caller_id>/sent"),
    }), 200


@mcp_usage_self_bp.route(
    "/api/v1/admin/funnel/near-converters/<caller_id>", methods=["GET"]
)
def near_converter_detail(caller_id):
    """r70-a (2026-05-26): deep-dive on one near-converter.

    Returns:
      - base aggregate (from /near-converters list)
      - FULL mcp_client UA string (not just sniffed platform)
      - all signal_types broken down (paywall_hit / paid_tool_blocked /
        redeem_url_viewed / etc.) so we see the funnel stage
      - tool-call SEQUENCE — what they tried BEFORE the paid tools
      - days_active + signals_per_day rate
      - is_likely_real_user heuristic: tool diversity + temporal spread
      - identity_hints: if their mcp_client UA has an IP / org / hostname
        we can pattern-match to (Claude Desktop, Cursor, etc.)
      - personalized outreach_draft tailored to the platform sniffed
    """
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401

    # We don't store caller_id-indexed rows; re-aggregate then filter
    rows = _fetch_near_converters(min_paid_403=1, limit=200, days=90)
    match = next((r for r in rows if r["caller_id"] == caller_id), None)
    if not match:
        return jsonify({
            "ok":    False,
            "error": "near_converter_not_found",
            "hint":  ("caller_id is first 16 chars of "
                       "sha256(mcp_client). May have rolled off the "
                       "90-day window."),
        }), 404

    # Dig into the full mcp_upgrade_signals history for this UA
    mcp_client_ua = match.get("user_agent") or match.get("client_name") or ""
    deep = _deep_history_for_caller(mcp_client_ua)

    # is_likely_real_user heuristic
    sig_per_day = (deep.get("total_signals", 0) /
                    max(1, deep.get("days_active", 1)))
    tool_diversity = len(deep.get("tools_attempted") or [])
    is_real = (tool_diversity >= 3 and
                deep.get("days_active", 0) >= 2 and
                sig_per_day < 200)  # < 200/day = not a runaway bot
    bot_signal = ""
    if not is_real:
        if tool_diversity < 3:
            bot_signal = "low tool diversity (< 3 distinct tools)"
        elif deep.get("days_active", 0) < 2:
            bot_signal = "single-day burst (no return visits)"
        elif sig_per_day >= 200:
            bot_signal = f"high rate ({sig_per_day:.0f} signals/day) — script-like"

    match["full_ua"]           = mcp_client_ua
    match["signals_by_type"]   = deep.get("signals_by_type") or {}
    match["tools_attempted"]   = deep.get("tools_attempted") or []
    match["tool_sequence"]     = deep.get("tool_sequence") or []
    match["days_active"]       = deep.get("days_active")
    match["signals_per_day"]   = round(sig_per_day, 2)
    match["is_likely_real_user"] = is_real
    match["bot_signal"]        = bot_signal or None
    match["identity_hints"]    = _identity_hints_from_ua(mcp_client_ua)
    match["outreach_draft"]    = _draft_near_converter_pitch(match)
    match["outreach_subject"]  = (
        f"DC Hub Pro — you've been calling "
        f"{next(iter(match.get('paid_403_by_tool') or {'get_grid_intelligence':0}))} "
        f"a lot lately"
    )
    return jsonify({"ok": True, "near_converter": match}), 200


def _deep_history_for_caller(mcp_client: str) -> dict:
    """Pull every signal for this mcp_client UA from the last 90 days."""
    if not mcp_client:
        return {}
    c = _db_conn()
    if not c: return {}
    try:
        with c.cursor() as cur:
            # Signals-by-type
            cur.execute("""
                SELECT signal_type, COUNT(*) AS n
                  FROM mcp_upgrade_signals
                 WHERE mcp_client = %s
                   AND created_at > NOW() - INTERVAL '90 days'
                 GROUP BY signal_type
                 ORDER BY n DESC
            """, (mcp_client,))
            sigs_by_type = {r[0]: int(r[1] or 0) for r in (cur.fetchall() or [])}

            # All tools attempted + counts
            cur.execute("""
                SELECT tool_requested, COUNT(*) AS n
                  FROM mcp_upgrade_signals
                 WHERE mcp_client = %s
                   AND tool_requested IS NOT NULL
                   AND created_at > NOW() - INTERVAL '90 days'
                 GROUP BY tool_requested
                 ORDER BY n DESC
                 LIMIT 30
            """, (mcp_client,))
            tools = [{"tool": r[0], "count": int(r[1] or 0)}
                      for r in (cur.fetchall() or [])]

            # Tool sequence — first 50 calls in chronological order
            cur.execute("""
                SELECT tool_requested, signal_type, created_at
                  FROM mcp_upgrade_signals
                 WHERE mcp_client = %s
                   AND tool_requested IS NOT NULL
                 ORDER BY created_at ASC
                 LIMIT 50
            """, (mcp_client,))
            seq = [{"tool": r[0], "signal_type": r[1],
                     "ts": r[2].isoformat() if r[2] else None}
                    for r in (cur.fetchall() or [])]

            # Days active
            cur.execute("""
                SELECT COUNT(DISTINCT DATE(created_at)) AS days,
                       COUNT(*) AS total
                  FROM mcp_upgrade_signals
                 WHERE mcp_client = %s
                   AND created_at > NOW() - INTERVAL '90 days'
            """, (mcp_client,))
            row = cur.fetchone() or (0, 0)
            days_active = int(row[0] or 0)
            total_signals = int(row[1] or 0)
        return {
            "signals_by_type":  sigs_by_type,
            "tools_attempted":  tools,
            "tool_sequence":    seq,
            "days_active":      days_active,
            "total_signals":    total_signals,
        }
    except Exception:
        return {}
    finally:
        try: c.close()
        except Exception: pass


def _identity_hints_from_ua(ua: str) -> dict:
    """Pattern-match the mcp_client string for org/version/host hints."""
    if not ua: return {}
    ua_low = ua.lower()
    hints: dict = {}
    # Claude Desktop pattern: "claude-desktop/0.7.x"
    import re as _re
    m = _re.search(r"claude[-_]desktop[/\s]([\d.]+)", ua_low)
    if m: hints["claude_desktop_version"] = m.group(1)
    m = _re.search(r"cursor[/\s]([\d.]+)", ua_low)
    if m: hints["cursor_version"] = m.group(1)
    m = _re.search(r"openai[-_]agent[/\s]([\d.]+)", ua_low)
    if m: hints["openai_agent_version"] = m.group(1)
    # Generic MCP SDK
    m = _re.search(r"mcp[-_]sdk[/\s]([\d.]+)", ua_low)
    if m: hints["mcp_sdk_version"] = m.group(1)
    m = _re.search(r"python/([\d.]+)", ua_low)
    if m: hints["python_version"] = m.group(1)
    m = _re.search(r"node\.?js?[/\s]([\d.]+)", ua_low)
    if m: hints["nodejs_version"] = m.group(1)
    # If we have NO platform hints, it's likely a raw script
    if not hints:
        if "python" in ua_low:    hints["likely_platform"] = "Python script (no SDK)"
        elif "node" in ua_low:    hints["likely_platform"] = "Node.js script (no SDK)"
        elif "curl" in ua_low:    hints["likely_platform"] = "curl (manual testing)"
        elif "mcp" in ua_low:     hints["likely_platform"] = "Generic MCP client"
    return hints
