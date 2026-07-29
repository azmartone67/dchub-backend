#!/usr/bin/env python3
"""
backfill_conversion_refunds.py — stamp sales that were refunded BEFORE the
refund webhook existed (2026-07-28).

★WHY. `mcp_conversions` only ever grew. No Stripe refund event was handled
anywhere — the webhook covered checkout.session.completed,
customer.subscription.*, and invoice.payment_failed, and nothing else — so a
refunded sale stayed in the ledger as revenue forever.

That is not hypothetical. gabriel.zuckerman@nlr.gov was double-billed
$3,000/yr across two Stripe customer records (a re-checkout mints a NEW
customer; `customer_email=` never reuses one). The duplicate was refunded by
hand in Stripe and the ledger never learned. Those two $3,000 rows then carried
77% of May and 96% of June reported MRR, manufacturing an apparent 84% "MRR
collapse" into July that never happened.

Going forward the `charge.refunded` webhook branch stamps these live. This
script fixes the history that predates it.

★REFUND STATE LIVES ON THE CHARGE, NOT THE INVOICE. The invoice for the
refunded sale still reads `status=paid` with
`post_payment_credit_notes_amount=0` — which looks exactly like an unrefunded
double charge. I nearly reported a $3,000 overcharge on a .gov customer off
that. The authority is `/v1/charges?customer=…` → `refunded` /
`amount_refunded`. Never judge refund state from the invoice.

★Stamps, never deletes: a refund is a real event in the customer's history and
the row is the audit trail. Read paths filter `refunded_at IS NULL`.

Usage:
    DATABASE_URL=... STRIPE_SECRET_KEY=... python3 backfill_conversion_refunds.py [--apply]
Dry run by default.
"""
from __future__ import annotations

import os
import sys

import requests

STRIPE_API = "https://api.stripe.com/v1"


def _charges(sk: str, customer: str) -> list[dict]:
    r = requests.get(f"{STRIPE_API}/charges",
                     auth=(sk, ""), params={"customer": customer, "limit": 20},
                     timeout=30)
    r.raise_for_status()
    return r.json().get("data") or []


def main() -> int:
    apply = "--apply" in sys.argv
    dsn = os.environ.get("DATABASE_URL") or ""
    sk = os.environ.get("STRIPE_SECRET_KEY") or ""
    if not dsn or not sk:
        print("need DATABASE_URL and STRIPE_SECRET_KEY", file=sys.stderr)
        return 2

    import psycopg2
    c = psycopg2.connect(dsn, sslmode="require", connect_timeout=20)
    c.autocommit = True
    stamped = []
    try:
        with c.cursor() as cur:
            cur.execute("""SELECT id, user_email, stripe_customer_id, mrr_cents,
                                  created_at::date
                             FROM mcp_conversions
                            WHERE stripe_customer_id IS NOT NULL
                              AND refunded_at IS NULL
                            ORDER BY created_at""")
            rows = cur.fetchall()
            print(f"{len(rows)} unstamped conversion(s) with a Stripe customer\n")
            # ★★MATCH EACH REFUND TO ONE ROW BY AMOUNT, and CONSUME it.
            # Summing a customer's refunds and stamping every unstamped row of
            # theirs is wrong: bryanseefeld95@gmail.com holds a $49 developer row
            # that WAS refunded and a $99 founding row that was NOT (still an
            # active Founding Member). Customer-wide stamping would delete live
            # revenue from every report — the mirror image of the bug being
            # fixed. kevin.d.serfass@gmail.com has the same shape ($9 + $49).
            # ★★★AND NET OFF THE KEPT CHARGES. Amount-matching alone is still
            # wrong, because a customer's charge list contains their RECURRING
            # monthly charges too — not just the opening sale.
            # cbraun@cbecommercial.com has SIX $99 charges on one customer:
            # 4 succeeded-and-kept, 1 refunded, 1 failed. He is an active monthly
            # subscriber who had a single month refunded. Matching his one $99
            # conversion row against the one refunded $99 charge would have
            # stamped a LIVE recurring customer as refunded and deleted him from
            # MRR — the mirror image of the bug being fixed.
            # kevin.d.serfass@gmail.com is the same shape twice ($49 ×3 and
            # $9 ×3, one refunded each).
            # Rule: a conversion counts as refunded only if, for that amount, the
            # customer has MORE refunded charges than kept ones.
            # ★Only status == 'succeeded' counts as kept. A FAILED charge is not
            # a payment: bryanseefeld95@gmail.com's second $99 is `failed`, so
            # his refunded opening charge nets out to genuinely refunded even
            # though his subscription is still active. (That is correct here —
            # `mrr_invoiced_usd` measures CASH; the subscription base is measured
            # separately by `mrr_run_rate_usd` from users.plan, which still
            # counts him.)
            _refunds_by_cust: dict[str, list[int]] = {}
            for _, _, cust, _, _ in rows:
                if cust in _refunds_by_cust:
                    continue
                try:
                    chs = _charges(sk, cust)
                    refunded, kept = [], []
                    for ch in chs:
                        amt_ref = int(ch.get("amount_refunded") or 0)
                        if amt_ref > 0:
                            refunded.append(amt_ref)
                        elif (ch.get("status") == "succeeded"):
                            kept.append(int(ch.get("amount") or 0))
                    for k in kept:              # a kept charge offsets a refund
                        if k in refunded:       # of the same amount
                            refunded.remove(k)
                    _refunds_by_cust[cust] = refunded
                except Exception as e:
                    print(f"  ! {cust}: Stripe lookup failed ({str(e)[:60]}) — skipped")
                    _refunds_by_cust[cust] = []

            for cid, email, cust, cents, day in rows:
                pool = _refunds_by_cust.get(cust) or []
                booked = int(cents or 0)
                if booked not in pool:
                    if pool:
                        print(f"  kept      id={cid}  {day}  {email}  "
                              f"booked=${booked/100:,.2f} — customer has refunds "
                              f"{[p/100 for p in pool]} but NONE match this amount")
                    continue
                pool.remove(booked)          # consume: one refund, one row
                print(f"  REFUNDED  id={cid}  {day}  {email}  "
                      f"booked=${booked/100:,.2f}  refunded=${booked/100:,.2f}")
                stamped.append((cid, booked))

            if not stamped:
                print("\nNo refunded sales found in the unstamped set.")
                return 0
            print(f"\n{len(stamped)} row(s) would be stamped "
                  f"(${sum(r for _, r in stamped)/100:,.2f} of booked revenue "
                  f"removed from MRR reads)")
            if not apply:
                print("DRY RUN — nothing written. Re-run with --apply.")
                return 0
            with c.cursor() as cur:
                for cid, refunded in stamped:
                    cur.execute("""UPDATE mcp_conversions
                                      SET refunded_at = NOW(), refunded_cents = %s
                                    WHERE id = %s AND refunded_at IS NULL""",
                                (refunded, cid))
                cur.execute("""SELECT COUNT(*), COALESCE(SUM(refunded_cents),0)/100.0
                                 FROM mcp_conversions WHERE refunded_at IS NOT NULL""")
                n, amt = cur.fetchone()
                print(f"\nAFTER: {n} stamped row(s), ${amt:,.2f} total refunded")
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
