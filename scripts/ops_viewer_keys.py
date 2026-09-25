#!/usr/bin/env python3
"""Mint, revoke and inspect ops viewer keys (see ops_viewer_gate.py).

    python3 scripts/ops_viewer_keys.py mint jonathan-claude-dashboard --note "Claude sessions"
    python3 scripts/ops_viewer_keys.py revoke prospecting-checks
    python3 scripts/ops_viewer_keys.py status
    python3 scripts/ops_viewer_keys.py denials --days 7

mint / revoke need the ADMIN key: $DCHUB_ADMIN_KEY, else the first line of
~/.dchub_admin_key. status / denials take a viewer key instead if
$DCHUB_VIEWER_KEY is set. The base URL is the Railway origin (not
dchub.cloud) unless $DCHUB_API_BASE says otherwise.

`mint` prints ONLY the new key on stdout (everything else goes to stderr), so
    KEY=$(python3 scripts/ops_viewer_keys.py mint dchub-brain-micro)
never echoes it. It is shown once and stored nowhere but in the caller you
load it into; minting a name that already has an active key is refused until
that key is revoked.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

DEFAULT_BASE = "https://dchub-backend-production.up.railway.app"
UA = "dchub-ops-viewer-keys/1.0"


def _admin_key() -> str:
    k = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    if k:
        return k.split()[0]
    path = os.path.expanduser("~/.dchub_admin_key")
    try:
        with open(path, encoding="utf-8") as fh:
            line = fh.readline().strip()
            return line.split()[0] if line else ""
    except OSError:
        return ""


def _call(method: str, path: str, key: str, body=None):
    import requests
    base = (os.environ.get("DCHUB_API_BASE") or DEFAULT_BASE).rstrip("/")
    r = requests.request(method, base + path, json=body, timeout=30, headers={
        "X-Admin-Key": key, "User-Agent": UA, "Accept": "application/json"})
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mint")
    m.add_argument("name")
    m.add_argument("--note", default="")
    r = sub.add_parser("revoke")
    r.add_argument("name")
    sub.add_parser("status")
    d = sub.add_parser("denials")
    d.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)

    if args.cmd in ("mint", "revoke"):
        key = _admin_key()
        if not key:
            print("no admin key: set DCHUB_ADMIN_KEY or ~/.dchub_admin_key", file=sys.stderr)
            return 2
    else:
        key = (os.environ.get("DCHUB_VIEWER_KEY") or "").strip() or _admin_key()
        if not key:
            print("no key: set DCHUB_VIEWER_KEY or DCHUB_ADMIN_KEY", file=sys.stderr)
            return 2

    if args.cmd == "mint":
        st, out = _call("POST", "/api/v1/admin/ops-gate/keys", key,
                        {"name": args.name, "note": args.note})
        if st != 201 or not out.get("key"):
            print("mint failed: HTTP %s %s" % (st, out.get("error", "")), file=sys.stderr)
            return 1
        print("minted viewer key for %s (shown once, below on stdout)" % args.name,
              file=sys.stderr)
        print(out["key"])
        return 0
    if args.cmd == "revoke":
        st, out = _call("POST", f"/api/v1/admin/ops-gate/keys/{args.name}/revoke", key)
        print(json.dumps(out), file=sys.stderr)
        return 0 if st == 200 else 1
    path = ("/api/v1/admin/ops-gate/status" if args.cmd == "status"
            else f"/api/v1/admin/ops-gate/denials?days={args.days}")
    st, out = _call("GET", path, key)
    print(json.dumps(out, indent=1))
    return 0 if st == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
