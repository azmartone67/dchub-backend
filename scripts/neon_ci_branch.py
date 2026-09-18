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
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

API = "https://console.neon.tech/api/v2"

# The stamp that util/ephemeral_db_guard.py looks for. A test that DROPs tables
# refuses to run against any database lacking it, so prod (which will never have
# it) can never be the target of a write-heavy test.
SENTINEL_TABLE = "_ci_ephemeral_branch"


def _req(method: str, path: str, key: str, body: dict | None = None) -> dict:
    """`requests`, not urllib — scripts/regression_lint.py bans
    urllib.request.urlopen repo-wide (rule: urllib-request-on-railway)."""
    r = requests.request(
        method, f"{API}{path}", json=body, timeout=120,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    if r.status_code >= 400:
        # NEVER echo the request body — it is not secret today, but this error
        # path is the one most likely to be pasted into a public issue.
        raise SystemExit(f"neon api {method} {path} -> {r.status_code}: {r.text[:600]}")
    # DELETE answers with an empty body; .json() would raise on it.
    return r.json() if r.text.strip() else {}


def _preflight(key: str, project: str) -> None:
    """Explain a misconfigured NEON_PROJECT_ID before it becomes a bare 404.

    Neon answers `/projects/<malformed>/branches` with "this route does not
    exist", which reads like the API moved rather than like a bad id — the
    first run of this workflow lost time to exactly that.

    ★ PRINTS NO IDENTIFIERS. The repo is public and so are its CI logs, so
      this reports SHAPE and COUNTS only. GitHub masks the configured id to
      ``***`` anyway, which would make a listing unreadable in the one case
      where it is correct. Run `neon_ci_branch.py doctor` locally to see the
      actual ids.
    """
    # Named first because it is the mistake people actually make: the Neon
    # console shows a connection string far more prominently than the project
    # id, and "152 chars, 7 path characters" does not tell you which of the two
    # you grabbed. Reports the SHAPE only — the value itself is never printed.
    if project.lower().startswith(("postgres://", "postgresql://")):
        raise SystemExit(
            "NEON_PROJECT_ID is a CONNECTION STRING, not a project id — the "
            "Neon console shows that far more prominently. The project id is "
            "the bare slug under Project settings -> General (e.g. "
            "'winter-frost-12345678').\n"
            "  ! That value is a LIVE DATABASE CREDENTIAL. It is stored as a "
            "secret so it is not exposed, but overwrite it now that it is in "
            "the wrong place, and check nothing else received the same paste.")

    # The second wrong value people paste, and the one a shape check cannot
    # catch: a branch id is a well-formed 28-character slug that simply names
    # the wrong KIND of object. Neon prefixes branch ids with "br-"; project
    # ids carry no prefix.
    if project.startswith("br-"):
        raise SystemExit(
            f"NEON_PROJECT_ID is a BRANCH id ({project}), not a project id — "
            "branch ids start with 'br-'. The project id is the segment after "
            "/projects/ in the console URL:\n"
            "  console.neon.tech/app/projects/<THIS-IS-THE-PROJECT-ID>\n"
            "  (a branch id IS what --parent-id takes, if you meant that)")

    bad = [c for c in project if c.isspace() or c in "/?#:@"]
    if bad:
        raise SystemExit(
            f"NEON_PROJECT_ID is malformed: {len(project)} chars containing "
            f"{len(bad)} whitespace/path character(s). It must be the BARE "
            "project id (e.g. 'winter-frost-12345678') — not a console URL, "
            "and not padded by the paste. Re-set it with:\n"
            "  printf %s 'winter-frost-12345678' | gh secret set NEON_PROJECT_ID "
            "--repo azmartone67/dchub-backend")

    # ★ Ask about THIS project, not about every project.
    #
    #   The first version listed /projects and checked for membership. That is
    #   a strictly broader question, it needs more privilege to answer, and an
    #   ORGANISATION-scoped key cannot answer it at all — Neon rejects the
    #   listing with `400 org_id is required`. So the diagnostic became the
    #   thing that failed, in front of the configuration it was meant to
    #   diagnose. A direct GET works for personal and org keys alike.
    try:
        _req("GET", f"/projects/{project}", key)
    except SystemExit as exc:
        detail = str(exc)
        if "-> 404" in detail:
            raise SystemExit(
                "NEON_PROJECT_ID is well-formed but this API key cannot reach "
                "it — wrong id, or a key belonging to a different account or "
                "organisation. Run `python3 scripts/neon_ci_branch.py doctor` "
                "LOCALLY to see what the key can reach.") from exc
        if "-> 403" in detail:
            raise SystemExit(
                "this API key is not authorised for that project — it is "
                "probably scoped to a different organisation.") from exc
        raise


def cmd_doctor(a: argparse.Namespace) -> None:
    """Local-only: print the project ids this key can reach. Never run in CI."""
    path = f"/projects?org_id={a.org_id}" if a.org_id else "/projects"
    try:
        projects = _req("GET", path, a.api_key).get("projects", [])
    except SystemExit as exc:
        if "org_id is required" in str(exc):
            raise SystemExit(
                "this is an ORGANISATION-scoped API key, so listing needs the "
                "org id: re-run with --org-id <id>. Find it on the Neon "
                "organisation settings page. (CI does not need it — the branch "
                "calls address the project directly.)") from exc
        raise
    print(f"{len(projects)} project(s) visible to this key:")
    for p in projects:
        mark = "  <-- NEON_PROJECT_ID" if p.get("id") == a.project_id else ""
        print(f"  {p['id']}  {p.get('name','')}  {p.get('region_id','')}{mark}")
    if not any(p.get("id") == a.project_id for p in projects):
        print("\n! the configured NEON_PROJECT_ID matches none of the above")


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
    _preflight(key, project)
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

    database, role = _resolve_db_and_role(key, project, branch_id, database, role)

    pw = _req("GET",
              f"/projects/{project}/branches/{branch_id}/roles/{role}/reveal_password",
              key).get("password")
    if not pw:
        raise SystemExit(f"could not reveal a password for role {role!r}")
    return f"postgresql://{role}:{pw}@{host}/{database}?sslmode=require"


def _resolve_db_and_role(key: str, project: str, branch_id: str,
                         db_hint: str, role_hint: str) -> tuple[str, str]:
    """Ask the branch what its database and owning role are actually called.

    `neondb` / `neondb_owner` are Neon's defaults for a project created through
    the console, and dchub's are NOT guaranteed to be those — this project
    predates that convention and was migrated between two clouds. A guess here
    fails as an opaque auth error at connect time, several steps downstream of
    the wrong assumption.

    The pairing that matters is database -> ITS OWNER, not two independent
    lookups: connecting as a role that does not own the database is a
    permissions failure that looks exactly like a wrong password.

    Names are identifiers, not credentials, so they are safe to name in an
    error. The PASSWORD is fetched separately and never printed.
    """
    dbs = _req("GET", f"/projects/{project}/branches/{branch_id}/databases",
               key).get("databases", [])
    if not dbs:
        raise SystemExit(f"branch {branch_id} reports no databases")

    chosen = next((d for d in dbs if d.get("name") == db_hint), None)
    if chosen is None:
        if db_hint not in ("", "neondb"):
            # An explicitly requested database that does not exist is an error,
            # not something to silently substitute.
            raise SystemExit(
                f"database {db_hint!r} is not on this branch. Available: "
                + ", ".join(sorted(d.get("name", "?") for d in dbs)))
        chosen = dbs[0]

    database = chosen["name"]
    owner = chosen.get("owner_name") or ""
    # An explicit --role wins; otherwise take the database's own owner.
    role = role_hint if role_hint not in ("", "neondb_owner") else owner
    if not role:
        raise SystemExit(f"database {database!r} reports no owner_name")
    return database, role


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

    doc = sub.add_parser("doctor")
    doc.add_argument("--org-id", default=os.environ.get("NEON_ORG_ID", ""))
    doc.set_defaults(fn=cmd_doctor)

    w = sub.add_parser("sweep")
    w.add_argument("--prefix", default="ci-")
    w.add_argument("--ttl-hours", type=int, default=6)
    w.set_defaults(fn=cmd_sweep)

    a = p.parse_args()
    # `gh secret set` keeps whatever was pasted, trailing newline included, and
    # a stray character lands in the URL PATH where it reads as a routing bug.
    a.org_id = (getattr(a, "org_id", "") or "").strip()
    a.api_key = (a.api_key or "").strip()
    a.project_id = (a.project_id or "").strip()
    if not a.api_key or not a.project_id:
        raise SystemExit("NEON_API_KEY and NEON_PROJECT_ID are required")
    a.fn(a)


if __name__ == "__main__":
    main()
