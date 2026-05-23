"""Phase LLL (2026-05-16) — enterprise bot identification + outreach signal.

The MCP funnel shows 19,826 calls under "unknown" platform across 43
unique IPs. Some of those IPs are SERIOUS enterprise prospects bot-
testing our API. We have ZERO outreach process.

This module:
  GET /api/v1/bots/whales       — IPs with >100 calls/day for 3+ consecutive days
                                   (likely enterprise / serious eval, not casual)
  GET /api/v1/bots/recent       — fresh bots in the last 7d

For each whale we surface:
  - ip_hash (privacy-safe identifier — never raw IP in response body)
  - user_agent fingerprint
  - day-count + total calls
  - tools most-called (signal of intent)
  - geo (CF cf-ipcountry header if present)
  - suggested outreach action: "block / convert / monitor"

Brain detector check_enterprise_bot_identified fires on the top whale
so it shows up in the heartbeat. Autopilot can't auto-onboard them
but flagging them gets a human on the trail.
"""

from __future__ import annotations

import os
import datetime
from flask import Blueprint, jsonify, request
import psycopg2
import psycopg2.extras


bot_outreach_bp = Blueprint("bot_outreach", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _compute_whales(min_days: int = 3, min_calls_per_day: int = 100) -> list[dict]:
    """Find IPs that hit us >100x/day for 3+ days. These aren't casual.

    Phase ZZZZZ-round6c (2026-05-23): exclude our own Railway egress
    range (AS400940, 162.220.232.0/24 + 162.220.233.0/24). The brain
    radar + dchub-selfheal + healer all probe public surfaces from
    Railway infra and were generating 22,677 calls / 14d, getting
    surfaced as the #1 whale to outreach. That was the platform
    flagging itself as an enterprise prospect — useless signal that
    crowded out real external whales below.

    Phase ZZZZZ-round14 (2026-05-23): the 22,677-call whale moved IP
    (Railway rotates egress, or this is a sibling Railway worker on a
    different /24) but kept the same bare 'node' UA. Real enterprise
    Node.js clients identify with 'node-fetch/x.y', 'axios/x.y',
    'undici', or product names — bare 'node' is what Node.js sets
    when no UA override is given, which is internal/scripted traffic.
    Filter by UA in addition to IP. Same defense-in-depth as the
    rate_limiter does for incoming requests."""
    c = _conn()
    if c is None: return []
    out = []
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Group calls by ip_address + day, then identify whales.
            # Exclude Railway internal egress IPs.
            # ALSO exclude rows whose UA is bare/generic internal-looking:
            # 'node', 'node-fetch' bare, 'python-requests' bare, etc.
            # We compute majority-UA per IP via a subquery and reject IPs
            # whose top UA is in the blocklist.
            cur.execute("""
                WITH daily AS (
                  SELECT ip_address, DATE(created_at) AS day,
                         COUNT(*) AS calls
                    FROM mcp_tool_calls
                   WHERE created_at >= NOW() - INTERVAL '14 days'
                     AND ip_address IS NOT NULL
                     AND ip_address != ''
                     AND ip_address NOT LIKE '162.220.232.%%'
                     AND ip_address NOT LIKE '162.220.233.%%'
                     AND ip_address != '127.0.0.1'
                   GROUP BY ip_address, DATE(created_at)
                  HAVING COUNT(*) >= %s
                ),
                whales AS (
                  SELECT ip_address,
                         COUNT(DISTINCT day) AS days_active,
                         SUM(calls) AS total_calls
                    FROM daily GROUP BY ip_address
                  HAVING COUNT(DISTINCT day) >= %s
                ),
                top_ua AS (
                  SELECT DISTINCT ON (ip_address)
                         ip_address, LOWER(COALESCE(user_agent, '')) AS ua
                    FROM mcp_tool_calls
                   WHERE created_at >= NOW() - INTERVAL '14 days'
                     AND ip_address IN (SELECT ip_address FROM whales)
                   ORDER BY ip_address, created_at DESC
                )
                SELECT w.*
                  FROM whales w
                  LEFT JOIN top_ua t ON t.ip_address = w.ip_address
                 WHERE COALESCE(t.ua, '') NOT IN (
                         'node', 'node-fetch', 'python-requests',
                         'python-urllib', 'urllib', 'python', ''
                       )
                 ORDER BY w.total_calls DESC LIMIT 20
            """, (min_calls_per_day, min_days))
            whales = cur.fetchall()

            # Enrich each whale with top tools + last seen + UA fingerprint
            for w in whales:
                ip = w["ip_address"]
                cur.execute("""
                    SELECT tool_name, COUNT(*) AS n
                      FROM mcp_tool_calls
                     WHERE ip_address = %s
                       AND created_at >= NOW() - INTERVAL '14 days'
                     GROUP BY tool_name ORDER BY n DESC LIMIT 5
                """, (ip,))
                top_tools = [{"tool": r["tool_name"], "calls": int(r["n"])} for r in cur.fetchall()]
                cur.execute("""
                    SELECT user_agent, MAX(created_at) AS last_seen
                      FROM mcp_tool_calls
                     WHERE ip_address = %s
                       AND created_at >= NOW() - INTERVAL '14 days'
                     GROUP BY user_agent ORDER BY MAX(created_at) DESC LIMIT 1
                """, (ip,))
                ua_row = cur.fetchone()
                ua = (ua_row.get("user_agent") if ua_row else "") or ""
                last_seen = ua_row.get("last_seen") if ua_row else None

                # Phase ZZZZZ-round19 (2026-05-23): IPinfo enrichment.
                # Replaces the brittle bare-UA filter (round 14) with a
                # real signal — IPinfo's company.type field marks IPs as
                # 'hosting' (datacenter — AWS/GCP/Azure/etc.) vs
                # 'business' / 'isp' / 'education' (real users).
                #
                # Round 19b: free IPinfo tier returns type=null for the
                # vast majority of IPs (Business plan upgrade gives
                # company.type). Use _is_hosting_ip() helper which checks
                # type AND falls back to ASN/hostname patterns — covers
                # AWS/GCP/Azure/DigitalOcean/Linode/Hetzner/OVH etc.
                # without needing the paid plan.
                enrich = {}
                try:
                    from routes.visitor_intelligence import _ipinfo_enrich
                    enrich = _ipinfo_enrich(ip)
                except Exception:
                    enrich = {}
                ip_company = enrich.get("company") or enrich.get("org") or ""
                ip_type = (enrich.get("type") or "").lower()
                is_hosting = False
                try:
                    from routes.brain_security_detectors import _is_hosting_ip
                    is_hosting = _is_hosting_ip(enrich)
                except Exception:
                    is_hosting = (ip_type == "hosting")

                # Suggest outreach action — IPinfo type now drives this.
                action = "monitor"
                ua_low = ua.lower()
                if is_hosting:
                    # Datacenter IPs are almost always bots/scrapers.
                    # Even high volume here is NOT enterprise signal.
                    action = "block_or_throttle"
                elif any(k in ua_low for k in ("scan", "spider", "crawler", "wget", "curl/")):
                    action = "block_or_throttle"
                elif ip_type == "business" and w["total_calls"] > 1000:
                    # Real company IP + high volume = enterprise lead.
                    action = "high_value_outreach"
                elif w["total_calls"] > 5000:
                    action = "high_value_outreach"
                elif w["days_active"] >= 7:
                    action = "outreach"             # sustained interest

                # Last 4 chars of ip_hash for safe display (not raw IP)
                import hashlib
                ip_h = hashlib.sha256(ip.encode()).hexdigest()[:12]

                out.append({
                    "ip_hash":       ip_h,
                    "ua_fingerprint":  ua[:80],
                    "days_active":    int(w["days_active"]),
                    "total_calls_14d": int(w["total_calls"]),
                    "calls_per_day_avg": round(int(w["total_calls"]) / max(1, int(w["days_active"])), 1),
                    "top_tools":      top_tools,
                    "last_seen":      last_seen.isoformat() if last_seen else None,
                    "suggested_action": action,
                    # round19: ipinfo-derived enterprise context
                    "ip_type":         ip_type or None,
                    "ip_company":      ip_company or None,
                    "ip_country":      enrich.get("country") or None,
                    "ip_city":         enrich.get("city") or None,
                })
    finally:
        try: c.close()
        except Exception: pass
    return out


# Phase ZZZZZ-round14 (2026-05-23): admin-only debug — return raw IPs
# of top whales so we can identify mystery bots like the bare-"node"
# 22,677-call client. NOT for general consumption — auth-gated.
@bot_outreach_bp.route("/api/v1/bots/whales/debug", methods=["GET"])
def whales_debug():
    from flask import jsonify, request, make_response
    provided = (request.headers.get("X-Admin-Key")
                or request.headers.get("X-Internal-Key")
                or request.args.get("admin_key"))
    try:
        from internal_auth import is_valid_internal_key
        if not is_valid_internal_key(provided):
            resp = make_response(jsonify(ok=False, error="unauthorized"), 401)
            resp.headers["Cache-Control"] = "no-store, max-age=0"
            return resp
    except Exception:
        return jsonify(ok=False, error="auth_module_unavailable"), 500
    c = _conn()
    if c is None: return jsonify(ok=False, error="no_db"), 500
    rows = []
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # All-IPs, all-UAs view BYPASSING the round14 UA filter so we
            # can see what the filter would otherwise hide.
            cur.execute("""
                WITH daily AS (
                  SELECT ip_address, DATE(created_at) AS day,
                         COUNT(*) AS calls,
                         MAX(user_agent) AS sample_ua
                    FROM mcp_tool_calls
                   WHERE created_at >= NOW() - INTERVAL '14 days'
                     AND ip_address IS NOT NULL
                     AND ip_address != ''
                   GROUP BY ip_address, DATE(created_at)
                ),
                whales AS (
                  SELECT ip_address,
                         COUNT(DISTINCT day) AS days_active,
                         SUM(calls) AS total_calls,
                         MAX(sample_ua) AS sample_ua
                    FROM daily GROUP BY ip_address
                )
                SELECT * FROM whales ORDER BY total_calls DESC LIMIT 15
            """)
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        try: c.close()
        except Exception: pass
    resp = jsonify(ok=True, count=len(rows), top_15_by_calls=rows,
                   note="admin-only — raw IPs returned for triage. "
                        "Verify with WHOIS, then add to filter if internal.")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@bot_outreach_bp.route("/api/v1/bots/whales", methods=["GET"])
def whales():
    """Phase DDDD (2026-05-16): now PRO-gated. The whale list IS the
    enterprise sales pipeline — high-volume bots have direct revenue
    implications. PRO subscribers get the full ranked list; everyone
    else gets a 402 with preview (count + suggested action only)."""
    from routes.tier_gate import _resolve_caller_tier, _gate_response
    tier, _ = _resolve_caller_tier()
    if (tier or "FREE").upper() not in ("PRO", "ENTERPRISE"):
        # Preview: count + top action only, no UA/ip_hash
        try:
            sample = _compute_whales()
            preview = {
                "whale_count":     len(sample),
                "value_proposition": ("PRO subscribers get the full ranked "
                                       "list of high-volume bots: ip_hash, "
                                       "UA fingerprint, calls/day, suggested "
                                       "outreach action. These are likely "
                                       "enterprise prospects worth direct "
                                       "outreach. Sample size hidden on free."),
            }
        except Exception:
            preview = {}
        return _gate_response(tier, "PRO", "bots_whales", preview)
    out = _compute_whales()
    resp = jsonify(
        whales=out,
        count=len(out),
        criteria={"min_days": 3, "min_calls_per_day": 100, "window_days": 14},
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
        note="ip_hash is sha256(ip):12 — privacy-safe stable identifier. "
              "Raw IPs never returned. Match on (ip_hash + ua_fingerprint) "
              "to dedupe across calls.",
    )
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


# ── Phase AAAA (2026-05-16) — dormant-MCP detector ─────────────────
def _compute_dormant(min_prior_calls: int = 10, idle_days: int = 14,
                       look_back_days: int = 90) -> list[dict]:
    """Find agent fingerprints that called us actively in the past
    (>= min_prior_calls historical calls within look_back_days) but
    have NOT called in the past idle_days. These are the prospect-
    waste list — agents that discovered us, hammered for a while,
    then went silent. The user reported /ai-integrations showing
    "90+ inactive MCP connections" — this surfaces the same set as
    a brain finding + a structured outreach worklist.

    No raw IPs ever returned (sha256:12 hash only) — privacy-safe
    and stable across calls so a human can dedupe by ip_hash."""
    c = _conn()
    if c is None: return []
    out: list[dict] = []
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute(f"""
                    WITH agg AS (
                      SELECT ip_address, user_agent,
                             COUNT(*) AS prior_calls,
                             MAX(created_at) AS last_call,
                             MIN(created_at) AS first_call,
                             COUNT(DISTINCT tool_name) AS distinct_tools
                        FROM mcp_tool_calls
                       WHERE created_at >= NOW() - INTERVAL '{int(look_back_days)} days'
                         AND ip_address IS NOT NULL
                       GROUP BY ip_address, user_agent
                    )
                    SELECT *
                      FROM agg
                     WHERE prior_calls >= %s
                       AND last_call < NOW() - INTERVAL '{int(idle_days)} days'
                     ORDER BY prior_calls DESC
                     LIMIT 50
                """, (min_prior_calls,))
                rows = cur.fetchall()
            except Exception:
                return out
    finally:
        try: c.close()
        except Exception: pass

    import hashlib
    now = datetime.datetime.now(datetime.timezone.utc)
    for r in rows:
        ip = (r.get("ip_address") or "")
        ip_h = hashlib.sha256(ip.encode()).hexdigest()[:12] if ip else "?"
        last = r.get("last_call")
        days_idle = None
        if last:
            if last.tzinfo is None:
                last = last.replace(tzinfo=datetime.timezone.utc)
            days_idle = round((now - last).total_seconds() / 86400.0, 1)
        out.append({
            "ip_hash":         ip_h,
            "ua_fingerprint":  (r.get("user_agent") or "")[:80],
            "prior_calls":     int(r.get("prior_calls") or 0),
            "distinct_tools":  int(r.get("distinct_tools") or 0),
            "days_idle":       days_idle,
            "last_call_at":    last.isoformat() if last else None,
            "first_call_at":   r["first_call"].isoformat() if r.get("first_call") else None,
            "suggested_action":(
                "high_priority_winback" if (r.get("prior_calls") or 0) >= 100
                else "soft_winback"     if (r.get("prior_calls") or 0) >= 30
                else "monitor"
            ),
        })
    return out


@bot_outreach_bp.route("/api/v1/bots/dormant", methods=["GET"])
def dormant():
    """Phase AAAA: agents that used to call us but have gone silent.
    The prospect-waste list — gives DC Hub Media a structured outreach
    target instead of generic 'reach out to AI platforms'.

    Phase DDDD (2026-05-16): now PRO-gated. Dormant agent list = direct
    sales pipeline. Free shows only the count + value prop."""
    from routes.tier_gate import _resolve_caller_tier, _gate_response
    tier, _ = _resolve_caller_tier()
    if (tier or "FREE").upper() not in ("PRO", "ENTERPRISE"):
        try:
            sample = _compute_dormant(min_prior_calls=30, idle_days=14)
            high = [d for d in sample if d.get("suggested_action") == "high_priority_winback"]
            preview = {
                "dormant_count":            len(sample),
                "high_priority_count":      len(high),
                "value_proposition": ("PRO subscribers get the full winback "
                                       "worklist with ip_hash, UA, prior calls, "
                                       "days idle, and suggested action per row. "
                                       "Highest-priority targets (>=100 prior "
                                       "calls) are likely enterprise prospects "
                                       "worth manual outreach."),
            }
        except Exception:
            preview = {}
        return _gate_response(tier, "PRO", "bots_dormant", preview)
    try:
        idle_days = max(7, min(90, int(request.args.get("idle_days") or 14)))
    except (ValueError, TypeError):
        idle_days = 14
    try:
        min_prior = max(1, min(1000, int(request.args.get("min_prior_calls") or 10)))
    except (ValueError, TypeError):
        min_prior = 10
    out = _compute_dormant(min_prior_calls=min_prior, idle_days=idle_days)
    resp = jsonify(
        dormant=out,
        count=len(out),
        criteria={"min_prior_calls": min_prior, "idle_days": idle_days, "look_back_days": 90},
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
        note=("Agents with prior_calls >= min_prior_calls within the last "
              "90 days that have not called in idle_days. ip_hash is "
              "sha256(ip):12 — raw IPs never returned. Use the "
              "suggested_action field to prioritize outreach: "
              "high_priority_winback (>=100 calls), soft_winback (>=30), "
              "monitor (rest)."),
    )
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


@bot_outreach_bp.route("/api/v1/bots/recent", methods=["GET"])
def recent_bots():
    """Bots seen in last 7d, ordered by volume. Less strict than whales."""
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT ip_address, user_agent, COUNT(*) AS calls,
                       COUNT(DISTINCT DATE(created_at)) AS days,
                       MAX(created_at) AS last_seen,
                       COUNT(DISTINCT tool_name) AS distinct_tools
                  FROM mcp_tool_calls
                 WHERE created_at >= NOW() - INTERVAL '7 days'
                   AND ip_address IS NOT NULL
                 GROUP BY ip_address, user_agent
                HAVING COUNT(*) >= 10
                 ORDER BY calls DESC LIMIT 40
            """)
            rows = cur.fetchall()
    finally:
        try: c.close()
        except Exception: pass
    import hashlib
    out = []
    for r in rows:
        ip = r["ip_address"]
        ip_h = hashlib.sha256(ip.encode()).hexdigest()[:12]
        out.append({
            "ip_hash":         ip_h,
            "ua":               (r.get("user_agent") or "")[:80],
            "calls_7d":         int(r["calls"]),
            "days_seen":        int(r["days"]),
            "distinct_tools":   int(r["distinct_tools"]),
            "last_seen":        r["last_seen"].isoformat() if r.get("last_seen") else None,
        })
    return jsonify(bots=out, count=len(out)), 200
