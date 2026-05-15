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
    is cheap. Returns True if all DDL ran OK, False if any errored."""
    try:
        with _conn() as c, c.cursor() as cur:
            for ddl in _SCHEMA:
                try:
                    cur.execute(ddl)
                except Exception:
                    return False
        return True
    except Exception:
        return False


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
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM brain_review_decisions
                    WHERE issue_hash = %s AND decision = 'reject'
                      AND decided_at > NOW() - INTERVAL '30 days'""",
                (h,))
            n = cur.fetchone()
            return bool(n and n[0] >= reject_threshold)
    except Exception:
        return False


def record_proposal_outcome(proposal_id, proposal_kind, still_broken,
                            evidence_url=None, evidence_note=None):
    """Record a post-merge outcome check. `proposal_kind` ∈ {'text','code'}.
    `still_broken` = True means the fix didn't work; False means it did.
    Returns True if recorded."""
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
                (proposal_id, proposal_kind, bool(still_broken),
                 (evidence_url or '')[:300],
                 (evidence_note or '')[:500]))
        return True
    except Exception:
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
                                  || jsonb_build_array(NOW()::text)
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

    def _safe(cur, sql, params=()):
        try:
            cur.execute(sql, params)
            return cur.fetchall()
        except Exception:
            return []

    try:
        with _conn() as c, c.cursor() as cur:
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

            # False-positive memory size (existing brain table)
            rows = _safe(cur, """
                SELECT COUNT(*) FROM brain_false_positives
                 WHERE refused_count >= 3""")
            payload["false_positive_memory"] = int(rows[0][0]) if rows else 0

            # Stuck issues (existing brain table)
            rows = _safe(cur, """
                SELECT COUNT(*) FROM brain_issue_persistence
                 WHERE seen_count >= 5""")
            payload["chronic_stuck_issues"] = int(rows[0][0]) if rows else 0

    except Exception as e:
        payload["error_partial"] = str(e)[:200]

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
    try:
        with _conn() as c, c.cursor() as cur:
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
    return jsonify(ok=True, outcomes=out, count=len(out),
                   generated_at=now_iso()), 200


# ─────────────────────────────────────────────────────────────────────
# 4A — Outcome prober (cron-callable)
# ─────────────────────────────────────────────────────────────────────
@brain_learning_bp.route("/api/v1/brain/probe-outcomes", methods=["POST", "GET"])
@_require_admin
def probe_outcomes():
    """Re-check approved-and-applied proposals at +24h and +7d windows.

    For each approved text proposal that was last_seen recently AND we
    haven't outcome-checked yet, ask: 'did the underlying pattern stop
    appearing?' Records the answer in brain_fix_outcomes.
    """
    _ensure_schema()
    checked = 0
    new_outcomes = 0
    errors = []
    try:
        with _conn() as c, c.cursor() as cur:
            # Find approved text proposals from the last 30 days that
            # don't already have an outcome record in the last 24h.
            try:
                cur.execute("""
                    SELECT bp.id, bp.find, bp.last_seen_at,
                           COALESCE(bp.proposed_at, bp.last_seen_at) AS applied_at
                      FROM brain_proposed_fixes bp
                     WHERE bp.approved = TRUE
                       AND COALESCE(bp.proposed_at, bp.last_seen_at) > NOW() - INTERVAL '30 days'
                       AND NOT EXISTS (
                           SELECT 1 FROM brain_fix_outcomes bo
                            WHERE bo.proposal_id = bp.id
                              AND bo.proposal_kind = 'text'
                              AND bo.checked_at > NOW() - INTERVAL '24 hours')
                     ORDER BY bp.last_seen_at DESC NULLS LAST LIMIT 50""")
                candidates = cur.fetchall()
            except Exception as e:
                candidates = []
                errors.append(f"candidate-select: {str(e)[:120]}")

            # The "did it actually fix?" signal: the issue is "still broken"
            # if the SAME find_text appears in a fresh /heal/findings scan.
            # We approximate by checking the existing persistence table —
            # if the issue's last_seen_at moved FORWARD after applied_at,
            # the fix didn't stick.
            for prop_id, find_txt, last_seen, applied_at in candidates:
                checked += 1
                try:
                    # Heuristic: was this find_text seen in persistence after applied_at?
                    cur.execute("""
                        SELECT MAX(last_seen_at) FROM brain_issue_persistence
                         WHERE issue_label ILIKE %s""",
                        (f"%{(find_txt or '')[:40]}%",))
                    row = cur.fetchone()
                    persistence_last = row[0] if row else None
                    still_broken = (persistence_last is not None
                                    and applied_at is not None
                                    and persistence_last > applied_at + timedelta(hours=24))
                    cur.execute("""
                        INSERT INTO brain_fix_outcomes
                            (proposal_id, proposal_kind, applied_at,
                             checked_at, still_broken, evidence_note,
                             check_count)
                        VALUES (%s, %s, %s, NOW() ON CONFLICT DO NOTHING, %s, %s, 1)
                        ON CONFLICT DO NOTHING""",
                        (prop_id, "text", applied_at, bool(still_broken),
                         f"persistence_last={persistence_last.isoformat() if persistence_last else 'none'}"))
                    new_outcomes += 1
                except Exception as e:
                    errors.append(f"prop_id={prop_id}: {str(e)[:60]}")
                    if len(errors) >= 5:
                        break
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 200

    return jsonify(ok=True, candidates_checked=checked,
                   new_outcomes_recorded=new_outcomes,
                   errors=errors[:5], generated_at=now_iso()), 200


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
    try:
        with _conn() as c, c.cursor() as cur:
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
    try:
        with _conn() as c, c.cursor() as cur:
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

    def _safe(cur, sql, params=()):
        try:
            cur.execute(sql, params); return cur.fetchall()
        except Exception:
            return []

    metrics = {}
    grade_components = {}
    try:
        with _conn() as c, c.cursor() as cur:
            # Fix-success rate (most important — 35% of grade)
            rows = _safe(cur, """
                SELECT COUNT(*) FILTER (WHERE still_broken = FALSE),
                       COUNT(*) FILTER (WHERE still_broken = TRUE)
                  FROM brain_fix_outcomes
                 WHERE checked_at > NOW() - INTERVAL '30 days'""")
            if rows and rows[0]:
                worked = int(rows[0][0] or 0); failed = int(rows[0][1] or 0)
                if (worked + failed) > 0:
                    rate = worked / (worked + failed)
                    metrics["fix_success_rate"] = round(rate * 100, 1)
                    grade_components["fix_success"] = (
                        4 if rate >= 0.85 else
                        3 if rate >= 0.70 else
                        2 if rate >= 0.50 else
                        1 if rate >= 0.30 else 0)
                else:
                    metrics["fix_success_rate"] = None
                    grade_components["fix_success"] = None

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
                 WHERE proposed_at > NOW() - INTERVAL '30 days'""")
            text_30d = int(rows[0][0]) if rows else 0
            rows = _safe(cur, """
                SELECT COUNT(*) FROM brain_proposed_code_fixes
                 WHERE proposed_at > NOW() - INTERVAL '30 days'""")
            code_30d = int(rows[0][0]) if rows else 0
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
                  (SELECT COUNT(*) FROM brain_temporal_patterns) AS temporal""")
            if rows and rows[0]:
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

    # Compute weighted grade
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
    return jsonify(attach_sources(payload, sources)), 200


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
    try:
        with _conn() as c, c.cursor() as cur:
            for t in ("brain_fix_outcomes", "brain_review_decisions",
                      "brain_temporal_patterns", "brain_model_performance"):
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {t}")
                    tables[t] = int(cur.fetchone()[0])
                except Exception:
                    tables[t] = None
    except Exception:
        pass
    return jsonify(ok=True, schema_ready=ok, tables=tables,
                   generated_at=now_iso()), 200
