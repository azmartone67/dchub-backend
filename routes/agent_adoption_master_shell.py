"""agent_adoption_master_shell.py — the AI-agent adoption funnel conductor (2026-07-23).

ONE conductor for the question the 7-agent iteration program kept guessing at:
"are the iterations turning into REAL agent tool-use, or just reach?" It measures
the honest funnel end-to-end and names the next-best action per platform lane, so
we stop conflating crawler REACH with real MCP EXECUTION.

THE FUNNEL (per platform + global):
  reach        AI-platform crawler / citation hits (ai_cumulative.requests_7d)
  real_calls   de-looped real external tool calls (mcp_calls_identity,
               is_real_external — CF-POP + probe + internal excluded)
  real_agents  distinct real agent_ids
  planner_first % of tool-using sessions whose FIRST call is execute_plan — the
               behavioural signal the agents themselves said is the real test.
               ★ Was plan_query until 2026-07-28. execute_plan replaced it as the
               front door and this metric was never rewired, so it scored agents
               that had correctly migrated as NOT planner-first — a stale metric
               driving the lane classifier below toward the opposite of the fix.
  conversions  real paid conversions (mcp_conversions, non-test)

THE LANES (per platform → state + next action + owner):
  CONNECTOR_GAP  high reach, ~0 real calls — the connector isn't mounted in real
                 user environments. THE binding lever. Owner: BD/distribution
                 (directory/store submission, connector publish).
  ATTRIBUTION    real calls, but landing in the generic 'mcp'/null bucket, not the
                 platform column. Owner: dev (X-MCP-Platform header mapping).
  FIRST_TOUCH    real calls, but planner-first ~0 — agents skip execute_plan. Owner:
                 dev (lead with execute_plan in instructions/tool-desc/llms.txt).
  HEALTHY        real agents + reasonable planner adoption.

DIAGNOSTIC + read-only: measures and NAMES the actuator per lane; it fires nothing
itself (submissions/attribution changes run via their own gated paths). The output
is the OPERATOR funnel + digest — a human decides the distribution moves.

Endpoints (admin-keyed):
  GET  /api/v1/admin/agent-adoption/state       full funnel + per-platform lanes
  POST /api/v1/admin/agent-adoption/master-tick  same, persists a trend snapshot
  POST /api/v1/admin/agent-adoption/digest       ?send=true emails the operator
"""
from __future__ import annotations

import datetime
import html as _html
import os
import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger("agent_adoption_master_shell")
agent_adoption_master_shell_bp = Blueprint("agent_adoption_master_shell", __name__)

# Platforms we actually iterate with / want real tool-use from. Maps the display
# name to the tokens each data source tags it with (they differ slightly). Generic
# buckets (internal, direct, mcp, null) are deliberately excluded — they are NOT a
# platform and lump anonymous callers together.
PLATFORMS = [
    ("Claude / Anthropic",  ("claude", "anthropicapi", "anthropic")),
    ("ChatGPT / OpenAI",    ("chatgpt", "openai")),
    ("Gemini / Google",     ("gemini", "gemini-enterprise", "google")),
    ("Grok / xAI",          ("grok", "xai")),
    ("Microsoft Copilot",   ("copilot",)),
    ("Meta AI",             ("meta",)),
    ("Mistral / Le Chat",   ("mistral",)),
    ("Perplexity",          ("perplexity",)),
    ("DeepSeek / Qwen",     ("deepseek", "qwen", "zai")),
    ("Cursor / Cline / Windsurf", ("cursor", "cline", "windsurf", "continue")),
    # Distribution CHANNEL, not an AI vendor: the MCPMarketHub-hosted connector
    # self-tags via DCHUB_SOURCE_PLATFORM=mcpmarket (mcp-server #173) so its
    # traffic surfaces as its own row instead of the unattributed 'mcp' bucket.
    ("MCPMarketHub",        ("mcpmarket",)),
]

REACH_FLOOR = 500        # 7d reach above which we EXPECT some real calls
POWER_CALLS = 200        # 7d real calls = a genuinely engaged platform


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        c.autocommit = True
        return c
    except Exception:
        return None


def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or
                os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or request.headers.get("Authorization", "").replace("Bearer ", "").strip())
    return bool(expected) and provided == expected


def _disabled() -> bool:
    """Kill switch (r-shell-killswitch 2026-07-27). Every shell must have one:
    without it there is nothing to pull when a shell misbehaves in prod, and
    this was the last shell with routes and no switch (Shell #37 lane 5)."""
    return (os.environ.get("AGENT_ADOPTION_SHELL_DISABLE") or "").strip() == "1"


def _measure():
    """Pull the honest funnel. Fail-soft — a missing source degrades to zeros for
    that field, never an exception."""
    c = _conn()
    out = {"platforms": [], "planner_first_pct": None, "planner_first_sessions": 0,
           "tool_sessions": 0, "real_calls_7d": 0, "real_agents_7d": 0,
           "reach_7d": 0, "conversions_30d": 0, "generic_mcp_calls_7d": 0}
    if c is None:
        return out
    try:
        with c.cursor() as cur:
            # reach per platform (ai_cumulative) — crawler/citation, NOT tool-use
            reach = {}
            try:
                cur.execute("SELECT lower(platform), COALESCE(requests_7d,0) FROM ai_cumulative")
                for p, r in cur.fetchall():
                    reach[(p or "").strip()] = int(r or 0)
            except Exception as e:
                logger.warning("[aa] reach: %s", str(e)[:120])

            # real tool-use per platform (de-looped) — the canonical basis:
            # is_public_ip AND is_real_external (QA sweep 2026-09-02, F6:
            # this query lacked is_public_ip, so it counted a population no
            # other surface publishes).
            real = {}
            try:
                cur.execute("""
                    SELECT lower(platform) AS p,
                           COUNT(*) AS calls,
                           COUNT(DISTINCT agent_id) AS agents
                      FROM mcp_calls_identity
                     WHERE is_public_ip AND is_real_external IS TRUE
                       AND created_at > NOW() - INTERVAL '7 days'
                     GROUP BY 1
                """)
                for p, calls, agents in cur.fetchall():
                    real[(p or "").strip()] = (int(calls or 0), int(agents or 0))
            except Exception as e:
                logger.warning("[aa] real: %s", str(e)[:120])

            all_tokens = []
            for disp, tokens in PLATFORMS:
                rch = sum(reach.get(t, 0) for t in tokens)
                rc = sum(real.get(t, (0, 0))[0] for t in tokens)
                ag = sum(real.get(t, (0, 0))[1] for t in tokens)
                out["platforms"].append({"platform": disp, "tokens": list(tokens),
                                         "reach_7d": rch, "real_calls_7d": rc,
                                         "real_agents_7d": ag})
                out["reach_7d"] += rch
                out["real_calls_7d"] += rc
                out["real_agents_7d_platform_sum"] = (
                    out.get("real_agents_7d_platform_sum", 0) + ag)
                all_tokens.extend(tokens)

            # ★ F6: DISTINCT agents ONCE over the union of every named token.
            # Summing per-platform distinct counts double-counts an agent seen
            # under two tags (claude + anthropic, cursor + cline) — the sum is
            # kept beside it as real_agents_7d_platform_sum so the old series
            # is still readable, but real_agents_7d is the honest headcount.
            try:
                cur.execute("""
                    SELECT COUNT(DISTINCT agent_id)
                      FROM mcp_calls_identity
                     WHERE is_public_ip AND is_real_external IS TRUE
                       AND created_at > NOW() - INTERVAL '7 days'
                       AND lower(platform) = ANY(%s)
                """, (list(all_tokens),))
                out["real_agents_7d"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception as e:
                logger.warning("[aa] distinct agents: %s", str(e)[:120])
                out["real_agents_7d"] = int(out.get("real_agents_7d_platform_sum") or 0)
                out["real_agents_7d_fallback"] = "platform_sum"

            # generic (unattributable) real calls — the 'mcp'/null bucket
            try:
                cur.execute("""
                    SELECT COUNT(*) FROM mcp_calls_identity
                     WHERE is_real_external IS TRUE
                       AND created_at > NOW() - INTERVAL '7 days'
                       AND (platform IS NULL OR lower(platform) IN ('mcp',''))
                """)
                out["generic_mcp_calls_7d"] = int(cur.fetchone()[0] or 0)
            except Exception:
                pass

            # planner-first adoption (sessionized, 14d) — the behavioural test
            try:
                cur.execute("""
                    WITH first_call AS (
                      SELECT DISTINCT ON (session_id) session_id, tool
                        FROM mcp_call_log
                       WHERE timestamp > NOW() - INTERVAL '14 days'
                         AND session_id IS NOT NULL AND session_id <> ''
                       ORDER BY session_id, timestamp ASC
                    )
                    SELECT COUNT(*),
                           COUNT(*) FILTER (WHERE tool='execute_plan'),
                           COUNT(*) FILTER (WHERE tool='plan_query')
                      FROM first_call
                """)
                s, pf, legacy = cur.fetchone()
                out["tool_sessions"] = int(s or 0)
                out["planner_first_sessions"] = int(pf or 0)
                out["planner_first_pct"] = round(100.0 * (pf or 0) / max(int(s or 0), 1), 3)
                # plan_query-first is now itself a STALE-DOOR signal, not planner
                # adoption: that agent is running on pre-execute_plan instructions.
                out["legacy_door_first_sessions"] = int(legacy or 0)
                out["legacy_door_first_pct"] = round(100.0 * (legacy or 0) / max(int(s or 0), 1), 3)
                out["planner_first_caveat"] = (
                    "session-scoped: session_id rotates per MCP connection (~1.2 calls each), "
                    "so this is closer to 'share of calls' than 'share of agents'. "
                    "/api/v1/admin/planner-bypass measures the same behaviour per agent-day.")
            except Exception as e:
                logger.warning("[aa] planner: %s", str(e)[:120])

            # real conversions (30d, non-test)
            try:
                cur.execute("""SELECT COUNT(*) FROM mcp_conversions
                               WHERE is_test IS NOT TRUE
                                 AND created_at > NOW() - INTERVAL '30 days'""")
                out["conversions_30d"] = int(cur.fetchone()[0] or 0)
            except Exception:
                pass
    finally:
        try: c.close()
        except Exception: pass
    return out


# Platforms whose reach is AI-crawler / citation traffic with NO consumer MCP
# connector surface to submit to. Verified 2026-08: Meta AI exposes an ADS MCP
# (mcp.facebook.com/ads) that agents CONSUME — Meta is an MCP provider, not a
# consumer of external tools, so there is no door to "publish a connector" to.
# For these, high-reach + 0-calls is STRUCTURAL; the lever is GEO/citation, not
# distribution — routing them to BD as a connector gap aims effort at a door
# that does not exist.
GEO_ONLY_PLATFORMS = {"Meta AI"}


def _classify(pf: dict, planner_first_pct):
    """→ (lane, action, owner, priority 0-3). Honest: most platforms have reach
    but no real tool-use. Usually that is the connector-adoption gap (BD, not
    positioning) — EXCEPT crawler-only platforms (GEO_ONLY_PLATFORMS) with no
    connector surface, where the lever is GEO/citation instead."""
    reach, calls = pf["reach_7d"], pf["real_calls_7d"]
    if reach >= REACH_FLOOR and calls == 0:
        if pf.get("platform") in GEO_ONLY_PLATFORMS:
            return ("GEO_ONLY",
                    "High crawler/citation reach, but NO consumer MCP connector "
                    "surface exists here — 'publish a connector' is a door that "
                    "doesn't exist. The lever is GEO: stay the machine-readable, "
                    "authoritative CITED source (llms.txt/full, schema.org, the "
                    "integration page). Measure citation share, not tool calls.",
                    "GEO/content", 3)
        return ("CONNECTOR_GAP",
                "High reach, ZERO real tool calls — the connector isn't mounted in "
                "real user environments. The binding lever: publish/submit the "
                "connector door (directory/store) so reach converts to execution.",
                "BD/distribution", 3)
    if calls == 0:
        return ("DORMANT", "No reach and no calls — no live surface yet; needs a "
                "connector path or a landing/integration page.", "BD/dev", 2)
    # has real calls
    if planner_first_pct is not None and planner_first_pct < 1.0:
        return ("FIRST_TOUCH",
                "Real calls landing, but planner-first ~0% — agents skip execute_plan. "
                "Lead with execute_plan in server instructions, tool descriptions, "
                "llms.txt and worked examples to shape cold-agent first-touch.",
                "dev", 2)
    if calls >= POWER_CALLS:
        return ("HEALTHY", "Engaged — real agents using tools. Nurture depth + "
                "planner-first.", "—", 0)
    return ("ACTIVATING", "Some real calls — early. Watch reach→call conversion + "
            "planner adoption.", "dev", 1)


def _route(lanes):
    """Auto-route: the single loud TOP MOVE + an owner-grouped action queue.
    Routable = any lane that names a real action (not HEALTHY, owner set). Lanes
    arrive pre-sorted (CONNECTOR_GAP first, then priority, then reach), so
    routable[0] IS the top move — e.g. the highest-reach connector-gap platform."""
    routable = [l for l in lanes
                if l["lane"] != "HEALTHY" and l["owner"] not in ("—", "", None)]
    top = None
    if routable:
        l = routable[0]
        top = {
            "platform": l["platform"], "lane": l["lane"], "owner": l["owner"],
            "reach_7d": l["reach_7d"], "real_calls_7d": l["real_calls_7d"],
            "action": l["action"],
            "why": (f'{l["reach_7d"]:,} reach → {l["real_calls_7d"]} real calls — '
                    f'the biggest reach-without-execution gap. Owner: {l["owner"]}.'),
        }
    by_owner = {}
    for l in routable:
        by_owner.setdefault(l["owner"], []).append({
            "platform": l["platform"], "lane": l["lane"], "priority": l["priority"],
            "reach_7d": l["reach_7d"], "real_calls_7d": l["real_calls_7d"],
            "action": l["action"],
        })
    return top, by_owner


def _funnel(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    m = _measure()
    pfp = m.get("planner_first_pct")
    lanes = []
    for pf in m["platforms"]:
        lane, action, owner, prio = _classify(pf, pfp)
        lanes.append({**pf, "lane": lane, "action": action, "owner": owner,
                      "priority": prio})
    order = {"CONNECTOR_GAP": 0, "GEO_ONLY": 1, "ATTRIBUTION": 2, "FIRST_TOUCH": 3,
             "ACTIVATING": 4, "DORMANT": 5, "HEALTHY": 6}
    lanes.sort(key=lambda x: (order.get(x["lane"], 9), -x["priority"], -x["reach_7d"]))
    top_move, action_queue = _route(lanes)
    # attribution health: what share of real calls are unattributable ('mcp'/null)
    attr_gap = m["generic_mcp_calls_7d"]
    total_real = m["real_calls_7d"] + attr_gap
    return {
        "as_of": now.isoformat(),
        "funnel": {
            "reach_7d": m["reach_7d"],
            "real_calls_7d": m["real_calls_7d"],
            # ★ F6 (2026-09-02): this is calls on NAMED platforms only (the
            # PLATFORMS tokens), on is_public_ip AND is_real_external. It is
            # NOT the funnel's real_external_calls_7d, which also carries the
            # unattributed 'mcp'/null bucket (generic_unattributed_calls_7d).
            "real_calls_7d_label": "calls on NAMED platforms (7d)",
            "real_agents_7d": m["real_agents_7d"],
            "real_agents_7d_basis": ("COUNT(DISTINCT agent_id) ONCE over the union of "
                                     "named-platform tokens, is_public_ip AND "
                                     "is_real_external, 7d"),
            "real_agents_7d_platform_sum": m.get("real_agents_7d_platform_sum"),
            "generic_unattributed_calls_7d": attr_gap,
            "attribution_gap_pct": round(100.0 * attr_gap / max(total_real, 1), 1),
            "planner_first_pct": pfp,
            "planner_first_sessions": m["planner_first_sessions"],
            "tool_sessions_14d": m["tool_sessions"],
            "conversions_30d": m["conversions_30d"],
        },
        "lanes": lanes,
        "top_move": top_move,
        "action_queue": action_queue,
        "headline": _headline(m, lanes),
        "note": ("REACH != real tool-use. Most platforms sit in CONNECTOR_GAP: high "
                 "crawler/citation reach, ~0 real MCP execution. That gap is closed by "
                 "DISTRIBUTION (connector publish + directory/store submission), not "
                 "more positioning. planner_first_pct is the behavioural test the "
                 "agents themselves named — track it as first-touch changes ship."),
    }


def _headline(m, lanes):
    gap = sum(1 for l in lanes if l["lane"] == "CONNECTOR_GAP")
    return (f"{m['real_agents_7d']} real agents · {m['real_calls_7d']} calls on NAMED platforms/7d "
            f"vs {m['reach_7d']:,} reach · planner-first {m.get('planner_first_pct')}% · "
            f"{gap} platforms in CONNECTOR_GAP (reach, no execution)")


def _persist_snapshot(f):
    """Append a trend row so we can see whether iterations move the funnel. Uses a
    tiny self-created table (fail-soft)."""
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_adoption_snapshots (
                    id BIGSERIAL PRIMARY KEY,
                    snapshot_at TIMESTAMPTZ DEFAULT NOW(),
                    reach_7d INTEGER, real_calls_7d INTEGER, real_agents_7d INTEGER,
                    planner_first_pct NUMERIC, attribution_gap_pct NUMERIC,
                    conversions_30d INTEGER, connector_gap_platforms INTEGER
                )
            """)
            fn = f["funnel"]
            gap = sum(1 for l in f["lanes"] if l["lane"] == "CONNECTOR_GAP")
            cur.execute("""
                INSERT INTO agent_adoption_snapshots
                  (reach_7d, real_calls_7d, real_agents_7d, planner_first_pct,
                   attribution_gap_pct, conversions_30d, connector_gap_platforms)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (fn["reach_7d"], fn["real_calls_7d"], fn["real_agents_7d"],
                  fn["planner_first_pct"], fn["attribution_gap_pct"],
                  fn["conversions_30d"], gap))
        return True
    except Exception as e:
        logger.warning("[aa] snapshot: %s", str(e)[:120])
        return False
    finally:
        try: c.close()
        except Exception: pass


@agent_adoption_master_shell_bp.route("/api/v1/admin/agent-adoption/state")
def aa_state():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    return jsonify(ok=True, **_funnel()), 200


@agent_adoption_master_shell_bp.route("/api/v1/admin/agent-adoption/master-tick",
                                      methods=["POST"])
def aa_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    f = _funnel()
    persisted = _persist_snapshot(f)
    return jsonify(ok=True, snapshot_persisted=persisted, **f), 200


def _digest_html(f):
    def esc(s): return _html.escape(str(s or ""))
    fn = f["funnel"]
    icon = {"CONNECTOR_GAP": "🔌", "GEO_ONLY": "📣", "ATTRIBUTION": "🏷️", "FIRST_TOUCH": "🧭",
            "ACTIVATING": "🔵", "DORMANT": "⚫", "HEALTHY": "🟢"}
    rows = "".join(
        f'<tr><td style="padding:6px 10px;border-bottom:1px solid #eee">'
        f'{icon.get(l["lane"],"")} <b>{esc(l["platform"])}</b> '
        f'<span style="color:#888;font-size:12px">— {esc(l["lane"])} · owner {esc(l["owner"])}</span>'
        f'<br><span style="color:#555;font-size:12px">reach {l["reach_7d"]:,} · '
        f'real calls {l["real_calls_7d"]} · agents {l["real_agents_7d"]}</span>'
        f'<br><span style="color:#334;font-size:13px">{esc(l["action"])}</span></td></tr>'
        for l in f["lanes"])
    tm = f.get("top_move")
    top_banner = (
        f'<div style="background:#fff7ed;border:1px solid #fdba74;border-radius:6px;'
        f'padding:12px 16px;margin-bottom:14px">'
        f'<div style="font-weight:700;color:#9a3412">🚀 TOP MOVE — {esc(tm["platform"])} '
        f'<span style="font-weight:400;color:#7c2d12">· {esc(tm["lane"])} · owner {esc(tm["owner"])}</span></div>'
        f'<div style="color:#7c2d12;font-size:13px;margin-top:4px">{esc(tm["why"])}</div>'
        f'<div style="color:#334;font-size:13px;margin-top:4px">{esc(tm["action"])}</div></div>'
    ) if tm else ""
    aq = f.get("action_queue") or {}
    queue_blocks = "".join(
        '<div style="margin-top:8px"><b style="color:#0f172a">&rarr; ' + esc(owner) + '</b>'
        '<ul style="margin:4px 0 0;padding-left:18px;color:#334;font-size:12px">'
        + "".join(
            f'<li style="margin:3px 0"><b>{esc(it["platform"])}</b> '
            f'<span style="color:#888">({esc(it["lane"])}, reach {it["reach_7d"]:,})</span></li>'
            for it in items[:5])
        + '</ul></div>'
        for owner, items in aq.items())
    queue_html = (
        '<div style="margin-top:14px"><div style="font-size:11px;color:#64748b;'
        'text-transform:uppercase;letter-spacing:.05em;margin-bottom:2px">Routed queue &middot; by owner</div>'
        + queue_blocks + '</div>') if aq else ""
    return f"""<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f1f5f9;padding:24px">
<div style="max-width:680px;margin:0 auto">
<div style="background:#0f172a;color:#fff;padding:16px 22px;border-radius:8px 8px 0 0">
<strong>DC Hub — AI-agent adoption funnel</strong>
<div style="color:#94a3b8;font-size:13px">{esc(f["headline"])}</div></div>
<div style="background:#f8fafc;padding:14px 22px 22px;border:1px solid #e2e8f0;border-top:0;border-radius:0 0 8px 8px">
{top_banner}
<p style="font-size:13px;color:#334">Real: <b>{fn["real_agents_7d"]} agents / {fn["real_calls_7d"]} calls</b> per 7d ·
Reach: {fn["reach_7d"]:,} · Planner-first: <b>{fn["planner_first_pct"]}%</b> ·
Attribution gap: {fn["attribution_gap_pct"]}% land as generic 'mcp' · Conversions 30d: {fn["conversions_30d"]}</p>
<table cellpadding=0 cellspacing=0 width="100%" style="border-collapse:collapse;background:#fff;border:1px solid #e2e8f0;border-radius:6px">{rows}</table>
{queue_html}
<p style="color:#64748b;font-size:12px;margin-top:16px">Reach ≠ execution. CONNECTOR_GAP platforms need DISTRIBUTION (connector publish + directory/store), not more positioning. Track planner_first_pct as first-touch changes ship.</p>
</div></div></div>"""


@agent_adoption_master_shell_bp.route("/api/v1/admin/agent-adoption/digest",
                                      methods=["POST", "GET"])
def aa_digest():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    send = request.args.get("send", "").lower() in ("1", "true", "yes")
    f = _funnel()
    _persist_snapshot(f)
    recipients = [e.strip() for e in
                  (os.environ.get("DCHUB_BRIEFING_EMAIL")
                   or os.environ.get("BRAIN_DIGEST_EMAIL")
                   or "jonathan@dchub.cloud").split(",") if e.strip()]
    html_body = _digest_html(f)
    if not send:
        return jsonify(ok=True, dry_run=True, funnel=f["funnel"],
                       recipients=recipients, html_bytes=len(html_body)), 200
    sent = failed = 0
    try:
        import requests as _rq
        subj = (f"DC Hub agent adoption: {f['funnel']['real_agents_7d']} real agents, "
                f"{f['funnel']['real_calls_7d']} calls on NAMED platforms/7d, "
                f"planner-first {f['funnel']['planner_first_pct']}%")
        for em in recipients:
            try:
                r = _rq.post("https://api.resend.com/emails", timeout=15,
                             json={"from": os.environ.get("DCHUB_FROM_EMAIL",
                                                          "DC Hub <jonathan@dchub.cloud>"),
                                   "to": [em], "subject": subj, "html": html_body},
                             headers={"Authorization":
                                      f"Bearer {os.environ.get('RESEND_API_KEY','').strip()}"})
                sent += 1 if 200 <= r.status_code < 300 else 0
                failed += 0 if 200 <= r.status_code < 300 else 1
            except Exception:
                failed += 1
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:160]), 500
    return jsonify(ok=True, sent=sent, failed=failed), 200
