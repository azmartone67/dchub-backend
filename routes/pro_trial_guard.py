"""One Pro trial per person (r-one-trial, 2026-09-24).

The Pro 7-day trial Payment Link (Pro $99/mo price, card required, checkout
metadata offer=pro_trial_7d) starts a Stripe subscription in 'trialing'.
Payment Links cannot limit a trial to one per person, so anyone could take a
second free week with the same email or the same agent key. Owner decision
(2026-09-24): on a REPEAT, end the new trial immediately, so Stripe charges the
first $99 now instead of granting another free week.

HOW
===
On checkout.session.completed for the trial offer (called from main.py's
webhook dispatcher, after handle_checkout_completed):

  1. the identity is the checkout email (lower-cased), the Stripe customer, and
     the key ref (`pk-<sha256 key>`) when the checkout carries one in
     client_reference_id;
  2. a PRIOR redemption on a different subscription matching ANY of those
     makes this one a repeat;
  3. every redemption is recorded in pro_trial_redemptions (PK = subscription
     id, so a webhook retry is a no-op and never acts twice);
  4. a repeat calls stripe.Subscription.modify(sub, trial_end='now'), and main
     alerts the owner.

The signal is the CHECKOUT session: its offer metadata, or its payment_link
id. The live trial link has no metadata (measured 2026-09-24), so the id is what
fires today; metadata stays accepted in case it is added later.

FAIL-SAFE: if the ledger cannot be read, nothing is ended — a repeat cannot be
proven, and ending a real first trial would charge someone who was promised a
free week. The failure is returned for the caller to log.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("pro_trial_guard")

OFFER = "pro_trial_7d"
TABLE = "pro_trial_redemptions"
_KEY_REF_RE = re.compile(r"^pk-[0-9a-f]{16,64}$")

DDL = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    stripe_subscription_id TEXT PRIMARY KEY,
    stripe_session_id      TEXT,
    stripe_customer_id     TEXT,
    email                  TEXT,
    key_ref                TEXT,
    repeat_of              TEXT,
    action                 TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW())"""

SEEN_SQL = f"SELECT 1 FROM {TABLE} WHERE stripe_subscription_id = %s"

PRIOR_SQL = f"""SELECT stripe_subscription_id FROM {TABLE}
    WHERE stripe_subscription_id <> %(sub)s
      AND (   (%(email)s <> '' AND LOWER(email) = %(email)s)
           OR (%(cust)s  <> '' AND stripe_customer_id = %(cust)s)
           OR (%(kref)s  <> '' AND key_ref = %(kref)s))
    ORDER BY created_at ASC LIMIT 1"""

INSERT_SQL = f"""INSERT INTO {TABLE}
    (stripe_subscription_id, stripe_session_id, stripe_customer_id,
     email, key_ref, repeat_of, action)
    VALUES (%(sub)s, %(sess)s, %(cust)s, %(email)s, %(kref)s, %(repeat_of)s, %(action)s)
    ON CONFLICT (stripe_subscription_id) DO NOTHING"""

UPDATE_ACTION_SQL = f"UPDATE {TABLE} SET action = %s WHERE stripe_subscription_id = %s"

# DDL once per process (r-ddl-once, 2026-08-31: a no-op DDL on every request
# still queues for a lock and stalled production behind a pg_dump).
_TABLE_READY = False


def is_trial_offer(session: dict) -> bool:
    """The trial checkout: offer metadata OR the trial Payment Link's id.

    The live link carries no offer metadata (2026-09-24 live gate: both trial
    checkouts skipped this guard), so the link id is the signal that fires."""
    from routes._stripe_links import PRO_TRIAL_PAYMENT_LINK_ID
    s = session or {}
    tagged = (((s.get("metadata") or {}).get("offer") or "").strip() == OFFER
              or (s.get("payment_link") or "") == PRO_TRIAL_PAYMENT_LINK_ID)
    return (tagged
            and s.get("mode") == "subscription"
            and bool(s.get("subscription")))


def identity(session: dict) -> dict:
    """{sub, sess, cust, email, kref} from a checkout session; '' when absent."""
    s = session or {}
    sub = s.get("subscription") or ""
    if isinstance(sub, dict):
        sub = sub.get("id") or ""
    cust = s.get("customer") or ""
    if isinstance(cust, dict):
        cust = cust.get("id") or ""
    email = ((s.get("customer_details") or {}).get("email")
             or s.get("customer_email") or "")
    ref = (s.get("client_reference_id") or "").strip().lower()
    return {"sub": str(sub), "sess": str(s.get("id") or ""), "cust": str(cust),
            "email": email.strip().lower(),
            "kref": ref if _KEY_REF_RE.match(ref) else ""}


def _conn():
    """Raw psycopg2 connection, or None. Never raises."""
    try:
        import psycopg2
        dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""
        if dsn:
            return psycopg2.connect(dsn, connect_timeout=6)
    except Exception as e:  # noqa: BLE001
        log.warning("pro_trial_guard: connect failed: %s", e)
    return None


def _end_trial_now(sub_id: str) -> None:
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    stripe.Subscription.modify(sub_id, trial_end="now")


def enforce_one_trial(session: dict, *, conn_factory=_conn, end_trial=_end_trial_now) -> dict:
    """Record this trial redemption; end it now if the person already had one.

    Returns {"skipped": reason} | {"repeat": bool, "repeat_of": sub|None,
    "action": ..., "identity": {...}} | {"error": ...}. Never raises."""
    global _TABLE_READY
    if not is_trial_offer(session):
        return {"skipped": "not_trial_offer"}
    ident = identity(session)
    if not (ident["email"] or ident["cust"] or ident["kref"]):
        return {"skipped": "no_identity", "identity": ident}
    conn = conn_factory()
    if conn is None:
        return {"error": "no_db", "identity": ident}
    try:
        with conn, conn.cursor() as cur:
            if not _TABLE_READY:
                cur.execute(DDL)
                _TABLE_READY = True
            cur.execute(SEEN_SQL, (ident["sub"],))
            if cur.fetchone():
                return {"skipped": "already_seen", "identity": ident}
            cur.execute(PRIOR_SQL, ident)
            row = cur.fetchone()
            repeat_of = row[0] if row else None
            action = "ending_trial" if repeat_of else "first_trial"
            cur.execute(INSERT_SQL, {**ident, "repeat_of": repeat_of, "action": action})
    except Exception as e:  # noqa: BLE001 — fail-safe: cannot prove a repeat
        log.warning("pro_trial_guard: ledger failed, nothing ended: %s", e)
        return {"error": f"ledger:{type(e).__name__}", "identity": ident}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    if not repeat_of:
        return {"repeat": False, "repeat_of": None, "action": "first_trial", "identity": ident}

    try:
        end_trial(ident["sub"])
        action = "trial_ended_now"
    except Exception as e:  # noqa: BLE001
        log.warning("pro_trial_guard: ending trial %s failed: %s", ident["sub"], e)
        action = f"end_failed:{type(e).__name__}"
    conn = conn_factory()
    if conn is not None:
        try:
            with conn, conn.cursor() as cur:
                cur.execute(UPDATE_ACTION_SQL, (action, ident["sub"]))
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return {"repeat": True, "repeat_of": repeat_of, "action": action, "identity": ident}
