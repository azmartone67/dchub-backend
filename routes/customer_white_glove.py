"""customer_white_glove.py — customer-lifecycle white-glove master shell (2026-07-20).

The human-customer analog of the agent-side white glove (agent_onboarding_master_
shell / model_relations): ONE conductor that MEASURES every paying customer's
lifecycle, CLASSIFIES it into a stage, computes the next-best-action, writes the
stage back to users.engagement_stage, and surfaces a daily operator health digest
plus one-click actions — instead of the scattered, mostly-unscheduled pieces
(activation_nudge, winback_outreach, usage_limit_emails, paid_account_health).

Motivated by marvinvitcu@gmail.com (2026-07-20): a $9 Starter customer who paid
for a month+ with ZERO API calls, invisible to every existing nudge (Starter
wasn't a "paid" plan; the nudge wasn't scheduled). The lesson: no single surface
told anyone he was stranded. This is that surface.

ENGAGEMENT STAGES (written to users.engagement_stage — NOT lifecycle_stage,
which is the CRM funnel's constrained column):
  new         paid, < GRACE_HOURS old — welcome grace, no action yet
  stranded    paid, active sub, 0 calls ever, past grace — ACTIVATION motion
  activating  made first calls, active within ACTIVE_DAYS — healthy, watch
  healthy     regular recent usage — no action
  cooling     was active, no calls in COOLING_DAYS..AT_RISK_DAYS — RE-ENGAGE
  at_risk     no calls AT_RISK_DAYS+, or a failed payment, still paying — SAVE
  power       high volume / near daily cap — EXPANSION / upgrade
  churned     subscription canceled/inactive or demoted — WINBACK

SAFETY (touches real paying customers):
  * MEASURE + CLASSIFY + PERSIST the stage always run (read + a users UPDATE of
    engagement_stage/last_touched_at only — no customer contact).
  * Customer EMAILS are never sent from here. Stage motions are ROUTED to the
    existing gated senders (activation_nudge, winback) and each of THOSE stays
    dry-run until its own arm flag is set. The primary output is the OPERATOR
    digest — a human decides.
  * CUSTOMER_WHITE_GLOVE_ACT=1 lets the tick fan stranded customers into the
    activation nudge automatically (which itself still needs ACTIVATION_NUDGE_ARM).

Endpoints (admin-keyed):
  GET  /api/v1/admin/customer-white-glove/state    full roster + stage + action
  POST /api/v1/admin/customer-white-glove/tick     measure→classify→persist
  POST /api/v1/admin/customer-white-glove/digest   ?send=true emails the operator
"""
from __future__ import annotations

import datetime
import html as _html
import os
import logging

from flask import Blueprint, jsonify, request

logger = logging.getLogger("customer_white_glove")
customer_white_glove_bp = Blueprint("customer_white_glove", __name__)

PAID_PLANS = ("starter", "developer", "pro", "paid", "enterprise", "founding")
GRACE_HOURS = 48
ACTIVE_DAYS = 14
COOLING_DAYS = 14
AT_RISK_DAYS = 30
POWER_CALLS_30D = 2000     # heuristic "power user" floor over 30d

# ── self-healing loop wiring ───────────────────────────────────────────
# The tick runs at /api/v1/admin/customer-white-glove/tick — NOT under
# /api/jobs/*, so jobs_routes' auto-stamp into cron_last_run never covers
# it. We self-stamp here so the platform's EXTERNAL dead-man
# (brain_consistency_radar.check_cron_freshness) watches this loop and
# fires `cron_silently_dead` if the scheduler dies or the endpoint 500s —
# i.e. the loop is watched even when the loop itself is dead.
LOOP_JOB_NAME = "customer_white_glove_tick"
LOOP_INTERVAL_S = 86400          # daily; the dead-man flags at 2× (48h late)
NUDGE_STALE_DAYS = 7             # nudged this long ago + still stranded = escalate
STRANDED_SYSTEMIC_RATIO = 0.40   # > this share stranded = systemic activation failure

# Stage severity (low = worse) — used to classify a transition as a
# recovery (up) vs a regression (down) for the momentum banner.
_STAGE_RANK = {"churned": 0, "at_risk": 1, "stranded": 2, "cooling": 3,
               "new": 4, "activating": 5, "healthy": 6, "power": 7}


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


def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or
                os.environ.get("DCHUB_INTERNAL_KEY") or "").strip()
    provided = (request.headers.get("X-Admin-Key")
                or request.args.get("admin_key")
                or request.headers.get("Authorization", "").replace("Bearer ", "").strip())
    return bool(expected) and provided == expected


# Owner / staff accounts that are real Stripe payers only because the founder
# tests the checkout flow — exclude so the customer board isn't self-noise.
_OWNER_MARKERS = ("azmartone", "nicomartone", "jonathan.martone", "martone")


def _is_internal(email: str) -> bool:
    e = (email or "").lower()
    if (not e) or "@dchub.cloud" in e or e.startswith(("qa-", "test", "smoke")):
        return True
    return any(m in e for m in _OWNER_MARKERS)


def _measure():
    """Real PAYING customers only (Stripe customer + a paid invoice — excludes
    the BD-outreach seeds and comped rows that share the 'developer'/'founding'
    plan labels), with TRUE cross-surface usage: web (users.api_calls_total) +
    MCP (mcp_call_log via the customer's mcp_dev_keys key). The MCP count is the
    signal that matters — the product IS the MCP server — and reading only the
    web api_keys store was mis-flagging stranded customers as healthy. Fail-soft."""
    c = _conn()
    if c is None:
        return []
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT u.email, u.name, u.plan, u.created_at, u.last_login,
                       u.subscription_status, u.payment_failed_count, u.demoted_at,
                       COALESCE(u.api_calls_total, 0) AS web_calls,
                       COALESCE((SELECT COUNT(*) FROM mcp_call_log ml
                                 JOIN mcp_dev_keys dk ON dk.api_key = ml.api_key
                                 WHERE lower(dk.email) = lower(u.email)), 0) AS mcp_calls,
                       (SELECT MAX(ml.timestamp) FROM mcp_call_log ml
                        JOIN mcp_dev_keys dk ON dk.api_key = ml.api_key
                        WHERE lower(dk.email) = lower(u.email)) AS last_mcp,
                       EXISTS(SELECT 1 FROM email_drip_log d
                              WHERE lower(d.user_email)=lower(u.email)
                                AND d.email_key='activation_nudge') AS nudged,
                       (SELECT MAX(d.sent_at) FROM email_drip_log d
                        WHERE lower(d.user_email)=lower(u.email)
                          AND d.email_key='activation_nudge') AS nudged_at,
                       -- r-truth (2026-08-19): this EXISTS had NO status
                       -- filter, so a customer whose every welcome attempt
                       -- logged 'skipped_duplicate' read as welcomed:true.
                       -- welcome_email_log records ATTEMPTS; only 'sent%' is a
                       -- delivery. Splitting the two makes "we tried and
                       -- nothing left the building" a visible state instead of
                       -- a green tick. Measured on rob@hedmarkholdings.com
                       -- (2026-08-19): 4 rows, 2 of them skipped_duplicate.
                       EXISTS(SELECT 1 FROM welcome_email_log w
                              WHERE lower(w.email)=lower(u.email)
                                AND COALESCE(w.plan,'') NOT LIKE 'receipt%%'
                                AND COALESCE(w.status,'') LIKE 'sent%%') AS welcomed,
                       EXISTS(SELECT 1 FROM welcome_email_log w
                              WHERE lower(w.email)=lower(u.email)
                                AND COALESCE(w.plan,'') NOT LIKE 'receipt%%') AS welcome_attempted,
                       -- The ONLY record of a HUMAN reaching this customer.
                       -- Onboarding Master Shell #43 lane 5 has reported "there
                       -- is no human white-glove track" as a standing FAIL;
                       -- surfacing it PER CUSTOMER is what turns that sentence
                       -- into a worklist.
                       (SELECT MAX(fc.contacted_at) FROM founding_customers fc
                        WHERE lower(fc.email)=lower(u.email)) AS human_contacted_at
                FROM users u
                WHERE u.plan IN %s AND u.email IS NOT NULL AND u.email <> ''
                  AND u.stripe_customer_id IS NOT NULL AND u.stripe_customer_id <> ''
                  AND COALESCE(u.invoices_paid_count, 0) > 0
            """, (PAID_PLANS,))
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                d["total_calls"] = int(d.get("web_calls") or 0) + int(d.get("mcp_calls") or 0)
                # recency = most recent of MCP activity or web login
                d["last_used_at"] = d.get("last_mcp") or d.get("last_login")
                rows.append(d)
    except Exception as e:
        logger.warning("[cwg] measure failed: %s", str(e)[:160])
        rows = []
    finally:
        try: c.close()
        except Exception: pass
    return [r for r in rows if not _is_internal(r.get("email"))]


def _age_days(ts, now):
    # users.created_at is TEXT (ISO strings, inconsistent) while api_keys.
    # last_used_at is a real timestamptz — handle both, never raise.
    if ts is None:
        return None
    try:
        if isinstance(ts, str):
            ts = datetime.datetime.fromisoformat(ts.replace("Z", "").strip())
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return (now - ts).total_seconds() / 86400.0
    except Exception:
        return None


def _classify(r, now):
    """→ (stage, action, priority 0-3). Deterministic."""
    sub = (r.get("subscription_status") or "").lower()
    demoted = r.get("demoted_at") is not None
    if demoted or sub in ("canceled", "cancelled", "unpaid", "incomplete_expired"):
        return "churned", "Winback: they canceled/lapsed — send the come-back offer.", 2
    joined_age = _age_days(r.get("created_at"), now)
    calls = int(r.get("total_calls") or 0)
    last_used_age = _age_days(r.get("last_used_at"), now)
    failed = int(r.get("payment_failed_count") or 0)

    if failed > 0:
        return "at_risk", f"Save: {failed} failed payment(s) — dunning / card-update outreach.", 3
    # r-truth (2026-08-19): attempted-but-never-DELIVERED outranks the grace
    # period. A customer whose every welcome logged 'skipped_duplicate' has
    # paid and been told nothing; the old `welcomed` flag rendered that state
    # as a green tick, so it could sit in "grace" forever.
    if r.get("welcome_attempted") and not r.get("welcomed"):
        return ("stranded",
                "ESCALATE: welcome was ATTEMPTED but never delivered (all "
                "attempts logged skipped/failed) — they paid and heard nothing. "
                "Resend manually: POST /api/v1/admin/resend-welcome.", 3)
    if joined_age is not None and joined_age < (GRACE_HOURS / 24.0):
        return "new", "Grace period — welcome delivered; watch for first call.", 0
    if calls == 0:
        # Close the action loop: if we already fired the automated nudge and
        # they're STILL at zero calls, the automated motion FAILED — escalate
        # to a human touch instead of re-firing the same email into the void.
        nudge_age = _age_days(r.get("nudged_at"), now)
        if r.get("nudged") and (nudge_age is None or nudge_age >= NUDGE_STALE_DAYS):
            since = f" {int(nudge_age)}d ago" if nudge_age is not None else ""
            return ("stranded",
                    f"ESCALATE: nudged{since}, still zero calls — automated nudge "
                    f"FAILED. Human touch (call / personal note), not another email.",
                    3)
        if r.get("nudged"):
            return ("stranded",
                    "Nudged recently — watch for first call before escalating.", 3)
        return "stranded", "Activation nudge — paid, zero calls past grace.", 3
    if last_used_age is None:
        return "activating", "Made calls but recency unknown — watch.", 1
    if last_used_age <= ACTIVE_DAYS:
        # power user?
        if calls >= POWER_CALLS_30D:
            return "power", "Expansion: high volume — surface the next tier / bulk export.", 1
        if joined_age is not None and joined_age <= ACTIVE_DAYS:
            return "activating", "Activated recently — nurture toward habit.", 1
        return "healthy", "Healthy recent usage — no action.", 0
    if last_used_age <= AT_RISK_DAYS:
        return "cooling", f"Re-engage: no calls in {int(last_used_age)}d — 'what changed?' check-in.", 2
    return "at_risk", f"Save: idle {int(last_used_age)}d but still paying — churn risk.", 3


def _roster(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    out = []
    for r in _measure():
        stage, action, prio = _classify(r, now)
        nudge_days = _age_days(r.get("nudged_at"), now)
        out.append({
            "email": r["email"], "name": r.get("name") or "", "plan": r.get("plan"),
            "stage": stage, "action": action, "priority": prio,
            "total_calls": int(r.get("total_calls") or 0),
            "mcp_calls": int(r.get("mcp_calls") or 0),
            "web_calls": int(r.get("web_calls") or 0),
            "joined_days": round(_age_days(r.get("created_at"), now) or 0, 1),
            "idle_days": (round(_age_days(r.get("last_used_at"), now), 1)
                          if r.get("last_used_at") else None),
            "welcomed": bool(r.get("welcomed")), "nudged": bool(r.get("nudged")),
            # welcome_attempted && !welcomed = every send logged skipped/failed
            "welcome_attempted": bool(r.get("welcome_attempted")),
            "nudge_days": (round(nudge_days, 1) if nudge_days is not None else None),
            # escalate = the automated motion already ran and did NOT work
            "escalate": bool(action.startswith("ESCALATE")),
            "subscription_status": r.get("subscription_status"),
            # r-truth (2026-08-19): human contact, per customer. `needs_human`
            # is the worklist Onboarding Shell #43 lane 5 has been describing
            # in prose ("4 payer(s) in 30d received no human contact") without
            # ever naming anyone. A payer past the grace window whom no human
            # has contacted IS the white-glove gap, one row at a time.
            "human_contacted_at": (r["human_contacted_at"].isoformat()
                                   if r.get("human_contacted_at") is not None
                                   and hasattr(r.get("human_contacted_at"), "isoformat")
                                   else r.get("human_contacted_at")),
            "needs_human": bool(
                r.get("human_contacted_at") is None
                and (_age_days(r.get("created_at"), now) or 0) >= (GRACE_HOURS / 24.0)
            ),
        })
    order = {"stranded": 0, "at_risk": 1, "cooling": 2, "churned": 3,
             "power": 4, "activating": 5, "new": 6, "healthy": 7}
    out.sort(key=lambda x: (order.get(x["stage"], 9), -x["priority"], -x["total_calls"]))
    return out


def _ensure_columns():
    """users.lifecycle_stage is OWNED by the CRM funnel subsystem — it carries
    a CHECK constraint (new/engaged/nurturing/opportunity/converted/churned/
    inactive) and writing our engagement vocabulary there both fails the
    constraint AND would corrupt the funnel. We keep our engagement stage in a
    dedicated, unconstrained column. Idempotent, fail-soft."""
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                        "engagement_stage TEXT")
    except Exception as e:
        logger.warning("[cwg] ensure columns failed: %s", str(e)[:120])
    finally:
        try: c.close()
        except Exception: pass


def _persist_stages(roster):
    """Write the computed engagement stage to users.engagement_stage (+ the
    last_touched_at heartbeat). NOT users.lifecycle_stage — that column is the
    CRM funnel's, with an incompatible CHECK constraint. Fail-soft."""
    _ensure_columns()
    c = _conn()
    if c is None:
        return 0
    n = 0
    try:
        with c.cursor() as cur:
            for r in roster:
                try:
                    # last_touched_at is the loop's freshness HEARTBEAT — bump
                    # it every tick (not only on stage change), otherwise a
                    # customer parked in one stage makes the freshness signal
                    # look stale while the loop is actually healthy.
                    cur.execute("""
                        UPDATE users SET engagement_stage=%s, last_touched_at=NOW(),
                               last_touch_by='customer_white_glove'
                        WHERE lower(email)=lower(%s)
                    """, (r["stage"], r["email"]))
                    n += cur.rowcount
                except Exception:
                    pass
    finally:
        try: c.close()
        except Exception: pass
    return n


def _stamp_cron_run():
    """Self-register into cron_last_run so the platform's EXTERNAL dead-man
    (brain_consistency_radar.check_cron_freshness) watches this loop. The
    tick lives at /api/v1/admin/... not /api/jobs/*, so jobs_routes' auto-
    stamp never covers it — without this stamp nothing notices when the loop
    silently stops. cron_last_run.job_name is the PK, hence ON CONFLICT."""
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO cron_last_run
                    (job_name, last_started_at, expected_interval_s, run_count)
                VALUES (%s, NOW(), %s, 1)
                ON CONFLICT (job_name) DO UPDATE SET
                    last_started_at = EXCLUDED.last_started_at,
                    expected_interval_s = COALESCE(EXCLUDED.expected_interval_s,
                                                   cron_last_run.expected_interval_s),
                    run_count = cron_last_run.run_count + 1
            """, (LOOP_JOB_NAME, LOOP_INTERVAL_S))
    except Exception as e:
        logger.warning("[cwg] cron stamp failed: %s", str(e)[:120])
    finally:
        try: c.close()
        except Exception: pass


def _ensure_events_table():
    c = _conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS customer_lifecycle_events (
                    id BIGSERIAL PRIMARY KEY,
                    email       TEXT NOT NULL,
                    from_stage  TEXT,
                    to_stage    TEXT NOT NULL,
                    total_calls INTEGER DEFAULT 0,
                    note        TEXT,
                    at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cle_at "
                        "ON customer_lifecycle_events(at)")
    except Exception as e:
        logger.warning("[cwg] events table ensure failed: %s", str(e)[:120])
    finally:
        try: c.close()
        except Exception: pass


def _prior_stages(emails):
    """Currently-persisted engagement_stage per email — read BEFORE the tick
    overwrites it, so a diff vs the freshly-computed stage is a real move."""
    if not emails:
        return {}
    c = _conn()
    if c is None:
        return {}
    out = {}
    try:
        with c.cursor() as cur:
            # Tolerate the very first run before the column exists.
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='users' AND column_name='engagement_stage'")
            if not cur.fetchone():
                return {}
            cur.execute("SELECT lower(email), engagement_stage FROM users "
                        "WHERE lower(email) = ANY(%s)",
                        ([e.lower() for e in emails],))
            for em, st in cur.fetchall():
                out[em] = st
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass
    return out


def _classify_move(old, new):
    """Category for the momentum banner. None old = first observation."""
    if old is None or old == new:
        return None
    up = {"activating", "healthy", "power"}
    if new in up and old in ("stranded", "new"):
        return "activated"
    if new in up and old in ("cooling", "at_risk"):
        return "recovered"
    if new == "stranded" and old != "stranded":
        return "newly_stranded"
    if new == "churned":
        return "churned"
    if _STAGE_RANK.get(new, 9) < _STAGE_RANK.get(old, 9):
        return "regressed"
    return "improved"


def _track_transitions(roster):
    """CLOSE THE ACTION LOOP. Compare each customer's freshly-computed stage
    to the previously-persisted one and log real transitions to
    customer_lifecycle_events. This is how the loop VERIFIES its own actions:
    a stranded→activating move proves a nudge worked; a stranded customer who
    was nudged and stayed stranded is a failure the escalation path catches.
    Call ONCE per day, from the tick, BEFORE _persist_stages overwrites the
    prior value. Writes history only — never contacts anyone. Fail-soft."""
    _ensure_events_table()
    emails = [r["email"] for r in roster]
    prior = _prior_stages(emails)
    logged = 0
    c = _conn()
    if c is None:
        return {"logged": 0}
    try:
        with c.cursor() as cur:
            for r in roster:
                em = r["email"].lower()
                new = r["stage"]
                old = prior.get(em)
                if old == new:
                    continue
                note = ("first_observation" if old is None
                        else (r.get("action") or "")[:200])
                try:
                    cur.execute(
                        "INSERT INTO customer_lifecycle_events"
                        "(email, from_stage, to_stage, total_calls, note) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (r["email"], old, new, r["total_calls"], note))
                    logged += 1
                except Exception:
                    pass
    except Exception as e:
        logger.warning("[cwg] track transitions failed: %s", str(e)[:120])
    finally:
        try: c.close()
        except Exception: pass
    return {"logged": logged}


def _recent_momentum(hours=20):
    """Read the transitions the tick recorded (last ~day) → the digest's
    momentum banner. Decoupled from _track_transitions so any surface can
    show momentum without re-running (or double-logging) the diff."""
    out = {"activated": [], "recovered": [], "newly_stranded": [],
           "churned": [], "regressed": [], "improved": [],
           "first_seen": 0, "transitions": 0}
    c = _conn()
    if c is None:
        return out
    try:
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.customer_lifecycle_events')")
            if not cur.fetchone()[0]:
                return out
            cur.execute(
                "SELECT email, from_stage, to_stage FROM customer_lifecycle_events "
                "WHERE at > NOW() - (%s || ' hours')::interval ORDER BY at",
                (str(int(hours)),))
            for em, old, new in cur.fetchall():
                if old is None:
                    out["first_seen"] += 1
                    continue
                cat = _classify_move(old, new)
                if cat:
                    out["transitions"] += 1
                    out.setdefault(cat, []).append(em)
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass
    return out


def _loop_freshness():
    """The loop's own heartbeat age (from cron_last_run). loop_stale=True is
    the never-stale alarm — the same signal check_cron_freshness fires on."""
    c = _conn()
    out = {"last_run_at": None, "age_hours": None, "loop_stale": None,
           "run_count": None}
    if c is None:
        return out
    try:
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.cron_last_run')")
            if not cur.fetchone()[0]:
                return out
            cur.execute(
                "SELECT last_started_at, run_count, "
                "EXTRACT(EPOCH FROM (NOW()-last_started_at)) "
                "FROM cron_last_run WHERE job_name=%s", (LOOP_JOB_NAME,))
            row = cur.fetchone()
            if row and row[0] is not None:
                out["last_run_at"] = row[0].isoformat()
                out["run_count"] = row[1]
                age_h = float(row[2]) / 3600.0
                out["age_hours"] = round(age_h, 1)
                out["loop_stale"] = age_h > (LOOP_INTERVAL_S * 2 / 3600.0)
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass
    return out


def _self_health(roster):
    """The loop reporting on itself: systemic activation signal + heartbeat.
    systemic_activation_failure mirrors the radar detector's threshold so the
    operator sees the same course-correction signal the brain agenda gets."""
    total = len(roster)
    stranded = sum(1 for r in roster if r["stage"] == "stranded")
    escalate = sum(1 for r in roster if r.get("escalate"))
    engaged = sum(1 for r in roster if r["total_calls"] > 0)
    ratio = round(stranded / total, 3) if total else 0.0
    fresh = _loop_freshness()
    # r-truth (2026-08-19): two counts the operator could not previously get
    # from any surface.
    #   needs_human        — payers past grace no human has ever contacted.
    #                        Onboarding Shell #43 lane 5 reports this as a
    #                        sentence; this is the number, and the roster names
    #                        the people.
    #   welcome_undelivered — paid, welcome ATTEMPTED, nothing ever sent. Was
    #                        invisible because `welcomed` did not filter on
    #                        status (skipped_duplicate counted as welcomed).
    needs_human = sum(1 for r in roster if r.get("needs_human"))
    undelivered = sum(1 for r in roster
                      if r.get("welcome_attempted") and not r.get("welcomed"))
    return {
        "loop": "customer_white_glove",
        "payers": total, "engaged": engaged,
        "stranded": stranded, "escalate": escalate,
        "needs_human": needs_human,
        "welcome_undelivered": undelivered,
        "stranded_ratio": ratio,
        "systemic_activation_failure": total >= 5 and ratio >= STRANDED_SYSTEMIC_RATIO,
        # A green board with nobody attended is the failure mode this loop was
        # built to stop being blind to — say it out loud rather than implying
        # it from a count of zero.
        "human_touch_gap": needs_human > 0,
        **fresh,
    }


def stranded_candidates(limit=200):
    """Authoritative 'who needs the activation nudge': real payers the loop
    classified 'stranded' (cross-surface zero calls, past grace, Stripe-verified,
    owner/seed excluded) who have NOT already been nudged.

    activation_nudge's own legacy query diverged three ways and must not be
    trusted to pick recipients: it measured the WEB api_keys store only (so it
    would nudge an MCP-active customer — eren, 1085 calls — telling him he never
    made a first query), it skipped the Stripe/invoice filter (so it could reach
    BD-outreach seeds like partnerships@… / press@…), and it capped at a 45-day
    window (so it MISSED real stranded payers older than that). Sourcing the
    recipient list from here keeps the send in lockstep with the health board."""
    out = []
    for r in _roster():
        if r.get("stage") == "stranded" and not r.get("nudged"):
            out.append({"email": r["email"], "name": r.get("name") or "",
                        "plan": r.get("plan"), "created_at": None})
            if len(out) >= limit:
                break
    return out


def _counts(roster):
    out = {}
    for r in roster:
        out[r["stage"]] = out.get(r["stage"], 0) + 1
    return out


@customer_white_glove_bp.route("/api/v1/admin/customer-white-glove/state")
def cwg_state():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    roster = _roster()
    return jsonify(ok=True, total=len(roster), counts=_counts(roster),
                   health=_self_health(roster), momentum=_recent_momentum(),
                   customers=roster), 200


@customer_white_glove_bp.route("/api/v1/admin/customer-white-glove/tick",
                               methods=["POST"])
def cwg_tick():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    # Heartbeat first so the external dead-man sees THIS run even if the work
    # below throws — a stamp on a crashing tick is better than no stamp.
    _stamp_cron_run()
    roster = _roster()
    # Track transitions BEFORE persisting: _track reads the prior persisted
    # stage; _persist then overwrites it with today's.
    tracked = _track_transitions(roster)
    persisted = _persist_stages(roster)
    counts = _counts(roster)
    acted = {"stranded_routed_to_activation": 0}
    # Optional: fan stranded → activation nudge (which itself is arm-gated).
    if os.environ.get("CUSTOMER_WHITE_GLOVE_ACT") == "1":
        try:
            from activation_nudge import run_activation_nudge
            res = run_activation_nudge()   # honors ACTIVATION_NUDGE_ARM internally
            acted["stranded_routed_to_activation"] = res.get("sent", 0)
            acted["activation_armed"] = res.get("armed")
        except Exception as e:
            acted["activation_error"] = str(e)[:120]
    return jsonify(ok=True, total=len(roster), counts=counts,
                   stages_persisted=persisted, transitions_logged=tracked.get("logged", 0),
                   health=_self_health(roster), momentum=_recent_momentum(), acted=acted,
                   note=("Stages written to users.engagement_stage; transitions logged to "
                         "customer_lifecycle_events; heartbeat stamped to cron_last_run "
                         "(watched by brain check_cron_freshness). Customer emails are NOT "
                         "sent from here — motions route to the gated senders.")), 200


def _momentum_banner(mom, health):
    """A one-strip 'what moved since yesterday + is the loop itself healthy'
    header — the closed-loop feedback the operator reads first."""
    def esc(s): return _html.escape(str(s or ""))
    chips = []
    for key, label, color in (
        ("activated", "✅ activated", "#16a34a"),
        ("recovered", "↩︎ recovered", "#16a34a"),
        ("newly_stranded", "🔴 newly stranded", "#dc2626"),
        ("regressed", "↓ regressed", "#d97706"),
        ("churned", "⚫ churned", "#475569")):
        n = len(mom.get(key) or [])
        if n:
            chips.append(f'<span style="display:inline-block;margin:0 8px 4px 0;'
                         f'padding:2px 9px;border-radius:12px;background:{color};'
                         f'color:#fff;font-size:12px">{label} {n}</span>')
    if mom.get("first_seen"):
        chips.append(f'<span style="display:inline-block;margin:0 8px 4px 0;'
                     f'padding:2px 9px;border-radius:12px;background:#64748b;'
                     f'color:#fff;font-size:12px">👋 first seen {mom["first_seen"]}</span>')
    movement = "".join(chips) or '<span style="color:#64748b;font-size:13px">No stage changes in the last day.</span>'
    # loop self-health line
    if health.get("loop_stale"):
        hb = (f'<span style="color:#dc2626;font-weight:600">⚠ LOOP STALE — last ran '
              f'{health.get("age_hours","?")}h ago (expected daily). The brain will '
              f'also flag this as cron_silently_dead.</span>')
    elif health.get("age_hours") is not None:
        hb = (f'<span style="color:#16a34a">● loop healthy — last ran '
              f'{health.get("age_hours")}h ago, {health.get("run_count","?")} runs.</span>')
    else:
        hb = '<span style="color:#64748b">loop heartbeat not yet recorded.</span>'
    sysfail = ""
    if health.get("systemic_activation_failure"):
        sysfail = (f'<div style="margin-top:8px;padding:8px 10px;background:#fef2f2;'
                   f'border-left:3px solid #dc2626;font-size:13px;color:#991b1b">'
                   f'Systemic activation failure: {health.get("stranded")}/{health.get("payers")} '
                   f'paying customers stranded ({int(health.get("stranded_ratio",0)*100)}%). '
                   f'This same signal is on the brain agenda (course-correction).</div>')
    return (f'<div style="background:#fff;border:1px solid #e2e8f0;border-radius:6px;'
            f'padding:12px 14px;margin:14px 0">'
            f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:.05em;'
            f'color:#64748b;margin-bottom:6px">Movement since yesterday</div>{movement}'
            f'<div style="margin-top:8px;font-size:12px">{hb}</div>{sysfail}</div>')


def _digest_html(roster, mom=None, health=None):
    counts = _counts(roster)
    mom = mom if mom is not None else _recent_momentum()
    health = health if health is not None else _self_health(roster)
    def esc(s): return _html.escape(str(s or ""))
    order = ["stranded", "at_risk", "cooling", "churned", "power", "activating"]
    labels = {"stranded": "🔴 Stranded — paid, never activated",
              "at_risk": "🟠 At risk — idle/failed payment, still paying",
              "cooling": "🟡 Cooling — was active, going quiet",
              "churned": "⚫ Churned — canceled/lapsed",
              "power": "🟢 Power users — expansion candidates",
              "activating": "🔵 Activating — nurture toward habit"}
    blocks = []
    for stage in order:
        rows = [r for r in roster if r["stage"] == stage]
        if not rows:
            continue
        trs = "".join(
            f'<tr><td style="padding:5px 10px;border-bottom:1px solid #eee">'
            f'{"<span style=\"background:#dc2626;color:#fff;font-size:10px;padding:1px 5px;border-radius:3px;margin-right:5px\">ESCALATE</span>" if r.get("escalate") else ""}'
            f'<b>{esc(r["email"])}</b> <span style="color:#888;font-size:12px">'
            f'({esc(r["plan"])} · {r["total_calls"]} calls · joined {r["joined_days"]:.0f}d'
            f'{" · idle "+str(int(r["idle_days"]))+"d" if r.get("idle_days") is not None else ""}'
            f'{" · nudged "+str(int(r["nudge_days"]))+"d ago" if r.get("nudge_days") is not None else ""})</span>'
            f'<br><span style="color:#555;font-size:13px">{esc(r["action"])}</span></td></tr>'
            for r in rows)
        blocks.append(
            f'<h3 style="margin:20px 0 6px;font-size:15px">{labels[stage]} ({len(rows)})</h3>'
            f'<table cellpadding=0 cellspacing=0 width="100%" style="border-collapse:collapse;'
            f'background:#fff;border:1px solid #e2e8f0;border-radius:6px">{trs}</table>')
    healthy = counts.get("healthy", 0) + counts.get("new", 0)
    return f"""<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f1f5f9;padding:24px">
<div style="max-width:660px;margin:0 auto">
<div style="background:#0f172a;color:#fff;padding:16px 22px;border-radius:8px 8px 0 0">
<strong>DC Hub — customer white-glove health</strong>
<div style="color:#94a3b8;font-size:13px">{len(roster)} paying customers · {healthy} healthy/new ·
{counts.get('stranded',0)} stranded · {counts.get('at_risk',0)} at-risk · {counts.get('cooling',0)} cooling</div>
</div>
<div style="background:#f8fafc;padding:8px 22px 22px;border:1px solid #e2e8f0;border-top:0;border-radius:0 0 8px 8px">
{_momentum_banner(mom, health)}
{''.join(blocks) or '<p style="color:#16a34a">All paying customers healthy — nobody needs a touch today.</p>'}
<p style="color:#64748b;font-size:12px;margin-top:18px">Stages written to users.engagement_stage. Arm the
activation sends with ACTIVATION_NUDGE_ARM=1; nothing emails a customer from this digest.</p>
</div></div></div>"""


@customer_white_glove_bp.route("/api/v1/admin/customer-white-glove/digest",
                               methods=["POST", "GET"])
def cwg_digest():
    if not _admin_ok():
        return jsonify(ok=False, error="unauthorized"), 401
    send = request.args.get("send", "").lower() in ("1", "true", "yes")
    roster = _roster()
    _persist_stages(roster)
    recipients = [e.strip() for e in
                  (os.environ.get("DCHUB_BRIEFING_EMAIL")
                   or os.environ.get("BRAIN_DIGEST_EMAIL")
                   or "jonathan@dchub.cloud").split(",") if e.strip()]
    html_body = _digest_html(roster)
    if not send:
        return jsonify(ok=True, dry_run=True, total=len(roster),
                       counts=_counts(roster), recipients=recipients,
                       html_bytes=len(html_body)), 200
    sent = failed = 0
    try:
        # Route the OPERATOR digest through the repo's sanctioned transactional
        # sender (main._resend_email) instead of a hand-rolled Resend POST. This
        # is the same operator-mail choke-point auth_routes / brain_automerge
        # (_alert_operator) use. It keeps this file off the marketing-bypass
        # ratchet (tests/test_marketing_chokepoint.py::test_no_new_resend_bypass)
        # while changing ONLY the transport: recipients, the send gate, the
        # subject and the body are untouched, so whether/when this digest sends
        # is unchanged. Lazy import — main imports routes, so a top-level import
        # would be circular. _resend_email reads DCHUB_RESEND_API_KEY or
        # RESEND_API_KEY and returns True on a 2xx.
        from main import _resend_email
        subj = (f"DC Hub customer health: {_counts(roster).get('stranded',0)} stranded, "
                f"{_counts(roster).get('at_risk',0)} at-risk")
        # Preserve the existing sender: DCHUB_FROM_EMAIL is a combined
        # "Name <email>" string here (default "DC Hub <jonathan@dchub.cloud>");
        # split it for _resend_email's (from_email, from_name) signature.
        from_hdr = os.environ.get("DCHUB_FROM_EMAIL", "DC Hub <jonathan@dchub.cloud>")
        if "<" in from_hdr and ">" in from_hdr:
            from_name = from_hdr.split("<", 1)[0].strip() or "DC Hub"
            from_email = from_hdr.split("<", 1)[1].split(">", 1)[0].strip()
        else:
            from_name, from_email = "DC Hub", from_hdr.strip()
        for em in recipients:
            try:
                ok_send = _resend_email(em, subj, html_body,
                                        from_email=from_email, from_name=from_name)
                sent += 1 if ok_send else 0
                failed += 0 if ok_send else 1
            except Exception:
                failed += 1
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:160]), 500
    return jsonify(ok=True, sent=sent, failed=failed, total=len(roster)), 200
