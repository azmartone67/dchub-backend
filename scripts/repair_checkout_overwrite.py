#!/usr/bin/env python3
"""One-shot guarded repair for backend#5555 on ONE account: undo a checkout
that re-pointed an existing users row to a new Stripe customer (and wrote
`role` from the plan).

Why: handle_checkout_completed overwrites users.stripe_customer_id and role on
every checkout, and the cancel handlers demote every users row WHERE
stripe_customer_id = <canceled customer>. So cancelling the new subscription
would demote the account off its original access. Live case 2026-09-25:
admin001 (azmartone@gmail.com) re-pointed to cus_VK46QB7qWNwWX5 by a test
trial checkout.

What it does:
  1. reads the users row for --email;
  2. no-op unless its stripe_customer_id == --stale-customer;
  3. asks Stripe for the address's OTHER customers with a live subscription
     (active / trialing / past_due) and picks the newest; none -> NULL;
  4. with --apply, in one transaction: sets stripe_customer_id to that value
     (WHERE id = row AND stripe_customer_id = stale, so it is idempotent) and,
     when --role is given, role = --role. Prints before/after.

Dry-run by default (read-only transaction, provably no writes).

Usage:
    python scripts/repair_checkout_overwrite.py --email E --stale-customer cus_X \
        [--role admin] [--apply] [--dsn DSN]
"""
import argparse
import os
import sys

LIVE = ("active", "trialing", "past_due")
ROLES = ("admin", "pro", "free", "enterprise", "founding", "developer")


def live_customers(email, stale, stripe_mod=None):
    """[(customer_id, newest_live_sub_created)] for the address, excluding stale."""
    stripe = stripe_mod
    if stripe is None:
        import stripe  # noqa: PLC0415
        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    out = []
    for c in stripe.Customer.list(email=email, limit=100).auto_paging_iter():
        cid = c["id"]
        if cid == stale:
            continue
        subs = stripe.Subscription.list(customer=cid, status="all", limit=100).data
        live = [s["created"] for s in subs if s["status"] in LIVE]
        if live:
            out.append((cid, max(live)))
    return sorted(out, key=lambda t: t[1], reverse=True)


def plan_repair(row, stale, candidates, role=None):
    """The write to make, or None. row = (id, plan, role, stripe_customer_id, status)."""
    if not row or row[3] != stale:
        return None
    return {"id": row[0],
            "stripe_customer_id": candidates[0][0] if candidates else None,
            "role": role}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--stale-customer", required=True)
    ap.add_argument("--role", choices=ROLES)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL")
                    or os.environ.get("NEON_DATABASE_URL"))
    a = ap.parse_args(argv)
    if not a.dsn:
        print("ERROR: no DSN (pass --dsn or set DATABASE_URL)")
        return 2
    import psycopg2  # noqa: PLC0415
    sel = ("SELECT id, plan, role, stripe_customer_id, subscription_status "
           "FROM users WHERE LOWER(email) = LOWER(%s)")
    conn = psycopg2.connect(a.dsn, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            if not a.apply:
                cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(sel, (a.email,))
            rows = cur.fetchall()
            if len(rows) != 1:
                print(f"ERROR: expected 1 users row for {a.email}, found {len(rows)}")
                return 3
            row = rows[0]
            print(f"before: id={row[0]} plan={row[1]} role={row[2]} "
                  f"stripe_customer_id={row[3]} status={row[4]}")
            cands = live_customers(a.email, a.stale_customer)
            print(f"other Stripe customers with a live subscription: {cands or 'none'}")
            fix = plan_repair(row, a.stale_customer, cands, a.role)
            if fix is None:
                print("no-op: stripe_customer_id is not the stale customer")
                return 0
            print(f"plan: stripe_customer_id -> {fix['stripe_customer_id']}"
                  + (f", role -> {fix['role']}" if fix["role"] else ", role unchanged"))
            if not a.apply:
                print("dry-run: nothing written (pass --apply)")
                return 0
            cur.execute("UPDATE users SET stripe_customer_id = %s "
                        "WHERE id = %s AND stripe_customer_id = %s",
                        (fix["stripe_customer_id"], fix["id"], a.stale_customer))
            n = cur.rowcount
            if fix["role"]:
                cur.execute("UPDATE users SET role = %s WHERE id = %s",
                            (fix["role"], fix["id"]))
            cur.execute(sel, (a.email,))
            after = cur.fetchone()
        conn.commit()
        print(f"applied ({n} row): id={after[0]} plan={after[1]} role={after[2]} "
              f"stripe_customer_id={after[3]} status={after[4]}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
