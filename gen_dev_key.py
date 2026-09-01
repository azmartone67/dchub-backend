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
        "\n★ SCOPE — THIS KEY AUTHENTICATES ON /mcp BUT NOT ON REST.\n"
        "  It was written to mcp_dev_keys only. That IS a real credential on the\n"
        "  MCP path: flask_mcp_endpoints POST /api/v1/keys/validate — the hop the\n"
        "  Node MCP server relays — reads mcp_dev_keys WHERE api_key = %s and\n"
        "  accepts status='active'.\n"
        "  On REST it resolves ANONYMOUS: util/tier_gate.resolve_tier grants from\n"
        "  api_keys (key_hash IN (sha256(key), rawkey) AND is_active=1), and its\n"
        "  mcp_dev_keys lookup is by key_hash — a column that table does not have —\n"
        "  so that step always raises and is swallowed.\n"
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
    """Revoke a key EVERYWHERE it can authenticate, then VERIFY the post-state.

    ★★★ 2026-08-16 — THIS COMMAND DID NOT REVOKE ANYTHING. It ran only
    `UPDATE mcp_dev_keys SET status='revoked'`, so the operator saw
    `"revoked": true` and the key stayed FULLY LIVE.

    ★★★ 2026-08-31 — THE 08-16 FIX OVER-CORRECTED INTO THE MIRROR-IMAGE BUG.
    It concluded "api_keys is the ONLY table consulted for auth" and exited 1
    with "REVOKE DID NOT TAKE" whenever api_keys matched 0 rows. That is
    backwards for MCP-minted keys, which are the MAJORITY (648 mcp_dev_keys
    rows on 08-31). Revoking a leaked free key printed the full failure banner
    and exited non-zero for a revoke that had completely succeeded.

    THERE ARE TWO KEY ID SPACES AND NEITHER IS "THE" AUTHENTICATOR — which one
    applies depends on where the key was MINTED:

      • api_keys      — dashboard / partner / paid keys. Consulted by
                        util/tier_gate.resolve_tier step 1b. key_hash is
                        sha256(key) for customer keys and the RAW string for
                        partner/admin keys, so both must be matched.
      • mcp_dev_keys  — MCP-minted keys (claim_free_key, OAuth, pair-code).
                        Consulted by flask_mcp_endpoints POST
                        /api/v1/keys/validate — the hop the Node MCP server
                        relays on every call — as
                        `WHERE api_key = %s` requiring status='active'.
                        ★ That is a REAL gate with teeth, not bookkeeping.

    (resolve_tier step 1a queries `mcp_dev_keys WHERE key_hash = %s`. That
    column does not exist, so it always raises UndefinedColumn into a bare
    except. It is dead code and authenticates nobody — do not count it.)

    So `revoked_in_api_keys: 0` + `revoked_in_mcp_dev_keys: 1` is a COMPLETE,
    SUCCESSFUL revoke of a free key, not a failure.

    ★ The exit code is now decided by the POST-STATE, re-read after the writes,
    not by rowcounts. Rowcounts say what changed; they cannot distinguish
    "already revoked" (success — the credential is not live) from "no such key"
    (failure — likely a typo or the wrong environment), and the old code failed
    loudly on both.

    NB api_keys.is_active is an INTEGER column: write 0, never FALSE. `IN (1,
    TRUE)` throws `operator does not exist: integer = boolean`, which the
    callers' bare excepts swallow into a silent anon fall-through.
    """
    import hashlib
    key_hash = hashlib.sha256(args.key.encode("utf-8")).hexdigest()
    with _connect() as conn, conn.cursor() as cur:
        # 1) dashboard / partner / paid keys
        cur.execute(
            """UPDATE api_keys SET is_active = 0
                WHERE key_hash IN (%s, %s)
                  AND (is_active IS NULL OR is_active = 1)""",
            (key_hash, args.key),
        )
        auth_n = cur.rowcount
        # 2) MCP-minted keys — /api/v1/keys/validate gates on this status
        cur.execute(
            """UPDATE mcp_dev_keys SET status='revoked'
                WHERE api_key = %s AND status = 'active'""",
            (args.key,),
        )
        ledger_n = cur.rowcount

        # 3) ★ re-read BOTH id spaces. This, not the rowcounts, decides the
        #    exit code — "did I change a row" is a weaker question than
        #    "is this credential still accepted anywhere".
        cur.execute(
            """SELECT COUNT(*) FILTER (WHERE is_active IS NULL OR is_active = 1),
                      COUNT(*)
                 FROM api_keys WHERE key_hash IN (%s, %s)""",
            (key_hash, args.key),
        )
        ak_live, ak_rows = cur.fetchone()
        cur.execute(
            """SELECT COUNT(*) FILTER (WHERE COALESCE(status, 'active') = 'active'),
                      COUNT(*)
                 FROM mcp_dev_keys WHERE api_key = %s""",
            (args.key,),
        )
        dk_live, dk_rows = cur.fetchone()

    found_rows = (ak_rows or 0) + (dk_rows or 0)
    still_live = (ak_live or 0) + (dk_live or 0)

    print(json.dumps({
        # never echo the whole credential back — matches resolve_tier's
        # `api_key[:8] + "…"` convention. Echoing it is how a live key ended up
        # in a transcript on 08-31.
        "api_key_prefix":          args.key[:12] + "…",
        "revoked_in_api_keys":     auth_n,
        "revoked_in_mcp_dev_keys": ledger_n,
        "rows_found":              found_rows,
        "still_accepted_anywhere": still_live > 0,
        "note": ("BOTH tables authenticate — api_keys via tier_gate step 1b, "
                 "mcp_dev_keys via /api/v1/keys/validate (the MCP hop). A key "
                 "minted by claim_free_key has NO api_keys row, so "
                 "revoked_in_api_keys=0 with mcp_dev_keys=1 is a COMPLETE "
                 "revoke, not a failure."),
    }, indent=2))

    if found_rows == 0:
        sys.stderr.write(
            "\nUNKNOWN KEY: matched no row in api_keys OR mcp_dev_keys.\n"
            "Nothing was revoked because nothing was found. Check for a typo "
            "and confirm you are pointed at the right environment "
            "(NEON_DATABASE_URL) before concluding this key is retired.\n")
        sys.exit(1)

    if still_live > 0:
        sys.stderr.write(
            "\nREVOKE DID NOT TAKE: the key is STILL ACCEPTED after the write "
            f"(api_keys live rows: {ak_live}, mcp_dev_keys active rows: {dk_live}).\n"
            "Do NOT treat this as a successful rotation — verify with a live "
            "call before retiring the old credential.\n")
        sys.exit(1)


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
