"""routes/white_glove_agent.py — the white-glove AGENT (2026-08-29).

WHY THIS EXISTS
===============
"White glove" was two disconnected jobs and no agent:

  · routes/white_glove_propagation.py  — keeps the copy on 16 registry
    listings numerically honest (MAINTENANCE of presence we already have).
  · routes/customer_white_glove.py     — classifies paying customers into
    lifecycle stages (MAINTENANCE of customers we already have).

Nobody owned the question the business actually asks every week: *are we
onboarding — new MCP registries, new agents, fresh domain content, live
partner motion, welcomed users — on a consistent cadence?* That question
was answered ad hoc, re-derived from scratch each time, and drifted.

This module is that owner. ONE agent, SIX lanes, one report to the brain.

WHAT IT IS NOT
--------------
It does not re-measure anything the two modules above already measure, and
it does not crawl. Every lane is a **pure DB read of persisted verdicts**,
preserving the no-self-request invariant that caused the 2026-07-06
flywheel outage. Acquisition scanning stays in registry_acquisition;
listing verdicts stay in registry_truth; lifecycle staging stays in
customer_white_glove. This agent READS their output and grades cadence.

THE FOUR-STATE VERDICT — "could not check" is never "ok"
--------------------------------------------------------
The registry loop regressed for months because `drift_detected` was a
BOOLEAN, and a boolean cannot say "I could not look" — 11 of 16 unreadable
listings all recorded FALSE and the board called them clean. Every lane
here returns one of FOUR states and the uncheckable one is its own:

  ok           measured, inside the cadence SLA
  off_cadence  measured, behind the SLA but the lane is still moving
  stalled      measured, and nothing has moved at all in the stall window
  unknown      COULD NOT MEASURE (missing table, timeout, error)

★ `unknown` never resolves a finding and never opens the lane's own
  finding. It opens a distinct `white_glove_lane_unmeasured` finding, so a
  blind lane is visible AS blind instead of silently reading healthy.

POOL SAFETY
-----------
Each lane runs under `SET LOCAL statement_timeout` (LANE_TIMEOUT_MS). A
lane that times out degrades to `unknown` — it never holds a connection
open against the primary pool, which has saturated at 80 before under
crawler read load.

Surfaces
--------
  GET  /api/v1/admin/white-glove/agent          pure-DB read of last run
  POST /api/v1/admin/white-glove/agent/run      run all lanes + report
Kill switch: WHITE_GLOVE_AGENT_DISABLE=1

All brain writes go through routes.brain_findings_writer.upsert_brain_finding
(the live table has no UNIQUE(issue,url); hand-rolled ON CONFLICT fails
silently — 10 files already made that mistake).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

white_glove_agent_bp = Blueprint("white_glove_agent", __name__)

KILL_SWITCH_ENV = "WHITE_GLOVE_AGENT_DISABLE"
LANE_TIMEOUT_MS = 8000

# ── Cadence SLAs ──────────────────────────────────────────────────────
# Each is the answer to "how long may this lane go without moving before
# the business should be told?" They are deliberately generous: this agent
# reports drift in ONBOARDING RATE, not in any single day's output.
SLA = {
    # A tracked listing that is broken, or unverified for longer than this,
    # is presence we believe we have and do not.
    "registry_presence_unverified_days": 7,
    # An absent directory sitting in the submission queue this long means
    # acquisition has stopped converting. (Measured 2026-08-29: the one
    # queued item had been waiting 32 days.)
    "registry_acquisition_queue_days": 14,
    # Discovery itself is weekly (Mondays). Two missed cycles = stalled.
    "registry_acquisition_scan_days": 21,
    # New distinct agents must appear at least this often.
    "agent_onboarding_window_days": 7,
    # Data-center / energy content freshness.
    "content_cadence_hours": 48,
    # Partner + directory outreach motion.
    "partner_outreach_days": 14,
    # A payer stranded (paid, zero calls) longer than this is an
    # unwelcomed user, which is the most expensive kind.
    "user_welcome_stranded_days": 14,
}

VERDICT_OK = "ok"
VERDICT_OFF = "off_cadence"
VERDICT_STALLED = "stalled"
VERDICT_UNKNOWN = "unknown"

_ACTIONABLE = (VERDICT_OFF, VERDICT_STALLED)


# ── DB helpers ────────────────────────────────────────────────────────
def _db_conn():
    """Reuse the presence crawler's connector (single connect policy)."""
    try:
        from routes.mcp_presence_crawler import _db_conn as _c
        return _c()
    except Exception:
        return None


def _table_exists(cur, name: str) -> bool:
    """to_regclass guard. A lane whose table is absent reports `unknown`,
    never `ok` — an UndefinedTable inside a bare except is how a lane goes
    permanently, invisibly dead."""
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",))
    row = cur.fetchone()
    return bool(row and row[0])


def _guarded(fn):
    """Run one lane under a statement timeout in its own SAVEPOINT.

    Any failure — timeout, missing column, bad cast — becomes `unknown`
    for that lane only, and rolls back just itself so the next lane still
    sees a usable transaction."""
    def wrapper(cur, now, lane_name):
        try:
            cur.execute("SAVEPOINT wg_lane")
            cur.execute(f"SET LOCAL statement_timeout = {LANE_TIMEOUT_MS}")
            out = fn(cur, now)
            cur.execute("RELEASE SAVEPOINT wg_lane")
            out["lane"] = lane_name
            return out
        except Exception as e:
            try:
                cur.execute("ROLLBACK TO SAVEPOINT wg_lane")
                cur.execute("RELEASE SAVEPOINT wg_lane")
            except Exception:
                pass
            return {"lane": lane_name, "verdict": VERDICT_UNKNOWN,
                    "observed": {}, "detail": f"could not measure: {str(e)[:160]}"}
    return wrapper


# ── Lane 1: registry presence (maintenance) ───────────────────────────
@_guarded
def _lane_registry_presence(cur, now):
    if not _table_exists(cur, "mcp_presence_listings"):
        return {"verdict": VERDICT_UNKNOWN, "observed": {},
                "detail": "mcp_presence_listings absent"}
    # One exclusion clause, built once and used by BOTH queries below.
    # Counting verdicts over the live set while ageing unverified rows over
    # the full set would grade two different populations.
    excl = ""
    if _table_exists(cur, "mcp_registry_defunct"):
        excl = ("registry_name NOT IN "
                "(SELECT registry_name FROM mcp_registry_defunct)")

    where_live = f"WHERE {excl}" if excl else ""
    cur.execute("SELECT verdict, COUNT(*) FROM mcp_presence_listings "
                f"{where_live} GROUP BY verdict")
    counts = {(v or "null"): n for v, n in cur.fetchall()}
    tracked = sum(counts.values())
    broken = counts.get("broken", 0)

    stale_clause = "verdict = 'unverified' AND checked_at < %s"
    if excl:
        stale_clause = f"{excl} AND {stale_clause}"
    cur.execute("SELECT COUNT(*) FROM mcp_presence_listings "
                f"WHERE {stale_clause}",
                (now - _days(SLA["registry_presence_unverified_days"]),))
    stale_unverified = cur.fetchone()[0] or 0

    observed = {"tracked": tracked, "verdicts": counts,
                "broken": broken, "stale_unverified": stale_unverified}
    if tracked == 0:
        return {"verdict": VERDICT_UNKNOWN, "observed": observed,
                "detail": "no listings tracked — nothing to grade"}
    if broken or stale_unverified:
        return {"verdict": VERDICT_OFF, "observed": observed,
                "detail": (f"{broken} listing(s) resolve to NOT-our-page and "
                           f"{stale_unverified} have been unreadable for over "
                           f"{SLA['registry_presence_unverified_days']}d, out of "
                           f"{tracked} tracked. Presence we believe we have "
                           f"and measurably do not.")}
    return {"verdict": VERDICT_OK, "observed": observed,
            "detail": f"all {tracked} tracked listings verified as ours"}


# ── Lane 2: registry acquisition (growth) ─────────────────────────────
@_guarded
def _lane_registry_acquisition(cur, now):
    if not _table_exists(cur, "registry_acquisition_candidates"):
        return {"verdict": VERDICT_UNKNOWN, "observed": {},
                "detail": "registry_acquisition_candidates absent — "
                          "the acquisition scan has never persisted a row"}
    cur.execute(
        "SELECT COUNT(*) FILTER (WHERE verdict = 'absent'), "
        "       MIN(first_absent_at) FILTER (WHERE verdict = 'absent'), "
        "       MAX(checked_at), COUNT(*) "
        "  FROM registry_acquisition_candidates")
    queued, oldest_absent, last_scan, pool = cur.fetchone()
    queued = queued or 0
    observed = {"queue_depth": queued, "candidate_pool": pool or 0,
                "oldest_absent_at": _iso(oldest_absent),
                "last_scan_at": _iso(last_scan),
                "queue_age_days": _age_days(oldest_absent, now),
                "scan_age_days": _age_days(last_scan, now)}

    if last_scan is None:
        return {"verdict": VERDICT_UNKNOWN, "observed": observed,
                "detail": "no candidate has ever been scanned"}

    scan_age = _age_days(last_scan, now)
    if scan_age is not None and scan_age > SLA["registry_acquisition_scan_days"]:
        return {"verdict": VERDICT_STALLED, "observed": observed,
                "detail": (f"acquisition discovery has not scanned in "
                           f"{scan_age:.0f}d (SLA "
                           f"{SLA['registry_acquisition_scan_days']}d). "
                           f"Maintenance keeps {pool} candidates from rotting; "
                           f"only acquisition grows presence, and directories "
                           f"are ~84% of new agent arrivals.")}

    queue_age = _age_days(oldest_absent, now)
    if queued and queue_age is not None and \
            queue_age > SLA["registry_acquisition_queue_days"]:
        return {"verdict": VERDICT_OFF, "observed": observed,
                "detail": (f"{queued} directory/directories confirmed absent "
                           f"and submittable, oldest waiting {queue_age:.0f}d "
                           f"(SLA {SLA['registry_acquisition_queue_days']}d). "
                           f"The queue converts to presence only when someone "
                           f"submits; nothing in the loop does.")}
    return {"verdict": VERDICT_OK, "observed": observed,
            "detail": (f"acquisition scanned {scan_age:.0f}d ago; "
                       f"{queued} item(s) queued inside SLA")}


# ── Lane 3: agent onboarding ──────────────────────────────────────────
@_guarded
def _lane_agent_onboarding(cur, now):
    if not _table_exists(cur, "mcp_call_log"):
        return {"verdict": VERDICT_UNKNOWN, "observed": {},
                "detail": "mcp_call_log absent"}
    win = SLA["agent_onboarding_window_days"]
    # Identity unit is the API KEY, not the session — session_id rotates
    # per connection (~1.2 calls/session), so a session-scoped count
    # flatters every rate by construction.
    cur.execute(
        "WITH recent AS ("
        "  SELECT DISTINCT api_key FROM mcp_call_log "
        "   WHERE timestamp > %s AND api_key IS NOT NULL AND api_key <> ''"
        ") "
        "SELECT COUNT(*) FROM recent", (now - _days(win),))
    active = cur.fetchone()[0] or 0
    cur.execute(
        "WITH recent AS ("
        "  SELECT DISTINCT api_key FROM mcp_call_log "
        "   WHERE timestamp > %s AND api_key IS NOT NULL AND api_key <> ''"
        ") "
        "SELECT COUNT(*) FROM ("
        "  SELECT r.api_key FROM recent r "
        "   GROUP BY r.api_key "
        "  HAVING (SELECT MIN(m.timestamp) FROM mcp_call_log m "
        "           WHERE m.api_key = r.api_key) > %s"
        ") t", (now - _days(win), now - _days(win)))
    newly = cur.fetchone()[0] or 0
    observed = {"active_agents": active, "newly_onboarded": newly,
                "window_days": win}
    if newly == 0:
        return {"verdict": VERDICT_STALLED, "observed": observed,
                "detail": (f"zero NEW agents made a first call in {win}d "
                           f"({active} keys active overall). Onboarding has "
                           f"stopped; retention is carrying the number.")}
    return {"verdict": VERDICT_OK, "observed": observed,
            "detail": f"{newly} new agent(s) first-called in {win}d "
                      f"({active} active)"}


# ── Lane 4: data-center + energy content cadence ──────────────────────
@_guarded
def _lane_content_cadence(cur, now):
    if not _table_exists(cur, "news"):
        return {"verdict": VERDICT_UNKNOWN, "observed": {},
                "detail": "news absent"}
    # published_date is TEXT on this table; the ::timestamptz cast is the
    # pattern already used in production (agent_winback_digest).
    cur.execute(
        "SELECT MAX(published_date::timestamptz) FROM news "
        " WHERE published_date IS NOT NULL AND published_date <> '' "
        "   AND published_date::timestamptz <= NOW()")
    newest = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM news "
        " WHERE published_date IS NOT NULL AND published_date <> '' "
        "   AND published_date::timestamptz > %s "
        "   AND published_date::timestamptz <= NOW()",
        (now - _days(7),))
    last7 = cur.fetchone()[0] or 0
    age_h = None
    if newest:
        age_h = (now - _aware(newest)).total_seconds() / 3600.0
    observed = {"newest_published_at": _iso(newest),
                "age_hours": round(age_h, 1) if age_h is not None else None,
                "published_last_7d": last7}
    if newest is None:
        return {"verdict": VERDICT_UNKNOWN, "observed": observed,
                "detail": "no dated content rows"}
    if age_h > SLA["content_cadence_hours"]:
        return {"verdict": VERDICT_OFF, "observed": observed,
                "detail": (f"newest data-center/energy item is {age_h:.0f}h old "
                           f"(SLA {SLA['content_cadence_hours']}h); "
                           f"{last7} published in 7d")}
    return {"verdict": VERDICT_OK, "observed": observed,
            "detail": f"newest item {age_h:.0f}h old; {last7} published in 7d"}


# ── Lane 5: partner + directory outreach ──────────────────────────────
@_guarded
def _lane_partner_outreach(cur, now):
    table = next((t for t in ("mcp_outreach_log", "outreach_events",
                              "email_drip_log")
                  if _table_exists(cur, t)), None)
    if table is None:
        return {"verdict": VERDICT_UNKNOWN, "observed": {},
                "detail": "no outreach ledger table present"}
    tscol = "sent_at" if table == "email_drip_log" else "created_at"
    cur.execute(f"SELECT MAX({tscol}), COUNT(*) FROM {table} "
                f" WHERE {tscol} > %s", (now - _days(90),))
    newest, n90 = cur.fetchone()
    observed = {"ledger": table, "newest_at": _iso(newest),
                "events_90d": n90 or 0,
                "age_days": _age_days(newest, now)}
    if newest is None:
        return {"verdict": VERDICT_STALLED, "observed": observed,
                "detail": (f"outreach ledger `{table}` has no event in 90d — "
                           f"partner motion is not merely slow, it is absent")}
    age = _age_days(newest, now)
    if age > SLA["partner_outreach_days"]:
        return {"verdict": VERDICT_OFF, "observed": observed,
                "detail": (f"last outreach event {age:.0f}d ago on `{table}` "
                           f"(SLA {SLA['partner_outreach_days']}d)")}
    return {"verdict": VERDICT_OK, "observed": observed,
            "detail": f"last outreach {age:.0f}d ago; {n90 or 0} events in 90d"}


# ── Lane 6: new-user welcome ──────────────────────────────────────────
@_guarded
def _lane_user_welcome(cur, now):
    """Reads the engagement stage customer_white_glove PERSISTS. Composing
    its verdict, not re-deriving it, is deliberate — two derivations of
    'stranded' would drift apart within a month."""
    if not _table_exists(cur, "users"):
        return {"verdict": VERDICT_UNKNOWN, "observed": {},
                "detail": "users absent"}
    cur.execute("SELECT column_name FROM information_schema.columns "
                " WHERE table_name = 'users' AND column_name = 'engagement_stage'")
    if not cur.fetchone():
        return {"verdict": VERDICT_UNKNOWN, "observed": {},
                "detail": "users.engagement_stage absent — customer_white_glove "
                          "has never written a stage back"}
    cur.execute("SELECT engagement_stage, COUNT(*) FROM users "
                " WHERE engagement_stage IS NOT NULL AND engagement_stage <> '' "
                " GROUP BY engagement_stage")
    stages = {s: n for s, n in cur.fetchall()}
    stranded = stages.get("stranded", 0)
    staged = sum(stages.values())
    observed = {"stages": stages, "stranded": stranded, "staged_users": staged}
    if staged == 0:
        return {"verdict": VERDICT_UNKNOWN, "observed": observed,
                "detail": "no user carries an engagement stage"}
    if stranded:
        return {"verdict": VERDICT_OFF, "observed": observed,
                "detail": (f"{stranded} paying user(s) are STRANDED — paid, "
                           f"key works, zero calls ever. An unwelcomed payer "
                           f"is the most expensive kind of unonboarded user.")}
    return {"verdict": VERDICT_OK, "observed": observed,
            "detail": f"no stranded payers across {staged} staged users"}


LANES = (
    ("registry_presence", _lane_registry_presence),
    ("registry_acquisition", _lane_registry_acquisition),
    ("agent_onboarding", _lane_agent_onboarding),
    ("content_cadence", _lane_content_cadence),
    ("partner_outreach", _lane_partner_outreach),
    ("user_welcome", _lane_user_welcome),
)


# ── small helpers ─────────────────────────────────────────────────────
def _days(n):
    from datetime import timedelta
    return timedelta(days=n)


def _aware(ts):
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _iso(ts):
    ts = _aware(ts)
    return ts.isoformat() if ts else None


def _age_days(ts, now):
    ts = _aware(ts)
    return None if ts is None else (now - ts).total_seconds() / 86400.0


# ── Brain reporting ───────────────────────────────────────────────────
def _report_to_brain(cur, lanes: list[dict]) -> dict:
    """One finding per lane. `count` is the lane's headline magnitude —
    per-detector free-form by table convention, documented in `detail`."""
    from routes.brain_findings_writer import upsert_brain_finding
    written = {"open": 0, "resolved": 0, "unmeasured": 0}
    blind = []
    for ln in lanes:
        issue = f"white_glove_{ln['lane']}"
        url = f"https://dchub.cloud/admin/white-glove#{ln['lane']}"
        verdict = ln["verdict"]
        if verdict == VERDICT_UNKNOWN:
            # ★ Never resolve, never open the lane's own finding — a blind
            #   lane is reported AS blind. Resolving here is exactly the
            #   `drift_detected=FALSE` bug that hid 11 broken listings.
            blind.append(ln["lane"])
            written["unmeasured"] += 1
            continue
        detail = (f"[{verdict}] {ln['detail']} | observed="
                  f"{json.dumps(ln.get('observed', {}), default=str)}")
        if verdict in _ACTIONABLE:
            upsert_brain_finding(
                cur, issue=issue, url=url,
                count=int(ln.get("count_hint") or 1),
                detail=detail[:1900], detector="white_glove_agent",
                status="open")
            written["open"] += 1
        else:
            upsert_brain_finding(
                cur, issue=issue, url=url, count=1,
                detail=detail[:1900], detector="white_glove_agent",
                status="resolved")
            written["resolved"] += 1
    if blind:
        upsert_brain_finding(
            cur, issue="white_glove_lane_unmeasured",
            url="https://dchub.cloud/admin/white-glove",
            count=len(blind),
            detail=("White-glove lanes that could NOT be measured this run: "
                    + ", ".join(sorted(blind))
                    + ". These are reported as blind, never as healthy — a "
                      "lane that cannot answer must not read `ok`.")[:1900],
            detector="white_glove_agent", status="open")
    else:
        upsert_brain_finding(
            cur, issue="white_glove_lane_unmeasured",
            url="https://dchub.cloud/admin/white-glove", count=1,
            detail="Every white-glove lane measured successfully.",
            detector="white_glove_agent", status="resolved")
    return written


_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS white_glove_agent_runs (
    id          SERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dry_run     BOOLEAN NOT NULL DEFAULT FALSE,
    verdicts    JSONB,
    summary     JSONB
)
"""


def run_white_glove_agent(dry_run: bool = False) -> dict:
    """Run every lane, report to the brain, persist the run. Never raises."""
    now = datetime.now(timezone.utc)
    out = {"ok": True, "ran_at": now.isoformat(), "dry_run": dry_run,
           "lanes": [], "counts": {}, "brain": {}}
    if os.environ.get(KILL_SWITCH_ENV) == "1":
        out.update(ok=False, disabled=True,
                   reason=f"{KILL_SWITCH_ENV}=1")
        return out
    conn = _db_conn()
    if conn is None:
        # ★ No DB is not a healthy run with zero problems — it is a run
        #   that did not happen.
        out.update(ok=False, error="db_unavailable",
                   note="no lane was measured; this is NOT a clean result")
        return out
    try:
        with conn.cursor() as cur:
            for name, fn in LANES:
                out["lanes"].append(fn(cur, now, name))
        counts = {}
        for ln in out["lanes"]:
            counts[ln["verdict"]] = counts.get(ln["verdict"], 0) + 1
        out["counts"] = counts
        out["actionable"] = [l["lane"] for l in out["lanes"]
                             if l["verdict"] in _ACTIONABLE]
        out["blind"] = [l["lane"] for l in out["lanes"]
                        if l["verdict"] == VERDICT_UNKNOWN]
        if not dry_run:
            with conn.cursor() as cur:
                out["brain"] = _report_to_brain(cur, out["lanes"])
                cur.execute(_RUNS_DDL)
                cur.execute(
                    "INSERT INTO white_glove_agent_runs "
                    "(dry_run, verdicts, summary) VALUES (%s, %s, %s)",
                    (dry_run, json.dumps({l["lane"]: l["verdict"]
                                          for l in out["lanes"]}),
                     json.dumps({"counts": counts,
                                 "actionable": out["actionable"],
                                 "blind": out["blind"]})))
            conn.commit()
            out["persisted"] = True
    except Exception as e:
        logger.error("white_glove_agent run failed: %s", e, exc_info=True)
        out.update(ok=False, error=str(e)[:200])
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


# ── Routes ────────────────────────────────────────────────────────────
def _authorized() -> bool:
    try:
        from routes.mcp_presence_crawler import _admin_or_cron_authorized
        return _admin_or_cron_authorized()
    except Exception:
        provided = (request.headers.get("X-Admin-Key")
                    or request.args.get("admin_key") or "").strip()
        expected = (os.environ.get("DCHUB_ADMIN_KEY")
                    or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
        return bool(expected) and provided == expected


@white_glove_agent_bp.route("/api/v1/admin/white-glove/agent/run",
                            methods=["GET", "POST"])
def wg_agent_run():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    dry = request.args.get("dry_run", "0") in ("1", "true", "yes")
    return jsonify(run_white_glove_agent(dry_run=dry))


@white_glove_agent_bp.route("/api/v1/admin/white-glove/agent",
                            methods=["GET"])
def wg_agent_status():
    """Pure DB read of persisted runs — no crawling, no self-request."""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    conn = _db_conn()
    if conn is None:
        return jsonify({"ok": False, "error": "db_unavailable"}), 503
    try:
        with conn.cursor() as cur:
            cur.execute(_RUNS_DDL)
            cur.execute("SELECT created_at, dry_run, verdicts, summary "
                        "  FROM white_glove_agent_runs "
                        " ORDER BY created_at DESC LIMIT 14")
            runs = [{"created_at": _iso(a), "dry_run": b,
                     "verdicts": c, "summary": d}
                    for a, b, c, d in cur.fetchall()]
        conn.commit()
        return jsonify({"ok": True, "runs": runs, "sla": SLA})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass
