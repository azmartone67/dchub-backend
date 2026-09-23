#!/usr/bin/env python3
"""Drive the weekly OSM refresh (dchub-osm-refresh.yml) and FAIL on a failed loader.

★ WHY THIS EXISTS. The workflow used to fire four loaders with
`curl ... || echo "(non-200)"`, sleep 480s between them, and print
/api/admin/loader-status — a process-local dict that another replica answered
with `{"loaders": {}}`. Nothing could fail it: `load_pipelines` had written into
a table that does not exist on every run, and the job was green.

Now each POST returns the id of a durable row in osm_load_runs (written by the
loader thread itself), and this script polls THAT row until it reaches a final
status. The run is red unless every loader ends success / no_new_data.

A row that stops heartbeating is `stalled` — its thread died, usually because a
deploy replaced the container mid-run. It is re-fired ONCE; a second stall (or
any error, timeout, or refused start) fails the job.

Env: RAILWAY (origin base URL), INTERNAL_KEY (X-Internal-Key).
"""
import json
import os
import sys
import time

import requests

LOADERS = [
    ("load-osm-substations-live", "osm_substations"),
    ("load-osm-power-plants-live", "osm_power_plants"),
    ("load-osm-transmission-live", "osm_transmission_lines"),
    ("load-osm-pipelines-live", "osm_pipelines"),
]
OK = {"success", "no_new_data"}
UA = "dchub-osm-refresh/1.0 (+https://dchub.cloud)"
POLL_S = int(os.environ.get("OSM_DRIVE_POLL_S", "30"))
# Loader budget is 2700s (osm_overpass_loader.LOADER_BUDGET_S) + margin.
DEADLINE_S = int(os.environ.get("OSM_DRIVE_DEADLINE_S", "3300"))
# ★ 2026-09-23: was 2 (one re-fire). Bots merge to main every 10-15 min and
# each merge redeploys Railway, killing the loader thread; a re-fire now
# RESUMES (the dead run's finished states are carried over), so each attempt
# makes progress and a sweep finishes across several of them. Still bounded:
# a loader that stalls this many times in a row fails the job.
MAX_ATTEMPTS = int(os.environ.get("OSM_DRIVE_MAX_ATTEMPTS", "10"))


def _call(method, path):
    base = os.environ.get("RAILWAY", "https://dchub-backend-production.up.railway.app")
    try:
        r = requests.request(
            method, base.rstrip("/") + path, timeout=60,
            json=({} if method == "POST" else None),
            headers={"X-Internal-Key": os.environ.get("INTERNAL_KEY", ""),
                     "User-Agent": UA})
    except Exception as e:  # noqa: BLE001 — a transport failure is a result
        return 0, {"error": f"{type(e).__name__}: {e}"}
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"body": r.text[:300]}


def fire(endpoint):
    """(run_id or None, reason). An 'already running' refusal that names a run
    id is adopted — that run is the one to wait for."""
    code, body = _call("POST", f"/api/admin/{endpoint}")
    rid = body.get("run_id")
    if code == 200 and (body.get("started") or body.get("reason") == "already running") and rid:
        return rid, "started" if body.get("started") else "adopted a run already in progress"
    return None, f"HTTP {code}: {json.dumps(body)[:300]}"


def wait(run_id, sleep=time.sleep, now=time.monotonic):
    """Poll until the run row is final or stalled. Returns the last row seen."""
    t0, last = now(), {"state": "unreadable", "note": "no row read yet"}
    while now() - t0 < DEADLINE_S:
        code, body = _call("GET", f"/api/admin/osm-load-runs?id={run_id}")
        runs = body.get("runs") if code == 200 else None
        if runs:
            last = runs[0]
            if last.get("state") != "running":
                return last
        sleep(POLL_S)
    last = dict(last)
    last["state"] = "timeout"
    return last


def verdict(results):
    """(exit_code, lines). results = [(loader, row_or_None, reason)]."""
    lines, bad = [], []
    for loader, row, reason in results:
        state = (row or {}).get("state") or "not_started"
        ok = state in OK
        if not ok:
            bad.append(loader)
        lines.append(f"{'OK ' if ok else 'BAD'} {loader:<24} {state:<12} "
                     f"inserted={(row or {}).get('inserted')} "
                     f"states={(row or {}).get('states_done')}/{(row or {}).get('states_total')} "
                     f"{((row or {}).get('note') or reason or '')[:160]}")
    return (1 if bad else 0), lines


def drive_one(endpoint, fire_fn=None, wait_fn=None):
    """Fire one loader and follow it to a final row, re-firing (= resuming) a
    stalled run up to MAX_ATTEMPTS times. Returns (row, reason, attempts)."""
    fire_fn, wait_fn = fire_fn or fire, wait_fn or wait
    row, reason, attempt = None, "", 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        rid, reason = fire_fn(endpoint)
        print(f"→ {endpoint} (attempt {attempt}): run_id={rid} {reason}", flush=True)
        if rid is None:
            break
        row = wait_fn(rid)
        print(f"  run {rid}: {row.get('state')} — {(row.get('note') or '')[:200]}", flush=True)
        if row.get("state") != "stalled":
            break
    return row, reason, attempt


def main():
    if not os.environ.get("INTERNAL_KEY"):
        print("::error::DCHUB_INTERNAL_KEY secret not set")
        return 1
    results = []
    for endpoint, loader in LOADERS:
        row, reason, _n = drive_one(endpoint)
        results.append((loader, row, reason))
    code, lines = verdict(results)
    print("\n".join(lines))
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a") as f:
            f.write("### OSM refresh\n```\n" + "\n".join(lines) + "\n```\n")
    for (loader, row, reason) in results:
        state = (row or {}).get("state") or "not_started"
        if state not in OK:
            print(f"::error::OSM loader {loader} ended {state}: "
                  f"{((row or {}).get('note') or reason or '')[:240]}")
    return code


if __name__ == "__main__":
    sys.exit(main())
