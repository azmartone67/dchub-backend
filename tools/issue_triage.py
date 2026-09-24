#!/usr/bin/env python3
"""tools/issue_triage.py — keep the GitHub issue list something a human can read.

MEASURED 2026-09-23: 197 open issues on dchub-backend, and 88 of them were ONE
alarm. dchub-self-healing.yml ran `gh issue create` on every red hourly probe
with an HH:MM title, never looked for an open one and never closed on green —
every one was `crm_export` red (a CRM queue blocked on a missing HubSpot key).
No loop owned the issue list as a whole: brain_issue_janitor owns brain-l15/
l22/l23, issue-autoclose owns workflow-failure/STATUS, spec-debt-reconcile owns
spec-debt, and the squasher agent lane works its own DB queue, never issues.

Two modes:

  watchdog  Called by dchub-self-healing.yml instead of `gh issue create`.
            ONE open tracker per alarm. A red probe opens it if none is open,
            comments only when the set of failing checks CHANGES (so an hourly
            probe does not bury the issue under 24 identical comments a day),
            and a green probe closes it.

  sweep     Daily. Closes older copies of a bot-filed issue whose title is
            identical once dates/times are stripped (the newest stays open),
            then rewrites one "[triage] Needs you" issue assigned to the owner:
            decisions nobody owns, alarms still red, machine findings nobody
            has touched in 14 days. It LISTS stale findings, it never closes
            them — only an exact duplicate is safe to close without evidence.

Everything that talks to GitHub goes through `Gh`, so tests substitute a fake
and exercise the real planning code.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys

REPO = os.environ.get("GITHUB_REPOSITORY", "azmartone67/dchub-backend")
OWNER_LOGIN = os.environ.get("TRIAGE_OWNER", "azmartone67")

WATCHDOG_PREFIX = "[watchdog] DC Hub health check"
WATCHDOG_TITLE = "[watchdog] DC Hub health check red"
WATCHDOG_LABEL = "watchdog"
SIG_RE = re.compile(r"<!-- watchdog-sig: (.*?) -->")

TRIAGE_TITLE = "[triage] Needs you — open decisions and red alarms"
TRIAGE_LABEL = "triage-digest"

BOT_LOGINS = {"github-actions", "app/github-actions", "github-actions[bot]"}
NEVER_CLOSE = {"keep", "pinned", "needs-decision", "needs-human-merge", TRIAGE_LABEL}
DECISION_LABELS = {"needs-decision", "needs-human-merge"}
ALARM_LABELS = {"watchdog", "deadman", "slo-gate", "kill-switch-probe"}
STALE_DAYS = 14

_TIME_BITS = re.compile(
    r"\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?Z?)?"  # dates, ISO stamps
    r"|\b\d{1,2}:\d\d(?::\d\d)?\b"                            # HH:MM[:SS]
)


# ── GitHub access ────────────────────────────────────────────────────────────
class Gh:
    """Thin `gh api` wrapper. Every failure raises: a triage run that could not
    read the issue list must not report that nothing needs attention."""

    def __init__(self, repo: str = REPO, dry: bool = False):
        self.repo, self.dry = repo, dry

    def _api(self, path: str, method: str = "GET", body: dict | None = None,
             paginate: bool = False):
        cmd = ["gh", "api", "-X", method, f"repos/{self.repo}/{path}"]
        if paginate:
            # --slurp wraps every page in one outer array: [[page1...], [page2...]]
            cmd[2:2] = ["--paginate", "--slurp"]
        inp = None
        if body is not None:
            cmd += ["--input", "-"]
            inp = json.dumps(body)
        out = subprocess.run(cmd, input=inp, capture_output=True, text=True, check=True).stdout
        if paginate:
            return [item for page in json.loads(out) for item in page]
        return json.loads(out) if out.strip() else None

    def open_issues(self) -> list[dict]:
        items = self._api("issues?state=open&per_page=100", paginate=True)
        return [i for i in items if "pull_request" not in i]

    def comments(self, number: int) -> list[dict]:
        return self._api(f"issues/{number}/comments?per_page=100", paginate=True)

    def create(self, title: str, body: str, labels: list[str],
               assignees: list[str] | None = None) -> int | None:
        if self.dry:
            return None
        payload = {"title": title, "body": body, "labels": labels}
        if assignees:
            payload["assignees"] = assignees
        return self._api("issues", "POST", payload)["number"]

    def comment(self, number: int, body: str) -> None:
        if not self.dry:
            self._api(f"issues/{number}/comments", "POST", {"body": body})

    def edit(self, number: int, **fields) -> None:
        if not self.dry:
            self._api(f"issues/{number}", "PATCH", fields)

    def close(self, number: int, comment: str, reason: str = "not_planned") -> None:
        self.comment(number, comment)
        self.edit(number, state="closed", state_reason=reason)


# ── helpers ──────────────────────────────────────────────────────────────────
def labels_of(issue: dict) -> set[str]:
    return {(l if isinstance(l, str) else l.get("name", "")) for l in issue.get("labels") or []}


def author_of(issue: dict) -> str:
    u = issue.get("user") or issue.get("author") or {}
    return u.get("login", "")


def is_bot(issue: dict) -> bool:
    return author_of(issue) in BOT_LOGINS


def title_key(title: str) -> str:
    """The title with dates and clock times removed, so hourly/daily re-files of
    one problem compare equal. Other digits (issue ids, counts) are kept: two
    findings that differ only in `inv #100694` vs `inv #100681` are different."""
    return re.sub(r"\s+", " ", _TIME_BITS.sub("", title)).strip(" —-:").lower()


def _parse_ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def failing_signature(health: dict | None, exit_code: int) -> str:
    """Stable description of what is red. Empty string means green."""
    if exit_code != 0 or not isinstance(health, dict):
        return "health endpoint unreachable"
    if health.get("status") != "red":
        return ""
    bad = sorted(k for k, v in (health.get("checks") or {}).items()
                 if isinstance(v, dict) and v.get("status") == "red")
    return ",".join(bad) or "status red, no red check named"


# ── watchdog ─────────────────────────────────────────────────────────────────
def watchdog(gh: Gh, health: dict | None, exit_code: int, run_url: str = "") -> str:
    sig = failing_signature(health, exit_code)
    trackers = sorted(
        (i for i in gh.open_issues()
         if WATCHDOG_LABEL in labels_of(i) and i["title"].startswith(WATCHDOG_PREFIX)),
        key=lambda i: i["number"], reverse=True)
    detail = json.dumps(health, indent=2)[:6000] if health is not None else "(no body)"
    run = f"\n\nRun: {run_url}" if run_url else ""

    if not sig:
        for t in trackers:
            gh.close(t["number"], "✅ Health probe is green again, closing. "
                     f"The watchdog opens a new tracker if it goes red again.{run}",
                     reason="completed")
        return f"green; closed {len(trackers)}"

    marker = f"<!-- watchdog-sig: {sig} -->"
    if not trackers:
        n = gh.create(WATCHDOG_TITLE,
                      f"Red checks: **{sig}**\n\n```json\n{detail}\n```{run}\n\n"
                      "This issue is REUSED: the hourly watchdog comments here only when "
                      "the set of red checks changes, and closes it when health is green.\n"
                      f"{marker}", [WATCHDOG_LABEL])
        return f"red ({sig}); opened #{n}"

    keep, extra = trackers[0], trackers[1:]
    for t in extra:
        gh.close(t["number"], f"Duplicate of #{keep['number']}: the watchdog keeps one tracker.")
    texts = [keep.get("body") or ""] + [c.get("body") or "" for c in gh.comments(keep["number"])]
    last = None
    for t in texts:
        found = SIG_RE.findall(t)
        if found:
            last = found[-1]
    if last == sig:
        return f"red ({sig}); unchanged on #{keep['number']}"
    gh.comment(keep["number"],
               f"Red checks changed: **{last or 'unknown'}** → **{sig}**\n\n"
               f"```json\n{detail}\n```{run}\n{marker}")
    return f"red ({sig}); noted change on #{keep['number']}"


# ── sweep ────────────────────────────────────────────────────────────────────
def plan_duplicates(issues: list[dict]) -> list[tuple[dict, dict]]:
    """[(older_copy, newest)] for bot-filed issues whose date-stripped titles match."""
    groups: dict[tuple, list[dict]] = {}
    for i in issues:
        if not is_bot(i) or labels_of(i) & NEVER_CLOSE:
            continue
        key = (title_key(i["title"]), frozenset(labels_of(i)))
        groups.setdefault(key, []).append(i)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda i: i["number"], reverse=True)
        out += [(old, members[0]) for old in members[1:]]
    return out


def render_digest(issues: list[dict], now: dt.datetime) -> str:
    def line(i):
        age = (now - _parse_ts(i["created_at"])).days
        return f"- #{i['number']} · {age}d · {i['title'][:110]}"

    live = [i for i in issues if TRIAGE_LABEL not in labels_of(i)]
    decisions = [i for i in live if labels_of(i) & DECISION_LABELS or not labels_of(i)]
    alarms = [i for i in live if labels_of(i) & ALARM_LABELS]
    cutoff = now - dt.timedelta(days=STALE_DAYS)
    stale = [i for i in live if is_bot(i) and i not in alarms
             and _parse_ts(i["updated_at"]) < cutoff]
    counts: dict[str, int] = {}
    for i in live:
        for l in labels_of(i) or {"(no label)"}:
            counts[l] = counts.get(l, 0) + 1

    def section(title, rows, empty, note="", cap=30):
        rows = sorted(rows, key=lambda i: i["created_at"])
        body = "\n".join(line(i) for i in rows[:cap])
        if len(rows) > cap:
            body += f"\n- … and {len(rows) - cap} more"
        return f"### {title} ({len(rows)})\n{note}{body or empty}\n"

    top = ", ".join(f"`{k}` {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:12])
    return (
        f"_Rewritten daily by issue-triage.yml · {now:%Y-%m-%d %H:%M} UTC · "
        f"{len(live)} open issues_\n\n"
        + section("Decisions waiting on you", decisions, "_None._",
                  "Labelled needs-decision / needs-human-merge, or no label at all "
                  "(no loop owns an unlabelled issue).\n")
        + "\n" + section("Alarms still red", alarms, "_None._",
                         "Machines re-check these, but the fix usually needs you "
                         "(a key, a config value, a vendor).\n")
        + "\n" + section(f"Machine findings untouched for {STALE_DAYS}+ days", stale, "_None._",
                         "No loop has updated these in two weeks. Close, label "
                         "needs-decision, or leave for the owning loop.\n")
        + f"\n**By label:** {top}\n\n"
        "Closed automatically: exact duplicates only (same bot title once dates "
        "are removed; the newest copy stays open)."
    )


def sweep(gh: Gh, now: dt.datetime | None = None, max_closes: int = 100) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    issues = gh.open_issues()
    if not issues:
        raise RuntimeError("open-issue list came back empty; refusing to report a clear queue")
    dupes = plan_duplicates(issues)[:max_closes]
    closed = set()
    for old, new in dupes:
        gh.close(old["number"], f"Duplicate of #{new['number']} (same issue re-filed). "
                 "Closed by issue-triage; the newest copy stays open.")
        closed.add(old["number"])
    remaining = [i for i in issues if i["number"] not in closed]
    body = render_digest(remaining, now)
    tracker = next((i for i in issues if TRIAGE_LABEL in labels_of(i)), None)
    if tracker is None:
        n = gh.create(TRIAGE_TITLE, body, [TRIAGE_LABEL], [OWNER_LOGIN])
    else:
        n = tracker["number"]
        if (tracker.get("body") or "").split("\n", 1)[-1] != body.split("\n", 1)[-1]:
            gh.edit(n, body=body)
    return {"open": len(issues), "duplicates_closed": sorted(closed), "digest": n}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["watchdog", "sweep"])
    ap.add_argument("--health", help="watchdog: path to the /api/v1/health JSON")
    ap.add_argument("--exit-code", type=int, default=0)
    ap.add_argument("--run-url", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-closes", type=int, default=100)
    a = ap.parse_args(argv)
    gh = Gh(dry=a.dry_run)
    if a.mode == "watchdog":
        # An unreadable body is "unreachable", never green.
        health = None
        if a.health and a.exit_code == 0:
            try:
                with open(a.health) as f:
                    health = json.load(f)
            except (OSError, ValueError):
                health = None
        print(watchdog(gh, health, a.exit_code, a.run_url))
    else:
        print(json.dumps(sweep(gh, max_closes=a.max_closes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
