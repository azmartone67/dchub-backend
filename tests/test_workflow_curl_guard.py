"""A scheduled call that cannot fail is not a scheduled call.

WHY THIS EXISTS — the same defect has now shipped FOUR times in one file
(.github/workflows/data-sync.yml alone):

  r80    (2026-06-04) evolution + auto-approve fired bare curls -> 401 every
         run while the workflow stayed green. The evolution engine and
         staged-facility auto-approval never ran.
  r86f              /api/jobs/discovery + /api/jobs/auto-approve sent the admin
         key in a header the reader never checked -> 401'd silently 4x/day.
  #2318  (2026-08-06) news-refresh 401'd for 147 HOURS of green because
         `|| echo "News refresh timed out"` swallowed it. The message was also
         false: it printed 0.7s into a call with --max-time 60.
  SH52-051 (2026-08-08) energy-discovery hit our own tier gate, got 402, and
         `curl -sf` + `bash -e` killed the job on market 1 of 23. Seven steps
         after it -- including the infra-growth snapshot writer -- had not run
         in weeks. /whats-new reported "growth not measured yet" as a data
         property when it was really an unrun step.

Every one is the same shape: an HTTP call whose failure cannot reach a human.
Either curl's exit status is discarded (`|| echo`, `|| true`), or the status
is never captured at all, so a 401/402/500 renders as a green check.

★ THIS IS A RATCHET, NOT A CLEANUP. 81 of 125 workflows carry this pattern
today; rewriting them in one pass is a bigger blast radius than the bug. So
the guard records the CURRENT count and fails only when it GROWS. Fixing a
workflow lowers the baseline; adding an unguarded call fails CI with the file
name. Debt is frozen, then paid down deliberately.

A step counts as GUARDED when it does any one of:
  * captures the status  -- `-w '%{http_code}'` (then the run block can test it)
  * lets curl fail       -- `--fail` / `--fail-with-body` / `-sf`
  * checks curl's exit   -- reads `$?` or `PIPESTATUS`
Anything else is a call whose failure is invisible.
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

WF_DIR = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"

# Frozen 2026-08-08 at the measured count: 154 steps across 86 files.
# LOWER this when you fix a workflow; never raise it.
#   154 -> 153 on 2026-08-10: news-ner-discovery.yml's dead-man beat was
#   `curl -s ... || true`. Guarded (captures %{http_code}, warns on non-2xx)
#   while staying fail-open, because a beat that 401s leaves the feed reading
#   stale on the board while the job is green — the same invisibility this
#   guard exists to stop. daily-infra-sync.yml's beat, added in the same
#   change, was written guarded from the start and does not move the count.
MAX_UNGUARDED_STEPS = 153

_CURL = re.compile(r"\bcurl\b")
_GUARDS = (
    "%{http_code}",
    "--fail",
    "-sf",
    "-fsS",
    "-sfS",
    "$?",
    "PIPESTATUS",
)


def _run_blocks(path: pathlib.Path):
    """Yield (step_name, run_text) for every step with a `run:` in this file.

    Parsed as YAML rather than grepped: a `curl` inside a comment or a
    `description:` string is not a call, and counting it would make the
    baseline drift for reasons unrelated to the defect.
    """
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return
    if not isinstance(doc, dict):
        return
    for job in (doc.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                yield step.get("name") or "<unnamed>", step["run"]


def _is_unguarded(run: str) -> bool:
    """A run block that invokes curl but never lets a bad status surface."""
    # Strip shell comments: the fix commentary in data-sync.yml quotes the very
    # patterns this scans for, and matching our own postmortem would hide a
    # real regression behind a false pass.
    code = re.sub(r"(?m)^\s*#.*$", "", run)
    if not _CURL.search(code):
        return False
    return not any(g in code for g in _GUARDS)


def _survey() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for f in sorted(WF_DIR.glob("*.yml")) + sorted(WF_DIR.glob("*.yaml")):
        for name, run in _run_blocks(f):
            if _is_unguarded(run):
                found.append((f.name, name))
    return found


def test_workflow_dir_is_readable():
    """A guard that scans nothing passes everything.

    Three tests in this repo have already passed by matching the comment that
    explained the bug they were meant to catch. If the glob breaks or the
    directory moves, every assertion below becomes vacuously true — so prove
    there is a corpus before judging it.
    """
    assert WF_DIR.is_dir(), f"workflow dir missing: {WF_DIR}"
    files = list(WF_DIR.glob("*.yml"))
    assert len(files) > 50, f"only {len(files)} workflows found — glob broken?"
    steps = sum(1 for f in files for _ in _run_blocks(f))
    assert steps > 100, f"only {steps} run-steps parsed — YAML parsing broken?"


def test_detector_actually_detects():
    """The detector must fire on the real historical bug and stay quiet on the
    real fix. Asserted against the exact shapes from #2318 and SH52-051 so a
    loosened regex fails here rather than silently passing the survey."""
    # #2318: swallowed by `|| echo`, no status ever captured.
    assert _is_unguarded(
        'curl -sS --max-time 60 -X POST "$U/api/jobs/news-refresh" '
        '|| echo "News refresh timed out"')
    # The shipped fix: status captured, then tested.
    assert not _is_unguarded(
        'CODE=$(curl -sS -o /tmp/n.json -w \'%{http_code}\' -X POST "$U" || echo 000)\n'
        'if [ "$CODE" != "200" ]; then exit 1; fi')
    # SH52-051's `curl -sf` DOES let curl fail — it is guarded by this
    # definition. Its bug was `bash -e` aborting the loop, a different defect;
    # this guard must not claim credit for catching that.
    assert not _is_unguarded('RESULT=$(curl -sf --max-time 60 "$U")')
    # A step with no curl at all is not a finding.
    assert not _is_unguarded('echo hello && python3 -c "print(1)"')
    # Commented-out curl is not a call.
    assert not _is_unguarded('# curl -sS "$U" || echo nope\necho ok')


def test_no_new_unguarded_curl_steps():
    """Ratchet: the count of failure-invisible curl steps must not grow."""
    found = _survey()
    n = len(found)
    if n > MAX_UNGUARDED_STEPS:
        new = "\n".join(f"    {f} :: {s}" for f, s in found[:20])
        pytest.fail(
            f"{n} unguarded curl steps, baseline is {MAX_UNGUARDED_STEPS}.\n"
            f"A new scheduled call was added whose failure cannot reach a "
            f"human — the r80 / r86f / #2318 / SH52-051 defect.\n"
            f"Capture the status and test it:\n"
            f"    CODE=$(curl -sS -o /tmp/r.json -w '%{{http_code}}' \"$URL\" "
            f"|| echo 000)\n"
            f"    [ \"$CODE\" = \"200\" ] || {{ echo \"::error::got $CODE\"; "
            f"exit 1; }}\n"
            f"Steps:\n{new}")
    # Ratchet hygiene: a large drop means the baseline is stale and no longer
    # applying pressure. Lower MAX_UNGUARDED_STEPS to lock the win in.
    assert n >= MAX_UNGUARDED_STEPS - 25, (
        f"only {n} unguarded steps against a baseline of "
        f"{MAX_UNGUARDED_STEPS} — good, now lower the baseline to {n} so the "
        f"ratchet keeps its grip")


# ─────────────────────────────────────────────────────────────────────────────
# A comment inside a backslash continuation deletes the rest of the command.
#
# 2026-08-26, be#3203: cron-heartbeat.yml gained a six-line `#` block between
# `curl -sS -X POST \` and its `-H` line. In bash the trailing `\` escapes the
# newline, so the comment is JOINED to the command and `#` swallows everything
# after it. curl ran with no URL; the -H/-w/URL lines were then parsed as a
# separate command. Every run from 07:54:22Z was red, and because STATUS came
# back 000 the workflow announced "backend outage; 38 master-ticks + warmers
# are not running" — a claim measured false the same morning.
#
# ★ Note what did NOT catch it. `bash -n` PASSES on the broken block: the
# result is still valid bash, just a different program. The five assertions in
# tests/test_cron_heartbeat_never_green_while_dead.py passed too — every one is
# a substring check, and the file still contained every substring. Only the
# lexical adjacency of `\` and `#` distinguishes the two programs, so that is
# what this scans for.
#
# Zero tolerance, not a ratchet: the survey measured 0 sites across 168
# workflows once cron-heartbeat was repaired. There is no debt to freeze.
# ─────────────────────────────────────────────────────────────────────────────

def _continuation_comments(run: str) -> list[tuple[int, str]]:
    """Lines where a `\\` continuation is followed by a comment.

    Returns (1-based line number of the continuing line, the comment text).

    A line that is ITSELF a comment cannot continue — bash ends a comment at
    the newline whether or not it ends in a backslash — so those are skipped
    rather than reported, which is what keeps the postmortem prose in this
    very repo from matching.
    """
    hits: list[tuple[int, str]] = []
    lines = run.splitlines()
    for i, line in enumerate(lines[:-1]):
        stripped = line.rstrip()
        if stripped.lstrip().startswith("#"):
            continue                      # a comment does not continue
        if not stripped.endswith("\\") or stripped.endswith("\\\\"):
            continue                      # not a continuation
        nxt = lines[i + 1].lstrip()
        if nxt.startswith("#"):
            hits.append((i + 1, nxt[:70]))
    return hits


def test_continuation_comment_detector_actually_detects():
    """Fire on the real be#3203 shape; stay quiet on the real fix.

    A detector that is only ever run against a clean corpus is indistinguishable
    from one that returns [] unconditionally — so pin both directions here.
    """
    # The exact defect, reduced.
    broken = ('BODY=$(curl -sS -X POST \\\n'
              '  # renamed the UA on 2026-08-26\n'
              '  -H "User-Agent: dchub-cron-heartbeat/1.0" \\\n'
              '  https://api.dchub.cloud/api/v1/cron/heartbeat)')
    assert _continuation_comments(broken), "missed the be#3203 defect"

    # The shipped fix: same comment, above the command instead of inside it.
    fixed = ('# renamed the UA on 2026-08-26\n'
             'BODY=$(curl -sS -X POST \\\n'
             '  -H "User-Agent: dchub-cron-heartbeat/1.0" \\\n'
             '  https://api.dchub.cloud/api/v1/cron/heartbeat)')
    assert not _continuation_comments(fixed)

    # A continuation followed by more command is the normal case.
    assert not _continuation_comments('curl -sS \\\n  -H "A: b" \\\n  "$URL"')

    # Consecutive comment lines, one ending in `\`, are inert: the first is
    # already a comment, so it continues nothing. This shape appears in real
    # postmortem prose and must not be reported.
    assert not _continuation_comments('# see the note above \\\n# ...continued\necho ok')

    # An escaped backslash at end-of-line is a literal `\`, not a continuation.
    assert not _continuation_comments('printf "%s" "back\\\\"\n# a comment')


def test_no_comment_inside_a_line_continuation():
    """Survey: zero sites, repo-wide. See the block comment above."""
    found: list[str] = []
    for f in sorted(WF_DIR.glob("*.yml")) + sorted(WF_DIR.glob("*.yaml")):
        for name, run in _run_blocks(f):
            for ln, text in _continuation_comments(run):
                found.append(f"    {f.name} :: {name} :: run-line {ln}\n"
                             f"        -> {text}")
    if found:
        pytest.fail(
            "a `#` comment sits inside a backslash continuation — the trailing "
            "`\\` joins it to the command and `#` deletes the rest of that "
            "command (be#3203: curl ran with no URL for 2h while the workflow "
            "blamed a backend outage).\n"
            "Move the comment ABOVE the command.\n" + "\n".join(found))


def test_cron_heartbeat_curl_still_receives_its_url():
    """The behavioural half: ask what argv curl actually ends up with.

    ★ ORDER MATTERS, and getting it wrong makes this test vacuous. The first
    draft stripped comments and then joined continuations, so the comment lines
    vanished before the `\\` could glue them to the command and the broken file
    PASSED. bash does the opposite: the lexer resolves `\\`-newline first, and
    only then does `#` begin a comment. Join first, then let shlex apply comment
    and quote rules — that is the order that reproduces the bug.

    Unlike the lexical guard above, this catches ANY future edit that severs the
    URL from the curl — a stray quote, a lost `\\`, a reordered flag — because it
    asks what the command becomes, not whether a substring is still in the file.
    """
    import shlex

    wf = WF_DIR / "cron-heartbeat.yml"
    doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
    run = doc["jobs"]["ping"]["steps"][0]["run"]

    # 1. Resolve continuations exactly as the lexer does. `[ \t]*` and not
    #    `\s*`: the latter eats newlines too and would silently splice
    #    unrelated following lines into the command.
    joined = re.sub(r"\\\n[ \t]*", " ", run)

    # 2. Drop whole-line comments. The prose in this file and in the workflow
    #    both say "curl" while explaining the bug; matching our own postmortem
    #    is how a guard passes for the wrong reason.
    body = [l for l in joined.splitlines() if not l.lstrip().startswith("#")]

    calls = [l for l in body if _CURL.search(l)]
    assert calls, "no curl invocation survives shell parsing of cron-heartbeat.yml"

    # 3. shlex with comments=True applies bash's own comment and quoting rules
    #    to what the lexer produced.
    argv = shlex.split(calls[0][calls[0].index("curl"):], comments=True)

    assert "https://api.dchub.cloud/api/v1/cron/heartbeat" in argv, (
        "curl is invoked WITHOUT its URL — this is the be#3203 failure "
        f"(`curl: (2) no URL specified`). argv={argv}")
    assert "User-Agent: dchub-cron-heartbeat/1.0" in argv, (
        "the self-traffic UA is not reaching curl. The whole point of be#3203 "
        "was that GH-Actions-CronHeartbeat/1.0 matched no family in "
        "mcp_calls_deloop._SCRIPT_INTERNAL_UA, so 288 calls/day were counted "
        f"as real external demand. argv={argv}")
