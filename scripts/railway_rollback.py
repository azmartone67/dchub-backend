#!/usr/bin/env python3
"""Roll production back via Railway, not via git.

Why this exists
---------------
`main` is protected (substance-gate, syntax-check, unit-tests required) and
`github-actions[bot]` is not an admin, so the old rollback path —
`git revert HEAD && git push origin main` — is rejected outright:

    remote: error: GH006: Protected branch update failed for refs/heads/main.
    remote: - Required status check "substance-gate" is expected.

A rollback that needs a green CI run deadlocks exactly when production is on
fire, because "production is broken" and "CI is red" are usually the same
event. So the rollback is a deploy-platform operation: Railway re-runs the
previous image directly. Branch protection cannot veto it and CI does not
gate it.

Reverting the git commit still has to happen, but it happens afterwards, as a
normal PR, at human pace. See docs/ROLLBACK-RUNBOOK.md.

Contract notes (verified against the live schema, 2026-07-28)
-------------------------------------------------------------
The published docs are wrong about the mutation's return type. They show:

    mutation { deploymentRollback(id: $id) { id status } }

The live schema rejects that:

    Field "deploymentRollback" must not have a selection since type
    "Boolean!" has no subfields.

`deploymentRollback` returns a bare `Boolean!`. Selecting subfields fails at
GraphQL validation time — i.e. it would fail during an outage, having never
been exercised. Keep the selection-less form below.

Usage
-----
    RAILWAY_TOKEN=... python3 scripts/railway_rollback.py --dry-run
    RAILWAY_TOKEN=... python3 scripts/railway_rollback.py --json

Exit codes: 0 rolled back (or dry-run found a target), 2 no eligible target,
3 API/auth failure, 4 rollback issued but the service never came back healthy.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time

import requests

API = os.environ.get("RAILWAY_API", "https://backboard.railway.com/graphql/v2")

# Stable Railway identifiers for the dchub production backend. These are
# identifiers, not secrets; only RAILWAY_TOKEN is sensitive.
PROJECT_ID = os.environ.get("RAILWAY_PROJECT_ID", "8b33570c-80fa-4869-8de6-dd62899a0eb2")
ENVIRONMENT_ID = os.environ.get("RAILWAY_ENVIRONMENT_ID", "95b195d6-38b6-4ff5-a70f-90609e79447b")
SERVICE_ID = os.environ.get("RAILWAY_SERVICE_ID", "f6198b88-799d-4b60-8cc8-069f3552fc99")

# Statuses that mean "this deployment is, or is becoming, the live one".
LIVE_STATUSES = {"SUCCESS", "DEPLOYING", "BUILDING", "QUEUED", "WAITING"}

# Anti-stacking: refuse to roll back onto a deployment that only just became
# live. More than one actor can react to the same incident (two worker
# sentinel processes, plus auto-rollback.yml as a backstop), and the second
# one to act would skip the just-restored image as "same commit as current"
# and roll back further than anyone intended.
MIN_CURRENT_AGE_S = int(os.environ.get("RAILWAY_ROLLBACK_MIN_AGE_S", "600"))

_DEPLOYMENTS_Q = """
query deployments($input: DeploymentListInput!, $first: Int) {
  deployments(input: $input, first: $first) {
    edges { node { id status createdAt canRollback meta } }
  }
}
"""

# No sub-selection: deploymentRollback returns Boolean!, not an object.
_ROLLBACK_M = """
mutation deploymentRollback($id: String!) {
  deploymentRollback(id: $id)
}
"""


class RailwayError(RuntimeError):
    pass


# ── auth: Railway's token types do NOT share a header ───────────────────────
# account / team token  ->  Authorization: Bearer <token>
# project token         ->  Project-Access-Token: <token>
#
# ★ Presenting a project token as a Bearer returns a bare "Not Authorized" with
# nothing pointing at the header. Measured 2026-07-28: three separate trips to
# the Railway dashboard were spent re-minting tokens against that error before
# anyone suspected the header. The token had been correct the whole time — it
# was being presented the wrong way.
#
# So try both rather than requiring the operator to know which type they hold.
# The style that works is cached for the process, so a rollback pays at most one
# extra request, once — and that only on the wrong first guess.
AUTH_STYLES = ("bearer", "project")
_auth_style: str | None = None


def _auth_headers(style: str, token: str) -> dict:
    base = {"User-Agent": "dchub-auto-rollback/1.0"}
    if style == "bearer":
        return {**base, "Authorization": f"Bearer {token}"}
    return {**base, "Project-Access-Token": token}


def _is_auth_error(msg: str) -> bool:
    low = msg.lower()
    return any(s in low for s in ("not authorized", "unauthorized", "authentication"))


def _post(query: str, variables: dict, token: str, style: str, timeout: int) -> dict:
    """One attempt with one auth style. Raises RailwayError on any failure."""
    try:
        resp = requests.post(
            API,
            json={"query": query, "variables": variables},
            headers=_auth_headers(style, token),
            timeout=timeout,
        )
    except Exception as exc:
        raise RailwayError(f"Railway API unreachable: {exc}") from exc

    # A 4xx/5xx still carries a GraphQL error body worth surfacing.
    try:
        payload = resp.json() or {}
    except ValueError:
        raise RailwayError(f"HTTP {resp.status_code} from Railway API (non-JSON body)")

    if payload.get("errors"):
        msgs = "; ".join(e.get("message", "?") for e in payload["errors"])
        raise RailwayError(msgs)
    return payload.get("data") or {}


def gql(query: str, variables: dict, token: str, timeout: int = 30) -> dict:
    """POST a GraphQL request and return `data`, raising on `errors`.

    Tries each auth style until one is accepted. Only an AUTHORIZATION failure
    falls through to the next style — a real error (bad query, unreachable API,
    missing deployment) is raised immediately rather than being retried and
    reported under a misleading "tried both headers" message.
    """
    global _auth_style
    order = ([_auth_style] + [s for s in AUTH_STYLES if s != _auth_style]) if _auth_style \
        else list(AUTH_STYLES)

    last: Exception | None = None
    for style in order:
        try:
            data = _post(query, variables, token, style, timeout)
        except RailwayError as exc:
            last = exc
            if _is_auth_error(str(exc)):
                continue
            raise
        if _auth_style != style:
            _auth_style = style
            print(f"Railway accepted the '{style}' auth header.", file=sys.stderr)
        return data

    raise RailwayError(
        f"{last} — rejected with every auth header tried ({', '.join(order)}). "
        "Account and team tokens use 'Authorization: Bearer'; project tokens use "
        "'Project-Access-Token'. Check the token is not expired and covers project "
        f"{PROJECT_ID}."
    )


def commit_sha(node: dict) -> str:
    """Best-effort commit SHA for a deployment (meta shape varies by source)."""
    meta = node.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            return ""
    for key in ("commitHash", "commitSHA", "commit", "sha"):
        val = meta.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def list_deployments(token: str, service_id: str = SERVICE_ID, first: int = 25) -> list[dict]:
    data = gql(
        _DEPLOYMENTS_Q,
        {
            "input": {
                "projectId": PROJECT_ID,
                "serviceId": service_id,
                "environmentId": ENVIRONMENT_ID,
            },
            "first": first,
        },
        token,
    )
    edges = ((data.get("deployments") or {}).get("edges")) or []
    return [e["node"] for e in edges if e.get("node")]


def _parse_ts(value) -> float:
    """Railway timestamps -> epoch seconds. 0.0 when unparseable."""
    if not value or not isinstance(value, str):
        return 0.0
    txt = value.strip().replace("Z", "+00:00")
    if " " in txt and "T" not in txt:
        txt = txt.replace(" ", "T", 1)
    try:
        dt = datetime.datetime.fromisoformat(txt)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def pick_rollback_target(
    deployments: list[dict],
    now: float | None = None,
    min_current_age_s: int = MIN_CURRENT_AGE_S,
) -> tuple[dict | None, dict | None, str]:
    """Choose what to roll back *from* and *to*.

    Pure function so it can be tested against real captured Railway data
    without a token. `deployments` must be newest-first, as the API returns.

    Returns (current, target, reason). `target` is None when no eligible
    rollback exists, and `reason` explains why.

    `min_current_age_s` is the anti-stacking guard. More than one actor can
    decide to roll back the same incident: the worker sentinel runs on two
    processes, and auto-rollback.yml is still armed as a backstop. Whoever
    acts second would see the freshly restored good image as "same commit as
    current", skip it, and roll back to something OLDER — turning one correct
    rollback into an over-rollback. So: if the live deployment changed within
    the last few minutes, someone already acted; do nothing. Pass 0 to
    override (the CLI exposes --force).
    """
    if not deployments:
        return None, None, "Railway returned no deployments for this service"

    current = next((d for d in deployments if d.get("status") in LIVE_STATUSES), None)
    if current is not None and min_current_age_s > 0:
        created = _parse_ts(current.get("createdAt"))
        age = (now if now is not None else time.time()) - created
        if created and age < min_current_age_s:
            return current, None, (
                f"live deployment is only {int(age)}s old (< {min_current_age_s}s) — "
                "something just deployed or already rolled back; refusing to stack "
                "a second rollback on top of it"
            )
    if current is None:
        return None, None, "no live deployment found (nothing is currently serving)"

    current_sha = commit_sha(current)
    seen_current = False
    for dep in deployments:
        if dep["id"] == current["id"]:
            seen_current = True
            continue
        if not seen_current:
            # Newer than the live deployment (e.g. a build that superseded it);
            # rolling "back" to it would roll forward.
            continue
        if dep.get("status") != "SUCCESS":
            continue
        if not dep.get("canRollback"):
            # Past the plan's image-retention window — image is gone.
            continue
        if current_sha and commit_sha(dep) == current_sha:
            # Same code as what is currently broken; rolling to it is a no-op.
            continue
        return current, dep, "ok"

    return current, None, (
        "no older SUCCESS deployment with canRollback=true and a different commit "
        "(image retention may have expired, or every retained build is the same commit)"
    )


def wait_until_live(token: str, target_id: str, service_id: str, timeout_s: int = 420) -> tuple[bool, str]:
    """Poll until the rolled-back image is the live deployment."""
    target_sha = ""
    deadline = time.time() + timeout_s
    last = "no status observed"
    while time.time() < deadline:
        try:
            deps = list_deployments(token, service_id=service_id)
        except RailwayError as exc:
            last = f"poll failed: {exc}"
            time.sleep(10)
            continue

        if not target_sha:
            for d in deps:
                if d["id"] == target_id:
                    target_sha = commit_sha(d)
                    break

        live = next((d for d in deps if d.get("status") == "SUCCESS"), None)
        if live:
            # A rollback creates a NEW deployment carrying the old image, so
            # match on commit, not on deployment id.
            if live["id"] == target_id or (target_sha and commit_sha(live) == target_sha):
                return True, f"live deployment {live['id'][:8]} @ {commit_sha(live)[:8] or '?'}"
            last = f"live is {live['id'][:8]} @ {commit_sha(live)[:8] or '?'} (waiting for {target_sha[:8] or target_id[:8]})"
        else:
            statuses = ", ".join(sorted({d.get("status", "?") for d in deps[:5]}))
            last = f"no SUCCESS deployment yet (recent statuses: {statuses})"
        time.sleep(10)
    return False, f"timed out after {timeout_s}s — {last}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Roll the Railway backend back to its last good deployment.")
    ap.add_argument("--dry-run", action="store_true", help="resolve the target and print it; change nothing")
    ap.add_argument("--json", action="store_true", help="emit a machine-readable summary on stdout")
    ap.add_argument("--service-id", default=SERVICE_ID)
    ap.add_argument("--wait-timeout", type=int, default=420)
    ap.add_argument("--no-wait", action="store_true", help="issue the rollback but do not poll for recovery")
    ap.add_argument("--force", action="store_true",
                    help=f"ignore the {MIN_CURRENT_AGE_S}s anti-stacking guard (a human is sure "
                         "no other actor has already rolled back)")
    args = ap.parse_args()

    out: dict = {"ok": False, "action": "none", "dry_run": args.dry_run}

    def emit(code: int) -> int:
        if args.json:
            print(json.dumps(out))
        return code

    token = os.environ.get("RAILWAY_TOKEN", "").strip()
    if not token:
        out["reason"] = "RAILWAY_TOKEN not set"
        print("::warning::RAILWAY_TOKEN not set — cannot roll back via Railway.", file=sys.stderr)
        return emit(3)

    try:
        deployments = list_deployments(token, service_id=args.service_id)
    except RailwayError as exc:
        out["reason"] = f"Railway API error: {exc}"
        print(f"::error::{out['reason']}", file=sys.stderr)
        return emit(3)

    current, target, reason = pick_rollback_target(
        deployments, min_current_age_s=0 if args.force else MIN_CURRENT_AGE_S
    )
    out["current"] = {"id": current["id"], "sha": commit_sha(current)} if current else None

    if target is None:
        out["reason"] = reason
        print(f"::error::No rollback target: {reason}", file=sys.stderr)
        return emit(2)

    out["target"] = {"id": target["id"], "sha": commit_sha(target), "created_at": target.get("createdAt")}
    print(
        f"current={current['id'][:8]} @ {commit_sha(current)[:8] or '?'} "
        f"-> target={target['id'][:8]} @ {commit_sha(target)[:8] or '?'} ({target.get('createdAt')})",
        file=sys.stderr,
    )

    if args.dry_run:
        out["ok"] = True
        out["action"] = "dry-run"
        return emit(0)

    try:
        gql(_ROLLBACK_M, {"id": target["id"]}, token)
    except RailwayError as exc:
        out["reason"] = f"deploymentRollback failed: {exc}"
        print(f"::error::{out['reason']}", file=sys.stderr)
        return emit(3)

    out["action"] = "rollback-issued"
    print(f"::notice::Rollback issued to deployment {target['id']}", file=sys.stderr)

    if args.no_wait:
        out["ok"] = True
        return emit(0)

    ok, detail = wait_until_live(token, target["id"], args.service_id, args.wait_timeout)
    out["ok"] = ok
    out["detail"] = detail
    if not ok:
        print(f"::error::Rollback issued but service did not come back: {detail}", file=sys.stderr)
        return emit(4)
    print(f"::notice::Rollback complete — {detail}", file=sys.stderr)
    return emit(0)


if __name__ == "__main__":
    sys.exit(main())
