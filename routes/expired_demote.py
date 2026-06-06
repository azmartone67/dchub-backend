"""
routes/expired_demote.py — Nightly demote cron for expired one-time tier buyers (2026-06-06).

Background
----------
Commit 594756e8 ("fix(stripe-webhook): track tier_expires_at + source_plan on
one-time Pro Annual ($1,188)") fixed the IMMEDIATE entitlement on the new
mode='payment' Pro Annual checkout — buyers get plan='pro' on
checkout.session.completed and we now ALSO stamp
`users.tier_expires_at = NOW() + 365 days` + `users.source_plan = 'pro_annual_onetime'`
so the expiry is TRACKED.

But tracked != enforced. Without this cron, year-2/3/4 buyers stay tier='pro'
forever even past 365 days. This module is the enforcement leg of that fix.

Scope
-----
ONE-TIME-PURCHASE TIERS ONLY. Subscription-mode buyers (mode='subscription' on
Stripe) leave `tier_expires_at` NULL — their expiry follows
`subscription_status` / Stripe auto-renew, which is already handled by
`handle_subscription_deleted` in main.py. We MUST NOT touch them.

We isolate one-time buyers via `source_plan ILIKE '%_onetime'` (set by the
webhook at checkout). Anything without that label is out of scope.

Demote
------
On expiry we mirror the existing subscription-canceled downgrade path
(see main.py handle_subscription_deleted ~line 12092):

  - users.plan                = 'free'
  - users.role                = 'free'
  - users.subscription_status = 'expired'
  - users.demoted_at          = NOW()
  - users.demoted_reason      = 'tier_expired_onetime'
  - api_keys.rate_limit_tier  = 'free' (for the user's keys)

The `demoted_at` + `demoted_reason` columns already exist on `users` (added
in routes/schema_repair.py for the Stripe-dunning demote path; verified
2026-06-06 — see lines 139-140). No schema change needed.

Endpoints
---------
GET  /api/v1/admin/lifecycle/demote-preview
    Admin-keyed. Shows what WOULD be demoted right now, no DB write.
    Same query as the live demote, just no UPDATE. Cron operators use
    this to sanity-check before letting the nightly run go live.

POST /api/v1/admin/lifecycle/demote-expired
    Admin-keyed OR cron secret. Runs the LIVE demote. Default LIVE
    because the cron is the primary caller; pass DCHUB_DEMOTE_DRY_RUN=1
    in the environment to force dry-run on any caller (belt-and-suspenders
    kill switch). The `?dry=1` query param does the same per-call.

Cron
----
SCHEDULE slot (3, 3, "expired_onetime_demote") in crawler_scheduler.py.
03:00 UTC daily (an empty hour between the 02:00 tool_calibration slot
and 04:00 gas_pricing_refresh — no overlap with any existing crawler).
Same-hour-same-day cap → one run per UTC day.

Safety
------
  - Targets only `source_plan ILIKE '%_onetime'` AND tier_expires_at < NOW()
    AND plan != 'free' — three conjunctive predicates. Even an accidental
    NULL tier_expires_at can never match.
  - DCHUB_DEMOTE_DRY_RUN=1 kill switch.
  - Per-row try/except — one bad row never poisons the rest.
  - Audit via brain_findings (constraint-agnostic upsert) so an operator
    can see "expired_demote_run" rows on the brain dashboard alongside
    every other lifecycle event.
  - Defensive everywhere — the runner is called from the scheduler
    thread; uncaught exceptions kill that thread.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

from flask import Blueprint, jsonify, request


logger = logging.getLogger(__name__)

expired_demote_bp = Blueprint("expired_demote", __name__)


# ── DB plumbing (mirrors market_verdict_shifts._db_conn) ─────────────


def _db_conn():
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        return psycopg2.connect(url, connect_timeout=5) if url else None
    except Exception as e:
        logger.warning("expired_demote db_conn failed: %s", e)
        return None


def _admin_or_cron_authorized() -> bool:
    """Same gate as market_verdict_shifts — admin key OR cron secret."""
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or request.args.get("key") or "")
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    if expected and provided == expected:
        return True
    cron_hdr = request.headers.get("X-Internal-Cron", "")
    cron_env = os.environ.get("DCHUB_CRON_SECRET", "")
    return bool(cron_env) and cron_hdr == cron_env


def _dry_run_env() -> bool:
    """Kill switch: DCHUB_DEMOTE_DRY_RUN=1 forces dry-run regardless of caller."""
    v = (os.environ.get("DCHUB_DEMOTE_DRY_RUN", "") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _log(msg: str) -> None:
    try:
        sys.stderr.write(f"[expired-demote] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


# ── Core ──────────────────────────────────────────────────────────────


# SELECT used by both preview and live paths. Source-of-truth predicate.
_SELECT_EXPIRED_SQL = """
    SELECT id, email, plan, source_plan, tier_expires_at, stripe_customer_id
      FROM users
     WHERE tier_expires_at IS NOT NULL
       AND tier_expires_at < NOW()
       AND source_plan ILIKE %s
       AND plan != 'free'
     ORDER BY tier_expires_at ASC
     LIMIT %s
"""

# UPDATE writes plan/role/subscription_status + demoted_* in one shot.
# RETURNING gives us the rows we actually changed so the caller can log
# them and (if extended later) fire a renewal-nudge email.
_UPDATE_DEMOTE_SQL = """
    UPDATE users
       SET plan = 'free',
           role = 'free',
           subscription_status = 'expired',
           demoted_at = NOW(),
           demoted_reason = 'tier_expired_onetime'
     WHERE id = %s
 RETURNING id, email, source_plan, tier_expires_at
"""

# Downgrade api_keys to free rate-limit tier too — mirrors the canonical
# subscription-canceled path in handle_subscription_deleted (main.py ~12102).
_UPDATE_KEYS_SQL = """
    UPDATE api_keys
       SET rate_limit_tier = 'free'
     WHERE user_id = %s
"""


def _audit_finding(cur, summary: str, count: int) -> None:
    """Write a row into brain_findings via the canonical writer so
    operators see the demote activity on the brain dashboard. Soft-fails."""
    try:
        from routes.brain_findings_writer import upsert_brain_finding
        upsert_brain_finding(
            cur,
            issue="expired_onetime_demote_run",
            url="/api/v1/admin/lifecycle/demote-expired",
            count=max(1, int(count)),
            detail=summary[:1000],
            detector="expired_demote_cron",
            status="open",
        )
    except Exception as e:
        _log(f"audit_finding skipped: {e}")


def run_expired_demote(dry_run: bool = False, limit: int = 500) -> dict:
    """Find expired one-time buyers and (live) demote them to plan='free'.

    Returns:
        dict with keys:
          dry_run    : bool
          checked    : int    (rows matched by SELECT)
          demoted    : int    (rows updated; 0 in dry_run)
          rows       : list[dict]   (id/email/source_plan/tier_expires_at)
          errors     : list[str]
    """
    # Env-level kill switch wins over caller-passed dry_run=False.
    if _dry_run_env():
        dry_run = True

    out: dict[str, Any] = {
        "dry_run": dry_run,
        "checked": 0,
        "demoted": 0,
        "rows": [],
        "errors": [],
    }

    conn = _db_conn()
    if conn is None:
        out["errors"].append("no_db_conn")
        return out

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(_SELECT_EXPIRED_SQL, ("%_onetime", int(limit)))
                expired = cur.fetchall() or []
                out["checked"] = len(expired)

                if not expired:
                    # Still write the heartbeat row so operators can see
                    # the cron RAN (and matched zero) vs not having run
                    # at all. Cheap.
                    _audit_finding(cur, "expired_demote: 0 matched", 0)
                    return out

                if dry_run:
                    # SELECT-only: surface the would-be-demoted rows.
                    for r in expired:
                        uid, email, plan, src, exp_at, _cust = r
                        out["rows"].append({
                            "id":              uid,
                            "email":           email,
                            "current_plan":    plan,
                            "source_plan":     src,
                            "tier_expires_at": exp_at.isoformat() if exp_at else None,
                        })
                    _audit_finding(
                        cur,
                        f"expired_demote DRY_RUN: {len(expired)} would be demoted",
                        len(expired),
                    )
                    return out

                # LIVE: per-row try/except + savepoint so one bad row
                # never wedges the rest.
                for r in expired:
                    uid, email, plan, src, exp_at, _cust = r
                    sp = f"sp_dem_{abs(hash(str(uid))) % 10_000_000}"
                    try:
                        cur.execute(f"SAVEPOINT {sp}")
                        cur.execute(_UPDATE_DEMOTE_SQL, (uid,))
                        upd = cur.fetchone()
                        if upd:
                            cur.execute(_UPDATE_KEYS_SQL, (uid,))
                            out["demoted"] += 1
                            out["rows"].append({
                                "id":              upd[0],
                                "email":           upd[1],
                                "source_plan":     upd[2],
                                "tier_expires_at": upd[3].isoformat() if upd[3] else None,
                            })
                        cur.execute(f"RELEASE SAVEPOINT {sp}")
                    except Exception as e:
                        try:
                            cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                        except Exception:
                            pass
                        err = f"row {uid}/{email}: {e}"
                        out["errors"].append(err[:240])
                        _log(err)

                _audit_finding(
                    cur,
                    f"expired_demote LIVE: demoted={out['demoted']} "
                    f"checked={out['checked']} errors={len(out['errors'])}",
                    out["demoted"],
                )
    except Exception as e:
        out["errors"].append(f"top-level: {e}")
        _log(f"top-level error: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return out


# ── HTTP endpoints ────────────────────────────────────────────────────


@expired_demote_bp.route(
    "/api/v1/admin/lifecycle/demote-preview", methods=["GET"])
def demote_preview():
    """Admin-gated DRY-RUN: shows what would be demoted right now."""
    if not _admin_or_cron_authorized():
        return jsonify({"error": "admin_key_required"}), 401
    try:
        limit = int(request.args.get("limit", "500"))
    except Exception:
        limit = 500
    result = run_expired_demote(dry_run=True, limit=max(1, min(limit, 5000)))
    return jsonify(result), 200


@expired_demote_bp.route(
    "/api/v1/admin/lifecycle/demote-expired", methods=["POST", "GET"])
def demote_expired():
    """Admin OR cron-gated LIVE demote. Pass ?dry=1 to override to dry-run
    on a single call without touching the env kill switch."""
    if not _admin_or_cron_authorized():
        return jsonify({"error": "admin_key_required"}), 401
    dry = (request.args.get("dry", "") or "").strip().lower() in (
        "1", "true", "yes", "on")
    try:
        limit = int(request.args.get("limit", "500"))
    except Exception:
        limit = 500
    result = run_expired_demote(
        dry_run=dry, limit=max(1, min(limit, 5000)))
    return jsonify(result), 200
