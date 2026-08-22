#!/usr/bin/env python3
"""Kill-switch behavioural probe — "config SET" must equal "config IN EFFECT".

★ WHY (2026-08-22): BRAIN_REVIEW_LANE_ENABLED=0 was set in Railway while the
lane kept opening brain/review-* PRs for four more minutes, because the
env-change redeploy FAILED and the old container kept serving the old env.
A switch is only real when the behaviour it forbids has stopped — and nothing
checked that. This probe reads the OWNER'S INTENT from the registry below
(repo-side: no Railway credentials, the registry says what the owner MEANS),
asks production and GitHub what actually happened in the last WINDOW_H
hours, and beats the public dead-man ledger with the verdict:

    success   every OBSERVABLE switch agrees with its registry value
    error     at least one switch is set but not in effect (or vice-versa)

Per switch, a probe that cannot observe (endpoint 404 — e.g. a surface a
parallel PR has not shipped yet — or an API that is unreachable) reports
`unknown`: never red, always named in the note. A run in which NOTHING could
be observed beats nothing and exits 2 — a blind probe must be loud, not
green; the feed then ages out on the board like any dead loop would.

Runs off-worker on GitHub Actions (.github/workflows/kill-switch-probe.yml,
every 2h). This module is the ONE writer of feed `kill-switch-probe`
(cadence 3h). It is deliberately NOT in tools/deadman/watch.py WORKFLOWS —
a conclusion watcher would overwrite this computed status with a bare
success (tests/test_alarm_reachability.py fences exactly that).

Env: DCHUB_ADMIN_KEY (admin reads + the beat), GH_TOKEN + GH_REPO (gh pr
list), API_BASE (default https://dchub.cloud).

No module-scope side effects: importable by tests (via importlib, tools/ is
not a package) without touching the network.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys

import requests

API_BASE = (os.environ.get("API_BASE") or "https://dchub.cloud").rstrip("/")
GH_REPO = os.environ.get("GH_REPO", "azmartone67/dchub-backend")

FEED = "kill-switch-probe"
CADENCE_HOURS = 3          # the workflow runs every 2h → overdue after 6h of silence
WINDOW_H = 2               # behaviour is judged over the last two hours
UA = "dchub-kill-switch-probe/1.0 (+https://dchub.cloud/api/v1/ops/deadman)"
# /api/v1/brain/* admin reads are relayed web → worker and can sit behind a
# long drain; the edge allows >3 min on those paths. Never below 180 s.
TIMEOUT_WORKER_PROXIED_S = 200
TIMEOUT_PUBLIC_S = 60
NOTE_MAX = 280             # the beat route clamps note to 280 chars

AGREE, VIOLATION, UNKNOWN = "agree", "violation", "unknown"

REVIEW_BRANCH_PREFIX = "brain/review-"      # routes/brain_review_lane.REVIEW_BRANCH_PREFIX
AUTOFIX_BRANCH_PREFIX = "brain/autofix-"    # routes/brain_automerge.AUTOFIX_BRANCH_PREFIX

# ── the registry: what the owner INTENDS each switch to be ───────────────────
# `expected` is the owner's declared intent as of `since` — NOT a mirror of
# Railway. Flipping production without updating this file is precisely the
# drift the probe exists to surface. `observe` names the behaviour collector
# (OBSERVERS below); `rule` states, in words, what "in effect" means so a
# human can check the verdict by hand.
SWITCHES = {
    "BRAIN_REVIEW_LANE_ENABLED": {
        "expected": "0", "service": "dchub-worker", "since": "2026-08-19",
        "observe": "review_lane",
        "rule": "when 0 the review lane must open ZERO brain/review-* pull "
                "requests in the window; when 1 opening is permitted",
    },
    "BRAIN_AUTOMERGE_ENABLED": {
        "expected": "1", "service": "dchub-worker", "since": "2026-08-22",
        "observe": "automerge",
        "rule": "brain/autofix-* merges are permitted ONLY when ENABLED=1 and "
                "DRY_RUN=0; any merge outside that is a violation of both switches",
    },
    "BRAIN_AUTOMERGE_DRY_RUN": {
        "expected": "0", "service": "dchub-worker", "since": "2026-08-22",
        "observe": "automerge",
        "rule": "when 1 (shadow mode) ZERO brain/autofix-* merges may land; "
                "evaluated together with BRAIN_AUTOMERGE_ENABLED",
    },
    "ACTION_CLASSES_ENABLED": {
        "expected": "1", "service": "dchub-backend", "since": "2026-08-22",
        "observe": "action_classes",
        "rule": "GET /api/v1/brain/squasher/classes (Step 2) publishes the running "
                "process's own view as `enabled`; it must equal the registry value — "
                "a process still serving the old env is the review-lane incident "
                "verbatim. When 0, no row may be resolved_by_action_class in the "
                "window either. Endpoint/field absent is unknown, not red",
    },
    "SQUASHER_QUEUE_DISABLE": {
        "expected": "0", "service": "dchub-worker", "since": "2026-08-22",
        "observe": "squasher_queue",
        "rule": "when 1 ZERO squasher_work_queue rows may carry requested_at "
                "inside the window; when 0 enqueueing is permitted",
    },
    "MONTHLY_QUOTA_ENFORCE": {
        "expected": "1", "service": "dchub-backend", "since": "2026-08-08",
        "observe": "quota_wall",
        "rule": "the wall's own published state, GET /api/v1/mcp/funnel "
                "quota_wall.enforce, must equal the registry value",
    },
}


# ── pure evaluation: registry value + observation → verdict ─────────────────

def _v(state, detail):
    return {"state": state, "detail": detail}


def evaluate(name, expected, obs, registry=None):
    """Pure. `obs` is the dict the switch's observer returned, or None when
    nothing could be observed. Every `unknown` here is deliberate: an
    unobservable switch is reported, never guessed healthy and never red."""
    reg = registry or SWITCHES
    kind = reg[name]["observe"]
    if obs is None:
        return _v(UNKNOWN, "not observable (endpoint missing or API unreachable)")

    if kind == "review_lane":
        n = obs.get("review_prs_opened")
        if n is None:
            return _v(UNKNOWN, "GitHub PR listing unavailable")
        if expected == "0" and n > 0:
            return _v(VIOLATION, f"set 0 but {n} {REVIEW_BRANCH_PREFIX}* PR(s) opened in the last {WINDOW_H}h: "
                                 f"{', '.join(obs.get('branches') or [])[:120]}")
        return _v(AGREE, f"{n} {REVIEW_BRANCH_PREFIX}* PR(s) opened in the last {WINDOW_H}h"
                         + ("" if expected == "0" else " (lane enabled — opening is permitted)"))

    if kind == "automerge":
        n = obs.get("autofix_prs_merged")
        if n is None:
            return _v(UNKNOWN, "GitHub merged-PR listing unavailable")
        enabled = reg["BRAIN_AUTOMERGE_ENABLED"]["expected"] == "1"
        dry = reg["BRAIN_AUTOMERGE_DRY_RUN"]["expected"] == "1"
        permitted = enabled and not dry
        if not permitted and n > 0:
            return _v(VIOLATION, f"merges forbidden (ENABLED={'1' if enabled else '0'}, DRY_RUN={'1' if dry else '0'}) "
                                 f"but {n} {AUTOFIX_BRANCH_PREFIX}* PR(s) merged in the last {WINDOW_H}h")
        return _v(AGREE, f"{n} {AUTOFIX_BRANCH_PREFIX}* PR(s) merged in the last {WINDOW_H}h"
                         + (" (merges permitted)" if permitted else " (merges forbidden)"))

    if kind == "action_classes":
        enabled = obs.get("enabled")          # the process's OWN view of ACTION_CLASSES_ENABLED
        n = obs.get("resolved_by_action_class")
        if enabled is None and n is None:
            return _v(UNKNOWN, "neither /squasher/classes nor a resolved_by_action_class field is served yet")
        if enabled is not None and bool(enabled) != (expected == "1"):
            return _v(VIOLATION, f"set {expected} but the running process publishes enabled={enabled} "
                                 f"(/api/v1/brain/squasher/classes — the env change is not in effect)")
        if n is not None and expected == "0" and n > 0:
            return _v(VIOLATION, f"set 0 but {n} squasher row(s) resolved by an action class in the last {WINDOW_H}h")
        parts = []
        if enabled is not None:
            parts.append(f"process publishes enabled={enabled}")
            if obs.get("executions_24h") is not None:
                parts.append(f"executions_24h={obs['executions_24h']}")
        if n is not None:
            parts.append(f"{n} row(s) resolved by an action class in the last {WINDOW_H}h")
        return _v(AGREE, "; ".join(parts))

    if kind == "squasher_queue":
        n = obs.get("rows_requested")
        if n is None:
            return _v(UNKNOWN, "queue served without rows")
        if expected == "1" and n > 0:
            return _v(VIOLATION, f"set 1 (disabled) but {n} row(s) enqueued in the last {WINDOW_H}h")
        return _v(AGREE, f"{n} row(s) enqueued in the last {WINDOW_H}h"
                         + (" (queue disabled)" if expected == "1" else " (queue enabled)"))

    if kind == "quota_wall":
        enforce = obs.get("enforce")
        if enforce is None:
            return _v(UNKNOWN, "funnel served but quota_wall.enforce absent")
        if bool(enforce) != (expected == "1"):
            return _v(VIOLATION, f"set {expected} but the wall publishes enforce={enforce}")
        return _v(AGREE, f"wall publishes enforce={enforce}")

    return _v(UNKNOWN, f"no evaluator for observe={kind!r}")


def aggregate(results):
    """(status, note): 'error' on ANY violation; 'success' when at least one
    switch agreed and none violated; None when every switch was unknown
    (blind — the caller must not beat)."""
    states = [r["state"] for r in results.values()]
    if any(s == VIOLATION for s in states):
        status = "error"
    elif any(s == AGREE for s in states):
        status = "success"
    else:
        status = None
    parts = [f"{n}={r['state']}" for n, r in results.items()]
    if status == "error":
        parts = [f"{n}={r['state']}" for n, r in results.items() if r["state"] == VIOLATION] + \
                [f"{n}={r['state']}" for n, r in results.items() if r["state"] != VIOLATION]
    return status, ("; ".join(parts))[:NOTE_MAX]


# ── observation helpers (network lives ONLY here) ───────────────────────────

def _utcnow():
    return dt.datetime.now(dt.timezone.utc)


def _window_start(now=None, hours=WINDOW_H):
    return (now or _utcnow()) - dt.timedelta(hours=hours)


def _since_qualifier(now=None):
    return _window_start(now).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _parse_ts(s):
    """ISO-8601 → aware datetime, or None. Tolerates a trailing Z."""
    if not s or not isinstance(s, str):
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t


def _gh_prs(extra_args):
    """`gh pr list … --json` → list of PR dicts, or None when gh failed."""
    cmd = ["gh", "pr", "list", "--repo", GH_REPO, "--limit", "100",
           "--json", "number,headRefName,createdAt,mergedAt,state"] + list(extra_args)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001 — gh missing / timeout → unknown, not red
        print(f"  gh pr list failed: {e}")
        return None
    if out.returncode != 0:
        print(f"  gh pr list exited {out.returncode}: {(out.stderr or '').strip()[:300]}")
        return None
    try:
        data = json.loads(out.stdout or "[]")
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def observe_review_lane(now=None):
    prs = _gh_prs(["--state", "all", "--search", f"created:>={_since_qualifier(now)}"])
    if prs is None:
        return None
    start = _window_start(now)
    hits = [p.get("headRefName") for p in prs
            if (p.get("headRefName") or "").startswith(REVIEW_BRANCH_PREFIX)
            and (_parse_ts(p.get("createdAt")) or start) >= start]
    return {"review_prs_opened": len(hits), "branches": hits}


def observe_automerge(now=None):
    prs = _gh_prs(["--state", "merged", "--search", f"merged:>={_since_qualifier(now)}"])
    if prs is None:
        return None
    start = _window_start(now)
    hits = [p.get("headRefName") for p in prs
            if (p.get("headRefName") or "").startswith(AUTOFIX_BRANCH_PREFIX)
            and (_parse_ts(p.get("mergedAt")) or start) >= start]
    return {"autofix_prs_merged": len(hits), "branches": hits}


def _get(path, admin=False, timeout=TIMEOUT_PUBLIC_S):
    """GET API_BASE+path → (http_status, parsed_json_or_None). A transport
    failure (or an admin read with no key) is (None, None)."""
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if admin:
        key = os.environ.get("DCHUB_ADMIN_KEY") or ""
        if not key:
            print(f"  GET {path}: DCHUB_ADMIN_KEY missing — admin surface not observable")
            return None, None
        headers["X-Admin-Key"] = key
    try:
        r = requests.get(API_BASE + path, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        print(f"  GET {path} failed: {e}")
        return None, None
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, body


def count_resolved_by_action_class(body, start):
    """Tolerant reader for the field the Step-2 inbox will expose. Accepts:
       * top-level list  `resolved_by_action_class: [{resolved_at|finished_at, …}]`
       * top-level count `counts.resolved_by_action_class` (window-agnostic)
       * per-row marker  rows[i].resolved_by_action_class truthy (+ finished_at/resolved_at)
    Returns the in-window count, or None when NONE of these is present —
    the field-absent case is unknown, never a silent zero."""
    if not isinstance(body, dict):
        return None
    lst = body.get("resolved_by_action_class")
    if isinstance(lst, list):
        n = 0
        for item in lst:
            ts = _parse_ts((item or {}).get("resolved_at") or (item or {}).get("finished_at")) if isinstance(item, dict) else None
            if ts is None or ts >= start:
                n += 1
        return n
    counts = body.get("counts")
    if isinstance(counts, dict) and isinstance(counts.get("resolved_by_action_class"), int):
        return int(counts["resolved_by_action_class"])
    rows = body.get("rows")
    if isinstance(rows, list) and any(isinstance(r, dict) and "resolved_by_action_class" in r for r in rows):
        n = 0
        for r in rows:
            if not (isinstance(r, dict) and r.get("resolved_by_action_class")):
                continue
            ts = _parse_ts(r.get("resolved_at") or r.get("finished_at"))
            if ts is None or ts >= start:
                n += 1
        return n
    return None


def observe_action_classes(now=None):
    """Two reads, both optional: the Step-2 registry+state surface
    (`enabled` = what the RUNNING process thinks the switch is, `day_used` =
    executed-and-not-dry-run runs in 24h) and the inbox's resolved-by-class
    field. None when neither is served (Step 2 not shipped, 401, 5xx)."""
    out = {}
    status, body = _get("/api/v1/brain/squasher/classes", admin=True, timeout=TIMEOUT_WORKER_PROXIED_S)
    if (status == 200 and isinstance(body, dict) and body.get("known") is True
            and isinstance(body.get("enabled"), bool)):
        out["enabled"] = body["enabled"]
        if isinstance(body.get("day_used"), int):
            out["executions_24h"] = body["day_used"]
    status, body = _get("/api/v1/brain/squasher/inbox", admin=True, timeout=TIMEOUT_WORKER_PROXIED_S)
    if status == 200 and isinstance(body, dict):
        out["resolved_by_action_class"] = count_resolved_by_action_class(body, _window_start(now))
    return out or None


def observe_squasher_queue(now=None):
    status, body = _get("/api/v1/brain/squasher/queue", admin=True, timeout=TIMEOUT_WORKER_PROXIED_S)
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("rows"), list):
        return None
    start = _window_start(now)
    n = sum(1 for r in body["rows"]
            if isinstance(r, dict) and (_parse_ts(r.get("requested_at")) or start - dt.timedelta(days=1)) >= start)
    return {"rows_requested": n}


def observe_quota_wall(now=None):
    status, body = _get("/api/v1/mcp/funnel", timeout=TIMEOUT_PUBLIC_S)
    if status != 200 or not isinstance(body, dict):
        return None
    qw = body.get("quota_wall")
    if not isinstance(qw, dict) or "enforce" not in qw:
        return {"enforce": None}
    return {"enforce": bool(qw["enforce"])}


OBSERVERS = {
    "review_lane": observe_review_lane,
    "automerge": observe_automerge,
    "action_classes": observe_action_classes,
    "squasher_queue": observe_squasher_queue,
    "quota_wall": observe_quota_wall,
}


def run(registry=None, observers=None, now=None):
    """Observe each behaviour ONCE (switches sharing an observer share the
    observation) and evaluate every registered switch."""
    reg = registry or SWITCHES
    fns = observers or OBSERVERS
    now = now or _utcnow()
    seen = {}
    results = {}
    for name, spec in reg.items():
        kind = spec["observe"]
        if kind not in seen:
            fn = fns.get(kind)
            try:
                seen[kind] = fn(now) if fn else None
            except Exception as e:  # noqa: BLE001 — an observer crash is unknown, not red
                print(f"  observer {kind} crashed: {type(e).__name__}: {e}")
                seen[kind] = None
        results[name] = evaluate(name, spec["expected"], seen[kind], reg)
    return results


# ── the beat: ONE writer of feed `kill-switch-probe` ────────────────────────

def beat(status, note, http=None):
    """POST the verdict to the dead-man ledger. LOUD by design: raises on a
    non-2xx, on a non-JSON body and on ok != true. The response is printed in
    full — a truncated body hid results before (never `| head -c` it)."""
    key = os.environ.get("DCHUB_ADMIN_KEY") or ""
    if not key:
        raise RuntimeError("DCHUB_ADMIN_KEY missing — cannot beat the ledger")
    body = {"feed": FEED, "status": status, "cadence_hours": CADENCE_HOURS,
            "note": (note or "")[:NOTE_MAX]}
    r = (http or requests).post(
        API_BASE + "/api/v1/admin/ingest-runs/beat", json=body,
        headers={"X-Admin-Key": key, "User-Agent": UA, "Content-Type": "application/json"},
        timeout=TIMEOUT_WORKER_PROXIED_S)
    text = r.text
    print(f"beat {FEED} status={status} -> HTTP {r.status_code}: {text}")
    if not 200 <= int(r.status_code) < 300:
        raise RuntimeError(f"beat returned HTTP {r.status_code}: {text}")
    try:
        j = r.json()
    except ValueError:
        raise RuntimeError(f"beat returned non-JSON: {text}")
    if not isinstance(j, dict) or j.get("ok") is not True:
        raise RuntimeError(f"beat ok!=true: {text}")
    return j


def main(argv=None):
    results = run()
    print(f"kill-switch probe · window {WINDOW_H}h · {_utcnow().isoformat()}")
    for name, v in results.items():
        print(f"  {name:27s} expected={SWITCHES[name]['expected']}  {v['state']:9s} {v['detail']}")
    status, note = aggregate(results)
    if status is None:
        print("::error::kill-switch-probe observed NOTHING (every switch unknown) — "
              "not beating; a blind probe must be loud, not green")
        return 2
    beat(status, note)            # raises → non-zero exit → red run (loud)
    if status == "error":
        print(f"::error::kill switch set ≠ in effect: {note}")
        return 1
    print(f"all observable switches agree with the registry: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
