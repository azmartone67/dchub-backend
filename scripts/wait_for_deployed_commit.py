#!/usr/bin/env python3
"""wait_for_deployed_commit.py — block until the Railway origin RUNS a commit,
or fail loudly (2026-09-11).

WHY THIS EXISTS
===============
Two post-deploy steps waited on a clock: sitemap-snapshot.yml slept 120s and
rebuilt the sitemap snapshot, post-deploy-smoke.yml slept 90s and smoke-tested.
#4385 merged as 9923d803d at 06:01:57Z on 2026-09-11:

    06:02:00Z  Railway deployment 97a7304d created; both workflow runs created
    06:03:40Z  smoke test starts (done 06:04:01Z)
    06:04:02Z  origin still answers with the OLD code
    06:04:03Z  snapshot rebuild starts: generation 513, 27,199 URLs, ok
    06:04:22Z  origin answers with the NEW code for the first time

Both runs were green, and both had exercised the build #4385 replaced;
/sitemap-static.xml kept listing three brief URLs that 301. Nor was it bad
luck: over Railway's 39 most recent swaps of this service, deployment created
-> previous deployment REMOVED took 111-202s. A fixed sleep either loses that
race or waits minutes for nothing.

★ THE RUNNING COMMIT IS THE SIGNAL. GET /api/v1/admin/build-info reports
RAILWAY_GIT_COMMIT_SHA for the deploy that answered (routes/build_info.py —
read-only, no database). Poll it until the origin runs --expect; exit non-zero
if it never does, so the next step cannot run against the old build and report
the result as the new one.

"Runs --expect" means the answering deploy is:
  * --expect itself, or
  * a DESCENDANT of it (GitHub compare status `ahead`). Railway deploys every
    push and main often moves within minutes, so a later push can go live
    before this one ever does. build-info then never names --expect — but the
    code it runs contains it, which is all the next step needs.
Never: an older commit (`behind`), a diverged one (a rollback), a compare
GitHub did not answer, or an answer naming no 40-hex SHA. build_info falls
back to RAILWAY_DEPLOYMENT_ID — a UUID — and "unanswerable" is not "current".

★ CONFIRMED, NOT SEEN ONCE. The service runs 2 replicas. One matching read
proves one request reached the new deploy, not that the next request (the
rebuild, the smoke test) will, so --confirm consecutive reads must match and a
stale read restarts the count. That narrows the swap window; it cannot close
it, because the next step's request is a different request.

★ A REFUSED KEY FAILS AT ONCE. 401/403 (or BUILD_INFO_DISABLE=1) means this
step cannot see the deploy at all. Polling to the deadline would report a dead
credential as a slow deploy, ten minutes late.

Reads the ORIGIN, not dchub.cloud: the edge worker answers GETs from the stale
Render failover while Railway is erroring — and a deploy swap is exactly when
Railway errors.

HTTP goes through curl, because scripts/regression_lint.py blocks
urllib.request.urlopen; headers go to curl on stdin (--config -), so the admin
key and the GitHub token never appear in a process listing.

Usage (a workflow step, push events only):
    python3 scripts/wait_for_deployed_commit.py --expect "$EXPECT_SHA"
Env:
    DCHUB_ADMIN_KEY    required
    GH_TOKEN           for the compare API; the repo is public, but anonymous
                       calls share a 60/hour limit per runner IP
    GITHUB_REPOSITORY  owner/repo (set by Actions)
Exit: 0 arrived and confirmed; 1 never arrived, or unreadable; 2 bad arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

ORIGIN = "https://dchub-backend-production.up.railway.app"
BUILD_INFO_PATH = "/api/v1/admin/build-info"
GITHUB_API = "https://api.github.com"
DEFAULT_REPO = "azmartone67/dchub-backend"
UA = "DCHub-DeployWait/1.0"

_FULL_SHA = re.compile(r"[0-9a-f]{40}")

# GitHub's compare status for base=EXPECT...head=RUNNING, read as "does the
# running commit contain EXPECT?". Anything unlisted is unknown — never yes.
_CONTAINS = {"identical": True, "ahead": True, "behind": False, "diverged": False}


def curl_get(url: str, headers: dict, max_time: float) -> tuple:
    """GET -> (status, body), or (None, why) when no HTTP response arrived.

    Headers travel in a curl config on stdin rather than in argv: they carry
    the admin key and the GitHub token, and argv is visible to every process on
    the host. `-q` first, so a ~/.curlrc cannot change what this does."""
    def quoted(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    config = "".join(f"header = {quoted(f'{k}: {v}')}\n" for k, v in headers.items())
    config += f"url = {quoted(url)}\n"
    try:
        proc = subprocess.run(
            ["curl", "-q", "-sS", "--max-time", f"{max_time:g}",
             "-w", "\n%{http_code}", "--config", "-"],
            input=config, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=max_time + 15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"curl did not complete: {e}"[:200]
    if proc.returncode != 0:
        return None, f"no response (curl exit {proc.returncode}: {proc.stderr.strip()})"[:200]
    body, _, status = proc.stdout.rpartition("\n")
    if not status.isdigit():
        return None, f"no HTTP status from curl ({status[:40]!r})"
    return int(status), body


def classify(status, body: str) -> tuple:
    """One build-info answer -> (kind, detail).

      ("sha", sha)     the answering deploy runs this 40-hex commit
      ("fatal", why)   this step cannot see the deploy; waiting changes nothing
      ("wait", why)    no usable answer yet — a swap in flight, a transient error
    """
    if status is None:
        return "wait", body
    if status in (401, 403):
        return "fatal", (f"build-info refused DCHUB_ADMIN_KEY (HTTP {status}), so this "
                         f"step cannot see which commit is deployed")
    try:
        doc = json.loads(body)
    except ValueError:
        doc = None
    doc = doc if isinstance(doc, dict) else {}
    if status == 404 and doc.get("error") == "disabled":
        return "fatal", "build-info is switched off on the origin (BUILD_INFO_DISABLE=1)"
    if status != 200:
        return "wait", f"build-info answered HTTP {status}"
    commit = doc.get("commit") if isinstance(doc.get("commit"), dict) else {}
    sha = commit.get("sha")
    if isinstance(sha, str) and _FULL_SHA.fullmatch(sha.strip().lower()):
        return "sha", sha.strip().lower()
    return "wait", (f"build-info names no commit SHA "
                    f"(var={commit.get('var')!r}, sha={sha!r})")


def github_contains(api: str, repo: str, token):
    """-> contains(expect, running): True when `running` is `expect` or descends
    from it, False when it does not, None when GitHub did not say."""
    def contains(expect: str, running: str):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        status, body = curl_get(
            f"{api}/repos/{repo}/compare/{expect}...{running}?per_page=1",
            headers, max_time=20)
        if status != 200:
            return None
        try:
            return _CONTAINS.get(json.loads(body).get("status"))
        except (ValueError, AttributeError):
            return None
    return contains


def wait_for_commit(expect, read, contains, *, timeout, interval, confirm,
                    confirm_interval, clock=time.monotonic, sleep=time.sleep,
                    log=print) -> tuple:
    """Poll `read` until `confirm` consecutive answers show a commit containing
    `expect`. -> ("arrived" | "fatal" | "timeout", summary).

    The deadline is strict: no read starts after `timeout` has elapsed."""
    start = clock()
    known = {}      # running sha -> verdict; a commit's ancestry never changes
    streak = 0
    while True:
        kind, detail = read()
        elapsed = clock() - start
        if kind == "fatal":
            return "fatal", detail
        here = False
        if kind != "sha":
            seen = detail
        elif detail == expect:
            here, seen = True, f"origin runs {expect[:9]}"
        else:
            if detail not in known:
                verdict = contains(expect, detail)
                if verdict is not None:     # an unanswered compare is asked again
                    known[detail] = verdict
            verdict = known.get(detail)
            if verdict:
                here, seen = True, f"origin runs {detail[:9]}, a descendant of {expect[:9]}"
            elif verdict is False:
                seen = f"origin runs {detail[:9]}, which does not contain {expect[:9]}"
            else:
                seen = (f"origin runs {detail[:9]}; GitHub did not say whether "
                        f"it contains {expect[:9]}")
        streak = streak + 1 if here else 0
        log(f"[{elapsed:4.0f}s] {seen}"
            + (f"  ({streak}/{confirm})" if here else "  — waiting"))
        if streak >= confirm:
            return "arrived", (f"{seen} — {confirm} consecutive reads, "
                               f"{elapsed:.0f}s after the wait began")
        pause = confirm_interval if here else interval
        if clock() + pause - start > timeout:
            return "timeout", (f"the origin did not run {expect[:9]} within "
                               f"{timeout:g}s — last read: {seen}")
        sleep(pause)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Wait until the Railway origin runs a commit, or fail.")
    ap.add_argument("--expect", required=True,
                    help="full 40-hex SHA the origin must run (github.sha)")
    ap.add_argument("--base", default=ORIGIN, help="origin serving build-info")
    ap.add_argument("--github-api", default=GITHUB_API)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO)
    ap.add_argument("--timeout", type=float, default=600,
                    help="give up after this many seconds (default 600)")
    ap.add_argument("--interval", type=float, default=15,
                    help="seconds between reads while the old build answers")
    ap.add_argument("--confirm", type=int, default=3,
                    help="consecutive matching reads required (default 3)")
    ap.add_argument("--confirm-interval", type=float, default=5)
    a = ap.parse_args(argv)

    expect = a.expect.strip().lower()
    if not _FULL_SHA.fullmatch(expect):
        print(f"::error title=deploy wait::--expect must be a full 40-hex commit "
              f"SHA, got {a.expect!r}")
        return 2
    if a.confirm < 1 or min(a.timeout, a.interval, a.confirm_interval) <= 0:
        print("::error title=deploy wait::--confirm must be >= 1 and "
              "--timeout/--interval/--confirm-interval > 0")
        return 2
    key = (os.environ.get("DCHUB_ADMIN_KEY") or "").strip()
    if not key:
        print("::error title=deploy wait::DCHUB_ADMIN_KEY is empty, so this step "
              "cannot read which commit the origin runs")
        return 1

    url = a.base.rstrip("/") + BUILD_INFO_PATH

    def read():
        # Cache-busted although the origin does not cache: nothing on this
        # path may ever be answered from a stored copy.
        return classify(*curl_get(f"{url}?_={time.time_ns()}",
                                  {"X-Admin-Key": key, "User-Agent": UA},
                                  max_time=10))

    contains = github_contains(a.github_api.rstrip("/"), a.repo,
                               (os.environ.get("GH_TOKEN") or "").strip() or None)
    print(f"waiting for {url} to run {expect} (up to {a.timeout:g}s, every "
          f"{a.interval:g}s, {a.confirm} consecutive reads)", flush=True)
    outcome, summary = wait_for_commit(
        expect, read, contains, timeout=a.timeout, interval=a.interval,
        confirm=a.confirm, confirm_interval=a.confirm_interval,
        log=lambda m: print(m, flush=True))
    if outcome == "arrived":
        print(f"::notice title=deploy landed::{summary}")
        return 0
    if outcome == "fatal":
        print(f"::error title=deploy wait::{summary}. Stopping: the next step "
              f"must not run against a deploy this step could not identify.")
        return 1
    print(f"::error title=deploy never landed::{summary}. Stopping: the next step "
          f"would run against the build that was live BEFORE {expect[:9]} and "
          f"report the result as {expect[:9]}.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
