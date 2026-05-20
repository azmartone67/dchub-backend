"""Phase FF+25-followup-r19 (2026-05-20) — founding customers.
==========================================================================

Kevin Serfass (kevin.d.serfass@gmail.com) is the first new paid
customer to come in via the website front-door (not the MCP funnel).
$9 → $49 within 60 seconds at 2026-05-20 20:03 UTC. Pure top-funnel
conversion driven by Switzerland positioning + the brand polish.

The first dozen paid customers matter disproportionately:
  · They're proof the value-prop lands
  · They become reference customers (with permission)
  · They tell us which use cases the product actually solves
  · They tolerate the rough edges that prevent customer #50 from
    converting

This module gives us a queryable founding-customer cohort + a brain
signal so the Inspector celebrates / tracks these specifically.

ENDPOINTS:
  POST /api/v1/admin/founding-customers/tag      add an email to the
                                                   founding cohort
  POST /api/v1/admin/founding-customers/untag    remove
  GET  /api/v1/admin/founding-customers           list (admin)
  GET  /api/v1/founding-customers/count           public count

Used by:
  · brain_inspector — adds founding_customers count to signal block
  · /status dashboard — surfaces the count as a positive metric
  · Inspector system prompt rule: when founding_customers > 0,
    name them as a positive Healthy item
"""
import os
import logging
import datetime
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
founding_customers_bp = Blueprint("founding_customers", __name__)


_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _ensure_table():
    c = _get_db()
    if c is None: return
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS founding_customers (
                    email           TEXT PRIMARY KEY,
                    tagged_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    plan_at_tag     TEXT,
                    first_payment_at TIMESTAMPTZ,
                    stripe_customer_id TEXT,
                    notes           TEXT,
                    contact_status  TEXT DEFAULT 'new',
                    contacted_at    TIMESTAMPTZ,
                    consented_to_cite BOOLEAN DEFAULT FALSE
                )
            """)
        try: c.commit()
        except Exception: pass
    except Exception as e:
        logger.warning(f"[founding-customers] table create failed: {e}")
    finally:
        try: c.close()
        except Exception: pass


@founding_customers_bp.route("/api/v1/admin/founding-customers/tag",
                              methods=["POST"])
def tag_founding():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    _ensure_table()
    p = request.get_json(silent=True) or {}
    email = (p.get("email") or "").lower().strip()
    if not email:
        return jsonify(ok=False, error="email_required"), 400
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO founding_customers
                  (email, plan_at_tag, first_payment_at,
                   stripe_customer_id, notes)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET
                  notes = COALESCE(founding_customers.notes, '')
                          || E'\\n' || COALESCE(EXCLUDED.notes, '')
            """, (
                email, p.get("plan"),
                p.get("first_payment_at"),
                p.get("stripe_customer_id"),
                p.get("notes"),
            ))
        try: c.commit()
        except Exception: pass
        return jsonify(ok=True, email=email,
                       tagged_at=datetime.datetime.utcnow().isoformat() + "Z")
    finally:
        try: c.close()
        except Exception: pass


@founding_customers_bp.route("/api/v1/admin/founding-customers",
                              methods=["GET"])
def list_founding():
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    _ensure_table()
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT email, tagged_at, plan_at_tag, first_payment_at,
                       stripe_customer_id, contact_status, contacted_at,
                       consented_to_cite, notes
                  FROM founding_customers
                 ORDER BY tagged_at DESC
            """)
            rows = []
            for r in cur.fetchall():
                rows.append({
                    "email": r[0],
                    "tagged_at": str(r[1]) if r[1] else None,
                    "plan_at_tag": r[2],
                    "first_payment_at": str(r[3]) if r[3] else None,
                    "stripe_customer_id": r[4],
                    "contact_status": r[5],
                    "contacted_at": str(r[6]) if r[6] else None,
                    "consented_to_cite": r[7],
                    "notes": r[8],
                })
        return jsonify(ok=True, count=len(rows), founding=rows)
    finally:
        try: c.close()
        except Exception: pass


@founding_customers_bp.route("/api/v1/founding-customers/count",
                              methods=["GET"])
def public_count():
    """Public — just the count, no PII. Brain Inspector reads this and
    the Inspector brief celebrates each milestone (1, 5, 10, 25, 50)."""
    _ensure_table()
    c = _get_db()
    if c is None:
        return jsonify(count=0), 200
    try:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM founding_customers")
            n = int((cur.fetchone() or [0])[0] or 0)
        return jsonify(count=n,
                       milestone=("first" if n == 1
                                   else ("5+" if n >= 5
                                         else f"{n} of 5 to milestone")),
                       generated_at=datetime.datetime.utcnow().isoformat() + "Z")
    finally:
        try: c.close()
        except Exception: pass


# ── Auto-tag hook ────────────────────────────────────────────────────
# Called from the Stripe webhook on checkout.session.completed +
# customer.subscription.created so every paid signup auto-tags into the
# founding cohort UNTIL the cap is hit. After cap, ordinary paid
# customers just become regular customers and no founding row is added.
#
# Cap is FOUNDING_CUSTOMERS_CAP env var (default 25). After hitting the
# cap the auto-tag stops permanently — those 25 become the canonical
# "founding 25" cohort for marketing / reference use.
FOUNDING_CAP = int(os.environ.get("FOUNDING_CUSTOMERS_CAP", "25"))


def auto_tag_if_under_cap(
    email: str,
    plan: str = "developer",
    stripe_customer_id: str | None = None,
    first_payment_at: str | None = None,
    notes: str | None = None,
) -> dict:
    """Idempotently tag a paid customer into founding_customers if the
    cohort is below FOUNDING_CAP. Safe to call from the Stripe webhook —
    no exception bubbles up if the table is missing or the connection
    fails.

    Returns {tagged: bool, position: int|None, cap: int}.
    """
    out: dict = {"tagged": False, "position": None,
                 "cap": FOUNDING_CAP, "reason": ""}
    if not email or "@" not in email:
        out["reason"] = "invalid_email"
        return out
    email = email.lower().strip()
    try:
        _ensure_table()
        c = _get_db()
        if c is None:
            out["reason"] = "no_db"
            return out
        try:
            with c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM founding_customers")
                cohort_size = int((cur.fetchone() or [0])[0] or 0)
                if cohort_size >= FOUNDING_CAP:
                    out["reason"] = f"cap_reached ({cohort_size}/{FOUNDING_CAP})"
                    return out
                # Already tagged?
                cur.execute(
                    "SELECT 1 FROM founding_customers WHERE email = %s",
                    (email,),
                )
                if cur.fetchone():
                    out["reason"] = "already_tagged"
                    return out
                cur.execute("""
                    INSERT INTO founding_customers
                      (email, plan_at_tag, first_payment_at,
                       stripe_customer_id, notes, contact_status)
                    VALUES (%s, %s, %s, %s, %s, 'auto-tagged')
                """, (email, plan, first_payment_at,
                       stripe_customer_id, notes))
            try: c.commit()
            except Exception: pass
            out["tagged"] = True
            out["position"] = cohort_size + 1
            return out
        finally:
            try: c.close()
            except Exception: pass
    except Exception as e:
        out["reason"] = f"exception: {str(e)[:160]}"
        return out


def notify_admin_of_founding(email: str, position: int, plan: str,
                              stripe_customer_id: str | None) -> None:
    """Send Jonathan an admin alert email so he knows immediately when
    a new founding customer lands. Best-effort — never raises."""
    try:
        from main import send_admin_alert_email
    except Exception:
        return
    cap = FOUNDING_CAP
    subj = f"Founding customer #{position} of {cap} — {email}"
    body = (
        f"<h2>Founding customer #{position} of {cap} just signed up</h2>"
        f"<p><b>Email:</b> {email}</p>"
        f"<p><b>Plan:</b> {plan}</p>"
        f"<p><b>Stripe:</b> {stripe_customer_id or '(none)'}</p>"
        f"<p>The first {cap} paying customers matter disproportionately. "
        f"Reach out personally within the next hour — even a 60-second "
        f"welcome note converts a buyer into a reference customer.</p>"
        f"<p>"
        f"<a href='https://dchub.cloud/api/v1/admin/customer-lookup?"
        f"email={email}'>Customer record</a> · "
        f"<a href='https://dashboard.stripe.com/customers/"
        f"{stripe_customer_id or ''}'>Stripe</a> · "
        f"<a href='https://dchub.cloud/api/v1/admin/founding-customers'>"
        f"Cohort</a>"
        f"</p>"
    )
    try:
        send_admin_alert_email(subj, body)
    except Exception as e:
        logger.warning(f"[founding-customers] admin alert failed: {e}")


def _smoke():
    logger.info(f"[founding-customers] ready · cap={FOUNDING_CAP} · "
                 f"POST /tag · GET /api/v1/admin/founding-customers · "
                 f"auto_tag_if_under_cap() importable")

_smoke()
