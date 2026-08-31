"""
routes/billing_portal.py — customer-initiated Stripe portal: resolver + funnel
instrumentation (2026-08-31, r-portal-trap).

THE PROBLEM THIS SOLVES: a paying customer emailed on 2026-08-30 saying "the
website is not letting me open Stripe to cancel it." He was right, and the cause
is structural rather than flaky.

`/api/stripe/portal` (main.py) resolved the Stripe customer with EXACTLY ONE
lookup:

    SELECT stripe_customer_id FROM users WHERE id = %s
    if not user or not user[0]: return 404 'No subscription found'

`users.stripe_customer_id` is written by the checkout webhook path. Anyone who
paid through a bare Stripe PAYMENT LINK (buy.stripe.com/...) rather than a
Checkout Session bound to a logged-in account never got that column populated —
the same email-matching gap already documented on the MCP side by
`reconcile_mcp_tiers` ("the checkout webhook upgrades mcp_dev_keys by matching on
email, so a customer who paid BEFORE claiming a key matched 0 rows"). The admin
customer portal states the blast radius plainly: billing and the paywall are
"nearly-DISJOINT populations".

For those accounts the portal was not flaky. It was CLOSED. They are billed on a
recurring basis and the only self-serve exit returns 404, which the dashboard
renders as the generic toast "Unable to open billing portal". A customer who
cannot cancel is a chargeback and a complaint, not a churn statistic.

WHAT THIS MODULE DOES
  · resolve_stripe_customer() — falls back to an exact-email lookup in Stripe
    when the column is empty, prefers a customer with a LIVE subscription,
    BACKFILLS users.stripe_customer_id so the repair is permanent, and returns a
    machine-readable reason when it genuinely finds nothing.
  · log_portal_event() — fire-and-forget funnel row. Every attempt and every
    outcome. Never raises, never blocks the response.

WHY THE TELEMETRY SHIPS WITH THE FIX: this failure was INVISIBLE. The frontend
swallowed every error into one toast, the handler only `print()`ed, and nothing
counted attempts. The absence of cancellations read as retention. A cancel funnel
you cannot see is one you cannot claim works — so the counter lands in the same
change as the fix.

SAFETY: read-mostly. The only write to `users` is backfilling
stripe_customer_id from a customer Stripe itself matched on the account's own
verified email. No plan, tier, or subscription state is touched here; cancelation
happens in Stripe's own hosted portal, never in this process.
"""

import hashlib
import logging
import os

logger = logging.getLogger(__name__)

_schema_ready = False

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS billing_portal_events (
    id          BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id     TEXT,
    email_hash  TEXT,
    event       TEXT NOT NULL,
    detail      TEXT,
    ip_hash     TEXT
);
CREATE INDEX IF NOT EXISTS idx_bpe_created ON billing_portal_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bpe_event   ON billing_portal_events (event, created_at DESC);
"""

# Funnel vocabulary. ATTEMPT is emitted before any work so that
# attempts - (ok + recovered) is a real abandonment/failure count rather than a
# count of the failures we happened to remember to log.
EV_ATTEMPT       = "attempt"
EV_OK            = "ok"
EV_RECOVERED     = "recovered_by_email"   # the fix firing: column was empty, Stripe knew them
EV_NO_CUSTOMER   = "no_customer"          # genuinely never paid — the honest 404
EV_STRIPE_ERROR  = "stripe_error"
EV_NOT_CONFIGURED = "not_configured"


def _hash(s):
    """Short, stable, non-reversible. Emails are hashed rather than stored so
    this funnel table carries no PII of its own."""
    if not s:
        return None
    return hashlib.sha256(str(s).strip().lower().encode()).hexdigest()[:16]


def _ensure_schema(conn):
    global _schema_ready
    if _schema_ready:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
        try:
            conn.commit()
        except Exception:
            pass
        _schema_ready = True
    except Exception as e:
        logger.warning("[billing_portal] schema ensure failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass


def log_portal_event(conn, event, user_id=None, email=None, detail=None, ip=None):
    """Fire-and-forget funnel row. NEVER raises — instrumentation must not be
    able to break the cancel path it exists to measure."""
    if conn is None:
        return
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO billing_portal_events
                     (user_id, email_hash, event, detail, ip_hash)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                (str(user_id)[:64] if user_id else None,
                 _hash(email),
                 str(event)[:32],
                 str(detail)[:300] if detail else None,
                 _hash(ip)))
        try:
            conn.commit()
        except Exception:
            pass
    except Exception as e:
        logger.warning("[billing_portal] event log failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass


def _live_first(customers):
    """Prefer a customer with a subscription that can actually be managed.

    A repeat buyer can hold SEVERAL Stripe customer records against one email
    (the duplicate-subscription trap). Handing the portal the wrong one shows a
    customer an empty portal and looks exactly like the bug we are fixing, so
    rank: live subscription first, then most recently created.
    """
    def rank(c):
        subs = ((c.get("subscriptions") or {}).get("data") or []) if isinstance(c, dict) else []
        live = any((s.get("status") in ("active", "trialing", "past_due", "unpaid"))
                   for s in subs)
        return (0 if live else 1, -(c.get("created") or 0))
    return sorted(customers, key=rank)


def resolve_stripe_customer(stripe, conn, user_id):
    """Return (customer_id, reason, email).

    reason is one of: 'column' (already linked), 'email_backfill' (the repair
    fired), or None when nothing was found. Never raises.

    The account email is read from `users` here rather than taken from the JWT:
    the token payload shape is not guaranteed to carry it, and the email that
    matters for a Stripe lookup is the one on the ACCOUNT, not the one in a
    token that may predate an email change.
    """
    email = None

    # 1. The happy path — already linked. One query gets both fields.
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT stripe_customer_id, email FROM users WHERE id = %s",
                (user_id,))
            row = cur.fetchone()
        if row:
            email = row[1]
            if row[0]:
                return row[0], "column", email
    except Exception as e:
        logger.warning("[billing_portal] users lookup failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass

    # 2. The repair — the column is empty, but Stripe may still know this email.
    #    This is the whole point of the module: a payment-link buyer is a real
    #    customer whose row simply never got written.
    if not email:
        return None, None, None
    try:
        found = stripe.Customer.list(email=str(email).strip().lower(),
                                     limit=10, expand=["data.subscriptions"])
        data = [dict(c) for c in (found.get("data") or [])]
    except Exception as e:
        logger.warning("[billing_portal] stripe customer lookup failed: %s", e)
        return None, None, email
    if not data:
        return None, None, email

    cid = _live_first(data)[0].get("id")
    if not cid:
        return None, None, email

    # 3. Make the repair permanent so the next open is a plain column hit.
    #    Guarded: only fills an EMPTY column, never overwrites an existing link.
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE users
                      SET stripe_customer_id = %s
                    WHERE id = %s
                      AND COALESCE(stripe_customer_id, '') = ''""",
                (cid, user_id))
        try:
            conn.commit()
        except Exception:
            pass
    except Exception as e:
        logger.warning("[billing_portal] backfill failed (serving anyway): %s", e)
        try:
            conn.rollback()
        except Exception:
            pass

    return cid, "email_backfill", email


def support_mailto():
    """The escape hatch shown when self-serve genuinely cannot resolve. A dead
    toast is what turned a cancelation into a support email we only saw because
    the customer bothered to write twice."""
    return os.environ.get("DCHUB_SUPPORT_EMAIL", "jonathan@dchub.cloud")
