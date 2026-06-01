"""Phase r60-evolution (2026-05-29) — Brain evolution visibility surface.

The user asked "how is our brain evolving?" — the existing /brain/heartbeat
answers "is it alive right now?", but never "is it getting better over
time?". /brain/value-shipped answers "what did it produce?" but not "how
much of what it did was AUTONOMOUS vs nudged by a human?".

This module is the longitudinal evolution view. Two things:

  1. GET /api/v1/brain/evolution — single endpoint returning autonomy +
     learning deltas across 24h / 7d / 30d windows, plus a 0-100
     evolution score so the operator can see ONE number trend in the
     right direction.

  2. brain_notifications table + log_notification() helper — every
     significant brain decision (heal a stuck issue, accept an L5
     proposal, ship a code fix, sync static files) writes a row here
     PLUS a stderr line tagged BRAIN_EVOLUTION:. The user said
     "LinkedIn shows 0 notifications — they want to FEEL the brain
     working" — this is the feed.

Design rules (per the task spec):
  - Single-DB queries only — no remote fetches, no worker-thread hammer
  - 5-min cache so dashboard polls don't repeat-query Postgres
  - Backward-compatible: heartbeat consumers ignore the new
    `evolution` sub-block if they don't know it
  - Fail-soft: missing tables → null fields, never 5xx
"""

from __future__ import annotations

import os
import sys
import time as _time
import datetime as _dt
import json as _json
from flask import Blueprint, jsonify, request
import psycopg2
import psycopg2.extras


brain_evolution_bp = Blueprint("brain_evolution", __name__)


# ─────────────────────────────────────────────────────────────────────────
# Connection
# ─────────────────────────────────────────────────────────────────────────


def _conn():
    """Short-lived DB connection. Returns None when DATABASE_URL absent
    or connect fails. Caller MUST handle None (fail-soft)."""
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────
# Notifications: lightweight brain decision stream
# ─────────────────────────────────────────────────────────────────────────
#
# The user is staring at their LinkedIn notification badge showing 0 —
# they have no visceral signal the brain is doing anything. This table
# captures every significant autonomous decision so a downstream UI
# (notification bell, slack feed, daily digest email) can pull from
# ONE place. log_notification() is fire-and-forget; failure to write
# never blocks the brain's actual work.


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS brain_notifications (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL,
    summary     TEXT NOT NULL,
    detail      JSONB,
    url         TEXT,
    severity    TEXT NOT NULL DEFAULT 'info',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS brain_notifications_recent_idx
    ON brain_notifications(created_at DESC);
CREATE INDEX IF NOT EXISTS brain_notifications_kind_idx
    ON brain_notifications(kind, created_at DESC);
"""

_SCHEMA_INITIALIZED = {"ok": False}


def _ensure_schema() -> bool:
    """Idempotent. Safe to call repeatedly — only fires DDL once per process."""
    if _SCHEMA_INITIALIZED["ok"]:
        return True
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA_DDL)
        _SCHEMA_INITIALIZED["ok"] = True
        return True
    except Exception as e:
        print(f"[brain_evolution] schema init failed: {e}",
              file=sys.stderr, flush=True)
        return False
    finally:
        try: c.close()
        except Exception: pass


# Best-effort at import — failure is non-fatal
try: _ensure_schema()
except Exception: pass


def log_notification(
    kind: str,
    summary: str,
    *,
    detail: dict | None = None,
    url: str | None = None,
    severity: str = "info",
) -> bool:
    """Record a significant brain decision. Fire-and-forget.

    Args:
        kind: short machine identifier, e.g. 'heal_stuck_issue',
              'l5_proposal_accepted', 'static_sync', 'autopilot_action'.
        summary: human-readable single sentence, e.g.
              "Healed stuck issue 'mcp_health_stale' after 7 cycles".
        detail: optional structured context (jsonb) for the dashboard.
        url: optional permalink to the affected surface.
        severity: 'info' | 'win' | 'warn'. UI can colour-code.

    Returns True on write success, False otherwise. Failure ALSO emits
    a stderr line tagged BRAIN_EVOLUTION: so the signal isn't lost even
    when the DB is down (Railway logs become the audit trail).
    """
    # Tagged log line so grep / log aggregators can rebuild the feed
    # even if the DB write fails. Single line, no newlines in summary.
    safe_summary = (summary or "")[:240].replace("\n", " ").replace("\r", " ")
    try:
        print(
            f"BRAIN_EVOLUTION: kind={kind} severity={severity} {safe_summary}",
            file=sys.stderr, flush=True,
        )
    except Exception:
        pass

    if not _ensure_schema():
        return False
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO brain_notifications
                       (kind, summary, detail, url, severity)
                   VALUES (%s, %s, %s::jsonb, %s, %s) ON CONFLICT DO NOTHING""",
                (kind, safe_summary,
                 _json.dumps(detail or {}, default=str),
                 url, severity),
            )
        return True
    except Exception as e:
        print(f"[brain_evolution] log_notification write failed: {e}",
              file=sys.stderr, flush=True)
        return False
    finally:
        try: c.close()
        except Exception: pass


# ─────────────────────────────────────────────────────────────────────────
# Evolution snapshot computation
# ─────────────────────────────────────────────────────────────────────────


_EVOLUTION_CACHE: dict = {"payload": None, "ts": 0.0}
_EVOLUTION_TTL_S = 180.0  # 3 min — dashboards poll every 60s; this absorbs
                          # 3 polls per recompute, which keeps Postgres
                          # cool while still feeling near-real-time.


def _safe_one(cur, query: str, default=None):
    """Run a query expecting ONE scalar. Returns default on any error
    (table missing, column missing, transaction aborted)."""
    try:
        cur.execute(query)
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else default
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
        return default


def _safe_dual(cur, query: str, default=(0, 0)) -> tuple[int, int]:
    """Run a query expecting (count_a, count_b). Returns (0, 0) on error."""
    try:
        cur.execute(query)
        row = cur.fetchone()
        if not row:
            return default
        return (int(row[0] or 0), int(row[1] or 0))
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
        return default


def _safe_rows(cur, query: str, default=None):
    """Run a query expecting many rows. Returns default ([]) on error."""
    if default is None:
        default = []
    try:
        cur.execute(query)
        return cur.fetchall() or default
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
        return default


def compute_evolution_snapshot() -> dict:
    """Build the full evolution payload. Single DB connection, all
    queries fail-soft. Caller is responsible for caching."""
    out: dict = {
        "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
        "ok": True,
    }
    c = _conn()
    if c is None:
        out["ok"] = False
        out["error"] = "db_unavailable"
        return out

    try:
        with c.cursor() as cur:
            # ── Findings resolved (from brain_issue_persistence) ──
            # An issue is "resolved" when:
            #   - it has a positive last_outcome (proposed/approved/applied), OR
            #   - it stopped being seen (last_seen_at older than 24h
            #     but first_seen_at within the window — i.e. brain
            #     reported it, then it disappeared)
            # 7d window — count distinct (issue_label, url) rows that
            # appear newly-quiet inside the window AND had at least one
            # sighting before they went quiet.
            findings_resolved_7d = _safe_one(cur, """
                SELECT COUNT(*) FROM brain_issue_persistence
                 WHERE (last_outcome IN ('proposed','approved',
                                         'approval_count_incremented',
                                         'applied','shipped','merged')
                        AND last_seen_at >= NOW() - INTERVAL '7 days')
                    OR (last_seen_at < NOW() - INTERVAL '1 day'
                        AND last_seen_at >= NOW() - INTERVAL '7 days'
                        AND first_seen_at < last_seen_at)
            """, default=0)
            findings_resolved_30d = _safe_one(cur, """
                SELECT COUNT(*) FROM brain_issue_persistence
                 WHERE (last_outcome IN ('proposed','approved',
                                         'approval_count_incremented',
                                         'applied','shipped','merged')
                        AND last_seen_at >= NOW() - INTERVAL '30 days')
                    OR (last_seen_at < NOW() - INTERVAL '1 day'
                        AND last_seen_at >= NOW() - INTERVAL '30 days'
                        AND first_seen_at < last_seen_at)
            """, default=0)

            # ── Top 3 resolved finding types ──
            top_resolved_rows = _safe_rows(cur, """
                SELECT issue_label, COUNT(*) AS n
                  FROM brain_issue_persistence
                 WHERE last_seen_at >= NOW() - INTERVAL '30 days'
                   AND (last_outcome IN ('proposed','approved',
                                         'approval_count_incremented',
                                         'applied','shipped','merged')
                        OR last_seen_at < NOW() - INTERVAL '1 day')
                 GROUP BY issue_label
                 ORDER BY n DESC, issue_label ASC
                 LIMIT 3
            """, default=[])
            top_3_resolved = [
                {"issue_label": r[0], "resolved_count": int(r[1] or 0)}
                for r in top_resolved_rows
            ]

            # ── Autonomous actions (24h / 30d) ──
            ap_24h, ap_30d = _safe_dual(cur, """
                SELECT
                  COUNT(*) FILTER (WHERE outcome = 'executed_ok'
                                    AND started_at >= NOW() - INTERVAL '24 hours'),
                  COUNT(*) FILTER (WHERE outcome = 'executed_ok'
                                    AND started_at >= NOW() - INTERVAL '30 days')
                  FROM brain_autopilot_actions
            """, default=(0, 0))
            ap_errors_24h, ap_escalations_24h = _safe_dual(cur, """
                SELECT
                  COUNT(*) FILTER (WHERE outcome = 'execution_failed'
                                    AND started_at >= NOW() - INTERVAL '24 hours'),
                  COUNT(*) FILTER (WHERE outcome = 'escalated'
                                    AND started_at >= NOW() - INTERVAL '24 hours')
                  FROM brain_autopilot_actions
            """, default=(0, 0))

            # ── Layer 5 proposals (30d) ──
            # brain_proposed_code_fixes has a `status` column moving
            # proposed → pr_opened → merged_healthy → reverted (or
            # rejected). Count proposed in 30d, accepted (= pr_opened or
            # merged_healthy), rejected (= 'rejected' / 'reverted').
            l5_proposed_30d = _safe_one(cur, """
                SELECT COUNT(*) FROM brain_proposed_code_fixes
                 WHERE proposed_at >= NOW() - INTERVAL '30 days'
            """, default=0)
            l5_accepted_30d = _safe_one(cur, """
                SELECT COUNT(*) FROM brain_proposed_code_fixes
                 WHERE proposed_at >= NOW() - INTERVAL '30 days'
                   AND status IN ('pr_opened','merged_healthy','approved',
                                  'applied','shipped','merged')
            """, default=0)
            l5_rejected_30d = _safe_one(cur, """
                SELECT COUNT(*) FROM brain_proposed_code_fixes
                 WHERE proposed_at >= NOW() - INTERVAL '30 days'
                   AND status IN ('rejected','reverted','dismissed')
            """, default=0)

            # ── Heal cache hits (24h) ──
            # Each row in heal_findings_cache represents one cron-driven
            # refresh that wrote to DB. Treat each row as a "cache hit
            # producing fresh material for the brain". When the table
            # is missing, gracefully null out.
            heal_hits_24h = _safe_one(cur, """
                SELECT COUNT(*) FROM heal_findings_cache
                 WHERE computed_at >= NOW() - INTERVAL '24 hours'
            """, default=None)

            # ── Decisions taken without human approval (30d) ──
            # Composite: autopilot actions executed + L5 proposals
            # auto-promoted + autonomous press releases + autonomous
            # LinkedIn posts. Anything the brain ran without explicit
            # human click.
            ap_executed_30d = ap_30d  # already counted above
            press_30d = _safe_one(cur, """
                SELECT COUNT(*) FROM auto_press_releases
                 WHERE generated_at >= NOW() - INTERVAL '30 days'
            """, default=0)
            li_30d = _safe_one(cur, """
                SELECT COUNT(*) FROM auto_press_releases
                 WHERE linkedin_sent_at IS NOT NULL
                   AND linkedin_sent_at >= NOW() - INTERVAL '30 days'
            """, default=0)
            decisions_30d = int(
                (ap_executed_30d or 0)
                + (l5_accepted_30d or 0)
                + (press_30d or 0)
                + (li_30d or 0)
            )

            # ── Notification stream depth (sanity check / proof of life) ──
            notif_24h = _safe_one(cur, """
                SELECT COUNT(*) FROM brain_notifications
                 WHERE created_at >= NOW() - INTERVAL '24 hours'
            """, default=0)
            notif_7d = _safe_one(cur, """
                SELECT COUNT(*) FROM brain_notifications
                 WHERE created_at >= NOW() - INTERVAL '7 days'
            """, default=0)

    finally:
        try: c.close()
        except Exception: pass

    # ── Evolution score (0-100) ──
    # Composite of three normalised components, each capped at 1.0:
    #   self_resolve_ratio = findings_resolved_7d / max(7, baseline)
    #     — caps when brain self-resolves 35+ findings in 7d
    #   autonomy_ratio     = autonomous_actions_24h / baseline (5 actions/day)
    #     — caps when 5+ autonomous actions happen daily
    #   learning_ratio     = l5_accepted_30d / max(l5_proposed_30d, 1)
    #     — proportion of code proposals that landed
    #
    # Weights: 40% self-resolve (the core "brain heals itself" signal)
    #          30% autonomy (the "doing things" signal)
    #          30% learning (the "getting better at code" signal)
    fr_7d = int(findings_resolved_7d or 0)
    self_resolve_ratio = min(1.0, fr_7d / 35.0)
    autonomy_ratio = min(1.0, int(ap_24h or 0) / 5.0)
    learning_ratio = (
        (int(l5_accepted_30d or 0) / max(int(l5_proposed_30d or 1), 1))
        if int(l5_proposed_30d or 0) > 0 else 0.0
    )
    evolution_score = round(
        (self_resolve_ratio * 40.0)
        + (autonomy_ratio    * 30.0)
        + (learning_ratio    * 30.0),
        1,
    )

    # Verdict ladder mirrors heartbeat style for consistency
    if evolution_score >= 70.0:
        verdict = "ascending"
        verdict_detail = (
            f"score {evolution_score}/100 — brain self-resolved {fr_7d} "
            f"findings + ran {int(ap_24h or 0)} autonomous actions in last 24h"
        )
    elif evolution_score >= 40.0:
        verdict = "steady"
        verdict_detail = (
            f"score {evolution_score}/100 — moderate autonomy, "
            f"{fr_7d} findings resolved in 7d"
        )
    elif evolution_score >= 15.0:
        verdict = "warming"
        verdict_detail = (
            f"score {evolution_score}/100 — some signal but most "
            "decisions still need human action"
        )
    else:
        verdict = "quiet"
        verdict_detail = (
            f"score {evolution_score}/100 — brain is not autonomously "
            "doing much; check cron health + autopilot disable env"
        )

    out.update({
        "verdict": verdict,
        "verdict_detail": verdict_detail,
        "evolution_score": evolution_score,
        "components": {
            "self_resolve_ratio": round(self_resolve_ratio, 3),
            "autonomy_ratio":     round(autonomy_ratio, 3),
            "learning_ratio":     round(learning_ratio, 3),
        },
        "findings_resolved_7d":    fr_7d,
        "findings_resolved_30d":   int(findings_resolved_30d or 0),
        "autonomous_actions_24h":  int(ap_24h or 0),
        "autonomous_actions_30d":  int(ap_30d or 0),
        "autonomous_errors_24h":   int(ap_errors_24h or 0),
        "autonomous_escalations_24h": int(ap_escalations_24h or 0),
        "layer_5_proposals_30d": {
            "submitted": int(l5_proposed_30d or 0),
            "accepted":  int(l5_accepted_30d or 0),
            "rejected":  int(l5_rejected_30d or 0),
        },
        "heal_cache_hits_24h":   (int(heal_hits_24h) if heal_hits_24h is not None
                                  else None),
        "decisions_taken_30d":   decisions_30d,
        "top_3_resolved_finding_types": top_3_resolved,
        "notifications": {
            "last_24h": int(notif_24h or 0),
            "last_7d":  int(notif_7d or 0),
        },
        "purpose": (
            "Longitudinal evolution view — answers 'how is the brain "
            "evolving?'. Pairs with /api/v1/brain/heartbeat (point-in-time "
            "vitals) and /api/v1/brain/value-shipped (what got produced). "
            "Read by the /brain dashboard + included as the `evolution` "
            "sub-block of heartbeat for backward-compatible adoption."
        ),
    })
    return out


@brain_evolution_bp.get("/api/v1/brain/evolution-alt")
@brain_evolution_bp.get("/api/v1/brain/evolution")
def brain_evolution():
    """Public — brain evolution snapshot. Cache-controlled for dashboard
    polling. Pass ?force=1 to bypass the cache (rare; admin use).
    """
    force = (request.args.get("force") or "").lower() in ("1", "true", "yes")
    now = _time.time()
    cached = _EVOLUTION_CACHE["payload"]
    age = (now - _EVOLUTION_CACHE["ts"]) if cached is not None else None

    if not force and cached is not None and age < _EVOLUTION_TTL_S:
        resp = dict(cached)
        resp["_cache_age_seconds"] = round(age, 1)
        resp["_cached"] = True
        out = jsonify(resp)
        out.headers["Cache-Control"] = "public, max-age=60, s-maxage=120"
        out.headers["Access-Control-Allow-Origin"] = "*"
        return out, 200

    payload = compute_evolution_snapshot()
    _EVOLUTION_CACHE["payload"] = payload
    _EVOLUTION_CACHE["ts"]      = now
    out = jsonify(payload)
    out.headers["Cache-Control"] = "public, max-age=60, s-maxage=120"
    out.headers["Access-Control-Allow-Origin"] = "*"
    return out, 200


@brain_evolution_bp.get("/api/v1/brain/notifications")
def brain_notifications():
    """Public — recent brain decisions feed. Default 50, cap 200."""
    try: limit = max(1, min(200, int(request.args.get("limit") or 50)))
    except ValueError: limit = 50
    c = _conn()
    if c is None:
        return jsonify(ok=False, error="db_unavailable",
                       items=[], count=0), 200
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute("""
                    SELECT id, kind, summary, detail, url, severity, created_at
                      FROM brain_notifications
                     ORDER BY created_at DESC
                     LIMIT %s
                """, (limit,))
                rows = cur.fetchall() or []
            except Exception:
                # Table may not exist yet on a brand-new boot
                rows = []
        items = []
        for r in rows:
            items.append({
                "id":         r["id"],
                "kind":       r["kind"],
                "summary":    r["summary"],
                "detail":     r["detail"],
                "url":        r["url"],
                "severity":   r["severity"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            })
        out = jsonify(ok=True, count=len(items), items=items)
        out.headers["Cache-Control"] = "public, max-age=30"
        out.headers["Access-Control-Allow-Origin"] = "*"
        return out, 200
    finally:
        try: c.close()
        except Exception: pass
