#!/usr/bin/env python3
"""Create / stamp / destroy an ephemeral Neon branch for a CI run.

WHY THIS EXISTS
---------------
`db-parity` runs 19 SQL lanes against a `postgres:16` service container. That
container is EMPTY — no postgis, no pgvector, no h3, no schema — so every lane
hand-rolls its own setup and carries its own `*_DSN` env name. 18 names for one
database, because the database could not be prod-shaped.

A Neon schema-only branch IS prod-shaped: prod's schema and extensions, zero
rows. That is the one thing a service container cannot be.

SCHEMA-ONLY BRANCHES ARE ROOT BRANCHES
--------------------------------------
`init_source: "schema-only"` copies the parent's SCHEMA and then detaches — the
result is an independent ROOT branch with no parent (reset-from-parent is not
supported on it, and it cannot itself be a parent). Two consequences:

  * There is no long-lived "ci-template" to cut from and keep fresh. Every run
    cuts its own branch straight from the production branch, so the schema is
    never stale by construction.
  * Root branches are CAPPED PER PROJECT (3 Free / 5 Launch / 25 Scale). A
    leaked branch is not untidy, it is a a future CI outage. Hence `expires_at`
    below AND `destroy` in an `if: always()` step AND the sweeper.

NO DATA LEAVES PROD. The branch carries schema only, which matters because
dchub-backend is a PUBLIC repo: a data branch would put real `users`, `deals`,
`api_keys` and `enterprise_inquiries` rows inside a CI job.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://console.neon.tech/api/v2"

# The stamp that util/ephemeral_db_guard.py looks for. A test that DROPs tables
# refuses to run against any database lacking it, so prod (which will never have
# it) can never be the target of a write-heavy test.
SENTINEL_TABLE = "_ci_ephemeral_branch"


def _req(method: str, path: str, key: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            raw = resp.read().decode() or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:600]
        # NEVER echo the request body — it is not secret today, but this error
        # path is the one most likely to be pasted into a public issue.
        raise SystemExit(f"neon api {method} {path} -> {e.code}: {detail}")


def _default_branch_id(key: str, project: str) -> str:
    """The production branch, resolved from the API rather than hardcoded.

    Pinning a branch id in the workflow would make this silently cut from the
    wrong branch the day prod is renamed or restored onto a new one.
    """
    branches = _req("GET", f"/projects/{project}/branches", key).get("branches", [])
    for b in branches:
        if b.get("default") or b.get("primary"):
            return b["id"]
    raise SystemExit("no default branch found — cannot resolve the parent to cut from")


def cmd_create(a: argparse.Namespace) -> None:
    key, project = a.api_key, a.project_id
    parent = a.parent_id or _default_branch_id(key, project)

    # expires_at is the SELF-HEALING backstop for the root-branch cap. `destroy`
    # in an `if: always()` step covers the normal path; a cancelled run, a
    # runner that dies mid-job, or a force-pushed PR skips that step entirely.
    # Without expiry those leak until the cap is hit and EVERY later PR fails to
    # get a database — an outage whose cause is hours upstream of its symptom.
    expires = datetime.now(timezone.utc) + timedelta(hours=a.ttl_hours)

    body = {
        "branch": {
            "name": a.name,
            "parent_id": parent,
            "init_source": "schema-only",   # schema + extensions, ZERO rows
            "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "endpoints": [{
            "type": "read_write",
            # CI does not need burst headroom; it needs to not cost anything.
            # Pinning min==max removes the autoscaler's ramp entirely.
            "autoscaling_limit_min_cu": 0.25,
            "autoscaling_limit_max_cu": 0.25,
            # Suspend hard the moment the job stops querying. CU-hours are the
            # whole cost story for per-PR branches.
            "suspend_timeout_seconds": 60,
        }],
    }
    out = _req("POST", f"/projects/{project}/branches", key, body)
    branch_id = out["branch"]["id"]

    dsn = _connection_uri(out, key, project, branch_id, a.database, a.role)
    _emit(branch_id=branch_id, dsn=dsn)


def _connection_uri(out: dict, key: str, project: str, branch_id: str,
                    database: str, role: str) -> str:
    """Prefer the URI the create call returns; rebuild it when it does not.

    The API omits `connection_uris` when the parent carries MORE THAN ONE role
    or database — it cannot guess which pair you meant. dchub's prod branch is
    exactly that shape, so the rebuild path is the one that actually runs; the
    happy path is kept because it is correct when it fires and costs one `if`.
    """
    uris = out.get("connection_uris") or []
    if uris and uris[0].get("connection_uri"):
        return uris[0]["connection_uri"]

    eps = out.get("endpoints") or []
    if not eps:
        raise SystemExit("branch created without an endpoint — nothing to connect to")
    host = eps[0]["host"]

    pw = _req("GET",
              f"/projects/{project}/branches/{branch_id}/roles/{role}/reveal_password",
              key).get("password")
    if not pw:
        raise SystemExit(f"could not reveal a password for role {role!r}")
    return f"postgresql://{role}:{pw}@{host}/{database}?sslmode=require"


def _emit(*, branch_id: str, dsn: str) -> None:
    """Hand the DSN back to the job with the mask applied FIRST.

    Order matters and is not cosmetic: a value printed before `::add-mask::`
    reaches the runner is in the public log forever. This repo is PUBLIC.
    """
    print(f"::add-mask::{dsn}")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as fh:
            fh.write(f"branch_id={branch_id}\n")
            fh.write(f"dsn={dsn}\n")
    else:
        print(dsn)
    print(f"branch {branch_id} created (schema-only, expires per ttl)", file=sys.stderr)


def cmd_stamp(a: argparse.Namespace) -> None:
    """Write the sentinel that marks this database as safe to DROP tables in."""
    import psycopg2
    conn = psycopg2.connect(a.dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {SENTINEL_TABLE} ("
            "  branch_id text,"
            "  stamped_at timestamptz NOT NULL DEFAULT now())")
        cur.execute(f"INSERT INTO {SENTINEL_TABLE} (branch_id) VALUES (%s)",
                    (a.branch_id,))
    conn.close()
    print(f"stamped {SENTINEL_TABLE} on {a.branch_id}", file=sys.stderr)


def cmd_destroy(a: argparse.Namespace) -> None:
    # Deleting an already-deleted branch must not fail the job — `destroy` runs
    # under `if: always()`, so it legitimately races the expiry backstop.
    try:
        _req("DELETE", f"/projects/{a.project_id}/branches/{a.branch_id}", a.api_key)
        print(f"deleted branch {a.branch_id}", file=sys.stderr)
    except SystemExit as e:
        if "404" in str(e):
            print(f"branch {a.branch_id} already gone", file=sys.stderr)
            return
        raise


def cmd_sweep(a: argparse.Namespace) -> None:
    """Delete leaked CI branches. Belt to expires_at's braces.

    Matches on the `ci-` name prefix and NEVER on age alone: an age-only sweep
    would happily delete the production branch of a project that has been quiet.
    """
    branches = _req("GET", f"/projects/{a.project_id}/branches",
                    a.api_key).get("branches", [])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=a.ttl_hours)
    killed = 0
    for b in branches:
        name = b.get("name", "")
        if not name.startswith(a.prefix):
            continue
        if b.get("default") or b.get("primary"):
            # Belt: a branch named ci-* that is somehow the default is a
            # configuration accident, not a sweep target.
            print(f"REFUSING to sweep default branch {name}", file=sys.stderr)
            continue
        created = b.get("created_at", "")
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < cutoff:
            _req("DELETE", f"/projects/{a.project_id}/branches/{b['id']}", a.api_key)
            print(f"swept {name} ({b['id']}, created {created})", file=sys.stderr)
            killed += 1
    print(f"swept {killed} leaked branch(es)", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api-key", default=os.environ.get("NEON_API_KEY", ""))
    p.add_argument("--project-id", default=os.environ.get("NEON_PROJECT_ID", ""))
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create")
    c.add_argument("--name", required=True)
    c.add_argument("--parent-id", default=os.environ.get("NEON_PARENT_BRANCH_ID", ""))
    c.add_argument("--database", default=os.environ.get("NEON_CI_DATABASE", "neondb"))
    c.add_argument("--role", default=os.environ.get("NEON_CI_ROLE", "neondb_owner"))
    c.add_argument("--ttl-hours", type=int, default=3)
    c.set_defaults(fn=cmd_create)

    s = sub.add_parser("stamp")
    s.add_argument("--dsn", required=True)
    s.add_argument("--branch-id", required=True)
    s.set_defaults(fn=cmd_stamp)

    d = sub.add_parser("destroy")
    d.add_argument("--branch-id", required=True)
    d.set_defaults(fn=cmd_destroy)

    w = sub.add_parser("sweep")
    w.add_argument("--prefix", default="ci-")
    w.add_argument("--ttl-hours", type=int, default=6)
    w.set_defaults(fn=cmd_sweep)

    a = p.parse_args()
    if not a.api_key or not a.project_id:
        raise SystemExit("NEON_API_KEY and NEON_PROJECT_ID are required")
    a.fn(a)


if __name__ == "__main__":
    main()
