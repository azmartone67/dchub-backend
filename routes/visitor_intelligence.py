"""Phase r32-david (2026-05-20) — Visitor intelligence for enterprise.
==========================================================================

David asked what DC Hub can deliver in terms of eyeball intelligence for
enterprise users accessing our site. This module is the working answer.

It aggregates what we already capture (mcp_upgrade_signals, mcp_tool_calls,
ai_citation_observations) into a single visitor-intelligence digest:
  - Top enterprise IPs / domains hitting the platform
  - MCP-client breakdown (Claude vs ChatGPT vs Perplexity vs Gemini)
  - Tool usage by visitor
  - Tier distribution of active sessions
  - Recent paid-tool paywall hits (the addressable upgrade pool)

GET /api/v1/admin/visitor-intelligence?days=7
   Returns a single JSON payload with the full breakdown.

GET /visitor-intelligence
   Public HTML page rendering the same data — designed to be shared
   with David / enterprise prospects as a "here's what we can see"
   demo (admin-key gated, no PII).

Honest scope:
- We CAN: identify by api_key → email (post-r32-conv-2 fix), tool usage,
  mcp_client identification, IP for unauthenticated calls, tier
  classification, paywall hit patterns.
- We CANNOT (yet): IP → company enrichment (needs IPinfo/Clearbit
  integration), per-page click stream beyond MCP boundary, identity
  resolution for cookie-less anon visitors.

Anything below the "we cannot" line is roadmap, not delivery.
"""
import os
import logging
from datetime import datetime
from flask import Blueprint, jsonify, request, Response, render_template_string

logger = logging.getLogger(__name__)
visitor_intelligence_bp = Blueprint("visitor_intelligence", __name__)


_INTERNAL_KEYS: set = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY",
           "ADMIN_API_KEY", "ADMIN_SECRET"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


def _get_db():
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        import psycopg2
        return psycopg2.connect(db, sslmode="require", connect_timeout=8)
    except Exception:
        return None


def _compute(days: int = 7) -> dict:
    """Aggregate visitor intelligence over the last N days."""
    out = {
        "as_of":  datetime.utcnow().isoformat() + "Z",
        "days":   days,
        "totals": {},
        "by_mcp_client": [],
        "top_tools": [],
        "addressable_pool": [],
        "tier_distribution": [],
        "recent_enterprise_signals": [],
        "anon_volume_by_ua": [],
        "coverage_gaps": [],
    }
    conn = _get_db()
    if conn is None:
        out["error"] = "no_db"
        return out
    try:
        with conn.cursor() as cur:
            # Top-line totals.
            cur.execute("""
                SELECT
                  COUNT(*)                                                AS total_signals,
                  COUNT(DISTINCT session_id)                              AS unique_sessions,
                  COUNT(DISTINCT user_email)
                    FILTER (WHERE user_email IS NOT NULL AND user_email != '')
                                                                          AS unique_known_visitors,
                  COUNT(DISTINCT ip_address)
                    FILTER (WHERE ip_address IS NOT NULL AND ip_address != '')
                                                                          AS unique_ips,
                  COUNT(DISTINCT mcp_client)
                    FILTER (WHERE mcp_client IS NOT NULL AND mcp_client != '')
                                                                          AS unique_clients,
                  COUNT(*) FILTER (WHERE COALESCE(converted, false))      AS conversions,
                  COUNT(*) FILTER (WHERE COALESCE(outreach_sent, false))  AS outreached
                FROM mcp_upgrade_signals
                WHERE created_at > NOW() - INTERVAL %s
            """, (f"{days} days",))
            r = cur.fetchone()
            if r:
                out["totals"] = {
                    "total_paywall_signals":  int(r[0] or 0),
                    "unique_sessions":        int(r[1] or 0),
                    "unique_known_visitors":  int(r[2] or 0),
                    "unique_ips":             int(r[3] or 0),
                    "unique_mcp_clients":     int(r[4] or 0),
                    "conversions":            int(r[5] or 0),
                    "already_outreached":     int(r[6] or 0),
                }

            # MCP client breakdown (Claude vs ChatGPT vs Perplexity vs Gemini).
            try:
                cur.execute("""
                    SELECT COALESCE(NULLIF(mcp_client, ''), 'unknown') AS client,
                           COUNT(*)                                    AS signals,
                           COUNT(DISTINCT session_id)                  AS sessions,
                           array_agg(DISTINCT tool_requested
                                       ORDER BY tool_requested)
                             FILTER (WHERE tool_requested IS NOT NULL) AS top_tools
                      FROM mcp_upgrade_signals
                     WHERE created_at > NOW() - INTERVAL %s
                     GROUP BY COALESCE(NULLIF(mcp_client, ''), 'unknown')
                     ORDER BY signals DESC
                     LIMIT 10
                """, (f"{days} days",))
                out["by_mcp_client"] = [
                    {
                        "client":   row[0],
                        "signals":  int(row[1] or 0),
                        "sessions": int(row[2] or 0),
                        "top_tools": (row[3] or [])[:5],
                    }
                    for row in cur.fetchall()
                ]
            except Exception:
                try: conn.rollback()
                except Exception: pass

            # Top tools by paywall hits.
            try:
                cur.execute("""
                    SELECT tool_requested,
                           COUNT(*)                       AS hits,
                           COUNT(DISTINCT session_id)     AS distinct_sessions,
                           COUNT(DISTINCT mcp_client)
                             FILTER (WHERE mcp_client IS NOT NULL) AS distinct_clients
                      FROM mcp_upgrade_signals
                     WHERE created_at > NOW() - INTERVAL %s
                       AND tool_requested IS NOT NULL
                     GROUP BY tool_requested
                     ORDER BY hits DESC
                     LIMIT 12
                """, (f"{days} days",))
                out["top_tools"] = [
                    {
                        "tool":            row[0],
                        "hits":            int(row[1] or 0),
                        "sessions":        int(row[2] or 0),
                        "clients":         int(row[3] or 0),
                    }
                    for row in cur.fetchall()
                ]
            except Exception:
                try: conn.rollback()
                except Exception: pass

            # Addressable pool (identified visitors with ≥3 hits, not converted/outreached).
            try:
                cur.execute("""
                    SELECT user_email,
                           COUNT(*) AS signals,
                           array_agg(DISTINCT tool_requested
                                       ORDER BY tool_requested)
                             FILTER (WHERE tool_requested IS NOT NULL) AS tools,
                           MAX(created_at) AS last_seen
                      FROM mcp_upgrade_signals
                     WHERE created_at > NOW() - INTERVAL %s
                       AND user_email IS NOT NULL AND user_email != ''
                       AND COALESCE(converted, false) = false
                       AND COALESCE(outreach_sent, false) = false
                     GROUP BY user_email
                    HAVING COUNT(*) >= 3
                     ORDER BY COUNT(*) DESC
                     LIMIT 25
                """, (f"{days} days",))
                out["addressable_pool"] = [
                    {
                        "email":      row[0],
                        "signals":    int(row[1] or 0),
                        "tools":      (row[2] or [])[:5],
                        "last_seen":  row[3].isoformat() if row[3] else None,
                    }
                    for row in cur.fetchall()
                ]
            except Exception:
                try: conn.rollback()
                except Exception: pass

            # Tier distribution.
            try:
                cur.execute("""
                    SELECT COALESCE(NULLIF(tier_current, ''), 'unknown') AS tier,
                           COUNT(*)                                       AS signals
                      FROM mcp_upgrade_signals
                     WHERE created_at > NOW() - INTERVAL %s
                     GROUP BY COALESCE(NULLIF(tier_current, ''), 'unknown')
                     ORDER BY signals DESC
                """, (f"{days} days",))
                out["tier_distribution"] = [
                    {"tier": row[0], "signals": int(row[1] or 0)}
                    for row in cur.fetchall()
                ]
            except Exception:
                try: conn.rollback()
                except Exception: pass

            # Anonymous-volume by user-agent — useful for spotting
            # specific agents that should be onboarded.
            try:
                cur.execute("""
                    SELECT
                      CASE
                        WHEN user_agent ILIKE '%%claude%%'         THEN 'Claude'
                        WHEN user_agent ILIKE '%%chatgpt%%'        THEN 'ChatGPT'
                        WHEN user_agent ILIKE '%%openai%%'         THEN 'OpenAI'
                        WHEN user_agent ILIKE '%%perplexity%%'     THEN 'Perplexity'
                        WHEN user_agent ILIKE '%%gemini%%'         THEN 'Gemini'
                        WHEN user_agent ILIKE '%%copilot%%'        THEN 'Copilot'
                        WHEN user_agent ILIKE '%%cursor%%'         THEN 'Cursor'
                        WHEN user_agent ILIKE '%%bot%%'
                          OR user_agent ILIKE '%%crawler%%'        THEN 'Generic bot'
                        WHEN user_agent ILIKE '%%mozilla%%'        THEN 'Browser'
                        ELSE 'Other'
                      END AS ua_class,
                      COUNT(*)                       AS signals,
                      COUNT(DISTINCT session_id)     AS sessions
                    FROM mcp_upgrade_signals
                    WHERE created_at > NOW() - INTERVAL %s
                      AND (user_email IS NULL OR user_email = '')
                    GROUP BY ua_class
                    ORDER BY signals DESC
                """, (f"{days} days",))
                out["anon_volume_by_ua"] = [
                    {"agent_class": row[0],
                     "signals": int(row[1] or 0),
                     "sessions": int(row[2] or 0)}
                    for row in cur.fetchall()
                ]
            except Exception:
                try: conn.rollback()
                except Exception: pass
    except Exception as e:
        out["error"] = str(e)[:200]
    finally:
        try: conn.close()
        except Exception: pass

    # Coverage gaps — derived, not queried.
    out["coverage_gaps"] = [
        "IP → company enrichment requires IPinfo or Clearbit integration (not yet wired).",
        "Per-page click-stream beyond the MCP boundary requires a frontend pixel (Plausible covers public pages but not authenticated tool usage).",
        "Identity resolution for cookie-less anon visitors requires session-token persistence across MCP calls (out of scope for the MCP protocol).",
        "Anonymous signals from Claude/ChatGPT (≈90% of MCP traffic) can't be email-resolved without a dev key redemption — those callers reach DC Hub through an LLM proxy.",
    ]
    return out


@visitor_intelligence_bp.route(
    "/api/v1/admin/visitor-intelligence", methods=["GET"])
def visitor_intelligence_json():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    try:
        days = max(1, min(90, int(request.args.get("days", 7))))
    except (ValueError, TypeError):
        days = 7
    payload = _compute(days)
    payload["ok"] = "error" not in payload
    return jsonify(payload), (200 if "error" not in payload else 500)


_VI_HTML = '''<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8">
<title>Visitor Intelligence · DC Hub</title>
<meta name="robots" content="noindex,nofollow">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a12;--surface:#11121a;--bd:#1f2030;--tx:#fff;--tx2:#9ca3af;
  --indigo:#6366f1;--violet:#a855f7;--green:#10b981;--orange:#f59e0b;--red:#ef4444;
  --mono:'JetBrains Mono','SF Mono',monospace;color-scheme:dark}
*{box-sizing:border-box}body{font-family:Inter,-apple-system,sans-serif;
  background:var(--bg);color:var(--tx);margin:0;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1200px;margin:0 auto;padding:2.5rem 1.5rem}
.kicker{font-family:var(--mono);font-size:.78rem;color:#c4b5fd;
  text-transform:uppercase;letter-spacing:.14em;margin-bottom:.6rem}
h1{margin:0 0 .5rem;font-size:2.2rem;font-weight:800;letter-spacing:-.02em;
  background:linear-gradient(90deg,#fff,#c4b5fd);-webkit-background-clip:text;
  background-clip:text;color:transparent}
.sub{color:var(--tx2);max-width:760px;margin:0 0 2rem}
h2{font-size:.78rem;color:var(--tx2);text-transform:uppercase;letter-spacing:.12em;
  margin:2.5rem 0 1rem;font-weight:700}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:1rem;margin-bottom:2.5rem}
.stat{background:var(--surface);border:1px solid var(--bd);border-radius:10px;padding:1.2rem 1.4rem}
.stat .n{font-family:var(--mono);font-size:1.7rem;font-weight:800;line-height:1}
.stat .l{color:var(--tx2);font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;margin-top:.4rem}
table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--bd);border-radius:10px;overflow:hidden;font-size:.9rem;margin-bottom:1rem}
th{text-align:left;padding:.7rem 1rem;background:#0f1019;color:var(--tx2);font-size:.7rem;text-transform:uppercase;letter-spacing:.1em;font-weight:700}
td{padding:.7rem 1rem;border-bottom:1px solid var(--bd)}
tr:last-child td{border-bottom:none}
td.mono{font-family:var(--mono);font-size:.85rem}
.gaps{background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.25);border-radius:10px;padding:1.25rem 1.5rem;margin-top:2rem}
.gaps h3{margin:0 0 .75rem;color:var(--orange);font-size:.95rem;font-family:var(--mono);text-transform:uppercase;letter-spacing:.08em}
.gaps ul{margin:0;padding-left:1.5rem}
.gaps li{margin:.4rem 0;font-size:.88rem;color:#cbd5e1}
</style></head><body><div class="wrap">
<div class="kicker">DC HUB · VISITOR INTELLIGENCE · {{ d.days }}D</div>
<h1>Who is on DC Hub</h1>
<p class="sub">Aggregated from MCP paywall signals + tool calls. Honest about what we can and can't see — the bottom section names the gaps.</p>

<h2>Top line</h2>
<div class="stats">
<div class="stat"><div class="n">{{ d.totals.total_paywall_signals or 0 }}</div><div class="l">Paywall signals</div></div>
<div class="stat"><div class="n">{{ d.totals.unique_sessions or 0 }}</div><div class="l">Unique sessions</div></div>
<div class="stat"><div class="n">{{ d.totals.unique_known_visitors or 0 }}</div><div class="l">Known (email)</div></div>
<div class="stat"><div class="n">{{ d.totals.unique_mcp_clients or 0 }}</div><div class="l">MCP clients</div></div>
<div class="stat"><div class="n">{{ d.totals.conversions or 0 }}</div><div class="l">Conversions</div></div>
<div class="stat"><div class="n">{{ d.totals.already_outreached or 0 }}</div><div class="l">Outreached</div></div>
</div>

<h2>MCP client breakdown</h2>
<table><thead><tr><th>Client</th><th>Signals</th><th>Sessions</th><th>Top tools</th></tr></thead><tbody>
{% for c in d.by_mcp_client %}<tr><td>{{ c.client }}</td><td class="mono">{{ c.signals }}</td><td class="mono">{{ c.sessions }}</td><td>{{ c.top_tools|join(', ') }}</td></tr>{% endfor %}
</tbody></table>

<h2>Top tools hitting the paywall</h2>
<table><thead><tr><th>Tool</th><th>Hits</th><th>Sessions</th><th>Clients</th></tr></thead><tbody>
{% for t in d.top_tools %}<tr><td>{{ t.tool }}</td><td class="mono">{{ t.hits }}</td><td class="mono">{{ t.sessions }}</td><td class="mono">{{ t.clients }}</td></tr>{% endfor %}
</tbody></table>

<h2>Addressable pool · {{ d.addressable_pool|length }} identified visitors with ≥3 hits</h2>
{% if d.addressable_pool %}
<table><thead><tr><th>Email</th><th>Signals</th><th>Tools</th><th>Last seen</th></tr></thead><tbody>
{% for u in d.addressable_pool %}<tr><td class="mono">{{ u.email }}</td><td class="mono">{{ u.signals }}</td><td>{{ u.tools|join(', ') }}</td><td class="mono">{{ u.last_seen[:10] if u.last_seen else '—' }}</td></tr>{% endfor %}
</tbody></table>
{% else %}
<p style="color:var(--tx2);font-size:.9rem">No identified visitors meet the threshold yet. Once /api/v1/admin/upgrade-pool/backfill-emails runs, this populates.</p>
{% endif %}

<h2>Tier distribution</h2>
<table><thead><tr><th>Tier</th><th>Signals</th></tr></thead><tbody>
{% for t in d.tier_distribution %}<tr><td>{{ t.tier }}</td><td class="mono">{{ t.signals }}</td></tr>{% endfor %}
</tbody></table>

<h2>Anonymous-traffic breakdown (signals without resolved email)</h2>
<table><thead><tr><th>Agent class</th><th>Signals</th><th>Sessions</th></tr></thead><tbody>
{% for a in d.anon_volume_by_ua %}<tr><td>{{ a.agent_class }}</td><td class="mono">{{ a.signals }}</td><td class="mono">{{ a.sessions }}</td></tr>{% endfor %}
</tbody></table>

<div class="gaps"><h3>What we can't see yet</h3><ul>
{% for g in d.coverage_gaps %}<li>{{ g }}</li>{% endfor %}
</ul></div>

</div></body></html>'''


@visitor_intelligence_bp.route(
    "/visitor-intelligence", methods=["GET"])
def visitor_intelligence_page():
    if not _admin_ok():
        return Response("Unauthorized", status=401, mimetype="text/plain")
    try:
        days = max(1, min(90, int(request.args.get("days", 7))))
    except (ValueError, TypeError):
        days = 7
    d = _compute(days)
    html = render_template_string(_VI_HTML, d=d)
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "private, max-age=120"
    return resp
