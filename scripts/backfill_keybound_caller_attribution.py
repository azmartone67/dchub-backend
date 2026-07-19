#!/usr/bin/env python3
"""One-shot guarded backfill for #1660's residual: mcp_conversions rows with
caller_id=NULL (and/or key-bound rows missing the new `platform` column value)
are attributed the SAME way the live webhooks now do it —
mcp_signal_canonical.attribute_keybound_conversion / resolve_key_platform:

  1. key link: stripe_subscription_id → mcp_topups.stripe_session_id →
     api_key_hash (the pk- pack ledger keeps the sha256[:32] per checkout);
     run the canonical attribute_keybound_conversion on that hash.
  2. email fallback: caller_id = LOWER(user_email) (the exact schema_repair
     rule) + platform via email → the buyer's key → mcp_call_log history.
  3. unresolvable → left NULL. COALESCE-only writes: never relabels a row.

Dry-run by default (SET default_transaction_read_only=on — provably no
writes); pass --apply to stamp. Reports counts either way.

Usage:
    python scripts/backfill_keybound_caller_attribution.py [--apply] [--dsn DSN]
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the stamps (default: read-only dry-run)")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL")
                    or os.environ.get("NEON_DATABASE_URL"))
    args = ap.parse_args()
    if not args.dsn:
        print("ERROR: no DSN (pass --dsn or set DATABASE_URL)")
        return 2

    import psycopg2
    import mcp_signal_canonical as msc
    msc.DSN = args.dsn  # module reads DSN at import; pin it to ours

    conn = psycopg2.connect(args.dsn, connect_timeout=10)
    cur = conn.cursor()
    if not args.apply:
        cur.execute("SET default_transaction_read_only = on")
        conn.commit()

    # ── column guard ─────────────────────────────────────────────────────
    cur.execute("""SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'mcp_conversions'
                      AND column_name = 'platform'""")
    has_platform = bool(cur.fetchone())
    if args.apply and not has_platform:
        cur.execute("ALTER TABLE mcp_conversions "
                    "ADD COLUMN IF NOT EXISTS platform TEXT")
        conn.commit()
        has_platform = True
        print("DDL: added mcp_conversions.platform")

    # ── target rows ──────────────────────────────────────────────────────
    # NB: no bound params in this query → psycopg2 leaves % untouched.
    plat_arm = ("OR (source ILIKE '%keybound%' "
                "    AND COALESCE(platform, '') = '')" if has_platform
                else "OR source ILIKE '%keybound%'")
    cur.execute(f"""SELECT id, user_email, caller_id, source,
                           stripe_subscription_id
                      FROM mcp_conversions
                     WHERE caller_id IS NULL {plat_arm}
                     ORDER BY id""")
    rows = cur.fetchall()

    counts = {"examined": len(rows), "key_linked": 0, "platform_resolved": 0,
              "caller_stamped": 0, "platform_stamped": 0, "unresolvable": 0}
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {len(rows)} candidate row(s)")

    for cid, email, caller, source, sref in rows:
        e = (email or "").strip().lower() or None
        # 1) key link via the pack ledger
        key_hash = None
        if sref:
            cur.execute("SELECT api_key_hash FROM mcp_topups "
                        "WHERE stripe_session_id = %s LIMIT 1", (sref,))
            r = cur.fetchone()
            key_hash = r[0] if r and r[0] else None
        if key_hash:
            counts["key_linked"] += 1
        kp = msc.resolve_key_platform(key_hash=key_hash, email=e)
        plat = kp.get("platform")
        if plat:
            counts["platform_resolved"] += 1
        new_caller = e or kp.get("last_session")
        print(f"  conv={cid} source={source!r} sref={str(sref)[:24]!r} "
              f"key_linked={bool(key_hash)} raw_key={bool(kp.get('raw_key'))} "
              f"platform={plat!r} caller: {caller!r} -> "
              f"{(caller or new_caller)!r}")
        if not (new_caller or plat or key_hash):
            counts["unresolvable"] += 1
            continue
        if not args.apply:
            # dry-run tallies what WOULD be stamped
            if caller is None and new_caller:
                counts["caller_stamped"] += 1
            if plat:
                counts["platform_stamped"] += 1
            continue
        # ── apply ────────────────────────────────────────────────────────
        if key_hash:
            out = msc.attribute_keybound_conversion(
                key_hash=key_hash, buyer_email=e, stripe_ref=sref)
            print(f"    attribute_keybound_conversion: {out}")
            counts["caller_stamped"] += int(out.get("stamped") or 0)
            counts["platform_stamped"] += int(out.get("platform_stamped") or 0)
        else:
            if caller is None and new_caller:
                cur.execute("""UPDATE mcp_conversions
                                  SET caller_id = COALESCE(caller_id, %s)
                                WHERE id = %s AND caller_id IS NULL""",
                            (new_caller, cid))
                counts["caller_stamped"] += cur.rowcount or 0
                conn.commit()
            if plat and sref:
                counts["platform_stamped"] += msc.stamp_conversion_platform(
                    stripe_ref=sref, platform=plat)

    print(f"[{mode}] counts: {counts}")
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
