#!/usr/bin/env python3
"""The board: durable memory, delta detection, and the one thing this tool fires.

Shell #39's finding was that six months of better instruments were pointed at an
unchanged engine — every shell ends "names an actuator per lane, FIRES NOTHING",
and a seventh read-only shell would BE the bug. So this one acts, within the hard
line that has held since the autonomy core was written: it never merges, deploys,
executes a plan, or writes to main. It maintains a board and keeps exactly one
GitHub issue current.

Two properties make it useful rather than noisy:

**Memory.** State lives on a dedicated ``qa-superuser-state`` branch, off main and
off the backend it watches. Without memory every run re-reports the same findings
and the board becomes wallpaper; with it, a finding can be NEW, STILL RED,
REGRESSED, RECOVERED or FLAPPING — and "regressed" is the one that deserves to
interrupt someone.

**Silence.** The issue body is rewritten every run so it always shows current
truth, but a COMMENT — the thing that notifies — is posted only when something
actually changed. Every watcher on this platform learned that lesson the same
way: an alarm that fires on every tick is an alarm nobody reads.
"""
from __future__ import annotations

import base64
import datetime
import json
import subprocess

from . import config as C
from .finding import BLIND, CRITICAL, GAUGE, PASS, RED, Finding
from .http import QA_UA_TOKEN

NOW = datetime.datetime.now(datetime.timezone.utc)

# Delta classes, most alarming first.
NEW = "NEW"
REGRESSED = "REGRESSED"      # was passing, now failing — the interrupt-worthy one
STILL = "STILL"              # failing before, failing now
RECOVERED = "RECOVERED"      # was failing, now passing
FLAPPING = "FLAPPING"        # crossed the pass/fail line repeatedly
UNCHANGED = "UNCHANGED"

FLAP_THRESHOLD = 4   # transitions retained in history before we call it flapping


def _gh(args: list[str], input_text: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(["gh"] + args, capture_output=True, text=True,
                           timeout=90, input=input_text)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


# ── durable state on its own branch ─────────────────────────────────────────
def load_state() -> dict:
    """Read prior state from the state branch.

    A missing file is a first run, not an error. An UNREADABLE file, though, is
    reported by the caller as blindness rather than silently treated as empty —
    otherwise a transient API failure would make every existing finding look NEW
    and fire a full-board alarm.
    """
    rc, out = _gh(["api", f"repos/{C.GH_REPO}/contents/{C.STATE_PATH}",
                   "-f", f"ref={C.STATE_BRANCH}"])
    if rc != 0:
        if "404" in out or "Not Found" in out:
            return {"first_run": True, "findings": {}, "runs": 0}
        return {"unreadable": out[:200], "findings": {}, "runs": 0}
    try:
        payload = json.loads(out)
        raw = base64.b64decode(payload.get("content", "")).decode()
        state = json.loads(raw)
        state["_sha"] = payload.get("sha")
        state.setdefault("findings", {})
        state.setdefault("runs", 0)
        return state
    except Exception as e:  # noqa: BLE001
        return {"unreadable": f"{type(e).__name__}: {e}", "findings": {}, "runs": 0}


def save_state(state: dict) -> bool:
    """Write state back to the state branch. Never touches main."""
    state = {k: v for k, v in state.items() if not k.startswith("_")}
    body = json.dumps(state, indent=1, sort_keys=True, default=str)
    content = base64.b64encode(body.encode()).decode()
    args = ["api", "--method", "PUT",
            f"repos/{C.GH_REPO}/contents/{C.STATE_PATH}",
            "-f", f"message=qa-superuser board {NOW.isoformat()}",
            "-f", f"content={content}",
            "-f", f"branch={C.STATE_BRANCH}"]
    sha = state.get("_sha") or _current_sha()
    if sha:
        args += ["-f", f"sha={sha}"]
    rc, out = _gh(args)
    if rc != 0:
        print(f"state write failed (non-fatal): {out[:300]}")
    return rc == 0


def _current_sha() -> str | None:
    rc, out = _gh(["api", f"repos/{C.GH_REPO}/contents/{C.STATE_PATH}",
                   "-f", f"ref={C.STATE_BRANCH}", "--jq", ".sha"])
    return out.strip() if rc == 0 and out.strip() else None


def ensure_state_branch() -> None:
    """Create the state branch as an orphan if it does not exist yet."""
    rc, _ = _gh(["api", f"repos/{C.GH_REPO}/branches/{C.STATE_BRANCH}"])
    if rc == 0:
        return
    rc, out = _gh(["api", f"repos/{C.GH_REPO}/git/refs/heads/main", "--jq", ".object.sha"])
    if rc != 0:
        print(f"cannot resolve main to seed state branch: {out[:200]}")
        return
    base = out.strip()
    rc, out = _gh(["api", "--method", "POST", f"repos/{C.GH_REPO}/git/refs",
                   "-f", f"ref=refs/heads/{C.STATE_BRANCH}", "-f", f"sha={base}"])
    print(f"state branch {C.STATE_BRANCH}: "
          + ("created" if rc == 0 else f"could not create ({out[:160]})"))


# ── delta ───────────────────────────────────────────────────────────────────
def _failing(rec: dict) -> bool:
    return rec.get("verdict") == RED and rec.get("severity") in (CRITICAL, "major")


def classify(run: dict, state: dict) -> dict[str, str]:
    """Assign every finding in this run a delta class against prior state."""
    prior = state.get("findings", {})
    out: dict[str, str] = {}
    for f in run["findings"]:
        key = f["key"]
        was = prior.get(key)
        now_failing = f["verdict"] == RED and f["severity"] in (CRITICAL, "major")
        if was is None:
            out[key] = NEW if now_failing else UNCHANGED
            continue
        was_failing = bool(was.get("failing"))
        transitions = int(was.get("transitions", 0))
        if now_failing and not was_failing:
            out[key] = FLAPPING if transitions >= FLAP_THRESHOLD else REGRESSED
        elif not now_failing and was_failing:
            out[key] = FLAPPING if transitions >= FLAP_THRESHOLD else RECOVERED
        elif now_failing:
            out[key] = STILL
        else:
            out[key] = UNCHANGED
    return out


def merge_state(run: dict, state: dict, deltas: dict[str, str]) -> dict:
    """Fold this run into durable state, keeping first-seen and flap counts."""
    prior = state.get("findings", {})
    merged: dict[str, dict] = {}
    for f in run["findings"]:
        key = f["key"]
        was = prior.get(key, {})
        failing = f["verdict"] == RED and f["severity"] in (CRITICAL, "major")
        transitions = int(was.get("transitions", 0))
        if was and bool(was.get("failing")) != failing:
            transitions += 1
        merged[key] = {
            "title": f["title"],
            "surface": f["surface"],
            "seat": f["seat"],
            "verdict": f["verdict"],
            "severity": f["severity"],
            "failing": failing,
            "first_seen": was.get("first_seen") or run["generated_at"],
            "last_seen": run["generated_at"],
            "failing_since": (was.get("failing_since") or run["generated_at"])
            if failing else None,
            "transitions": transitions,
            "value": f.get("value"),
        }
    return {
        "updated_at": run["generated_at"],
        "runs": int(state.get("runs", 0)) + 1,
        "last_counts": run["counts"],
        "canary_fired": run["canary_fired"],
        "findings": merged,
        "_sha": state.get("_sha"),
    }


# ── rendering ───────────────────────────────────────────────────────────────
_ICON = {RED: "🔴", BLIND: "⚪", GAUGE: "📊", PASS: "🟢"}
_DELTA_NOTE = {
    NEW: "**NEW**", REGRESSED: "**REGRESSED**", STILL: "still red",
    RECOVERED: "recovered", FLAPPING: "**FLAPPING**", UNCHANGED: "",
}


def render(run: dict, state: dict, deltas: dict[str, str]) -> str:
    c = run["counts"]
    lines = [
        "## DC Hub QA super-user — outside-in board",
        "",
        f"_Run {run['generated_at']} against `{run['edge']}` "
        f"(run #{state.get('runs', 1)})._",
        "",
    ]

    if not run["canary_fired"]:
        lines += [
            "> ⚠️ **THIS RUN IS NOT TRUSTWORTHY.** The must-fail control did not "
            "fire, so the harness could not be shown capable of reporting a "
            "failure. Every PASS has been demoted to *unobserved*. Fix the "
            "harness before reading anything below as reassurance.",
            "",
        ]

    lines += [
        f"**{c['red']} red** · {c['blind']} unobserved · {c['gauge']} gauges · "
        f"{c['pass']} passing  —  {c['critical']} critical",
        "",
        "Verdicts are observations from a real caller's seat. `⚪ unobserved` "
        "means the probe could not look — it is **never** a failure. `📊 gauge` "
        "means a number is reported because no threshold exists that the platform "
        "itself defines.",
        "",
    ]

    reds = [f for f in run["findings"] if f["verdict"] == RED]
    if reds:
        lines += ["### Red", ""]
        for f in reds:
            note = _DELTA_NOTE.get(deltas.get(f["key"], ""), "")
            since = (state.get("findings", {}).get(f["key"], {}) or {}).get("failing_since")
            age = ""
            if since:
                try:
                    d = (NOW - datetime.datetime.fromisoformat(since)).days
                    age = f" · failing {d}d" if d >= 1 else ""
                except Exception:  # noqa: BLE001
                    pass
            lines += [
                f"#### {_ICON[RED]} {f['title']} {note}",
                f"- **Surface** `{f['surface']}` · **seat** `{f['seat']}` · "
                f"**severity** {f['severity']}{age}",
                f"- **Observed**: {f['evidence']}",
                f"- **Measured from**: {f['basis']}",
                f"- **Red when**: {f['red_when']}",
            ]
            if f.get("remedy"):
                lines.append(f"- **Why it matters**: {f['remedy']}")
            lines.append("")

    gauges = [f for f in run["findings"] if f["verdict"] == GAUGE]
    if gauges:
        lines += ["### Gauges (tracked, no pass/fail claim)", "",
                  "| metric | value | observed |", "| --- | --- | --- |"]
        for f in gauges:
            ev = f["evidence"].replace("|", "\\|")[:150]
            lines.append(f"| {f['title']} | `{f.get('value')}` | {ev} |")
        lines.append("")

    unobs = [f for f in run["findings"] if f["verdict"] == BLIND]
    if unobs:
        lines += ["### Unobserved (not failures)", ""]
        for f in unobs:
            lines.append(f"- `{f['surface']}`/`{f['seat']}` — {f['title']}: "
                         f"{f['evidence']}")
        lines.append("")

    passing = [f for f in run["findings"] if f["verdict"] == PASS]
    if passing:
        lines += ["<details><summary>"
                  f"{len(passing)} passing check(s)</summary>", ""]
        for f in passing:
            lines.append(f"- {_ICON[PASS]} `{f['surface']}` {f['title']} — "
                         f"{f['evidence'][:160]}")
        lines += ["", "</details>", ""]

    lines += [
        "---",
        "*Probe traffic self-identifies as "
        f"`{QA_UA_TOKEN}` — "
        "exclude it from reach and usage metrics by **User-Agent**, not by "
        "platform tag (the MCP server overwrites the platform field).*",
        "",
        "*This board never merges, deploys or executes. It reports and keeps one "
        "issue current.*",
    ]
    return "\n".join(lines)


def changed_lines(run: dict, deltas: dict[str, str]) -> list[str]:
    """The delta summary — the only thing that justifies a notification."""
    out = []
    by_key = {f["key"]: f for f in run["findings"]}
    for key, d in deltas.items():
        if d in (NEW, REGRESSED, RECOVERED, FLAPPING):
            f = by_key.get(key, {})
            out.append(f"- **{d}** — {f.get('title', key)} "
                       f"(`{f.get('surface')}`/`{f.get('seat')}`)")
    return out


# ── the one actuation ───────────────────────────────────────────────────────
def upsert_issue(body: str, deltas: dict, run: dict) -> None:
    """Keep exactly ONE issue current; comment only when something changed."""
    changes = changed_lines(run, deltas)
    title = (f"[qa-superuser] {run['counts']['red']} red across the caller-facing "
             f"surfaces")

    _gh(["label", "create", C.ISSUE_LABEL, "-R", C.GH_REPO, "--color", "0e8a16",
         "--description", "outside-in QA super-user board"])

    rc, out = _gh(["issue", "list", "-R", C.GH_REPO, "--state", "open",
                   "--label", C.ISSUE_LABEL, "--json", "number", "--jq",
                   ".[0].number"])
    number = out.strip() if rc == 0 and out.strip().isdigit() else None

    if number:
        _gh(["issue", "edit", number, "-R", C.GH_REPO, "--title", title,
             "--body-file", "-"], input_text=body)
        if changes:
            comment = ("### Change since the last run\n\n" + "\n".join(changes)
                       + f"\n\n_Board updated in place above ({run['generated_at']})._")
            _gh(["issue", "comment", number, "-R", C.GH_REPO,
                 "--body-file", "-"], input_text=comment)
            print(f"issue #{number} updated + commented ({len(changes)} change(s))")
        else:
            print(f"issue #{number} body refreshed; no change -> stayed silent")
    else:
        rc, out = _gh(["issue", "create", "-R", C.GH_REPO, "--title", title,
                       "--label", C.ISSUE_LABEL, "--body-file", "-"],
                      input_text=body)
        print(f"opened qa-superuser issue: {out.strip()[:120]}")


def actuate(run: dict) -> dict:
    """Load state, classify, render, publish, persist. Returns the board dict."""
    ensure_state_branch()
    state = load_state()
    if state.get("unreadable"):
        # Treat unreadable prior state as blindness about HISTORY: publish the
        # board, but do not claim anything is NEW or REGRESSED, because we cannot
        # know. Firing a full-board alarm on a transient API failure is exactly
        # the false-alarm class the dead-man watcher had to be taught to avoid.
        print(f"prior state unreadable ({state['unreadable']}) — "
              "publishing without delta claims")
        deltas = {f["key"]: UNCHANGED for f in run["findings"]}
    else:
        deltas = classify(run, state)

    body = render(run, state, deltas)
    if C.DRY_RUN:
        print(body)
        print("\n(QA_DRY_RUN=1 — issue and state untouched)")
        return {"body": body, "deltas": deltas}

    upsert_issue(body, deltas, run)
    save_state(merge_state(run, state, deltas))
    return {"body": body, "deltas": deltas}
