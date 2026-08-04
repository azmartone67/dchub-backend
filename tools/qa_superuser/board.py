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
RECOVERED = "RECOVERED"      # was failing, now OBSERVED passing
FLAPPING = "FLAPPING"        # crossed the pass/fail line repeatedly
WENT_BLIND = "WENT_BLIND"    # was failing, now unobservable — NOT a recovery
DISAPPEARED = "DISAPPEARED"  # was failing, absent from this run — NOT a recovery
UNCHANGED = "UNCHANGED"

# Observation states for the delta layer. Deliberately three, not two: see
# observed_state() for why collapsing UNOBSERVED into "not failing" published
# false recoveries.
FAILING = "failing"
PASSING = "passing"
UNOBSERVED = "unobserved"

FLAP_THRESHOLD = 4   # transitions retained in history before we call it flapping

# A finding absent from this many consecutive runs is finally forgotten. Long
# enough that a crashed probe or a transient blind spell never loses history,
# short enough that a deliberately retired check does not linger forever.
ABSENT_RUNS_BEFORE_FORGET = 20


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
    # ★ ref in the query string — see _current_sha(). With `-f ref=...` gh issues
    # a POST, which fails, and the failure text happened to contain "404", so this
    # took the first-run branch on EVERY run: no deltas were ever real, every red
    # was re-announced as NEW, and the board looked like it was working.
    rc, out = _gh(["api", "--method", "GET",
                   f"repos/{C.GH_REPO}/contents/{C.STATE_PATH}"
                   f"?ref={C.STATE_BRANCH}"])
    if rc != 0:
        # Only a genuine "this file does not exist yet" is a first run. Match the
        # documented 404 shape rather than a bare "404" substring, which also
        # appears in unrelated error text.
        if "Not Found" in out or "No commit found" in out:
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


def save_state(state: dict) -> tuple[bool, str]:
    """Write state back to the state branch. Never touches main.

    Returns (ok, detail) — the caller PUBLISHES this on the board. A memory layer
    that fails silently is worse than no memory layer: the board keeps rendering,
    keeps commenting, and every run quietly believes it is the first one.
    """
    # ★ Read the sha BEFORE stripping underscore keys. The original stripped
    # first and then read `_sha` from the stripped dict, so it was ALWAYS None —
    # the update degraded to a create, which GitHub rejects with
    # 422 "sha wasn't supplied" once the file exists.
    sha = state.get("_sha") or _current_sha()
    payload = {k: v for k, v in state.items() if not k.startswith("_")}
    body = json.dumps(payload, indent=1, sort_keys=True, default=str)
    content = base64.b64encode(body.encode()).decode()
    args = ["api", "--method", "PUT",
            f"repos/{C.GH_REPO}/contents/{C.STATE_PATH}",
            "-f", f"message=qa-superuser board {NOW.isoformat()}",
            "-f", f"content={content}",
            "-f", f"branch={C.STATE_BRANCH}"]
    if sha:
        args += ["-f", f"sha={sha}"]
    rc, out = _gh(args)
    if rc != 0:
        detail = out.strip()[:300]
        # ::error:: so it is impossible to miss in the Actions UI, and the board
        # carries it too (see actuate) — the previous "non-fatal" print scrolled
        # past in a 200-line log and the memory layer was dead for every run.
        print(f"::error::qa-superuser state write FAILED — the board has no "
              f"memory this run: {detail}")
        return False, detail
    return True, f"wrote {len(body)}b to {C.STATE_BRANCH}:{C.STATE_PATH}"


def _current_sha() -> str | None:
    """Current blob sha of the state file, or None if it does not exist yet.

    ★ The ref goes in the QUERY STRING. `gh api <path> -f ref=<branch>` does NOT
    do what it looks like: gh switches the method to POST as soon as any
    -f/--field is present, so this became a POST to the contents endpoint and
    failed. Same trap applies to load_state below.
    """
    rc, out = _gh(["api", "--method", "GET",
                   f"repos/{C.GH_REPO}/contents/{C.STATE_PATH}"
                   f"?ref={C.STATE_BRANCH}", "--jq", ".sha"])
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


def observed_state(f: dict) -> str:
    """FAILING / PASSING / UNOBSERVED for one finding.

    ★ THE DELTA LAYER NEEDS THREE STATES, NOT TWO. The original asked only
    "is it failing?", which silently answered NO for a BLIND finding — so a RED
    that became unobservable was classified RECOVERED and the board posted
    "**RECOVERED**" at the exact moment the probe went blind. Good news is the
    one output nobody investigates.

    That is rule 1 (BLIND is not RED) broken one layer above where it is
    enforced: finding.counts_as_failure is correct for COUNTING, and reusing its
    two-way logic for COMPARING is what introduced the bug.
    """
    if f["verdict"] == BLIND:
        return UNOBSERVED
    if f["verdict"] == RED and f["severity"] in (CRITICAL, "major"):
        return FAILING
    return PASSING


def classify(run: dict, state: dict) -> dict[str, str]:
    """Assign every finding a delta class against prior state.

    Covers three cases the first version did not: a finding that went
    UNOBSERVED, one that VANISHED from the run entirely, and the difference
    between "we saw it pass" and "we could not look".
    """
    prior = state.get("findings", {})
    out: dict[str, str] = {}
    seen: set[str] = set()

    for f in run["findings"]:
        key = f["key"]
        seen.add(key)
        was = prior.get(key)
        now = observed_state(f)

        if was is None:
            out[key] = NEW if now == FAILING else UNCHANGED
            continue

        was_failing = bool(was.get("failing"))

        if now == UNOBSERVED:
            # We did not look, so nothing changed about the platform. Say so —
            # and if we lost sight of a LIVE red, that is worth announcing,
            # because an unwatched failure reads as an absent one.
            out[key] = WENT_BLIND if was_failing else UNCHANGED
            continue

        now_failing = now == FAILING
        transitions = int(was.get("transitions", 0))
        if now_failing and not was_failing:
            out[key] = FLAPPING if transitions >= FLAP_THRESHOLD else REGRESSED
        elif not now_failing and was_failing:
            out[key] = FLAPPING if transitions >= FLAP_THRESHOLD else RECOVERED
        elif now_failing:
            out[key] = STILL
        else:
            out[key] = UNCHANGED

    # A finding that VANISHED from the run is not a fix. The original dropped it
    # from state entirely, so a red that stopped being produced — a crashed
    # probe, a renamed key, a skipped seat — simply disappeared from the board,
    # which looks exactly like a repair.
    for key, was in prior.items():
        if key in seen:
            continue
        out[key] = DISAPPEARED if was.get("failing") else UNCHANGED
    return out


def merge_state(run: dict, state: dict, deltas: dict[str, str]) -> dict:
    """Fold this run into durable state, keeping first-seen and flap counts."""
    prior = state.get("findings", {})
    merged: dict[str, dict] = {}
    for f in run["findings"]:
        key = f["key"]
        was = prior.get(key, {})
        now = observed_state(f)

        if now == UNOBSERVED:
            # ★ Carry the prior verdict forward UNCHANGED. Writing failing=False
            # here would let one blind run erase a live red from history, and the
            # NEXT run would then see pass -> red and announce it as REGRESSED,
            # resetting failing_since and hiding how long it had really been red.
            carried = dict(was) if was else {}
            carried.update({
                "title": f["title"], "surface": f["surface"], "seat": f["seat"],
                "last_seen": run["generated_at"],
                "unobserved_at": run["generated_at"],
            })
            carried.setdefault("failing", False)
            carried.setdefault("first_seen", run["generated_at"])
            carried.setdefault("transitions", 0)
            merged[key] = carried
            continue

        failing = now == FAILING
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

    # ★ Carry forward findings this run did not produce at all. Dropping them
    # deleted the history of any red whose probe crashed or whose key changed,
    # and on the board that is indistinguishable from a fix. They are retained,
    # marked absent, and aged out only after they have been gone a long time —
    # so a genuinely retired check does not accumulate forever.
    for key, was in prior.items():
        if key in merged:
            continue
        absent = int(was.get("absent_runs", 0)) + 1
        if absent > ABSENT_RUNS_BEFORE_FORGET:
            continue
        carried = dict(was)
        carried["absent_runs"] = absent
        carried["last_absent_at"] = run["generated_at"]
        merged[key] = carried

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
    RECOVERED: "**RECOVERED**", FLAPPING: "**FLAPPING**", UNCHANGED: "",
    WENT_BLIND: "**LOST SIGHT OF** (was red, now unobservable — not a fix)",
    DISAPPEARED: "**VANISHED** (was red, not produced this run — not a fix)",
}


def render(run: dict, state: dict, deltas: dict[str, str],
           memory: tuple[bool, str] = (True, "")) -> str:
    c = run["counts"]
    lines = [
        "## DC Hub QA super-user — outside-in board",
        "",
        f"_Run {run['generated_at']} against `{run['edge']}` "
        f"(run #{state.get('runs', 1)})._",
        "",
    ]

    # A board that cannot remember cannot tell NEW from STILL RED, and will
    # re-announce every finding forever. Say so ON the board — the failure that
    # only ever reached a log line went unnoticed for every run.
    if not memory[0]:
        lines += [
            "> ⚠️ **THIS BOARD HAS NO MEMORY.** Writing durable state to "
            f"`{C.STATE_BRANCH}` failed: `{memory[1]}`. Deltas below "
            "(NEW / REGRESSED / RECOVERED) are unreliable, and the next run will "
            "believe it is the first one. Fix persistence before trusting any "
            "trend on this board.",
            "",
        ]

    if state.get("unreadable"):
        lines += [
            "> ⚠️ **Prior state was unreadable this run**, so no NEW / REGRESSED "
            "claims are made below — only current verdicts. This is deliberate: "
            "a transient API failure must not fire a whole-board 'everything is "
            "new' alarm.",
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


def changed_lines(run: dict, deltas: dict[str, str],
                  state: dict | None = None) -> list[str]:
    """The delta summary — the only thing that justifies a notification.

    ★ Falls back to PRIOR state for the title and surface. A DISAPPEARED finding
    is by definition not in this run, so looking it up only in the run printed a
    raw hash key and `None`/`None` — the least legible line on the board attached
    to the most easily-missed event.
    """
    out = []
    by_key = {f["key"]: f for f in run["findings"]}
    prior = (state or {}).get("findings", {})
    for key, d in deltas.items():
        if d not in (NEW, REGRESSED, RECOVERED, FLAPPING, WENT_BLIND,
                     DISAPPEARED):
            continue
        f = by_key.get(key) or prior.get(key) or {}
        note = _DELTA_NOTE.get(d) or f"**{d}**"
        out.append(f"- {note} — {f.get('title', key)} "
                   f"(`{f.get('surface', '?')}`/`{f.get('seat', '?')}`)")
    return out


# ── the one actuation ───────────────────────────────────────────────────────
def upsert_issue(body: str, deltas: dict, run: dict,
                 state: dict | None = None) -> None:
    """Keep exactly ONE issue current; comment only when something changed."""
    changes = changed_lines(run, deltas, state)
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

    if C.DRY_RUN:
        print(render(run, state, deltas, memory=(True, "dry run — not written")))
        print("\n(QA_DRY_RUN=1 — issue and state untouched)")
        return {"body": None, "deltas": deltas}

    # ★ PERSIST FIRST, then render, so the board can REPORT whether its own
    # memory survived. The original order published the board and wrote state
    # afterwards, so a failed write could only ever appear in the log — which is
    # exactly how the memory layer stayed dead while the board looked healthy.
    if state.get("unreadable"):
        # ★ DO NOT WRITE. `state["findings"]` is empty because the READ failed,
        # not because there is no history — merging into it and saving would
        # overwrite a real board (runs, first_seen, failing_since, flap counts)
        # with a single run's worth of data. Losing history to a transient API
        # blip is worse than skipping one write.
        memory = (False, f"prior state unreadable ({state['unreadable']}) — "
                         "skipped the write rather than overwrite real history")
        print(f"::warning::qa-superuser skipped its state write: {memory[1]}")
    else:
        memory = save_state(merge_state(run, state, deltas))
    body = render(run, state, deltas, memory=memory)
    upsert_issue(body, deltas, run, state)
    return {"body": body, "deltas": deltas, "memory_ok": memory[0]}
