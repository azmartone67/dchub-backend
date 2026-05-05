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
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE mcp_dev_keys SET status='revoked' WHERE api_key=%s AND status='active'",
            (args.key,),
        )
        n = cur.rowcount
    print(json.dumps({"revoked": bool(n), "api_key": args.key}, indent=2))


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
