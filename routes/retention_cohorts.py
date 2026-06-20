"""routes/retention_cohorts.py — RETENTION COHORT analyzer (2026-06-19).

WHY THIS EXISTS
---------------
The brain diagnosed RETENTION as the flywheel leak (~74 external agents try the
MCP per week, ~1 returns) but flagged its own answer at 0.2 confidence because it
had NO instrumentation to reason on. This module is that instrumentation: it
measures key-based retention over the durable `api_key` identity in
`mcp_call_log`, so the next retention investigate() is data-grounded instead of
an inference dressed as a finding.

WHAT IT MEASURES (all over mcp_call_log, all DISTINCT api_key)
-------------------------------------------------------------
  1. NEW KEYS            — distinct api_keys whose FIRST call landed in the window.
  2. RETURN RATE         — distinct-returners / distinct-first-callers, where a
                           "return" = a 2nd call (the headline), with a stricter
                           multi-day variant (a call on a 2nd distinct calendar
                           day) surfaced separately. A retention rate is ALWAYS
                           distinct-returners / distinct-first-callers — never
                           COUNT(*) / COUNT(*).
  3. REUSE BUCKETS       — distribution of calls-per-key: 1 (one-off) / 2-5
                           (light) / 6+ (heavy).
  4. FIRST-TOOL MIX      — the tool of each key's FIRST call (what agents reach
                           for first).
  5. RETENTION-BY-FIRST-TOOL — for keys whose first tool was T, the return rate.
                           Answers "which first tool makes agents come back?"
                           (does get_grid_intelligence retain better?).
  6. TIME-TO-2ND-CALL    — median + p95 seconds from first call to the return
                           call, over keys that returned.

HONESTY (non-negotiable)
------------------------
  · DE-LOOP via the canonical `mcp_calls_deloop` module. mcp_call_log has NO
    client_name/user_agent column — its `platform` is PRE-CLASSIFIED (server.mjs
    wrote the same buckets the PLATFORM_CASE classifier emits). So we cannot call
    deloop_calls_where() (it reads those two raw columns). Instead we REUSE the
    canonical `PROBE_PLATFORMS` constant from that module and exclude those
    pre-classified platforms directly:  platform NOT IN (<PROBE_PLATFORMS>).
    This keeps the probe/self-traffic definition in ONE place — adding a probe
    bucket to mcp_calls_deloop.PROBE_PLATFORMS automatically tightens this too.
  · COUNT(DISTINCT api_key), NEVER COUNT(*), for every cohort headcount.
  · NULL / empty api_key rows are excluded (anon calls have no durable identity).
  · Best-effort: every query is wrapped; the analyzer NEVER raises and returns
    {} when there is no DB or no data.

ENDPOINT
--------
  GET /api/v1/mcp/retention/cohorts   (admin-gated)  — the cohort summary for ops.
    Optional ?days=30 (clamped 1..180).

This file touches NO contested files, mirroring routes/mcp_retention.py's
self-contained drop-in posture. brain_investigator imports compute_retention_cohorts
as a new evidence Source.
"""
from __future__ import annotations

import logging
import os

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

retention_cohorts_bp = Blueprint("retention_cohorts", __name__)


# ── DE-LOOP: reuse the canonical probe list (do NOT hand-roll) ───────────
def _deloop_platform_predicate(col: str = "platform") -> str:
    """Boolean SQL fragment that is TRUE for a real external call: the
    pre-classified `platform` is NOT one of the canonical probe/self buckets.

    REUSES mcp_calls_deloop.PROBE_PLATFORMS — the single source of truth for
    what counts as self/probe traffic — so this stays in lock-step with the
    funnel's `tool_calls_7d_real` de-loop. mcp_call_log has no client_name /
    user_agent, so we apply the SAME exclusion list to its pre-classified
    platform column rather than re-running the PLATFORM_CASE UA classifier.

    Inlined SQL literals over trusted hardcoded constants (no bound params), so
    there is no %-substitution collision (see the psycopg2 empty-tuple % trap)."""
    try:
        from mcp_calls_deloop import PROBE_PLATFORMS as _probes
    except Exception:
        # Fail SAFE to the canonical list literal if the import is unavailable
        # in some stripped context — kept byte-identical to mcp_calls_deloop.
        _probes = (
            "internal-dchub", "curl", "python-script", "node-script",
            "node-http-client", "postman", "insomnia", "verify",
        )
    in_list = ",".join(
        "'" + str(p).replace("'", "''") + "'"
        for p in sorted(set(_probes)))
    # COALESCE so a NULL platform (real external call we couldn't classify) is
    # treated as '' which is NOT in the probe list → kept (honest: unknown is
    # real external demand, matching mcp_calls_deloop's 'unknown' handling).
    return f"COALESCE({col}, '') NOT IN ({in_list})"


# ── DB (direct psycopg2, autocommit read-only) ──────────────────────────
def _conn():
    """Raw psycopg2 connection, autocommit so a failed query never poisons a
    shared transaction. None when DATABASE_URL is unset — caller degrades."""
    try:
        import psycopg2
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if not dsn:
            return None
        c = psycopg2.connect(dsn, sslmode="require", connect_timeout=6)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("retention_cohorts: _conn failed: %s", e)
        return None


def _clamp_days(days) -> int:
    try:
        d = int(days)
    except (TypeError, ValueError):
        return 30
    return max(1, min(180, d))


# ── The analyzer ────────────────────────────────────────────────────────
def compute_retention_cohorts(days: int = 30, conn=None) -> dict:
    """Compute the key-based retention cohort summary over the last `days`.

    Returns a dict (best-effort; {} when there is no DB or no data):
      {
        "window_days": int,
        "new_keys": int,                  # DISTINCT api_key, first call in window
        "returned_keys": int,             # of those, made a 2nd call
        "return_rate": float,             # returned_keys / new_keys * 100 (pct)
        "multiday_returned_keys": int,    # made a call on a 2nd distinct day
        "multiday_rate": float,           # multiday_returned / new_keys * 100
        "reuse_buckets": {"1": int, "2-5": int, "6+": int},
        "first_tool_mix": [{"tool", "count", "pct"}],          # desc by count
        "retention_by_first_tool": [
            {"tool", "first_callers", "returned", "return_rate"}  # desc by rate
        ],
        "median_time_to_2nd_call_seconds": float | None,
        "p95_time_to_2nd_call_seconds": float | None,
      }

    HONEST-NUMBERS: every headcount is COUNT(DISTINCT api_key); a return rate is
    ALWAYS distinct-returners / distinct-first-callers; the de-loop predicate is
    applied in EVERY scan. NEVER raises.
    """
    days = _clamp_days(days)
    own_conn = conn is None
    if own_conn:
        conn = _conn()
    if conn is None:
        return {}

    dl = _deloop_platform_predicate("platform")
    # The de-looped, identified, in-window base set. We materialize each key's
    # first-call timestamp + first tool ONCE in a CTE that every metric reuses.
    base_cte = f"""
        WITH base AS (
            SELECT api_key, tool, timestamp, session_id
            FROM mcp_call_log
            WHERE timestamp >= NOW() - INTERVAL '{days} days'
              AND {dl}
              AND api_key IS NOT NULL AND api_key <> ''
        ),
        firsts AS (
            SELECT api_key,
                   MIN(timestamp) AS first_ts,
                   (ARRAY_AGG(tool ORDER BY timestamp ASC))[1] AS first_tool
            FROM base
            GROUP BY api_key
        )
    """

    out: dict = {"window_days": days}
    try:
        with conn.cursor() as cur:
            def run(sql, params=None):
                cur.execute(sql, params or ())
                return cur.fetchall()

            # ── (1) + (2) + (5) new keys, returns, multi-day, time-to-2nd ──
            # Per first-caller: number of calls, distinct days, distinct
            # sessions, and the gap to the 2nd call. All over DISTINCT api_key.
            rows = run(base_cte + f"""
                SELECT
                    COUNT(DISTINCT f.api_key)                              AS new_keys,
                    COUNT(DISTINCT f.api_key) FILTER (
                        WHERE EXISTS (
                            SELECT 1 FROM base b
                            WHERE b.api_key = f.api_key
                              AND b.timestamp > f.first_ts))               AS returned_keys,
                    COUNT(DISTINCT f.api_key) FILTER (
                        WHERE EXISTS (
                            SELECT 1 FROM base b
                            WHERE b.api_key = f.api_key
                              AND DATE(b.timestamp) > DATE(f.first_ts)))   AS multiday_keys
                FROM firsts f
            """)
            if rows and rows[0]:
                new_keys = int(rows[0][0] or 0)
                returned = int(rows[0][1] or 0)
                multiday = int(rows[0][2] or 0)
                out["new_keys"] = new_keys
                out["returned_keys"] = returned
                out["multiday_returned_keys"] = multiday
                # A retention rate is distinct-returners / distinct-first-callers.
                out["return_rate"] = (
                    round(100.0 * returned / new_keys, 1) if new_keys else 0.0)
                out["multiday_rate"] = (
                    round(100.0 * multiday / new_keys, 1) if new_keys else 0.0)
            else:
                out["new_keys"] = 0

            # No identified, de-looped traffic in window → nothing to report.
            if not out.get("new_keys"):
                return {} if own_conn else {"window_days": days, "new_keys": 0}

            # ── (3) reuse buckets: calls-per-key 1 / 2-5 / 6+ ──
            rows = run(base_cte + """
                , per_key AS (
                    SELECT api_key, COUNT(*) AS cnt FROM base GROUP BY api_key
                )
                SELECT
                    COUNT(*) FILTER (WHERE cnt = 1)            AS one_off,
                    COUNT(*) FILTER (WHERE cnt BETWEEN 2 AND 5) AS light,
                    COUNT(*) FILTER (WHERE cnt >= 6)           AS heavy
                FROM per_key
            """)
            if rows and rows[0]:
                out["reuse_buckets"] = {
                    "1": int(rows[0][0] or 0),
                    "2-5": int(rows[0][1] or 0),
                    "6+": int(rows[0][2] or 0),
                }

            # ── (4) first-tool mix ──
            new_keys = out["new_keys"]
            rows = run(base_cte + """
                SELECT first_tool, COUNT(DISTINCT api_key) AS keys
                FROM firsts
                WHERE first_tool IS NOT NULL
                GROUP BY first_tool
                ORDER BY keys DESC, first_tool ASC
                LIMIT 20
            """)
            out["first_tool_mix"] = [
                {
                    "tool": r[0],
                    "count": int(r[1] or 0),
                    "pct": round(100.0 * int(r[1] or 0) / new_keys, 1) if new_keys else 0.0,
                }
                for r in (rows or [])
            ]

            # ── (5cont) retention by first tool ──
            # first_callers = DISTINCT keys whose first tool was T; returned =
            # of those, how many made a 2nd call. return_rate = returned /
            # first_callers (distinct-returners / distinct-first-callers).
            rows = run(base_cte + """
                SELECT
                    f.first_tool,
                    COUNT(DISTINCT f.api_key)                          AS first_callers,
                    COUNT(DISTINCT f.api_key) FILTER (
                        WHERE EXISTS (
                            SELECT 1 FROM base b
                            WHERE b.api_key = f.api_key
                              AND b.timestamp > f.first_ts))           AS returned
                FROM firsts f
                WHERE f.first_tool IS NOT NULL
                GROUP BY f.first_tool
                HAVING COUNT(DISTINCT f.api_key) > 0
                ORDER BY (COUNT(DISTINCT f.api_key) FILTER (
                            WHERE EXISTS (SELECT 1 FROM base b
                                          WHERE b.api_key = f.api_key
                                            AND b.timestamp > f.first_ts))::float
                          / NULLIF(COUNT(DISTINCT f.api_key), 0)) DESC,
                         first_callers DESC
                LIMIT 20
            """)
            out["retention_by_first_tool"] = [
                {
                    "tool": r[0],
                    "first_callers": int(r[1] or 0),
                    "returned": int(r[2] or 0),
                    "return_rate": round(100.0 * int(r[2] or 0) / int(r[1]), 1) if r[1] else 0.0,
                }
                for r in (rows or [])
            ]

            # ── (6) time-to-2nd-call: median + p95 seconds ──
            rows = run(base_cte + """
                , seconds AS (
                    SELECT f.api_key,
                           EXTRACT(EPOCH FROM (
                               MIN(b.timestamp) - f.first_ts)) AS secs
                    FROM firsts f
                    JOIN base b
                      ON b.api_key = f.api_key
                     AND b.timestamp > f.first_ts
                    GROUP BY f.api_key, f.first_ts
                )
                SELECT
                    PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY secs) AS median_s,
                    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY secs) AS p95_s,
                    COUNT(*) AS n
                FROM seconds
            """)
            if rows and rows[0] and rows[0][2]:
                out["median_time_to_2nd_call_seconds"] = (
                    round(float(rows[0][0]), 1) if rows[0][0] is not None else None)
                out["p95_time_to_2nd_call_seconds"] = (
                    round(float(rows[0][1]), 1) if rows[0][1] is not None else None)
            else:
                out["median_time_to_2nd_call_seconds"] = None
                out["p95_time_to_2nd_call_seconds"] = None

        return out
    except Exception as e:
        logger.warning("retention_cohorts: compute failed: %s", e)
        return {}
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


def retention_cohort_evidence(days: int = 30) -> list[dict]:
    """Adapt compute_retention_cohorts() into brain-investigator evidence items
    ({claim, source, value}). CONCISE + CAPPED so it never bloats the evidence
    block. Best-effort: [] when there is no data. NEVER raises.

    This is the function brain_investigator.gather_evidence() calls as a new
    retention Source so the next retention investigate() reasons on REAL,
    de-looped, DISTINCT-key funnel data instead of a 0.2-confidence guess."""
    try:
        c = compute_retention_cohorts(days=days)
    except Exception as e:
        logger.warning("retention_cohorts: evidence adapter failed: %s", e)
        return []
    if not c or not c.get("new_keys"):
        return []

    src = "mcp_call_log (DISTINCT api_key, de-looped via mcp_calls_deloop.PROBE_PLATFORMS)"
    items: list[dict] = []

    items.append({
        "claim": f"Distinct NEW MCP api_keys (first call) in last {c['window_days']}d",
        "source": src,
        "value": c["new_keys"],
    })
    items.append({
        "claim": (f"Return rate = distinct keys making a 2nd call / distinct new "
                  f"keys ({c.get('returned_keys', 0)}/{c['new_keys']}), {c['window_days']}d"),
        "source": src,
        "value": f"{c.get('return_rate', 0.0)}%",
    })
    if "multiday_rate" in c:
        items.append({
            "claim": (f"Multi-day return rate = keys with a call on a 2nd distinct "
                      f"day / new keys ({c.get('multiday_returned_keys', 0)}/{c['new_keys']})"),
            "source": src,
            "value": f"{c['multiday_rate']}%",
        })
    rb = c.get("reuse_buckets") or {}
    if rb:
        tot = (rb.get("1", 0) + rb.get("2-5", 0) + rb.get("6+", 0)) or 1
        items.append({
            "claim": (f"Key reuse distribution {c['window_days']}d: {rb.get('1', 0)} one-off "
                      f"(1 call), {rb.get('2-5', 0)} light (2-5), {rb.get('6+', 0)} heavy (6+)"),
            "source": "mcp_call_log (api_key call-count distribution)",
            "value": (f"{round(100.0 * rb.get('1', 0) / tot, 1)}% one-off / "
                      f"{round(100.0 * rb.get('2-5', 0) / tot, 1)}% light / "
                      f"{round(100.0 * rb.get('6+', 0) / tot, 1)}% heavy"),
        })
    # Top 3 first-tools + their return rates (the "which tool retains?" signal).
    for row in (c.get("retention_by_first_tool") or [])[:3]:
        items.append({
            "claim": (f"Retention for keys whose FIRST tool was {row['tool']}: "
                      f"{row['returned']}/{row['first_callers']} returned"),
            "source": "mcp_call_log (DISTINCT api_key, first tool, de-looped)",
            "value": f"{row['return_rate']}% return rate",
        })
    med = c.get("median_time_to_2nd_call_seconds")
    if med is not None:
        items.append({
            "claim": "Median time-to-2nd-call (keys that returned)",
            "source": src,
            "value": f"{round(med / 3600.0, 1)}h ({med}s)",
        })
    return items


# ── Endpoint (admin-gated) ──────────────────────────────────────────────
def _admin_ok() -> bool:
    """Reuse the brain mechanical classifier's admin gate so the gate stays in
    one place; fall back to an inline internal-key check so the endpoint is
    NEVER accidentally left open."""
    try:
        from routes.brain_mechanical_classifier import _admin_ok as _mech_admin_ok
        return bool(_mech_admin_ok())
    except Exception:
        keys = set()
        for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
            v = os.environ.get(_n)
            if v:
                keys.add(v)
        sent = (request.headers.get("X-Internal-Key")
                or request.headers.get("X-Admin-Key")
                or request.args.get("admin_key") or "").strip()
        return bool(sent) and sent in keys


@retention_cohorts_bp.get("/api/v1/mcp/retention/cohorts")
def get_retention_cohorts():
    """Admin-gated key-based retention cohort summary for ops. Returns the same
    measured cohorts that feed the brain investigator. Optional ?days=30."""
    if not _admin_ok():
        return jsonify(ok=False, error="admin only",
                       hint="X-Admin-Key / X-Internal-Key header required"), 403
    days = _clamp_days(request.args.get("days") or 30)
    try:
        cohorts = compute_retention_cohorts(days=days)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 500
    if not cohorts:
        return jsonify(ok=True, days=days, cohorts={},
                       note="no de-looped identified traffic in window"), 200
    return jsonify(ok=True, days=days, cohorts=cohorts,
                   evidence=retention_cohort_evidence(days=days)), 200


def register_retention_cohorts(app) -> None:
    """Idempotent registration helper for main.py."""
    try:
        app.register_blueprint(retention_cohorts_bp)
    except Exception as e:
        logger.warning("retention_cohorts: blueprint registration failed: %s", e)
