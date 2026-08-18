"""
brain_learning.py — Phase GG (2026-05-14): Bundle 4 — Brain Learning Loop.

The Explore-agent audit of brain (Phase GG, 2026-05-14) found that brain
is a proposal engine with shallow memory: it detects → proposes →
approves, but the feedback loop *stops at merge*. No post-merge
verification, no rejection memory, no effectiveness metrics, no
temporal pattern analysis, no model tuning. This module closes those
gaps.

It is PURELY ADDITIVE — does not modify any existing brain table or
write path. Existing brain_v2_layer4 / brain_v2_layer5 / brain_v2_store
code continues to run unchanged. This module adds:

  • brain_fix_outcomes        — post-merge verification (4A)
  • brain_review_decisions    — human-reviewer memory  (4B)
  • brain_temporal_patterns   — chronic/intermittent classifier (4C)
  • brain_model_performance   — per-model success tracker (4D)

  • GET  /api/v1/brain/effectiveness         — month-over-month dashboard (4A)
  • GET  /api/v1/brain/outcomes              — recent outcome tracking (4A)
  • POST /api/v1/brain/review-decision       — record human review (4B, admin)
  • GET  /api/v1/brain/temporal-patterns     — classified issue list (4C)
  • GET  /api/v1/brain/model-performance     — per-model stats (4D)
  • GET  /api/v1/brain/self-assessment       — brain's own letter grade (4E)
  • POST /api/v1/brain/probe-outcomes        — cron-callable outcome probe (4A)

Helpers exported for opt-in integration by existing brain code:
  • check_rejection_skip(issue_label, find_text) -> bool
  • record_proposal_outcome(proposal_id, ...)
  • record_model_run(layer, model, ...)
"""
import hashlib
import json
import os
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write
from util.db_honesty import close_quietly, try_fetchall, unpoison

try:
    from util.provenance import src, attach_sources, now_iso
except Exception:
    def src(claim, source, observed_at=None, url=None):
        return {"claim": claim, "source": source,
                "observed_at": observed_at.isoformat() if hasattr(observed_at, 'isoformat') else observed_at,
                "url": url}
    def attach_sources(p, s, generated_at=None):
        out = dict(p) if isinstance(p, dict) else {"result": p}
        out["sources"] = [x for x in (s or []) if x]
        out["generated_at"] = generated_at or datetime.now(timezone.utc).isoformat()
        return out
    def now_iso():
        return datetime.now(timezone.utc).isoformat()

brain_learning_bp = Blueprint("brain_learning", __name__)

ADMIN_KEY = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("ADMIN_KEY")


# ─────────────────────────────────────────────────────────────────────
# Connection + schema
# ─────────────────────────────────────────────────────────────────────
def _conn():
    import psycopg2
    c = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=8)
    c.autocommit = True
    return c


_SCHEMA = [
    # 4A: post-merge outcome verification. Links to brain_proposed_fixes.id
    # via foreign-key-by-convention (no hard FK so missing proposal rows
    # don't break inserts).
    """CREATE TABLE IF NOT EXISTS brain_fix_outcomes (
        id              BIGSERIAL PRIMARY KEY,
        proposal_id     BIGINT,
        proposal_kind   TEXT NOT NULL,
        applied_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        checked_at      TIMESTAMPTZ,
        still_broken    BOOLEAN,
        evidence_url    TEXT,
        evidence_note   TEXT,
        check_count     INT NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS ix_bfo_proposal ON brain_fix_outcomes (proposal_id, proposal_kind)",
    "CREATE INDEX IF NOT EXISTS ix_bfo_applied ON brain_fix_outcomes (applied_at DESC)",

    # 4B: human-reviewer rejection memory.
    """CREATE TABLE IF NOT EXISTS brain_review_decisions (
        id              BIGSERIAL PRIMARY KEY,
        proposal_kind   TEXT NOT NULL,
        proposal_id     BIGINT,
        issue_hash      TEXT NOT NULL,
        issue_label     TEXT,
        decision        TEXT NOT NULL,
        reviewer        TEXT,
        reviewer_note   TEXT,
        decided_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_brd_hash ON brain_review_decisions (issue_hash, decision)",
    "CREATE INDEX IF NOT EXISTS ix_brd_when ON brain_review_decisions (decided_at DESC)",

    # 4C: temporal classification. One row per (issue_label, url).
    """CREATE TABLE IF NOT EXISTS brain_temporal_patterns (
        id                BIGSERIAL PRIMARY KEY,
        issue_label       TEXT NOT NULL,
        url               TEXT NOT NULL DEFAULT '',
        seen_timestamps   JSONB NOT NULL DEFAULT '[]'::jsonb,
        first_seen_at     TIMESTAMPTZ,
        last_seen_at      TIMESTAMPTZ,
        classification    TEXT,
        classified_at     TIMESTAMPTZ,
        UNIQUE (issue_label, url)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_btp_class ON brain_temporal_patterns (classification, last_seen_at DESC)",

    # 4D: per-model performance.
    """CREATE TABLE IF NOT EXISTS brain_model_performance (
        id              BIGSERIAL PRIMARY KEY,
        layer           TEXT NOT NULL,
        model           TEXT NOT NULL,
        run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        latency_ms      INT,
        outcome         TEXT,
        proposal_id     BIGINT,
        approved        BOOLEAN,
        rejected        BOOLEAN,
        notes           TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_bmp_model ON brain_model_performance (layer, model, run_at DESC)",
]


def _ensure_schema():
    """Idempotent. Safe to call on every request — CREATE TABLE IF NOT EXISTS
    is cheap. Returns True if all DDL ran OK, False if any errored.

    ★ 2026-08-01 — this was `with _conn() as c`, and for DDL that was worse
    than for a read. Every statement ran inside ONE transaction, so the early
    `return False` left the block with no exception in flight, `__exit__`
    called commit() on an already-aborted transaction, and Postgres turned
    that into a ROLLBACK — silently DISCARDING every table and index the
    earlier iterations had just created. One bad DDL anywhere in _SCHEMA meant
    none of it persisted, on every call, forever. Per-statement autocommit
    keeps the ones that worked. All of _SCHEMA succeeds against the live DB
    today; this is about it staying that way.
    """
    c = None
    try:
        c = _conn()
        with c.cursor() as cur:
            for ddl in _SCHEMA:
                try:
                    # cur.execute, NOT try_fetchall: DDL returns no result set
                    # and fetchall() would raise "no results to fetch" on every
                    # SUCCESSFUL statement, i.e. always report False.
                    cur.execute(ddl)
                except Exception:
                    unpoison(cur)
                    return False
        return True
    except Exception:
        return False
    finally:
        close_quietly(c)


def _require_admin(fn):
    @wraps(fn)
    def w(*a, **kw):
        provided = (request.headers.get("X-Admin-Key") or
                    request.args.get("admin_key") or "").strip()
        if ADMIN_KEY and provided != ADMIN_KEY:
            return jsonify(error="unauthorized"), 401
        return fn(*a, **kw)
    return w


# ─────────────────────────────────────────────────────────────────────
# Helpers (exported for opt-in integration by brain_v2_layer4/5)
# ─────────────────────────────────────────────────────────────────────
def issue_hash(issue_label, find_text=""):
    """Stable hash for an issue identity. Same label+find on Layer 4 and
    Layer 5 produces the same hash → cross-layer integration."""
    s = f"{(issue_label or '').strip().lower()}|{(find_text or '').strip()[:200]}"
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def check_rejection_skip(issue_label, find_text="", reject_threshold=2):
    """Should brain skip this proposal because a human already rejected it?

    Returns True if there are >= `reject_threshold` 'reject' decisions for
    this issue_hash within the last 30 days. Safe to call from any layer.
    """
    if not issue_label:
        return False
    h = issue_hash(issue_label, find_text)
    c = None
    try:
        c = _conn()
        with c.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM brain_review_decisions
                    WHERE issue_hash = %s AND decision = 'reject'
                      AND decided_at > NOW() - INTERVAL '30 days'""",
                (h,))
            n = cur.fetchone()
            return bool(n and n[0] >= reject_threshold)
    except Exception:
        # Fail OPEN on purpose: this gate suppresses a proposal humans have
        # already rejected, so an unreadable answer must not silently start
        # suppressing proposals nobody rejected.
        return False
    finally:
        close_quietly(c)


def record_proposal_outcome(proposal_id, proposal_kind, still_broken,
                            evidence_url=None, evidence_note=None):
    """Record a post-merge outcome check. `proposal_kind` ∈ {'text','code'}.
    `still_broken` is TRI-STATE (2026-07-11): True = the fix didn't work;
    False = it did; None = CHECKED but indeterminate (cannot verify) — the
    NULL row marks the proposal as checked (so probes don't re-select it)
    while every rate reader excludes it, same rule as
    autopilot_outcomes.succeeded IS NULL. Returns True if recorded."""
    if proposal_id is None or proposal_kind not in ('text', 'code'):
        return False
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO brain_fix_outcomes
                       (proposal_id, proposal_kind, checked_at,
                        still_broken, evidence_url, evidence_note,
                        check_count)
                   VALUES (%s, %s, NOW() ON CONFLICT DO NOTHING, %s, %s, %s, 1)
                   ON CONFLICT DO NOTHING""",
                (proposal_id, proposal_kind,
                 (None if still_broken is None else bool(still_broken)),
                 (evidence_url or '')[:300],
                 (evidence_note or '')[:500]))
        return True
    except Exception:
        note_swallowed_write("brain_fix_outcomes", where="brain_learning.record_proposal_outcome")
        return False


def record_model_run(layer, model, outcome, latency_ms=None,
                     proposal_id=None, approved=None, rejected=None,
                     notes=None):
    """Log a single Claude-API call's outcome. Layers can call this opt-in
    after each proposal attempt."""
    if not (layer and model):
        return False
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO brain_model_performance
                       (layer, model, latency_ms, outcome,
                        proposal_id, approved, rejected, notes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (layer[:40], model[:80], latency_ms,
                 (outcome or '')[:60],
                 proposal_id,
                 approved if approved is not None else None,
                 rejected if rejected is not None else None,
                 (notes or '')[:300]))
        return True
    except Exception:
        note_swallowed_write("brain_model_performance", where="brain_learning.record_model_run")
        return False


def bump_temporal(issue_label, url=""):
    """Append a 'seen' timestamp for an issue. Maintains a rolling array
    of timestamps (capped at 200) and re-classifies."""
    if not issue_label:
        return False
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO brain_temporal_patterns
                       (issue_label, url, seen_timestamps,
                        first_seen_at, last_seen_at)
                   VALUES (%s, %s, jsonb_build_array(NOW() ON CONFLICT DO NOTHING::text), NOW(), NOW())
                   ON CONFLICT (issue_label, url) DO UPDATE
                   SET seen_timestamps =
                          (CASE
                             WHEN jsonb_array_length(brain_temporal_patterns.seen_timestamps) >= 200
                             THEN brain_temporal_patterns.seen_timestamps
                             ELSE brain_temporal_patterns.seen_timestamps
                                  || jsonb_build_array(NOW())
                           END),
                       last_seen_at = NOW()""",
                (issue_label[:200], (url or '')[:300]))
            # re-classify
            cur.execute(
                """SELECT seen_timestamps, first_seen_at, last_seen_at
                     FROM brain_temporal_patterns
                    WHERE issue_label = %s AND url = %s""",
                (issue_label[:200], (url or '')[:300]))
            row = cur.fetchone()
            if row:
                klass = _classify_temporal(row[0], row[1], row[2])
                cur.execute(
                    """UPDATE brain_temporal_patterns
                          SET classification = %s, classified_at = NOW()
                        WHERE issue_label = %s AND url = %s""",
                    (klass, issue_label[:200], (url or '')[:300]))
        return True
    except Exception:
        note_swallowed_write("brain_temporal_patterns", where="brain_learning.bump_temporal")
        return False


def _classify_temporal(timestamps, first_seen, last_seen):
    """Bucket: chronic / intermittent / spiking / resolved.

    Heuristics:
      - resolved   = last_seen > 7 days ago
      - spiking    = >= 5 occurrences in last 24h
      - chronic    = >= 10 total + >=70% of days in lifetime have an event
      - intermittent = otherwise (events sporadic, not daily)
    """
    try:
        n = len(timestamps) if isinstance(timestamps, list) else 0
        if not n:
            return "unknown"
        now = datetime.now(timezone.utc)

        last_dt = last_seen if last_seen else None
        if isinstance(last_dt, str):
            last_dt = datetime.fromisoformat(last_dt.replace('Z', '+00:00'))
        if last_dt and last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)

        first_dt = first_seen if first_seen else None
        if isinstance(first_dt, str):
            first_dt = datetime.fromisoformat(first_dt.replace('Z', '+00:00'))
        if first_dt and first_dt.tzinfo is None:
            first_dt = first_dt.replace(tzinfo=timezone.utc)

        if last_dt and (now - last_dt) > timedelta(days=7):
            return "resolved"

        # Last 24h count
        cutoff_24h = now - timedelta(hours=24)
        recent = 0
        for ts in timestamps:
            try:
                t = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                if t >= cutoff_24h:
                    recent += 1
            except Exception:
                continue
        if recent >= 5:
            return "spiking"

        # Density over lifetime
        if first_dt and last_dt and n >= 10:
            lifetime_days = max(1, (last_dt - first_dt).days)
            density = n / lifetime_days
            if density >= 0.7:
                return "chronic"
        return "intermittent"
    except Exception:
        return "unknown"


# ─────────────────────────────────────────────────────────────────────
# 4A — Effectiveness dashboard
# ─────────────────────────────────────────────────────────────────────
@brain_learning_bp.route("/api/v1/brain/effectiveness", methods=["GET"])
def brain_effectiveness():
    """Month-over-month metrics. Reads from existing brain tables
    (brain_proposed_fixes, brain_proposed_code_fixes, brain_learning_log)
    + new brain_fix_outcomes. Answers: 'is brain getting smarter?'"""
    _ensure_schema()
    payload = {"ok": True, "purpose": (
        "Month-over-month brain effectiveness. Use this to answer "
        "'is brain learning?'. Look at fix_success_rate trending up "
        "and human_rejection_rate trending down.")}
    sources = []
    errors = {}

    def _safe(cur, sql, params=(), key=None):
        """Rows, or [] with the failure RECORDED — never a silent swallow.

        ★ 2026-08-01: was a bare `except: return []`, which is the swallow that
        let /agent/index publish an all-zero coverage inventory for months
        (#2071). Every read in this handler currently passes against the live
        DB; the shape is fenced anyway, because "passes today" is exactly what
        was true of agent_index the day before a column was renamed under it.
        """
        rows, err = try_fetchall(cur, sql, params)
        if err and key:
            errors[key] = err
        return rows

    c = None
    try:
        c = _conn()
        with c.cursor() as cur:
            # Proposals by month
            rows = _safe(cur, """
                SELECT TO_CHAR(proposed_at, 'YYYY-MM') AS month,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE approved = TRUE) AS approved,
                       COUNT(*) FILTER (WHERE approval_count >= 2) AS reached_threshold
                  FROM brain_proposed_fixes
                 WHERE proposed_at IS NOT NULL
                 GROUP BY month ORDER BY month DESC LIMIT 6""")
            text_by_month = [{
                "month": r[0], "total": int(r[1] or 0),
                "approved": int(r[2] or 0),
                "approved_pct": round(100 * (r[2] or 0) / (r[1] or 1), 1),
            } for r in rows]
            payload["text_proposals_by_month"] = text_by_month
            if text_by_month:
                sources.append(src(
                    f"Text proposal trend ({len(text_by_month)} months)",
                    "brain_proposed_fixes", now_iso()))

            # Code proposals by month
            rows = _safe(cur, """
                SELECT TO_CHAR(proposed_at, 'YYYY-MM') AS month,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status = 'merged') AS merged,
                       COUNT(*) FILTER (WHERE status = 'reviewed') AS reviewed
                  FROM brain_proposed_code_fixes
                 WHERE proposed_at IS NOT NULL
                 GROUP BY month ORDER BY month DESC LIMIT 6""")
            code_by_month = [{
                "month": r[0], "total": int(r[1] or 0),
                "merged": int(r[2] or 0),
                "reviewed": int(r[3] or 0),
            } for r in rows]
            payload["code_proposals_by_month"] = code_by_month
            if code_by_month:
                sources.append(src(
                    f"Code proposal trend ({len(code_by_month)} months)",
                    "brain_proposed_code_fixes", now_iso()))

            # Outcome verification — the headline metric
            rows = _safe(cur, """
                SELECT COUNT(*) AS checks,
                       COUNT(*) FILTER (WHERE still_broken = FALSE) AS fix_worked,
                       COUNT(*) FILTER (WHERE still_broken = TRUE) AS fix_failed
                  FROM brain_fix_outcomes
                 WHERE checked_at > NOW() - INTERVAL '30 days'""")
            if rows and rows[0]:
                total = int(rows[0][0] or 0)
                worked = int(rows[0][1] or 0)
                failed = int(rows[0][2] or 0)
                payload["outcome_verification_30d"] = {
                    "checks_performed": total,
                    "fix_succeeded": worked,
                    "fix_failed": failed,
                    "success_rate_pct": round(100 * worked / max(1, worked + failed), 1)
                        if (worked + failed) else None,
                }
                if total:
                    sources.append(src(
                        f"Outcome verification ({total} checks)",
                        "brain_fix_outcomes", now_iso()))

            # Human rejection rate
            rows = _safe(cur, """
                SELECT decision, COUNT(*)
                  FROM brain_review_decisions
                 WHERE decided_at > NOW() - INTERVAL '30 days'
                 GROUP BY decision""")
            by_decision = {r[0]: int(r[1]) for r in rows}
            total_reviews = sum(by_decision.values())
            payload["human_reviews_30d"] = {
                "total": total_reviews,
                "by_decision": by_decision,
                "rejection_rate_pct": round(100 * by_decision.get('reject', 0) /
                                            max(1, total_reviews), 1) if total_reviews else None,
            }

            # False-positive memory size (existing brain table).
            # ★ null, not 0. `int(rows[0][0]) if rows else 0` mapped a FAILED
            # read to "the brain remembers zero false positives" — a claim
            # about the system's own learning that would read as a regression
            # rather than as a broken query.
            rows = _safe(cur, """
                SELECT COUNT(*) FROM brain_false_positives
                 WHERE refused_count >= 3""", key="false_positive_memory")
            payload["false_positive_memory"] = (
                None if "false_positive_memory" in errors
                else (int(rows[0][0]) if rows else 0))

            # Stuck issues (existing brain table)
            rows = _safe(cur, """
                SELECT COUNT(*) FROM brain_issue_persistence
                 WHERE seen_count >= 5""", key="chronic_stuck_issues")
            payload["chronic_stuck_issues"] = (
                None if "chronic_stuck_issues" in errors
                else (int(rows[0][0]) if rows else 0))

    except Exception as e:
        payload["error_partial"] = str(e)[:200]
        errors["connection"] = str(e)[:160]
    finally:
        close_quietly(c)

    payload["complete"] = not errors
    if errors:
        payload["query_errors"] = errors
        payload["completeness_note"] = (
            f"PARTIAL - {len(errors)} metric(s) could not be read and are "
            "null. null means UNKNOWN, not zero.")

    return jsonify(attach_sources(payload, sources)), 200


@brain_learning_bp.route("/api/v1/brain/outcomes", methods=["GET"])
def brain_outcomes():
    """Recent outcome verifications — did approved fixes actually work?"""
    _ensure_schema()
    try:
        limit = min(int(request.args.get("limit") or 50), 200)
    except Exception:
        limit = 50
    out = []
    c = None
    try:
        c = _conn()
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, proposal_id, proposal_kind, applied_at, checked_at,
                       still_broken, evidence_url, evidence_note, check_count
                  FROM brain_fix_outcomes
                 ORDER BY applied_at DESC NULLS LAST LIMIT %s""", (limit,))
            for r in cur.fetchall():
                out.append({
                    "id": r[0], "proposal_id": r[1], "proposal_kind": r[2],
                    "applied_at": r[3].isoformat() if r[3] else None,
                    "checked_at": r[4].isoformat() if r[4] else None,
                    "still_broken": r[5],
                    "evidence_url": r[6], "evidence_note": r[7],
                    "check_count": r[8],
                })
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200
    finally:
        close_quietly(c)
    return jsonify(ok=True, outcomes=out, count=len(out),
                   generated_at=now_iso()), 200


# ─────────────────────────────────────────────────────────────────────
# INV4 (loop-closing, 2026-06-02) — stuck-findings escalation view
# ─────────────────────────────────────────────────────────────────────
@brain_learning_bp.route("/api/v1/brain/stuck-findings", methods=["GET"])
def brain_stuck_findings():
    """Read-only HUMAN-review queue: findings the brain has repeatedly
    failed to resolve (high seen_count, still actively appearing, last
    outcome NOT verified_resolved). Surfaces, rather than silently looping,
    the worklist that needs a person — including REGRESSIONS (findings that
    were verified-resolved then re-emerged, ordered first by reopen_count).

    Purely a view over brain_issue_persistence's lifecycle columns; it
    triggers NO action and changes NO state. Public read (like the other
    /brain/* dashboards). Query params:
      min_count   (default 5)   minimum seen_count to qualify
      hours       (default 72)  only findings seen within this window
      limit       (default 50, max 200)
    """
    try:
        min_count = max(1, int(request.args.get("min_count") or 5))
    except Exception:
        min_count = 5
    try:
        hours = max(1, int(request.args.get("hours") or 72))
    except Exception:
        hours = 72
    try:
        limit = min(int(request.args.get("limit") or 50), 200)
    except Exception:
        limit = 50

    rows = []
    try:
        from routes import brain_v2_store as _bs
        rows = _bs.list_stuck_findings(min_count=min_count, limit=limit,
                                       stale_after_hours=hours)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200],
                       generated_at=now_iso()), 200

    regressions = [r for r in rows if int(r.get("reopen_count") or 0) > 0]
    return jsonify(
        ok=True,
        stuck=rows,
        count=len(rows),
        regression_count=len(regressions),
        criteria={"min_count": min_count, "window_hours": hours,
                  "limit": limit,
                  "note": "last_outcome NOT verified_resolved; "
                          "regressions (reopen_count>0) listed first"},
        generated_at=now_iso(),
    ), 200


# ─────────────────────────────────────────────────────────────────────
# Move 5b (2026-06-26) — needs-human escalation queue
# Complements /stuck-findings (the tried-and-FAILED set) with the
# DISTINCT population of findings the autopilot has NO autonomous action
# for: it logs them as brain_autopilot_actions.outcome='escalated' and
# moves on. Pure read view (no schema change, no autopilot hot-path edit)
# joining those escalations to still-OPEN brain_findings, plus an admin
# resolve action that reuses the finding's existing status/resolved_at.
# Admin-gated — surfaces brain_findings.detail (internal context).
# ─────────────────────────────────────────────────────────────────────
@brain_learning_bp.route("/api/v1/brain/needs-human", methods=["GET"])
@_require_admin
def brain_needs_human():
    """Read-only human-review queue: OPEN findings the autopilot escalated
    because no autonomous action exists for their pattern (distinct from
    /stuck-findings, which is the tried-and-failed set). Triggers NO action
    and changes NO state. Query params:
      hours  (default 168 = 7d)  only escalations within this window
      limit  (default 50, max 200)
    """
    try:
        hours = max(1, int(request.args.get("hours") or 168))
    except Exception:
        hours = 168
    try:
        limit = min(int(request.args.get("limit") or 50), 200)
    except Exception:
        limit = 50

    out = []
    c = None
    try:
        c = _conn()
        with c.cursor() as cur:
            cur.execute("""
                SELECT * FROM (
                  SELECT DISTINCT ON (f.id)
                         f.id, f.issue, f.url, f.count, f.detail, f.detector,
                         f.first_seen, f.last_seen, f.seen_count,
                         COALESCE(f.status,'open') AS status,
                         a.started_at AS escalated_at, a.error AS reason
                    FROM brain_findings f
                    JOIN brain_autopilot_actions a
                      ON a.finding_issue = f.issue
                     AND COALESCE(a.finding_url,'') = COALESCE(f.url,'')
                     AND a.outcome = 'escalated'
                   WHERE COALESCE(f.status,'open') = 'open'
                     AND a.started_at > NOW() - make_interval(hours => %s)
                   ORDER BY f.id, a.started_at DESC
                ) q
                ORDER BY q.escalated_at DESC
                LIMIT %s
            """, (hours, limit))
            cols = [d[0] for d in cur.description]
            for r in cur.fetchall():
                row = dict(zip(cols, r))
                for k in ("first_seen", "last_seen", "escalated_at"):
                    if row.get(k) is not None:
                        row[k] = row[k].isoformat()
                out.append(row)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200],
                       generated_at=now_iso()), 200
    finally:
        close_quietly(c)

    return jsonify(
        ok=True,
        needs_human=out,
        count=len(out),
        criteria={"window_hours": hours, "limit": limit,
                  "note": "OPEN findings the autopilot escalated (no autonomous "
                          "action for the pattern); resolve via "
                          "POST /api/v1/brain/needs-human/resolve"},
        generated_at=now_iso(),
    ), 200


@brain_learning_bp.route("/api/v1/brain/needs-human/resolve", methods=["POST"])
@_require_admin
def brain_needs_human_resolve():
    """Human operator marks an escalated finding resolved/dismissed. Sets the
    finding's EXISTING status + resolved_at (no new columns). Body JSON:
        finding_id : int   (required)
        action     : 'resolved' | 'dismissed'   (default 'resolved')
    """
    body = request.get_json(silent=True) or {}
    try:
        finding_id = int(body.get("finding_id"))
    except Exception:
        return jsonify(ok=False, error="finding_id (int) required"), 400
    action = (body.get("action") or "resolved").strip().lower()
    if action not in ("resolved", "dismissed"):
        return jsonify(ok=False,
                       error="action must be 'resolved' or 'dismissed'"), 400

    row = None
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                UPDATE brain_findings
                   SET status = %s, resolved_at = NOW()
                 WHERE id = %s
             RETURNING id, issue, url
            """, (action, finding_id))
            row = cur.fetchone()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200],
                       generated_at=now_iso()), 200

    if not row:
        return jsonify(ok=False, error="no finding id=%s" % finding_id,
                       generated_at=now_iso()), 200
    return jsonify(ok=True, id=row[0], issue=row[1], url=row[2],
                   action=action, generated_at=now_iso()), 200


# ─────────────────────────────────────────────────────────────────────
# 4A — Outcome prober (cron-callable)
# ─────────────────────────────────────────────────────────────────────

# Patterns whose autonomous action is a STAGING / WORKLIST step BY DESIGN
# (routes/brain_autopilot._PATTERN_LIBRARY): build the L13 nudge drafts DRY
# (a human flips dry=false to send), persist a growth snapshot for L5, or
# record a collision into cron_collision_proposals ("actual YAML edit remains
# human work"). The action's own success criterion is "worklist/record
# written" — which the executed_ok 2xx already proved. The FINDING is a
# standing business condition (unconverted demand, trial stagnation, a
# collision backlog) that only HUMAN follow-through can clear, so grading
# these actions by finding-persistence fabricates a permanent-failure signal:
# measured 2026-07-10, 302 of the 385 autopilot still_broken=TRUE rows in the
# 30d fix-signal sample came from exactly this check, and its 82 "absent"
# passes were detector-cadence noise (<30m freshness vs slower detector
# scans). Honest verdict for these: cannot_verify (NULL) — the same exclusion
# rule as autopilot_outcomes.succeeded IS NULL (R3).
_STAGING_PATTERNS = frozenset({
    "addressable_demand_unconverted",   # _action_conversion_build_worklist (DRY)
    "trial_to_paid_stagnation",         # _action_conversion_build_worklist (DRY)
    "tool_signal_to_conversion_leak",   # _action_conversion_build_worklist (DRY)
    "mcp_conversion_rate_below_floor",  # _action_conversion_build_worklist (DRY)
    "cron_schedule_collision",          # records worklist; YAML edit is human work
    "mcp_demand_gap_unaddressed",       # persists a growth snapshot for L5
})

# decide_outcome dormancy window for autopilot probes: the finding must have
# been live within this many days BEFORE the action for its silence after the
# action to be creditable to the action at all (mirrors the reconciler's
# BRAIN_MERGE_RECONCILER_RECENT_DAYS discipline, tightened for the faster
# autopilot loop).
_PROBE_RECENT_DAYS = 7


@brain_learning_bp.route("/api/v1/brain/probe-outcomes", methods=["POST", "GET"])
@_require_admin
def probe_outcomes():
    """Make fix_success MEASURABLE — but ONLY on fixes that were genuinely
    APPLIED. Mirrors each genuinely-applied autopilot action into
    brain_fix_outcomes (the table self-assessment's fix_success_rate reads),
    re-checking whether its original finding is still present.

    VALIDITY (r65, 2026-06-01) — this used to score approved Layer-4 TEXT
    proposals via a fuzzy ILIKE persistence heuristic. That was a LIE:
    `brain_proposed_fixes.approved` only means approval_count>=2 (the same
    find/replace SUGGESTION was seen on >=2 healer cycles) — it has NO
    applied_at column and NOTHING auto-applies a Layer-4 text proposal to a
    file. Scoring un-applied suggestions as fix_worked/fix_failed fabricated
    the headline metric. So we now measure ONLY genuinely-applied fixes:

      • Autopilot (L21) actions that actually executed an action_endpoint
        with a 2xx — brain_autopilot_actions.outcome='executed_ok'. These
        ARE applied (a real cron/cache/refresh endpoint fired). For each one
        older than a 6h settle window that we haven't mirrored yet:
          - the per-pattern EFFECT verifier's verdict
            (autopilot_outcomes.succeeded, latest per action) is reused
            verbatim when it exists — it IS the real signal (R3b);
          - STAGING/WORKLIST patterns (_STAGING_PATTERNS) are recorded
            cannot_verify (still_broken=NULL) — grading a worklist-builder
            by finding-persistence fabricates failures (see the constant's
            comment for the 07-10 measurements);
          - everything else is judged with the merge reconciler's
            unit-tested decide_outcome discipline against
            brain_findings.last_seen: re-seen AFTER the action ⇒ TRUE,
            live before + quiet since ⇒ FALSE, dormant/never-tracked ⇒
            NULL (refuse to fabricate). This replaces the old
            "seen in the last 30 minutes" recheck, whose verdicts measured
            detector CADENCE, not fix effect (R66, 2026-07-11).

      • Code proposals whose merge_outcome is set (merged_healthy /
        merged_reverted) already flow into brain_fix_outcomes via
        record_proposal_outcome() at mark-merge time (brain_v2_layer5 r64), so
        they need no probing here. Merged MECHANICAL code fixes are verified
        against ground truth on main by /api/v1/brain/verify-merged-fixes.

    Skips anything not genuinely applied. It is intentionally better for
    fix_success to stay honestly null than to report a fabricated number.

    SETTLE_HOURS gives an applied fix time to propagate before we judge it.
    """
    _ensure_schema()
    SETTLE_HOURS = 6
    checked = 0
    new_outcomes = 0
    errors = []
    try:
        with _conn() as c, c.cursor() as cur:
            # GENUINELY-APPLIED FIXES ONLY: autopilot actions that actually
            # executed an endpoint with a 2xx (outcome='executed_ok'), settled
            # for >= SETTLE_HOURS, not already mirrored into brain_fix_outcomes.
            # outcome_verified (when set by the autopilot verifier cron) is the
            # SAME present/absent check, so carry it through directly.
            try:
                # R3b (2026-06-16): grade fix-success on the REAL effect verifier
                # (autopilot_outcomes.succeeded — latest per action) instead of the
                # finding-disappeared a.outcome_verified, which scored no-ops as
                # fixed and feeds the 35%-weight learning grade. NULL succeeded
                # (cannot_verify) falls through to the in-process brain_findings
                # re-check below, unchanged.
                cur.execute("""
                    SELECT a.id, a.pattern_name, a.finding_issue,
                           a.finding_url, a.started_at, o.succeeded
                      FROM brain_autopilot_actions a
                      LEFT JOIN LATERAL (
                          SELECT succeeded FROM autopilot_outcomes ao
                           WHERE ao.autopilot_action_id = a.id
                           ORDER BY ao.verified_at DESC LIMIT 1
                      ) o ON TRUE
                     WHERE a.outcome = 'executed_ok'
                       AND a.finding_issue IS NOT NULL
                       AND a.started_at <= NOW() - (%s * INTERVAL '1 hour')
                       AND a.started_at >  NOW() - INTERVAL '30 days'
                       AND NOT EXISTS (
                           SELECT 1 FROM brain_fix_outcomes bo
                            WHERE bo.proposal_id = a.id
                              AND bo.proposal_kind = 'autopilot')
                     ORDER BY a.started_at DESC NULLS LAST LIMIT 100""",
                    (SETTLE_HOURS,))
                candidates = cur.fetchall()
            except Exception as e:
                candidates = []
                errors.append(f"autopilot-select: {str(e)[:120]}")

            for act_id, pattern, issue, url, applied_at, verified in candidates:
                checked += 1
                try:
                    # Prefer the autopilot verifier's own result (the real
                    # per-pattern effect check).
                    if verified is not None:
                        # succeeded TRUE  => verified real effect (fixed)
                        # succeeded FALSE => no real effect (still broken)
                        still_broken = (verified is False)
                        evidence = ("autopilot_outcomes.succeeded="
                                    f"{bool(verified)} (effect verifier re-checked "
                                    "brain_findings)")
                    elif (pattern or "") in _STAGING_PATTERNS:
                        # Worklist/record-only action: the finding is cleared
                        # by human follow-through, never by the staging call —
                        # refuse to grade it (see _STAGING_PATTERNS comment).
                        still_broken = None
                        evidence = (f"cannot_verify: staging/worklist action "
                                    f"({pattern}) — stages human work; finding "
                                    "persistence is not its success criterion")
                    else:
                        # R66 (2026-07-11): judge with the merge reconciler's
                        # unit-tested grace/dormancy discipline against the
                        # finding's ACTUAL last_seen — not the old "seen in
                        # the last 30 minutes" recheck, whose verdicts tracked
                        # detector cadence instead of fix effect.
                        cur.execute("""
                            SELECT MAX(last_seen) FROM brain_findings
                             WHERE issue = %s AND COALESCE(url,'') = %s""",
                            (issue, (url or '')))
                        row = cur.fetchone()
                        last_seen = row[0] if row else None
                        if last_seen is None:
                            still_broken = None
                            evidence = ("cannot_verify: finding not tracked in "
                                        "brain_findings — refusing to fabricate")
                        else:
                            from routes.brain_merge_reconciler import decide_outcome
                            state, still_broken, evidence = decide_outcome(
                                applied_at, last_seen,
                                datetime.now(timezone.utc),
                                SETTLE_HOURS, _PROBE_RECENT_DAYS,
                                noun="action")
                            if state != "outcome":
                                # dormant-before-action / no-evidence ⇒
                                # indeterminate, keep the honest reason.
                                still_broken = None
                    cur.execute("""
                        INSERT INTO brain_fix_outcomes
                            (proposal_id, proposal_kind, applied_at,
                             checked_at, still_broken, evidence_url,
                             evidence_note, check_count)
                        VALUES (%s, 'autopilot', %s, NOW() ON CONFLICT DO NOTHING, %s, %s, %s, 1)
                        ON CONFLICT DO NOTHING""",
                        (act_id, applied_at,
                         (None if still_broken is None else bool(still_broken)),
                         (url or '')[:300], evidence[:500]))
                    new_outcomes += 1
                except Exception as e:
                    errors.append(f"act_id={act_id}: {str(e)[:60]}")
                    if len(errors) >= 5:
                        break
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200

    return jsonify(ok=True,
                   measured_source="applied_autopilot_actions",
                   candidates_checked=checked,
                   new_outcomes_recorded=new_outcomes,
                   note=("Only genuinely-applied fixes are scored. Approved "
                         "Layer-4 text proposals are NOT applied and are no "
                         "longer probed (would fabricate fix_success). Code "
                         "merges flow in via record_proposal_outcome."),
                   errors=errors[:5], generated_at=now_iso()), 200


# ─────────────────────────────────────────────────────────────────────
# 4A.2 — Ground-truth verifier for merged MECHANICAL code fixes
# ─────────────────────────────────────────────────────────────────────
@brain_learning_bp.route("/api/v1/brain/verify-merged-fixes",
                         methods=["POST", "GET"])
@_require_admin
def verify_merged_fixes():
    """Verify merged MECHANICAL brain code fixes against GROUND TRUTH (the
    file on main) and record the verdicts through the canonical
    brain_fix_outcomes writer.

    WHY (2026-07-11): the fix-outcome verifier
    (routes/brain_fix_outcome_verify.verify_fix_resolved — armed via
    BRAIN_FIX_VERIFY=1) existed since 06-19 but its recorder INSERTed into
    columns that don't exist on the live brain_fix_outcomes table (created by
    THIS module's _SCHEMA), so not one verdict ever landed: 39 of 40 merged
    single-file fixes in the 30d window had NO effect verdict while the
    fix-signal trust rate was dragged by doc-only spec PRs and detector-
    cadence noise. This sweep closes that gap with the verifier the system
    already designed:

      resolved (search_text GONE from the file on main AND replace_text
      PRESENT — the fix landed and HELD)          → still_broken = FALSE
      anti-pattern STILL present on main (no-op /
      drifted back)                               → still_broken = TRUE
      indeterminate (file unreadable / replaced by
      a later edit / nothing to check)            → still_broken = NULL
                                                    (checked, excluded)

    Scope: status='merged' proposals with a real file_path + search_text
    (reconciler backfills use file_path='github:<branch>' and are doc-only —
    skipped), merged within the last 30 days, not already verdict-carrying.
    Idempotent via NOT EXISTS; LIMIT per run; cron-driven from
    routes/cron_heartbeat._DISPATCH ('brain_fix_verify_sweep').

    Honors the same arm switch as the verifier module: BRAIN_FIX_VERIFY=1
    (record-only, never blocks or reverts anything)."""
    if os.environ.get("BRAIN_FIX_VERIFY", "0") != "1":
        return jsonify(ok=False, disabled=True,
                       error="BRAIN_FIX_VERIFY!=1 — verifier not armed"), 200
    try:
        limit = max(1, min(50, int(request.args.get("limit", "25"))))
    except Exception:
        limit = 25
    _ensure_schema()
    try:
        from routes.brain_fix_outcome_verify import verify_fix_resolved
    except Exception as e:
        return jsonify(ok=False, error=f"verifier import: {str(e)[:120]}"), 200

    resolved_n = still_broken_n = indeterminate_n = 0
    rows_out, errors = [], []
    c = None
    try:
        c = _conn()
        with c.cursor() as cur:
            cur.execute("""
                SELECT p.id, p.file_path, p.search_text, p.replace_text,
                       p.pr_url
                  FROM brain_proposed_code_fixes p
                 WHERE p.status = 'merged'
                   AND p.file_path NOT LIKE 'github:%%'
                   AND COALESCE(p.search_text, '') <> ''
                   AND COALESCE(p.reviewed_at, p.proposed_at)
                       >= NOW() - INTERVAL '30 days'
                   AND NOT EXISTS (
                       SELECT 1 FROM brain_fix_outcomes bo
                        WHERE bo.proposal_id = p.id
                          AND bo.proposal_kind = 'code')
                 ORDER BY p.reviewed_at DESC NULLS LAST
                 LIMIT %s""", (limit,))
            candidates = cur.fetchall()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200
    finally:
        close_quietly(c)

    for pid, fp, st, rt, pr_url in candidates:
        try:
            verdict = verify_fix_resolved({
                "id": pid, "file_path": fp,
                "search_text": st, "replace_text": rt,
            })
            resolved = verdict.get("resolved")
            reason = str(verdict.get("reason") or "")[:300]
            still_broken = (None if resolved is None else (not resolved))
            if resolved is True:
                resolved_n += 1
            elif resolved is False:
                still_broken_n += 1
            else:
                indeterminate_n += 1
            recorded = record_proposal_outcome(
                pid, "code", still_broken,
                evidence_url=(pr_url or fp),
                evidence_note=f"ground-truth: {reason} [{fp}]")
            rows_out.append({"proposal_id": pid, "file": fp,
                             "resolved": resolved, "reason": reason,
                             "recorded": bool(recorded)})
        except Exception as e:
            errors.append(f"pid={pid}: {str(e)[:80]}")
            if len(errors) >= 5:
                break

    return jsonify(ok=True,
                   measured_source="merged_mechanical_code_fixes",
                   candidates=len(candidates),
                   resolved=resolved_n,
                   still_broken=still_broken_n,
                   indeterminate=indeterminate_n,
                   detail=rows_out[:50],
                   errors=errors[:5],
                   generated_at=now_iso()), 200


# ─────────────────────────────────────────────────────────────────────
# 4B — Review decisions (rejection memory)
# ─────────────────────────────────────────────────────────────────────
@brain_learning_bp.route("/api/v1/brain/review-decision", methods=["POST"])
@_require_admin
def post_review_decision():
    """Record a human-reviewer decision on a brain proposal.

    Body JSON:
        proposal_kind: 'text' | 'code'
        proposal_id:   integer (optional but recommended)
        issue_label:   string (used for hashing)
        find_text:     string (used for hashing; for code proposals = search_text)
        decision:      'approve' | 'reject' | 'defer'
        reviewer:      string (e.g. 'jmartone@dchub.cloud')
        reviewer_note: string
    """
    _ensure_schema()
    body = request.get_json(silent=True) or {}
    kind = (body.get("proposal_kind") or "").strip().lower()
    if kind not in ("text", "code"):
        return jsonify(ok=False, error="proposal_kind must be 'text' or 'code'"), 400
    decision = (body.get("decision") or "").strip().lower()
    if decision not in ("approve", "reject", "defer"):
        return jsonify(ok=False, error="decision must be approve|reject|defer"), 400

    issue_label = (body.get("issue_label") or "")[:200]
    find_text = (body.get("find_text") or "")[:500]
    h = issue_hash(issue_label, find_text)
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO brain_review_decisions
                    (proposal_kind, proposal_id, issue_hash, issue_label,
                     decision, reviewer, reviewer_note)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id""",
                (kind, body.get("proposal_id"), h, issue_label,
                 decision, (body.get("reviewer") or '')[:120],
                 (body.get("reviewer_note") or '')[:500]))
            row = cur.fetchone()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200
    return jsonify(ok=True, id=row[0] if row else None,
                   issue_hash=h, decision=decision,
                   note="Brain will skip future proposals with this hash after 2 rejects."), 200


# ─────────────────────────────────────────────────────────────────────
# 4C — Temporal pattern listing
# ─────────────────────────────────────────────────────────────────────
@brain_learning_bp.route("/api/v1/brain/temporal-patterns", methods=["GET"])
def brain_temporal_patterns():
    """List issues classified by their temporal shape (chronic / intermittent /
    spiking / resolved). Use to give Layer 5 a richer prompt: chronic-stale
    needs a different fix strategy than intermittent-flapping."""
    _ensure_schema()
    klass = (request.args.get("classification") or "").strip().lower() or None
    try:
        limit = min(int(request.args.get("limit") or 50), 200)
    except Exception:
        limit = 50
    out = []
    counts = {}
    c = None
    try:
        c = _conn()
        with c.cursor() as cur:
            cur.execute("""
                SELECT classification, COUNT(*)
                  FROM brain_temporal_patterns
                 WHERE classification IS NOT NULL
                 GROUP BY classification""")
            counts = {r[0]: int(r[1]) for r in cur.fetchall()}

            if klass:
                cur.execute("""
                    SELECT issue_label, url, classification, first_seen_at,
                           last_seen_at, jsonb_array_length(seen_timestamps)
                      FROM brain_temporal_patterns
                     WHERE classification = %s
                     ORDER BY last_seen_at DESC NULLS LAST LIMIT %s""",
                    (klass, limit))
            else:
                cur.execute("""
                    SELECT issue_label, url, classification, first_seen_at,
                           last_seen_at, jsonb_array_length(seen_timestamps)
                      FROM brain_temporal_patterns
                     ORDER BY last_seen_at DESC NULLS LAST LIMIT %s""",
                    (limit,))
            for r in cur.fetchall():
                out.append({
                    "issue_label": r[0], "url": r[1],
                    "classification": r[2],
                    "first_seen_at": r[3].isoformat() if r[3] else None,
                    "last_seen_at": r[4].isoformat() if r[4] else None,
                    "occurrence_count": int(r[5] or 0),
                })
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200
    finally:
        close_quietly(c)
    return jsonify(ok=True, classification_filter=klass,
                   counts_by_class=counts, patterns=out,
                   generated_at=now_iso()), 200


# ─────────────────────────────────────────────────────────────────────
# 4D — Model performance
# ─────────────────────────────────────────────────────────────────────
@brain_learning_bp.route("/api/v1/brain/model-performance", methods=["GET"])
def brain_model_performance():
    """Per-(layer, model) success metrics. Use to decide whether to switch
    models on a layer."""
    _ensure_schema()
    out = []
    c = None
    try:
        c = _conn()
        with c.cursor() as cur:
            cur.execute("""
                SELECT layer, model,
                       COUNT(*) AS runs,
                       COUNT(*) FILTER (WHERE approved = TRUE) AS approvals,
                       COUNT(*) FILTER (WHERE rejected = TRUE) AS rejections,
                       AVG(latency_ms) AS avg_latency,
                       MIN(run_at) AS earliest, MAX(run_at) AS latest
                  FROM brain_model_performance
                 WHERE run_at > NOW() - INTERVAL '60 days'
                 GROUP BY layer, model
                 ORDER BY runs DESC""")
            for r in cur.fetchall():
                runs = int(r[2] or 0); appr = int(r[3] or 0); rej = int(r[4] or 0)
                decided = appr + rej
                out.append({
                    "layer": r[0], "model": r[1],
                    "runs": runs,
                    "approvals": appr,
                    "rejections": rej,
                    "approval_rate_pct": round(100 * appr / max(1, decided), 1) if decided else None,
                    "avg_latency_ms": int(r[5]) if r[5] is not None else None,
                    "earliest_run": r[6].isoformat() if r[6] else None,
                    "latest_run": r[7].isoformat() if r[7] else None,
                })
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200
    finally:
        close_quietly(c)

    # Recommendation per layer: highest approval_rate_pct with >= 10 runs
    rec = {}
    by_layer = {}
    for row in out:
        by_layer.setdefault(row["layer"], []).append(row)
    for layer, rows in by_layer.items():
        eligible = [r for r in rows if r["runs"] >= 10 and r["approval_rate_pct"] is not None]
        if eligible:
            best = max(eligible, key=lambda r: r["approval_rate_pct"])
            rec[layer] = {
                "recommended_model": best["model"],
                "approval_rate_pct": best["approval_rate_pct"],
                "based_on_runs": best["runs"],
            }
    return jsonify(ok=True, model_performance=out,
                   recommendations=rec, generated_at=now_iso()), 200


# ─────────────────────────────────────────────────────────────────────
# 4E — Brain self-assessment (the headline tool)
# ─────────────────────────────────────────────────────────────────────
@brain_learning_bp.route("/api/v1/brain/self-assessment", methods=["GET"])
def brain_self_assessment():
    """Brain grades itself. Returns a letter grade (A/B/C/D/F) with rationale
    derived from concrete metrics. Read this in agents to decide whether to
    trust auto-applied brain fixes vs. fall back to deterministic logic."""
    _ensure_schema()

    # Phase GG Bundle 7: 5-min in-memory cache so /brain dashboard + /status
    # page don't pay the 1.8s recompute cost on every hit. Cache lives in
    # module globals (per-worker; acceptable since the recompute is cheap
    # and stale-by-5-min is fine for a public dashboard).
    import time as _time
    global _SA_CACHE
    try:
        _SA_CACHE
    except NameError:
        _SA_CACHE = {"payload": None, "ts": 0}
    SA_TTL = 300  # 5 minutes
    nocache = (request.args.get("nocache") or "").lower() in ("1", "true")
    if not nocache and _SA_CACHE["payload"] and (_time.time() - _SA_CACHE["ts"]) < SA_TTL:
        resp = jsonify(_SA_CACHE["payload"])
        resp.headers["X-DC-Cache"] = "hit"
        resp.headers["X-DC-Cache-Age"] = str(int(_time.time() - _SA_CACHE["ts"]))
        return resp, 200

    errors = {}

    def _safe(cur, sql, params=(), key=None):
        """Rows, or [] with the failure RECORDED. See the note in #2071.

        ★ A GRADE DERIVED FROM AN UNREAD INPUT IS A FABRICATED GRADE. This
        handler scores the brain out of 4 per component and publishes a letter.
        The old bare-`except` version mapped a failed read to [] and then to 0,
        and a 0 here is not "no data" — it is the WORST possible score, so a
        broken query rendered as a confident F. Components whose input could
        not be read are now dropped from the weighting entirely.
        """
        rows, err = try_fetchall(cur, sql, params)
        if err and key:
            errors[key] = err
        return rows

    metrics = {}
    grade_components = {}
    c = None
    try:
        c = _conn()
        with c.cursor() as cur:
            # Fix-success rate (most important — 35% of grade).
            # HONESTY (2026-06-26): grade on the VERIFIED REAL-EFFECT rate
            # (autopilot_outcomes.succeeded) — the SAME signal /brain/self-model
            # reports — NOT the brain_fix_outcomes re-check-pass rate, which mixed
            # easy link/schema re-checks in and overstated success (~72% = grade A)
            # while real verified effect was ~33% (self-model: "degraded"). The two
            # public surfaces now agree. The re-check rate stays visible, labeled.
            rows = _safe(cur, """
                SELECT COUNT(*) FILTER (WHERE succeeded IS TRUE
                                         AND verified_at >= NOW() - INTERVAL '30 days'),
                       COUNT(*) FILTER (WHERE succeeded IS FALSE
                                         AND verified_at >= NOW() - INTERVAL '30 days')
                  FROM autopilot_outcomes""")
            v_ok = int(rows[0][0] or 0) if rows and rows[0] else 0
            v_fail = int(rows[0][1] or 0) if rows and rows[0] else 0
            v_sample = v_ok + v_fail
            if v_sample >= 5:
                rate = v_ok / v_sample
                metrics["fix_success_rate"] = round(rate * 100, 1)      # verified real-effect
                metrics["verified_effect_sample_30d"] = v_sample
                grade_components["fix_success"] = (
                    4 if rate >= 0.85 else
                    3 if rate >= 0.70 else
                    2 if rate >= 0.50 else
                    1 if rate >= 0.30 else 0)
            else:
                metrics["fix_success_rate"] = None
                grade_components["fix_success"] = None
            # Secondary, labeled: the endpoint re-check-pass rate (does NOT drive the grade).
            rrows = _safe(cur, """
                SELECT COUNT(*) FILTER (WHERE still_broken = FALSE),
                       COUNT(*) FILTER (WHERE still_broken = TRUE)
                  FROM brain_fix_outcomes
                 WHERE checked_at > NOW() - INTERVAL '30 days'""")
            if rrows and rrows[0]:
                rw = int(rrows[0][0] or 0); rf = int(rrows[0][1] or 0)
                if (rw + rf) > 0:
                    metrics["recheck_pass_rate"] = round(rw / (rw + rf) * 100, 1)

            # Human-rejection rate (25% — lower is better)
            rows = _safe(cur, """
                SELECT decision, COUNT(*) FROM brain_review_decisions
                 WHERE decided_at > NOW() - INTERVAL '60 days'
                 GROUP BY decision""")
            by_dec = {r[0]: int(r[1]) for r in rows}
            total = sum(by_dec.values())
            if total > 0:
                rej = by_dec.get('reject', 0) / total
                metrics["human_rejection_rate"] = round(rej * 100, 1)
                metrics["human_review_count_60d"] = total
                grade_components["rejection"] = (
                    4 if rej <= 0.10 else
                    3 if rej <= 0.20 else
                    2 if rej <= 0.40 else
                    1 if rej <= 0.60 else 0)
            else:
                metrics["human_rejection_rate"] = None
                metrics["human_review_count_60d"] = 0
                grade_components["rejection"] = None

            # Cron health (20%) — last_run_at within 2 hours = healthy
            rows = _safe(cur, """
                SELECT value FROM brain_meta WHERE key = 'last_run_at'""")
            if rows and rows[0]:
                try:
                    last = datetime.fromisoformat(str(rows[0][0]).replace('Z', '+00:00'))
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    minutes_ago = (datetime.now(timezone.utc) - last).total_seconds() / 60
                    metrics["minutes_since_last_run"] = round(minutes_ago, 1)
                    grade_components["cron_health"] = (
                        4 if minutes_ago <= 90 else
                        3 if minutes_ago <= 180 else
                        2 if minutes_ago <= 720 else
                        1 if minutes_ago <= 1440 else 0)
                except Exception:
                    grade_components["cron_health"] = None

            # Volume & momentum (10%) — proposals in last 30d
            rows = _safe(cur, """
                SELECT COUNT(*) FROM brain_proposed_fixes
                 WHERE proposed_at > NOW() - INTERVAL '30 days'""",
                key="proposals_text_30d")
            text_30d = int(rows[0][0]) if rows else 0
            rows = _safe(cur, """
                SELECT COUNT(*) FROM brain_proposed_code_fixes
                 WHERE proposed_at > NOW() - INTERVAL '30 days'""",
                key="proposals_code_30d")
            code_30d = int(rows[0][0]) if rows else 0
            if "proposals_text_30d" in errors or "proposals_code_30d" in errors:
                # Unreadable -> null, and NO volume grade. Scoring 0 here would
                # publish "the brain proposed nothing this month" as an F.
                metrics["proposals_30d"] = None
            else:
                metrics["proposals_30d"] = {"text": text_30d, "code": code_30d}
                grade_components["volume"] = (
                    4 if (text_30d + code_30d) >= 30 else
                    3 if (text_30d + code_30d) >= 10 else
                    2 if (text_30d + code_30d) >= 3 else
                    1 if (text_30d + code_30d) >= 1 else 0)

            # Memory depth (10%) — how much state has brain accumulated?
            rows = _safe(cur, """
                SELECT
                  (SELECT COUNT(*) FROM brain_false_positives) AS fp,
                  (SELECT COUNT(*) FROM brain_issue_persistence) AS persist,
                  (SELECT COUNT(*) FROM brain_temporal_patterns) AS temporal""",
                key="memory_depth")
            if "memory_depth" in errors:
                metrics["memory_depth"] = None
            elif rows and rows[0]:
                fp, persist, temporal = rows[0]
                metrics["memory_depth"] = {
                    "false_positives_remembered": int(fp or 0),
                    "issues_persisted": int(persist or 0),
                    "temporal_patterns_classified": int(temporal or 0),
                }
                total_mem = int(fp or 0) + int(persist or 0) + int(temporal or 0)
                grade_components["memory"] = (
                    4 if total_mem >= 200 else
                    3 if total_mem >= 50 else
                    2 if total_mem >= 10 else
                    1 if total_mem >= 1 else 0)
    except Exception as e:
        metrics["error_partial"] = str(e)[:200]
        errors["connection"] = str(e)[:160]
    finally:
        close_quietly(c)

    # Compute weighted grade. Components whose input could not be read are
    # absent from grade_components, so they drop out of both the numerator and
    # `weight_sum` below — the grade is computed over what was actually
    # measured, and `grade_basis` names what is missing rather than letting an
    # unread input silently drag the letter down.
    weights = {"fix_success": 0.35, "rejection": 0.25, "cron_health": 0.20,
               "volume": 0.10, "memory": 0.10}
    score_sum = 0; weight_sum = 0
    for comp, w in weights.items():
        v = grade_components.get(comp)
        if v is not None:
            score_sum += v * w
            weight_sum += w
    weighted = score_sum / weight_sum if weight_sum else None

    if weighted is None:
        letter = "I"  # incomplete
        rationale = "Insufficient data for any grade component — brain is too new or metrics aren't populated yet."
    else:
        letter = ("A" if weighted >= 3.5 else
                  "B" if weighted >= 2.5 else
                  "C" if weighted >= 1.5 else
                  "D" if weighted >= 0.5 else "F")
        rationale = _build_rationale(letter, metrics, grade_components)

    sources = [
        src("Fix-success rate", "brain_fix_outcomes", now_iso()),
        src("Rejection rate", "brain_review_decisions", now_iso()),
        src("Cron health", "brain_meta.last_run_at", now_iso()),
        src("Memory depth", "brain_false_positives + brain_issue_persistence + brain_temporal_patterns", now_iso()),
    ]
    payload = {
        "ok": True,
        "grade": letter,
        "weighted_score": round(weighted, 2) if weighted is not None else None,
        "rationale": rationale,
        "metrics": metrics,
        "component_scores": grade_components,
        "weights": weights,
        # ★ The letter is only as trustworthy as the components behind it. If a
        # read failed, the grade was computed over LESS than the full weighting
        # and a consumer that treats "C or below -> fall back" needs to know
        # that, rather than acting on a letter derived from partial input.
        "grade_complete": not errors,
        **({"query_errors": errors,
            "graded_weight": round(weight_sum, 2),
            "grade_basis": (
                f"PARTIAL - {len(errors)} metric(s) could not be read and were "
                f"EXCLUDED from the grade (weight actually scored: "
                f"{weight_sum:.2f} of 1.00). Unreadable metrics are null, not "
                "0 — scoring them 0 would publish an unread input as an F.")}
           if errors else {}),
        "purpose": ("Brain's letter-grade self-assessment. Agents should "
                    "fall back to deterministic logic when grade is C or below."),
        "drill_deeper": {
            "effectiveness": "/api/v1/brain/effectiveness",
            "outcomes":      "/api/v1/brain/outcomes",
            "temporal":      "/api/v1/brain/temporal-patterns",
            "models":        "/api/v1/brain/model-performance",
            "brain_status":  "/api/v1/brain/status",
        },
    }
    final = attach_sources(payload, sources)
    # Cache for next 5 min (Bundle 7).
    try:
        _SA_CACHE["payload"] = final
        _SA_CACHE["ts"] = _time.time()
    except Exception:
        pass
    resp = jsonify(final)
    resp.headers["X-DC-Cache"] = "miss"
    return resp, 200


def _build_rationale(letter, metrics, comp):
    parts = []
    fsr = metrics.get("fix_success_rate")
    if fsr is not None:
        parts.append(f"fix-success {fsr}%")
    elif comp.get("fix_success") is None:
        parts.append("no outcome verifications yet")
    rr = metrics.get("human_rejection_rate")
    if rr is not None:
        parts.append(f"rejection {rr}%")
    msl = metrics.get("minutes_since_last_run")
    if msl is not None:
        parts.append(f"last cron run {msl:.0f}min ago")
    p30 = metrics.get("proposals_30d") or {}
    if (p30.get("text", 0) + p30.get("code", 0)) > 0:
        parts.append(f"{p30.get('text',0)}+{p30.get('code',0)} proposals/30d")
    md = metrics.get("memory_depth") or {}
    total_mem = (md.get("false_positives_remembered", 0)
                 + md.get("issues_persisted", 0)
                 + md.get("temporal_patterns_classified", 0))
    if total_mem:
        parts.append(f"{total_mem} state rows accumulated")
    base = "; ".join(parts) or "no signal"
    if letter == "A":
        return f"Brain is performing well. {base}."
    if letter == "B":
        return f"Brain is solid but has room to improve. {base}."
    if letter == "C":
        return f"Brain is functional but inconsistent. {base}."
    if letter == "D":
        return f"Brain is struggling — review feedback loop. {base}."
    if letter == "F":
        return f"Brain is failing — cron may be stalled or fixes aren't working. {base}."
    return f"Insufficient signal. {base}."


# ─────────────────────────────────────────────────────────────────────
# Health probe (so we can confirm the module is wired in)
# ─────────────────────────────────────────────────────────────────────
@brain_learning_bp.route("/api/v1/brain/learning/health", methods=["GET"])
def brain_learning_health():
    ok = _ensure_schema()
    tables = {}
    # ★ Already published None for an unreadable table — the right shape. But
    # under `with _conn()` the FIRST failure aborted the transaction, so the
    # remaining three cascaded to None as well and a health check reported all
    # four tables unreadable when only one was. Per-table truth now.
    c = None
    try:
        c = _conn()
        with c.cursor() as cur:
            for t in ("brain_fix_outcomes", "brain_review_decisions",
                      "brain_temporal_patterns", "brain_model_performance"):
                rows, err = try_fetchall(cur, f"SELECT COUNT(*) FROM {t}")
                tables[t] = None if (err or not rows) else int(rows[0][0])
    except Exception:
        pass
    finally:
        close_quietly(c)
    return jsonify(ok=True, schema_ready=ok, tables=tables,
                   generated_at=now_iso()), 200
