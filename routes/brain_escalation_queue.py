"""brain_escalation_queue.py — the escalations, made durable and countable.
===========================================================================

WHY (measured live, 2026-08-29)
-------------------------------
Nine paying customers are stranded at zero calls. The loop handled them
correctly at every step and still lost them:

  1. `activation_nudge` is ARMED and it FIRED. `email_drip_log` carries an
     `activation_nudge` row for all nine — six at ~39.9 days, tj@karklins
     at 18.4d, rob@hedmarkholdings at 8.7d.
  2. All nine stayed at zero calls.
  3. `customer_white_glove._classify` drew the right conclusion:
       "ESCALATE: nudged 39d ago, still zero calls — automated nudge
        FAILED. Human touch (call / personal note), not another email."
  4. `stranded_candidates` then correctly refuses to re-send, because it
     excludes anyone already nudged. The daily job reports
     `armed: true, candidates: 0` — not broken, DONE. It has concluded
     email won't work and handed off.
  5. ★ The hand-off has no catcher. The escalation is computed inside
     `main.py::_compute_heal_findings()` ON READ and never persisted.
     Live `/api/v1/brain/findings/triage` returns `source_findings: 0` —
     zero findings of any kind, including the
     `customer_nudge_failed_needs_human` that `check_customer_activation_health`
     is registered to emit at `escalate >= 3`, with NINE qualifying rows.

So the escalation exists only as a number rendered on a page nobody is
obliged to open. A correct decision, reached honestly, discarded — the same
defect class as the zero-writer twin and the never-delivered registry row,
one layer up and costing more.

WHAT THIS IS
------------
The catcher. One durable row per escalation, with a status somebody has to
move, and a resolution that is MEASURED rather than asserted.

★ The verifier is the customer's own behaviour. An escalation auto-resolves
as `activated` the moment that account makes its first call — nothing has to
be marked done for the good outcome to be recorded. That makes each row a
claim in the sense the claim ledger already means it: a bounded statement
("a human touch will activate this account") with a scalar outcome at a
horizon, gradeable without anyone's self-report. `activated` vs
`contacted`-and-still-silent is the first honest read this loop has ever had
on whether human touches work at all.

Statuses: open -> contacted -> activated (measured) | resolved | dismissed

NOT A SENDER. This module has no email path and imports none. The terminal
act is deliberately human — step 3 above concluded that another automated
email is the wrong move, and this respects it. All it guarantees is that the
nine cannot be silently lost.

SAFETY
------
Admin-gated. Writes only its own table. `sync` is idempotent (upsert on
email) and never re-opens a row a human closed — only the customer's own
first call can reopen the story, and that writes `activated`, not `open`.
DDL through `ddl_cursor` (the pooled wrapper silently skips DDL when
SKIP_DDL is set, which it is by default on Railway).

ROUTES
------
  GET  /api/v1/brain/escalations                  the queue (open first)
  POST /api/v1/brain/escalations/sync             refresh from the roster
  POST /api/v1/brain/escalations/resolve          {email|id, status, note, by}

Plain function for the shells (JSON-safe, never raises):
  sync() -> dict   ·   queue(status=...) -> dict
"""
from __future__ import annotations

import json
import logging
import os

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

brain_escalation_queue_bp = Blueprint("brain_escalation_queue", __name__)

# A human moved it, or the customer did. Anything else is not a resolution.
_OPEN = "open"
_TERMINAL = ("activated", "resolved", "dismissed")
_SETTABLE = ("contacted", "resolved", "dismissed")


def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or
                os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or request.headers.get("Authorization", "").replace("Bearer ", "").strip())
    return bool(expected) and provided == expected


def ensure_schema() -> bool:
    """DDL through ddl_cursor — the pooled wrapper skips CREATE TABLE when
    SKIP_DDL is set, and it defaults to set on Railway. That trap hid a
    table for three months; this is the marked path around it."""
    try:
        from db_utils import ddl_cursor
        with ddl_cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_escalations (
                    id             BIGSERIAL PRIMARY KEY,
                    email          TEXT NOT NULL UNIQUE,
                    name           TEXT,
                    plan           TEXT,
                    stage          TEXT,
                    reason         TEXT,
                    priority       INTEGER NOT NULL DEFAULT 3,
                    context        JSONB,
                    status         TEXT NOT NULL DEFAULT 'open',
                    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    contacted_at   TIMESTAMPTZ,
                    resolved_at    TIMESTAMPTZ,
                    resolved_by    TEXT,
                    resolution_note TEXT,
                    calls_at_open  INTEGER NOT NULL DEFAULT 0
                )""")
            cur.execute("CREATE INDEX IF NOT EXISTS brain_escalations_open_idx "
                        "ON brain_escalations (status, priority DESC, "
                        "first_seen_at)")
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[escalations] ensure_schema failed: %s", str(e)[:160])
        return False


def _roster_escalations() -> list:
    """The white-glove board's own verdict. Never re-derived here — a second
    opinion on who is escalating would be a second source of truth."""
    try:
        from routes.customer_white_glove import _roster
    except Exception:
        from customer_white_glove import _roster  # local shell
    return [r for r in (_roster() or []) if r.get("escalate")]


def sync() -> dict:
    """Idempotent. Upserts a row per currently-escalating customer, and
    auto-resolves any open row whose customer has started calling.

    Never re-opens a row a human closed: the ON CONFLICT refreshes context
    and last_seen_at only. The one thing that CAN overrule a human is the
    customer themselves — first call writes `activated`, which is the
    outcome the whole queue exists to produce.
    """
    if not ensure_schema():
        return {"ok": False, "error": "schema unavailable"}

    from db_utils import safe_db
    opened = refreshed = activated = 0
    try:
        rows = _roster_escalations()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"roster unavailable: {str(e)[:140]}"}

    try:
        with safe_db() as conn:
            cur = conn.cursor()
            for r in rows:
                ctx = {k: r.get(k) for k in
                       ("total_calls", "mcp_calls", "web_calls", "joined_days",
                        "idle_days", "nudge_days", "welcomed", "nudged",
                        "welcome_attempted")}
                cur.execute("""
                    INSERT INTO brain_escalations
                        (email, name, plan, stage, reason, priority, context,
                         calls_at_open)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (email) DO UPDATE
                       SET last_seen_at = NOW(),
                           reason  = EXCLUDED.reason,
                           context = EXCLUDED.context,
                           priority = EXCLUDED.priority
                    RETURNING (xmax = 0) AS inserted
                """, (r.get("email"), r.get("name"), r.get("plan"),
                      r.get("stage"), r.get("action"),
                      int(r.get("priority") or 3), json.dumps(ctx),
                      int(r.get("total_calls") or 0)))
                got = cur.fetchone()
                if got and got[0]:
                    opened += 1
                else:
                    refreshed += 1

            # ★ the verifier: they started calling. Nobody has to mark this.
            still = {(r.get("email") or "").lower() for r in rows}
            cur.execute("SELECT id, email FROM brain_escalations "
                        "WHERE status IN ('open', 'contacted')")
            for eid, email in (cur.fetchall() or ()):
                if (email or "").lower() in still:
                    continue
                cur.execute("""
                    UPDATE brain_escalations
                       SET status = 'activated', resolved_at = NOW(),
                           resolved_by = 'system',
                           resolution_note = 'no longer escalating — the '
                                             'account is making calls'
                     WHERE id = %s""", (eid,))
                activated += 1
            conn.commit()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}

    return {"ok": True, "escalating_now": len(rows), "opened": opened,
            "refreshed": refreshed, "auto_activated": activated,
            "basis": ("rows from customer_white_glove._roster where "
                      "escalate=True; auto_activated = open rows that "
                      "dropped off it, i.e. the account started calling")}


def queue(status: str = _OPEN, limit: int = 100) -> dict:
    """The worked queue. JSON-safe, never raises."""
    if not ensure_schema():
        return {"ok": False, "error": "schema unavailable", "rows": []}
    from db_utils import safe_db
    out, counts = [], {}
    try:
        with safe_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT status, COUNT(*) FROM brain_escalations "
                        "GROUP BY status")
            counts = {s: int(n) for s, n in (cur.fetchall() or ())}
            if status == "all":
                cur.execute(
                    "SELECT id, email, name, plan, reason, priority, status, "
                    "first_seen_at, contacted_at, resolved_at, resolved_by, "
                    "resolution_note, context FROM brain_escalations "
                    "ORDER BY (status = 'open') DESC, priority DESC, "
                    "first_seen_at LIMIT %s", (int(limit),))
            else:
                cur.execute(
                    "SELECT id, email, name, plan, reason, priority, status, "
                    "first_seen_at, contacted_at, resolved_at, resolved_by, "
                    "resolution_note, context FROM brain_escalations "
                    "WHERE status = %s ORDER BY priority DESC, first_seen_at "
                    "LIMIT %s", (status, int(limit)))
            cols = ("id", "email", "name", "plan", "reason", "priority",
                    "status", "first_seen_at", "contacted_at", "resolved_at",
                    "resolved_by", "resolution_note", "context")
            for row in (cur.fetchall() or ()):
                d = dict(zip(cols, row))
                for k in ("first_seen_at", "contacted_at", "resolved_at"):
                    if d.get(k) is not None and hasattr(d[k], "isoformat"):
                        d[k] = d[k].isoformat()
                out.append(d)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200], "rows": []}

    open_n = counts.get("open", 0) + counts.get("contacted", 0)
    return {
        "ok": True, "counts": counts, "open_total": open_n,
        "rows": out,
        "note": ("Terminal action is HUMAN by design — the loop already "
                 "concluded another automated email is the wrong move. "
                 "`activated` is measured from the account's own first "
                 "call, never self-reported."),
    }


def _set_status(ident, status: str, note: str, by: str) -> dict:
    if status not in _SETTABLE:
        return {"ok": False,
                "error": f"status must be one of {', '.join(_SETTABLE)} "
                         f"— 'activated' is measured, never set by hand"}
    if not ensure_schema():
        return {"ok": False, "error": "schema unavailable"}
    from db_utils import safe_db
    try:
        with safe_db() as conn:
            cur = conn.cursor()
            where, param = (("id = %s", int(ident)) if str(ident).isdigit()
                            else ("lower(email) = lower(%s)", str(ident)))
            cur.execute(f"""
                UPDATE brain_escalations
                   SET status = %s,
                       resolution_note = %s,
                       resolved_by = %s,
                       contacted_at = CASE WHEN %s = 'contacted'
                                           THEN NOW() ELSE contacted_at END,
                       resolved_at  = CASE WHEN %s IN ('resolved', 'dismissed')
                                           THEN NOW() ELSE resolved_at END
                 WHERE {where}
                 RETURNING id, email, status""",
                        (status, note or None, by or "admin", status, status,
                         param))
            row = cur.fetchone()
            conn.commit()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    if not row:
        return {"ok": False, "error": f"no escalation matching {ident!r}"}
    return {"ok": True, "id": row[0], "email": row[1], "status": row[2]}


# ── routes ───────────────────────────────────────────────────────────────

@brain_escalation_queue_bp.route("/api/v1/brain/escalations", methods=["GET"])
def escalations_route():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    return jsonify(queue(status=request.args.get("status", _OPEN),
                         limit=int(request.args.get("limit", 100))))


@brain_escalation_queue_bp.route("/api/v1/brain/escalations/sync",
                                 methods=["POST"])
def escalations_sync_route():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    return jsonify(sync())


@brain_escalation_queue_bp.route("/api/v1/brain/escalations/resolve",
                                 methods=["POST"])
def escalations_resolve_route():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    b = request.get_json(silent=True) or {}
    ident = b.get("id") or b.get("email")
    if not ident:
        return jsonify(ok=False, error="id or email required"), 400
    out = _set_status(ident, str(b.get("status") or ""), b.get("note") or "",
                      b.get("by") or "admin")
    return jsonify(out), (200 if out.get("ok") else 400)


def register_brain_escalation_queue(app) -> bool:
    app.register_blueprint(brain_escalation_queue_bp)
    return True
