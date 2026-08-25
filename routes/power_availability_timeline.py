"""power_availability_timeline.py — WHEN does power get easier here? (2026-07-30)

The brain's most-endorsed proposal of the 07-30 digest (six independent drafts,
every adversary pass: "build it"): power-delivery TIMING is the #1 unanswered
question in data-center site selection — "where is power cheap/abundant today"
is answered by rank_markets / get_grid_data; "when will power be deliverable
tomorrow" is what procurement actually asks, and LLM training data cannot hold
it (queues and moratoriums move weekly).

WHAT THIS v1 IS — and the honesty line it will not cross:
Composes THREE dated lanes we already own, per US state, into a year-by-year
timeline of supply-side signals:
  · planned_generators (EIA-860M monthly): new capacity by planned online
    year, split by CONFIDENCE CLASS — under_construction (U/V) vs planned
    (P/L/T) vs testing (TS). Never blended: mixing them into one number would
    smuggle certainty.
  · generator_retirements (EIA-860M): scheduled retirements by year — the
    subtractions.
  · interconnect_queue (LBNL): active queue depth as CONGESTION CONTEXT only.
    The feed carries no commercial-operation dates, and most queued MW
    historically never completes — so the queue lane never contributes a
    delivery date, only pressure.

★ GENERATION ≠ DELIVERABLE LOAD. New supply coming online is a signal that
power gets EASIER, not a promise that a data center can energize N MW — load
interconnection is a separate utility process this data does not see. The
payload says this in constraint_coverage, names what else it cannot know
(utility study timelines, large-load tariff processes, substation-level
delivery, PPA availability), and the MCP tool's description carries the same
frame. Competitors sell this question behind paywalls by overclaiming it;
the moat here is answering the answerable part with dated, sourced numbers
and DECLARING the rest.

House rules: definition_version + changelog on the payload; per-lane source
vintages; rates/na values never fabricated; state grain (a state can span
ISOs — ISO membership is reported as context, never guessed; the taxonomy's
fail-open default is deliberately NOT used here).

Endpoint (public, keyless, cached in-process 15m):
  GET /api/v1/power/availability-timeline?state=TX[&years=5][&mw=200]
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

# Same dead-status verdict the retirement-headroom scan uses — imported, not
# copied (a second list is how /claim-class drifts start).
from routes.retirement_headroom import _DEAD_STATUS_RE
from util.iso_taxonomy import STATE_ISO
# ★ Fail-soft, matching this module's own `try: import requests` convention.
# tests/test_cross_layer_public_reason_hygiene.py execs the route with ALL
# first-party imports BLOCKED so every degraded branch is deterministic — a
# hard top-level import breaks that harness. When the helper is unavailable we
# publish "unknown", which the vocabulary already defines as "read it
# defensively". An absent-or-honest shape beats a guessed one.
try:
    from util.constraint_coverage_shape import shape_of as _shape_of
except Exception:                                    # pragma: no cover
    _shape_of = None


def _cc_shape(value):
    return _shape_of(value) if _shape_of else "unknown"

logger = logging.getLogger("power_availability_timeline")
power_availability_timeline_bp = Blueprint("power_availability_timeline", __name__)

DEFINITION_VERSION = 1
DEFINITION_CHANGELOG = {
    1: "initial — state grain; three dated lanes (EIA-860M planned generators "
       "by confidence class, EIA-860M scheduled retirements, LBNL queue depth "
       "as congestion context with NO dates); supply-side signals only, "
       "generation≠deliverable-load declared; utility study timelines / "
       "large-load tariff processes / substation-level delivery / PPA "
       "availability named as out of coverage",
}

_STMT_TIMEOUT_MS = 6000
_MAX_YEARS = 6

# EIA-860M status → confidence class. U/V are under construction, P/L/T are
# planned (permitting stages), TS is testing/startup.
#
# ★ 3rd live landmine of this endpoint's first hour, and the worst: LIVE
# status values are PARENTHESIZED long strings — "(U) Under construction,
# less than…" — so a bare ILIKE 'U%' matched NOTHING and every megawatt fell
# silently into other_mw (VA showed 0 under-construction while the raw feed
# carried CVOW's 2,640 MW '(U)' row). The gateway's own pipeline handler
# strips this prefix (server.mjs: replace(/^\\([A-Za-z]+\\)\\s*/, '')) —
# extract the code the same way here. TS must be matched before bare T.
_STATUS_CLASS_SQL = r"""
CASE UPPER(SUBSTRING(COALESCE(status,'') FROM '^\(?([A-Za-z]{1,2})'))
  WHEN 'U' THEN 'under_construction'
  WHEN 'V' THEN 'under_construction'
  WHEN 'TS' THEN 'testing'
  WHEN 'P' THEN 'planned'
  WHEN 'L' THEN 'planned'
  WHEN 'T' THEN 'planned'
  ELSE 'other'
END
"""

CONSTRAINT_COVERAGE = [
    "generation ≠ deliverable load: new supply is a signal that power gets "
    "easier in a region, never a promise that a specific data-center load can "
    "energize — load interconnection is a separate utility process this data "
    "does not see",
    "utility-specific study timelines (e.g. multi-year cluster studies) are "
    "not in these feeds and are not estimated",
    "large-load tariff processes and moratoria (ERCOT large-load rules, "
    "utility-by-utility) are not tracked here",
    "substation-level deliverability is not derivable at state grain — "
    "get_hosting_capacity covers select utilities' published capacity maps",
    "the interconnection queue feed carries no commercial-operation dates; "
    "most queued MW historically never completes, so queue depth is reported "
    "as congestion pressure only and never contributes a delivery date",
    "PPA availability and pricing are out of scope",
]


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


def _bounded(cur, sql, params=None, fetch="all"):
    """One aggregate per explicit transaction with SET LOCAL statement_timeout
    (the only form that sticks on Neon's pooled endpoint). ROLLBACK on error so
    a timed-out query never poisons the next one."""
    cur.execute("BEGIN")
    try:
        cur.execute("SET LOCAL statement_timeout = %d" % _STMT_TIMEOUT_MS)
        cur.execute(sql, params or ())
        result = cur.fetchone() if fetch == "one" else cur.fetchall()
        cur.execute("COMMIT")
        return result
    except Exception:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        raise


def _build(state: str, years: int, mw) -> dict:
    now = datetime.now(timezone.utc)
    this_year = now.year
    out = {
        "ok": True,
        "entity": "power_availability_timeline",
        "state": state,
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "definition_version": DEFINITION_VERSION,
        "definition_changelog": DEFINITION_CHANGELOG,
        "frame": "supply-side timing signals — when power gets EASIER here, "
                 "by year and confidence class. NOT a load-interconnection "
                 "promise; read constraint_coverage before citing.",
        # A state can span ISOs (TX is ERCOT+SPP+MISO+WECC edges) — report
        # membership as context, never resolve to one. The taxonomy's
        # fail-open default is deliberately not consulted here.
        "iso_context": sorted(STATE_ISO.get(state, []))
                       if isinstance(STATE_ISO.get(state), (list, tuple, set))
                       else ([STATE_ISO[state]] if STATE_ISO.get(state) else []),
        "window_years": years,
        "constraint_coverage": CONSTRAINT_COVERAGE,
        # ★2026-08-25: DERIVED from the value being sent, never a literal —
        # a hardcoded label is a second thing to keep in sync, and the whole
        # 08-25 sweep was labels that had drifted from their payload.
        "constraint_coverage_shape": _cc_shape(CONSTRAINT_COVERAGE),
        "companions": {
            "now": "get_grid_intelligence — live headroom & telemetry for the "
                   "operator serving this state",
            "survivors": "get_refined_queue — queue entries that cleared "
                         "refinement, the closest thing to a survival read",
            "substation_grain": "get_hosting_capacity — published utility "
                                "capacity maps where they exist",
        },
        "sources": {},
    }
    if mw is not None:
        out["mw_context"] = {
            "requested_mw": mw,
            "note": "reported for context only — this endpoint does not and "
                    "cannot state when a specific load of this size can "
                    "energize (see constraint_coverage)",
        }

    c = _conn()
    if c is None:
        out["ok"] = False
        out["error"] = "no database connection"
        return out
    try:
        with c.cursor() as cur:
            # ── dated new supply, by confidence class and year ─────────────
            rows = _bounded(cur, f"""
                SELECT planned_year::int AS yr, ({_STATUS_CLASS_SQL}) AS cls,
                       ROUND(SUM(capacity_mw))::float AS mw, COUNT(*) AS units
                  FROM planned_generators
                 WHERE state = %s
                   AND planned_year IS NOT NULL
                   AND planned_year BETWEEN %s AND %s
                 GROUP BY 1, 2
              """, (state, this_year, this_year + years - 1))
            by_year: dict[int, dict] = {}
            for yr, cls, mw_sum, units in rows or []:
                y = by_year.setdefault(int(yr), {
                    "year": int(yr),
                    "under_construction_mw": 0.0, "planned_mw": 0.0,
                    "testing_mw": 0.0, "other_mw": 0.0,
                    "retiring_mw": 0.0, "units": 0,
                })
                y[f"{cls}_mw"] = float(mw_sum or 0)
                y["units"] += int(units or 0)

            # ── scheduled retirements (the subtractions) ───────────────────
            rows = _bounded(cur, """
                SELECT EXTRACT(YEAR FROM retirement_date)::int AS yr,
                       ROUND(SUM(capacity_mw))::float
                  FROM generator_retirements
                 WHERE state = %s
                   AND status = 'planned_retirement'
                   AND retirement_date >= CURRENT_DATE
                   AND EXTRACT(YEAR FROM retirement_date) < %s
                 GROUP BY 1
              """, (state, this_year + years))
            for yr, mw_sum in rows or []:
                y = by_year.setdefault(int(yr), {
                    "year": int(yr),
                    "under_construction_mw": 0.0, "planned_mw": 0.0,
                    "testing_mw": 0.0, "other_mw": 0.0,
                    "retiring_mw": 0.0, "units": 0,
                })
                y["retiring_mw"] = float(mw_sum or 0)

            timeline = [by_year[y] for y in sorted(by_year)]
            cum = 0.0
            for y in timeline:
                # The one derived number, and it is deliberately conservative:
                # cumulative under-construction+testing MINUS scheduled
                # retirements. Speculative 'planned' capacity is NEVER in it —
                # blending confidence classes is how timing gets overclaimed.
                cum += y["under_construction_mw"] + y["testing_mw"] - y["retiring_mw"]
                y["cumulative_firm_signal_mw"] = round(cum, 1)
            out["timeline"] = timeline
            out["timeline_note"] = (
                "cumulative_firm_signal_mw = running (under_construction + "
                "testing − retirements). 'planned' capacity is shown but never "
                "folded in — permitting-stage projects are speculative."
            )

            # ── queue congestion context (NO dates by design) ──────────────
            # PERCENTILE_CONT accepts numeric/interval, never DATE — the raw
            # ORDER BY queue_date form 503'd every live call while pure-function
            # CI stayed green (only a live Postgres can reject it). Median via
            # epoch, cast back to a date.
            row = _bounded(cur, """
                SELECT COALESCE(SUM(capacity_mw),0)::float, COUNT(*),
                       MIN(queue_date),
                       to_timestamp(PERCENTILE_CONT(0.5) WITHIN GROUP (
                         ORDER BY EXTRACT(EPOCH FROM queue_date)))::date
                  FROM interconnect_queue
                 WHERE state = %s
                   AND COALESCE(queue_status,'') !~* %s
              """, (state, _DEAD_STATUS_RE), fetch="one")
            qmw, qn, oldest, median = row or (0, 0, None, None)
            out["queue_context"] = {
                "active_mw": round(float(qmw or 0), 1),
                "active_projects": int(qn or 0),
                "oldest_entry": oldest.isoformat() if oldest else None,
                "median_entry": median.isoformat() if median else None,
                "caveat": "congestion pressure only — this feed has no "
                          "commercial-operation dates and most queued MW "
                          "historically never completes",
            }

            # ── per-lane vintages — a timeline without dates on its own data
            #    would be the exact overclaim this tool exists to avoid.
            # Each lookup is individually fail-soft: a missing vintage renders
            # null, it must never take the timeline down. Live-schema lesson
            # (2nd landmine of this endpoint's first hour): the LIVE
            # interconnect_queue predates the repo DDL's created_at column —
            # LIVE ≠ repo DDL, the power_plants class — so queue recency reads
            # MAX(queue_date) (proven live) and is LABELLED as what it is.
            def _vintage(sql):
                try:
                    r = _bounded(cur, sql, fetch="one")
                    return r[0] if r else None
                except Exception as ve:
                    logger.warning("[power-timeline] vintage: %s", str(ve)[:120])
                    return None

            v1 = _vintage("SELECT MAX(ingested_at) FROM planned_generators")
            v2 = _vintage("SELECT MAX(source_month) FROM generator_retirements")
            v3 = _vintage("SELECT MAX(queue_date) FROM interconnect_queue")
            out["sources"] = {
                "planned_generators": {
                    "feed": "EIA-860M monthly (planned + under-construction "
                            "generators)",
                    "vintage": v1.isoformat() if v1 else None},
                "generator_retirements": {
                    "feed": "EIA-860M planned retirements",
                    "vintage": str(v2) if v2 else None},
                "interconnect_queue": {
                    "feed": "LBNL interconnection queue",
                    "latest_queue_entry": v3.isoformat() if v3 else None,
                    "vintage_note": "recency shown as the newest queue-entry "
                                    "date the feed carries"},
            }
            if not timeline:
                out["empty_reason"] = (
                    "no dated planned generation or scheduled retirements for "
                    "this state inside the window — a real absence from the "
                    "feeds, not a placeholder"
                )
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)[:200]
        logger.warning("[power-timeline] %s: %s", state, str(e)[:200])
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


# In-process cache: public + DB = stampede risk (the sitemap lesson). Keyed by
# (state, years, mw-bucket); 15 minutes matches the source cadence (monthly
# feeds — anything fresher is theater).
_CACHE: dict = {}
_CACHE_TTL_S = 900
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 256


@power_availability_timeline_bp.route("/api/v1/power/availability-timeline",
                                      methods=["GET"])
def power_availability_timeline():
    state = (request.args.get("state") or "").strip().upper()[:2]
    if not state or len(state) != 2 or not state.isalpha():
        return jsonify(ok=False, error="state (2-letter US code) is required",
                       example="/api/v1/power/availability-timeline?state=TX"), 400
    try:
        years = max(1, min(_MAX_YEARS, int(request.args.get("years", 5))))
    except Exception:
        years = 5
    mw = None
    try:
        if request.args.get("mw"):
            mw = max(1, min(100000, int(float(request.args.get("mw")))))
    except Exception:
        mw = None

    key = (state, years, mw)
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _CACHE_TTL_S:
            payload = dict(hit[1])
            payload["cache_age_s"] = round(now - hit[0], 1)
            return jsonify(payload), 200, {"Cache-Control": "public, max-age=300"}
    data = _build(state, years, mw)
    if data.get("ok"):
        with _CACHE_LOCK:
            if len(_CACHE) >= _CACHE_MAX:
                _CACHE.clear()
            _CACHE[key] = (time.time(), data)
        return jsonify(data), 200, {"Cache-Control": "public, max-age=300"}
    return jsonify(data), 503
