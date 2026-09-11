"""
monetization_master_shell.py — the MONETIZE & RETAIN actuator (2026-07-11).

The three moat pillars are BUILT (provenance envelope, 5-continent telemetry,
agent-state lock-in). This shell runs the missing fourth motion: converting
that moat into revenue + measured retention. It is the funnel counterpart to
depth_master_shell (which deepens data) — this one watches who USES the deep
data and turns heavy free usage into leads, retention into a weekly number,
and the interconnect contract (structured citations) into a guarded canary.

Same house master-shell pattern as depth/grid_data/growth shells: admin-gated,
killable, one bounded action per tick, 0-100 lane scores, weakest lane acts,
snapshot table, fail-soft everywhere.

Three lanes:
  1. metered_billing  — meter GRID+FIBER paid-demand tool usage per identity
        (mcp_call_log: api_key+tier; mcp_calls_identity: agent_id fallback).
        FREE identities over the 7d threshold become rows in
        metered_billing_decisions + mcp_paid_intent (the leads ledger the
        funnel already reads). ★ THIS SHELL NEVER BLOCKS A CALL — enforcement
        is a SEPARATE, default-off switch (MONETIZE_METERED_ENFORCE) that a
        future gate consumes; until armed this is decision+lead generation.
        Deliberately EXCLUDES the free flagship hooks (get_grid_scoreboard,
        get_energy_prices, get_renewable_energy, get_power_pipeline) — those
        are the citation/acquisition funnel and stay free.
  2. retention        — pillar-3 instrumentation: weekly cohort metrics
        (distinct agents, new vs returning, keyed-call share, key-holder
        week-over-week return, portfolio engagement) → retention_weekly.
        Files a brain finding when keyed WoW return regresses >20%.
  3. citation_contract — canary on the interconnect contract: openapi
        x-dchub-envelope discriminator present + a live MCP probe carries the
        machine-readable structuredContent.citation object. Files a finding
        the tick it breaks (agents build branch-before-execute on this).

Endpoints:
  POST /api/v1/admin/monetize/master-tick   — measure → score → act → persist
  GET  /api/v1/admin/monetize/master-state  — latest snapshot + decisions + retention

Kill switches: MONETIZE_MASTER_DISABLED=1 (whole tick),
MONETIZE_MASTER_ACT_DISABLED=1 (shadow: measure+persist, no writes),
MONETIZE_LEVER_<NAME>_OFF=1 (per-lane).
Tunables: MONETIZE_GRID_FIBER_FREE_CALLS_7D (default 100),
MONETIZE_METERED_TOOLS (csv override), MONETIZE_MCP_PROBE_URL.
"""
import os
import json
import hmac
import urllib.request
from datetime import datetime, timezone, timedelta

from flask import Blueprint, jsonify, request

monetization_master_shell_bp = Blueprint("monetization_master_shell", __name__)


# ── auth + kill switches (mirror depth_master_shell) ──────────────────
def _admin_key():
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    expected = (_admin_key() or "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Admin-Key")
           or request.headers.get("X-Internal-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, expected)


def _disabled() -> bool:
    return str(os.environ.get("MONETIZE_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


def _act_disabled() -> bool:
    return str(os.environ.get("MONETIZE_MASTER_ACT_DISABLED", "")).lower() in ("1", "true", "yes")


def _lever_off(name: str) -> bool:
    return str(os.environ.get(f"MONETIZE_LEVER_{name.upper()}_OFF", "")).lower() in ("1", "true", "yes")


# ── DB (same two-conn discipline as depth shell) ──────────────────────
def _conn():
    try:
        from routes.ai_reach import _conn as _raw
        return _raw()
    except Exception:
        return None


def _txn_conn():
    """autocommit=False conn — REQUIRED for upsert_brain_finding (SAVEPOINT-
    wrapped; silently no-ops under autocommit — the documented trap)."""
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        import psycopg2
        c = psycopg2.connect(db, sslmode="require", connect_timeout=8)
        c.autocommit = False
        return c
    except Exception:
        return None


# ── config ────────────────────────────────────────────────────────────
# Paid-demand grid+fiber tools (the brain's metered-billing target set).
# NOT metered: the free flagship hooks — they are the acquisition funnel.
_DEFAULT_METERED_TOOLS = (
    "get_grid_intelligence,get_grid_data,compare_isos,get_interconnection_queue,"
    "get_refined_queue,get_retirement_headroom,grid_transition_radar,"
    "get_fiber_intel,get_metro_fiber,get_fiber_readiness,plan_fiber_leadin"
)
_PAID_TIERS = {"developer", "pro", "starter", "enterprise", "founding", "research_seed"}


def _metered_tools() -> tuple:
    raw = os.environ.get("MONETIZE_METERED_TOOLS", _DEFAULT_METERED_TOOLS)
    return tuple(t.strip() for t in raw.split(",") if t.strip())


def _threshold() -> int:
    try:
        return int(os.environ.get("MONETIZE_GRID_FIBER_FREE_CALLS_7D", "100"))
    except Exception:
        return 100


# ── tables ────────────────────────────────────────────────────────────
def _ensure_tables() -> bool:
    # Raw-conn DDL (db_utils get_db() silently SKIPs DDL — same as paid_intent).
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS metered_billing_decisions (
                    id            BIGSERIAL PRIMARY KEY,
                    decided_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    id_kind       TEXT NOT NULL,      -- 'api_key' | 'agent_id'
                    identity      TEXT NOT NULL,      -- key value/hash or agent_id
                    tier          TEXT,
                    window_days   INTEGER,
                    metered_calls INTEGER,
                    threshold     INTEGER,
                    decision      TEXT NOT NULL,      -- 'over_threshold_free'
                    enforced      BOOLEAN NOT NULL DEFAULT FALSE,
                    detail        JSONB,
                    UNIQUE (id_kind, identity)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS retention_weekly (
                    id                   BIGSERIAL PRIMARY KEY,
                    computed_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    week_start           DATE NOT NULL UNIQUE,
                    agents_7d            INTEGER,
                    new_agents_7d        INTEGER,
                    returning_agents_7d  INTEGER,
                    keyed_identities_7d  INTEGER,
                    keyed_returning_7d   INTEGER,
                    keyed_call_share_pct NUMERIC(5,2),
                    saved_sites_total    INTEGER,
                    alerts_armed_total   INTEGER,
                    portfolio_calls_7d   INTEGER,
                    detail               JSONB
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS monetize_snapshots (
                    id            SERIAL PRIMARY KEY,
                    computed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    monetize_score NUMERIC(6,2),
                    weakest_lever TEXT,
                    action_taken  TEXT,
                    lever_scores  JSONB,
                    findings_filed INTEGER,
                    detail        JSONB
                )
            """)
        return True
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


# ═══════════════════════════════════════════════════════════════════════
# LANE 1 — metered_billing (measure heavy free grid/fiber usage → leads)
# ═══════════════════════════════════════════════════════════════════════
def _metered_usage(days: int = 7) -> dict:
    """Per-identity metered-tool call counts over the window.
    Keyed callers (mcp_call_log.api_key) take precedence; anonymous heavy
    agents are counted by mcp_calls_identity.agent_id (NEVER session_id)."""
    tools = _metered_tools()
    out = {"keyed": [], "anon": [], "error": None}
    c = _conn()
    if c is None:
        out["error"] = "no db"
        return out
    try:
        with c.cursor() as cur:
            cur.execute(
                """SELECT api_key, COALESCE(NULLIF(tier,''),'free') AS tier, COUNT(*)
                   FROM mcp_call_log
                   WHERE "timestamp" > NOW() - (%s || ' days')::interval
                     AND tool = ANY(%s) AND api_key IS NOT NULL AND api_key <> ''
                   GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 200""",
                (days, list(tools)))
            out["keyed"] = [{"identity": k, "tier": t, "calls": int(n)} for k, t, n in cur.fetchall()]
            cur.execute(
                """SELECT agent_id, COUNT(*)
                   FROM mcp_calls_identity
                   WHERE created_at > NOW() - (%s || ' days')::interval
                     AND tool_name = ANY(%s) AND is_real_external
                     AND agent_id IS NOT NULL AND agent_id <> ''
                   GROUP BY 1 ORDER BY 2 DESC LIMIT 200""",
                (days, list(tools)))
            out["anon"] = [{"identity": a, "tier": "anon", "calls": int(n)} for a, n in cur.fetchall()]
    except Exception as e:
        out["error"] = str(e)[:200]
    finally:
        try: c.close()
        except Exception: pass
    return out


def _over_threshold_free(usage: dict) -> list:
    th = _threshold()
    rows = []
    for r in usage.get("keyed", []):
        if r["calls"] >= th and (r["tier"] or "free").lower() not in _PAID_TIERS:
            rows.append({**r, "id_kind": "api_key"})
    for r in usage.get("anon", []):
        if r["calls"] >= th:
            rows.append({**r, "id_kind": "agent_id"})
    return rows


def _act_metered(over: list) -> str:
    """Bounded action: upsert decisions + file NEW ones to the paid-intent
    ledger (cap 10/tick). Never blocks anything — decision + lead only."""
    if not over:
        return "metered: 0 over-threshold identities"
    filed = 0
    c = _conn()
    if c is None:
        return "metered: no db"
    try:
        with c.cursor() as cur:
            for r in over[:25]:
                cur.execute(
                    """INSERT INTO metered_billing_decisions
                       (id_kind, identity, tier, window_days, metered_calls,
                        threshold, decision, detail)
                       VALUES (%s,%s,%s,7,%s,%s,'over_threshold_free',%s)
                       ON CONFLICT (id_kind, identity) DO UPDATE SET
                         decided_at = NOW(), tier = EXCLUDED.tier,
                         metered_calls = EXCLUDED.metered_calls,
                         threshold = EXCLUDED.threshold
                       RETURNING (xmax = 0)""",
                    (r["id_kind"], r["identity"], r.get("tier"),
                     r["calls"], _threshold(),
                     json.dumps({"tools": "grid_fiber_paid_demand"})))
                is_new = bool(cur.fetchone()[0])
                if is_new and filed < 10:
                    # lead into the existing paid-intent ledger (funnel reads it)
                    cur.execute(
                        """INSERT INTO mcp_paid_intent
                           (tool, tier, api_key_hash, raw_args, user_agent)
                           VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        ("metered_grid_fiber_over_threshold", r.get("tier"),
                         (r["identity"][:64] if r["id_kind"] == "api_key" else None),
                         json.dumps({"id_kind": r["id_kind"], "identity": r["identity"][:64],
                                     "calls_7d": r["calls"], "threshold": _threshold()}),
                         "monetize_master_shell"))
                    filed += 1
    except Exception as e:
        return f"metered: error {str(e)[:120]}"
    finally:
        try: c.close()
        except Exception: pass
    return f"metered: {len(over)} over-threshold, {filed} new leads filed"


# ═══════════════════════════════════════════════════════════════════════
# LANE 2 — retention (pillar-3 instrumentation: the weekly truth)
# ═══════════════════════════════════════════════════════════════════════
def _measure_retention() -> dict:
    m = {"error": None}
    c = _conn()
    if c is None:
        return {"error": "no db"}
    try:
        with c.cursor() as cur:
            cur.execute("""
                WITH active AS (
                  SELECT DISTINCT agent_id FROM mcp_calls_identity
                  WHERE created_at > NOW() - interval '7 days' AND is_real_external
                    AND agent_id IS NOT NULL AND agent_id <> ''),
                firsts AS (
                  SELECT agent_id, MIN(created_at) AS f FROM mcp_calls_identity
                  WHERE is_real_external GROUP BY agent_id)
                SELECT COUNT(*) FILTER (WHERE TRUE),
                       COUNT(*) FILTER (WHERE f.f >= NOW() - interval '7 days'),
                       COUNT(*) FILTER (WHERE f.f <  NOW() - interval '7 days')
                FROM active a JOIN firsts f USING (agent_id)""")
            total, new, ret = cur.fetchone()
            m.update(agents_7d=int(total or 0), new_agents_7d=int(new or 0),
                     returning_agents_7d=int(ret or 0))
            cur.execute("""
                WITH wk AS (
                  SELECT DISTINCT api_key FROM mcp_call_log
                  WHERE "timestamp" > NOW() - interval '7 days'
                    AND api_key IS NOT NULL AND api_key <> '')
                SELECT (SELECT COUNT(*) FROM wk),
                       (SELECT COUNT(*) FROM wk WHERE EXISTS (
                          SELECT 1 FROM mcp_call_log p WHERE p.api_key = wk.api_key
                            AND p."timestamp" <= NOW() - interval '7 days' LIMIT 1))""")
            kn, kr = cur.fetchone()
            m.update(keyed_identities_7d=int(kn or 0), keyed_returning_7d=int(kr or 0))
            cur.execute("""
                SELECT
                  ROUND(100.0 * COUNT(*) FILTER (WHERE api_key IS NOT NULL AND api_key <> '')
                        / GREATEST(COUNT(*), 1), 2)
                FROM mcp_call_log WHERE "timestamp" > NOW() - interval '7 days'""")
            m["keyed_call_share_pct"] = float(cur.fetchone()[0] or 0)
            for key, sql in (
                ("saved_sites_total", "SELECT COUNT(*) FROM agent_shortlist_sites"),
                ("alerts_armed_total", "SELECT COUNT(*) FROM saved_lp_alerts"),
            ):
                try:
                    cur.execute(sql)
                    m[key] = int(cur.fetchone()[0] or 0)
                except Exception:
                    m[key] = None
            cur.execute("""
                SELECT COUNT(*) FROM mcp_call_log
                WHERE "timestamp" > NOW() - interval '7 days'
                  AND tool IN ('get_changes','list_saved_sites','get_shortlist',
                               'save_site','save_to_shortlist')""")
            m["portfolio_calls_7d"] = int(cur.fetchone()[0] or 0)
    except Exception as e:
        m["error"] = str(e)[:200]
    finally:
        try: c.close()
        except Exception: pass
    return m


def _act_retention(m: dict) -> str:
    """Bounded action: upsert this week's snapshot; file a finding on a >20%
    WoW regression in keyed returns (the pillar-3 health metric)."""
    if m.get("error"):
        return f"retention: measure error {m['error']}"
    week_start = (datetime.now(timezone.utc) - timedelta(
        days=datetime.now(timezone.utc).weekday())).date()
    prev = None
    c = _conn()
    if c is None:
        return "retention: no db"
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT keyed_returning_7d FROM retention_weekly
                           WHERE week_start < %s ORDER BY week_start DESC LIMIT 1""",
                        (week_start,))
            row = cur.fetchone()
            prev = int(row[0]) if row and row[0] is not None else None
            cur.execute(
                """INSERT INTO retention_weekly
                   (week_start, agents_7d, new_agents_7d, returning_agents_7d,
                    keyed_identities_7d, keyed_returning_7d, keyed_call_share_pct,
                    saved_sites_total, alerts_armed_total, portfolio_calls_7d, detail)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (week_start) DO UPDATE SET
                     computed_at = NOW(),
                     agents_7d = EXCLUDED.agents_7d,
                     new_agents_7d = EXCLUDED.new_agents_7d,
                     returning_agents_7d = EXCLUDED.returning_agents_7d,
                     keyed_identities_7d = EXCLUDED.keyed_identities_7d,
                     keyed_returning_7d = EXCLUDED.keyed_returning_7d,
                     keyed_call_share_pct = EXCLUDED.keyed_call_share_pct,
                     saved_sites_total = EXCLUDED.saved_sites_total,
                     alerts_armed_total = EXCLUDED.alerts_armed_total,
                     portfolio_calls_7d = EXCLUDED.portfolio_calls_7d""",
                (week_start, m.get("agents_7d"), m.get("new_agents_7d"),
                 m.get("returning_agents_7d"), m.get("keyed_identities_7d"),
                 m.get("keyed_returning_7d"), m.get("keyed_call_share_pct"),
                 m.get("saved_sites_total"), m.get("alerts_armed_total"),
                 m.get("portfolio_calls_7d"), json.dumps({})))
    except Exception as e:
        return f"retention: persist error {str(e)[:120]}"
    finally:
        try: c.close()
        except Exception: pass
    cur_ret = m.get("keyed_returning_7d") or 0
    if prev and prev > 0 and cur_ret < prev * 0.8:
        _file_finding(
            "retention_keyed_wow_regression",
            f"Keyed weekly returns dropped {prev} -> {cur_ret} (>20% WoW). Pillar-3 "
            f"(portfolio deltas / personalized next_session) is the lever — check "
            f"retention_weekly + /my-agent engagement before shipping new acquisition.")
        return f"retention: snapshot upserted (wk {week_start}); REGRESSION finding filed ({prev}->{cur_ret})"
    return f"retention: snapshot upserted (wk {week_start}); keyed returns {cur_ret} (prev {prev})"


# ═══════════════════════════════════════════════════════════════════════
# LANE 3 — citation_contract (interconnect canary: structured citations)
# ═══════════════════════════════════════════════════════════════════════
def _probe_citation_contract() -> dict:
    out = {"envelope_discriminator": False, "mcp_citation_object": False, "error": None}
    base = os.environ.get("RAILWAY_BACKEND_URL") or "dchub-backend-production.up.railway.app"
    if not base.startswith("http"):
        base = "https://" + base
    try:
        with urllib.request.urlopen(f"{base}/openapi.json", timeout=12) as r:
            info = (json.loads(r.read()).get("info") or {})
            out["envelope_discriminator"] = bool(info.get("x-dchub-envelope"))
    except Exception as e:
        out["error"] = f"openapi: {str(e)[:100]}"
    mcp = os.environ.get("MONETIZE_MCP_PROBE_URL",
                         "https://dchub-mcp-server-production-4d2e.up.railway.app/mcp")
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "why_dchub", "arguments": {}}}).encode()
        req = urllib.request.Request(mcp, data=body, headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "replace")
        payload = raw
        for line in raw.splitlines():          # SSE frame → data: {...}
            if line.startswith("data: "):
                payload = line[6:]
                break
        sc = (json.loads(payload).get("result") or {}).get("structuredContent") or {}
        cit = sc.get("citation")
        out["mcp_citation_object"] = bool(isinstance(cit, dict) and cit.get("source")
                                          and cit.get("license"))
    except Exception as e:
        out["error"] = ((out["error"] + "; ") if out["error"] else "") + f"mcp: {str(e)[:100]}"
    return out


def _act_citation(probe: dict) -> str:
    broken = [k for k in ("envelope_discriminator", "mcp_citation_object") if not probe.get(k)]
    if not broken:
        return "citation: contract intact (envelope + structured citation object)"
    _file_finding(
        "citation_contract_broken",
        f"Interconnect contract canary FAILED: {', '.join(broken)} missing "
        f"(probe error: {probe.get('error')}). Agents branch-before-execute on "
        f"x-dchub-envelope + structuredContent.citation — restore before anything else.")
    return f"citation: BROKEN ({', '.join(broken)}) — finding filed"


# ── findings (txn conn — savepoint trap) ──────────────────────────────
def _file_finding(issue: str, detail: str) -> bool:
    try:
        from routes.brain_findings_writer import upsert_brain_finding
    except Exception:
        return False
    conn = _txn_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            upsert_brain_finding(cur, issue=issue,
                                 url="/api/v1/admin/monetize/master-tick",
                                 count=1, detail=detail, detector="monetize_master")
        conn.commit()
        return True
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return False
    finally:
        try: conn.close()
        except Exception: pass


# ── MEASURE → SCORE → ACT → PERSIST ───────────────────────────────────
def _scores(usage, over, retention, probe, decisions_fresh_days) -> dict:
    s = {}
    # metered: fully instrumented = decisions fresh + no unfiled over-threshold
    if usage.get("error"):
        s["metered_billing"] = 0.0
    else:
        s["metered_billing"] = max(10.0, 100.0
                                   - 12.0 * len(over)
                                   - (25.0 if decisions_fresh_days is None
                                      or decisions_fresh_days > 8 else 0.0))
    # retention: measured this week + keyed return health
    if retention.get("error"):
        s["retention"] = 0.0
    else:
        kn = retention.get("keyed_identities_7d") or 0
        kr = retention.get("keyed_returning_7d") or 0
        s["retention"] = round(min(100.0, 40.0 + (60.0 * kr / kn if kn else 0.0)), 2)
    s["citation_contract"] = (100.0 if probe.get("envelope_discriminator")
                              and probe.get("mcp_citation_object")
                              else 50.0 if probe.get("envelope_discriminator")
                              or probe.get("mcp_citation_object") else 0.0)
    return s


@monetization_master_shell_bp.route("/api/v1/admin/monetize/master-tick", methods=["POST"])
def monetize_master_tick():
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    if _disabled():
        return jsonify({"ok": True, "skipped": "MONETIZE_MASTER_DISABLED"}), 200
    _ensure_tables()

    usage = _metered_usage()
    over = _over_threshold_free(usage)
    retention = _measure_retention()
    probe = _probe_citation_contract()

    fresh_days = None
    c = _conn()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() - MAX(decided_at)))/86400 "
                            "FROM metered_billing_decisions")
                row = cur.fetchone()
                fresh_days = float(row[0]) if row and row[0] is not None else None
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass

    scores = _scores(usage, over, retention, probe, fresh_days)
    weakest = min(scores, key=scores.get)

    action = "shadow (MONETIZE_MASTER_ACT_DISABLED)"
    if not _act_disabled():
        if _lever_off(weakest):
            action = f"{weakest}: lever OFF"
        elif weakest == "metered_billing":
            action = _act_metered(over)
        elif weakest == "retention":
            action = _act_retention(retention)
        else:
            action = _act_citation(probe)

    total = round(sum(scores.values()) / len(scores), 2)
    detail = {"over_threshold": len(over), "threshold": _threshold(),
              "retention": {k: v for k, v in retention.items() if k != "error"},
              "citation_probe": probe,
              "enforce_armed": os.environ.get("MONETIZE_METERED_ENFORCE", "0")}
    c = _conn()
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute(
                    """INSERT INTO monetize_snapshots
                       (monetize_score, weakest_lever, action_taken, lever_scores, findings_filed, detail)
                       VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (total, weakest, action[:500], json.dumps(scores), 0, json.dumps(detail)))
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass

    return jsonify({"ok": True, "monetize_score": total, "lever_scores": scores,
                    "weakest": weakest, "action": action, "detail": detail})


@monetization_master_shell_bp.route("/api/v1/admin/monetize/master-state", methods=["GET"])
def monetize_master_state():
    if not _admin_ok():
        return jsonify({"error": "unauthorized"}), 401
    out = {"snapshots": [], "decisions": [], "retention_weeks": []}
    c = _conn()
    if c is None:
        return jsonify({"error": "no db"}), 503
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT computed_at, monetize_score, weakest_lever, action_taken, lever_scores
                           FROM monetize_snapshots ORDER BY computed_at DESC LIMIT 10""")
            out["snapshots"] = [{"at": str(a), "score": float(s or 0), "weakest": w,
                                 "action": act, "scores": ls}
                                for a, s, w, act, ls in cur.fetchall()]
            cur.execute("""SELECT decided_at, id_kind, LEFT(identity, 20), tier, metered_calls,
                                  threshold, enforced
                           FROM metered_billing_decisions ORDER BY decided_at DESC LIMIT 25""")
            out["decisions"] = [{"at": str(a), "id_kind": k, "identity": i + "…", "tier": t,
                                 "calls_7d": n, "threshold": th, "enforced": e}
                                for a, k, i, t, n, th, e in cur.fetchall()]
            cur.execute("""SELECT week_start, agents_7d, new_agents_7d, returning_agents_7d,
                                  keyed_identities_7d, keyed_returning_7d, keyed_call_share_pct,
                                  saved_sites_total, alerts_armed_total, portfolio_calls_7d
                           FROM retention_weekly ORDER BY week_start DESC LIMIT 8""")
            cols = ("week_start", "agents_7d", "new_agents_7d", "returning_agents_7d",
                    "keyed_identities_7d", "keyed_returning_7d", "keyed_call_share_pct",
                    "saved_sites_total", "alerts_armed_total", "portfolio_calls_7d")
            out["retention_weeks"] = [dict(zip(cols, (str(r[0]),) + tuple(
                float(x) if hasattr(x, "quantize") else x for x in r[1:]))) for r in cur.fetchall()]
    except Exception as e:
        out["error"] = str(e)[:200]
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(out)
