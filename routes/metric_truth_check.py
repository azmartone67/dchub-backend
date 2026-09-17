"""metric_truth_check.py — WEEKLY METRIC GROUND-TRUTH CHECKER (2026-07-11).

Two shell metrics were WILDLY wrong this week and both were MEASUREMENT bugs,
not data bugs: media health read 35 while the real lane was 100 (frozen
citation-velocity dedup + memory-only post counters), and verified facilities
read 5 while the raw table held 4,903 (the lane counted the drained pending
queue, issue #1539). Both sat unnoticed because nothing ever re-derived the
headline numbers from the raw tables.

This module is that re-derivation: once a week (Sunday, heartbeat-dispatched)
it recomputes 5 headline metrics DIRECTLY from the raw tables with SQL that is
deliberately INDEPENDENT of the shells' own code paths, compares each against
the shell-REPORTED value (the newest row of that shell's snapshot table), and
files a brain finding — via the canonical writer, routes.brain_findings_writer
.upsert_brain_finding — whenever the two diverge by more than
METRIC_TRUTH_THRESHOLD_PCT (default 30%). A stale/missing shell snapshot
(>8 days old) is ALSO filed: "the lane stopped measuring" is exactly the
grid-data 7d-stuck failure class.

The checks (shell-reported value ← vs → independent recompute):
  · media_posts_24h        media_master_snapshots.posts_24h
                           ↔ linkedin_posts + social_media_posts durable
                             ledgers, window ANCHORED at the snapshot's
                             computed_at (comparing a 06:00 snapshot against
                             a NOW-ending window would false-positive on
                             pure window shift).
  · citation_velocity_7d   media_master_snapshots.citation_velocity_7d
                           ↔ COUNT(DISTINCT engine) of dchub_cited rows in
                             ai_citations, anchored 7d window. (The shell
                             unions two testimonial tables too; ai_citations
                             is the alive feed — the 30% band absorbs the
                             definitional gap, and 0-vs-N still trips.)
  · verified_facilities    facility_count_snapshots.verified_count
                           ↔ COUNT(*) FROM discovered_facilities WHERE
                             COALESCE(is_duplicate,0)=0 (the #1539 canonical
                             definition — NOT the pending-queue filter).
  · real_agents_7d         audience_snapshots.real_agents_7d
                           ↔ COUNT(DISTINCT agent_id) FROM mcp_calls_identity
                             WHERE is_real_external, anchored 7d window.
  · automerge_merges_7d    brain_automerge_log kind='merge' rows (the engine's
                           OWN ledger — whose INSERT was one of the swallowed
                           writes) ↔ brain_proposed_code_fixes status='merged'
                           (stamped by the merge reconciler FROM GitHub, an
                           independent evidence path). Human-merged brain PRs
                           inflate the reconciler side — the finding detail
                           says so; treat this pair as "look here", not proof.

Divergence rule: rel = |shell−truth| / max(|shell|, |truth|, 1) > threshold
AND |shell−truth| >= 3 (absolute floor so 2-vs-1 on a quiet week never pages).

Safety posture (same contract as every shell):
  · KILL SWITCH: METRIC_TRUTH_CHECK_DISABLE=1 — checked in the cron predicate
    (routes/cron_heartbeat.py) AND in the endpoint, so it works with or
    without a deploy.
  · READ-mostly: the only writes are the metric_truth_runs audit row and the
    brain findings (canonical writer, savepoint-wrapped). Every check is
    individually guarded — a missing table yields verdict "error:…", never a
    500, never a crashed run.
  · IDEMPOTENT per ISO week: metric_truth_runs(iso_week PK) + ON CONFLICT DO
    NOTHING claims the week; repeat in-window fires are cheap no-ops
    (?force=1 for operators).
  · The report also carries routes._swallowed_writes.swallowed_write_counts()
    — chronic swallowed-write sites and wrong headline metrics are the same
    disease (silent measurement death), so they surface together.
"""
from __future__ import annotations

import datetime
import hmac
import json
import os

from flask import Blueprint, jsonify, request

from routes._swallowed_writes import swallowed_write_counts
from routes.brain_findings_writer import upsert_brain_finding

metric_truth_bp = Blueprint("metric_truth", __name__,
                            url_prefix="/api/v1/admin/metric-truth")

DETECTOR = "metric_truth_check"
STALE_DAYS = 8          # a weekly-or-faster lane older than this is stuck
ABS_FLOOR = 3           # never flag |shell-truth| < 3 (quiet-week noise)


# ── config / auth (same shape as analyst_note / brain_rag) ───────────
def _disabled() -> bool:
    return str(os.environ.get("METRIC_TRUTH_CHECK_DISABLE", "")).lower() in (
        "1", "true", "yes")


def _threshold() -> float:
    try:
        return float(os.environ.get("METRIC_TRUTH_THRESHOLD_PCT", "30")) / 100.0
    except Exception:
        return 0.30


def _admin_ok() -> bool:
    """Accept X-Admin-Key=DCHUB_ADMIN_KEY or X-Internal-Key=DCHUB_INTERNAL_KEY /
    DCHUB_SYNC_KEY — the exact headers cron_heartbeat._hit() attaches."""
    pairs = [
        (request.headers.get("X-Admin-Key"), os.environ.get("DCHUB_ADMIN_KEY")),
        (request.headers.get("X-Internal-Key"), os.environ.get("DCHUB_INTERNAL_KEY")),
        (request.headers.get("X-Internal-Key"), os.environ.get("DCHUB_SYNC_KEY")),
    ]
    for got, exp in pairs:
        got = (got or "").strip()
        exp = (exp or "").strip()
        if got and exp and hmac.compare_digest(got, exp):
            return True
    return False


# ── DB — direct connection, NOT the main pool (shell convention). ────
# Autocommit stays OFF: upsert_brain_finding uses SAVEPOINTs, which need a
# real transaction. Callers commit explicitly and close in finally.
def _conn():
    url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url, connect_timeout=10)
    except Exception:
        return None


def _scalar(cur, sql, params=()):
    """One-value query; None on any failure (missing table/column) so a
    single broken check can never kill the run."""
    try:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return None


def _row(cur, sql, params=()):
    try:
        cur.execute(sql, params)
        return cur.fetchone()
    except Exception:
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return None


# ── divergence math (pure — unit-tested directly) ────────────────────
def compute_divergence(shell, truth):
    """Relative divergence in [0..1], or None if either side is missing."""
    if shell is None or truth is None:
        return None
    try:
        s, t = float(shell), float(truth)
    except (TypeError, ValueError):
        return None
    return abs(s - t) / max(abs(s), abs(t), 1.0)


def is_diverged(shell, truth, threshold) -> bool:
    rel = compute_divergence(shell, truth)
    if rel is None:
        return False
    return rel > threshold and abs(float(shell) - float(truth)) >= ABS_FLOOR


def _is_stale(as_of, now) -> bool:
    if as_of is None:
        return True
    try:
        if isinstance(as_of, datetime.date) and not isinstance(as_of, datetime.datetime):
            as_of = datetime.datetime(as_of.year, as_of.month, as_of.day,
                                      tzinfo=datetime.timezone.utc)
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=datetime.timezone.utc)
        return (now - as_of) > datetime.timedelta(days=STALE_DAYS)
    except Exception:
        return True


# ── the five checks ──────────────────────────────────────────────────
# Each returns {metric, shell, truth, shell_as_of, note} and NEVER raises.
def _check_media(cur):
    """media_posts_24h + citation_velocity_7d from ONE snapshot fetch."""
    snap = _row(cur, """
        SELECT posts_24h, citation_velocity_7d, computed_at
          FROM media_master_snapshots
         ORDER BY id DESC LIMIT 1
    """)
    if not snap:
        return [{"metric": "media_posts_24h", "shell": None, "truth": None,
                 "shell_as_of": None, "note": "no media_master_snapshots row"},
                {"metric": "citation_velocity_7d", "shell": None, "truth": None,
                 "shell_as_of": None, "note": "no media_master_snapshots row"}]
    posts, cv7, at = snap[0], snap[1], snap[2]

    # Durable post ledgers, window anchored at the snapshot's computed_at.
    truth_posts = None
    li = _scalar(cur, """
        SELECT COUNT(*) FROM linkedin_posts
         WHERE status = 'success'
           AND created_at >  %s::timestamptz - INTERVAL '24 hours'
           AND created_at <= %s::timestamptz
    """, (at, at))
    sm = _scalar(cur, """
        SELECT COUNT(*) FROM social_media_posts
         WHERE status = 'published'
           AND COALESCE(publish_platform, platform) <> 'linkedin'
           AND NULLIF(published_at, '') IS NOT NULL
           AND published_at::timestamptz >  %s::timestamptz - INTERVAL '24 hours'
           AND published_at::timestamptz <= %s::timestamptz
    """, (at, at))
    if li is not None or sm is not None:
        truth_posts = int(li or 0) + int(sm or 0)

    # Distinct citing engines in the anchored 7d window (alive feed only).
    ts_col = "COALESCE(observed_at, created_at)" if _has_col(
        cur, "ai_citations", "observed_at") else "created_at"
    truth_cv = _scalar(cur, f"""
        SELECT COUNT(DISTINCT COALESCE(NULLIF(engine, ''), 'ai'))
          FROM ai_citations
         WHERE dchub_cited = true
           AND {ts_col} >  %s::timestamptz - INTERVAL '7 days'
           AND {ts_col} <= %s::timestamptz
    """, (at, at))

    return [
        {"metric": "media_posts_24h", "shell": posts, "truth": truth_posts,
         "shell_as_of": at,
         "note": "durable ledgers (linkedin_posts + social_media_posts), "
                 "24h window anchored at snapshot computed_at"},
        {"metric": "citation_velocity_7d", "shell": cv7, "truth": truth_cv,
         "shell_as_of": at,
         "note": "COUNT(DISTINCT engine) dchub_cited ai_citations, anchored "
                 "7d window; shell also unions testimonial tables"},
    ]


def _check_verified_facilities(cur):
    snap = _row(cur, """
        SELECT verified_count, snapshot_date
          FROM facility_count_snapshots
         ORDER BY snapshot_date DESC LIMIT 1
    """)
    shell, as_of = (snap[0], snap[1]) if snap else (None, None)
    truth = _scalar(cur, """
        SELECT COUNT(*) FROM discovered_facilities
         WHERE COALESCE(is_duplicate, 0) = 0
    """)
    return [{"metric": "verified_facilities", "shell": shell, "truth": truth,
             "shell_as_of": as_of,
             "note": "canonical #1539 definition: deduped fleet, NOT the "
                     "drained pending queue"}]


def _check_real_agents(cur):
    snap = _row(cur, """
        SELECT real_agents_7d, computed_at
          FROM audience_snapshots
         ORDER BY id DESC LIMIT 1
    """)
    if not snap:
        return [{"metric": "real_agents_7d", "shell": None, "truth": None,
                 "shell_as_of": None, "note": "no audience_snapshots row"}]
    shell, at = snap[0], snap[1]
    truth = _scalar(cur, """
        SELECT COUNT(DISTINCT agent_id) FROM mcp_calls_identity
         WHERE is_real_external
           AND created_at >  %s::timestamptz - INTERVAL '7 days'
           AND created_at <= %s::timestamptz
    """, (at, at))
    return [{"metric": "real_agents_7d", "shell": shell, "truth": truth,
             "shell_as_of": at,
             "note": "mcp_calls_identity is_real_external, anchored 7d window"}]


def _check_automerge(cur):
    now_ledger = _scalar(cur, """
        SELECT COUNT(*) FROM brain_automerge_log
         WHERE kind = 'merge'
           AND merged_at > NOW() - INTERVAL '7 days'
    """)
    reconciled = _scalar(cur, """
        SELECT COUNT(*) FROM brain_proposed_code_fixes
         WHERE status = 'merged'
           AND reviewed_at > NOW() - INTERVAL '7 days'
    """)
    return [{"metric": "automerge_merges_7d", "shell": now_ledger,
             "truth": reconciled, "shell_as_of": None,
             "note": "engine ledger vs merge-reconciler credit (GitHub-"
                     "derived); human-merged brain PRs inflate the "
                     "reconciler side — investigate, don't auto-conclude"}]


def _has_col(cur, table, col) -> bool:
    v = _scalar(cur, """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s LIMIT 1
    """, (table, col))
    return bool(v)


# ── audit-row schema (idempotency + history) ─────────────────────────
def _ensure_runs_table(cur) -> bool:
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS metric_truth_runs (
                iso_week  TEXT PRIMARY KEY,
                ran_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                report    JSONB
            )
        """)
        return True
    except Exception:
        try:
            cur.connection.rollback()
        except Exception:
            pass
        return False


# ── the run ──────────────────────────────────────────────────────────
def run_check(force: bool = False, now: datetime.datetime | None = None) -> dict:
    """Recompute all metrics, compare, file findings for divergent/stale
    lanes, persist the report. Idempotent per ISO week unless force.
    Returns the report dict; never raises."""
    if _disabled():
        return {"ok": True, "skipped": "METRIC_TRUTH_CHECK_DISABLE"}
    now = now or datetime.datetime.now(datetime.timezone.utc)
    iso = now.isocalendar()
    week_key = f"{iso[0]}-W{iso[1]:02d}"

    conn = _conn()
    if conn is None:
        return {"ok": False, "error": "no database connection"}
    try:
        cur = conn.cursor()
        if not _ensure_runs_table(cur):
            return {"ok": False, "error": "metric_truth_runs unavailable"}
        conn.commit()

        # Claim the week (idempotency). ON CONFLICT DO NOTHING + RETURNING:
        # no row back == someone already ran this week.
        if not force:
            try:
                cur.execute("""
                    INSERT INTO metric_truth_runs (iso_week) VALUES (%s)
                    ON CONFLICT (iso_week) DO NOTHING RETURNING iso_week
                """, (week_key,))
                claimed = cur.fetchone()
                conn.commit()
                if not claimed:
                    return {"ok": True, "skipped": "already_ran",
                            "iso_week": week_key}
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return {"ok": False, "error": "week-claim failed"}

        threshold = _threshold()
        results = []
        for check in (_check_media, _check_verified_facilities,
                      _check_real_agents, _check_automerge):
            try:
                results.extend(check(cur))
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                results.append({"metric": check.__name__, "shell": None,
                                "truth": None, "shell_as_of": None,
                                "note": f"error: {type(e).__name__}: {str(e)[:120]}"})

        findings_filed = 0
        for r in results:
            r["rel_divergence"] = compute_divergence(r["shell"], r["truth"])
            # automerge has no snapshot timestamp (both sides are live 7d
            # counts) — staleness only applies to snapshot-backed lanes.
            stale = (r["metric"] != "automerge_merges_7d"
                     and _is_stale(r["shell_as_of"], now))
            if r["shell"] is None or stale:
                r["verdict"] = "shell_stale_or_missing"
            elif r["truth"] is None:
                r["verdict"] = "truth_unavailable"
            elif is_diverged(r["shell"], r["truth"], threshold):
                r["verdict"] = "diverged"
            else:
                r["verdict"] = "ok"

            if r["verdict"] in ("diverged", "shell_stale_or_missing"):
                # Stable issue text per (metric, verdict) so weekly repeats
                # bump seen_count on ONE finding instead of piling up rows.
                if r["verdict"] == "diverged":
                    issue = (f"metric-truth: {r['metric']} shell-reported "
                             f"diverges >{int(threshold * 100)}% from raw recompute")
                else:
                    issue = (f"metric-truth: {r['metric']} shell snapshot "
                             f"stale or missing (>{STALE_DAYS}d)")
                detail = json.dumps({
                    "metric": r["metric"], "shell": r["shell"],
                    "truth": r["truth"],
                    "shell_as_of": str(r["shell_as_of"] or ""),
                    "rel_divergence": r["rel_divergence"],
                    "note": r["note"], "week": week_key,
                }, default=str)[:900]
                try:
                    out = upsert_brain_finding(
                        cur, issue, url="/api/v1/admin/metric-truth/status",
                        detail=detail, detector=DETECTOR)
                    if out in ("inserted", "updated"):
                        findings_filed += 1
                        conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass

        report = {
            "ok": True, "iso_week": week_key,
            "ran_at": now.isoformat(),
            "threshold_pct": int(threshold * 100),
            "results": results,
            "findings_filed": findings_filed,
            "swallowed_write_counts": swallowed_write_counts(),
        }
        try:
            cur.execute("""
                INSERT INTO metric_truth_runs (iso_week, ran_at, report)
                VALUES (%s, NOW() ON CONFLICT DO NOTHING, %s::jsonb)
                ON CONFLICT (iso_week)
                DO UPDATE SET ran_at = NOW(), report = EXCLUDED.report
            """, (week_key, json.dumps(report, default=str)))
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        return report
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── HTTP surface ─────────────────────────────────────────────────────
@metric_truth_bp.route("/check", methods=["POST"])
def check_endpoint():
    if not _admin_ok():
        return jsonify(error="admin key required"), 403
    if _disabled():
        return jsonify(ok=True, skipped="METRIC_TRUTH_CHECK_DISABLE"), 200
    force = str(request.args.get("force", "")).lower() in ("1", "true", "yes")
    return jsonify(run_check(force=force)), 200


# AUTO-REPAIR: duplicate route '/status' also in ai_agent.py:357 — review and remove one
@metric_truth_bp.route("/status", methods=["GET"])
def status_endpoint():
    if not _admin_ok():
        return jsonify(error="admin key required"), 403
    conn = _conn()
    if conn is None:
        return jsonify(ok=False, error="no database connection"), 200
    try:
        cur = conn.cursor()
        row = _row(cur, """
            SELECT iso_week, ran_at, report FROM metric_truth_runs
             ORDER BY ran_at DESC NULLS LAST LIMIT 1
        """)
        if not row:
            return jsonify(ok=True, latest=None), 200
        return jsonify(ok=True, latest={
            "iso_week": row[0],
            "ran_at": (row[1].isoformat() if row[1] else None),
            "report": row[2],
        }), 200
    finally:
        try:
            conn.close()
        except Exception:
            pass
