#!/usr/bin/env python3
"""
gen_dev_key.py — Mint, list, revoke, and upgrade DC Hub developer API keys.
Targets Neon Postgres directly via NEON_DATABASE_URL (or DATABASE_URL).

Usage:
    export NEON_DATABASE_URL='postgres://…neon.tech/…'

    python gen_dev_key.py mint    --email dev@acme.com --tier free
    python gen_dev_key.py mint    --email dev@acme.com --tier paid --note "Acquired via /ai signup"
    python gen_dev_key.py list    [--email dev@acme.com] [--tier free|paid|enterprise]
    python gen_dev_key.py revoke  --key dch_live_xxx
    python gen_dev_key.py upgrade --key dch_live_xxx --tier paid
    python gen_dev_key.py stats   [--days 7]

Keys are formatted: dch_live_<32-char-hex>

Dependencies:
    pip install 'psycopg[binary]>=3.2'
"""

import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timezone

import psycopg

NEON_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not NEON_URL:
    sys.stderr.write("ERROR: set NEON_DATABASE_URL (or DATABASE_URL) env var first.\n")
    sys.exit(2)


def _connect():
    return psycopg.connect(NEON_URL, autocommit=True)


def cmd_mint(args):
    api_key      = f"dch_live_{secrets.token_hex(16)}"
    developer_id = f"dev_{secrets.token_hex(8)}"
    metadata     = {"note": args.note} if args.note else {}

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO mcp_dev_keys
                 (api_key, developer_id, email, tier, status, metadata)
               VALUES (%s, %s, %s, %s, 'active', %s::jsonb)""",
            (api_key, developer_id, args.email, args.tier, json.dumps(metadata)),
        )

    print(json.dumps({
        "api_key":      api_key,
        "developer_id": developer_id,
        "email":        args.email,
        "tier":         args.tier,
        "created_at":   datetime.now(timezone.utc).isoformat(),
    }, indent=2))
    sys.stderr.write(
        "\nGive this key to the developer. They configure it as X-API-Key on /mcp.\n"
        "\n★ WARNING — THIS KEY DOES NOT AUTHENTICATE YET.\n"
        "  It was written to mcp_dev_keys only. util/tier_gate.resolve_tier grants\n"
        "  access from api_keys (key_hash IN (sha256(key), rawkey) AND is_active=1);\n"
        "  its mcp_dev_keys lookup is by key_hash, a column that table does not have,\n"
        "  so that step always raises and is swallowed. Until an api_keys row exists\n"
        "  this key resolves as ANONYMOUS.\n"
        "  Minting the api_keys row is deliberately NOT automated here: it grants\n"
        "  privilege and needs an explicit user_id + tier decision.\n"
    )


def cmd_list(args):
    sql = ("SELECT api_key, developer_id, email, tier, status, "
           "created_at, last_used_at FROM mcp_dev_keys WHERE 1=1")
    params = []
    if args.email:
        sql += " AND email = %s"
        params.append(args.email)
    if args.tier:
        sql += " AND tier = %s"
        params.append(args.tier)
    sql += " ORDER BY created_at DESC LIMIT 200"

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    out = [
        {
            "api_key": r[0], "developer_id": r[1], "email": r[2], "tier": r[3],
            "status": r[4],
            "created_at":   r[5].isoformat() if r[5] else None,
            "last_used_at": r[6].isoformat() if r[6] else None,
        }
        for r in rows
    ]
    print(json.dumps(out, indent=2))


def cmd_revoke(args):
    """Revoke a key EVERYWHERE it can authenticate.

    ★★★ TWO DISJOINT TABLES AUTHENTICATE, AND WHICH ONE DEPENDS ON ORIGIN.

      * `api_keys`     — dashboard / partner / paid keys. Read by
                         util/tier_gate.resolve_tier step 1b:
                             key_hash IN (sha256(key), rawkey)
                               AND (is_active IS NULL OR is_active = 1)
                         key_hash is sha256(key) for customer keys and the RAW
                         string for partner/admin keys, so both are matched.
                         is_active is an INTEGER: write 0, never FALSE — `IN (1,
                         TRUE)` throws `operator does not exist: integer =
                         boolean`, which the callers' bare excepts swallow into
                         a silent anon fall-through.

      * `mcp_dev_keys` — MCP-minted keys (claim_free_key, OAuth, pair-code).
                         Read by flask_mcp_endpoints POST /api/v1/keys/validate
                         — the hop the Node MCP server relays — which matches
                         `api_key = %s` and requires `status = 'active'`.
                         Setting status='revoked' is a COMPLETE revoke for this
                         class. There is no key_hash column and no api_keys row.

    resolve_tier step 1a LOOKS like a third path but is dead code: it queries
    `mcp_dev_keys WHERE key_hash = %s`, and that table has no key_hash column
    (migration_001_api_keys.sql — api_key is the PK), so it always raises
    UndefinedColumn into a bare except.

    ── This command has now failed in BOTH directions ───────────────────────
    2026-08-16: it wrote mcp_dev_keys ONLY, so revoking an api_keys-backed key
    reported success and left the credential FULLY LIVE. Fixed by also writing
    api_keys.

    2026-08-31: that fix OVER-CORRECTED. It failed the whole command whenever
    api_keys matched 0 rows — printing "REVOKE DID NOT TAKE … Do NOT treat this
    as a successful rotation" and exiting 1 — and its JSON note claimed
    "api_keys is the ONLY table consulted for auth". For an MCP-minted key both
    are backwards: it has no api_keys row BY CONSTRUCTION, and
    `revoked_in_mcp_dev_keys: 1` is already a complete revoke. Verified live on
    2026-08-31 revoking a leaked free key: the tool printed the failure banner
    and exited nonzero while the key was fully dead — mcp_dev_keys.status
    ='revoked', the /api/v1/keys/validate query returned accept=false, and a
    live tools/call on https://dchub.cloud/mcp with the key returned
    byte-identical output to a bogus key and to no key at all.

    A revoke tool that cries failure on a real revoke is not a safe tool: the
    operator either stops trusting the exit code or re-runs a rotation that
    already succeeded.

    ── The failure condition is now measured against the key's ACTUAL home ──
    Both tables are read BEFORE and AFTER the updates:

      * no row in EITHER table   → hard failure, exit 1. Nothing was revoked,
        because this tool never issued this key. (dch_trial_ keys live in a
        THIRD table, auto_trial_keys, which this command does not manage.)
      * still live afterwards    → hard failure, exit 1. The UPDATE did not take.
      * a home, nothing left live → success, exit 0, naming the home and its gate.

    The post-state is re-read rather than inferred from rowcount, so "it is
    revoked" is a measurement rather than a claim.
    """
    import hashlib
    key_hash = hashlib.sha256(args.key.encode("utf-8")).hexdigest()

    # Row census per table: (rows_present, rows_still_live). Presence answers
    # "should this table have held the key"; liveness answers "is it dead yet".
    _API_STATE = """SELECT COUNT(*)::int,
                           COUNT(*) FILTER (
                               WHERE is_active IS NULL OR is_active = 1)::int
                      FROM api_keys WHERE key_hash IN (%s, %s)"""
    _MCP_STATE = """SELECT COUNT(*)::int,
                           COUNT(*) FILTER (WHERE status = 'active')::int
                      FROM mcp_dev_keys WHERE api_key = %s"""
    _GATE = {
        "api_keys":     "util/tier_gate.resolve_tier step 1b, on is_active",
        "mcp_dev_keys": "POST /api/v1/keys/validate, on status='active'",
    }

    with _connect() as conn, conn.cursor() as cur:
        # ── BEFORE ────────────────────────────────────────────────────────
        cur.execute(_API_STATE, (key_hash, args.key))
        api_rows, api_live = cur.fetchone() or (0, 0)
        cur.execute(_MCP_STATE, (args.key,))
        mcp_rows, mcp_live = cur.fetchone() or (0, 0)

        # ── REVOKE ── both tables; a given key normally lives in exactly one.
        cur.execute(
            """UPDATE api_keys SET is_active = 0
                WHERE key_hash IN (%s, %s)
                  AND (is_active IS NULL OR is_active = 1)""",
            (key_hash, args.key),
        )
        auth_n = cur.rowcount
        cur.execute(
            """UPDATE mcp_dev_keys SET status='revoked'
                WHERE api_key = %s AND status = 'active'""",
            (args.key,),
        )
        ledger_n = cur.rowcount

        # ── AFTER ── re-read; never infer the result from rowcount alone.
        cur.execute(_API_STATE, (key_hash, args.key))
        _, api_live_after = cur.fetchone() or (0, 0)
        cur.execute(_MCP_STATE, (args.key,))
        _, mcp_live_after = cur.fetchone() or (0, 0)

    homes = [t for t, n in (("api_keys", api_rows),
                            ("mcp_dev_keys", mcp_rows)) if n]
    still_live = [t for t, n in (("api_keys", api_live_after),
                                 ("mcp_dev_keys", mcp_live_after)) if n]
    already = bool(homes) and not still_live and (api_live + mcp_live) == 0

    print(json.dumps({
        "api_key": args.key,
        "found_in": homes,                  # ← which table SHOULD have held it
        "revoked_in_api_keys": auth_n,
        "revoked_in_mcp_dev_keys": ledger_n,
        "authenticating_rows_disabled": auth_n + ledger_n,
        "still_live_in": still_live,        # ← non-empty is the real failure
        "already_revoked": already,
        "revoked": bool(homes) and not still_live,
        "note": ("api_keys (dashboard/partner/paid) and mcp_dev_keys "
                 "(MCP-minted; gated by POST /api/v1/keys/validate on "
                 "status='active') are DISJOINT authenticators and a key lives "
                 "in ONE of them. The revoke is complete when no live row "
                 "remains in the table that holds THIS key — so "
                 "revoked_in_api_keys: 0 with revoked_in_mcp_dev_keys: 1 is a "
                 "COMPLETE revoke, not a failure. still_live_in is the field "
                 "that means the key may still work."),
    }, indent=2))

    if not homes:
        sys.stderr.write(
            "\nREVOKE DID NOT TAKE: this key has no row in api_keys OR "
            "mcp_dev_keys.\n"
            "Nothing was revoked — this tool never issued this key. Trial keys "
            "(dch_trial_…) live in auto_trial_keys, which this command does not "
            "manage.\n"
            "Do NOT treat this as a successful rotation — verify with a live "
            "call before retiring the old credential.\n")
        sys.exit(1)

    if still_live:
        sys.stderr.write(
            "\nREVOKE DID NOT TAKE: the key is STILL LIVE in "
            f"{', '.join(still_live)} after the UPDATE.\n"
            + "".join(f"  {t} gate: {_GATE[t]}\n" for t in still_live)
            + "Do NOT treat this as a successful rotation — verify with a live "
              "call before retiring the old credential.\n")
        sys.exit(1)

    sys.stderr.write(
        "\nREVOKE COMPLETE"
        + (" — already revoked before this run; no rows changed" if already else "")
        + ".\n"
        + "".join(f"  {t}: no live row remains (gate: {_GATE[t]})\n" for t in homes))
    if homes == ["mcp_dev_keys"]:
        sys.stderr.write(
            "  This is an MCP-minted key: it has no api_keys row by "
            "construction, so revoked_in_api_keys: 0 is expected here and is "
            "NOT a failure.\n")


def cmd_upgrade(args):
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE mcp_dev_keys SET tier=%s WHERE api_key=%s AND status='active'",
            (args.tier, args.key),
        )
        n = cur.rowcount
    print(json.dumps(
        {"upgraded": bool(n), "api_key": args.key, "tier": args.tier},
        indent=2,
    ))


def cmd_stats(args):
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tier, COUNT(*)::int, COUNT(*) FILTER (WHERE last_used_at IS NOT NULL)::int "
            "FROM mcp_dev_keys WHERE status='active' GROUP BY tier ORDER BY tier"
        )
        keys = [{"tier": r[0], "n": r[1], "active_users": r[2]} for r in cur.fetchall()]

        cur.execute(
            """SELECT COUNT(*)::int                                AS calls,
                      COUNT(DISTINCT api_key)                       AS keyed_devs,
                      COUNT(*) FILTER (WHERE status='blocked_paid_only')::int AS upgrade_blocks
               FROM mcp_call_log
               WHERE timestamp >= NOW() - make_interval(days => %s)""",
            (args.days,),
        )
        r = cur.fetchone() or (0, 0, 0)
        funnel = {"calls": r[0] or 0, "keyed_devs": r[1] or 0, "upgrade_blocks": r[2] or 0}

    print(json.dumps({"days": args.days, "keys_by_tier": keys, "funnel": funnel}, indent=2))


def main():
    p = argparse.ArgumentParser(prog="gen_dev_key.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mint", help="Mint a new developer API key")
    m.add_argument("--email", required=True)
    m.add_argument("--tier", choices=["free", "paid", "enterprise"], default="free")
    m.add_argument("--note", default=None)
    m.set_defaults(func=cmd_mint)

    l = sub.add_parser("list", help="List API keys")
    l.add_argument("--email")
    l.add_argument("--tier", choices=["free", "paid", "enterprise"])
    l.set_defaults(func=cmd_list)

    r = sub.add_parser("revoke", help="Revoke a key")
    r.add_argument("--key", required=True)
    r.set_defaults(func=cmd_revoke)

    u = sub.add_parser("upgrade", help="Change a key's tier (e.g. free → paid)")
    u.add_argument("--key", required=True)
    u.add_argument("--tier", required=True, choices=["free", "paid", "enterprise"])
    u.set_defaults(func=cmd_upgrade)

    s = sub.add_parser("stats", help="Quick funnel stats from mcp_call_log")
    s.add_argument("--days", type=int, default=7)
    s.set_defaults(func=cmd_stats)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
