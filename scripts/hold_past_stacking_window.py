#!/usr/bin/env python3
"""hold_past_stacking_window.py — hold a push-path rollback decision until the
rollback it may order can actually execute (2026-09-11).

WHY THIS EXISTS
===============
scripts/railway_rollback.py refuses to roll back while the live deployment is
younger than MIN_CURRENT_AGE_S (600s). That is its anti-stacking guard: several
actors react to one incident (the worker sentinel, this workflow's cron and push
lanes), a deployment created minutes ago usually means one of them already
rolled back, and a second rollback would land further back than anyone meant.

The guard cannot tell that apart from a deploy that is young because it JUST
LANDED. auto-rollback's push lane decided ~6.5 minutes after a merge (the deploy
wait, then 5 samples over 4 minutes), so a burn it found on the commit it had
just measured was refused every time: it alerted, and restored nothing.

So the push lane holds here — after the deploy wait, before the samples — until
the live deployment is old enough that the decision after it lands --margin
seconds past the window:

    hold until  created_at(live deployment) + MIN_CURRENT_AGE_S + margin - before_decision

--before-decision is the least time the steps between this hold and the
rollback take. The guard itself is untouched and still decides: a rollback by
anyone else creates a new deployment, which is young again.

★ NEVER WAIT OUT SOMEONE ELSE'S ROLLBACK. The hold anchors only on a live
deployment that runs --expect, or a later commit containing it. A live
deployment on any other commit is a rollback or a deploy this run never
measured, and holding past ITS window would disarm the guard for exactly the
case it exists for. Nor does it hold while a newer deployment is already on its
way above the live one (a newer push, or a rollback in flight). Either way: no
hold, and the guard decides.

★ ORDER AGAINST THE SENTINEL. The worker sentinel samples every 60s and acts on
its first sample past the window. --margin must exceed that cadence, so an armed
sentinel acts first and this lane's rollback then finds a young deployment and
is refused — one rollback, not two racing for the same boundary.

★ AN UNREADABLE ANSWER NEVER MAKES IT ACT SOONER. No RAILWAY_TOKEN: no rollback
can run from this job, so there is nothing to hold for. Railway's API failing:
hold the full span from now — a bound, because the deploy the wait step just
confirmed was created before this step began, and no automated actor can have
rolled back a deploy that young. Exit 0 always: this step only times the
decision; the steps after it make it.

Usage (auto-rollback.yml, push path):
    python3 scripts/hold_past_stacking_window.py --expect "$EXPECT_SHA" --margin 120 --before-decision 240
Env: RAILWAY_TOKEN, GH_TOKEN (compare API), GITHUB_REPOSITORY
Exit: 0 held or nothing to hold; 2 bad arguments.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    """railway_rollback and the deploy wait, by path. The guard's constant, its
    notion of the live deployment and the ancestry check are theirs; a copy here
    would be the drift this hold exists to close."""
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rb = _load("railway_rollback")
wait = _load("wait_for_deployed_commit")


def plan(deployments, expect, contains):
    """What the hold anchors on, from Railway's newest-first deployment list.

      ("hold", created_at, detail)  the live deployment runs this push: age it
      ("skip", None, why)           not ours to age — the guard decides now
      ("bound", None, why)          cannot read the live deployment's age
    """
    i_live = next((i for i, d in enumerate(deployments) if d.get("status") == "SUCCESS"), None)
    if i_live is None:
        return "bound", None, "Railway lists no SUCCESS deployment"
    live = deployments[i_live]
    on_the_way = [d for d in deployments[:i_live] if d.get("status") in rb.LIVE_STATUSES]
    if on_the_way:
        d = on_the_way[0]
        return "skip", None, (f"deployment {d['id'][:8]} ({d.get('status')}) is already on its "
                              f"way above the live one: a newer push, or a rollback in flight")
    sha = (rb.commit_sha(live) or "").strip().lower()
    if not sha:
        return "skip", None, f"live deployment {live['id'][:8]} names no commit"
    if not (sha == expect or (len(sha) >= 7 and expect.startswith(sha))):
        verdict = contains(expect, sha) if wait._FULL_SHA.fullmatch(sha) else None
        if verdict is not True:
            said = "does not contain" if verdict is False else "GitHub did not say contains"
            return "skip", None, (f"live deployment {live['id'][:8]} runs {sha[:9]}, which {said} "
                                  f"{expect[:9]}: someone's rollback, or a deploy this run never measured")
    created = rb._parse_ts(live.get("createdAt"))
    if not created:
        return "bound", None, f"live deployment {live['id'][:8]} has no readable createdAt"
    return "hold", created, f"live deployment {live['id'][:8]} runs {sha[:9]}"


def parser():
    ap = argparse.ArgumentParser(
        description="Hold a push-path rollback decision past the anti-stacking window.")
    ap.add_argument("--expect", required=True, help="full 40-hex SHA of the push (github.sha)")
    ap.add_argument("--margin", type=int, required=True,
                    help="seconds past the window the decision must land")
    ap.add_argument("--before-decision", type=int, required=True,
                    help="least seconds the steps between this hold and the rollback take")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY") or wait.DEFAULT_REPO)
    ap.add_argument("--github-api", default=wait.GITHUB_API)
    return ap


def main(argv=None, *, clock=time.time, sleep=time.sleep) -> int:
    a = parser().parse_args(argv)
    expect = a.expect.strip().lower()
    if not wait._FULL_SHA.fullmatch(expect) or a.margin < 0 or a.before_decision < 0:
        print("::error title=stacking window::--expect must be a full 40-hex SHA, and "
              "--margin and --before-decision must be >= 0")
        return 2
    span = rb.MIN_CURRENT_AGE_S + a.margin - a.before_decision
    start = clock()
    token = (os.environ.get("RAILWAY_TOKEN") or "").strip()
    if not token:
        print("::notice title=stacking window::RAILWAY_TOKEN is not set, so no rollback can "
              "run from this job: nothing to hold for")
        return 0
    try:
        contains = wait.github_contains(a.github_api.rstrip("/"), a.repo,
                                        (os.environ.get("GH_TOKEN") or "").strip() or None)
        kind, created, detail = plan(rb.list_deployments(token), expect, contains)
    except Exception as e:  # unreadable must never make it act sooner: hold the bound
        kind, created, detail = "bound", None, f"Railway's deployment list failed ({str(e)[:160]})"
    if kind == "skip":
        print(f"::notice title=stacking window::no hold, {detail}. The rollback script's "
              f"own guard decides.")
        return 0
    until = (created if kind == "hold" else start) + span
    left = until - clock()
    bound = ("" if kind == "hold" else
             " — a bound: the deploy the wait step confirmed was created before this step began")
    print(f"::notice title=stacking window::{detail}; holding {max(left, 0):.0f}s so the decision "
          f"lands at least {a.margin}s past the {rb.MIN_CURRENT_AGE_S}s window{bound}", flush=True)
    while left > 0:
        sleep(min(30.0, left))
        left = until - clock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
