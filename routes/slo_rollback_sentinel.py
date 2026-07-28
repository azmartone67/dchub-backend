"""slo_rollback_sentinel — the SLO probe, moved off GitHub cron onto the worker.

Why it moved
------------
`auto-rollback.yml` declares `cron: '4-59/5 * * * *'`, but GitHub throttles
scheduled workflows hard in a repo this busy. Measured over 100 scheduled runs
(2026-07-25 → 07-28): **median gap 29.7 min, p90 72.6, max 84.1, never under
22.** The safety net advertised 5-minute detection and delivered ~30–85.

This runs on `dchub-worker`, whose scheduler is ours and actually fires every
5 minutes. Three further properties fall out of running it here:

  * **No HTTP hop.** The verdict comes from `slo_error_budget.compute_budget()`
    in-process. The GitHub job curled `https://dchub.cloud/...`, so a broken
    edge, a broken web replica, or a slow CF route all corrupted the reading of
    whether the web replica was broken.
  * **Separate failure domain.** `dchub-worker` is a different Railway service
    from `dchub-backend`. The web service can be 5xxing while the sentinel that
    grades it keeps running.
  * **Shared verdict.** Same function the endpoint serves, so the detector and
    the endpoint cannot drift on what `hard_burn` means.

Known blind spot (pre-existing, unchanged by the move): the verdict reads
`brain_http_errors`, which the WEB role writes after each response. A *total*
outage writes no rows at all, which grades as `within_budget`. Total-outage
detection belongs to the uptime probes and the failover chain, not here.

Actuation is DISARMED by default
--------------------------------
Detection always runs. Rolling production back does not, unless BOTH:

    SLO_SENTINEL_ROLLBACK=1      # explicit arm
    RAILWAY_TOKEN=<project token>

Disarmed, it still detects, logs, and files a brain finding — the same posture
`auto-rollback.yml` has without a token. This matches how the rest of the
autonomy stack ships: detection automatic, actuation human-gated until someone
decides otherwise.
"""

import datetime as _dt
import importlib.util
import logging
import os
import pathlib
import threading
import time

from flask import Blueprint, jsonify

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

sentinel_bp = Blueprint("slo_rollback_sentinel", __name__)
log = logging.getLogger(__name__)

# Sampling: keep the semantics auto-rollback.yml used — a rolling window of the
# last 5 observations, act on 3+ hard_burn. One reading per minute, so a burn is
# confirmed in ~3 minutes instead of GitHub cron's ~30–85.
INTERVAL_S = int(os.environ.get("SLO_SENTINEL_INTERVAL_S", "60"))
WINDOW_N = 5
TRIGGER_N = 3

# After acting, stay quiet. A rollback takes minutes to build and warm up, and
# brain_http_errors still holds the pre-rollback 5xx for its 5-minute window —
# without this the sentinel would re-trigger on the damage it already fixed.
COOLDOWN_S = int(os.environ.get("SLO_SENTINEL_COOLDOWN_S", "1800"))

_STATE = {
    "samples": [],          # rolling verdicts, newest last
    "last_verdict": None,
    "last_checked": None,
    "last_action": None,
    "last_action_at": 0.0,
    "actions": 0,
    "errors": 0,
}
_LOCK = threading.Lock()


def _armed():
    """Rollback actuation requires an explicit arm AND a token."""
    flag = (os.environ.get("SLO_SENTINEL_ROLLBACK", "") or "").strip().lower()
    return flag in ("1", "true", "yes") and bool(os.environ.get("RAILWAY_TOKEN", "").strip())


def _enabled():
    return (os.environ.get("SLO_SENTINEL_ENABLED", "1") or "").strip().lower() not in ("0", "false", "no")


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


def _load_rollback_module():
    """Import scripts/railway_rollback.py by path.

    Deliberately reused rather than reimplemented: that module is the one with
    tests over target selection and the GraphQL contract (deploymentRollback
    returns Boolean!, which the published Railway docs get wrong). A second
    copy of the rollback logic here would be the drift class this file's
    shared-verdict design exists to avoid.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    path = root / "scripts" / "railway_rollback.py"
    if not path.exists():
        raise RuntimeError(f"railway_rollback.py not found at {path}")
    spec = importlib.util.spec_from_file_location("railway_rollback", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _file_finding(verdict, pattern, n5xx, action):
    """Record what happened in brain_findings (best-effort, no DDL).

    Uses the canonical schema (issue, url, count, detail, detector, status) —
    see routes/brain_layer14_slo_burn.py for the same shape.
    """
    if not (_pg and _dsn()):
        return
    try:
        with _pg.connect(_dsn(), connect_timeout=4) as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO brain_findings
                    (issue, url, count, detail, detector, status)
                VALUES (%s, %s, 1, %s, %s, 'open')
                """,
                (
                    f"slo_sentinel_{action}",
                    pattern or "unknown",
                    f"SLO sentinel: {TRIGGER_N}+/{WINDOW_N} samples {verdict} "
                    f"(worst path {pattern} = {n5xx} 5xx/5min). Action: {action}.",
                    "slo_rollback_sentinel",
                ),
            )
            c.commit()
    except Exception as e:
        log.warning("[slo-sentinel] finding insert failed: %s", e)


def _rollback():
    """Roll the backend back to its last good Railway deployment.

    Returns (action, detail).
    """
    try:
        rb = _load_rollback_module()
    except Exception as e:
        return "rollback-unavailable", f"could not load railway_rollback: {e}"

    token = os.environ.get("RAILWAY_TOKEN", "").strip()
    try:
        deployments = rb.list_deployments(token)
    except Exception as e:
        return "rollback-failed", f"Railway API error: {e}"

    current, target, reason = rb.pick_rollback_target(deployments)
    if target is None:
        return "no-target", reason

    try:
        rb.gql(rb._ROLLBACK_M, {"id": target["id"]}, token)
    except Exception as e:
        return "rollback-failed", f"deploymentRollback failed: {e}"

    sha = rb.commit_sha(target)[:8] or target["id"][:8]
    return "rolled-back", f"rolled back to {sha} (deployment {target['id'][:8]})"


def check_once():
    """One sample. Returns the verdict string, or None when it could not read.

    Exposed for tests and for the status endpoint's `?force=1`.
    """
    from routes.slo_error_budget import compute_budget

    payload, _status = compute_budget()
    verdict = payload.get("verdict")
    if verdict is None:
        with _LOCK:
            _STATE["errors"] += 1
            _STATE["last_checked"] = _dt.datetime.utcnow().isoformat() + "Z"
        log.warning("[slo-sentinel] could not read budget: %s", payload.get("reason"))
        return None

    top = (payload.get("top_5xx_paths") or [{}])[0]
    with _LOCK:
        _STATE["samples"].append(verdict)
        del _STATE["samples"][:-WINDOW_N]
        _STATE["last_verdict"] = verdict
        _STATE["last_checked"] = _dt.datetime.utcnow().isoformat() + "Z"
        hard = _STATE["samples"].count("hard_burn")
        cooling = (time.time() - _STATE["last_action_at"]) < COOLDOWN_S

    if hard < TRIGGER_N:
        return verdict

    if cooling:
        log.warning("[slo-sentinel] %d/%d hard_burn but within cooldown — not acting",
                    hard, WINDOW_N)
        return verdict

    pattern = top.get("pattern")
    n5xx = top.get("n5xx") or 0

    if not _armed():
        action, detail = "detected-disarmed", (
            "SLO_SENTINEL_ROLLBACK is not set to 1, or RAILWAY_TOKEN is missing — "
            "detected but did not roll back"
        )
        log.error("[slo-sentinel] HARD BURN %d/%d on %s (%s 5xx) — DISARMED, no rollback",
                  hard, WINDOW_N, pattern, n5xx)
    else:
        action, detail = _rollback()
        log.error("[slo-sentinel] HARD BURN %d/%d on %s (%s 5xx) — action=%s %s",
                  hard, WINDOW_N, pattern, n5xx, action, detail)

    with _LOCK:
        _STATE["last_action"] = {
            "action": action,
            "detail": detail,
            "verdict": "hard_burn",
            "pattern": pattern,
            "n5xx": n5xx,
            "at": _dt.datetime.utcnow().isoformat() + "Z",
        }
        _STATE["last_action_at"] = time.time()
        _STATE["actions"] += 1
        # Clear the window so the next decision needs fresh evidence rather
        # than re-firing on the samples that already triggered this one.
        _STATE["samples"] = []

    _file_finding("hard_burn", pattern, n5xx, action)
    return verdict


def _loop():
    # The DB pool and blueprints are still coming up at boot; take the first
    # sample one interval in rather than logging a spurious read failure on
    # every deploy (same reason brain_layer14_slo_burn sleeps first).
    time.sleep(INTERVAL_S)
    while True:
        try:
            check_once()
        except Exception as e:
            with _LOCK:
                _STATE["errors"] += 1
            log.warning("[slo-sentinel] iteration failed: %s", e)
        time.sleep(INTERVAL_S)


def start_scheduler():
    """Start the sentinel thread. Caller is responsible for role-gating."""
    if getattr(start_scheduler, "_started", False):
        return False
    if not _enabled():
        log.info("[slo-sentinel] disabled via SLO_SENTINEL_ENABLED")
        return False
    start_scheduler._started = True
    threading.Thread(target=_loop, daemon=True, name="slo-rollback-sentinel").start()
    log.info("[slo-sentinel] started: every %ss, %d/%d hard_burn triggers, armed=%s",
             INTERVAL_S, TRIGGER_N, WINDOW_N, _armed())
    return True


@sentinel_bp.route("/api/v1/slo/sentinel/status", methods=["GET"])
def status():
    """What the sentinel has seen and done. Read-only."""
    with _LOCK:
        snap = {
            "samples": list(_STATE["samples"]),
            "last_verdict": _STATE["last_verdict"],
            "last_checked": _STATE["last_checked"],
            "last_action": _STATE["last_action"],
            "actions": _STATE["actions"],
            "errors": _STATE["errors"],
            "cooldown_remaining_s": max(
                0, int(COOLDOWN_S - (time.time() - _STATE["last_action_at"]))
            ) if _STATE["last_action_at"] else 0,
        }
    snap.update({
        "running": bool(getattr(start_scheduler, "_started", False)),
        "enabled": _enabled(),
        "armed": _armed(),
        "interval_s": INTERVAL_S,
        "trigger": f"{TRIGGER_N}/{WINDOW_N} hard_burn",
        "cooldown_s": COOLDOWN_S,
        "role": os.environ.get("DCHUB_ROLE", "all"),
        "note": (
            "armed=false means a confirmed hard_burn is detected and recorded "
            "but production is NOT rolled back. Set SLO_SENTINEL_ROLLBACK=1 and "
            "RAILWAY_TOKEN on dchub-worker to arm."
        ),
    })
    # The blueprint is registered in every role so this is reachable from the
    # public domain, but only the worker runs the loop. Served from a web
    # replica this reports the WEB process's (empty) state — say so, rather
    # than letting a reader mistake it for "the sentinel is doing nothing".
    if not snap["running"]:
        snap["serving_process_runs_loop"] = False
        snap["where"] = (
            "This reply came from a process that does not run the loop "
            f"(DCHUB_ROLE={snap['role']}). The sentinel runs on dchub-worker; "
            "read dchub-worker logs for its real state."
        )
    else:
        snap["serving_process_runs_loop"] = True
    return jsonify(snap), 200
