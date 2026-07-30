"""agent_success_report.py — the public weekly Agent Success Report (2026-07-30).

ChatGPT's one genuinely new proposal from the 07-30 partner round: a public,
versioned weekly report of whether AI agents are actually SUCCEEDING here —
not raw traffic, which we already publish, but adoption of the front door and
time-to-first-result, on the honest population.

Every number on this surface is crawler-excluded. That is the report's whole
reason to exist as a separate endpoint: the 07-28 audit found the "real agent"
init population dominated by MCP registry crawlers/indexers/health checkers,
and this codebase has retracted enough headline numbers (86 agents, 35-41k
calls, 89.4% one-and-gone) to know that a public metric starts from the
excluded view or it starts wrong.

SOURCES — exactly two, both pre-existing:
  · mcp_calls_identity — the canonical crawler-excluded identity VIEW
    (rendered from mcp_calls_deloop by scripts/render_identity_views.py;
    registry-crawler families applied to Neon 2026-07-28 22:12Z, 9/9 verified).
    Supplies: tool calls, active agents, median time-to-first-result, and the
    generic-'mcp'-bucket share that gates the per-platform split.
  · the planner-bypass agent-day episode model (routes/planner_bypass.py,
    DEFINITION_VERSION 2) — planner adoption vs manual orchestration with the
    observed/judgement split. Reused via _measure(extra_where=…) with the
    registry-crawler exclusions ADDED (regex-form predicates, because those
    queries run with bound params where a literal LIKE % eats an argument).
    The population therefore differs from the admin endpoint's — each metric
    block below declares its own definition_version for exactly that reason.

HOUSE RULES ENFORCED HERE (tests pin all three):
  · every rate is None + status UNMEASURED on an empty denominator — a rate
    that could not be measured must never render as 0% or 100%;
  · every metric carries definition_version + definition_changelog, and a
    version without a changelog entry fails CI (the planner-bypass pattern:
    the numbers were never wrong, the MEANING moved — so the meaning is now a
    declared, versioned contract);
  · per-platform splits are GATED AT BIRTH: the attribution fix (client_name_raw
    on the stateless path, dchub-mcp-server 8c0d08b) landed 2026-07-28, needs
    ~7 days of accumulation, AND the generic-'mcp' bucket share must have
    actually dropped before a per-platform number is publishable. The gate is
    encoded, not remembered: the split publishes itself only when BOTH
    conditions verify against live data, and the payload shows the gate state
    either way. (7d window: judging the fix early would measure the old data.)

Endpoint (public, read-only, cached):
  GET /api/v1/reports/agent-success

Stale-while-revalidate cache, single-flight refresh (same shape as
/api/v1/reach): a public DB-backed endpoint must never let a crawler stampede
reach the pool (the sitemap incident). Kill switch:
AGENT_SUCCESS_REPORT_DISABLE=1.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, datetime, timezone

from flask import Blueprint, jsonify

from mcp_calls_deloop import (
    PLATFORM_CASE,
    internal_tag_regex_predicate,
    real_ua_predicate,
)
from routes.planner_bypass import (
    DEFINITION_VERSION as EPISODE_MODEL_VERSION,
    _measure as _episode_measure,
    _rate,
)

logger = logging.getLogger("agent_success_report")
agent_success_report_bp = Blueprint("agent_success_report", __name__)

WINDOW_DAYS = 7
_STMT_TIMEOUT_MS = 6000

# The payload SHAPE version (field layout / gating semantics). Individual
# metrics carry their own versions below — bump those when a metric's MEANING
# changes; bump this when the envelope itself does.
REPORT_DEFINITION_VERSION = 1
REPORT_DEFINITION_CHANGELOG = {
    1: "initial — weekly rolling 7d window; crawler-excluded population only; "
       "per-platform split gated on attribution accumulation + verified "
       "generic-bucket drop",
}

# ── Crawler exclusions for the EPISODE queries (mcp_call_log) ──────────────
# The identity view already carries the exclusions for everything read from
# mcp_calls_identity. The episode model reads mcp_call_log WITH bound params,
# so it gets the regex twins of the same shared family constants. Regex form
# is load-bearing: the LIKE form's literal % would be consumed by psycopg2
# paramstyle substitution (the trap that took /api/v1/map down 2026-07-17).
CRAWLER_EXCLUSION_WHERE = (
    " AND " + internal_tag_regex_predicate("platform")
    + " AND " + real_ua_predicate("user_agent")
)

# ── Per-platform attribution gate ──────────────────────────────────────────
# The fix that makes per-platform splits meaningful (client_name_raw threaded
# on the stateless path + _PLATFORM_RECALL) landed in dchub-mcp-server on this
# date. Before it, ~88-90% of real calls landed in the generic bucket — a
# split over that would be a split of the unattributed remainder, i.e. noise
# published with confidence.
#
# ★ THE BUCKET RENAMED THE SAME DAY. The baseline was measured when the
# classifier labelled generic traffic 'mcp'; a 07-28 PLATFORM_CASE change
# routes client_name IN ('mcp','mcp-client','client','default') into its own
# real bucket 'mcp-generic-client'. Gate on the FAMILY, not the old label —
# `= 'mcp'` reads 0.0% against live data (verified 2026-07-30: the live split
# shows mcp-generic-client 3,436 of 4,416 calls = 77.8%, and zero 'mcp' rows)
# and would have silently false-opened the gate on day 7.
GENERIC_BUCKETS = ("mcp", "mcp-generic-client")
ATTRIBUTION_FIX_DATE = date(2026, 7, 28)
ATTRIBUTION_MIN_ACCUMULATION_DAYS = 7          # full window must be post-fix
MCP_BUCKET_SHARE_PRE_FIX = 0.88                # 3,179/3,623 measured 07-28
MCP_BUCKET_MAX_SHARE_TO_PUBLISH = 0.80         # "actually dropped" threshold


def _attribution_gate(days_since_fix, mcp_share):
    """(passed, status, reason). Pure — fully unit-testable.

    Both conditions must hold: the trailing window is entirely post-fix, AND
    the generic-'mcp' bucket share measurably dropped below the pre-fix
    baseline. Either alone is not evidence the split is honest: early data
    still measures the old writer, and an aged window with an unchanged share
    means the fix did not take."""
    if days_since_fix < ATTRIBUTION_MIN_ACCUMULATION_DAYS:
        return (False, "GATED_ACCUMULATING",
                f"attribution fix landed {ATTRIBUTION_FIX_DATE.isoformat()}; "
                f"{days_since_fix}d of ≥{ATTRIBUTION_MIN_ACCUMULATION_DAYS}d "
                "post-fix accumulation — a 7d split computed now would mostly "
                "measure pre-fix writes")
    if mcp_share is None:
        return (False, "GATED_ATTRIBUTION_UNVERIFIED",
                "generic-client bucket share could not be measured — cannot "
                "verify the attribution fix took, so the split stays gated")
    if mcp_share > MCP_BUCKET_MAX_SHARE_TO_PUBLISH:
        return (False, "GATED_ATTRIBUTION_UNVERIFIED",
                f"generic-client bucket ({'/'.join(GENERIC_BUCKETS)}) still holds "
                f"{round(100 * mcp_share, 1)}% of real calls (pre-fix baseline "
                f"~{round(100 * MCP_BUCKET_SHARE_PRE_FIX)}%, publish threshold "
                f"≤{round(100 * MCP_BUCKET_MAX_SHARE_TO_PUBLISH)}%) — the share "
                "has not verifiably dropped, so a per-platform split would "
                "still be a split of the unattributed remainder")
    return (True, "MEASURED", "attribution accumulation and bucket-share gates passed")


# ── Metric definitions — the versioned contract this surface publishes ─────
# Every metric block in the payload is rendered from THIS registry, so a
# metric cannot ship without its version and changelog (test-pinned). Bump a
# metric's version whenever its MEANING changes, even if name and type do not
# — the name/type-stable case is exactly the one that bit agent_adoption's
# planner_first consumer.
METRICS = {
    "tool_calls_7d": {
        "definition": "COUNT(*) over mcp_calls_identity WHERE is_real_external, "
                      "trailing 7 days. Counts tool CALLS, never sessions.",
        "unit": "calls",
        "source": "mcp_calls_identity (crawler-excluded view over mcp_tool_calls)",
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — identity-view population (internal traffic, scripted "
               "UAs, QA tags and registry/health/scanner crawlers excluded; "
               "r-registry-crawlers families applied 2026-07-28)",
        },
    },
    "active_agents_7d": {
        "definition": "COUNT(DISTINCT agent_id) over mcp_calls_identity WHERE "
                      "is_real_external AND is_public_ip, trailing 7 days. "
                      "agent_id = md5(first public X-Forwarded-For hop); "
                      "Cloudflare-POP hops are NULL and never counted.",
        "unit": "distinct agents",
        "source": "mcp_calls_identity",
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — same agent grain as /api/v1/reach real_agents_7d "
               "(never session_id, which rotates per connection)",
        },
    },
    "planner_adoption_pct": {
        "definition": "Of agent-day episodes with 2+ calls (opportunities), the "
                      "share whose FIRST call was execute_plan. Pure observation "
                      "— no judgement about whether the planner SHOULD have been "
                      "used.",
        "unit": "% of opportunity episodes",
        "source": f"planner-bypass episode model v{EPISODE_MODEL_VERSION} "
                  "(mcp_call_log, agent-day unit) + registry-crawler exclusions",
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — episode model v2 semantics (agent-day unit; durable "
               "api_key first, session fallback) with the registry-crawler "
               "exclusions ADDED. Population therefore differs from "
               "/api/v1/admin/planner-bypass, which publishes the un-excluded "
               "fleet view under its own version.",
        },
    },
    "manual_orchestration_pct": {
        "definition": "Of opportunity episodes, the share that hand-chained: 2+ "
                      "DISTINCT tools AND a hand-off signal (a later call carrying "
                      "a planner-contract chaining key the first call lacked), "
                      "without execute_plan first. Benign fan-out (same tool, "
                      "different args) and single-capability lookups are excluded "
                      "and reported separately — they are correct usage, not "
                      "bypass.",
        "unit": "% of opportunity episodes",
        "source": f"planner-bypass episode model v{EPISODE_MODEL_VERSION} "
                  "(mcp_call_log, agent-day unit) + registry-crawler exclusions",
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — episode model v2 semantics with the registry-crawler "
               "exclusions ADDED (population differs from the admin endpoint); "
               "observed shape only, the bypass JUDGEMENT stays on the admin "
               "surface",
        },
    },
    "median_time_to_first_result_ms": {
        "definition": "Per agent-day episode: milliseconds from the episode's "
                      "first tool call to completion of its first SUCCESSFUL "
                      "call (arrival gap + that call's response_time_ms). "
                      "Median across episodes with at least one successful "
                      "call; episodes with none are counted separately, never "
                      "averaged in as zero.",
        "unit": "milliseconds (median)",
        "source": "mcp_calls_identity (agent_id × day grain)",
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — success is writer-reported (a tracker payload with no "
               "status field records success=TRUE), so 'first result' means "
               "'first call not explicitly reported failed'",
        },
    },
}

# ── SQL — all identity-view queries run with NO bound params ───────────────
# Window and thresholds are trusted module constants inlined as literals, so
# psycopg2 performs no %-substitution and PLATFORM_CASE's ILIKE '%…%' literals
# are safe exactly as they are in mcp_calls_deloop itself.
_W = f"created_at >= NOW() - ({WINDOW_DAYS} * INTERVAL '1 day')"

_SQL_TOTALS = f"""
SELECT COUNT(*) FILTER (WHERE is_real_external)                        AS tool_calls,
       COUNT(DISTINCT agent_id) FILTER (WHERE is_real_external
                                          AND is_public_ip)            AS active_agents
  FROM mcp_calls_identity
 WHERE {_W}
"""

_SQL_TTFR = f"""
WITH rows7 AS (
  SELECT agent_id, created_at::date AS day, created_at, success,
         COALESCE(response_time_ms, 0) AS rt
    FROM mcp_calls_identity
   WHERE {_W}
     AND is_real_external AND is_public_ip AND agent_id IS NOT NULL
),
ep AS (
  SELECT agent_id, day, MIN(created_at) AS first_ts
    FROM rows7 GROUP BY agent_id, day
),
ok1 AS (
  SELECT DISTINCT ON (agent_id, day) agent_id, day, created_at AS ok_ts, rt
    FROM rows7 WHERE success ORDER BY agent_id, day, created_at ASC
)
SELECT COUNT(*)                                          AS episodes,
       COUNT(o.ok_ts)                                    AS episodes_with_result,
       PERCENTILE_CONT(0.5) WITHIN GROUP (
         ORDER BY EXTRACT(EPOCH FROM (o.ok_ts - e.first_ts)) * 1000.0 + o.rt)
         FILTER (WHERE o.ok_ts IS NOT NULL)              AS median_ttfr_ms
  FROM ep e LEFT JOIN ok1 o USING (agent_id, day)
"""

# Gate evidence: the share of real calls whose canonical bucket is the generic
# FAMILY (see GENERIC_BUCKETS — the 07-28 classifier renamed the old 'mcp'
# label to 'mcp-generic-client'; matching only the old label reads 0.0%).
_GENERIC_IN = ", ".join(f"'{b}'" for b in GENERIC_BUCKETS)
_SQL_MCP_SHARE = f"""
SELECT COUNT(*)                                             AS real_calls,
       COUNT(*) FILTER (WHERE ({PLATFORM_CASE.strip()})
                          IN ({_GENERIC_IN}))               AS generic_bucket_calls
  FROM mcp_calls_identity
 WHERE {_W} AND is_real_external
"""

_SQL_PLATFORM_SPLIT = f"""
SELECT ({PLATFORM_CASE.strip()})                                  AS platform,
       COUNT(*)                                                   AS calls,
       COUNT(DISTINCT agent_id) FILTER (WHERE is_public_ip)       AS agents
  FROM mcp_calls_identity
 WHERE {_W} AND is_real_external
 GROUP BY 1 ORDER BY calls DESC LIMIT 15
"""


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


def _bounded(cur, sql, fetch="one"):
    """One aggregate per explicit transaction with SET LOCAL statement_timeout
    — the only form that sticks on Neon's pooled endpoint (see
    flask_mcp_endpoints._reach_bounded, verified live 2026-07-01). ROLLBACK on
    error so a timed-out query never poisons the next one."""
    cur.execute("BEGIN")
    try:
        cur.execute("SET LOCAL statement_timeout = %d" % _STMT_TIMEOUT_MS)
        cur.execute(sql)
        result = cur.fetchone() if fetch == "one" else cur.fetchall()
        cur.execute("COMMIT")
        return result
    except Exception:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        raise


def _metric_block(key, value, status, **extra):
    """Render one metric with its full versioned contract attached."""
    block = {"value": value, "status": status}
    block.update(extra)
    block.update(METRICS[key])
    return block


def _build_report() -> dict:
    now = datetime.now(timezone.utc)
    days_since_fix = (now.date() - ATTRIBUTION_FIX_DATE).days
    out = {
        "ok": True,
        "report": "agent-success-weekly",
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": WINDOW_DAYS,
        "definition_version": REPORT_DEFINITION_VERSION,
        "definition_changelog": REPORT_DEFINITION_CHANGELOG,
        "population": (
            "real external agents only. Internal/self traffic, scripted UAs, QA "
            "tags AND MCP registry/health/scanner crawlers are excluded "
            "(mcp_calls_deloop families, r-registry-crawlers 2026-07-28). "
            "Ambiguous gateways that proxy real users (smithery, glama, "
            "agent-toolscloud) are deliberately KEPT."
        ),
        "sources": {
            "identity_view": "mcp_calls_identity (crawler exclusions applied to "
                             "Neon 2026-07-28 22:12Z, 9/9 families verified)",
            "episode_model": f"routes/planner_bypass.py definition_version "
                             f"{EPISODE_MODEL_VERSION} over mcp_call_log, with "
                             "the same crawler exclusions added (regex form)",
        },
        "metrics": {},
    }

    c = _conn()
    if c is None:
        out["ok"] = False
        out["error"] = "no database connection"
        for key in METRICS:
            out["metrics"][key] = _metric_block(key, None, "UNAVAILABLE")
        return out

    mcp_share = None
    platform_rows = None
    split_error = None
    try:
        with c.cursor() as cur:
            # ── totals off the identity view ────────────────────────────────
            try:
                calls, agents = _bounded(cur, _SQL_TOTALS)
                out["metrics"]["tool_calls_7d"] = _metric_block(
                    "tool_calls_7d", int(calls or 0), "MEASURED")
                out["metrics"]["active_agents_7d"] = _metric_block(
                    "active_agents_7d", int(agents or 0), "MEASURED")
            except Exception as e:
                logger.warning("[agent-success] totals: %s", str(e)[:150])

            # ── median time-to-first-result ─────────────────────────────────
            try:
                episodes, with_result, ttfr = _bounded(cur, _SQL_TTFR)
                episodes, with_result = int(episodes or 0), int(with_result or 0)
                measured = with_result > 0 and ttfr is not None
                block = _metric_block(
                    "median_time_to_first_result_ms",
                    round(float(ttfr), 1) if measured else None,
                    "MEASURED" if measured else "UNMEASURED",
                    episodes=episodes,
                    episodes_with_result=with_result,
                    episodes_without_result=episodes - with_result,
                )
                if not measured:
                    block["unmeasured_reason"] = (
                        "no agent-day episode in the window recorded a "
                        "successful call — nothing to take a median over"
                    )
                out["metrics"]["median_time_to_first_result_ms"] = block
            except Exception as e:
                logger.warning("[agent-success] ttfr: %s", str(e)[:150])

            # ── generic-bucket share (the attribution gate's evidence) ──────
            try:
                real_calls, generic_calls = _bounded(cur, _SQL_MCP_SHARE)
                real_calls, generic_calls = int(real_calls or 0), int(generic_calls or 0)
                if real_calls:
                    mcp_share = round(generic_calls / real_calls, 4)
            except Exception as e:
                logger.warning("[agent-success] generic share: %s", str(e)[:150])

            # ── the split itself — computed ONLY behind the gate ────────────
            if _attribution_gate(days_since_fix, mcp_share)[0]:
                try:
                    rows = _bounded(cur, _SQL_PLATFORM_SPLIT, fetch="all")
                    platform_rows = [
                        {"platform": p, "calls": int(n or 0), "agents": int(a or 0)}
                        for (p, n, a) in (rows or [])
                    ]
                except Exception as e:
                    logger.warning("[agent-success] split: %s", str(e)[:150])
                    split_error = str(e)[:150]
    except Exception as e:
        logger.warning("[agent-success] identity block: %s", str(e)[:200])
    finally:
        try:
            c.close()
        except Exception:
            pass

    # A partial DB failure must degrade to explicit UNAVAILABLE blocks, never
    # to a 500 and never to invented zeros.
    for key in ("tool_calls_7d", "active_agents_7d",
                "median_time_to_first_result_ms"):
        out["metrics"].setdefault(
            key, _metric_block(key, None, "UNAVAILABLE",
                               error="identity view query failed"))

    # ── per-platform block — the gate IS the payload, present either way ───
    passed, status, reason = _attribution_gate(days_since_fix, mcp_share)
    per_platform = {
        "status": status,
        "reason": reason,
        "definition_version": 1,
        "definition_changelog": {
            1: "initial — born gated: publishes only after "
               f"{ATTRIBUTION_MIN_ACCUMULATION_DAYS}d of post-fix "
               "accumulation AND a verified generic-bucket drop",
        },
        "gate": {
            "attribution_fix_date": ATTRIBUTION_FIX_DATE.isoformat(),
            "attribution_fix": "client_name_raw threaded on the stateless "
                               "path + platform recall (dchub-mcp-server, "
                               "2026-07-28)",
            "min_accumulation_days": ATTRIBUTION_MIN_ACCUMULATION_DAYS,
            "days_since_fix": days_since_fix,
            "earliest_eligible": "2026-08-04",
            "generic_buckets": list(GENERIC_BUCKETS),
            "generic_bucket_share_7d": mcp_share,
            "generic_bucket_share_pre_fix_baseline": MCP_BUCKET_SHARE_PRE_FIX,
            "max_share_to_publish": MCP_BUCKET_MAX_SHARE_TO_PUBLISH,
            "share_note": "share of real 7d calls whose canonical platform "
                          "bucket is generic (the pre-fix 'mcp' label, renamed "
                          "'mcp-generic-client' by the 07-28 classifier) — an "
                          "attribution-quality gauge, not a usage split. When "
                          "the split publishes, the generic bucket stays "
                          "visible as its own row, never redistributed.",
            "passed": passed,
        },
    }
    if passed:
        if platform_rows is not None:
            per_platform["platforms"] = platform_rows
        else:
            per_platform["status"] = "UNAVAILABLE"
            per_platform["error"] = split_error or "split query failed"
    out["per_platform"] = per_platform

    # ── episode metrics (planner adoption / manual orchestration) ──────────
    # Own connection inside _episode_measure; crawler exclusions threaded in.
    try:
        ep = _episode_measure(WINDOW_DAYS, extra_where=CRAWLER_EXCLUSION_WHERE)
        opportunities = ep.get("opportunities")
        shared = {
            "numerator_unit": "agent-day episodes",
            "denominator": opportunities,
            "denominator_definition": "episodes with 2+ calls in the window",
            "episodes_total": ep.get("episodes"),
        }
        if ep.get("ok") and ep.get("status") in ("MEASURED", "UNMEASURED"):
            measured = ep["status"] == "MEASURED"
            adoption = _metric_block(
                "planner_adoption_pct",
                ep.get("planner_adoption_pct") if measured else None,
                ep["status"], numerator=ep.get("planner_adopted"), **shared)
            manual = _metric_block(
                "manual_orchestration_pct",
                ep.get("manual_orchestration_pct") if measured else None,
                ep["status"], numerator=ep.get("manual_orchestration"), **shared)
            if not measured:
                for b in (adoption, manual):
                    b["unmeasured_reason"] = ep.get("unmeasured_reason") or (
                        "no opportunity episodes in the window")
            else:
                # Context that keeps the two rates honest at a glance: what was
                # deliberately NOT counted against the fleet.
                adoption["not_counted_as_bypass"] = manual["not_counted_as_bypass"] = {
                    "benign_fanout": ep.get("benign_fanout"),
                    "benign_direct_single_call": ep.get("benign_direct_single_call"),
                    "independent_multi_no_handoff": ep.get("independent_multi_no_handoff"),
                }
            out["metrics"]["planner_adoption_pct"] = adoption
            out["metrics"]["manual_orchestration_pct"] = manual
            out["assumptions"] = ep.get("assumptions")
        else:
            raise RuntimeError(ep.get("error") or "episode measure failed")
    except Exception as e:
        logger.warning("[agent-success] episodes: %s", str(e)[:150])
        for key in ("planner_adoption_pct", "manual_orchestration_pct"):
            out["metrics"][key] = _metric_block(key, None, "UNAVAILABLE",
                                                error=str(e)[:150])

    # ok = at least one metric actually measured (or honestly UNMEASURED).
    # A build where every block is UNAVAILABLE is a failed build — the route
    # must not cache it, and a consumer must not mistake it for a quiet week.
    out["ok"] = any(b.get("status") != "UNAVAILABLE"
                    for b in out["metrics"].values())
    if not out["ok"]:
        out["error"] = "all metrics unavailable"
    return out


# ── Stale-while-revalidate cache, single-flight refresh ────────────────────
_CACHE = {"data": None, "ts": 0.0}
_CACHE_TTL_S = 900
_REFRESH_LOCK = threading.Lock()
_REFRESH_RUNNING = False


def _refresh_cache():
    global _REFRESH_RUNNING
    try:
        data = _build_report()
        if data.get("ok"):
            _CACHE["data"] = data
            _CACHE["ts"] = time.time()
    except Exception as e:
        logger.warning("[agent-success] refresh failed: %s", str(e)[:200])
    finally:
        with _REFRESH_LOCK:
            _REFRESH_RUNNING = False


def _disabled() -> bool:
    return (os.environ.get("AGENT_SUCCESS_REPORT_DISABLE") or "").strip() == "1"


@agent_success_report_bp.route("/api/v1/reports/agent-success", methods=["GET"])
def agent_success_report():
    global _REFRESH_RUNNING
    if _disabled():
        return jsonify(ok=False, error="disabled"), 503
    now = time.time()
    data = _CACHE["data"]
    if data is not None:
        age = now - _CACHE["ts"]
        if age >= _CACHE_TTL_S:
            with _REFRESH_LOCK:
                if not _REFRESH_RUNNING:
                    _REFRESH_RUNNING = True
                    threading.Thread(target=_refresh_cache,
                                     name="agent-success-refresh",
                                     daemon=True).start()
        payload = dict(data)
        payload["cache_age_s"] = round(age, 1)
        return jsonify(payload), 200, {"Cache-Control": "public, max-age=300"}
    # First build since boot — bounded inline (per-query statement_timeout).
    data = _build_report()
    if data.get("ok"):
        _CACHE["data"] = data
        _CACHE["ts"] = time.time()
        payload = dict(data)
        payload["cache_age_s"] = 0.0
        return jsonify(payload), 200, {"Cache-Control": "public, max-age=300"}
    return jsonify(data), 503
