"""
routes/backfunnel_master_shell.py — Back-of-Funnel Truth Master Shell (2026-07-06).

ONE pane for the back-of-funnel work landed 2026-07-06 (see the funnel fix-wave):
retention durable-keys, claim→paid attribution, and the conversion demand leak.
It measures whether those fixes are LANDING, using the CORRECTED metrics the
older panes were blind to — so the operator watches the needle move (or not) with
honest numbers instead of the structurally-wrong ones.

Why a dedicated shell (not just the flywheel pane): the flywheel retention check
reads `auto_trial_keys` ONLY (blind to the dch_live_ claim-key cohort agents are
told to SAVE), and no pane measured the attribution join capability or the honest
relay-vs-web conversion split. This consolidates all three into one back-of-funnel
truth surface.

The three lanes:
  1. RETENTION / DURABLE KEYS — mature key-reuse across BOTH cohorts
     (auto_trial_keys ∪ dch_live_ mcp_dev_keys), the dch_live_ claim-key
     cross-session reuse the r-durable-key fix targets, and real agents returning
     on a 2nd day. Actuator (SHIPPED): forward real caller IP (server.mjs) + 30d
     claim reuse window (DCHUB_CLAIM_REUSE_HOURS).
  2. ATTRIBUTION / claim→paid — the join is now Mcp-Session-Id based (the dead
     sha256(dch_trial_) vs api_key_hash(dch_live_) leg could never match). Checks
     the join's left key exists on recent sessions, and gauges whether paid
     top-ups carry a session id yet (the relay→webhook→topup chain being
     exercised). Actuator (SHIPPED): sid join + relay-link client_reference_id.
  3. CONVERSION DEMAND — the REAL leak: relay converts ~0. Gauges the conversion
     origin split (web/organic vs relay), unlock_more_data engagement, and flags
     relay-attributable conversions. Actuator (NOT shipped): depth-tease /
     unlock_more_data value-moment — a product/offer problem, not plumbing.

★ PURE-DB shell: every probe is a Neon query — NO outbound HTTP (mirrors
flywheel_master_shell; a master-shell tick must never self-request — 2026-07-06
pool-saturation incident). Admin-gated, killable (BACKFUNNEL_DISABLED=1),
fail-soft, snapshot row per tick, 60s cache. READ-ONLY / DIAGNOSTIC: each lane
NAMES an actuator but fires NOTHING.

Endpoints:
  GET/POST /api/v1/admin/backfunnel/master-tick   JSON scoreboard (3 lanes)
  GET      /admin/backfunnel                        HTML dashboard (60s refresh)
  GET      /api/v1/admin/backfunnel                 CF zone-worker bypass alias

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY) — same gate as flywheel_master_shell.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

backfunnel_master_shell_bp = Blueprint("backfunnel_master_shell", __name__)

# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("BACKFUNNEL_DISABLED") or "").strip() == "1"


# ── db helpers (mirror flywheel_master_shell) ─────────────────────────

def _conn():
    """Raw psycopg2 connection OUTSIDE the app pool — one short-lived
    connection per tick, so a tick never checks out a shared pool slot."""
    try:
        import psycopg2 as _pg
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[backfunnel] db connect failed: %s", e)
        return None


def _row(c, sql: str):
    """Fail-soft single-row fetch → tuple, or None on error/empty. Literal SQL
    only (no params tuple — avoids the empty-tuple % trap; none of these queries
    carry a literal %)."""
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()
    except Exception as e:
        logger.debug("[backfunnel] row failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _rows(c, sql: str) -> list:
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    except Exception as e:
        logger.debug("[backfunnel] rows failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return []


def _check(cid: str, name: str, passed, detail: str) -> dict:
    # passed: True / False / None (None = gauge/indeterminate, shown as "?")
    return {"id": cid, "name": name, "pass": passed, "detail": (detail or "")[:300]}


def _num(v, default=None):
    try:
        return float(v)
    except Exception:
        return default


# ── lane 1 · retention / durable keys (corrected) ─────────────────────

def _lane_retention(c) -> list[dict]:
    out = []
    if c is None:
        return [_check("ret_nodb", "retention lane needs db", None, "no db")]

    # 1a — dch_live_ claim-key cross-session reuse. The r-durable-key fix
    # (forward-IP + 30d window) targets THIS cohort — the key agents are told to
    # SAVE. GAUGE: 0 today (fix just shipped), watch it climb over 1-2 weeks.
    r = _row(c,
        "SELECT ROUND(100.0*COUNT(*) FILTER (WHERE created_at < now()-interval '7 days' "
        " AND last_used_at IS NOT NULL "
        " AND date_trunc('week',last_used_at)>date_trunc('week',created_at))"
        " /NULLIF(COUNT(*) FILTER (WHERE created_at < now()-interval '7 days'),0),1),"
        " COUNT(*) FILTER (WHERE created_at < now()-interval '7 days') "
        "FROM mcp_dev_keys WHERE api_key LIKE 'dch_live_%' "
        "AND metadata->>'source'='claim_api' AND created_at >= now()-interval '30 days'")
    if r is None:
        out.append(_check("ret_dchlive_reuse", "dch_live_ claim-key cross-week reuse % (watch climb)",
                          None, "query failed"))
    else:
        pct, mature = r[0], r[1]
        out.append(_check("ret_dchlive_reuse", "dch_live_ claim-key cross-week reuse % (watch climb)",
                          None, f"{pct}% of {mature} mature keys — r-durable-key shipped 07-06; expect 1-2wk lag"))

    # 1b — HONEST mature key-reuse across BOTH cohorts (auto_trial_keys ∪
    # dch_live_ mcp_dev_keys). The flywheel pane reads auto_trial ONLY and is
    # blind to the dch_live_ cohort. Regression floor 8%. FAIL below.
    r = _row(c,
        "WITH k AS ("
        " SELECT minted_at t0,last_used_at t1 FROM auto_trial_keys WHERE minted_at>=now()-interval '30 days' "
        " UNION ALL "
        " SELECT created_at,last_used_at FROM mcp_dev_keys WHERE api_key LIKE 'dch_live_%' AND created_at>=now()-interval '30 days') "
        "SELECT ROUND(100.0*COUNT(*) FILTER (WHERE t0<now()-interval '7 days' AND t1 IS NOT NULL "
        " AND date_trunc('week',t1)>date_trunc('week',t0))/NULLIF(COUNT(*) FILTER (WHERE t0<now()-interval '7 days'),0),1),"
        " COUNT(*) FILTER (WHERE t0<now()-interval '7 days') FROM k")
    # r-honest-gates (2026-07-31): a gauge that documents its own contamination
    # must not also be a red gate — same change as the flywheel shell, same
    # rationale: the 1d/1e decomposition below is the gate; this is the trend
    # gauge (floor 8 stays in the text).
    if r is None or r[0] is None:
        out.append(_check("ret_mature_reuse", "mature key-reuse % (gauge; floor 8; see ret_claim_carry/activation gates)",
                          None, "no mature cohort yet"))
    else:
        out.append(_check("ret_mature_reuse", "mature key-reuse % (gauge; floor 8; see ret_claim_carry/activation gates)",
                          None, f"{r[0]}% combined ({r[1]} cohort) — RAW: this denominator is "
                          f"CONTAMINATED by web-map (web-UI) mints + ~90 one-shot QA/probe keys that can "
                          f"never reuse. The GATED reads are ret_claim_carry + ret_claim_activation."))

    # 1d — THE ACTIVATION WIRING GAP (2026-07-21). Decomposing the 4% showed it is
    # mostly measurement noise + this bug: claim_free_key mints a durable key, but
    # if the agent's session keeps calling WITHOUT switching to the new key,
    # last_used_at never advances and no reuse history ever builds. Live confirm:
    # of agent claims whose session kept calling after mint, only ~18% (9/50)
    # carried the new key — 82% kept the pre-claim anon identity. THAT is the leak.
    # Actuator (SHIPPED behind DCHUB_CLAIM_SESSION_BIND): bind session→minted key so
    # post-claim same-session calls carry it. PASS >= 70%.
    # r-honest-gates (2026-07-31): rendered from the SHARED module (also feeds
    # the flywheel shell — the twin copies are gone). Gates the POST-FIX
    # cohort only; pre-fix claims predate the wiring and are un-fixable
    # history (rebaseline-artifact class). Pre-fix + blended stay in detail.
    from routes.retention_claim_checks import claim_carry_verdict
    _cc_pass, _cc_detail = claim_carry_verdict(c)
    out.append(_check("ret_claim_carry",
                      "post-claim sessions carry the new key (post-fix cohort)",
                      _cc_pass, _cc_detail))

    # 1e — ACTIVATION: of explicitly-claimed MATURE keys, how many were ever USED
    # (any call >1min after mint)? The upstream leak — a claimed key that never
    # makes a call can never reuse. Live baseline ~6% (10/158). PASS >= 40%.
    from routes.retention_claim_checks import claim_activation_verdict
    _ca_pass, _ca_detail = claim_activation_verdict(c)
    out.append(_check("ret_claim_activation",
                      "claimed keys used after mint (mature post-fix cohort)",
                      _ca_pass, _ca_detail))

    # 1c — the retention TRUTH via identity: real external agents on >=2 days/7d.
    n = _row(c,
        "SELECT count(*) FROM (SELECT agent_id FROM mcp_calls_identity WHERE is_real_external "
        "AND created_at>=now()-interval '7 days' GROUP BY agent_id "
        "HAVING count(DISTINCT date_trunc('day',created_at))>=2) t")
    v = (n[0] if n else None)
    out.append(_check("ret_multiday", "real agents returning on a 2nd day (7d)",
                      (v or 0) >= 1 if v is not None else None,
                      f"{v} multi-day agents/7d" if v is not None else "query failed"))
    return out


# ── lane 2 · attribution / claim→paid ─────────────────────────────────

def _lane_attribution(c) -> list[dict]:
    out = []
    if c is None:
        return [_check("attr_nodb", "attribution lane needs db", None, "no db")]

    # 2a — the join's LEFT key exists: recent high-intent sessions carry an
    # mcp_session_id (the identifier the corrected join binds on). >=95% = the
    # attribution surface is healthy. PASS/FAIL.
    r = _row(c,
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE mcp_session_id IS NOT NULL AND mcp_session_id<>'') "
        "FROM mcp_high_intent_sessions WHERE last_hit_at >= now()-interval '7 days'")
    if r is None or not r[0]:
        out.append(_check("attr_sid_present", "high-intent sessions carry a session id (7d)",
                          None, "no recent sessions"))
    else:
        total, with_sid = int(r[0]), int(r[1] or 0)
        pct = round(100.0*with_sid/total, 1) if total else 0.0
        out.append(_check("attr_sid_present", "high-intent sessions carry a session id (7d)",
                          pct >= 95.0, f"{with_sid}/{total} = {pct}% carry sid (join left key)"))

    # 2b — GAUGE: are paid top-ups carrying a session id yet? This is the
    # relay→webhook→topup chain being EXERCISED. 0/N = no relay payment has run
    # the chain yet (real payers use the bare website pricing link). Not a
    # failure — a readiness gauge.
    r = _row(c,
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE mcp_session_id IS NOT NULL AND mcp_session_id<>'') "
        "FROM mcp_topups WHERE paid_at>=now()-interval '30 days'")
    if r is None:
        out.append(_check("attr_topup_sid", "paid top-ups carrying a session id (30d)",
                          None, "query failed"))
    else:
        out.append(_check("attr_topup_sid", "paid top-ups carrying a session id (30d)",
                          None, f"{int(r[1] or 0)}/{int(r[0] or 0)} paid top-ups carry sid — relay→topup chain exercised?"))

    # 2c — GAUGE: honest claim→paid via sid (corrected join). The number the
    # broken sha256(trial-key) join could never surface.
    n = _row(c,
        "SELECT COUNT(DISTINCT h.mcp_session_id) FROM mcp_high_intent_sessions h "
        "JOIN mcp_topups t ON t.mcp_session_id=h.mcp_session_id "
        "WHERE h.claim_used_at>=now()-interval '30 days' AND t.paid_at IS NOT NULL")
    out.append(_check("attr_claim_to_paid", "claim→paid via session id (30d, corrected)",
                      None, f"{(n[0] if n else '?')} relay conversions bound via sid"))
    return out


# ── lane 3 · conversion demand (the real leak) ────────────────────────

def _lane_demand(c) -> list[dict]:
    out = []
    if c is None:
        return [_check("dem_nodb", "demand lane needs db", None, "no db")]

    # 3a — the REAL leak: does the relay produce ANY attributable conversion?
    # relay_paid via sid vs total conversions. 0 relay = demand/offer problem
    # (the 8 conversions are web/organic). FAIL at 0 — this is the point.
    r = _row(c,
        "SELECT (SELECT COUNT(DISTINCT h.mcp_session_id) FROM mcp_high_intent_sessions h "
        " JOIN mcp_topups t ON t.mcp_session_id=h.mcp_session_id "
        " WHERE t.paid_at>=now()-interval '30 days'),"
        " (SELECT COUNT(*) FROM mcp_conversions WHERE created_at>=now()-interval '30 days' "
        "  AND COALESCE(is_test,false)=false)")
    if r is None:
        out.append(_check("dem_relay_converts", "relay produces >=1 conversion (30d)",
                          None, "query failed"))
    else:
        relay_paid, total = int(r[0] or 0), int(r[1] or 0)
        out.append(_check("dem_relay_converts", "relay produces >=1 conversion (30d)",
                          relay_paid >= 1,
                          f"{relay_paid} relay-attributed of {total} total conversions — the demand leak"))

    # 3b — GAUGE: unlock_more_data engagement (distinct callers hitting the
    # value-moment tool). Engagement without conversion = offer problem.
    n = _row(c,
        "SELECT COUNT(DISTINCT api_key) FROM mcp_call_log "
        "WHERE tool='unlock_more_data' AND timestamp>=now()-interval '30 days'")
    out.append(_check("dem_unlock_engage", "distinct agents hitting unlock_more_data (30d)",
                      None, f"{(n[0] if n else '?')} agents reached the checkout value-moment"))

    # 3c — GAUGE: where conversions actually originate (relay vs web vs organic).
    rows = _rows(c,
        "SELECT COALESCE(source,'?'), COUNT(*) FROM mcp_conversions "
        "WHERE created_at>=now()-interval '30 days' AND COALESCE(is_test,false)=false "
        "GROUP BY 1 ORDER BY 2 DESC")
    split = " · ".join(f"{s}:{n}" for s, n in rows) if rows else "none"
    out.append(_check("dem_origin_split", "conversion origin split (30d)",
                      None, split))
    return out


# ── lane 4 · reachability & activation (phase 2 levers) ───────────────

def _lane_reachability(c) -> list[dict]:
    out = []
    if c is None:
        return [_check("reach_nodb", "reachability lane needs db", None, "no db")]

    # 4a — GAUGE, THE binding constraint: are high-intent prospects captured as
    # REACHABLE (an email) or lost anonymous? reachable emails / distinct
    # hi-intent sessions (30d). Baseline ~1/1565 = 0.06%. Watch it climb as the
    # r-lead-bridge stamp (bind_email→mcp_upgrade_signals.user_email) + the
    # tightened free-taste (DCHUB_TRIAL_TOOL_DAILY_FULL) drive binds.
    r = _row(c,
        "SELECT COUNT(DISTINCT user_email) FILTER (WHERE user_email IS NOT NULL AND user_email<>''),"
        " COUNT(DISTINCT session_id) FILTER (WHERE session_id IS NOT NULL AND session_id<>'') "
        "FROM mcp_upgrade_signals WHERE created_at > now()-interval '30 days'")
    if r is None:
        out.append(_check("reach_emails", "reachable high-intent emails (30d)", None, "query failed"))
    else:
        emails, sess = int(r[0] or 0), int(r[1] or 0)
        rate = round(100.0*emails/sess, 2) if sess else 0.0
        out.append(_check("reach_emails", "reachable high-intent emails (30d)",
                          None, f"{emails} reachable of {sess} sessions = {rate}% — r-lead-bridge shipped 07-07, watch climb"))

    # 4b — PASS/FAIL: the nurture's actual sendable audience — signals with an
    # email that maps to a key with EXPLICIT marketing_opt_in (what
    # lost_conversion_outreach gates on), not converted, not already worked.
    # >=1 = the nurture has someone to work; 0 = starved (capture + opt-in must
    # produce a CONSENTED lead — marketing consent is correctly explicit).
    n = _row(c,
        "SELECT COUNT(*) FROM mcp_upgrade_signals s "
        "WHERE s.user_email IS NOT NULL AND s.user_email<>'' "
        "AND COALESCE(s.converted,false)=false AND COALESCE(s.outreach_sent,false)=false "
        "AND EXISTS (SELECT 1 FROM mcp_dev_keys k WHERE LOWER(k.email)=LOWER(s.user_email) "
        "           AND k.metadata->>'marketing_opt_in'='true')")
    v = (n[0] if n else None)
    out.append(_check("reach_optin_audience", "nurture has an opted-in lead to work (>=1)",
                      (int(v) >= 1) if v is not None else None,
                      f"{v} opted-in sendable leads" if v is not None else "query failed"))

    # 4c — GAUGE: the nurture arm itself (lost_conversion_outreach — scheduled
    # 16:00 UTC daily on the worker, real-send, opt-in + suppression gated). It
    # only fires when 4b > 0. Shows all-time sent + days since last (went dark
    # ~28d ago when the opted-in audience dried to 0 — machine works, funnel dry).
    r = _row(c,
        "SELECT COUNT(*) FILTER (WHERE outreach_sent), "
        "ROUND(EXTRACT(EPOCH FROM (now()-MAX(outreach_sent_at)))/86400.0,1) "
        "FROM mcp_upgrade_signals")
    if r is None or r[0] is None:
        out.append(_check("reach_nurture_sends", "nurture outreach fired (lost_conversion, daily 16:00)",
                          None, "no outreach yet"))
    else:
        out.append(_check("reach_nurture_sends", "nurture outreach fired (lost_conversion, daily 16:00)",
                          None, f"{int(r[0] or 0)} sent all-time · last {r[1]}d ago (fires only when 4b>0)"))
    return out


# ── lane 5 · quality-lift weekly watch (recipe/contract/provenance) ────
# ITEM 5 WATCH (2026-07-15): a DURABLE weekly monitor for whether the recipe/
# contract/provenance quality work is lifting first-call-success + return rate.
# All three metrics are computed over the LAST COMPLETE ISO week
# [date_trunc('week',now())-7d, date_trunc('week',now())) — the in-progress week
# is excluded so a partial period never reads as a cliff (same discipline as
# routes/mcp_retention.py). Snapshotted to backfunnel_snapshots each tick, so the
# green/red history accumulates week over week without a manual eyeball.
def _lane_quality_watch(c) -> list[dict]:
    out = []
    if c is None:
        return [_check("qw_nodb", "quality-watch lane needs db", None, "no db")]

    # 5a — FIRST-CALL SUCCESS %: of keys whose FIRST-EVER logged call landed in
    # the last complete week, what share returned data (status='ok') vs an
    # error/paywall. PASS >= 60%.
    r = _row(c,
        "WITH firsts AS ("
        " SELECT DISTINCT ON (api_key) api_key, timestamp AS first_ts, status "
        " FROM mcp_call_log WHERE api_key IS NOT NULL AND api_key <> '' "
        " ORDER BY api_key, timestamp ASC) "
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE status='ok'), "
        " ROUND(100.0*COUNT(*) FILTER (WHERE status='ok')/NULLIF(COUNT(*),0),1) "
        "FROM firsts "
        "WHERE first_ts >= date_trunc('week',now())-interval '7 days' "
        "  AND first_ts <  date_trunc('week',now())")
    if r is None or not r[0]:
        out.append(_check("qw_first_call_ok", "first-call success % (new keys, last wk) >= 60",
                          None, "no new keys last week"))
    else:
        total, okc, pct = int(r[0]), int(r[1] or 0), _num(r[2])
        out.append(_check("qw_first_call_ok", "first-call success % (new keys, last wk) >= 60",
                          (pct or 0) >= 60.0,
                          f"{pct}% ({okc}/{total} new keys' first call = ok; rest error/paywall/trial)"))

    # 5b — RETURNING-AGENT RATE: of distinct real external agents active last
    # week, what share were active on >=2 DISTINCT days (mcp_calls_identity — the
    # honest de-looped north-star). Baseline ~4.7%; PASS >= 5.0 so the lane flips
    # GREEN only once the quality work pushes returns past today's floor.
    r = _row(c,
        "WITH wk AS ("
        " SELECT agent_id, COUNT(DISTINCT date_trunc('day',created_at)) AS days "
        " FROM mcp_calls_identity "
        " WHERE is_real_external AND is_public_ip "
        "   AND created_at >= date_trunc('week',now())-interval '7 days' "
        "   AND created_at <  date_trunc('week',now()) "
        " GROUP BY agent_id) "
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE days>=2), "
        " ROUND(100.0*COUNT(*) FILTER (WHERE days>=2)/NULLIF(COUNT(*),0),1) FROM wk")
    if r is None or not r[0]:
        out.append(_check("qw_return_rate", "returning-agent rate (>=2 days, last wk) >= 5%",
                          None, "no real-external agents last week"))
    else:
        agents, ret, pct = int(r[0]), int(r[1] or 0), _num(r[2])
        out.append(_check("qw_return_rate", "returning-agent rate (>=2 days, last wk) >= 5%",
                          (pct or 0) >= 5.0,
                          f"{pct}% ({ret}/{agents} real agents on >=2 distinct days) — baseline ~4.7%"))

    # 5c — KEY-REUSE %: the durable cross-week return also at /api/v1/mcp/retention
    # (summary.pct_returned_next_week_all_durable) — mature 8-30d cohort across
    # BOTH durable cohorts (auto_trial_keys ∪ dch_live_ mcp_dev_keys). Floor 8%
    # (same as lane 1b / flywheel). The LIKE '%' literal is safe: _row passes NO
    # params tuple, so psycopg2 never %-substitutes.
    r = _row(c,
        "WITH k AS ("
        " SELECT minted_at t0, last_used_at t1 FROM auto_trial_keys WHERE minted_at>=now()-interval '30 days' "
        " UNION ALL "
        " SELECT created_at, last_used_at FROM mcp_dev_keys WHERE api_key LIKE 'dch_live_%' AND created_at>=now()-interval '30 days') "
        "SELECT ROUND(100.0*COUNT(*) FILTER (WHERE t0<now()-interval '7 days' AND t1 IS NOT NULL "
        " AND date_trunc('week',t1)>date_trunc('week',t0))/NULLIF(COUNT(*) FILTER (WHERE t0<now()-interval '7 days'),0),1), "
        " COUNT(*) FILTER (WHERE t0<now()-interval '7 days') FROM k")
    if r is None or r[0] is None:
        out.append(_check("qw_key_reuse", "key-reuse % (mature, BOTH cohorts, 30d) >= 8",
                          None, "no mature cohort yet — see /api/v1/mcp/retention"))
    else:
        pct = _num(r[0])
        out.append(_check("qw_key_reuse", "key-reuse % (mature, BOTH cohorts, 30d) >= 8",
                          (pct or 0) >= 8.0,
                          f"{r[0]}% of {r[1]} mature keys = /api/v1/mcp/retention pct_returned_next_week_all_durable"))
    return out


# ── tick orchestration ────────────────────────────────────────────────

_LANES = [
    ("retention",   "1 · Retention / durable keys (corrected)", _lane_retention,
     "SHIPPED: forward real caller IP (server.mjs) + 30d claim window (DCHUB_CLAIM_REUSE_HOURS) → durable-identity build"),
    ("attribution", "2 · Attribution / claim→paid",             _lane_attribution,
     "SHIPPED: session-id join (mcp_high_intent_claim + funnel_health) + relay-link client_reference_id (+ sid-preserve bridge 07-07)"),
    ("demand",      "3 · Conversion demand (the real leak)",    _lane_demand,
     "NOT shipped — product/offer: depth-tease / unlock_more_data value-moment (relay converts ~0; the 8 are web/organic)"),
    ("reachability","4 · Reachability & activation (phase 2)",  _lane_reachability,
     "SHIPPED: r-lead-bridge (bind_email→mcp_upgrade_signals.user_email) + free-taste tighten (DCHUB_TRIAL_TOOL_DAILY_FULL 8→4); nurture=lost_conversion_outreach daily+opt-in gated. NEXT lever: consented-lead volume"),
    ("quality_watch","5 · Quality-lift weekly watch (recipe/contract/provenance)", _lane_quality_watch,
     "WATCH-ONLY (ITEM 5, 2026-07-15): first-call-success + returning-agent-rate + key-reuse over the LAST COMPLETE ISO week; snapshots to backfunnel_snapshots. No actuator — this pane exists to confirm the recipe/contract/provenance work moves retention"),
]

_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()
_TICK_TTL = 60.0


def _ensure_snapshots(c) -> None:
    try:
        with c.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS backfunnel_snapshots ("
                " id BIGSERIAL PRIMARY KEY,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " lanes_pass INT, lanes_total INT, payload JSONB)")
    except Exception as e:
        logger.debug("[backfunnel] snapshot ddl skipped: %s", e)


def _run_tick() -> dict:
    c = _conn()
    lanes = []
    for key, label, fn, actuator in _LANES:
        try:
            checks = fn(c)
        except Exception as e:  # a lane must never sink the tick
            checks = [_check(f"{key}_error", "lane crashed", None, str(e)[:200])]
        decided = [ch for ch in checks if ch["pass"] is not None]
        lane_pass = bool(decided) and all(ch["pass"] for ch in decided)
        lanes.append({"lane": key, "label": label, "pass": lane_pass,
                      "actuator": actuator, "checks": checks,
                      "progress": f"{sum(1 for ch in decided if ch['pass'])}/{len(checks)}"})
    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lanes_pass": sum(1 for l in lanes if l["pass"]),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "note": "read-only DIAGNOSTIC probes (pure-DB, no self-requests); names an "
                "actuator per lane but fires nothing; see routes/backfunnel_master_shell.py",
    }
    if c is not None:
        try:
            _ensure_snapshots(c)
            with c.cursor() as cur:
                cur.execute("INSERT INTO backfunnel_snapshots (lanes_pass, lanes_total, payload) "
                            "VALUES (%s, %s, %s)",
                            (payload["lanes_pass"], payload["lanes_total"], json.dumps(payload)))
        except Exception as e:
            logger.debug("[backfunnel] snapshot insert failed: %s", e)
        try:
            c.close()
        except Exception:
            pass
    return payload


def _tick_cached() -> dict:
    with _cache_lock:
        if _cache["payload"] is not None and time.time() - _cache["ts"] < _TICK_TTL:
            return _cache["payload"]
    payload = _run_tick()
    with _cache_lock:
        _cache["ts"] = time.time()
        _cache["payload"] = payload
    return payload


# ── routes ────────────────────────────────────────────────────────────

@backfunnel_master_shell_bp.route("/api/v1/admin/backfunnel/master-tick", methods=["GET", "POST"])
def backfunnel_master_tick():
    if _disabled():
        # 404 not 404: a 5xx from Railway trips the CF failover breaker
        # (proxyWithRetry) to stale Render. A kill-switch must NEVER 5xx.
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("fresh") or "") == "1":
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


@backfunnel_master_shell_bp.route("/admin/backfunnel", methods=["GET"])
@backfunnel_master_shell_bp.route("/api/v1/admin/backfunnel", methods=["GET"])
def backfunnel_dashboard():
    if _disabled():
        return Response("backfunnel disabled", status=404)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _tick_cached()

    def _chip(v):
        if v is True:
            return '<span style="color:#22c55e">✓</span>'
        if v is False:
            return '<span style="color:#ef4444">✗</span>'
        return '<span style="color:#eab308">?</span>'

    cards = []
    for lane in p["lanes"]:
        rows = "".join(
            f"<tr><td style='padding:4px 8px'>{_chip(ch['pass'])}</td>"
            f"<td style='padding:4px 8px'>{_esc(ch['name'])}</td>"
            f"<td style='padding:4px 8px;color:#94a3b8'>{_esc(ch['detail'])}</td></tr>"
            for ch in lane["checks"])
        border = "#22c55e" if lane["pass"] else "#334155"
        cards.append(
            f"<div style='background:#0f172a;border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'>"
            f"<div style='font-weight:700;font-size:15px'>{_chip(lane['pass'])} "
            f"{_esc(lane['label'])} <span style='color:#64748b;font-weight:400'>"
            f"({lane['progress']} checks green)</span></div>"
            f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>{rows}</table>"
            f"<div style='margin-top:8px;font-size:12px;color:#64748b'>⚡ actuator (not fired): "
            f"{_esc(lane.get('actuator',''))}</div></div>")

    green = p["lanes_pass"] == p["lanes_total"]
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='60'>"
        "<title>Back-of-Funnel Truth · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:880px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>Back-of-Funnel Truth Master Shell "
        f"<span style='color:{'#22c55e' if green else '#eab308'}'>"
        f"{p['lanes_pass']}/{p['lanes_total']} lanes green</span></h2>"
        f"<div style='color:#64748b;font-size:12px'>retention · attribution · conversion demand — "
        f"the 07-06 funnel fix-wave, measured with CORRECTED metrics · read-only DIAGNOSTIC "
        f"(pure-DB, no self-requests; names an actuator per lane, fires nothing) · 60s cache · "
        f"generated {_esc(p['generated_at'])} · JSON: /api/v1/admin/backfunnel/master-tick</div>"
        + "".join(cards) + "</body>")
    return Response(html, mimetype="text/html")
