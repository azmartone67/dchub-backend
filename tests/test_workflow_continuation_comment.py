"""A `#` comment inside a `\\` continuation silently truncates the command.

WHY THIS EXISTS
───────────────
be#3203 (merged 2026-08-26 07:34:48Z) added a six-line explanatory comment to
cron-heartbeat.yml, placing it BETWEEN the `curl … \\` line and its `-H`
argument:

    BODY=$(curl -sS -X POST \\
      # r-selftraffic-ua (2026-08-26): was GH-Actions-CronHeartbeat/1.0, which
      … five more comment lines …
      -H "User-Agent: dchub-cron-heartbeat/1.0" \\
      -w "\\nHTTP_STATUS=%{http_code}\\n" \\
      https://api.dchub.cloud/api/v1/cron/heartbeat || true)

The trailing `\\` escapes the NEWLINE, joining that line to the comment — so `#`
swallows the rest of the command. curl ran with no URL at all, and the `-H …`
lines were then parsed as separate commands:

    curl: (2) no URL specified
    line 18: -H: command not found

Every run returned STATUS=000 from 07:54:22Z (last success 07:33:53Z) until it
was fixed. ★★★ The job is the ONLY driver for 38 master-tick dispatches in
routes/cron_heartbeat.py plus the grid warmer and brain warming, so this was not
a cosmetic red — and its own error text blamed a "backend outage" that was NOT
happening (the backend served 0x5xx across 500 sampled requests throughout). A
broken CALLER reporting itself as a broken CALLEE.

WHY THE EXISTING GUARD DID NOT CATCH IT
───────────────────────────────────────
tests/test_workflow_curl_guard.py polices a DIFFERENT class — an HTTP call whose
failure cannot reach a human (exit status discarded). cron-heartbeat passes that
guard correctly: it does capture `%{http_code}`. This defect is shell SYNTAX, not
error handling, and nothing covered it.

THE CONTRACT
────────────
  C1. In every `run:` block of every workflow, a line ending in a continuation
      `\\` is never followed by a comment line.
  C2. Heredoc bodies are exempt — inside `<<EOF`, `#` is data, not a comment.
  C3. This is ZERO TOLERANCE, not a ratchet. Measured 2026-08-26 across 168
      workflow files / 425 run steps: exactly ONE offender existed, and it was
      the live outage. There is no debt to freeze here.
  C4. The scan must actually observe the workflows — see tests/_scan_floors.py.
"""
from __future__ import annotations

import io
import pathlib
import re

import pytest
import yaml

WF_DIR = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"

# `<<EOF`, `<<-EOF`, `<<'EOF'`, `<<"EOF"` — capture the terminator.
_HEREDOC = re.compile(r"<<-?\s*'?\"?([A-Za-z_][A-Za-z0-9_]*)'?\"?")


def _offenders(run_text: str):
    """(line_no, continued_line, comment_line) for each C1 violation."""
    out = []
    lines = run_text.split("\n")
    terminator = None
    for i, line in enumerate(lines):
        if terminator is not None:                 # C2: inside a heredoc
            if line.strip() == terminator:
                terminator = None
            continue
        m = _HEREDOC.search(line)
        if m:
            terminator = m.group(1)
            continue
        stripped = line.rstrip()
        # `\\` at EOL is an escaped backslash, not a continuation.
        if not stripped.endswith("\\") or stripped.endswith("\\\\"):
            continue
        for nxt in lines[i + 1:]:
            if not nxt.strip():                    # blank lines keep looking
                continue
            if nxt.lstrip().startswith("#"):
                out.append((i + 1, stripped.strip(), nxt.strip()))
            break
    return out


def _run_steps():
    """(path, job_name, step_index, run_text) for every `run:` in every workflow."""
    files = sorted(WF_DIR.glob("*.yml")) + sorted(WF_DIR.glob("*.yaml"))
    for path in files:
        try:
            doc = yaml.safe_load(io.open(path, encoding="utf-8"))
        except yaml.YAMLError:
            continue                               # malformed YAML is another guard's job
        if not isinstance(doc, dict):
            continue
        for job_name, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            for idx, step in enumerate(job.get("steps") or []):
                if isinstance(step, dict) and isinstance(step.get("run"), str):
                    yield path, job_name, idx, step["run"]


# ── Tripwire ────────────────────────────────────────────────────────────────

def test_the_scan_actually_sees_the_workflows():
    """C4: ★ a scanner that finds nothing is byte-identical to a clean repo.
    Pin that this guard really walked a fleet of workflows with run blocks."""
    steps = list(_run_steps())
    assert len(list(WF_DIR.glob("*.yml"))) >= 130, "workflow glob collapsed"
    assert len(steps) >= 300, f"only {len(steps)} run-steps found — scan collapsed"


def test_detector_fires_on_the_be3203_shape():
    """★ Proves the detector can FAIL. This is the exact text that broke
    cron-heartbeat.yml; if this stops being flagged, the guard is decoration."""
    broken = (
        'BODY=$(curl -sS -X POST \\\n'
        '  # r-selftraffic-ua (2026-08-26): was GH-Actions-CronHeartbeat/1.0\n'
        '  -H "User-Agent: dchub-cron-heartbeat/1.0" \\\n'
        '  https://api.dchub.cloud/api/v1/cron/heartbeat || true)\n'
    )
    hits = _offenders(broken)
    assert len(hits) == 1, f"detector missed the known-broken shape: {hits}"
    assert hits[0][0] == 1


def test_heredoc_bodies_are_exempt():
    """C2: inside a heredoc `#` is data. A false positive here would push
    someone to 'fix' correct code."""
    ok = (
        "cat <<'EOF' > /tmp/x.py\n"
        "value = 1 \\\n"
        "# this is python source, not a shell comment\n"
        "EOF\n"
        'curl -sS "$URL"\n'
    )
    assert _offenders(ok) == []


def test_a_plain_continuation_is_not_flagged():
    """The overwhelmingly common case must stay silent."""
    ok = (
        'curl -sS \\\n'
        '  -H "A: b" \\\n'
        '  "$URL"\n'
        '# a comment AFTER the command is fine\n'
    )
    assert _offenders(ok) == []


# ── C1 / C3: the repo-wide check ────────────────────────────────────────────

def test_no_comment_inside_a_line_continuation():
    """C1 + C3: zero tolerance. One offender existed on 2026-08-26 and it was a
    live outage of 38 master-ticks."""
    bad = []
    for path, job, idx, run in _run_steps():
        for line_no, cont, comment in _offenders(run):
            bad.append(
                f"{path.name} :: job={job} step[{idx}] :: run-line {line_no}\n"
                f"        {cont}\n"
                f"        {comment}\n"
                f"    → the `\\` joins this comment to the command above it, so `#` "
                f"swallows the rest. Move the comment ABOVE the command."
            )
    assert not bad, (
        f"{len(bad)} workflow step(s) put a comment inside a line continuation:\n\n"
        + "\n".join(bad)
    )
