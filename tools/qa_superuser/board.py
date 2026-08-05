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
import os
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
    if f["verdict"] in (BLIND, GAUGE):
        # A GAUGE makes NO pass/fail claim by construction — that is its whole
        # definition (rule 3). So it is not evidence of passing, and a check that
        # flips RED -> GAUGE has not been fixed; it has stopped asserting.
        # Treating GAUGE as PASSING published a live false "**RECOVERED** - No
        # numeric quota meter exposed to an anonymous caller", where the real
        # event was that the probe's trial state changed and the check simply
        # stopped making a claim. Same family as the BLIND bug, one case over.
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
            "flap_announced": bool(was.get("flap_announced"))
            or deltas.get(key) == FLAPPING,
            # Consecutive runs this finding has reported as a GAUGE. Resets the
            # moment it asserts again, so a check that flips RED<->GAUGE with
            # the runner's trial state never accumulates a retraction.
            "gauge_runs": (int(was.get("gauge_runs") or 0) + 1
                           if f["verdict"] == GAUGE else 0),
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
    WENT_BLIND: "**NOT RE-CONFIRMED** (was red; this run made no pass/fail claim — not a fix)",
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
            flaps = int((state.get("findings", {}).get(f["key"], {}) or {})
                        .get("transitions", 0))
            unstable = (" · ⚠️ **UNSTABLE** — has crossed the pass/fail line "
                        f"{flaps}x; treat a single reading with care"
                        if flaps >= FLAP_THRESHOLD else "")
            lines += [
                f"#### {_ICON[RED]} {f['title']} {note}{unstable}",
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
        was = prior.get(key) or {}
        # ★ Announce FLAPPING ONCE. A check that genuinely oscillates — the anon
        # quota probe flips with the runner IP's trial state — would otherwise
        # notify on every single flip, which is the "alarm nobody reads" failure
        # this board is built to avoid. Its current verdict stays visible in the
        # rendered body every run; the NOTIFICATION fires once, when we learn it
        # is unstable.
        if d == FLAPPING and was.get("flap_announced"):
            continue
        f = by_key.get(key) or was or {}
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
    body = BOARD_MARKER + "\n" + body

    _gh(["label", "create", C.ISSUE_LABEL, "-R", C.GH_REPO, "--color", "0e8a16",
         "--description", "outside-in QA super-user board"])

    number = _find_board_issue()

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


def beat_dashboard(run: dict, merged: dict) -> tuple[bool, str]:
    """Post this run to the backend so the operator has a browser board.

    ★ TARGETS THE RAILWAY ORIGIN, NEVER THE CF EDGE. The zone's 15s route timeout
    kills admin POSTs through dchub.cloud — verified on the same request: edge
    503, origin 200. Every brain workflow uses the origin for this reason.

    ★ FAILS LOUDLY BUT NEVER FATALLY. By the time this runs the GitHub issue is
    already written, and the issue — not this backend — is the authoritative
    board. A dchub outage must not turn into a failed probe run, because the
    probe exists precisely to observe dchub outages.

    Findings are enriched with `failing_since` and `transitions` from the merged
    state so the page can show "failing 3d" and "UNSTABLE 6x" without keeping a
    second copy of the history.
    """
    admin = (C.ADMIN_KEY or "").strip()
    if not admin:
        return False, "no admin key in the environment — dashboard not updated"

    hist = merged.get("findings", {})
    existing = open_issue_numbers()
    enriched = []
    for f in run["findings"]:
        rec = dict(f)
        was = hist.get(f["key"]) or {}
        rec["failing_since"] = was.get("failing_since")
        rec["transitions"] = was.get("transitions", 0)
        # ★ So the dashboard can offer "view the issue you already opened"
        # instead of minting a second one. The button had no dedup: clicking it
        # twice for the same finding produced #2203 and #2209 for one defect,
        # and a duplicate-issue backlog is the same disease as an unclosed one.
        rec["issue_number"] = existing.get(f["key"])
        enriched.append(rec)

    payload = json.dumps({
        "generated_at": run["generated_at"],
        "canary_fired": run["canary_fired"],
        "edge": run["edge"],
        "counts": run["counts"],
        "findings": enriched,
        "memory_ok": run.get("memory_ok"),
    }).encode()

    url = f"{C.ORIGIN}/api/v1/admin/qa-superuser/beat"
    try:
        import requests
        r = requests.post(url, data=payload, timeout=30, headers={
            "Content-Type": "application/json",
            "X-Admin-Key": admin,
            "User-Agent": QA_UA_TOKEN + "/1.0",
        })
    except Exception as e:  # noqa: BLE001
        detail = f"{type(e).__name__}: {e}"
        print(f"::warning::qa-superuser dashboard beat failed (non-fatal, the "
              f"GitHub issue is authoritative): {detail}")
        return False, detail
    if r.status_code >= 400:
        detail = f"HTTP {r.status_code} {r.text[:160]}"
        print(f"::warning::qa-superuser dashboard beat rejected (non-fatal): "
              f"{detail}")
        return False, detail
    return True, f"posted to {url}"


# The marker the dashboard's "Open an issue" button writes into every per-finding
# issue body. It is what lets a later run recognise its own issue.
ISSUE_KEY_MARKER = "finding key "

# ★★ THE ROLLING BOARD'S IDENTITY. An invisible marker in its body, because
# "the first open issue carrying our label" is NOT an identity once anything
# else can wear that label.
#
# It shipped as `gh issue list --label qa-superuser --json number --jq
# '.[0].number'`, which was correct while exactly one such issue existed. Then
# the dashboard's "Open an issue" button started minting per-finding issues with
# the SAME label — gh returns newest-first, so the next run overwrote the
# NEWEST per-finding issue with the whole board, destroying the operator's issue
# title and body. Observed live: #2205 ("EDGE is caching 1 path(s) that declare
# no-store") was rewritten as "3 red across the caller-facing surfaces".
#
# The marker is an HTML comment, so it is invisible in the rendered issue but
# unambiguous to match on, and it cannot collide with a per-finding issue.
BOARD_MARKER = "<!-- qa-superuser:rolling-board -->"


def _find_board_issue() -> str | None:
    """The rolling board issue, identified by its marker — never by position.

    Falls back to the OLDEST labelled issue that predates the marker so an
    already-running board is adopted rather than duplicated on the first deploy
    of this fix; that fallback additionally requires the board's own title
    shape, so a per-finding issue can never be adopted by mistake.
    """
    rc, out = _gh(["issue", "list", "-R", C.GH_REPO, "--state", "open",
                   "--label", C.ISSUE_LABEL, "--limit", "100",
                   "--json", "number,body,title"])
    if rc != 0:
        return None
    try:
        issues = json.loads(out or "[]")
    except Exception:  # noqa: BLE001
        return None

    for iss in issues:
        if BOARD_MARKER in (iss.get("body") or ""):
            return str(iss.get("number"))

    # Pre-marker adoption: the board is the oldest labelled issue whose title is
    # the board's, and which carries NO per-finding key.
    candidates = [i for i in issues
                  if "red across the caller-facing" in (i.get("title") or "")
                  and ISSUE_KEY_MARKER not in (i.get("body") or "")]
    if candidates:
        return str(min(candidates, key=lambda i: i.get("number", 0)).get("number"))
    return None


def open_issue_numbers() -> dict[str, int]:
    """finding key -> the OLDEST open per-finding issue for it.

    Oldest rather than newest so that when duplicates already exist the board
    points at the original — the one carrying the discussion — and the copies
    can be closed without the link moving.

    Returns {} on any read failure: the dashboard then falls back to offering a
    new issue, which is the safe direction. Offering to create one that already
    exists is a duplicate; suppressing the button on a bad read would hide the
    only way to file.
    """
    rc, out = _gh(["issue", "list", "-R", C.GH_REPO, "--state", "open",
                   "--label", C.ISSUE_LABEL, "--limit", "100",
                   "--json", "number,body"])
    if rc != 0:
        return {}
    try:
        issues = json.loads(out or "[]")
    except Exception:  # noqa: BLE001
        return {}
    found: dict[str, int] = {}
    for iss in sorted(issues, key=lambda i: i.get("number", 0)):
        body = iss.get("body") or ""
        if ISSUE_KEY_MARKER not in body:
            continue
        key = body.split(ISSUE_KEY_MARKER, 1)[1].split("\n", 1)[0].strip()
        if key:
            found.setdefault(key, iss.get("number"))
    return found


def close_resolved_issues(run: dict, deltas: dict[str, str]) -> list[str]:
    """Close the per-finding issues whose finding is now OBSERVED passing.

    ★ WHY THE BOARD CLOSES THEM AND NOT A HUMAN. The board is the only thing
    that can verify a fix FROM THE CALLER'S SEAT — the same seat that found the
    defect. A human closing the issue is asserting "I believe this is fixed";
    the board closing it is asserting "I asked the product again, from the same
    seat, with the same check, and it now answers correctly", and it attaches
    that evidence. Leaving them open forever was the alternative, and a board
    that opens issues nobody closes becomes the backlog nobody works — which is
    the failure this whole tool was built in response to.

    ★ SAFE BY CONSTRUCTION, because of the delta model rather than a promise:
    RECOVERED already requires an OBSERVED pass. A finding that went
    unobservable is WENT_BLIND, one that vanished from the run is DISAPPEARED,
    and one that stopped asserting is also WENT_BLIND — none of them can reach
    this function. So an issue cannot be closed because the probe stopped
    looking, only because it looked and the answer was right.

    Still nothing that merges, deploys or executes: closing an issue this tool
    itself opened is the same class of act as opening and updating one.
    Reversible in one click, and the proof is in the closing comment.
    """
    if os.environ.get("QA_SUPERUSER_NO_CLOSE"):
        return []
    # ★★ CLOSE ON CURRENT STATE, NOT ON THE TRANSITION.
    #
    # This used to close only findings whose delta was RECOVERED — the moment a
    # red turned green. That leaves ORPHANS: an issue opened by a human WHILE a
    # finding was red, for a finding that had already flipped back green in an
    # earlier run, never sees a RECOVERED event again and stays open forever.
    # Observed exactly that with #2228, filed during a stale board reading after
    # the underlying defect was already fixed.
    #
    # Keying on the CURRENT verdict makes the closer idempotent: every run
    # re-checks every open issue against what the probe observes right now, so
    # an issue filed at any point is closed on the next run after its finding
    # passes, regardless of when the transition happened.
    #
    # ★ The safety property is UNCHANGED and, if anything, tightened: this
    # requires an explicit `verdict == PASS` — an OBSERVED pass. BLIND (could
    # not look), GAUGE (makes no claim), a finding absent from the run, and any
    # RED are all excluded, so "the probe stopped looking" still cannot close a
    # defect. It is the same rule, applied to state instead of to an edge.
    passing = [f["key"] for f in run["findings"] if f.get("verdict") == PASS]
    if not passing:
        return []
    recovered = passing

    rc, out = _gh(["issue", "list", "-R", C.GH_REPO, "--state", "open",
                   "--label", C.ISSUE_LABEL, "--limit", "100",
                   "--json", "number,body,title"])
    if rc != 0:
        print(f"::warning::qa-superuser could not list issues to close: {out[:160]}")
        return []
    try:
        issues = json.loads(out or "[]")
    except Exception:  # noqa: BLE001
        return []

    by_key = {f["key"]: f for f in run["findings"]}
    closed = []
    for key in recovered:
        f = by_key.get(key)
        if not f:
            continue
        marker = ISSUE_KEY_MARKER + key
        for iss in issues:
            # Exact marker match. The rolling board issue carries no finding key,
            # so it can never be matched here — but require the marker rather
            # than relying on that, because closing the board itself would take
            # the whole surface down.
            if marker not in (iss.get("body") or ""):
                continue
            num = str(iss.get("number"))
            comment = (
                "### Verified fixed from the caller's seat\n\n"
                f"The QA super-user re-ran the same check from the same "
                f"`{f.get('seat')}` seat and it now passes.\n\n"
                f"**Observed:** {f.get('evidence')}\n\n"
                f"**Measured from:** {f.get('basis')}\n\n"
                f"_Closed automatically by the run at {run['generated_at']}. "
                "This is an observed pass, not an absence of evidence — a check "
                "that merely stopped being observable is reported as unobserved "
                "and would NOT have closed this._")
            _gh(["issue", "comment", num, "-R", C.GH_REPO, "--body-file", "-"],
                input_text=comment)
            crc, cout = _gh(["issue", "close", num, "-R", C.GH_REPO,
                             "--reason", "completed"])
            if crc == 0:
                closed.append(f"#{num} ({f.get('title')})")
                print(f"closed issue #{num} — {key} verified fixed")
            else:
                print(f"::warning::could not close #{num}: {cout[:140]}")

    closed += _withdraw_retracted_issues(run, issues)
    return closed


# A finding must have been a GAUGE for this many CONSECUTIVE runs before its
# issue is withdrawn. One run is a flap; a sustained retraction is a decision.
WITHDRAW_AFTER_GAUGE_RUNS = 6


def _withdraw_retracted_issues(run: dict, issues: list) -> list[str]:
    """Close issues whose finding was WITHDRAWN, not fixed.

    ★ THE THIRD OUTCOME. An issue closes when its check passes again. But a
    check can also stop making a claim — turn into a GAUGE because the assertion
    was wrong, or because no threshold the platform defines exists to fail
    against. That finding will never pass, because it no longer asserts anything,
    so its issue stays open forever. #2210 sat exactly there: the quota check was
    reading a field that does not exist, was corrected to a GAUGE, and the issue
    it had already opened became permanently unclosable.

    That is "the issues just sit there" wearing a different hat, and the fix is
    NOT to let gauges close defects — a gauge makes no claim and must never be
    read as a fix. It is to say plainly that the CLAIM was retracted, and close
    the issue as `not planned` rather than `completed`, so the distinction
    survives in the record.

    ★ Guarded by a sustained-retraction requirement, because a check that flips
    RED<->GAUGE with the runner's trial state (the quota meter does exactly this)
    must not close its own issue on the first quiet run.
    """
    withdrawn = []
    for f in run["findings"]:
        if f.get("verdict") != GAUGE:
            continue
        if int(f.get("gauge_runs") or 0) < WITHDRAW_AFTER_GAUGE_RUNS:
            continue
        marker = ISSUE_KEY_MARKER + f["key"]
        for iss in issues:
            if marker not in (iss.get("body") or ""):
                continue
            num = str(iss.get("number"))
            comment = (
                "### Withdrawn — the claim was retracted, not fixed\n\n"
                f"This check no longer asserts anything. It has reported as a "
                f"**gauge** for {f.get('gauge_runs')} consecutive runs, which "
                "means it now publishes a number without a pass/fail claim — "
                "either the original assertion was wrong, or no threshold the "
                "platform itself defines exists to fail against.\n\n"
                f"**Now reports:** {f.get('title')}\n\n"
                f"**Observed:** {f.get('evidence')}\n\n"
                "> **This is not a statement that the underlying behaviour is "
                "correct.** It is a statement that this instrument stopped "
                "claiming otherwise. If the behaviour still concerns you, the "
                "gauge is on the board and the number is above — reopen and say "
                "what 'good' should mean, and it can become an assertion again.\n\n"
                f"_Closed as `not planned` by the run at {run['generated_at']}._")
            _gh(["issue", "comment", num, "-R", C.GH_REPO, "--body-file", "-"],
                input_text=comment)
            crc, cout = _gh(["issue", "close", num, "-R", C.GH_REPO,
                             "--reason", "not planned"])
            if crc == 0:
                withdrawn.append(f"#{num} (withdrawn: {f.get('title')})")
                print(f"withdrew issue #{num} — {f['key']} is a sustained GAUGE")
            else:
                print(f"::warning::could not withdraw #{num}: {cout[:140]}")
    return withdrawn


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
        merged = merge_state(run, state, deltas)
    else:
        merged = merge_state(run, state, deltas)
        memory = save_state(merged)
    body = render(run, state, deltas, memory=memory)
    upsert_issue(body, deltas, run, state)

    # ★ AFTER the board is published, so a failure here can never cost us the
    # report — same ordering rule as the dashboard beat.
    # ★ Stamp the consecutive-gauge count from the just-merged state onto the
    #   run, so the closer can tell a one-run flap from a sustained retraction.
    #   It lives in state (it is history) but the closer reads the run.
    _gr = {k: int(v.get("gauge_runs") or 0)
           for k, v in (merged.get("findings") or {}).items()}
    for _f in run["findings"]:
        _f["gauge_runs"] = _gr.get(_f["key"], 0)

    closed = close_resolved_issues(run, deltas)

    # ★ LAST, and only after the issue is written. The GitHub issue is the
    # authoritative board; this is a convenience view hosted on the very backend
    # being probed, so its failure must never cost us the real report.
    run["memory_ok"] = memory[0]
    beat_ok, beat_note = beat_dashboard(run, merged)
    return {"body": body, "deltas": deltas, "memory_ok": memory[0],
            "dashboard_ok": beat_ok, "dashboard_note": beat_note,
            "closed_issues": closed}
